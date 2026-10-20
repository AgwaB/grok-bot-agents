"""Private run inputs and atomic, lineage-wide restart accounting (stdlib only).

lineage.json lives in the original run. Reservations count even if startup fails;
a native rejection followed by a fresh replay consumes another restart. File
locking is short-lived and independent of the existing workdir ownership lock.
Reservations also own custom IDs before a child directory or receipt exists.
"""
import importlib.util
import json
import os
import re
import sys
from contextlib import contextmanager
from pathlib import Path

ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
AUTOMATIC_RESTARTS = 2

# Load by trusted sibling path, including when gba imports this module by spec.
_wake_spec = importlib.util.spec_from_file_location("_gba_wake_transport", Path(__file__).with_name("wake.py"))
assert _wake_spec is not None and _wake_spec.loader is not None
wake = importlib.util.module_from_spec(_wake_spec)
_wake_spec.loader.exec_module(wake)


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
        if hasattr(os, "O_DIRECTORY"):
            directory_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    finally:
        tmp.unlink(missing_ok=True)


@contextmanager
def locked(path):
    # Never unlink the inode: all contenders must lock the same object. POSIX
    # flock and Windows byte-range locks release automatically on process exit.
    fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "r+b") as stream:
        if os.name == "nt":
            import msvcrt
            if os.fstat(stream.fileno()).st_size == 0:
                stream.write(b"\0")
                stream.flush()
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def wake_state(run):
    return read_wake_state(root_run(run))


def read_wake_state(origin):
    value = read_json(origin / "wake.json")
    if not value:
        if (origin / "wake.json").exists():
            raise LifecycleError("invalid_wake_state")
        return {"schema": 1, "mode": "supervisor", "requested_mode": "supervisor",
                "claimed": False, **wake.outcome("skipped", "legacy_supervisor")}
    if (value.get("schema") != 1 or value.get("mode") not in {"webhook", "supervisor", "none"}
            or value.get("requested_mode") not in wake.MODES or type(value.get("claimed")) is not bool
            or value.get("outcome") not in {"pending", "sending", "sent", "failed", "skipped"}):
        raise LifecycleError("invalid_wake_state")
    return value


def supervisor_handoff(run, action="handoff"):
    """Disable the entire lineage before publishing handoff or killing an owner."""
    run = Path(run)
    origin = root_run(run)
    with locked(origin / ".wake.lock"):
        state = wake_state(run)
        if state["claimed"]:
            raise LifecycleError("webhook_already_claimed")
        handoff = read_json(run / "supervisor.handoff.json")
        generation = handoff.get("generation", 0)
        if type(generation) is not int or not 0 <= generation < 1000000:
            raise LifecycleError("invalid_handoff")
        if action == "started" and handoff.get("state") != "pending":
            raise LifecycleError("handoff_not_pending")
        if action == "handoff" and handoff.get("state") == "active":
            raise LifecycleError("supervisor_already_active")
        if action == "rotate" or (action == "recover" and handoff.get("state") != "pending") or not generation:
            generation += 1
        state.update(mode="supervisor", **wake.outcome("skipped", "supervisor_handoff"))
        # If interrupted between these writes, the webhook is still disabled and
        # `next` will request a supervisor; never the other way around.
        write_json(origin / "wake.json", state)
        handoff = {"schema": 1, "state": "active" if action == "started" else "pending",
                   "generation": generation, "updated_at": wake.utc_now()}
        write_json(run / "supervisor.handoff.json", handoff)
        return handoff


