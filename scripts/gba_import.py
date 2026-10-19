"""Consent-bound import bookkeeping; transfers and approvals remain bot-only."""
import contextlib
import fnmatch
import hashlib
import importlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import uuid
from typing import Any

AGENTS = ('claude', 'codex', 'pi', 'omp')
ID = re.compile(r'import-[a-f0-9]{32}')
ENV = re.compile(r'[A-Za-z_][A-Za-z0-9_]{0,127}')
FORBIDDEN = {'.ssh', '.gnupg', '.aws', '.azure', '.kube', '.config', '.mozilla',
             'Library', 'AppData', '.netrc', '.git-credentials', '.agent-keys',
             '.git', '__pycache__', 'sessions', 'logs', 'cache', 'oauth.json'}


def result(status, reason, action=None, **data):
    return dict(status=status, reason=reason, next_action=action,
                requires_approval=action in ('request_consent', 'copy_to_box', 'approve_overwrite',
                                            'approve_runtime_code', 'review_interrupted_import'), **data)


def configure(subparsers):
    p = subparsers.add_parser('import', help=__doc__)
    sub = p.add_subparsers(dest='import_command', required=True)
    inventory = sub.add_parser('inventory', help='Run on the caller; emit allowlisted file names and SHA256, not contents')
    inventory.add_argument('--agents', required=True)
    inventory.add_argument('--exported-keys', action='store_true')
    plan = sub.add_parser('plan')
    plan.add_argument('--inventory', required=True)
    plan.add_argument('--selection', help='JSON array of whole-file row ids approved by the user')
    for name in ('advance', 'status', 'verify'):
        q = sub.add_parser(name)
        q.add_argument('id')
        if name == 'advance':
            q.add_argument('--overwrite', action='store_true')
            q.add_argument('--approve-runtime', action='store_true')
            q.add_argument('--retry-interrupted', action='store_true')
        q.add_argument('--json', action='store_true')
    plan.add_argument('--json', action='store_true')


def helpers(root):
    sys.path.insert(0, str(root / 'skills/import-agents/scripts'))
    plan = importlib.import_module('import_plan')
    safety = importlib.import_module('import_safety')
    return plan.PLAN, safety.atomic_write, safety.guard_path


