#!/usr/bin/env python3
"""Split a tarball of home-relative paths (made on the user's computer) into the per-CLI stage dirs.

  stage_tar.py <bundle.tgz> [--stage /workspace/uploads/agent-import] [--cli codex,claude,pi,omp]

Member mapping: .codex/* -> codex/*, .claude/* -> claude/*, .claude.json -> claude/_home/.claude.json,
.pi/agent/* -> pi/*, .omp/agent/* -> omp/*. Anything else is skipped.
Skips macOS metadata (._*, .DS_Store), *.pyc, symlinks/hardlinks/devices, absolute paths and '..'.
Never prints file contents. Stage dirs are created 700. Then run apply_import.sh per CLI.
"""
import argparse, json, os, sys, tarfile

MAP = [(".codex/", "codex/"), (".claude.json", "claude/_home/.claude.json"), (".claude/", "claude/"),
       (".pi/agent/", "pi/"), (".omp/agent/", "omp/")]

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
        for m in tf:
            name = m.name.lstrip("./") if m.name.startswith("./") else m.name
            base = os.path.basename(name.rstrip("/"))
            if m.isdir(): continue
            if name.startswith("/") or ".." in name.split("/"): skip("unsafe path"); continue
            if base.startswith("._") or base == ".DS_Store": skip("macOS metadata"); continue
            if base.endswith(".pyc") or "/__pycache__/" in "/" + name: skip("pyc"); continue
            if not m.isreg(): skip("link or special file"); continue
            t = target(name)
            if not t or t.split("/")[0] not in clis: skip("not a CLI path"); continue
            dest = os.path.join(a.stage, t)
            os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            src = tf.extractfile(m)
            with open(dest, "wb") as f: f.write(src.read())
            os.chmod(dest, 0o600)
            c = t.split("/")[0]; counts[c] = counts.get(c, 0) + 1
    print(json.dumps({"ok": True, "stage": a.stage, "staged_files": counts, "skipped": skipped}, indent=2))

if __name__ == "__main__":
    main()