def finish_wake(run):
    """Results exist first; only the current lineage head can claim delivery.

    The durable claim is never cleared, even on crash/failed delivery. Claiming
    and supervisor ownership use the same lock; network I/O does not hold it.
    """
    run = Path(run)
    origin = root_run(run)
    with locked(origin / ".wake.lock"):
        state = wake_state(run)
        info = lineage(run)
        head = info["attempts"][-1]["id"] if info["attempts"] else origin.name
        if state["claimed"] or head != run.name:
            return state
        if state["mode"] != "webhook":
            state.update(wake.outcome("skipped", "supervisor_handoff" if state["mode"] == "supervisor" else "wake_disabled"))
            write_json(origin / "wake.json", state)
            return state
        meta = read_json(run / "meta.json")
        status, agent = meta.get("status"), meta.get("agent")
        if status not in {"ok", "error", "timeout", "rate_limited", "killed", "stalled"} or agent not in {"claude", "codex", "pi", "omp"}:
            raise LifecycleError("terminal_state_missing")
        _, error = wake.settings()
        if error:
            state.update(wake.outcome("skipped", error))
            write_json(origin / "wake.json", state)
            return state
        reason = meta.get("error_kind")
        if reason not in {"conn_error", "incomplete_output", "auth_error", "killed", "stall_terminal"}:
            reason = status
        ended = meta.get("ended_at")
        if not isinstance(ended, str) or not re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ", ended):
            ended = wake.utc_now()
        payload = {"event": "run_done", "run_id": run.name, "root_id": origin.name,
                   "agent": agent, "status": status, "reason": reason,
                   "ended_at": ended, "note": wake.NOTE}
        state.update(claimed=True, run_id=run.name, ended_at=ended,
                     **wake.outcome("sending", "delivery_claimed"))
        write_json(origin / "wake.json", state)
    delivered = wake.send_bounded(payload)
    with locked(origin / ".wake.lock"):
        state.update(delivered)
        write_json(origin / "wake.json", state)
    return state


