#!/usr/bin/env python3
"""Offline importer regressions. All child processes use disposable HOMEs and stubs.

Run through run_import_tests.sh; never print child output or fixture contents on
failure. Test IDs correspond to MATRIX.md (subtests add variants).
"""
import io
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
S = ROOT / "skills/import-agents/scripts"
NODE = os.environ.get("MATRIX_NODE") or shutil.which("node")
PWSH = os.environ.get("PWSH") or shutil.which("pwsh")
PYTHON = sys.executable
CLIS = ("codex", "claude", "pi", "omp")
DIRS = {"codex": ".codex", "claude": ".claude", "pi": ".pi/agent", "omp": ".omp/agent"}


class ImportMatrix(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="import-matrix-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'Zoë Smith'
        self.home.mkdir(mode=0o700)
        self.stage = self.home / "stage"
        self.bin = self.home / ".local/bin"
        self.bin.mkdir(parents=True)
        self.canary = "canary-" + secrets.token_hex(16)
        self.env = {"HOME": str(self.home), "USERPROFILE": str(self.home), "USER": "fixture",
                    "PATH": str(self.bin) + ":/usr/bin:/bin", "LANG": "C.UTF-8",
                    "TMPDIR": str(self.root), "AGENT_IMPORT_STAGE": str(self.stage),
                    "PYTHONDONTWRITEBYTECODE": "1", "CANARY": self.canary}
        if NODE:
            (self.bin / "node").symlink_to(NODE)
        # No installer, store or network-capable helper can reach an actual account.
        for tool in ("npm", "npx", "uv", "uvx", "git", "security", "secret-tool", "op", "pgrep"):
            self.stub(tool, "exit 1\n")
        for cli in CLIS:
            self.assertFalse(shutil.which(cli, path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin"),
                             "unsafe ambient CLI: run in a clean test container")

    def put(self, rel, data, base=None):
        p = (base or self.home) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data if isinstance(data, bytes) else data.encode())
        p.chmod(0o600)
        return p

    def js(self, rel, data, base=None):
        return self.put(rel, json.dumps(data), base)

    def stub(self, name, body):
        p = self.put(name, "#!/bin/sh\n" + body, self.bin)
        p.chmod(0o700)
        return p

    def hide(self, *names):
        # dash cannot call its own `command` builtin from a same-named function.
        # Delegate the non-hidden lookups to a clean POSIX shell instead.
        return ('command() { case "$*" in ' + '|'.join('"-v ' + n + '"' for n in names)
                + ') return 1;; esac; /bin/sh -c \'command "$@"\' sh "$@"; }\n')

    def call(self, script, *args, rc: int | tuple[int, ...] = 0, extra=None, prefix=""):
        env = dict(self.env, **(extra or {}))
        cmd = (["/bin/sh", "-s", "--"] if script.endswith(".sh") and script != "apply_import.sh"
               else ["/bin/bash", str(S / script)] if script.endswith(".sh")
               else [PYTHON, str(S / script)])
        data = (prefix + (S / script).read_text()).encode() if cmd[1] == "-s" else None
        r = subprocess.run(cmd + list(map(str, args)), input=data, capture_output=True, env=env,
                           cwd=self.home, timeout=35)
        self.assertFalse(self.canary.encode() in r.stdout + r.stderr, "secret leaked in child output (suppressed)")
        self.assertIn(r.returncode, (rc,) if isinstance(rc, int) else rc, script + ": unexpected return code")
        try:
            return json.loads(r.stdout)
        except ValueError:
            self.fail(script + ": invalid JSON (output suppressed)")

    def detect(self, *clis, **kw):
        return self.call("detect_local.sh", "--no-login-shell", *(clis or CLIS), **kw)

    def apply(self, cli, *args, **kw):
        return self.call("apply_import.sh", cli, "--no-verify", *args, **kw)

    def bundle(self, *args, **kw):
        result = self.call("bundle_local.sh", *args, **kw)
        with tarfile.open(self.home / "agent-import.tgz") as tf:
            return result, set(tf.getnames())

    def archive(self, entries):
        p = self.root / "bundle.tar"
        with tarfile.open(p, "w") as tf:
            for name, data, kind in entries:
                m = tarfile.TarInfo(name)
                m.type = kind
                if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    m.linkname = "../../outside"
                raw = data.encode() if isinstance(data, str) else data
                m.size = len(raw) if kind == tarfile.REGTYPE else 0
                tf.addfile(m, io.BytesIO(raw))
        return p

    def db(self, rel, rows, wal=False):
        p = self.home / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        c = sqlite3.connect(p)
        c.execute("create table auth_credentials(provider text, credential_type text, data text, disabled_cause text)")
        c.commit()
        if wal:
            c.execute("pragma journal_mode=wal")
            c.execute("pragma wal_autocheckpoint=0")
        c.executemany("insert into auth_credentials values (?, 'oauth', ?, ?)",
                      [(provider, self.canary, disabled) for provider, disabled in rows])
        c.commit()
        if wal:
            self.addCleanup(c.close)
        else:
            c.close()
        return p

    def sqlite_stub(self):
        p = self.put("sqlite3", f"#!{PYTHON}\nimport sqlite3,sys\n"
                     "c=sqlite3.connect(sys.argv[-2])\n"
                     "try:\n for r in c.execute(sys.argv[-1]): print('|'.join(str(x) for x in r))\n"
                     "finally: c.close()\n", self.bin)
        p.chmod(0o700)

    def test_detect_empty(self):
        d = self.detect()
        self.assertEqual(set(d["clis"]), set(CLIS))
        for c in d["clis"].values():
            self.assertFalse(c["installed"] or c["has_auth"] or c["has_config"])
            self.assertEqual(c["login_store"], "none")

    def test_detect_installed(self):
        self.stub("uname", "echo Darwin\n")
        for c in CLIS:
            self.stub(c, 'echo "fixture 1.2.3"\n')
        d = self.detect()
        self.assertEqual(d["os"], "macos")
        for c in d["clis"].values():
            self.assertTrue(c["installed"])
            self.assertIn("1.2.3", c["version"])
            self.assertFalse(c["has_auth"])

    def test_detect_partial(self):
        for c, f in (("codex", "auth.json"), ("claude", ".credentials.json"), ("pi", "auth.json")):
            self.js(DIRS[c] + "/" + f, {})
        self.put(".omp/agent/config.yml", "tools: {}\n")
        d = self.detect()["clis"]
        for c in CLIS:
            self.assertFalse(d[c]["installed"])
            self.assertEqual(d[c]["login_store"], "none" if c == "omp" else "file")
        self.assertTrue(d["omp"]["has_config"])

    def test_pi_expiry(self):
        self.js(".pi/agent/auth.json", {"old": {"type": "oauth", "access": self.canary, "expires": 1},
                "future": {"type": "oauth", "expires": 9999999999999}, "unknown": {"type": "oauth"}})
        d = self.detect("pi")["secrets"]["inventory"]["items"]
        self.assertEqual({x["provider"]: x["expired"] for x in d}, {"old": True, "future": False, "unknown": None})

    def test_literal_inventory(self):
        self.js(".pi/agent/models.json", {"providers": {"sample": {"apiKey": self.canary}}})
        self.js(".claude/settings.json", {"env": {"ANTHROPIC_API_KEY": self.canary}})
        self.put(".codex/config.toml", f'experimental_bearer_token = "{self.canary}"\n')
        self.put(".omp/agent/models.yml", f'providers:\n  sample:\n    apiKey: {self.canary}\n')
        d = self.detect()["secrets"]["inventory"]["items"]
        self.assertEqual({x["cli"] for x in d if x["kind"] == "literal_in_config"}, set(CLIS))

    def test_bundle_auth(self):
        auth = self.home / ".codex/auth.json"
        self.put(".codex/config.toml", 'model = "fixture"\n')
        self.js(".claude/.credentials.json", {"claudeAiOauth": {"accessToken": self.canary}})
        self.js(".codex/.credentials.json", {"remote": {"token": self.canary}})
        variants = [(json.dumps({"OPENAI_API_KEY": self.canary}, indent=2), True),
                    (json.dumps({"OPENAI_API_KEY": self.canary}), True),
                    ('{"tokens":\n{"access_token":"' + self.canary + '"}}', False),
                    ('{"tokens":null,"OPENAI_API_KEY":null}', False),
                    ('{"tokens":', False), ('[]', False), ('{}', False)]
        for index, (data, static) in enumerate(variants):
            with self.subTest(variant=index):
                auth.write_text(data)
                _, members = self.bundle("codex", "claude")
                self.assertEqual(".codex/auth.json" in members, static)
                self.assertNotIn(".codex/.credentials.json", members)
                self.assertNotIn(".claude/.credentials.json", members)
        self.js(".codex/auth.json", {"tokens": {"access_token": self.canary}})
        _, members = self.bundle("--copy-login", "claude,codex", "codex", "claude")
        self.assertTrue({".codex/auth.json", ".codex/.credentials.json", ".claude/.credentials.json"} <= members)

    def test_export_invalid(self):
        self.call("export_secrets.sh", "env:OPENAI_API_KEY", "file:" + self.canary, rc=2)
        self.assertFalse((self.home / ".agent-import-secrets").exists())

    def test_resolve_success(self):
        self.js(".pi/agent/models.json", {"providers": {"sample": {"apiKey": "op://vault/key"}}})
        self.stub("op", 'printf "%s" "$CANARY"\n')
        d = self.call("export_secrets.sh", "resolve:pi:models.json:sample=TEST_KEY")
        self.assertEqual(d["items"][0]["status"], "ok")
        self.assertTrue(self.canary in (self.home / ".agent-import-secrets/secrets.env").read_text())
        self.assertEqual((self.home / ".agent-import-secrets/secrets.env").stat().st_mode & 0o777, 0o600)

    def test_resolve_failure(self):
        for mode in ("failed", "empty", "multiline"):
            with self.subTest(mode=mode):
                body = {'failed': 'echo "$CANARY"; exit 1\n', 'empty': 'exit 0\n',
                        'multiline': 'printf "%s\\nsecond\\n" "$CANARY"\n'}[mode]
                self.stub("op", body)
                self.js(".pi/agent/auth.json", {"sample": {"key": "!op read vault"}})
                d = self.call("export_secrets.sh", "resolve:pi:auth.json:sample=TEST_KEY")
                self.assertNotEqual(d["items"][0]["status"], "ok")
                self.assertFalse((self.home / ".agent-import-secrets/secrets.env").exists())
                self.assertFalse((self.home / ".agent-import-secrets/rewrites.txt").exists())

    def test_keychain(self):
        self.stub("uname", "echo Darwin\n")
        self.stub("security", 'case " $* " in *" -w "*) printf \'{"access":"%s"}\\n\' "$CANARY";; *) exit 0;; esac\n')
        self.assertEqual(self.detect("claude")["clis"]["claude"]["login_store"], "keychain")
        self.assertEqual(self.call("export_secrets.sh", "keychain:claude")["items"][0]["status"], "ok")
        for body in ('printf \'{"access":"%s"}\\n\' "$CANARY"; exit 1\n',
                     'printf \'{malformed %s\\n\' "$CANARY"\n', 'exit 44\n'):
            self.stub("security", body)
            d = self.call("export_secrets.sh", "keychain:claude")
            self.assertNotEqual(d["items"][0]["status"], "ok")
            self.assertFalse((self.home / ".agent-import-secrets/claude/.credentials.json").exists())

    def test_keyring(self):
        self.put(".codex/config.toml", 'cli_auth_credentials_store = "keyring"\n')
        self.stub("secret-tool", 'echo called >> "$HOME/store-calls"\n'
                  'case "$*" in *username*) exit 1;; *) printf \'{"token":"%s"}\\n\' "$CANARY";; esac\n')
        self.assertEqual(self.detect("codex")["clis"]["codex"]["login_store"], "none")
        self.assertFalse((self.home / "store-calls").exists())
        self.assertEqual(self.detect("codex", extra={"DBUS_SESSION_BUS_ADDRESS": "fixture"})["clis"]["codex"]["login_store"], "keyring")
        d = self.call("export_secrets.sh", "keychain:codex")
        self.assertNotEqual(d["items"][0]["status"], "ok")
        self.stub("secret-tool", 'printf \'{"token":"%s"}\\n\' "$CANARY"\n')
        self.assertEqual(self.call("export_secrets.sh", "keychain:codex")["items"][0]["status"], "ok")

    def test_env_roundtrip(self):
        value = self.canary + "' $NOT_EXPANDED `touch nope` $(touch nope)"
        self.call("export_secrets.sh", "--no-login-shell", "env:TEST_KEY", extra={"TEST_KEY": value})
        self.put("_secrets/secrets.env", (self.home / ".agent-import-secrets/secrets.env").read_bytes(), self.stage)
        self.call("apply_import.sh", "keys")
        r = subprocess.run(["/bin/sh", "-c", '. "$HOME/.agent-keys"; [ "$TEST_KEY" = "$EXPECTED" ]'],
                           env=dict(self.env, EXPECTED=value), cwd=self.home, capture_output=True, timeout=5)
        self.assertEqual(r.returncode, 0)
        self.assertFalse(r.stdout or r.stderr)
        self.assertFalse((self.home / "nope").exists())

    def test_shell_env(self):
        # Stub startup avoids /etc/profile, which can replace HOME in CI containers.
        shell = self.stub("login-shell", 'printf "noise %s\\n" "$CANARY"\n'
                          'export DEEPSEEK_API_KEY="$CANARY"\n/bin/sh -c "$2"\n')
        d = self.call("detect_local.sh", "pi", extra={"SHELL": str(shell)})
        self.assertEqual(d["secrets"]["env_set"]["DEEPSEEK_API_KEY"], "login_shell")
        self.assertNotIn("DEEPSEEK_API_KEY", self.detect("pi", extra={"SHELL": str(shell)})["secrets"]["env_set"])
        d = self.call("export_secrets.sh", "env:DEEPSEEK_API_KEY", extra={"SHELL": str(shell)})
        self.assertEqual(d["items"][0]["status"], "ok")

    def test_keys_idempotent(self):
        self.put(".agent-keys", "# keep comment\nexport OTHER_KEY='keep'\nexport TEST_KEY='old'\nTEST_KEY=duplicate\n")
        data = "export TEST_KEY='" + self.canary + "'\n"
        self.put("_secrets/secrets.env", data, self.stage)
        before = (self.home / ".agent-keys").read_bytes()
        self.call("apply_import.sh", "keys", "--dry-run", rc=4)
        self.call("apply_import.sh", "keys", rc=4)
        self.assertTrue(before == (self.home / ".agent-keys").read_bytes())
        result = self.call("apply_import.sh", "keys", "--overwrite")
        backup = Path(result["agent_keys"]["backups"][0])
        self.assertTrue(backup.read_bytes() == before)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        p = self.home / ".agent-keys"
        first = p.read_bytes()
        self.assertEqual(first.count(b"TEST_KEY="), 1)
        self.assertIn(b"# keep comment", first)
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.stage / "_secrets/secrets.env").exists())
        self.put("_secrets/secrets.env", data, self.stage)
        self.call("apply_import.sh", "keys")
        self.assertTrue(p.read_bytes() == first)
        self.assertEqual((self.home / ".bashrc").read_text().count("[ -f"), 1)

    def test_keys_invalid(self):
        self.put("_secrets/secrets.env", "export BAD=$(touch \"$HOME/nope\")\n" + self.canary + "\n", self.stage)
        d = self.call("apply_import.sh", "keys")
        self.assertEqual(d["agent_keys"]["rejected_lines"], 2)
        self.assertFalse((self.home / "nope").exists())

    def test_legacy_auth(self):
        self.js(".pi/agent/oauth.json", {"token": self.canary})
        self.js(".pi/agent/pkg-token.json", {"token": self.canary})
        self.put(".pi/agent/.env", "KEY=" + self.canary)
        _, members = self.bundle("pi")
        self.assertIn(".pi/agent/pkg-token.json", members)
        self.assertNotIn(".pi/agent/oauth.json", members)
        self.assertNotIn(".pi/agent/.env", members)
        _, members = self.bundle("--include", ".pi/agent/.env", "pi")
        self.assertIn(".pi/agent/.env", members)

    def test_conflicts(self):
        self.put(".pi/agent/AGENTS.md", "old")
        self.put("pi/AGENTS.md", "new", self.stage)
        self.put("_secrets/secrets.env", "export KEY='" + self.canary + "'\n", self.stage)
        for flags in ((), ("--dry-run",)):
            self.apply("pi", *flags, rc=4)
            self.assertEqual((self.home / ".pi/agent/AGENTS.md").read_text(), "old")
            self.assertFalse((self.home / ".agent-keys").exists())
            self.assertFalse(list(self.home.rglob("*.bak.*")))

    def test_backups(self):
        p = self.put(".pi/agent/AGENTS.md", "zero")
        for text in ("one", "two"):
            self.put("pi/AGENTS.md", text, self.stage)
            self.apply("pi", "--overwrite")
        backups = list(p.parent.glob("AGENTS.md.bak.*"))
        self.assertEqual({b.read_text() for b in backups}, {"zero", "one"})
        self.assertTrue(all(b.stat().st_mode & 0o777 == 0o600 for b in backups))

    def test_idempotent_apply(self):
        self.put("pi/AGENTS.md", "same", self.stage)
        self.apply("pi", "--dry-run")
        self.assertFalse((self.home / ".pi").exists())
        self.apply("pi", "--keep-stage")
        self.assertTrue((self.stage / "pi/AGENTS.md").exists())
        d = self.apply("pi")
        self.assertIn("identical on box", d["skipped"])
        self.assertFalse(d["backups"])
        self.assertFalse((self.stage / "pi").exists())

    def claude_fixture(self, same=False):
        credentials = {"claudeAiOauth": {"accessToken": self.canary}}
        self.js("claude/.credentials.json", credentials, self.stage)
        if same:
            self.js(".claude/.credentials.json", credentials)
        self.js("claude/_home/.claude.json", {"oauthAccount": {"accountUuid": "source"},
                "hasCompletedOnboarding": True, "userID": self.canary,
                "mcpServers": {"sample": {"command": "node"}}}, self.stage)
        self.js(".claude.json", {"keepBox": True})

    def test_claude_merge(self):
        self.claude_fixture()
        self.apply("claude")
        d = json.loads((self.home / ".claude.json").read_text())
        self.assertTrue(d["keepBox"] and d["hasCompletedOnboarding"])
        self.assertEqual(d["oauthAccount"]["accountUuid"], "source")
        self.assertNotIn("userID", d)

    def test_claude_identical_login(self):
        self.claude_fixture(same=True)
        self.apply("claude", "--keep-stage")
        p = self.home / ".claude.json"
        self.assertEqual(json.loads(p.read_text())["oauthAccount"]["accountUuid"], "source")
        before = p.stat().st_mtime_ns
        self.apply("claude")
        self.assertEqual(p.stat().st_mtime_ns, before)

    def test_claude_conflict(self):
        self.claude_fixture()
        self.js(".claude.json", {"mcpServers": {"sample": {"command": "box-tool"}}})
        self.put("claude/CLAUDE.md", "new", self.stage)
        self.apply("claude", rc=4)
        self.assertFalse((self.home / ".claude/CLAUDE.md").exists())
        self.assertFalse((self.home / ".claude/.credentials.json").exists())
        self.assertFalse(list(self.home.rglob("*.bak.*")))

    def test_codex_stores(self):
        for store in ("keyring", "auto", "ephemeral"):
            with self.subTest(store=store):
                self.put("codex/config.toml", f'cli_auth_credentials_store = "{store}"\n[profile]\ncli_auth_credentials_store = "keep"\n', self.stage)
                self.apply("codex", "--overwrite")
                import tomllib
                d = tomllib.loads((self.home / ".codex/config.toml").read_text())
                self.assertEqual(d["cli_auth_credentials_store"], "file")
                self.assertEqual(d["mcp_oauth_credentials_store"], "file")
                self.assertEqual(d["profile"]["cli_auth_credentials_store"], "keep")

    def test_codex_mcp(self):
        self.put("codex/config.toml", '[mcp_servers.mac]\nenabled = true\ncommand = "/Applications/Missing/app"\n', self.stage)
        self.apply("codex")
        import tomllib
        cfg = tomllib.loads((self.home / ".codex/config.toml").read_text())
        self.assertFalse(cfg["mcp_servers"]["mac"]["enabled"])

    def test_omp_disabled(self):
        self.db(".omp/agent/agent.db", [("provider", None)])
        self.db("stage/omp/agent.db", [("provider", "refresh_token_reused")])
        self.put("omp/config.yml", "tools: {}\n", self.stage)
        before = (self.home / ".omp/agent/agent.db").read_bytes()
        d = self.apply("omp", "--overwrite", rc=5)
        self.assertEqual(d["omp_auth"]["action"], "kept_box_login")
        self.assertTrue(before == (self.home / ".omp/agent/agent.db").read_bytes())
        self.assertTrue((self.home / ".omp/agent/config.yml").exists())

    def test_omp_corrupt(self):
        self.db(".omp/agent/agent.db", [("provider", None)])
        before = (self.home / ".omp/agent/agent.db").read_bytes()
        self.put("omp/agent.db", self.canary, self.stage)
        self.apply("omp", "--overwrite", "--keep-stage", rc=5)
        self.assertTrue(before == (self.home / ".omp/agent/agent.db").read_bytes())
        self.apply("omp", "--overwrite", "--force-auth")
        self.assertTrue((self.home / ".omp/agent/agent.db").read_text() == self.canary)

    def test_omp_wal(self):
        self.sqlite_stub()
        p = self.db(".omp/agent/agent.db", [("mixed", "disabled"), ("mixed", None), ("dead", "disabled")], wal=True)
        files = list(p.parent.glob("agent.db*"))
        before = {f.name: f.read_bytes() for f in files}
        d = self.detect("omp")["clis"]["omp"]
        self.assertEqual(d["auth_status"], {"dead": "disabled", "mixed": "usable"})
        self.assertTrue(d["importable_auth"])
        self.assertTrue(all(f.read_bytes() == before[f.name] for f in files))
        for f in files:
            self.put("omp/" + f.name, f.read_bytes(), self.stage)
        # Distinct destination, so apply cannot be satisfied by identical source bytes.
        shutil.move(str(p.parent), str(self.home / "source-db"))
        self.apply("omp")
        c = sqlite3.connect(p)
        try:
            self.assertEqual(c.execute("select count(*) from auth_credentials").fetchone()[0], 3)
        finally:
            c.close()

    def test_omp_sidecar_symlink(self):
        self.db(".omp/agent/agent.db", [("old", None)])
        self.db("stage/omp/agent.db", [("new", None)])
        target = self.put("outside-db", "keep")
        before = (self.home / ".omp/agent/agent.db").read_bytes()
        for suffix in ("-wal", "-shm"):
            link = self.home / (".omp/agent/agent.db" + suffix)
            link.symlink_to(target)
            self.apply("omp", "--overwrite", rc=2)
            self.assertTrue((self.home / ".omp/agent/agent.db").read_bytes() == before)
            self.assertEqual(target.read_text(), "keep")
            link.unlink()

    def test_omp_stale_wal(self):
        self.db(".omp/agent/agent.db", [("old", None)])
        self.db("stage/omp/agent.db", [("new", None)])
        for suffix in ("-wal", "-shm"):
            self.put(".omp/agent/agent.db" + suffix, b"stale")
        self.apply("omp", "--overwrite")
        for suffix in ("-wal", "-shm"):
            self.assertFalse((self.home / (".omp/agent/agent.db" + suffix)).exists())
            self.assertEqual(len(list((self.home / ".omp/agent").glob("agent.db" + suffix + ".bak.*"))), 1)

    def test_missing_sqlite(self):
        self.db(".omp/agent/agent.db", [("provider", None)])
        prefix = self.hide("sqlite3")
        d = self.detect("omp", prefix=prefix)["clis"]["omp"]
        self.assertEqual(d["auth_status"], {})
        self.assertIn("sqlite3", d["note"])

    def test_linked_skills(self):
        p = self.put("shared/日本語 skill/SKILL.md", "skill")
        d = self.home / ".codex/skills"
        d.mkdir(parents=True)
        (d / "linked").symlink_to(p.parent)
        (d / "dangling").symlink_to(self.home / "missing")
        self.assertEqual(self.detect("codex")["clis"]["codex"]["symlinks"], 2)
        _, names = self.bundle("codex")
        self.assertIn(".codex/skills/linked/SKILL.md", names)
        self.assertFalse(any("dangling" in n for n in names))
        self.call("stage_tar.py", self.home / "agent-import.tgz")
        box = self.root / "box"
        box.mkdir()
        self.apply("codex", extra={"HOME": str(box)})
        self.assertEqual((box / ".codex/skills/linked/SKILL.md").read_text(), "skill")

    def test_tar_safety(self):
        entries = [(".codex/AGENTS.md", "safe", tarfile.REGTYPE)]
        for name in ("../outside", "/outside", ".codex/../outside", ".codex/._x", ".codex/.DS_Store",
                     ".codex/skills/._metadata/file", ".codex/skills/__pycache__/x.pyc"):
            entries.append((name, "ignored", tarfile.REGTYPE))
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE, tarfile.CHRTYPE):
            entries.append((".codex/special-" + kind.decode(), "", kind))
        self.call("stage_tar.py", self.archive(entries))
        self.assertEqual([p.relative_to(self.stage).as_posix() for p in self.stage.rglob("*") if p.is_file()], ["codex/AGENTS.md"])

    def test_stage_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        self.stage.mkdir()
        (self.stage / "codex").symlink_to(outside)
        self.call("stage_tar.py", self.archive([(".codex/AGENTS.md", "bad", tarfile.REGTYPE)]), rc=2)
        self.assertFalse(list(outside.iterdir()))

    def test_destination_symlink(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.home / ".codex").symlink_to(outside)
        self.put("codex/AGENTS.md", "bad", self.stage)
        self.apply("codex", rc=2)
        self.assertFalse(list(outside.iterdir()))
        (self.home / ".codex").unlink()
        target = self.put("sentinel", "keep", outside)
        dest = self.home / ".codex/AGENTS.md"
        dest.parent.mkdir()
        for point in (target, outside / "dangling"):
            dest.symlink_to(point)
            self.apply("codex", rc=2)
            self.assertEqual(target.read_text(), "keep")
            self.assertFalse((outside / "dangling").exists())
            dest.unlink()
        # An old predictable temp name cannot clobber its target.
        (dest.parent / "AGENTS.md.import-tmp").symlink_to(target)
        self.apply("codex")
        self.assertEqual(target.read_text(), "keep")

    def test_manifest_roots(self):
        for root in (".ssh", ".aws", "../escape", "/absolute", ".config/google-chrome"):
            with self.subTest(root=root):
                entries = [(".agent-import-manifest.txt", "home " + root + "\n", tarfile.REGTYPE),
                           (root + "/secret", self.canary, tarfile.REGTYPE)]
                self.call("stage_tar.py", self.archive(entries))
                self.assertFalse(any(p.is_file() for p in (self.stage / "pi").rglob("*")))
        self.put("pi/_home/.ssh/id_rsa", self.canary, self.stage)
        self.apply("pi")
        self.assertFalse((self.home / ".ssh").exists())

    def test_overrides(self):
        codex, claude = self.home / "custom codex", self.home / "custom claude"
        env = {"CODEX_HOME": str(codex), "CLAUDE_CONFIG_DIR": str(claude)}
        self.js("auth.json", {"OPENAI_API_KEY": self.canary}, codex)
        self.js("settings.json", {}, claude)
        self.js(".claude.json", {"mcpServers": {"relocated": {"url": "https://example.invalid"}}}, claude)
        d = self.detect("codex", "claude", extra=env)
        self.assertTrue(d["clis"]["codex"]["has_auth"])
        self.assertTrue(any("relocated" in x["where"] for x in d["secrets"]["inventory"]["items"]))
        for cli in ("codex", "claude"):
            self.call("bundle_local.sh", cli, extra=env, rc=2)
        self.stub("secret-tool", 'printf \'{"token":"%s"}\\n\' "$CANARY"\n')
        self.assertEqual(self.call("export_secrets.sh", "keychain:codex", extra=env)["items"][0]["status"], "ok")
        self.put("codex/AGENTS.md", "instructions", self.stage)
        self.js("claude/_home/.claude.json", {"mcpServers": {"new": {"url": "https://example.invalid"}}}, self.stage)
        self.apply("codex", extra=env)
        self.apply("claude", extra=env)
        self.assertTrue((codex / "AGENTS.md").is_file())
        self.assertIn("new", json.loads((claude / ".claude.json").read_text())["mcpServers"])
        self.assertFalse((self.home / ".claude.json").exists())
        p = self.call("parity_check.py", "--cli", "claude", "--no-run", extra=env, rc=(0, 1))
        self.assertTrue(any("relocated" in g["item"] for g in p["claude"]["auth"]))

    def test_pi_override(self):
        dest = self.home / "custom pi"
        env = {"PI_CODING_AGENT_DIR": str(dest)}
        self.js("auth.json", {"sample": {"key": "!op read vault"}}, dest)
        self.stub("op", 'printf "%s" "$CANARY"\n')
        self.assertTrue(self.detect("pi", extra=env)["clis"]["pi"]["has_auth"])
        self.assertEqual(self.call("export_secrets.sh", "resolve:pi:auth.json:sample=TEST_KEY", extra=env)["items"][0]["status"], "ok")
        self.call("bundle_local.sh", "pi", extra=env, rc=2)
        self.put("pi/AGENTS.md", "override", self.stage)
        self.apply("pi", extra=env)
        self.assertEqual((dest / "AGENTS.md").read_text(), "override")
        self.assertFalse((self.home / ".pi").exists())
        self.js("auth.json", {"sample": {"key": "UNSET_KEY"}}, dest)
        p = self.call("parity_check.py", "--cli", "pi", "--no-run", extra=env)
        self.assertIn("UNSET_KEY", json.dumps(p))

    def test_windows_paths(self):
        source = 'C:\\Users\\Zoë Smith'
        d = self.call("import_plan.py", "--os", "windows", "--home", source, "--cli", "codex", "--json")
        self.assertIn(source + '\\.codex\\config.toml', json.dumps(d, ensure_ascii=False).replace('\\\\', '\\'))
        self.js("pi/settings.json", {"cwd": source + '\\work', "forward": source.replace('\\', '/') + '/work'}, self.stage)
        self.apply("pi", "--src-home", source)
        data = json.loads((self.home / ".pi/agent/settings.json").read_text())
        self.assertTrue(all(v.startswith(str(self.home)) for v in data.values()))

    def test_quoted_home(self):
        box = self.root / 'Zoë \'quoted\' "double"'
        box.mkdir()
        self.js("pi/settings.json", {"cwd": "/Users/source/work"}, self.stage)
        self.apply("pi", "--src-home", "/Users/source", extra={"HOME": str(box)})
        self.assertEqual(json.loads((box / ".pi/agent/settings.json").read_text())["cwd"], str(box) + "/work")

    def test_scanner_fallbacks(self):
        self.js(".pi/agent/auth.json", {"sample": {"key": self.canary}})
        d = self.detect("pi", prefix=self.hide("node", "bun"))
        self.assertEqual(d["secrets"]["scanner"], "none")
        self.assertTrue(d["clis"]["pi"]["has_auth"])
        self.stub("bun", 'exec "$MATRIX_NODE" "$@"\n')
        d = self.detect("pi", prefix=self.hide("node"), extra={"MATRIX_NODE": NODE})
        self.assertEqual(d["secrets"]["scanner"], "bun")
        self.assertTrue(d["secrets"]["inventory"]["items"])
        self.stub("uname", "echo Darwin\n")
        # Dispatch-only JXA emulation: native Foundation is deliberately not claimed.
        self.stub("osascript", 'shift 2; exec "$MATRIX_NODE" "$@"\n')
        d = self.detect("pi", prefix=self.hide("node", "bun"), extra={"MATRIX_NODE": NODE})
        self.assertEqual(d["secrets"]["scanner"], "jxa")
        self.stub("op", 'printf "%s" "$CANARY"\n')
        self.js(".pi/agent/auth.json", {"sample": {"key": "op://vault/key"}})
        d = self.call("export_secrets.sh", "resolve:pi:auth.json:sample=TEST_KEY",
                      prefix=self.hide("node", "bun"), extra={"MATRIX_NODE": NODE})
        self.assertEqual(d["items"][0]["status"], "ok")

    def test_version_output(self):
        import time
        self.stub("pi", 'echo "fixture 1.2.3 $CANARY"\n')
        self.assertEqual(self.detect("pi")["clis"]["pi"]["version"], "1.2.3")
        self.stub("pi", 'echo "diagnostic $CANARY" >&2; exit 1\n')
        self.assertEqual(self.detect("pi")["clis"]["pi"]["version"], "")
        self.stub("pi", 'exec sleep 30\n')
        start = time.monotonic()
        self.assertEqual(self.detect("pi")["clis"]["pi"]["version"], "")
        self.assertLess(time.monotonic() - start, 15)

    def test_bundle_skip(self):
        self.js(".pi/agent/auth.json", {"sample": {"key": self.canary}})
        self.put(".pi/agent/skills/space name/SKILL.md", "omit")
        self.put(".pi/agent/AGENTS.md", "keep")
        _, names = self.bundle("--skip", ".pi/agent/auth.json", "--skip", ".pi/agent/skills/space name/", "pi")
        self.assertNotIn(".pi/agent/auth.json", names)
        self.assertNotIn(".pi/agent/skills/space name/SKILL.md", names)
        self.assertIn(".pi/agent/AGENTS.md", names)

    def test_apply_filters(self):
        self.put("codex/skills/ok/SKILL.md", "keep", self.stage)
        for rel in ("sessions/log", "skills/._dir/file", "skills/.DS_Store", "skills/x.pyc", "skills/__pycache__/x"):
            self.put("codex/" + rel, "omit", self.stage)
        outside = self.put("outside", self.canary)
        (self.stage / "codex/skills/link").symlink_to(outside)
        os.mkfifo(self.stage / "codex/skills/pipe")
        self.apply("codex")
        dest = self.home / ".codex"
        self.assertEqual([p.relative_to(dest).as_posix() for p in dest.rglob("*") if p.is_file()], ["skills/ok/SKILL.md"])
        self.assertTrue(outside.read_text() == self.canary)

    def test_large_binary(self):
        binary = b'\xff\x00\xfe' + self.canary.encode()
        self.put("pi/skills/binary/data.bin", binary, self.stage)
        big = self.put("pi/skills/huge", b"", self.stage)
        with big.open("wb") as f:
            f.truncate(201 * 1024 * 1024)
        self.apply("pi")
        self.assertTrue((self.home / ".pi/agent/skills/binary/data.bin").read_bytes() == binary)
        self.assertFalse((self.home / ".pi/agent/skills/huge").exists())
        # Sparse tar: oversized payload is skipped without reading/allocating it.
        p = self.root / "large.tar"
        size = 201 * 1024 * 1024
        m = tarfile.TarInfo(".pi/agent/skills/huge")
        m.size = size
        with p.open("wb") as f:
            f.write(m.tobuf())
            f.seek(size, 1)
            f.write(b"\0" * 1024)
        d = self.call("stage_tar.py", p)
        self.assertEqual(d["skipped"]["too large"], 1)
        self.assertFalse((self.stage / "pi/skills/huge").exists())

    def test_package_bundle(self):
        package = self.home / "projects/local package"
        self.js("package.json", {"name": "fixture", "version": "1.0.0"}, package)
        self.put("index.js", "// source", package)
        for rel in ("node_modules/x/file", ".venv/lib/x", ".git/config"):
            self.put(rel, "omit", package)
        self.js(".pi/agent/settings.json", {"packages": [str(package)]})
        _, names = self.bundle("pi")
        self.assertIn("projects/local package/index.js", names)
        self.assertFalse(any("node_modules" in p or ".venv" in p or ".git" in p for p in names))
        self.call("stage_tar.py", self.home / "agent-import.tgz")
        box = self.root / "box"
        box.mkdir()
        self.apply("pi", "--src-home", str(self.home), extra={"HOME": str(box)})
        self.assertEqual((box / "projects/local package/index.js").read_text(), "// source")

    def test_forbidden_links(self):
        self.put(".codex/AGENTS.md", "keep")
        for i, rel in enumerate((".ssh/id_rsa", ".aws/credentials", ".gnupg/private", ".config/gcloud/key",
                                 ".mozilla/profile/cookies", "Library/Keychains/login", "AppData/Local/Chrome/Cookies",
                                 ".agent-keys", ".claude/.credentials.json")):
            p = self.put(rel, self.canary)
            link = self.home / (".codex/skills/leak" + str(i))
            link.parent.mkdir(parents=True, exist_ok=True)
            link.symlink_to(p)
        self.put("external", self.canary, self.root)
        (self.home / ".codex/skills/outside").symlink_to(self.root / "external")
        _, names = self.bundle("codex")
        self.assertFalse(any("skills/" in n for n in names))

    def test_bundle_failure(self):
        self.put(".codex/AGENTS.md", "keep")
        self.put(".agent-import-secrets/secrets.env", "export TEST_KEY='" + self.canary + "'\n")
        self.stub("tar", 'printf "%s" "$CANARY" > "$2"; echo "$CANARY" >&2; exit 2\n')
        d = self.call("bundle_local.sh", "codex", rc=1)
        self.assertFalse(d["ok"])
        self.assertFalse((self.home / "agent-import.tgz").exists())
        self.assertTrue((self.home / ".agent-import-secrets/secrets.env").is_file())

    def test_diagnostics(self):
        self.stub("npm", 'echo "$CANARY" >&2; exit 1\n')
        self.js("pi/extensions/sample/package.json", {"dependencies": {"fixture": "1.0.0"}}, self.stage)
        d = self.apply("pi")
        self.assertFalse(d["deps_installed"][0]["ok"])
        self.put("codex/config.toml", 'model = "unterminated ' + self.canary, self.stage)
        self.apply("codex")
        # No parser exception payload, even when the invalid value is itself a key.
        self.js("claude/settings.json", {"enabledPlugins": {"sample": True}, "extraKnownMarketplaces": []}, self.stage)
        self.stub("claude", 'echo "$CANARY"; exit 1\n')
        self.apply("claude")
        self.js(".pi/agent/settings.json", {"packages": []})
        self.stub("pi", 'echo "Error: $CANARY" >&2; exit 1\n')
        p = self.call("parity_check.py", "--cli", "pi", rc=1)
        self.assertFalse(p["pi"]["ok"])
        self.put("_meta/manifest.txt", "version pi " + "9" * 5000 + ".1.0 " + self.canary + "\n", self.stage)
        self.put("pi/AGENTS.md", "safe", self.stage)
        d = self.apply("pi")
        self.assertEqual(d["post_install"]["version"]["computer"], "unknown")

    def test_pi_overlay(self):
        base = self.home / "runtime/node_modules"
        for name, version in (("@fixture/main", "1.0.0"), ("@fixture/core", "1.0.0"), ("other", "2.0.0")):
            self.js("package.json", {"name": name, "version": version}, base / name)
            self.put("index.js", "box", base / name)
            self.js("pi/npm/node_modules/" + name + "/package.json", {"name": name, "version": "1.0.0"}, self.stage)
            self.put("pi/npm/node_modules/" + name + "/index.js", "patch", self.stage)
        self.stub("pi", 'printf "  npm:@fixture/main\\n    %s/runtime/node_modules/@fixture/main\\n" "$HOME"\n')
        d = self.apply("pi")
        self.assertEqual({x["package"] for x in d["post_install"]["npm_overlay"]}, {"@fixture/main", "@fixture/core"})
        self.assertEqual((base / "@fixture/main/index.js").read_text(), "patch")
        self.assertEqual((base / "@fixture/core/index.js").read_text(), "patch")
        self.assertEqual((base / "other/index.js").read_text(), "box")

    def test_overlay_symlinks(self):
        base = self.home / "runtime/node_modules/fixture"
        self.js("package.json", {"name": "fixture", "version": "1.0.0"}, base)
        self.js("pi/npm/node_modules/fixture/package.json", {"name": "fixture", "version": "1.0.0"}, self.stage)
        self.stub("pi", 'printf "  npm:fixture\\n    %s/runtime/node_modules/fixture\\n" "$HOME"\n')
        target = self.put("outside", "keep")
        (base / "index.js").symlink_to(target)
        self.put("pi/npm/node_modules/fixture/index.js", "patch", self.stage)
        self.apply("pi", "--keep-stage")
        self.assertEqual(target.read_text(), "keep")
        (base / "index.js").unlink()
        (self.stage / "pi/npm/node_modules/fixture/index.js").unlink()
        (self.stage / "pi/npm/node_modules/fixture/index.js").symlink_to(target)
        self.apply("pi")
        self.assertFalse((base / "index.js").exists())

    def test_pi_venv(self):
        root = "projects/local package"
        self.js("pi/_home/" + root + "/package.json", {"name": "fixture"}, self.stage)
        self.put("_meta/manifest.txt", "home " + root + "\nvenv " + root + " 3.11\npip " + root + " fixture==1.0\n", self.stage)
        self.stub("pi", "exit 0\n")
        self.stub("uv", 'echo "$*" >> "$HOME/uv-calls"\n'
                  'if [ "$1" = venv ]; then mkdir -p .venv/bin; : > .venv/bin/python; fi\n')
        d = self.apply("pi")
        self.assertTrue(d["post_install"]["venvs"][0]["ok"])
        self.assertEqual((self.home / root / ".venv/import-requirements.txt").read_text(), "fixture==1.0\n")
        self.assertEqual((self.home / "uv-calls").read_text().count("\n"), 2)

    def test_claude_plugins(self):
        old, sha = "a" * 40, "b" * 40
        market = self.home / "market"
        (market / ".git").mkdir(parents=True)
        box = self.home / ".claude/plugins"
        (box / "cache/fixture/sample/old").mkdir(parents=True)
        self.js("known_marketplaces.json", {"fixture": {"installLocation": str(market)}}, box)
        self.js("installed_plugins.json", {"plugins": {"sample@fixture": [{"version": "old", "gitCommitSha": old,
                "installPath": str(box / "cache/fixture/sample/old") }]}}, box)
        self.js("claude/plugins/installed_plugins.json", {"plugins": {"sample@fixture": [{"version": "1.0.0", "gitCommitSha": sha}]}}, self.stage)
        self.stub("claude", "exit 0\n")
        tool = self.stub("fixture-hud", "exit 0\n")
        self.js("claude/settings.json", {"enabledPlugins": {"sample@fixture": True},
                "statusLine": {"command": '\"' + str(self.home / "missing tools/fixture-hud") + '\" --status'}}, self.stage)
        archive = self.archive([("plugins/sample/index.js", "pinned", tarfile.REGTYPE)])
        self.stub("git", 'case "$3" in\n cat-file) exit 0;;\n'
                  ' show) echo \'{"plugins":[{"name":"sample","source":"./plugins/sample"}]}\';;\n'
                  ' archive) cat "$FIXTURE_ARCHIVE";;\n *) exit 1;;\nesac\n')
        d = self.apply("claude", extra={"FIXTURE_ARCHIVE": str(archive)})
        self.assertEqual(d["post_install"]["plugins_pinned"][0]["sha"], sha[:12])
        self.assertEqual((box / "cache/fixture/sample/1.0.0/index.js").read_text(), "pinned")
        self.assertEqual(json.loads((box / "installed_plugins.json").read_text())["plugins"]["sample@fixture"][0]["gitCommitSha"], sha)
        command = json.loads((self.home / ".claude/settings.json").read_text())["statusLine"]["command"]
        import shlex
        self.assertEqual(shlex.split(command), [str(tool), "--status"])

    def test_dependency_install(self):
        self.js("pi/extensions/sample/package.json", {"dependencies": {"fixture": "1.0.0"}}, self.stage)
        self.js("pi/extensions/sample/package-lock.json", {}, self.stage)
        self.js("pi/packages/local/package.json", {"devDependencies": {"fixture": "1.0.0"}}, self.stage)
        self.stub("npm", 'echo "$PWD|$*" >> "$HOME/npm-calls"\n'
                  'echo "$CANARY" >&2\n[ "$1" != ci ]\n')
        d = self.apply("pi")
        self.assertTrue(all(x["ok"] for x in d["deps_installed"]))
        calls = (self.home / "npm-calls").read_text().splitlines()
        self.assertEqual(len(calls), 3)
        self.assertTrue(all("--omit=dev" in c for c in calls if "/extensions/" in c))
        self.assertTrue(all("--omit=dev" not in c for c in calls if "/packages/" in c))

    def test_verify_status(self):
        for rc in (7, 0):
            with self.subTest(rc=rc):
                box = self.root / ("verify-" + str(rc))
                box.mkdir()
                self.js("codex/auth.json", {"OPENAI_API_KEY": self.canary}, self.stage)
                self.stub("codex", 'echo "$CANARY"; echo "$CANARY" >&2; exit ' + str(rc) + '\n')
                d = self.call("apply_import.sh", "codex", extra={"HOME": str(box)})
                self.assertEqual(d["verify"]["rc"], rc)
                config = box / ".grok-bot-agents/config.json"
                self.assertEqual(config.exists(), rc == 0)
                if rc == 0:
                    self.assertEqual(json.loads(config.read_text())["agents"]["codex"]["auth"], "imported")
        box = self.root / "no-verify"
        box.mkdir()
        self.js("codex/auth.json", {"OPENAI_API_KEY": self.canary}, self.stage)
        self.apply("codex", extra={"HOME": str(box)})
        self.assertFalse((box / ".grok-bot-agents/config.json").exists())

    def test_verify_identical_auth(self):
        auth = {"OPENAI_API_KEY": self.canary}
        self.js(".codex/auth.json", auth)
        self.js("codex/auth.json", auth, self.stage)
        self.stub("codex", "exit 0\n")
        d = self.call("apply_import.sh", "codex")
        self.assertFalse(d["backups"])
        self.assertTrue((self.home / ".grok-bot-agents/config.json").is_file())

    def test_verify_existing_config(self):
        config = self.js(".grok-bot-agents/config.json", {
            "version": 1, "enabled": ["pi"], "permission_mode": "safe", "worktree_per_task": True,
            "timeout_sec": 120, "agents": {"pi": {"auth": "oauth", "model": "keep"},
            "codex": {"auth": "oauth", "model": "old"}}, "routing": {"mode": "ask"}})
        self.js("codex/auth.json", {"OPENAI_API_KEY": self.canary}, self.stage)
        self.stub("codex", "exit 0\n")
        self.call("apply_import.sh", "codex")
        data = json.loads(config.read_text())
        self.assertEqual(data["enabled"], ["codex", "pi"])
        self.assertEqual(data["permission_mode"], "safe")
        self.assertEqual(data["agents"]["pi"]["model"], "keep")
        self.assertEqual(data["agents"]["codex"]["model"], "native")
        config.unlink()
        target = self.put("outside-config", self.canary)
        config.symlink_to(target)
        self.js("codex/auth.json", {"OPENAI_API_KEY": self.canary + "-new"}, self.stage)
        d = self.call("apply_import.sh", "codex", "--overwrite")
        self.assertIn("failed", d["config"])
        self.assertTrue(target.read_text() == self.canary)

    def test_stage_aux_symlinks(self):
        target = self.put("outside", self.canary)
        for rel, member in (("_meta/manifest.txt", ".agent-import-manifest.txt"),
                            ("_secrets/secrets.env", ".agent-import-secrets/secrets.env")):
            with self.subTest(path=rel):
                link = self.stage / rel
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
                self.call("stage_tar.py", self.archive([(member, "home projects/pkg\n", tarfile.REGTYPE)]), rc=2)
                self.assertTrue(target.read_text() == self.canary)
                link.unlink()

    def test_apply_aux_symlinks(self):
        target = self.put("outside-aux", self.canary)
        for rel in ("_meta/manifest.txt", "_secrets/rewrites.txt", "_secrets/secrets.env"):
            with self.subTest(path=rel):
                link = self.stage / rel
                link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(target)
                self.put("pi/AGENTS.md", "new", self.stage)
                self.apply("pi", rc=2)
                self.assertFalse((self.home / ".pi/agent/AGENTS.md").exists())
                self.assertTrue(target.read_text() == self.canary)
                link.unlink()

    def test_parity_no_run(self):
        self.stub("pi", 'echo called >> "$HOME/cli-calls"; echo "$CANARY"; exit 1\n')
        self.js(".pi/agent/settings.json", {"packages": ["~/local", "npm:fixture"]})
        self.js("local/package.json", {"name": "fixture"})
        p = self.call("parity_check.py", "--cli", "pi", "--no-run", rc=(0, 1))
        self.assertFalse((self.home / "cli-calls").exists())
        self.assertFalse(any(g["item"] == "pi package ~/local" for g in p["pi"]["gaps"]))
        self.assertTrue(any("not checked" in g["detail"] for g in p["pi"]["gaps"]))

    @unittest.skipUnless(PWSH, "pwsh unavailable; native Windows remains manual")
    def test_powershell(self):
        self.js(".pi/agent/auth.json", {"sample": {"type": "api_key", "key": self.canary}})
        self.js(".claude/.credentials.json", {"claudeAiOauth": {"accessToken": self.canary}})
        self.stub("pi", 'echo "1.2.3 $CANARY"\n')
        script = S / "detect_local.ps1"
        commands = [["-File", str(script), "-Clis", "pi,claude"],
                    ["-Command", "& ([scriptblock]::Create([IO.File]::ReadAllText($env:DETECT_SCRIPT))) -Clis pi,claude"]]
        for cmd in commands:
            r = subprocess.run([str(PWSH), "-NoProfile", "-NonInteractive"] + cmd, capture_output=True,
                               env=dict(self.env, DETECT_SCRIPT=str(script)), cwd=self.home, timeout=35)
            self.assertFalse(self.canary.encode() in r.stdout + r.stderr, "secret leaked (suppressed)")
            self.assertEqual(r.returncode, 0, "PowerShell failed (output suppressed)")
            try:
                d = json.loads(r.stdout)
            except ValueError:
                self.fail("PowerShell invalid JSON (output suppressed)")
            self.assertEqual(d["os"], "windows")
            self.assertEqual(d["clis"]["pi"]["version"], "1.2.3")
            self.assertTrue(d["secrets"]["inventory"]["items"])


if __name__ == "__main__":
    unittest.main()
