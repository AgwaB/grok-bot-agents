#!/usr/bin/env bash
# Install staged CLI files (copied from the user's computer with CopyToBox) into the box's CLI home.
#   apply_import.sh <codex|claude|pi|omp> [--src-home /Users/alice] [--dry-run] [--overwrite] [--force-auth] [--keep-stage] [--no-verify]
#   apply_import.sh keys [--dry-run] [--overwrite]     merge the staged _secrets/secrets.env (export_secrets.sh) into ~/.agent-keys
# Stage: ${AGENT_IMPORT_STAGE:-/workspace/uploads/agent-import}/<cli>/<rel path as in import_plan.py>
# - Installs only allowlisted paths; anything else (SSH keys, other dotfiles, caches, sessions) is skipped and reported.
# - Never prints file contents. Rejects symlinks and non-regular files.
# - An existing different file is a conflict: exits 4 unless --overwrite, which first backs it up to <file>.bak.<ts>.
# - Rewrites the user's home prefix (--src-home) to the box $HOME in text config files; warns about other absolute paths.
# - Claude: only the mcpServers key of ~/.claude.json is merged.
# - Files 600, dirs 700. Runs the CLI's status check; if it passes, updates ~/.grok-bot-agents/config.json
#   (enable, auth=imported, model/reasoning=native so the imported settings apply).
# - Deletes the staged files for that CLI afterwards unless --keep-stage (they contain tokens).
# - Imported extensions/skills with a package.json but no node_modules (bundles exclude node_modules) get their
#   npm dependencies installed (npm ci, else npm install; --omit=dev). Reported in "deps_installed"; failures are warnings.
# - omp: reads only provider/credential_type/disabled state of auth_credentials (never the data column) from temp copies.
#   If every staged login is disabled (or there is none) while the box agent.db has a working one, agent.db* is NOT
#   replaced, even with --overwrite (the rest still installs); "omp_auth" explains it and the exit code is 5.
#   --force-auth replaces it anyway.
# - Secrets: the staged _secrets/secrets.env is merged into ~/.agent-keys (600; same-name lines replaced, no dupes; names
#   only in the output) by `keys`, or automatically by the first real CLI run. Then it's overwritten and deleted.
#   Pi/omp keys that were `!command` / op:// and got resolved on the computer are rewritten to env-name references
#   (_secrets/rewrites.txt). Codex config.toml is pinned to file credential stores (the box has no OS keyring).
#   A copied Claude login (opt-in) also merges oauthAccount + hasCompletedOnboarding from ~/.claude.json.
#   Claude/Codex subscription logins are not bundled by default: the warning says to log in on the box instead.
# - The status check runs with ~/.agent-keys sourced. Staged auth files are overwritten before deletion.
# Exit codes: 0 ok, 2 usage/stage error, 4 conflicts (rerun with --overwrite), 5 installed but box omp login kept.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
exec python3 - "$HERE" "$@" <<'PY'
import datetime, fnmatch, hashlib, json, os, re, shutil, stat, subprocess, sys, tempfile
here, args = sys.argv[1], sys.argv[2:]
sys.path.insert(0, here)
from import_plan import PLAN
from import_safety import atomic_write, claude_global, guard_path, package_root
import post_import

def die(msg, code=2):
    print(json.dumps({"ok": False, "error": msg})); sys.exit(code)

STAGE_ROOT = os.environ.get("AGENT_IMPORT_STAGE", "/workspace/uploads/agent-import")
if args and args[0] == "keys":
    try: r = post_import.merge_agent_keys(STAGE_ROOT, dry=True)
    except (ValueError, OSError): die("unsafe keys or staging path")
    if r and r["replaced"] and "--overwrite" not in args[1:]:
        print(json.dumps({"ok": False, "agent_keys": r})); sys.exit(4)
    if "--dry-run" not in args[1:]:
        try: r = post_import.merge_agent_keys(STAGE_ROOT, overwrite="--overwrite" in args[1:])
        except post_import.KeyConflict: die("key overwrite approval required", 4)
        except (ValueError, OSError): die("unsafe keys or staging path")
    print(json.dumps({"ok": True, "dry_run": "--dry-run" in args[1:], "agent_keys": r} if r is not None
                     else {"ok": True, "agent_keys": None, "note": "no staged secrets.env"}, indent=2)); sys.exit(0)

