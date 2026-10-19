"""Linux process-tree CPU and TCP evidence; unavailable visibility is never idle proof."""
import os
import re
import sys
from pathlib import Path


def tcp_state(pids, proc=Path("/proc")):
    unknown = False
    for pid in pids:
        directory = proc / str(pid)
        sockets = set()
        try:
            for fd in (directory / "fd").iterdir():
                try:
                    target = os.readlink(fd)
                except FileNotFoundError:  # closed since enumeration
                    continue
                if target.startswith("socket:["):
                    sockets.add(target[8:-1])
            if not sockets:
                continue
            # /proc/PID/net is namespace-wide, NOT per-process. Join by owned fd inode.
            for name in ("tcp", "tcp6"):
                table = directory / "net" / name
                try:
                    lines = table.read_text().splitlines()[1:]
                except FileNotFoundError:
                    if name == "tcp6" and (directory / "net/tcp").exists():
                        continue  # IPv6 may be disabled on this host
                    raise
                for line in lines:
                    fields = line.split()
                    if len(fields) < 10:
                        unknown = True
                    elif fields[3] == "01" and fields[9] in sockets:
                        return "connected"
        except OSError:
            unknown = True
    return "unknown" if unknown else "disconnected"


def sample(root, tck, ignore, proc=Path("/proc")):
    procs = {}
    incomplete = False
    try:
        paths = list(proc.iterdir())
    except OSError:
        return 0, 0, "unknown", "unknown"
    for directory in paths:
        if not directory.name.isdigit():
            continue
        try:
            fields = (directory / "stat").read_text().rsplit(")", 1)[1].split()
            command = (directory / "cmdline").read_bytes().replace(b"\0", b" ").decode("utf-8", "replace")
            procs[int(directory.name)] = (int(fields[1]), int(fields[11]) + int(fields[12]), command, fields[19])
        except (OSError, ValueError, IndexError):
            try:
                if directory.stat().st_uid == os.getuid():
                    incomplete = True  # an unreadable child might still own a socket
            except FileNotFoundError:
                pass  # exited during enumeration
            except OSError:
                incomplete = True
    if root not in procs:
        return 0, 0, "unknown", "unknown"
    kids = {}
    for pid, (parent, _, _, _) in procs.items():
        kids.setdefault(parent, []).append(pid)
    total, busy, stack, tree = 0, 0, [(root, 0)], set()
    while stack:
        pid, depth = stack.pop()
        if pid in tree:
            continue
        tree.add(pid)
        _, ticks, command, _ = procs[pid]
        if not (depth >= 2 and ignore.search(command)):
            total += ticks
            if depth >= 2:
                busy += 1
        stack += [(child, depth + 1) for child in kids.get(pid, [])]
    try:
        boot = (proc / "sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return total * 100 // tck, busy, "unknown", "unknown"
    identity = f"{boot}:{root}:{procs[root][3]}"
    tcp = tcp_state(tree, proc)
    if incomplete and tcp != "connected":
        tcp = "unknown"
    return total * 100 // tck, busy, tcp, identity


if __name__ == "__main__":
    print(*sample(int(sys.argv[1]), int(sys.argv[2]), re.compile(sys.argv[3])))