def read_json(path, guard):
    guard(path)
    p = Path(path)
    if not p.is_file() or p.stat().st_size > 4 * 1024 * 1024:
        raise ValueError('invalid metadata')
    try:
        return json.loads(p.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('unreadable metadata') from None


def safe_text(value):
    if not isinstance(value, str) or not value or len(value) > 4096 or any(ord(c) < 32 for c in value):
        raise ValueError('invalid name')
    return value


def allowed(agent, rel, plan):
    parts = PurePosixPath(rel).parts
    if (not parts or rel.startswith('/') or '\\' in rel or ':' in rel or
            any(p in ('', '.', '..') or p in FORBIDDEN or p.startswith('._') for p in rel.split('/')) or
            rel.endswith(('/', '.pyc')) or '.DS_Store' in parts):
        return False
    if agent == 'keys':
        return rel in ('secrets.env', 'rewrites.txt')
    if agent not in AGENTS:
        return False
    if agent == 'omp' and rel in ('agent.db-wal', 'agent.db-shm'):
        return True
    for pattern, _kind, _note in plan[agent]['files']:
        if pattern == '_home/':
            continue  # external package roots need the legacy reviewed manifest workflow
        pattern = pattern.replace('../', '_home/')
        if pattern.endswith('/'):
            if rel.startswith(pattern) and 'node_modules' not in parts[len(PurePosixPath(pattern).parts):]:
                return True
        elif fnmatch.fnmatchcase(rel, pattern) and ('/' in pattern or '/' not in rel):
            return True
    return False


def normalize(inventory, plan):
    """Input contains concrete source file names + SHA256, never file bodies."""
    if not isinstance(inventory, dict) or inventory.get('schema') != 1:
        raise ValueError('inventory schema')
    source_home = safe_text(inventory['home'])
    os_name = inventory['os']
    if os_name not in ('macos', 'linux', 'windows'):
        raise ValueError('source OS')
    rows = []
    for f in inventory['files']:
        if set(f) - {'agent', 'rel', 'sha256', 'source', 'names'}:
            raise ValueError('file-level consent required')
        agent, rel = f['agent'], safe_text(f['rel'])
        if not allowed(agent, rel, plan):
            raise ValueError('not allowlisted')
        digest = f['sha256']
        if not isinstance(digest, str) or not re.fullmatch('[a-f0-9]{64}', digest):
            raise ValueError('source digest required')
        base = '.agent-import-secrets' if agent == 'keys' else plan[agent]['home']
        source_rel = '.claude.json' if (agent, rel) == ('claude', '_home/.claude.json') else base + '/' + rel
        default_source = source_home.rstrip('/\\') + '/' + source_rel
        source = safe_text(f.get('source', default_source))
        # Directory overrides and exported OS-store logins are allowed, never forbidden credential roots.
        source_parts = source.replace('\\', '/').split('/')
        if any(part.lower() in {p.lower() for p in FORBIDDEN} or part in ('.', '..') for part in source_parts):
            raise ValueError('forbidden source')
        suffix = '.claude.json' if (agent, rel) == ('claude', '_home/.claude.json') else rel
        if not re.match(r'(?:/|[A-Za-z]:[/\\\\])', source) or not source.replace('\\', '/').endswith('/' + suffix):
            raise ValueError('source does not match allowlisted file')
        names = f.get('names', [])
        if not isinstance(names, list) or any(not isinstance(n, str) or not ENV.fullmatch(n) for n in names):
            raise ValueError('invalid names')
        if agent == 'keys' and rel == 'secrets.env' and not names:
            raise ValueError('exported names required')
        rows.append({'id': agent + ':' + rel, 'agent': agent, 'rel': rel, 'source': source,
                     'sha256': digest, 'names': sorted(set(names))})
    if not rows or len(rows) > 10000 or len({r['id'] for r in rows}) != len(rows):
        raise ValueError('invalid files')
    return source_home, os_name, rows


def state_root():
    return Path.home() / '.grok-bot-agents/imports'


def load_operation(ident, guard, plan):
    if not ID.fullmatch(ident):
        raise ValueError('invalid id')
    operation = state_root() / ident
    state = read_json(operation / 'state.json', guard)
    if (state['operation_id'] != ident or state.get('schema') != 1 or
            state.get('phase') not in ('awaiting_transfer', 'applying', 'applied', 'verified')):
        raise ValueError('invalid state')
    _, _, normalized = normalize({'schema': 1, 'home': state['source_home'], 'os': state['source_os'],
                                  'files': [{k: v for k, v in f.items() if k != 'id'} for f in state['files']]}, plan)
    applied = state.get('applied')
    selected = {f['agent'] for f in normalized}
    if (state['files'] != normalized or not isinstance(applied, list) or
            any(not isinstance(a, str) for a in applied) or len(set(applied)) != len(applied) or
            not set(applied) <= selected or
            (state['phase'] in ('applied', 'verified') and set(applied) != selected)):
        raise ValueError('invalid receipts')
    return operation, state


def save(operation, state, atomic):
    atomic(str(operation / 'state.json'), (json.dumps(state, separators=(',', ':')) + '\n').encode())


def public(state, operation, reason=None):
    phase = state['phase']
    data: dict[str, Any] = {'operation_id': state['operation_id'], 'phase': phase,
                           'selected_count': len(state['files']), 'applied_agents': state.get('applied', []),
                           'stage': str(operation / 'stage')}
    if phase == 'awaiting_transfer':
        data['transfers'] = [{'artifact_id': f['id'], 'source': f['source'],
                             'destination': str(operation / 'stage' / ('_secrets' if f['agent'] == 'keys' else f['agent']) / f['rel'])}
                            for f in state['files']]
        return result('blocked', reason or 'transfer_required', 'copy_to_box', **data)
    if phase == 'applying':
        return result('blocked', 'interrupted_apply', 'review_interrupted_import', **data)
    if phase == 'applied':
        return result('ok', reason or 'applied_not_verified', 'verify_import', **data)
    return result('ok', reason or 'import_verified', 'delete_source_exports', **data)


def digest_file(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def check_stage(state, operation, guard):
    stage = operation / 'stage'
    expected = {('_secrets' if f['agent'] == 'keys' else f['agent']) + '/' + f['rel']: f for f in state['files']}
    found = set()
    for base, dirs, files in os.walk(stage, followlinks=False):
        for name in dirs + files:
            p = Path(base) / name
            guard(p)
            if p.is_dir():
                continue
            if not stat.S_ISREG(p.stat().st_mode) or p.stat().st_size > 200 * 1024 * 1024:
                raise ValueError('unsafe stage')
            rel = p.relative_to(stage).as_posix()
            if rel not in expected:
                return 'unselected_staged_file'
            found.add(rel)
            if digest_file(p) != expected[rel]['sha256']:
                return 'source_changed'
            if rel == '_secrets/secrets.env':
                staged_line = importlib.import_module('post_import').STAGED_LINE
                names = []
                for line in p.read_text(encoding='utf-8').splitlines():
                    if not line.strip():
                        continue
                    match = staged_line.fullmatch(line)
                    if not match:
                        return 'invalid_export'
                    names.append(match.group(1))
                if sorted(names) != expected[rel]['names']:
                    return 'unselected_export_name'
    if '_secrets/rewrites.txt' in found:
        names = set(expected.get('_secrets/secrets.env', {}).get('names', []))
        for line in (stage / '_secrets/rewrites.txt').read_text(encoding='utf-8').splitlines():
            fields = line.split()
            if (len(fields) != 4 or fields[0] not in AGENTS or
                    fields[0] + '/' + fields[1] not in expected or fields[3] not in names or
                    not re.fullmatch(r'[A-Za-z0-9_.@:+-]+', fields[2])):
                return 'unselected_rewrite'
    return None if found == set(expected) else 'transfer_incomplete'


@contextlib.contextmanager
def lock(root, guard):
    # Local Linux/macOS box locks release on interruption, unlike stale lock directories.
    import fcntl
    guard(root / '.apply.lock')
    with (root / '.apply.lock').open('a') as stream:
        os.chmod(stream.name, 0o600)
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


def call_apply(root, state, operation, agent, dry=False, overwrite=False):
    env = dict(os.environ, AGENT_IMPORT_STAGE=str(operation / 'stage'))
    command = ['bash', str(root / 'skills/import-agents/scripts/apply_import.sh'), agent]
    if agent != 'keys':
        command += ['--src-home', state['source_home'], '--keep-stage', '--no-verify']
    if dry:
        command += ['--dry-run']
    if overwrite:
        command += ['--overwrite']
    process = subprocess.run(command, env=env, capture_output=True, text=True, timeout=1800)
    # Treat all helper output as untrusted; only these fixed fields cross the boundary.
    try:
        data = json.loads(process.stdout)
    except ValueError:
        data = {}
    return process.returncode, data


def remove_stage(path):
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError:
        raise ValueError('stage cleanup failed') from None


def inventory_files(args, plan, guard):
    agents = args.agents.split(',')
    if not agents or any(a not in AGENTS for a in agents):
        raise ValueError('invalid agents')
    home = Path.home()
    rows = []
    skipped = 0
    for agent in sorted(set(agents)):
        override = {'codex': 'CODEX_HOME', 'claude': 'CLAUDE_CONFIG_DIR', 'pi': 'PI_CODING_AGENT_DIR'}.get(agent)
        base = Path(os.environ.get(override, '') or home / plan[agent]['home']) if override else home / plan[agent]['home']
        candidates = set()
        for pattern, _kind, _note in plan[agent]['files']:
            if pattern == '_home/':
                continue
            if pattern == '../.claude.json':
                candidates.add((home / '.claude.json', '_home/.claude.json'))
                continue
            for candidate in base.glob(pattern.rstrip('/')):
                if candidate.is_dir():
                    candidates.update((p, p.relative_to(base).as_posix()) for p in candidate.rglob('*') if not p.is_dir())
                else:
                    candidates.add((candidate, candidate.relative_to(base).as_posix()))
        if agent == 'omp':
            candidates.update((base / n, n) for n in ('agent.db-wal', 'agent.db-shm'))
        by_rel = {rel: source for source, rel in candidates}
        if args.exported_keys and agent in ('claude', 'codex'):
            export_dir = home / '.agent-import-secrets' / agent
            for rel, kind, _note in plan[agent]['files']:
                exported = export_dir / rel
                if kind == 'auth' and exported.is_file():
                    by_rel[rel] = exported
        for rel, source in sorted(by_rel.items()):
            if not source.exists() or not allowed(agent, rel, plan):
                continue
            try:
                guard(source)
                if not source.is_file() or source.stat().st_size > 200 * 1024 * 1024:
                    raise ValueError('unsafe source')
            except ValueError:
                skipped += 1
                continue
            rows.append({'agent': agent, 'rel': rel, 'source': str(source), 'sha256': digest_file(source)})
    if args.exported_keys:
        source = home / '.agent-import-secrets/secrets.env'
        guard(source)
        names = sorted(importlib.import_module('post_import').agent_keys_names(str(source)))
        if source.is_file():
            rows.append({'agent': 'keys', 'rel': 'secrets.env', 'source': str(source),
                         'sha256': digest_file(source), 'names': names})
        rewrites = source.with_name('rewrites.txt')
        guard(rewrites)
        if rewrites.is_file():
            rows.append({'agent': 'keys', 'rel': 'rewrites.txt', 'source': str(rewrites), 'sha256': digest_file(rewrites)})
    os_name = 'windows' if os.name == 'nt' else 'macos' if sys.platform == 'darwin' else 'linux'
    inventory = {'schema': 1, 'home': str(home), 'os': os_name, 'files': rows}
    # The envelope itself is accepted by plan; no bot JSON reshaping is needed.
    return result('ok', 'inventory_ready', 'request_consent', **inventory, skipped_unsafe=skipped)


def handle(args, root):
    plan, atomic, guard = helpers(root)
    if args.import_command == 'inventory':
        return inventory_files(args, plan, guard)
    if args.import_command == 'plan':
        inventory = read_json(args.inventory, guard)
        source_home, source_os, rows = normalize(inventory, plan)
        if not args.selection:
            return result('blocked', 'consent_required', 'request_consent',
                          rows=[{'id': f['id'], 'source': f['source'], 'scope': 'whole_file', 'names': f['names']} for f in rows])
        selected = read_json(args.selection, guard)
        if not isinstance(selected, list) or not selected or any(not isinstance(s, str) for s in selected):
            return result('error', 'whole_file_selection_required')
        if len(set(selected)) != len(selected) or not set(selected) <= {f['id'] for f in rows}:
            return result('error', 'whole_file_selection_required')
        files = [f for f in rows if f['id'] in selected]
        db = {f['id'] for f in rows if f['agent'] == 'omp' and f['rel'].startswith('agent.db')}
        if db & set(selected) and ('omp:agent.db' not in db or not db <= set(selected)):
            return result('error', 'whole_database_selection_required')
        ident = 'import-' + uuid.uuid4().hex
        operation = state_root() / ident
        guard(operation)
        (operation / 'stage').mkdir(parents=True, mode=0o700)
        for f in files:
            folder = '_secrets' if f['agent'] == 'keys' else f['agent']
            (operation / 'stage' / folder / f['rel']).parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        for p in (operation, state_root(), state_root().parent):
            p.chmod(0o700)
        state = {'schema': 1, 'operation_id': ident, 'source_home': source_home, 'source_os': source_os,
                 'phase': 'awaiting_transfer', 'files': files, 'applied': []}
        save(operation, state, atomic)
        return public(state, operation)
    operation, state = load_operation(args.id, guard, plan)
    if args.import_command == 'status':
        return public(state, operation)
    with lock(state_root(), guard):
        # Re-read under the lock to avoid stale success/replay after concurrent calls.
        operation, state = load_operation(args.id, guard, plan)
        if args.import_command == 'verify':
            if state['phase'] not in ('applied', 'verified'):
                return public(state, operation)
            agents = sorted({f['agent'] for f in state['files']} - {'keys'})
            if agents:
                p = subprocess.run([sys.executable, str(root / 'skills/import-agents/scripts/parity_check.py'),
                                    '--cli', ','.join(agents), '--no-run'], capture_output=True, text=True, timeout=120)
                if p.returncode:
                    return result('blocked', 'parity_gaps', 'review_parity', operation_id=args.id)
            state['phase'] = 'verified'
            save(operation, state, atomic)
            # A successful offline verification removes only this operation's stage.
            guard(operation / 'stage')
            remove_stage(operation / 'stage')
            return public(state, operation)
        if state['phase'] in ('applied', 'verified'):
            return public(state, operation)
        if state['phase'] == 'applying' and not args.retry_interrupted:
            return public(state, operation)
        problem = check_stage(state, operation, guard)
        if problem:
            return result('blocked', problem, 'copy_to_box' if problem == 'transfer_incomplete' else 'review_transfer',
                          operation_id=args.id)
        agents = sorted({f['agent'] for f in state['files']})
        path = os.pathsep.join([str(Path.home() / '.local/bin'), str(Path.home() / '.bun/bin'), os.environ.get('PATH', '')])
        missing = [a for a in agents if a != 'keys' and not shutil.which(a, path=path)]
        if missing:
            return result('blocked', 'agents_missing', 'setup_agents', agents=missing, operation_id=args.id)
        if not args.approve_runtime and any(a != 'keys' for a in agents):
            return result('blocked', 'runtime_approval_required', 'approve_runtime_code', operation_id=args.id)
        # Preflight every destination before the first mutation, including pending keys.
        for agent in agents:
            rc, data = call_apply(root, state, operation, agent, dry=True, overwrite=args.overwrite)
            if rc == 4:
                return result('blocked', 'conflicts', 'approve_overwrite', operation_id=args.id, agent=agent)
            if rc or not data.get('ok'):
                return result('blocked', 'preflight_failed', 'review_import', operation_id=args.id, agent=agent)
        state['phase'] = 'applying'
        save(operation, state, atomic)
        for agent in agents:
            if agent in state['applied']:
                continue
            # Keep exports for resumability: apply_import consumes them. Give each
            # helper an owned working copy; the approved receipt stays immutable.
            scratch = operation / 'working'
            guard(scratch)
            if scratch.exists():
                remove_stage(scratch)
            shutil.copytree(operation / 'stage', scratch / 'stage')
            try:
                rc, data = call_apply(root, state, scratch, agent, overwrite=args.overwrite)
            finally:
                remove_stage(scratch)
            if rc or not data.get('ok'):
                return result('blocked', 'box_login_kept' if rc == 5 else 'apply_incomplete',
                              'review_interrupted_import', operation_id=args.id, agent=agent)
            state['applied'].append(agent)
            save(operation, state, atomic)
        state['phase'] = 'applied'
        save(operation, state, atomic)
        return public(state, operation)