if not args or args[0] not in PLAN: die("usage: apply_import.sh <codex|claude|pi|omp> [--src-home H] [--dry-run] [--overwrite] [--force-auth] [--keep-stage] [--no-verify] | apply_import.sh keys [--dry-run]")
cli = args[0]; opts = args[1:]
dry, overwrite, keep, noverify = "--dry-run" in opts, "--overwrite" in opts, "--keep-stage" in opts, "--no-verify" in opts
force_auth = "--force-auth" in opts
src_home = None
if "--src-home" in opts:
    i = opts.index("--src-home"); src_home = opts[i + 1] if i + 1 < len(opts) else die("--src-home needs a value")
HOME = os.path.expanduser("~")
stage = os.path.join(STAGE_ROOT, cli)
if not os.path.isdir(stage): die(f"nothing staged at {stage}")
p = PLAN[cli]
env_dir = {"codex": os.environ.get("CODEX_HOME"), "claude": os.environ.get("CLAUDE_CONFIG_DIR"),
           "pi": os.environ.get("PI_CODING_AGENT_DIR")}.get(cli)
dest_root = env_dir or os.path.join(HOME, p["home"])
try:
    guard_path(stage); guard_path(dest_root)
except ValueError: die("symlink in staging or destination path")
try: PACKAGE_ROOTS = post_import.read_manifest(STAGE_ROOT)["home"]
except (ValueError, OSError): die("unsafe or unreadable import manifest")

# allowlist: (pattern, kind)
allow = []
for rel, kind, _ in p["files"]:
    if rel.startswith("../"): allow.append(("_home/" + rel[3:], kind))
    elif rel.endswith("/"): allow.append((rel + "*", kind))
    else: allow.append((rel, kind))
if cli == "omp": allow += [("agent.db-wal", "auth"), ("agent.db-shm", "auth")]
SKIP_PARTS = {"sessions", "projects", "log", "logs", "cache", "history.jsonl", "todos", "shell-snapshots", "shell_snapshots",
              "statsig", "backups", "file-history", "node_modules", ".git", "__pycache__", "debug", "tmp"}
TEXT = (".json", ".toml", ".yml", ".yaml", ".md", ".txt")
MAX = 200 * 1024 * 1024

def kind_of(rel):
    parts = rel.split("/")
    if cli == "pi" and rel.startswith("_home/"):
        path = rel[len("_home/"):]
        if not package_root(path) or not any(path.startswith(r + "/") for r in PACKAGE_ROOTS): return None
    inner = parts[2:] if parts[:2] == ["npm", "node_modules"] else parts  # Pi npm overlay lives under node_modules
    if rel.endswith(".pyc") or parts[0] in SKIP_PARTS or set(inner) & {"__pycache__", "node_modules", ".git"}: return None
    for pat, k in allow:
        if "*" in pat and "/" not in pat and "/" in rel: continue  # globs like *auth*.json: top-level files only
        if fnmatch.fnmatchcase(rel, pat): return k
    return None

def sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""): h.update(b)
    return h.hexdigest()

home_variants = []
if src_home:
    s = src_home.rstrip("/\\")
    home_variants = sorted({s, s.replace("\\", "/"), s.replace("\\", "\\\\")}, key=len, reverse=True)
ABS = re.compile(r'(/Users/[^/"\s]+|/home/(?!box\b)[^/"\s]+|[A-Za-z]:\\\\?Users\\\\?[^\\"\s]+|/Applications/|/opt/homebrew/|/Volumes/|/Library/|/System/|\.app/Contents/)')

try: REWRITES = post_import.read_rewrites(STAGE_ROOT)
except (ValueError, OSError): die("unsafe or unreadable key rewrites")
auth_notes = {}  # names only: refs rewritten to env names, codex stores pinned

