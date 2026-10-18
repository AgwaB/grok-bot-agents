"""Shared filesystem boundaries for box-side imports; no file contents in errors."""
import os
import tempfile
from pathlib import Path

MAX_FILE = 200 * 1024 * 1024


def package_root(root):
    """Only explicit, non-sensitive home-relative package roots may escape the CLI dir."""
    parts = root.split("/")
    if not root or "\\" in root or ":" in root or any(p in ("", ".", "..") for p in parts):
        return False
    if root == ".pi/agent/packages" or root.startswith(".pi/agent/packages/"):
        return True
    return not parts[0].startswith(".") and parts[0] not in ("Library", "AppData")


def guard_path(path):
    """Reject existing symlink components (including dangling leaf links) before I/O.

    Not a concurrent-attacker sandbox: callers must own the staging/destination
    directories. Unique temp files avoid predictable-temp symlink clobbering.
    """
    p = Path(os.path.abspath(path))
    for part in (p, *p.parents):
        if part.is_symlink():
            raise ValueError("symlink in import path")


def atomic_write(path, data, mode=0o600):
    guard_path(path)
    parent = os.path.dirname(path)
    try:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    except OSError:
        raise ValueError("cannot create import directory") from None
    fd, tmp = tempfile.mkstemp(prefix=".agent-import-", dir=parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def claude_global(home):
    return os.path.join(os.environ.get("CLAUDE_CONFIG_DIR") or home, ".claude.json")
