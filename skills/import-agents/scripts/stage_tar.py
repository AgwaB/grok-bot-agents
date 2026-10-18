#!/usr/bin/env python3
"""Stage allowlisted home-relative tar members, never links or metadata.

CLI roots map to per-CLI stage dirs; consented exports map to _secrets or
Claude/Codex auth paths. Explicit safe manifest roots map to pi/_home.
Files are streamed with a 200 MiB per-file limit; diagnostics contain no content.
"""
import argparse
import json
import os
import shutil
import sys
import tarfile
import tempfile

from import_safety import MAX_FILE, atomic_write, guard_path, package_root

MAP = [(".agent-import-secrets/claude/", "claude/"), (".agent-import-secrets/codex/", "codex/"),
       (".agent-import-secrets/secrets.env", "_secrets/secrets.env"),
       (".agent-import-secrets/rewrites.txt", "_secrets/rewrites.txt"),
       (".codex/", "codex/"), (".claude.json", "claude/_home/.claude.json"),
       (".claude/", "claude/"), (".pi/agent/", "pi/"), (".omp/agent/", "omp/")]
MANIFEST = ".agent-import-manifest.txt"


def target(name):
    for pre, dst in MAP:
        if pre.endswith("/") and name.startswith(pre):
            return dst + name[len(pre):]
        if name == pre:
            return dst
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("bundle")
    ap.add_argument("--stage", default=os.environ.get("AGENT_IMPORT_STAGE", "/workspace/uploads/agent-import"))
    ap.add_argument("--cli", default="codex,claude,pi,omp")
    a = ap.parse_args()
    clis = set(a.cli.split(","))
    if not clis <= {"codex", "claude", "pi", "omp"}:
        raise ValueError("unknown cli")
    guard_path(a.stage)
    try:
        os.makedirs(a.stage, mode=0o700, exist_ok=True)
        os.chmod(a.stage, 0o700)
    except OSError:
        raise ValueError("cannot create stage") from None
    counts, skipped = {}, {}

    def skip(why):
        skipped[why] = skipped.get(why, 0) + 1

    with tarfile.open(a.bundle, "r:*") as tf:
        roots = []
        for mf in tf.getmembers():
            if mf.name not in (MANIFEST, "./" + MANIFEST) or not mf.isreg() or mf.size > 1024 * 1024:
                continue
            stream = tf.extractfile(mf)
            if stream is None:
                continue
            with stream:
                text = stream.read().decode("utf-8", "replace")
            atomic_write(os.path.join(a.stage, "_meta", "manifest.txt"), text.encode("utf-8"))
            for line in text.splitlines():
                if line.startswith("home ") and package_root(line[5:].strip()):
                    roots.append(line[5:].strip() + "/")
            break
        for m in tf:
            name = m.name[2:] if m.name.startswith("./") else m.name
            if name == MANIFEST or m.isdir():
                continue
            parts = name.split("/")
            if name.startswith("/") or "\\" in name or ".." in parts:
                skip("unsafe path")
                continue
            if any(p.startswith("._") or p == ".DS_Store" for p in parts):
                skip("macOS metadata")
                continue
            if name.endswith(".pyc") or "__pycache__" in parts:
                skip("pyc")
                continue
            if not m.isreg():
                skip("link or special file")
                continue
            if m.size > MAX_FILE:
                skip("too large")
                continue
            t = target(name)
            if not t and "pi" in clis:
                t = next(("pi/_home/" + name for r in roots if name.startswith(r)), None)
            if not t or (t.split("/")[0] not in clis and not t.startswith("_secrets/")):
                skip("not a CLI path")
                continue
            dest = os.path.join(a.stage, t)
            guard_path(dest)
            try:
                os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
            except OSError:
                raise ValueError("cannot create stage directory") from None
            src = tf.extractfile(m)
            if src is None:
                continue
            fd, tmp = tempfile.mkstemp(prefix=".agent-import-", dir=os.path.dirname(dest))
            try:
                with os.fdopen(fd, "wb") as f, src:
                    shutil.copyfileobj(src, f, length=1024 * 1024)
                os.chmod(tmp, 0o700 if m.mode & 0o111 and not t.startswith("_secrets/") else 0o600)
                os.replace(tmp, dest)
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)
            cli = t.split("/")[0]
            counts[cli] = counts.get(cli, 0) + 1
    print(json.dumps({"ok": True, "stage": a.stage, "staged_files": counts, "skipped": skipped}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, tarfile.TarError):
        print(json.dumps({"ok": False, "error": "unsafe stage path or unreadable archive"}))
        sys.exit(2)