def transform(data, rel, kind):
    """Return (bytes, rewrites, leftover_abs_count). Auth/db files keep their bytes, except resolved key refs."""
    data, done = post_import.rewrite_refs(cli, rel, data, REWRITES)
    if done: auth_notes.setdefault("refs_to_env_names", []).extend(f"{rel} {p}" for p in done)
    if cli == "codex" and rel == "config.toml":
        try:
            t, changed = post_import.pin_codex_file_store(data.decode("utf-8")); data = t.encode("utf-8")
            if changed: auth_notes["codex_file_store_pinned"] = changed
        except UnicodeDecodeError: pass
    if kind in ("auth", "package", "overlay", "meta") or not rel.endswith(TEXT): return data, 0, 0
    try: t = data.decode("utf-8")
    except UnicodeDecodeError: return data, 0, 0
    n = 0
    def replace_home(value):
        nonlocal n
        for v in home_variants:
            n += value.count(v); value = value.replace(v, HOME)
        return value
    if rel.endswith(".json") and home_variants:
        # Decode first: escaped Unicode/Windows paths and quotes in HOME must stay valid JSON.
        def walk(value):
            if isinstance(value, str): return replace_home(value)
            if isinstance(value, list): return [walk(v) for v in value]
            if isinstance(value, dict): return {replace_home(k): walk(v) for k, v in value.items()}
            return value
        try:
            obj = walk(json.loads(t))
            if n: t = json.dumps(obj, indent=2) + "\n"
        except ValueError: pass  # malformed configuration is left intact and reported by parity
    else:
        t = replace_home(t)
    return t.encode("utf-8"), n, len(ABS.findall(t))

def dest_of(rel):
    # Pi packages outside ~/.pi/agent (bundle manifest `home` roots) go back to the same place under the box home
    if cli == "pi" and rel.startswith("_home/"): return os.path.join(HOME, rel[len("_home/"):])
    return os.path.join(dest_root, rel)

ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
plan, skipped, conflicts, warnings = [], [], [], []
staged_rels = []  # every allowlisted staged file, installed or identical
for root, dirs, files in os.walk(stage, followlinks=False):
    for name in dirs[:]:
        if os.path.islink(os.path.join(root, name)):
            skipped.append({"path": os.path.relpath(os.path.join(root, name), stage), "why": "symlink (target not imported): " + os.readlink(os.path.join(root, name))}); dirs.remove(name)
    for name in files:
        full = os.path.join(root, name); rel = os.path.relpath(full, stage).replace(os.sep, "/")
        if any(n.startswith("._") or n == ".DS_Store" for n in rel.split("/")):
            skipped.append({"path": rel, "why": "macOS metadata"}); continue
        st = os.lstat(full)
        if stat.S_ISLNK(st.st_mode):
            skipped.append({"path": rel, "why": "symlink (target not imported): " + os.readlink(full)}); continue
        if not stat.S_ISREG(st.st_mode): skipped.append({"path": rel, "why": "not a regular file"}); continue
        if st.st_size > MAX: skipped.append({"path": rel, "why": "too large"}); continue
        k = kind_of(rel)
        if not k: skipped.append({"path": rel, "why": "not on the allowlist for " + cli}); continue
        staged_rels.append(rel)
        if k in ("meta", "overlay"): continue  # used by post_import.py, not copied as-is
        if k == "merge":
            plan.append({"rel": rel, "kind": k, "src": full, "dest": claude_global(HOME)}); continue
        dest = dest_of(rel)
        try: guard_path(dest)
        except ValueError: die("symlink in destination path")
        data, nrw, nabs = transform(open(full, "rb").read(), rel, k)
        if nabs and k != "dir": warnings.append(f"{rel}: {nabs} absolute path(s) from the other computer remain; check them")
        if os.path.exists(dest):
            if os.path.islink(dest) or not os.path.isfile(dest): die(f"destination is not a regular file: {dest}")
            if hashlib.sha256(data).hexdigest() == sha(dest): skipped.append({"path": rel, "why": "identical on box"}); continue
            conflicts.append(rel)
        plan.append({"rel": rel, "kind": k, "src": full, "dest": dest, "data": data, "rewrites": nrw})

def compact(items):
    """Top-level files listed individually; files inside dirs counted per top-level dir."""
    files, dirs = [], {}
    for x in items:
        rel = x["rel"] if isinstance(x, dict) else x
        top = rel.split("/")[0]
        if "/" in rel and not rel.startswith("_home/"): dirs[top + "/"] = dirs.get(top + "/", 0) + 1
        else: files.append(x)
    return {"files": files, "dirs": dirs}

