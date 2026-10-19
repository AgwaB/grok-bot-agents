"""Owned POSIX helper groups for imports; never signal a PID recovered from disk."""
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def group_live(pgid):
    """Exclude zombies, which cannot write; unavailable ownership evidence fails closed."""
    if not isinstance(pgid, int) or pgid <= 1:
        raise ValueError('invalid helper ownership')
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    if sys.platform.startswith('linux'):
        for entry in Path('/proc').iterdir():
            if not entry.name.isdigit():
                continue
            try:
                fields = (entry / 'stat').read_text().rsplit(')', 1)[1].split()
            except (FileNotFoundError, ProcessLookupError):
                continue
            if int(fields[2]) == pgid and fields[0] not in ('Z', 'X'):
                return True
        return False
    # macOS/BSD: inspect OS process metadata, never an agent/runtime executable.
    ps = '/bin/ps' if Path('/bin/ps').is_file() else '/usr/bin/ps'
    result = subprocess.run([ps, '-axo', 'pgid=,stat='], capture_output=True, text=True, timeout=5, check=True)
    for line in result.stdout.splitlines():
        group, state = line.split(None, 1)
        if int(group) == pgid and not state.startswith(('Z', 'X')):
            return True
    return False


def save_owner(path, record, atomic):
    atomic(str(path), (json.dumps(record) + '\n').encode())
    with path.open('rb') as stream:
        os.fsync(stream.fileno())
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def pending_writer(root, guard):
    """Conservative cross-operation exclusion. Reused groups block, but are never killed."""
    for path in root.glob('import-*/helper.json'):
        guard(path)
        try:
            record = json.loads(path.read_text())
            if record.get('schema') != 1 or record.get('phase') != 'running':
                return 'helper_ownership_unknown'
            if group_live(record['pgid']):
                return 'previous_writer_active'
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError):
            return 'helper_ownership_unknown'
        path.unlink()  # Established quiescence, not permission to kill an old owner.
    return None


def drain(process, grace=2):
    """Only the just-spawned, session-owning Popen object may authorize signalling."""
    for sig in (signal.SIGTERM, signal.SIGKILL):
        if not group_live(process.pid):
            break
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        deadline = time.monotonic() + grace
        while time.monotonic() < deadline:
            process.poll()
            if not group_live(process.pid):
                break
            time.sleep(0.02)
    process.wait(timeout=grace)
    if group_live(process.pid):
        raise ValueError('helper group did not stop')


def run(command, env, operation, atomic, guard, timeout=1800):
    """Publish ownership before opening the gate; keep it until all writers stop.

    A lost launch publication is deliberately unresolved. The gate receives EOF on
    parent death, so it cannot mutate destinations before ownership is recorded.
    SIGKILL after release leaves a live group receipt which blocks every import.
    """
    if os.name != 'posix':
        raise ValueError('import apply requires POSIX process groups')
    owner = operation / 'helper.json'
    guard(owner)
    if owner.exists():
        raise ValueError('undrained helper ownership')
    process = None
    old_term = signal.getsignal(signal.SIGTERM)

    def interrupted(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, interrupted)
    try:
        save_owner(owner, {'schema': 1, 'phase': 'launching'}, atomic)
        # A separate Python gate avoids any unrecorded apply/npm/CLI writer.
        process = subprocess.Popen([sys.executable, '-B', str(Path(__file__).resolve()), '--gate', *command],
                                   env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, start_new_session=True)
        save_owner(owner, {'schema': 1, 'phase': 'running', 'pgid': process.pid}, atomic)
        stdout, stderr = process.communicate('GO\n', timeout=timeout)
        if group_live(process.pid):
            raise ValueError('helper left active descendants')
        return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
    finally:
        # A second handled signal must not interrupt draining and release the lock early.
        old_int = signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        try:
            if process is not None:
                drain(process)
                for stream in (process.stdin, process.stdout, process.stderr):
                    if stream is not None:
                        stream.close()
                owner.unlink(missing_ok=True)
            # If spawn/publication was interrupted, leave the launching record unresolved.
        finally:
            signal.signal(signal.SIGINT, old_int)
            signal.signal(signal.SIGTERM, old_term)


if __name__ == '__main__':
    if len(sys.argv) > 2 and sys.argv[1] == '--gate' and sys.stdin.readline() == 'GO\n':
        os.execvpe(sys.argv[2], sys.argv[2:], os.environ)
    sys.exit(2)