def inventory(run, initial=False):
    """Resolve all ownership evidence, including reservations with no child yet.

    Forward pointers are only legacy evidence: they can be overwritten. Durable
    reservations and child receipts must agree; no last-writer-wins ownership.
    """
    run = Path(run)
    if run.is_symlink() or not valid_id(run.name) or not run.is_dir():
        raise LifecycleError("run_not_found")
    run = run.resolve()
    children = {p.name: p for p in run.parent.iterdir()
                if valid_id(p.name) and p.is_dir() and not p.is_symlink()}
    if len(children) > 4096:
        raise LifecycleError("run_inventory_limit")
    claims, roots, ledgers, receipts, reservations = {}, set(), {}, {}, {}

    def claim(name, owner):
        claims.setdefault(name, set()).add(owner)

    def metadata(child, filename):
        path = child / filename
        value = read_json(path)
        if not value and (path.exists() or path.is_symlink()):
            raise LifecycleError("invalid_run_lineage")
        return value

    for name, child in children.items():
        receipt = metadata(child, "lifecycle.json")
        if receipt:
            origin, attempt = receipt.get("root_id"), receipt.get("attempt")
            if (receipt.get("schema") != 1 or receipt.get("id") != name
                    or not valid_id(origin) or type(attempt) is not int or attempt < 1
                    or (origin == name and attempt != 1)
                    or type(receipt.get("extra_approved", False)) is not bool
                    or (origin == name and receipt.get("parent_id") is not None)
                    or (origin != name and (attempt < 2 or not valid_id(receipt.get("parent_id"))))):
                raise LifecycleError("invalid_run_lineage")
            receipts[name] = receipt
            roots.add(origin)
            if origin != name:
                claim(name, origin)
        ledger = metadata(child, "lineage.json")
        if ledger:
            attempts = ledger.get("attempts")
            if ledger.get("schema") != 1 or ledger.get("root_id") != name or not isinstance(attempts, list):
                raise LifecycleError("invalid_run_lineage")
            known = {name}
            for index, attempt in enumerate(attempts, 2):
                if (not isinstance(attempt, dict) or not valid_id(attempt.get("id"))
                        or attempt["id"] in known or not valid_id(attempt.get("parent_id"))
                        or attempt["parent_id"] not in known
                        or type(attempt.get("attempt")) is not int or attempt["attempt"] != index
                        or attempt.get("root_id", name) != name
                        or type(attempt.get("extra_approved", False)) is not bool
                        or type(attempt.get("legacy", False)) is not bool):
                    raise LifecycleError("invalid_run_lineage")
                target = attempt["id"]
                if target in reservations:
                    raise LifecycleError("invalid_run_lineage")
                reservations[target] = (name, attempt)
                known.add(target)
                claim(target, name)
            ledgers[name] = ledger
            roots.add(name)
        forward = metadata(child, "resumed.json")
        if forward:
            target = forward.get("new_id")
            if (not valid_id(target) or target == name
                    or forward.get("resumed_from", name) != name):
                raise LifecycleError("invalid_run_lineage")
            claim(target, name)

    # Published receipts must match the reservation, not just its root name.
    for name, receipt in receipts.items():
        if name in reservations:
            owner, reserved = reservations[name]
            if (receipt["root_id"] != owner
                    or any(receipt.get(key) != reserved.get(key) for key in ("parent_id", "attempt"))
                    or receipt.get("extra_approved", False) != reserved.get("extra_approved", False)):
                raise LifecycleError("invalid_run_lineage")

    # An exclusively allocated initial launch may legitimately use a -rN ID.
    # Only suffix guesses are overridden; durable/legacy ownership still fails.
    if initial:
        roots.add(run.name)
    resolved = {}

    def resolve(name, seen=frozenset()):
        if name in resolved:
            return resolved[name]
        if name in seen or len(seen) > 128:
            raise LifecycleError("invalid_run_lineage")
        parents = claims.get(name, set())
        # Suffix inference is only for legacy runs with no stronger evidence.
        if not parents and name not in roots:
            match = re.fullmatch(r"(.+)-r([0-9]+)", name)
            if match and match[2].lstrip("0") not in ("", "1") and match[1] in children:
                parents = {match[1]}
        owners = {resolve(parent, seen | {name}) for parent in parents}
        if name in roots or not parents:
            owners.add(name)
        if len(owners) != 1:
            raise LifecycleError("invalid_run_lineage")
        owner = owners.pop()
        if owner not in children:
            raise LifecycleError("invalid_run_lineage")
        resolved[name] = owner
        return owner

    for name in children.keys() | claims.keys() | roots:
        resolve(name)
    for receipt in receipts.values():
        if receipt["root_id"] != receipt["id"] and resolve(receipt["parent_id"]) != receipt["root_id"]:
            raise LifecycleError("invalid_run_lineage")
    return run.parent / resolved[run.name], ledgers, resolved


def root_run(run):
    return inventory(run)[0]


def lineage(run):
    origin, ledgers, owners = inventory(run)
    attempts = list(ledgers.get(origin.name, {}).get("attempts", []))
    known = {a["id"] for a in attempts}
    # Preserve legacy attempts, including a forward pointer to an unallocated ID.
    frontier = {name for name, owner in owners.items() if owner == origin.name}
    for name in sorted(frontier - {origin.name} - known):
        attempts.append({"id": name, "parent_id": origin.name, "attempt": len(attempts) + 2,
                         "extra_approved": False, "legacy": True})
    return {"schema": 1, "root_id": origin.name, "attempts": attempts}


def plan(run, new_id=None, allow_extra=False):
    info = lineage(run)
    if wake_state(run)["claimed"]:
        raise LifecycleError("webhook_already_claimed")
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
    # A reservation owns the name even before mkdir or lifecycle publication.
    _, _, allocated = inventory(run)
    destination = Path(run).parent / new_id
    if new_id in reserved or new_id in allocated or destination.exists() or destination.is_symlink():
        raise LifecycleError("run_id_exists")
    return {"root_id": info["root_id"], "id": new_id, "parent_id": Path(run).name,
            "attempt": len(info["attempts"]) + 2, "extra_approved": bool(allow_extra)}