def summarize_skipped():
    groups = {}
    for x in skipped:
        key = "symlink (target not imported)" if x["why"].startswith("symlink") else x["why"]
        g = groups.setdefault(key, {"count": 0, "examples": []}); g["count"] += 1
        if len(g["examples"]) < 12:
            g["examples"].append(x["path"] + (" -> " + x["why"].split(": ", 1)[1] if x["why"].startswith("symlink") else ""))
    out["skipped"] = groups

out = {"ok": True, "cli": cli, "dry_run": dry, "dest_root": dest_root, "skipped": skipped, "warnings": warnings,
       "installed": [], "backups": [], "conflicts": conflicts}
if cli == "omp" and any(x["rel"] == "agent.db" for x in plan) and not any(x["rel"] == "agent.db-wal" for x in plan):
    warnings.append("agent.db copied without agent.db-wal: fine if omp was closed on the other computer, otherwise recent logins may be missing")
if cli == "claude" and ".credentials.json" not in staged_rels and not os.path.exists(os.path.join(dest_root, ".credentials.json")):
    warnings.append("Claude login not copied (default): log in on the box with `claude auth login` (setup-agents headless recipe)")
if cli == "codex" and "auth.json" not in staged_rels and not os.path.exists(os.path.join(dest_root, "auth.json")):
    warnings.append("Codex login not copied (default): log in on the box with `codex login --device-auth` (setup-agents headless recipe)")
claude_login = cli == "claude" and ".credentials.json" in staged_rels
if claude_login and not any(x["rel"] == "_home/.claude.json" for x in plan):
    warnings.append("Claude login copied without ~/.claude.json: the account fields (oauthAccount) are missing, so Claude may ask to log in or onboard")

# --- omp: don't replace a working box login with a dead one -------------------------------------
def omp_creds(d):
    """[{provider, credential_type, enabled}] from <d>/agent.db (+wal/shm), read from a temp copy so nothing is
    written beside the original. Never selects the data column. None if unreadable, [] if no table/rows."""
    import sqlite3, tempfile
    if not os.path.isfile(os.path.join(d, "agent.db")): return None
    t = tempfile.mkdtemp(prefix="omp-auth-")
    try:
        os.chmod(t, 0o700)
        for n in ("agent.db", "agent.db-wal", "agent.db-shm"):
            guard_path(os.path.join(d, n))
            if os.path.isfile(os.path.join(d, n)): shutil.copyfile(os.path.join(d, n), os.path.join(t, n))
        c = sqlite3.connect(os.path.join(t, "agent.db"))
        try:
            if not c.execute("select 1 from sqlite_master where type='table' and name='auth_credentials'").fetchone(): return []
            rows = c.execute("select provider, credential_type, disabled_cause is null from auth_credentials").fetchall()
        finally: c.close()
        return [{"provider": r[0], "credential_type": r[1], "enabled": bool(r[2])} for r in rows]
    except Exception:
        return None
    finally:
        shutil.rmtree(t, ignore_errors=True)

omp_auth = None
DB = ("agent.db", "agent.db-wal", "agent.db-shm")  # one unit: logins may sit only in the WAL
if cli == "omp" and any(n in staged_rels for n in DB[1:]) and "agent.db" not in staged_rels:
    die("omp database sidecars require agent.db")
