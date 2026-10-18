#!/usr/bin/env bash
# Install staged CLI files (copied from the user's computer with CopyToBox) into the box's CLI home.
#   apply_import.sh <codex|claude|pi|omp> [--src-home /Users/alice] [--dry-run] [--overwrite] [--keep-stage] [--no-verify]
# Stage: ${AGENT_IMPORT_STAGE:-/workspace/uploads/agent-import}/<cli>/<rel path as in import_plan.py>
# - Installs only allowlisted paths; anything else (SSH keys, other dotfiles, caches, sessions) is skipped and reported.
# - Never prints file contents. Rejects symlinks and non-regular files.
# - An existing different file is a conflict: exits 4 unless --overwrite, which first backs it up to <file>.bak.<ts>.
# - Rewrites the user's home prefix (--src-home) to the box $HOME in text config files; warns about other absolute paths.
# - Claude: only the mcpServers key of ~/.claude.json is merged.
# - Files 600, dirs 700. Runs the CLI's status check; if it passes, updates ~/.grok-agents/config.json
#   (enable, auth=imported, model/reasoning=native so the imported settings apply).
# - Deletes the staged files for that CLI afterwards unless --keep-stage (they contain tokens).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
exec python3 - "$HERE" "$@" <<'PY'
import datetime, fnmatch, hashlib, json, os, re, shutil, stat, subprocess, sys
here, args = sys.argv[1], sys.argv[2:]
sys.path.insert(0, here)
from import_plan import PLAN

def die(msg, code=2):
    print(json.dumps({"ok": False, "error": msg})); sys.exit(code)

if not args or args[0] not in PLAN: die("usage: apply_import.sh <codex|claude|pi|omp> [--src-home H] [--dry-run] [--overwrite] [--keep-stage] [--no-verify]")
cli = args[0]; opts = args[1:]
dry, overwrite, keep, noverify = "--dry-run" in opts, "--overwrite" in opts, "--keep-stage" in opts, "--no-verify" in opts
src_home = None
if "--src-home" in opts:
    i = opts.index("--src-home"); src_home = opts[i + 1] if i + 1 < len(opts) else die("--src-home needs a value")
HOME = os.path.expanduser("~")
stage = os.path.join(os.environ.get("AGENT_IMPORT_STAGE", "/workspace/uploads/agent-import"), cli)
if not os.path.isdir(stage): die(f"nothing staged at {stage}")
p = PLAN[cli]
env_dir = {"codex": os.environ.get("CODEX_HOME"), "claude": os.environ.get("CLAUDE_CONFIG_DIR")}.get(cli)
dest_root = env_dir or os.path.join(HOME, p["home"])

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
    if parts[0] in SKIP_PARTS or set(parts) & {"__pycache__", "node_modules", ".git"}: return None
    for pat, k in allow:
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

def transform(data, rel, kind):
    """Return (bytes, rewrites, leftover_abs_count). Auth/db files are left alone."""
    if kind == "auth" or not rel.endswith(TEXT): return data, 0, 0
    try: t = data.decode("utf-8")
    except UnicodeDecodeError: return data, 0, 0
    n = 0
    for v in home_variants:
        n += t.count(v); t = t.replace(v, HOME)
    return t.encode("utf-8"), n, len(ABS.findall(t))

ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
plan, skipped, conflicts, warnings = [], [], [], []
for root, dirs, files in os.walk(stage, followlinks=False):
    for name in dirs[:]:
        if os.path.islink(os.path.join(root, name)):
            skipped.append({"path": os.path.relpath(os.path.join(root, name), stage), "why": "symlink (target not imported): " + os.readlink(os.path.join(root, name))}); dirs.remove(name)
    for name in files:
        full = os.path.join(root, name); rel = os.path.relpath(full, stage).replace(os.sep, "/")
        if name.startswith("._") or name == ".DS_Store":
            skipped.append({"path": rel, "why": "macOS metadata"}); continue
        st = os.lstat(full)
        if stat.S_ISLNK(st.st_mode):
            skipped.append({"path": rel, "why": "symlink (target not imported): " + os.readlink(full)}); continue
        if not stat.S_ISREG(st.st_mode): skipped.append({"path": rel, "why": "not a regular file"}); continue
        if st.st_size > MAX: skipped.append({"path": rel, "why": "too large"}); continue
        k = kind_of(rel)
        if not k: skipped.append({"path": rel, "why": "not on the allowlist for " + cli}); continue
        if k == "merge":
            plan.append({"rel": rel, "kind": k, "src": full, "dest": os.path.join(HOME, rel[len("_home/"):])}); continue
        dest = os.path.join(dest_root, rel)
        data, nrw, nabs = transform(open(full, "rb").read(), rel, k)
        if nabs: warnings.append(f"{rel}: {nabs} absolute path(s) from the other computer remain; check them")
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
if cli == "claude" and not any(x["rel"] == ".credentials.json" for x in plan) and not os.path.exists(os.path.join(dest_root, ".credentials.json")):
    warnings.append("no Claude credentials imported (macOS keeps them in Keychain); run `claude auth login` on the box")

