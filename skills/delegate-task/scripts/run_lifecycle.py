"""Private run inputs and atomic, lineage-wide restart accounting (stdlib only).

lineage.json lives in the original run. Reservations count even if startup fails;
a native rejection followed by a fresh replay consumes another restart. File
locking is short-lived and independent of the existing workdir ownership lock.
"""
import fcntl
import json
import os
import re
import sys
from pathlib import Path

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
AUTOMATIC_RESTARTS = 2


class LifecycleError(Exception):
    pass


def valid_id(value):
    return isinstance(value, str) and ID.fullmatch(value) is not None


def read_json(path):
    try:
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise LifecycleError("invalid_run_metadata")
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise LifecycleError("invalid_run_metadata")
        return value
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        raise LifecycleError("invalid_run_metadata") from None


def write_json(path, value):
    # Readers see either complete version. Callers serialize ledger mutations.
    tmp = path.with_name(path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def root_run(run):
    """Resolve new receipts and legacy resumed.json edges, including custom IDs."""
    run = Path(run).resolve()
    directory = run.parent
    if not valid_id(run.name) or not run.is_dir():
        raise LifecycleError("run_not_found")
    children = [p for p in directory.iterdir() if p.is_dir() and not p.is_symlink()]
    if len(children) > 4096:
        raise LifecycleError("run_inventory_limit")
    incoming = {}
    for child in children:
        receipt = read_json(child / "resumed.json")
        target = receipt.get("new_id")
        if valid_id(target):
            incoming[target] = child.name
    seen = set()
    while True:
        if run.name in seen:
            raise LifecycleError("invalid_run_lineage")
        seen.add(run.name)
        receipt = read_json(run / "lifecycle.json")
        origin = receipt.get("root_id")
        if origin and not valid_id(origin):
            raise LifecycleError("invalid_run_lineage")
        if origin == run.name:
            return run  # A recorded initial run may itself have a -rN name.
        if origin:
            parent = directory / origin
        elif run.name in incoming:
            parent = directory / incoming[run.name]
        else:
            match = re.fullmatch(r"(.+)-r([0-9]+)", run.name)
            candidate = directory / match[1] if match and int(match[2]) >= 2 else run
            parent = candidate if candidate.is_dir() else run
        if parent == run:
            return run
        if not parent.is_dir() or parent.is_symlink():
            raise LifecycleError("invalid_run_lineage")
        run = parent


def lineage(run):
    origin = root_run(run)
    ledger = read_json(origin / "lineage.json")
    attempts = ledger.get("attempts", [])
    if not isinstance(attempts, list) or any(not isinstance(a, dict) or not valid_id(a.get("id")) for a in attempts):
        raise LifecycleError("invalid_run_lineage")
    attempts = list(attempts)
    known = {a["id"] for a in attempts}
    # Import old receipts once; suffixes preserve attempts overwritten in legacy
    # resumed.json. New/custom names carry an explicit root receipt.
    frontier = {origin.name}
    directories = [p for p in origin.parent.iterdir() if p.is_dir() and not p.is_symlink()]
    for _ in range(len(directories) + 1):
        before = len(frontier)
        for child in directories:
            info = read_json(child / "lifecycle.json")
            suffix = re.fullmatch(re.escape(origin.name) + r"-r([0-9]+)", child.name)
            if info.get("root_id") == origin.name or (not info.get("root_id") and suffix and int(suffix[1]) >= 2):
                frontier.add(child.name)
            if child.name in frontier:
                target = read_json(child / "resumed.json").get("new_id")
                if valid_id(target):
                    frontier.add(target)
        if len(frontier) == before:
            break
    for name in sorted(frontier - {origin.name} - known):
        attempts.append({"id": name, "parent_id": origin.name, "attempt": len(attempts) + 2,
                         "extra_approved": False, "legacy": True})
    return {"schema": 1, "root_id": origin.name, "attempts": attempts}


def plan(run, new_id=None, allow_extra=False):
    info = lineage(run)
    if len(info["attempts"]) >= AUTOMATIC_RESTARTS and not allow_extra:
        raise LifecycleError("restart_approval_required")
    reserved = {a["id"] for a in info["attempts"]}
    if new_id is None:
        n = 2
        while (Path(run).parent / f"{info['root_id']}-r{n}").exists() or f"{info['root_id']}-r{n}" in reserved:
            n += 1
        new_id = f"{info['root_id']}-r{n}"
    if not valid_id(new_id):
        raise LifecycleError("invalid_run_id")
    if new_id in reserved or (Path(run).parent / new_id).exists():
        raise LifecycleError("run_id_exists")
    return {"root_id": info["root_id"], "id": new_id, "parent_id": Path(run).name,
            "attempt": len(info["attempts"]) + 2, "extra_approved": bool(allow_extra)}


def reserve(run, new_id=None, allow_extra=False):
    origin = root_run(run)
    # Never unlink the lock inode: all IDs in a lineage contend on the same fd.
    fd = os.open(origin / ".lineage.lock", os.O_RDWR | os.O_CREAT, 0o600)
    with os.fdopen(fd, "r+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        receipt = plan(run, new_id, allow_extra)
        ledger = lineage(run)
        ledger["attempts"].append(receipt)
        write_json(origin / "lineage.json", ledger)
        return receipt


def record_launch(run, values, inputs):
    timeout, agent, model, reasoning, permission, cwd, brief = values
    origin = os.environ.get("GBA_RESTART_ROOT", "")
    receipt = {"schema": 1, "root_id": run.name, "id": run.name, "attempt": 1}
    if origin:
        if not valid_id(origin):
            raise LifecycleError("invalid_run_lineage")
        ledger = lineage(run.parent / origin)
        matches = [a for a in ledger["attempts"] if a["id"] == run.name]
        if len(matches) != 1:
            raise LifecycleError("invalid_run_lineage")
        receipt.update(matches[0])
        receipt["root_id"] = origin
    write_json(run / "lifecycle.json", receipt)
    write_json(run / "launch.json", {"schema": 1, "agent": agent, "model": model,
               "reasoning": reasoning, "permission": permission, "timeout_sec": int(timeout),
               "cwd": str(Path(cwd).resolve()) if cwd else None,
               "prompt_file": str(Path(brief).resolve()) if brief else "prompt.md",
               "inputs": [str(Path(p).resolve()) for p in inputs]})


def main():
    try:
        action, run = sys.argv[1], Path(sys.argv[2]).resolve()
        if action == "record-launch":
            record_launch(run, sys.argv[3:10], sys.argv[10:])
            return 0
        new_id = sys.argv[3] or None
        allow = sys.argv[4] == "1"
        receipt = (reserve if action == "reserve" else plan)(run, new_id, allow)
        print(json.dumps(receipt, separators=(",", ":")))
        return 0
    except (LifecycleError, OSError, ValueError, IndexError) as error:
        code = str(error) if isinstance(error, LifecycleError) else "lifecycle_unavailable"
        print(json.dumps({"ok": False, "error": code}))
        return 6


if __name__ == "__main__":
    sys.exit(main())
