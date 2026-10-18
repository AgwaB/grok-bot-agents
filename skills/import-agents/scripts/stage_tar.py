#!/usr/bin/env python3
"""Split a tarball of home-relative paths (made on the user's computer) into the per-CLI stage dirs.

  stage_tar.py <bundle.tgz> [--stage /workspace/uploads/agent-import] [--cli codex,claude,pi,omp]

Member mapping: .codex/* -> codex/*, .claude/* -> claude/*, .claude.json -> claude/_home/.claude.json,
.pi/agent/* -> pi/*, .omp/agent/* -> omp/*. bundle_local.sh's .agent-import-manifest.txt -> _meta/manifest.txt;
its `home <dir>` roots (Pi packages outside ~/.pi/agent) -> pi/_home/<dir>/*. export_secrets.sh output:
.agent-import-secrets/claude/* -> claude/*, .agent-import-secrets/codex/* -> codex/*, secrets.env and rewrites.txt ->
_secrets/ (merged into ~/.agent-keys by apply_import.sh). Anything else is skipped.
Executable bits are kept (700), other files are 600.
Skips macOS metadata (._*, .DS_Store), *.pyc, symlinks/hardlinks/devices, absolute paths and '..'.
Never prints file contents. Stage dirs are created 700. Then run apply_import.sh per CLI.
"""
import argparse, json, os, sys, tarfile

MAP = [(".agent-import-secrets/claude/", "claude/"), (".agent-import-secrets/codex/", "codex/"),
       (".agent-import-secrets/secrets.env", "_secrets/secrets.env"), (".agent-import-secrets/rewrites.txt", "_secrets/rewrites.txt"),
       (".codex/", "codex/"), (".claude.json", "claude/_home/.claude.json"), (".claude/", "claude/"),
       (".pi/agent/", "pi/"), (".omp/agent/", "omp/")]
MANIFEST = ".agent-import-manifest.txt"

def target(name):
    for pre, dst in MAP:
        if pre.endswith("/") and name.startswith(pre): return dst + name[len(pre):]
        if name == pre: return dst
    return None

def main():
    ap = argparse.ArgumentParser(); ap.add_argument("bundle")
    ap.add_argument("--stage", default=os.environ.get("AGENT_IMPORT_STAGE", "/workspace/uploads/agent-import"))
    ap.add_argument("--cli", default="codex,claude,pi,omp")
    a = ap.parse_args(); clis = set(a.cli.split(","))
    os.makedirs(a.stage, mode=0o700, exist_ok=True); os.chmod(a.stage, 0o700)
    counts, skipped = {}, {}
    def skip(why): skipped[why] = skipped.get(why, 0) + 1
    with tarfile.open(a.bundle, "r:*") as tf:
        roots = []
        try:
            mf = tf.getmember(MANIFEST)
            text = tf.extractfile(mf).read().decode("utf-8", "replace") if mf.isreg() else ""
        except KeyError:
            text = ""
        if text:
            os.makedirs(os.path.join(a.stage, "_meta"), mode=0o700, exist_ok=True)
            mp = os.path.join(a.stage, "_meta", "manifest.txt")
            with open(mp, "w", encoding="utf-8") as f: f.write(text)
            os.chmod(mp, 0o600)
            for line in text.splitlines():
                parts = line.split(" ", 1)
                if parts[0] == "home" and len(parts) == 2:
                    r = parts[1].strip().strip("/")
                    if r and not r.startswith("/") and ".." not in r.split("/"): roots.append(r + "/")
        for m in tf:
            if (m.name[2:] if m.name.startswith("./") else m.name) == MANIFEST: continue
            name = m.name[2:] if m.name.startswith("./") else m.name
            base = os.path.basename(name.rstrip("/"))
            if m.isdir(): continue
            if name.startswith("/") or ".." in name.split("/"): skip("unsafe path"); continue
            if base.startswith("._") or base == ".DS_Store": skip("macOS metadata"); continue
            if base.endswith(".pyc") or "/__pycache__/" in "/" + name: skip("pyc"); continue
            if not m.isreg(): skip("link or special file"); continue
            t = target(name)
            if not t and "pi" in clis:
                t = next(("pi/_home/" + name for r in roots if name.startswith(r)), None)
            if not t or (t.split("/")[0] not in clis and not t.startswith("_secrets/")): skip("not a CLI path"); continue
            dest = os.path.join(a.stage, t)
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            src = tf.extractfile(m)
            with open(dest, "wb") as f: f.write(src.read())
            os.chmod(dest, 0o700 if m.mode & 0o111 and not t.startswith("_secrets/") else 0o600)
            c = t.split("/")[0]; counts[c] = counts.get(c, 0) + 1
    print(json.dumps({"ok": True, "stage": a.stage, "staged_files": counts, "skipped": skipped}, indent=2))

if __name__ == "__main__":
    main()
