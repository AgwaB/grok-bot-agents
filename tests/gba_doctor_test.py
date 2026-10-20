#!/usr/bin/env python3
"""Offline diagnostics, leak canaries, and fake (never provider) live verification."""
import argparse
import contextlib
import hashlib
import importlib
import io
import json
import os
import sqlite3
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
support = importlib.import_module("gba_setup_test")
setup = importlib.import_module("gba_setup")
doctor = importlib.import_module("gba_doctor")
CANARY = support.CANARY


class DoctorTests(support.Isolated):
    def invoke(self, command="doctor", agents="codex", live=False, probe_versions=False):
        parser = argparse.ArgumentParser()
        doctor.configure(parser.add_subparsers(dest="command", required=True))
        argv = ["doctor", "--json"] if command == "doctor" else ["verify", "--agents", agents]
        argv += (["--live"] if live else []) + (["--probe-versions"] if probe_versions else [])
        args = parser.parse_args(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            response = doctor.handle(args, ROOT)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(CANARY, json.dumps(response))
        for key in ("status", "reason", "next_action", "requires_approval"):
            self.assertIn(key, response)
        return response

    def ready(self):
        self.config(["codex"])
        self.executable("codex", '''import pathlib, os, sys
assert sys.argv[1:] == ["--version"], "no offline model requests"
with (pathlib.Path(os.environ["HOME"]) / "agent-starts").open("a") as log: log.write("version\\n")
print("codex-cli 1.2.3")
print("s3cret_CANARY_must_never_appear")
print("s3cret_CANARY_must_never_appear", file=sys.stderr)
''')
        for name, version in (("node", "22.19.0"), ("npm", "10.9.0"), ("bun", "1.3.3")):
            self.executable(name, '''import os, pathlib, sys
assert sys.argv[1:] == ["--version"]
with (pathlib.Path(os.environ["HOME"]) / "runtime-starts").open("a") as log: log.write(''' + repr(name + "\n") + ''')
print(''' + repr(version) + ''')
''')
        (self.home / ".agent-keys").write_text("export OPENAI_API_KEY=" + CANARY + "\n")

    def snapshot(self):
        return {str(path.relative_to(self.home)): (path.stat().st_mtime_ns,
                path.read_bytes() if path.is_file() else None) for path in self.home.rglob("*")}

    def managed(self, agent="codex", body=None):
        # Construct a private fake receipt directly; never run an installer.
        prefix = setup.base_dir() / "agents" / agent
        prefix.mkdir(parents=True, exist_ok=True)
        payload = prefix / "payload.py"
        payload.write_text(body if body is not None else '''import os, pathlib, sys
assert sys.argv[1:] == ["--version"]
with (pathlib.Path(os.environ["HOME"]) / "managed-starts").open("a") as log: log.write("version\\n")
print("1.2.3")
''')
        wrapper = self.home / ".local/bin" / agent
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        wrapper.write_text("#!" + sys.executable + "\n# gba-managed-v1 " + agent + "\nimport runpy\nrunpy.run_path(" + repr(str(payload)) + ")\n")
        wrapper.chmod(0o755)
        receipt = prefix / "gba-install.json"
        receipt.write_text(json.dumps({"verified": True, "entry": str(payload), "runtime": sys.executable,
                                      "wrapper_sha256": hashlib.sha256(wrapper.read_bytes()).hexdigest(),
                                      "entry_sha256": hashlib.sha256(payload.read_bytes()).hexdigest()}))
        return wrapper, payload, receipt

    def fake_bash(self, status="ok", summary="OK", exit_code=0):
        payload = json.dumps({"status": status, "summary": summary, "stderr_tail": CANARY,
                              "model": CANARY, "artifacts": [CANARY], "run_dir": CANARY})
        self.executable("bash", '''import json, os, pathlib, sys
args = sys.argv[1:]
assert args[0].endswith("/skills/delegate-task/scripts/run-agent.sh")
assert args[1:] == ["--agent", "codex", "--executable", str(pathlib.Path(os.environ["HOME"]) / "fakebin/codex"),
                    "--read-only", "--timeout", "120", "--wake", "none", "--prompt", "Reply with exactly: OK"]
with (pathlib.Path(os.environ["HOME"]) / "live-starts").open("a") as log: log.write("live\\n")
print(''' + repr(payload) + ''')
print("s3cret_CANARY_must_never_appear", file=sys.stderr)
sys.exit(''' + str(exit_code) + ''')
''')

    def test_doctor_default_is_static_without_live_or_key_execution(self):
        self.ready()
        marker = self.home / "should-not-exist"
        (self.home / ".agent-keys").write_text("export OPENAI_API_KEY=$(/usr/bin/touch " + str(marker) + ")\n")
        self.fake_bash()
        before = self.snapshot()
        with mock.patch.object(setup.subprocess, "Popen", side_effect=AssertionError("process forbidden")):
            response = self.invoke()
        self.assertEqual(before, self.snapshot())
        self.assertEqual(response["status"], "ok", response)
        self.assertIsNone(response["agents"]["codex"]["version"])
        self.assertEqual(response["agents"]["codex"]["version_status"], "not_requested")
        self.assertFalse(response["version_probes"]["requested"])
        self.assertTrue(all(entry["status"] == "not_requested" for entry in response["runtimes"].values()))
        self.assertEqual(response["agents"]["codex"]["path"], "$HOME/fakebin/codex")
        self.assertTrue(response["keys"]["presence"]["OPENAI_API_KEY"])
        self.assertEqual(response["parity"]["status"], "ok")
        self.assertFalse(response["live"]["requested"])
        self.assertFalse(marker.exists())
        self.assertFalse((self.home / "live-starts").exists())
        self.assertFalse((self.home / "agent-starts").exists())
        self.assertFalse((self.home / "runtime-starts").exists())

    def test_verify_offline_is_static_and_never_calls_runner(self):
        self.ready()
        self.fake_bash()
        before = self.snapshot()
        with mock.patch.object(setup.subprocess, "Popen", side_effect=AssertionError("process forbidden")), \
                mock.patch.object(doctor, "_live_one", side_effect=AssertionError("live must be explicit")):
            response = self.invoke("verify")
        self.assertEqual(before, self.snapshot())
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["operation"], "verify")
        self.assertEqual(response["mode"], "offline")

    def test_all_agents_are_static_even_when_auth_is_missing(self):
        self.ready()
        self.config(list(setup.AGENTS))
        for agent in ("claude", "pi", "omp"):
            self.executable(agent, 'raise AssertionError("must not execute")\n')
        before = self.snapshot()
        with mock.patch.object(setup.subprocess, "Popen", side_effect=AssertionError("process forbidden")):
            response = self.invoke()
        self.assertEqual(set(response["agents"]), set(setup.AGENTS))
        self.assertEqual(response["status"], "blocked")
        self.assertTrue(all(entry["version_status"] == "not_requested" for entry in response["agents"].values()))
        self.assertEqual(before, self.snapshot())

    def test_explicit_version_flag_probes_agents_and_runtimes_without_live(self):
        self.ready()
        self.fake_bash()
        for index, command in enumerate(("doctor", "verify"), 1):
            with self.subTest(command=command):
                response = self.invoke(command, probe_versions=True)
                self.assertEqual(response["status"], "ok", response)
                self.assertTrue(response["version_probes"]["requested"])
                self.assertEqual(response["agents"]["codex"]["version"], "1.2.3")
                self.assertEqual(response["agents"]["codex"]["version_status"], "ok")
                self.assertEqual(response["runtimes"]["node"]["version"], "22.19.0")
                self.assertEqual(response["runtimes"]["npm"]["version"], "10.9.0")
                self.assertEqual(response["runtimes"]["bun"]["version"], "1.3.3")
                self.assertEqual((self.home / "agent-starts").read_text(), "version\n" * index)
                self.assertEqual((self.home / "runtime-starts").read_text(), "node\nnpm\nbun\n" * index)
                self.assertFalse((self.home / "live-starts").exists())

    def test_modified_managed_wrapper_payload_or_receipt_blocks_execution(self):
        self.ready()
        self.fake_bash()
        for target in ("wrapper", "payload", "receipt", "orphan_wrapper"):
            for command, live, probes in (("doctor", False, False), ("doctor", False, True),
                                           ("verify", False, True), ("verify", True, False),
                                           ("verify", True, True)):
                with self.subTest(target=target, command=command, live=live, probes=probes):
                    wrapper, payload, receipt = self.managed()
                    if target in ("receipt", "orphan_wrapper"):
                        receipt.unlink()
                        if target == "orphan_wrapper":
                            payload.unlink()
                            payload.parent.rmdir()
                    else:
                        path = wrapper if target == "wrapper" else payload
                        path.write_text(path.read_text() + "# modified\n")
                    response = self.invoke(command, live=live, probe_versions=probes)
                    self.assertEqual(response["status"], "blocked", response)
                    self.assertTrue(response["agents"]["codex"]["managed"])
                    self.assertEqual(response["agents"]["codex"]["integrity"], "managed_install_invalid")
                    self.assertEqual(response["agents"]["codex"]["version_status"],
                                     "managed_install_invalid" if probes else "not_requested")
                    if live:
                        self.assertEqual(response["live"]["agents"]["codex"]["reason"], "managed_install_invalid")
                    self.assertFalse((self.home / "managed-starts").exists())
                    self.assertFalse((self.home / "live-starts").exists())

    def test_valid_managed_install_checks_integrity_before_version(self):
        self.ready()
        wrapper, _, _ = self.managed()
        events = []
        original_state, original_probe = setup.managed_state, setup.probe_version

        def state(agent):
            events.append("integrity")
            return original_state(agent)

        def probe(executable):
            if executable == str(wrapper):
                self.assertEqual(events, ["integrity"])
                events.append("version")
            return original_probe(executable)

        with mock.patch.object(setup, "managed_state", side_effect=state), \
                mock.patch.object(setup, "probe_version", side_effect=probe):
            response = self.invoke(probe_versions=True)
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(events, ["integrity", "version"])
        self.assertEqual((self.home / "managed-starts").read_text(), "version\n")

    def test_live_rechecks_managed_integrity_at_execution_boundary(self):
        self.ready()
        _, payload, _ = self.managed()
        self.fake_bash()
        original = doctor._parity

        def tamper(*args):
            payload.write_text(payload.read_text() + "# changed after diagnostic integrity check\n")
            return original(*args)

        with mock.patch.object(doctor, "_parity", side_effect=tamper):
            response = self.invoke("verify", live=True)
        self.assertEqual(response["status"], "blocked", response)
        self.assertEqual(response["agents"]["codex"]["integrity"], "managed_install_invalid")
        self.assertEqual(response["live"]["agents"]["codex"]["reason"], "managed_install_invalid")
        self.assertFalse((self.home / "managed-starts").exists())
        self.assertFalse((self.home / "live-starts").exists())

    def test_verify_live_routes_exact_OK_prompt_through_existing_runner(self):
        self.ready()
        self.fake_bash()
        response = self.invoke("verify", live=True)
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["reason"], "verification_ok")
        self.assertEqual(response["live"]["agents"]["codex"]["quota"], "not_limited")
        self.assertEqual((self.home / "live-starts").read_text(), "live\n")
        self.assertFalse((self.home / "agent-starts").exists())
        self.assertFalse((self.home / "runtime-starts").exists())

    def test_live_uses_checked_executable_not_off_path_npm_prefix(self):
        prefix = self.home / "custom npm prefix"
        (prefix / "bin").mkdir(parents=True)
        self.executable("npm", '''import os, pathlib, sys
assert sys.argv[1:] == ["config", "get", "prefix"]
home = pathlib.Path(os.environ["HOME"])
(home / "npm-started").touch()
print(home / "custom npm prefix")
''')
        events = {
            "codex": [{"type": "item.completed", "item": {"type": "agent_message", "text": "OK"}},
                      {"type": "turn.completed", "usage": {}}],
            "claude": [{"type": "result", "subtype": "success", "is_error": False, "result": "OK"}],
            "pi": [{"type": "message_end", "message": {"role": "assistant", "stopReason": "stop",
                    "content": [{"type": "text", "text": "OK"}]}}],
        }
        events["omp"] = events["pi"]
        for agent, records in events.items():
            with self.subTest(agent=agent):
                self.config([agent])
                wrapper, _, _ = self.managed(agent, '''import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
with (home / "checked-starts").open("a") as log: log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[1:] == ["--help"]:
    print("--approve")
else:
    assert sys.argv[-1] == "Reply with exactly: OK"
    if "-o" in sys.argv: pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text("OK")
    print(''' + repr("\n".join(json.dumps(record) for record in records)) + ''')
''')
                shadow = prefix / "bin" / agent
                shadow.write_text("#!" + sys.executable + '\nimport os, pathlib\n'
                                  '(pathlib.Path(os.environ["HOME"]) / "wrong-executable").touch()\n')
                shadow.chmod(0o700)
                with mock.patch.dict(os.environ, {"PATH": f"{self.bin}:/usr/bin:/bin",
                                                  "AGENT_RUNS_ROOT": str(self.home / "runs")}), \
                        mock.patch.object(doctor, "_parity", return_value={"status": "ok", "agents": {}}):
                    self.assertNotIn(str(prefix / "bin"), setup.user_path().split(os.pathsep))
                    response = self.invoke("verify", agents=agent, live=True)
                self.assertEqual(response["live"]["agents"][agent]["reason"], "live_ok", response)
                self.assertEqual(response["agents"][agent]["integrity"], "ok")
                self.assertEqual(response["agents"][agent]["path"], setup.location(str(wrapper)))
                self.assertFalse((self.home / "wrong-executable").exists())
                self.assertFalse((self.home / "npm-started").exists())
                starts = [json.loads(line) for line in (self.home / "checked-starts").read_text().splitlines()]
                self.assertEqual(len(starts), 2 if agent == "pi" else 1)
                if agent == "pi":
                    self.assertEqual(starts[0], ["--help"])
                    self.assertIn("--approve", starts[1])
                (self.home / "checked-starts").unlink()

    def test_live_quota_is_distinct_from_install_auth_or_version_failure(self):
        self.ready()
        self.fake_bash(status="rate_limited", summary=CANARY, exit_code=1)
        response = self.invoke("verify", live=True)
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["reason"], "quota_limited")
        self.assertEqual(response["next_action"], "retry_later")
        self.assertEqual(response["live"]["agents"]["codex"]["quota"], "limited")
        self.assertTrue(response["agents"]["codex"]["installed"])
        self.assertEqual(response["parity"]["status"], "ok")

    def test_live_requires_exact_OK_not_just_exit_zero(self):
        self.ready()
        self.fake_bash(summary="OK " + CANARY)
        response = self.invoke("verify", live=True)
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["live"]["agents"]["codex"]["reason"], "live_response_mismatch")

    def test_live_refuses_invalid_or_disabled_config(self):
        self.ready()
        cfg = json.loads(setup.config_path().read_text())
        cfg["enabled"] = ["pi"]
        setup.config_path().write_text(json.dumps(cfg))
        self.fake_bash()
        response = self.invoke("verify", live=True)
        self.assertEqual(response["live"]["agents"]["codex"]["reason"], "live_prerequisites_missing")
        self.assertFalse((self.home / "live-starts").exists())
        cfg["enabled"] = [CANARY]
        setup.config_path().write_text(json.dumps(cfg))
        response = self.invoke("verify", live=True)
        self.assertEqual(response["config"]["status"], "config_invalid")
        self.assertFalse((self.home / "live-starts").exists())

    def test_config_missing_and_invalid_return_stable_no_value_diagnostics(self):
        with mock.patch.object(doctor, "_parity", return_value={"status": "ok", "agents": {}}):
            self.assertEqual(self.invoke()["reason"], "config_missing")
            path = setup.config_path()
            path.parent.mkdir(parents=True)
            path.write_text('{"enabled": "' + CANARY + '"}')
            self.assertEqual(self.invoke()["reason"], "config_invalid")

    def test_real_parity_gaps_are_counts_only(self):
        self.ready()
        (self.home / ".agent-keys").unlink()
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").write_text('[mcp_servers.' + CANARY + ']\ncommand="' + CANARY + '"\n')
        response = self.invoke()
        self.assertEqual(response["status"], "blocked")
        counts = response["parity"]["agents"]["codex"]["counts"]
        self.assertEqual(counts["breaking"], 1)
        self.assertEqual(counts["degraded"], 1)

    def test_malformed_parity_and_live_output_cannot_leak(self):
        self.ready()
        with mock.patch.object(doctor, "_static_parity_helper", return_value={"check": lambda *a, **kw: CANARY}):
            parity = doctor._parity(ROOT, ["codex"])
        self.assertEqual(parity["reason"], "parity_result_invalid")
        self.fake_bash()
        with mock.patch.object(setup, "run_captured", return_value=(1, CANARY.encode(), CANARY.encode(), "ok")):
            live = doctor._live_one(ROOT, "codex")
        self.assertEqual(live["reason"], "live_result_invalid")
        self.assertNotIn(CANARY, json.dumps([parity, live]))

    def test_omp_static_parity_never_copies_or_writes_database(self):
        self.ready()
        self.config(["omp"])
        self.executable("omp", 'raise AssertionError("must not execute")\n')
        directory = self.home / ".omp/agent"
        directory.mkdir(parents=True)
        connection = sqlite3.connect(str(directory / "agent.db"))
        connection.execute("create table auth_credentials (provider text, disabled_cause text)")
        connection.execute("insert into auth_credentials values ('fake-provider', 'disabled')")
        connection.commit()
        connection.close()
        (directory / "models.yml").write_text("providers:\n  fake-provider:\n    apiKey: ${NOT_SET}\n")
        before = self.snapshot()
        with mock.patch.object(setup.subprocess, "Popen", side_effect=AssertionError("process forbidden")), \
                mock.patch.object(support.tempfile, "mkdtemp", side_effect=AssertionError("temp writes forbidden")):
            response = self.invoke("verify", agents="omp")
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["parity"]["agents"]["omp"]["counts"]["degraded"], 2)
        self.assertEqual(before, self.snapshot())
        (directory / "agent.db-wal").write_bytes(b"pending fake WAL; do not open/write sidecars")
        before = self.snapshot()
        with mock.patch.object(doctor.sqlite3, "connect", side_effect=AssertionError("WAL must not be ignored")) as connect:
            response = self.invoke("verify", agents="omp")
        connect.assert_not_called()
        self.assertEqual(response["parity"]["agents"]["omp"]["counts"]["degraded"], 2)
        self.assertEqual(before, self.snapshot())

    def test_version_has_full_line_allowlist_and_bounded_output(self):
        path = self.executable("codex", 'print("token=' + CANARY + ' 1.2.3")\n')
        self.assertIsNone(setup.probe_version(path)["version"])
        self.executable("codex", 'print("x" * 200000)\n')
        self.assertEqual(setup.probe_version(path)["status"], "command_output_limit")
        path = self.executable("codex", 'import time\ntime.sleep(30)\n')
        rc, out, err, reason = setup.run_captured([str(path), "--version"], timeout=0.05)
        self.assertEqual(reason, "command_timeout")
        self.assertIsNotNone(rc)
        self.assertEqual(out + err, b"")

    def test_key_file_symlink_is_not_read(self):
        self.ready()
        (self.home / ".agent-keys").unlink()
        target = self.home / "secret-file"
        target.write_text("OPENAI_API_KEY=" + CANARY)
        (self.home / ".agent-keys").symlink_to(target)
        presence, readable = doctor._key_presence(["OPENAI_API_KEY"])
        self.assertFalse(readable)
        self.assertFalse(presence["OPENAI_API_KEY"])

    def test_unknown_agent_input_never_echoed(self):
        response = self.invoke("verify", agents=CANARY)
        self.assertEqual(response["reason"], "invalid_agents")


if __name__ == "__main__":
    unittest.main()