def reserve(run, new_id=None, allow_extra=False):
    origin = root_run(run)
    # Serialize reservations across roots too: custom IDs share one namespace.
    # Never unlink the inode. A reservation is durable before child allocation.
    with locked(origin.parent / ".lineage.lock"):
        receipt = plan(run, new_id, allow_extra)
        supervisor_handoff(run, "recover")
        ledger = lineage(run)
        ledger["attempts"].append(receipt)
        write_json(origin / "lineage.json", ledger)
        return receipt


def recovery_inputs(launch):
    # Legacy cwd has no launch-time classification. Never guess from its current
    # git state or cwd != workdir: a repository checkout also differs from wt.
    if launch and launch.get("schema", 1) not in (1, 2):
        raise LifecycleError("invalid_run_metadata")
    key = "effective_inputs" if launch.get("schema") == 2 else "inputs"
    if launch.get("schema") == 2 and (launch.get("kind") not in {"repo", "work", "review"}
                                      or key not in launch):
        raise LifecycleError("invalid_run_metadata")
    inputs = launch.get(key, [])
    if not isinstance(inputs, list) or any(not isinstance(p, str) or not p for p in inputs):
        raise LifecycleError("invalid_run_metadata")
    return inputs


def record_launch(run, values, inputs, effective_inputs):
    timeout, agent, model, reasoning, permission, cwd, brief, kind = values
    try:
        timeout_sec = int(timeout)
    except ValueError:
        raise LifecycleError("invalid_run_metadata") from None
    if kind not in {"repo", "work", "review"} or timeout_sec <= 0:
        raise LifecycleError("invalid_run_metadata")
    origin = os.environ.get("GBA_RESTART_ROOT", "")
    if inventory(run, initial=not origin)[0].name != (origin or run.name):
        raise LifecycleError("invalid_run_lineage")
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
    requested = os.environ.get("GBA_WAKE_MODE", "auto")
    if origin:
        selected = wake_state(run)["mode"]
    else:
        config = read_json(Path(os.environ["GBA_LAUNCH_CONFIG"])) if os.environ.get("GBA_LAUNCH_CONFIG") else {}
        selected = wake.select(requested, timeout_sec, config)
        _, error = wake.settings() if selected == "webhook" else (None, None)
        initial = wake.outcome("skipped", error) if error else wake.outcome("pending", "awaiting_completion")
        with locked(run / ".wake.lock"):
            if (run / "wake.json").exists() or (run / "wake.json").is_symlink():
                # An early next/handoff may have claimed this run while workdir
                # preparation was still in progress. Initialization is create-only.
                selected = read_wake_state(run)["mode"]
            else:
                write_json(run / "wake.json", {"schema": 1, "mode": selected, "requested_mode": requested,
                           "claimed": False, **initial})
    receipt["wake_mode"] = selected
    write_json(run / "lifecycle.json", receipt)
    write_json(run / "launch.json", {"schema": 2, "wake_mode": selected, "wake_requested": requested,
               "kind": kind, "agent": agent, "model": model,
               "reasoning": reasoning, "permission": permission, "timeout_sec": timeout_sec,
               "cwd": str(Path(cwd).resolve()) if cwd else None,
               "prompt_file": str(Path(brief).resolve()) if brief else "prompt.md",
               "inputs": [str(Path(p).resolve()) for p in inputs],
               "effective_inputs": [str(Path(p).resolve()) for p in effective_inputs]})


def main():
    try:
        action, run = sys.argv[1], Path(sys.argv[2]).resolve()
        if action in {"finish-wake", "supervisor-handoff"}:
            value = finish_wake(run) if action == "finish-wake" else supervisor_handoff(run, "recover")
            print(json.dumps(value, separators=(",", ":")))
            return 0
        if action == "record-launch":
            count = int(sys.argv[11])
            if count < 0 or count > len(sys.argv[12:]):
                raise LifecycleError("invalid_run_metadata")
            record_launch(run, sys.argv[3:11], sys.argv[12:12 + count], sys.argv[12 + count:])
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
