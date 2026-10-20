"""Per-canonical-workdir ownership; the calling shell keeps fd 9 open until exit.

flock is inherited by native continuations (and agent children), so checkpoint and
launch form one critical section. Stable lock files live outside Git/work artifacts.
Linux uses procfs; other POSIX hosts need ps + lsof. Missing visibility fails closed.
"""
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from contextlib import suppress
from pathlib import Path


def lock_path(workdir):
    key = hashlib.sha256(os.fsencode(Path(workdir).resolve())).hexdigest()
    return Path.home() / ".grok-bot-agents/worktree-locks" / (key + ".lock")


def processes():
    """pid -> (ppid, start identity); omit zombies, which cannot hold a workdir."""
    result = {}
    if Path("/proc/self/stat").exists():
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                if fields[0] not in ("Z", "X"):
                    result[int(path.parent.name)] = (int(fields[1]), boot + ":" + fields[19])
            except (OSError, ValueError, IndexError):
                continue
    else:
        output = subprocess.check_output(["ps", "-axo", "pid=,ppid=,stat=,lstart="], text=True)
        for line in output.splitlines():
            pid, ppid, state, start = line.split(None, 3)
            if not state.startswith("Z"):
                result[int(pid)] = (int(ppid), start)
    return result


def ancestors(pid, procs):
    found = set()
    while pid in procs and pid not in found:
        found.add(pid)
        pid = procs[pid][0]
    return found


def descendants(roots, procs):
    found = set(roots)
    while True:
        children = {p for p, (parent, _) in procs.items() if parent in found}
        if children <= found:
            return found
        found.update(children)


def cwd_processes(workdir, procs, exclude):
    """Scan independently of agent.pid: old IDs and orphaned children are common."""
    directories = {}
    if Path("/proc/self/cwd").exists():
        for pid in procs.keys() - exclude:
            path = Path(f"/proc/{pid}")
            try:
                if path.stat().st_uid == os.getuid():
                    directories[pid] = os.readlink(path / "cwd")
            except FileNotFoundError:
                continue  # exited since the snapshot
            except PermissionError as exc:
                raise RuntimeError("cannot inspect a same-user process cwd; refusing ownership") from exc
    else:
        # lsof exit 1 means no matches; missing/failed inspection is not an empty scan.
        p = subprocess.run(["lsof", "-a", "-u", str(os.getuid()), "-d", "cwd", "-Fpn"],
                           text=True, capture_output=True)
        if p.returncode not in (0, 1) or p.stderr:
            raise RuntimeError("cannot inspect workdir processes with lsof")
        pid = None
        for line in p.stdout.splitlines():
            if line.startswith("p"):
                pid = int(line[1:])
            elif line.startswith("n") and pid in procs and pid not in exclude:
                directories[pid] = line[1:]
    return {pid for pid, cwd in directories.items()
            if Path(cwd) == workdir or workdir in Path(cwd).parents}


def recorded_owner(fd, procs):
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        owner = json.loads(os.read(fd, 4096))
        pid = owner["pid"]
        # The same shell can move on to a fresh workdir without changing PID/start.
        # Metadata alone is not proof it still owns this lock (fd 9 may now name B).
        if (procs.get(pid, (None, None))[1] == owner["start"]
                and os.path.samestat(os.stat(f"/proc/{pid}/fd/9"), os.fstat(fd))):
            return pid
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None


def signal_same_processes(targets, sig):
    if not targets:
        return
    if not hasattr(os, "pidfd_open") or not hasattr(signal, "pidfd_send_signal"):
        raise RuntimeError("safe --kill requires pidfd support; stop workdir processes manually on this host")
    handles = {}
    try:
        # Bind kernel process handles BEFORE checking start identity. A recycled PID
        # between the check and signal must never target the replacement process.
        for pid in targets:
            with suppress(ProcessLookupError):
                handles[pid] = os.pidfd_open(pid)
        current = processes()
        for pid, handle in handles.items():
            if current.get(pid, (None, None))[1] == targets[pid]:
                with suppress(ProcessLookupError):
                    signal.pidfd_send_signal(handle, sig)
    finally:
        for handle in handles.values():
            os.close(handle)


def guard(workdir, owner_pid, fd, kill=False, dry=False):
    deadline = time.monotonic() + 12
    signalled = {}
    term_at = None
    published = False
    while True:
        procs = processes()
        excluded = ancestors(os.getpid(), procs)
        live = cwd_processes(workdir, procs, excluded)
        locked = False
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                pass
        else:
            locked = True  # write-free dry-run, no existing lock file
        owner = recorded_owner(fd, procs) if fd is not None and not locked else None
        if locked and not dry and not published:
            # Publish on acquisition, BEFORE draining legacy cwd processes. Otherwise
            # a concurrent --kill can mistake stale metadata for the new lock holder.
            assert fd is not None  # acquire always receives the shell's open fd
            identity = procs[owner_pid][1]
            data = json.dumps({"pid": owner_pid, "start": identity, "workdir": str(workdir)}) + "\n"
            os.ftruncate(fd, 0)
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, data.encode())
            published = True
        if locked and not live:
            return
        if not kill:
            raise RuntimeError("workdir is still alive or locked; pass --kill to stop it first")
        if owner in excluded:
            raise RuntimeError("workdir is owned by this invocation's ancestor; cannot safely stop it")
        if dry:
            print("would stop live workdir process trees before checkpoint/launch", file=sys.stderr)
            return
        # Let the owning runner finish its summary/cleanup after its children stop.
        # If it cannot exit, terminate it too after a short grace period.
        roots = live | ({owner} if owner else set())
        targets = descendants(roots, procs) - excluded
        if term_at is None or time.monotonic() - term_at < 3:
            targets.discard(owner)
        new = {p: procs[p][1] for p in targets if p in procs and p not in signalled}
        signal_same_processes(new, signal.SIGTERM)
        signalled.update(new)
        if term_at is None:
            term_at = time.monotonic()
        if time.monotonic() - term_at >= 3:
            signal_same_processes(signalled, signal.SIGKILL)
        if time.monotonic() >= deadline:
            raise RuntimeError("workdir remains locked/busy after --kill; no checkpoint or launch performed")
        time.sleep(0.1)


def main():
    mode, directory = sys.argv[1:3]
    workdir = Path(directory).resolve()
    path = lock_path(workdir)
    if mode == "path":
        print(path)
        return
    if mode == "inherited":
        try:
            valid = (os.environ.get("DELEGATE_LOCK_WORKDIR") == str(workdir)
                     and os.path.samestat(os.fstat(9), path.stat()))
        except OSError:
            valid = False
        sys.exit(0 if valid else 1)
    dry = mode == "check"
    fd = None
    try:
        if dry:
            if path.exists():
                # A native-fallback shell can already own this exact lock on
                # fd 9. Probe a duplicate of that description, not a new open
                # that would conflict with our own still-held ownership.
                inherited = False
                with suppress(OSError):
                    inherited = (os.environ.get("DELEGATE_LOCK_WORKDIR") == str(workdir)
                                 and os.path.samestat(os.fstat(9), path.stat()))
                fd = os.dup(9) if inherited else os.open(path, os.O_RDONLY)
        else:
            fd = 9  # opened by the shell, inherited by every child until the run ends
        guard(workdir, int(sys.argv[3]), fd, kill="--kill" in sys.argv[4:], dry=dry)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(json.dumps({"ok": False, "error": str(exc)}))
        sys.exit(5)
    finally:
        if dry and fd is not None:
            os.close(fd)


if __name__ == "__main__":
    main()