db_changed = cli == "omp" and any(x["rel"] in DB for x in plan)
if db_changed:
    try:
        for n in DB: guard_path(os.path.join(dest_root, n))
    except ValueError: die("symlink in omp destination database")
    staged_c, box_c = omp_creds(stage), omp_creds(dest_root)
    s_ok = [c["provider"] for c in staged_c or [] if c["enabled"]]
    b_ok = [c["provider"] for c in box_c or [] if c["enabled"]]
    omp_auth = {"staged": staged_c, "box": box_c, "action": "replace"}
    dead = sorted({c["provider"] for c in staged_c or [] if not c["enabled"]} - set(s_ok))
    if not s_ok and b_ok and not force_auth:
        omp_auth["action"] = "kept_box_login"; db_changed = False
        plan = [x for x in plan if x["rel"] not in DB]
        conflicts = [r for r in conflicts if r not in DB]; out["conflicts"] = conflicts
        for r in DB:
            if r in staged_rels: skipped.append({"path": r, "why": "kept the box's working omp login (staged login is disabled or missing)"})
        warnings.append("omp: the login in the imported agent.db is " + (f"disabled ({', '.join(dead)})" if dead else "missing")
                        + f", while the box already has a working one ({', '.join(sorted(set(b_ok)))}). Kept the box's agent.db; "
                        "config files still import. Tell the user to run `omp` then `/login` on their computer. "
                        "Rerun with --force-auth to replace it anyway.")
    elif staged_c is not None and not s_ok:
        warnings.append("omp: the imported agent.db has no working login" + (f" (disabled: {', '.join(dead)})" if dead else "")
                        + ". After import, log in on the box (`omp` then `/login`), or have the user log in on their computer and re-import.")
    elif dead:
        warnings.append(f"omp: some imported logins are disabled: {', '.join(dead)} (others work)")

# --- npm deps for imported extensions/skills/packages (bundles exclude node_modules) ---------------
# Extensions/skills: production deps. Pi packages (settings.json `packages`): all deps, as on the computer, because
# some load dev deps at runtime (e.g. `typescript`).
DEP_ROOTS = {"pi": ("extensions", "skills"), "codex": ("skills",), "claude": ("skills",), "omp": ()}[cli]
def pkg_roots():
    roots = set()
    for rel in staged_rels:
        parts = rel.split("/")
        if cli == "pi" and parts[0] == "packages" and len(parts) >= 3: roots.add("/".join(parts[:2]))
    if cli == "pi":
        mp = os.path.join(os.path.dirname(stage), "_meta", "manifest.txt")
        if os.path.isfile(mp):
            for line in open(mp, encoding="utf-8", errors="replace"):
                if line.startswith("home "): roots.add("_home/" + line[5:].strip().strip("/"))
    return roots
PKG_ROOTS = pkg_roots()
dep_dirs = []
for rel in staged_rels:
    parts = rel.split("/")
    d_rel = "/".join(parts[:-1])
    if parts[-1] == "package.json" and (d_rel in PKG_ROOTS or (parts[0] in DEP_ROOTS and len(parts) in (2, 3))):
        if not os.path.isdir(os.path.join(dest_of(d_rel), "node_modules")): dep_dirs.append(d_rel)

def has_deps(staged_pkg, full=False):
    try: j = json.load(open(staged_pkg, encoding="utf-8"))
    except Exception: return True
    keys = ("dependencies", "optionalDependencies") + (("devDependencies", "peerDependencies") if full else ())
    return any(j.get(k) for k in keys)
dep_dirs = sorted(d for d in set(dep_dirs) if has_deps(os.path.join(stage, d, "package.json"), d in PKG_ROOTS))

def install_deps():
    res, env = [], dict(os.environ)
    n22 = os.path.join(HOME, ".local/opt/node22/bin")
    if os.path.isdir(n22): env["PATH"] = n22 + os.pathsep + env.get("PATH", "")
    if not shutil.which("npm", path=env["PATH"]):
        warnings.append(f"npm not found: install deps manually in {dep_dirs}"); return res
    for rel in dep_dirs:
        flags = (["--omit=dev"] if rel not in PKG_ROOTS else []) + ["--no-audit", "--no-fund"]
        d = dest_of(rel); tried = []
        if os.path.isdir(os.path.join(d, "node_modules")): continue
        cmds = ([["npm", "ci"] + flags] if os.path.isfile(os.path.join(d, "package-lock.json")) else []) + [["npm", "install"] + flags]
        ok = False
        for c in cmds:
            try:
                r = subprocess.run(c, cwd=d, env=env, capture_output=True, text=True, timeout=300)
                tried.append(c[1]); ok = r.returncode == 0
            except Exception:
                tried.append(c[1]); ok = False
            if ok: break
        res.append({"dir": rel, "ok": ok, "via": tried[-1]})
        if not ok: warnings.append(f"{rel}: npm {' then '.join(tried)} failed (output suppressed); that extension may not load, so run it by hand")
    return res