if conflicts and not overwrite:
    out.update(ok=False, error="existing box files differ; ask the user, then rerun with --overwrite (backs them up)")
    out["planned"] = compact([x["rel"] for x in plan]); summarize_skipped()
    print(json.dumps(out, indent=2)); sys.exit(4)
if dry:
    out["planned"] = [{"rel": x["rel"], "kind": x["kind"], "dest": x["dest"], "home_rewrites": x.get("rewrites", 0), "overwrites": x["rel"] in conflicts} for x in plan]
    out["planned"] = compact(out["planned"]); summarize_skipped(); print(json.dumps(out, indent=2)); sys.exit(0)

def backup(dest):
    b = f"{dest}.bak.{ts}"; shutil.copy2(dest, b); os.chmod(b, 0o600); out["backups"].append(b)

def ensure_dir(d):
    os.makedirs(d, mode=0o700, exist_ok=True)
    for x in [dest_root] if d.startswith(dest_root) else []:
        os.chmod(x, 0o700)

for x in plan:
    if x["kind"] == "merge":
        try: src = json.load(open(x["src"], encoding="utf-8"))
        except Exception: warnings.append(f"{x['rel']}: not valid JSON, skipped"); continue
        servers = src.get("mcpServers") or {}
        if not servers: skipped.append({"path": x["rel"], "why": "no mcpServers to merge"}); continue
        s = json.dumps(servers)
        for v in home_variants: s = s.replace(json.dumps(v)[1:-1], json.dumps(HOME)[1:-1])
        servers = json.loads(s)
        if ABS.search(s): warnings.append(".claude.json mcpServers: some server paths point at the other computer; those servers won't start on the box")
        dest = x["dest"]; cur = {}
        if os.path.exists(dest):
            cur = json.load(open(dest, encoding="utf-8")); backup(dest)
        existing = cur.get("mcpServers") or {}
        clash = [k for k in servers if k in existing and existing[k] != servers[k]]
        if clash and not overwrite:
            warnings.append(f"mcpServers kept as on box (differ, rerun with --overwrite to replace): {clash}")
            servers = {k: v for k, v in servers.items() if k not in clash}
        existing.update(servers); cur["mcpServers"] = existing
        tmp = dest + ".tmp"; json.dump(cur, open(tmp, "w", encoding="utf-8"), indent=2); os.chmod(tmp, 0o600); os.replace(tmp, dest)
        out["installed"].append({"rel": x["rel"], "dest": dest, "merged_mcp_servers": sorted(servers)})
        continue
    dest = x["dest"]; ensure_dir(os.path.dirname(dest))
    if os.path.exists(dest): backup(dest)
    tmp = dest + ".import-tmp"
    with open(tmp, "wb") as f: f.write(x["data"])
    os.chmod(tmp, 0o600); os.replace(tmp, dest)
    out["installed"].append({"rel": x["rel"], "kind": x["kind"], "dest": dest, "home_rewrites": x["rewrites"]})
if os.path.isdir(dest_root): os.chmod(dest_root, 0o700)

if not noverify:
    cmd = p["verify"]
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
        txt = (r.stdout + r.stderr).strip().splitlines()
        if cli == "pi": txt = [f"{len(txt)} lines of models listed"] if r.returncode == 0 else txt[-3:]
        out["verify"] = {"cmd": cmd, "rc": r.returncode, "out": [l[:160] for l in txt[:8]]}
    except Exception as e:
        out["verify"] = {"cmd": cmd, "rc": None, "out": [str(e)[:200]]}

# central config
cfg = os.path.join(here, "..", "..", "configure-agents", "scripts", "agents_config.py")
verified = noverify or (out.get("verify") or {}).get("rc") == 0
if any(i.get("kind") == "auth" for i in out["installed"]) and not verified:
    out["config"] = "unchanged: the status check failed, so fix the login first (files are installed; backups listed)"
elif any(i.get("kind") == "auth" for i in out["installed"]):
    r1 = subprocess.run([sys.executable, cfg, "enable", cli], capture_output=True, text=True)
    # follow the imported native model/reasoning: "native" = the runner passes no --model / reasoning flag
    r2 = subprocess.run([sys.executable, cfg, "set", f"agents.{cli}.auth", "imported",
                         f"agents.{cli}.model", "native", f"agents.{cli}.reasoning", "native"], capture_output=True, text=True)
    out["config"] = "enabled, auth=imported, model/reasoning=native" if r1.returncode == 0 and r2.returncode == 0 else ("config update failed: " + (r1.stdout + r1.stderr + r2.stdout + r2.stderr).strip()[-300:])
else:
    out["config"] = "unchanged (no auth file imported)"

out["two_machine_warning"] = ("The same login is now used on the box and the user's computer. These CLIs refresh OAuth tokens and "
    "a refresh on one side can invalidate the other (Codex reports 'OAuth refresh token was rejected'). If either side gets "
    "logged out, log in again there. A separate `login` on the box avoids this.") if any(i.get("kind") == "auth" for i in out["installed"]) else None

if not keep:
    shutil.rmtree(stage, ignore_errors=True); out["stage_removed"] = stage
summarize_skipped(); out["installed"] = compact(out["installed"])
print(json.dumps(out, indent=2))
PY