# Preflight Claude's global merge before any writes (including pending env keys).
for x in plan[:]:
    if x["kind"] != "merge": continue
    dest = x["dest"]
    try:
        guard_path(dest)
        src = json.load(open(x["src"], encoding="utf-8"))
        cur = json.load(open(dest, encoding="utf-8")) if os.path.exists(dest) else {}
        if not isinstance(src, dict) or not isinstance(cur, dict): raise ValueError()
        servers = src.get("mcpServers") or {}
        existing = cur.get("mcpServers") or {}
        if not isinstance(servers, dict) or not isinstance(existing, dict): raise ValueError()
    except (ValueError, OSError): die("unsafe or invalid Claude global configuration")
    acct = {k: src[k] for k in ("oauthAccount", "hasCompletedOnboarding") if claude_login and k in src}
    s = json.dumps(servers)
    for v in home_variants: s = s.replace(json.dumps(v)[1:-1], json.dumps(HOME)[1:-1])
    servers = json.loads(s)
    if any(k in existing and existing[k] != v for k, v in servers.items()) or any(k in cur and cur[k] != v for k, v in acct.items()):
        conflicts.append(x["rel"])
    before = json.dumps(cur, sort_keys=True)
    existing.update(servers)
    if existing: cur["mcpServers"] = existing
    cur.update(acct)
    if before == json.dumps(cur, sort_keys=True):
        plan.remove(x); skipped.append({"path": x["rel"], "why": "identical on box"}); continue
    x["data"] = json.dumps(cur, indent=2).encode("utf-8"); x["rewrites"] = 0

if conflicts and not overwrite:
    out.update(ok=False, error="existing box files differ; ask the user, then rerun with --overwrite (backs them up)")
    out["planned"] = compact([x["rel"] for x in plan])
    if omp_auth: out["omp_auth"] = omp_auth
    summarize_skipped(); print(json.dumps(out, indent=2)); sys.exit(4)
try: pending_keys = post_import.merge_agent_keys(STAGE_ROOT, dry=True)
except (ValueError, OSError): die("unsafe keys or staging path")
if pending_keys and pending_keys["replaced"] and not overwrite:
    out.update(ok=False, agent_keys=pending_keys, error="key overwrite approval required")
    print(json.dumps(out)); sys.exit(4)
if dry:
    if pending_keys: out["agent_keys"] = dict(pending_keys, note="merged into ~/.agent-keys on the real run (or `apply_import.sh keys`)")
    if auth_notes: out["auth"] = auth_notes
    out["planned"] = [{"rel": x["rel"], "kind": x["kind"], "dest": x["dest"], "home_rewrites": x.get("rewrites", 0), "overwrites": x["rel"] in conflicts} for x in plan]
    out["planned"] = compact(out["planned"])
    if dep_dirs: out["planned"]["npm_deps"] = dep_dirs
    out["planned"]["post_install"] = {"pi": "missing npm packages, npm overlay, package venvs", "claude": "marketplaces, enabledPlugins (pinned to the computer's SHA), tool paths, MCP uv tools",
                                      "codex": "disable Mac-only MCP servers / notify, MCP uv tools", "omp": "MCP uv tools"}[cli]
    if omp_auth: out["omp_auth"] = omp_auth
    summarize_skipped(); print(json.dumps(out, indent=2)); sys.exit(0)

def backup(dest):
    fd, b = tempfile.mkstemp(prefix=os.path.basename(dest) + f".bak.{ts}.", dir=os.path.dirname(dest))
    os.close(fd); shutil.copy2(dest, b); os.chmod(b, 0o600); out["backups"].append(b)

def ensure_dir(d):
    os.makedirs(d, mode=0o700, exist_ok=True)
    for x in [dest_root] if d.startswith(dest_root) else []:
        os.chmod(x, 0o700)

if pending_keys:
    try: out["agent_keys"] = post_import.merge_agent_keys(STAGE_ROOT, overwrite=overwrite)
    except post_import.KeyConflict: die("key overwrite approval required", 4)
    except (ValueError, OSError): die("unsafe keys or staging path")
for x in plan:
    dest = x["dest"]; ensure_dir(os.path.dirname(dest))
    if os.path.exists(dest): backup(dest)
    atomic_write(dest, x["data"], 0o700 if x["kind"] not in ("auth", "merge") and os.stat(x["src"]).st_mode & 0o111 else 0o600)
    out["installed"].append({"rel": x["rel"], "kind": x["kind"], "dest": dest, "home_rewrites": x["rewrites"]})
if db_changed:
    # a box WAL/SHM left beside a replaced agent.db would be replayed into it: back up and remove
    for n in DB[1:]:
        stale = os.path.join(dest_root, n)
        if os.path.isfile(stale) and n not in staged_rels:
            backup(stale); os.remove(stale)
if os.path.isdir(dest_root): os.chmod(dest_root, 0o700)
if dep_dirs: out["deps_installed"] = install_deps()
# packages, plugins, MCP binaries, Mac-only commands (post_import.py), then the parity check (parity_check.py)
import parity_check
out["post_install"] = post_import.run(cli, stage, dest_root, warnings)

if not noverify:
    cmd = p["verify"]
    try:
        # with ~/.agent-keys loaded, as run-agent.sh does
        r = subprocess.run(["bash", "-c", '[ -f "$HOME/.agent-keys" ] && . "$HOME/.agent-keys"; ' + cmd], capture_output=True, text=True, timeout=60)
        out["verify"] = {"cmd": cmd, "rc": r.returncode, "out": [], "note": "CLI output suppressed (may contain credentials)"}
    except Exception:
        out["verify"] = {"cmd": cmd, "rc": None, "out": [], "note": "status check failed or timed out"}

out["parity"] = parity_check.check(cli, no_run=noverify)
if not out["parity"]["ok"]:
    warnings.append("parity check found breaking gaps: " + "; ".join(f"{g['item']}: {g['detail']}" for g in out["parity"]["gaps"] + out["parity"].get("auth", []) if g["impact"] == "breaking")[:400])

# central config
sys.path.insert(0, os.path.join(here, "..", "..", "configure-agents", "scripts"))
verified = not noverify and (out.get("verify") or {}).get("rc") == 0
imported_auth = any(kind_of(rel) == "auth" for rel in staged_rels) and not (omp_auth and omp_auth["action"] == "kept_box_login")
if imported_auth and not verified:
    out["config"] = "unchanged: status check skipped or failed (files are installed; verify the login before enabling)"
elif imported_auth:
    try:
        import agents_config
        guard_path(agents_config.PATH)
        config = agents_config.load()
        if config is None: config = agents_config.default_config([])
        config["enabled"] = [a for a in agents_config.AGENTS if a == cli or a in config["enabled"]]
        # Keep other agents/settings; initialize only after a successful status check.
        config["agents"][cli].update(auth="imported", model="native", reasoning="native")
        if agents_config.validate(config): raise ValueError("invalid central config")
        agents_config.save(config)
        out["config"] = "enabled, auth=imported, model/reasoning=native"
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        out["config"] = "config update failed (output suppressed)"
else:
    out["config"] = "unchanged (no auth file imported)"

ROTATING = {"codex": ("auth.json", ".credentials.json"), "claude": (".credentials.json",), "pi": ("auth.json",), "omp": DB}[cli]
out["two_machine_warning"] = ("The same login is now used on the box and the user's computer. These CLIs refresh OAuth tokens and "
    "a refresh on one side can invalidate the other (Codex reports 'OAuth refresh token was rejected'; OpenAI says not to share "
    "one auth.json across machines). If either side gets logged out, log in again there. A separate `login` on the box avoids this.") \
    if any(i.get("rel") in ROTATING for i in out["installed"]) else None
if auth_notes: out["auth"] = auth_notes

if not keep:
    for x in plan:  # staged login files: overwrite before deleting
        if x["kind"] == "auth": post_import.shred(x["src"])
    shutil.rmtree(stage, ignore_errors=True); out["stage_removed"] = stage
if omp_auth: out["omp_auth"] = omp_auth
summarize_skipped(); out["installed"] = compact(out["installed"])
print(json.dumps(out, indent=2))
if omp_auth and omp_auth["action"] == "kept_box_login": sys.exit(5)
PY
