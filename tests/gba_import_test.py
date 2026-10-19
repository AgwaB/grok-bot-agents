#!/usr/bin/env python3
"""Offline controller tests: synthetic files, isolated HOME and mocked helpers."""
import argparse
import hashlib
import importlib.util
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('gba_import', ROOT / 'scripts/gba_import.py')
assert SPEC and SPEC.loader
IMPORT = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(IMPORT)


class ImportController(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.content = b'{"provider-a":{"key":"sk-secret-canary"},"provider-b":{"key":"sk-unticked-canary"}}'
        self.files = [{'agent': 'pi', 'rel': 'auth.json', 'sha256': hashlib.sha256(self.content).hexdigest()}]
        self.inventory = self.home / 'inventory.json'
        self.selection = self.home / 'selection.json'
        self.write_inventory()
        self.selection.write_text(json.dumps(['pi:auth.json']))
        binary = self.home / '.local/bin/pi'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o700)

    def write_inventory(self):
        self.inventory.write_text(json.dumps({'schema': 1, 'home': '/Users/alice', 'os': 'macos', 'files': self.files}))

    def invoke(self, *argv):
        parser = argparse.ArgumentParser()
        sub = parser.add_subparsers(dest='command')
        IMPORT.configure(sub)
        response = IMPORT.handle(parser.parse_args(['import', *argv]), ROOT)
        self.assertIn(response['status'], ('ok', 'error', 'blocked'))
        self.assertIn('reason', response)
        self.assertIn('next_action', response)
        self.assertIn('requires_approval', response)
        self.assertNotIn('sk-secret-canary', json.dumps(response))
        self.assertNotIn('sk-unticked-canary', json.dumps(response))
        return response

    def plan(self):
        return self.invoke('plan', '--inventory', str(self.inventory), '--selection', str(self.selection))

    def stage(self, response):
        target = Path(response['transfers'][0]['destination'])
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.content)
        return target

    def test_consent_plan_is_write_free(self):
        before = sorted(str(p) for p in self.home.rglob('*'))
        r = self.invoke('plan', '--inventory', str(self.inventory))
        self.assertEqual(r['next_action'], 'request_consent')
        self.assertEqual(r['rows'][0]['scope'], 'whole_file')
        self.assertEqual(before, sorted(str(p) for p in self.home.rglob('*')))

    def test_inventory_no_values_or_writes(self):
        source = self.home / '.pi/agent'
        source.mkdir(parents=True)
        (source / 'auth.json').write_bytes(self.content)
        (source / 'AGENTS.md').symlink_to(self.inventory)
        before = sorted(str(p) for p in self.home.rglob('*'))
        response = self.invoke('inventory', '--agents', 'pi')
        self.assertEqual([f['rel'] for f in response['files']], ['auth.json'])
        self.assertEqual(response['skipped_unsafe'], 1)
        self.assertEqual(response['files'][0]['sha256'], hashlib.sha256(self.content).hexdigest())
        self.assertEqual(before, sorted(str(p) for p in self.home.rglob('*')))
        self.inventory.write_text(json.dumps(response))
        self.assertEqual(self.plan()['phase'], 'awaiting_transfer')

    def test_claude_global_inventory_honors_config_dir_without_default_fallback(self):
        default = self.home / '.claude.json'
        override = self.home / 'claude profile/.claude.json'
        override.parent.mkdir()
        default.write_text('{"mcpServers":{},"oauthAccount":{"id":"sk-unticked-canary"}}')
        override.write_text('{"mcpServers":{"profile":{}},"oauthAccount":{"id":"sk-secret-canary"}}')
        for config_dir, expected in ((None, default), ('', default), (str(override.parent), override)):
            with self.subTest(config_dir=config_dir):
                if config_dir is None:
                    os.environ.pop('CLAUDE_CONFIG_DIR', None)
                else:
                    os.environ['CLAUDE_CONFIG_DIR'] = config_dir
                before = {str(p): (p.stat().st_mtime_ns, p.stat().st_mode) for p in self.home.rglob('*')}
                response = self.invoke('inventory', '--agents', 'claude')
                self.assertEqual(len(response['files']), 1)
                row = response['files'][0]
                self.assertEqual((row['rel'], row['source']), ('_home/.claude.json', str(expected)))
                self.assertEqual(row['sha256'], IMPORT.digest_file(expected))
                self.assertEqual(before, {str(p): (p.stat().st_mtime_ns, p.stat().st_mode) for p in self.home.rglob('*')})
                self.inventory.write_text(json.dumps(response))
                self.selection.write_text(json.dumps(['claude:_home/.claude.json']))
                operation = self.plan()
                self.assertEqual(operation['transfers'][0]['source'], str(expected))
        override.unlink()
        self.assertEqual(self.invoke('inventory', '--agents', 'claude')['files'], [])
        self.assertTrue(default.is_file())

    def test_source_must_match_allowlisted_path(self):
        for source in ('/etc/passwd', '/Users/alice/.ssh/auth.json', '/Users/alice/.pi/../auth.json'):
            self.files[0]['source'] = source
            self.write_inventory()
            with self.assertRaises(ValueError):
                self.plan()

    def test_unique_ids_private_state_copy_requires_approval(self):
        a, b = self.plan(), self.plan()
        self.assertNotEqual(a['operation_id'], b['operation_id'])
        self.assertNotEqual(a['stage'], b['stage'])
        self.assertTrue(a['requires_approval'])
        self.assertTrue(all(Path(t['destination']).parent.is_dir() for t in a['transfers']))
        state = Path(a['stage']).parent / 'state.json'
        self.assertEqual(state.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.invoke('status', a['operation_id'])['phase'], 'awaiting_transfer')

    def test_corrupt_phase_or_receipts_do_not_report_success(self):
        a = self.plan()
        path = Path(a['stage']).parent / 'state.json'
        original = json.loads(path.read_text())
        for update in ({'phase': 'unknown'}, {'phase': 'verified'}, {'applied': ['unselected']}):
            path.write_text(json.dumps(dict(original, **update)))
            with self.assertRaises(ValueError):
                self.invoke('status', a['operation_id'])
        path.write_text(json.dumps(original))
        self.assertEqual(self.invoke('status', a['operation_id'])['phase'], 'awaiting_transfer')

    def test_inventory_prefers_explicit_store_export(self):
        exported = self.home / '.agent-import-secrets/codex/auth.json'
        exported.parent.mkdir(parents=True)
        exported.write_bytes(self.content)
        result = self.invoke('inventory', '--agents', 'codex', '--exported-keys')
        self.assertEqual(result['files'][0]['source'], str(exported))
        self.assertEqual(result['files'][0]['rel'], 'auth.json')
        self.inventory.write_text(json.dumps(result))
        self.selection.write_text(json.dumps(['codex:auth.json']))
        self.assertEqual(self.plan()['phase'], 'awaiting_transfer')

    def test_partial_secret_selection_rejected(self):
        self.selection.write_text(json.dumps(['pi:auth.json:provider-a']))
        self.assertEqual(self.plan()['reason'], 'whole_file_selection_required')
        self.assertFalse((self.home / '.grok-bot-agents').exists())

    def test_database_is_one_consent_unit(self):
        self.files = [{'agent': 'omp', 'rel': rel, 'sha256': '0' * 64} for rel in ('agent.db', 'agent.db-wal')]
        self.write_inventory()
        self.selection.write_text(json.dumps(['omp:agent.db']))
        self.assertEqual(self.plan()['reason'], 'whole_database_selection_required')

    def test_sidecars_without_database_are_rejected(self):
        for rels in (('agent.db-wal',), ('agent.db-shm',), ('agent.db-wal', 'agent.db-shm')):
            with self.subTest(rels=rels):
                self.files = [{'agent': 'omp', 'rel': rel, 'sha256': '0' * 64} for rel in rels]
                self.write_inventory()
                self.selection.write_text(json.dumps(['omp:' + rel for rel in rels]))
                self.assertEqual(self.plan()['reason'], 'whole_database_selection_required')
        self.assertFalse((self.home / '.grok-bot-agents').exists())

    def test_legacy_apply_refuses_orphan_sidecar(self):
        stage = self.home / 'stage/omp'
        stage.mkdir(parents=True)
        (stage / 'agent.db-wal').write_bytes(b'foreign sidecar')
        db = self.home / '.omp/agent/agent.db'
        db.parent.mkdir(parents=True)
        db.write_bytes(b'original database')
        p = subprocess.run(['bash', str(ROOT / 'skills/import-agents/scripts/apply_import.sh'),
                            'omp', '--overwrite', '--no-verify'],
                           env=dict(os.environ, AGENT_IMPORT_STAGE=str(stage.parent)), capture_output=True)
        self.assertEqual(p.returncode, 2)
        self.assertEqual(db.read_bytes(), b'original database')
        self.assertFalse(db.with_name('agent.db-wal').exists())

    def test_rejects_forbidden_paths(self):
        for rel in ('../.ssh/id_rsa', 'skills/.aws/key', 'auth.json/provider-a', 'sessions/log.json'):
            with self.subTest(rel=rel):
                self.files[0]['rel'] = rel
                self.write_inventory()
                with self.assertRaises(ValueError):
                    self.plan()

    def test_receipts_missing_extra_changed_symlink(self):
        a = self.plan()
        ident = a['operation_id']
        self.assertEqual(self.invoke('advance', ident)['reason'], 'transfer_incomplete')
        target = self.stage(a)
        target.write_bytes(b'changed')
        self.assertEqual(self.invoke('advance', ident)['reason'], 'source_changed')
        target.write_bytes(self.content)
        extra = target.parent / 'unselected.json'
        extra.write_text('{}')
        self.assertEqual(self.invoke('advance', ident)['reason'], 'unselected_staged_file')
        extra.unlink()
        target.unlink()
        target.symlink_to(self.inventory)
        with self.assertRaises(ValueError):
            self.invoke('advance', ident)

    def test_preflight_all_before_writes_and_runtime_approval(self):
        self.files.append({'agent': 'codex', 'rel': 'config.toml', 'sha256': hashlib.sha256(b'').hexdigest()})
        self.write_inventory()
        self.selection.write_text(json.dumps(['pi:auth.json', 'codex:config.toml']))
        a = self.plan()
        self.stage(a)
        second = Path(a['transfers'][1]['destination'])
        self.assertTrue(second.parent.is_dir())
        second.write_bytes(b'')
        (self.home / '.local/bin/codex').write_text('#!/bin/sh\nexit 0\n')
        (self.home / '.local/bin/codex').chmod(0o700)
        self.assertEqual(self.invoke('advance', a['operation_id'])['reason'], 'runtime_approval_required')
        calls = []
        real_apply = IMPORT.call_apply
        def apply(*args, **kwargs):
            calls.append((args[3], kwargs['dry']))
            return (4, {'ok': False}) if args[3] == 'pi' else real_apply(*args, **kwargs)
        with patch.object(IMPORT, 'call_apply', side_effect=apply):
            r = self.invoke('advance', a['operation_id'], '--approve-runtime')
        self.assertEqual(r['reason'], 'conflicts')
        self.assertTrue(all(dry for _, dry in calls))
        self.assertEqual(len(calls), 2)

    def test_interrupted_apply_explicit_retry_and_verify_cleanup(self):
        a = self.plan()
        self.stage(a)
        ident = a['operation_id']
        real_apply = IMPORT.call_apply
        def interrupt(*args, **kwargs):
            if kwargs.get('dry'):
                return real_apply(*args, **kwargs)
            raise TimeoutError()
        with patch.object(IMPORT, 'call_apply', side_effect=interrupt), self.assertRaises(TimeoutError):
            self.invoke('advance', ident, '--approve-runtime')
        self.assertEqual(self.invoke('status', ident)['reason'], 'interrupted_apply')
        self.assertEqual(self.invoke('advance', ident)['reason'], 'interrupted_apply')
        with patch.object(IMPORT, 'call_apply', wraps=real_apply) as apply:
            r = self.invoke('advance', ident, '--approve-runtime', '--retry-interrupted')
            self.assertEqual(apply.call_count, 2)
            self.assertEqual(r['phase'], 'applied')
            self.invoke('advance', ident)
            self.assertEqual(apply.call_count, 2)
        with patch.object(IMPORT.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'sk-secret-canary', 'sk-unticked-canary')):
            r = self.invoke('verify', ident)
        self.assertEqual(r['phase'], 'verified')
        self.assertFalse(Path(a['stage']).exists())
        self.assertEqual(self.invoke('status', ident)['phase'], 'verified')

    def test_real_apply_helper_keys_only_and_retry(self):
        self.content = b"export ALLOWED_KEY='sk-secret-canary'\n"
        self.files = [{'agent': 'keys', 'rel': 'secrets.env', 'sha256': hashlib.sha256(self.content).hexdigest(), 'names': ['ALLOWED_KEY']}]
        self.write_inventory()
        self.selection.write_text(json.dumps(['keys:secrets.env']))
        a = self.plan()
        self.stage(a)
        self.assertEqual(self.invoke('advance', a['operation_id'])['phase'], 'applied')
        keys = self.home / '.agent-keys'
        self.assertEqual(keys.read_bytes(), self.content)
        self.assertEqual(keys.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.home / '.bashrc').exists())
        self.invoke('advance', a['operation_id'])
        self.assertEqual(keys.read_bytes(), self.content)
        self.assertEqual(self.invoke('verify', a['operation_id'])['phase'], 'verified')

    def test_key_replacement_requires_approval_and_private_backup(self):
        self.content = b"export ALLOWED_KEY='sk-secret-canary'\n"
        self.files = [{'agent': 'keys', 'rel': 'secrets.env', 'sha256': hashlib.sha256(self.content).hexdigest(), 'names': ['ALLOWED_KEY']}]
        self.write_inventory()
        self.selection.write_text(json.dumps(['keys:secrets.env']))
        keys = self.home / '.agent-keys'
        previous = b"export ALLOWED_KEY='sk-unticked-canary'\nexport KEEP_ME='untouched'\n"
        keys.write_bytes(previous)
        a = self.plan()
        self.stage(a)
        self.assertEqual(self.invoke('advance', a['operation_id'])['reason'], 'conflicts')
        self.assertEqual(keys.read_bytes(), previous)
        self.assertEqual(list(self.home.glob('.agent-keys.bak.*')), [])
        self.assertEqual(self.invoke('advance', a['operation_id'], '--overwrite')['phase'], 'applied')
        backups = list(self.home.glob('.agent-keys.bak.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(backups[0].read_bytes(), previous)
        self.assertIn(b"export KEEP_ME='untouched'", keys.read_bytes())
        # An identical selected assignment needs neither replacement approval nor a second backup.
        b = self.plan()
        self.stage(b)
        self.assertEqual(self.invoke('advance', b['operation_id'])['phase'], 'applied')
        self.assertEqual(list(self.home.glob('.agent-keys.bak.*')), backups)
        report = self.invoke('status', a['operation_id'])['reports']['keys']
        self.assertEqual(report['backups'], [str(backups[0])])

    def test_later_duplicate_key_assignment_also_requires_overwrite(self):
        op = self.fixture_import('keys', 'secrets.env', b"export ALLOWED_KEY='sk-secret-canary'\n", ['ALLOWED_KEY'])
        keys = self.home / '.agent-keys'
        previous = self.content + b"export ALLOWED_KEY='sk-unticked-canary'\n"
        keys.write_bytes(previous)
        self.assertEqual(self.invoke('advance', op['operation_id'])['reason'], 'conflicts')
        self.assertEqual(keys.read_bytes(), previous)
        self.assertEqual(self.invoke('advance', op['operation_id'], '--overwrite')['phase'], 'applied')
        self.assertEqual(keys.read_bytes(), self.content)

    def test_database_sidecar_cleanup_is_preflighted_backed_up_and_verified(self):
        op = self.fixture_import('omp', 'agent.db', b'fixture database')
        stale = self.home / '.omp/agent/agent.db-wal'
        stale.parent.mkdir(parents=True)
        stale.write_bytes(b'foreign WAL')
        self.assertEqual(self.invoke('advance', op['operation_id'], '--approve-runtime')['reason'], 'conflicts')
        self.assertTrue(stale.exists())
        result = self.invoke('advance', op['operation_id'], '--approve-runtime', '--overwrite')
        self.assertEqual(result['phase'], 'applied')
        self.assertFalse(stale.exists())
        backups = result['reports']['omp']['backups']
        self.assertEqual(len(backups), 1)
        self.assertEqual(Path(backups[0]).read_bytes(), b'foreign WAL')
        self.assertEqual(Path(backups[0]).stat().st_mode & 0o777, 0o600)
        stale.write_bytes(b'foreign WAL')
        self.assertEqual(self.invoke('verify', op['operation_id'])['reason'], 'artifact_verification_failed')
        self.assertTrue(Path(op['stage']).exists())
        stale.unlink()
        with patch.object(IMPORT.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.assertEqual(self.invoke('verify', op['operation_id'])['phase'], 'verified')

    def test_identical_database_reconciles_stale_sidecars(self):
        op = self.fixture_import('omp', 'agent.db', b'fixture database')
        db = self.home / '.omp/agent/agent.db'
        db.parent.mkdir(parents=True)
        db.write_bytes(self.content)
        db.chmod(0o600)
        sidecars = [db.with_name(name) for name in ('agent.db-wal', 'agent.db-shm')]
        for sidecar in sidecars:
            sidecar.write_bytes(b'foreign sidecar')
        ident = op['operation_id']
        self.assertEqual(self.invoke('advance', ident, '--approve-runtime')['reason'], 'conflicts')
        self.assertTrue(all(path.exists() for path in sidecars))
        self.assertEqual(list(db.parent.glob('*.bak.*')), [])
        result = self.invoke('advance', ident, '--approve-runtime', '--overwrite')
        self.assertEqual(result['phase'], 'applied')
        self.assertTrue(all(not path.exists() for path in sidecars))
        self.assertEqual(len(result['reports']['omp']['backups']), 2)
        self.assertTrue(all(Path(p).stat().st_mode & 0o777 == 0o600 for p in result['reports']['omp']['backups']))
        self.assertEqual(IMPORT.digest_file(db), hashlib.sha256(self.content).hexdigest())

    def test_database_absence_receipts_cover_sidecars_missing_at_preflight(self):
        op = self.fixture_import('omp', 'agent.db', b'fixture database')
        ident = op['operation_id']
        self.assertEqual(self.invoke('advance', ident, '--approve-runtime')['phase'], 'applied')
        operation = Path(op['stage']).parent
        state = json.loads((operation / 'state.json').read_text())
        db = self.home / '.omp/agent/agent.db'
        sidecars = [db.with_name(name) for name in ('agent.db-wal', 'agent.db-shm')]
        self.assertEqual(set(state['receipts'][0]['absent_sidecars']), set(map(str, sidecars)))
        for path in sidecars:
            path.write_bytes(b'foreign sidecar')
            self.assertEqual(self.invoke('verify', ident)['reason'], 'artifact_verification_failed')
            self.assertTrue(Path(op['stage']).exists())
            path.unlink()
        with patch.object(IMPORT.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.assertEqual(self.invoke('verify', ident)['phase'], 'verified')

    def test_database_retry_after_replace_before_sidecar_removal(self):
        op = self.fixture_import('omp', 'agent.db', b'new fixture database')
        ident = op['operation_id']
        db = self.home / '.omp/agent/agent.db'
        db.parent.mkdir(parents=True)
        db.write_bytes(b'old fixture database')
        wal = db.with_name('agent.db-wal')
        wal.write_bytes(b'foreign WAL')
        # Fault injection in the real subprocess helper, immediately AFTER atomic
        # DB replacement and BEFORE sidecar cleanup. No production test hook.
        python = self.home / '.local/bin/python3'
        python.write_text('#!' + sys.executable + '''
import os, signal, sys
sys.argv = sys.argv[1:]
sys.path.insert(0, sys.argv[1])
import import_safety
original = import_safety.atomic_write
def interrupt(path, *args, **kwargs):
    original(path, *args, **kwargs)
    if os.path.basename(path) == 'agent.db':
        os.kill(os.getpid(), signal.SIGKILL)
import_safety.atomic_write = interrupt
exec(compile(sys.stdin.read(), '<apply_import>', 'exec'))
''')
        python.chmod(0o700)
        try:
            result = self.invoke('advance', ident, '--approve-runtime', '--overwrite')
        finally:
            python.unlink()
        self.assertEqual(result['reason'], 'apply_incomplete')
        self.assertEqual(IMPORT.digest_file(db), hashlib.sha256(self.content).hexdigest())
        self.assertTrue(wal.exists())
        state_path = Path(op['stage']).parent / 'state.json'
        expected = json.loads(state_path.read_text())['receipts'][0]['absent_sidecars']
        self.assertEqual(self.invoke('advance', ident)['reason'], 'interrupted_apply')
        self.assertEqual(self.invoke('advance', ident, '--approve-runtime', '--retry-interrupted')['reason'], 'conflicts')
        self.assertTrue(wal.exists())
        # Also lose the completion receipt after a successful cleanup. The next
        # preflight sees no deletions, but must retain every absence expectation.
        real_apply = IMPORT.call_apply
        def lose_completion(*args, **kwargs):
            rc, data = real_apply(*args, **kwargs)
            return (99, {}) if not kwargs.get('dry') and rc == 0 else (rc, data)
        with patch.object(IMPORT, 'call_apply', side_effect=lose_completion):
            result = self.invoke('advance', ident, '--approve-runtime', '--retry-interrupted', '--overwrite')
        self.assertEqual(result['reason'], 'apply_incomplete')
        self.assertFalse(wal.exists())
        backups = list(wal.parent.glob('agent.db-wal.bak.*'))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.invoke('advance', ident, '--approve-runtime', '--retry-interrupted')['phase'], 'applied')
        self.assertEqual(json.loads(state_path.read_text())['receipts'][0]['absent_sidecars'], expected)
        for name in ('agent.db-wal', 'agent.db-shm'):
            path = db.with_name(name)
            path.write_bytes(b'foreign sidecar')
            self.assertEqual(self.invoke('verify', ident)['reason'], 'artifact_verification_failed')
            self.assertTrue(Path(op['stage']).exists())
            path.unlink()
        with patch.object(IMPORT.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            self.assertEqual(self.invoke('verify', ident)['phase'], 'verified')

    def test_real_settings_only_apply_preserves_defaults(self):
        self.content = b'fixture instructions\n'
        self.files = [{'agent': 'codex', 'rel': 'AGENTS.md', 'sha256': hashlib.sha256(self.content).hexdigest()}]
        self.write_inventory()
        self.selection.write_text(json.dumps(['codex:AGENTS.md']))
        cli = self.home / '.local/bin/codex'
        cli.write_text('#!/bin/sh\nexit 0\n')
        cli.chmod(0o700)
        native = self.home / '.codex/config.toml'
        native.parent.mkdir()
        unselected = b'notify = ["/missing/notify"]\n[mcp_servers.example]\ncommand = "/missing/mcp"\n'
        native.write_bytes(unselected)
        config = self.home / '.grok-bot-agents/config.json'
        config.parent.mkdir()
        config.write_text('{"fixture":"existing overrides stay untouched"}')
        before = config.read_bytes()
        a = self.plan()
        self.stage(a)
        self.assertEqual(self.invoke('advance', a['operation_id'], '--approve-runtime')['phase'], 'applied')
        self.assertEqual((self.home / '.codex/AGENTS.md').read_bytes(), self.content)
        self.assertEqual(config.read_bytes(), before)
        self.assertEqual(native.read_bytes(), unselected)
        self.assertIn('runtime_repairs_deferred', self.invoke('status', a['operation_id'])['reports']['codex']['warnings'])

    def fixture_import(self, agent, rel, content, names=None):
        self.content = content
        self.files = [{'agent': agent, 'rel': rel, 'sha256': hashlib.sha256(content).hexdigest()}]
        if names is not None:
            self.files[0]['names'] = names
        self.write_inventory()
        self.selection.write_text(json.dumps([agent + ':' + rel]))
        if agent != 'keys':
            cli = self.home / '.local/bin' / agent
            cli.write_text('#!/bin/sh\nexit 0\n')
            cli.chmod(0o700)
        operation = self.plan()
        self.stage(operation)
        return operation

    def test_partial_import_never_runs_unselected_runtime_repairs(self):
        for agent, base in (('pi', '.pi/agent'), ('claude', '.claude')):
            with self.subTest(agent=agent):
                native = self.home / base / 'settings.json'
                native.parent.mkdir(parents=True)
                original = b'{"packages":["npm:unselected"],"enabledPlugins":{"private@market":true}}'
                native.write_bytes(original)
                op = self.fixture_import(agent, 'AGENTS.md' if agent == 'pi' else 'CLAUDE.md', b'Only instructions')
                cli = self.home / '.local/bin' / agent
                cli.write_text('#!/bin/sh\ntouch "$HOME/should-not-run"\nexit 0\n')
                result = self.invoke('advance', op['operation_id'], '--approve-runtime')
                self.assertEqual(result['phase'], 'applied')
                self.assertFalse((self.home / 'should-not-run').exists())
                self.assertEqual(native.read_bytes(), original)
                self.assertIn('runtime_repairs_deferred', result['reports'][agent]['warnings'])

    def test_identical_selected_files_become_private_preserving_executable_bits(self):
        os.environ['OPENAI_API_KEY'] = 'sk-secret-canary'
        for source_mode, old_mode, expected_mode in ((0o644, 0o644, 0o600), (0o755, 0o755, 0o700),
                                                      (0o755, 0o644, 0o700), (0o644, 0o700, 0o700)):
            with self.subTest(source_mode=oct(source_mode), old_mode=oct(old_mode)):
                op = self.fixture_import('codex', 'AGENTS.md', b'fixture instructions\n')
                staged = Path(op['transfers'][0]['destination'])
                staged.chmod(source_mode)
                target = self.home / '.codex/AGENTS.md'
                target.parent.mkdir(exist_ok=True)
                target.write_bytes(self.content)
                target.chmod(old_mode)
                before_mtime = target.stat().st_mtime_ns
                unselected = target.with_name('untouched.txt')
                unselected.write_bytes(b'unselected')
                unselected.chmod(0o644)
                plan, _, guard = IMPORT.helpers(ROOT)
                operation, state = IMPORT.load_operation(op['operation_id'], guard, plan)
                rc, _ = IMPORT.call_apply(ROOT, state, operation, 'codex', dry=True)
                self.assertEqual(rc, 0)
                self.assertEqual(target.stat().st_mode & 0o777, old_mode)
                self.assertEqual(self.invoke('advance', op['operation_id'], '--approve-runtime')['phase'], 'applied')
                self.assertEqual(target.stat().st_mode & 0o777, expected_mode)
                self.assertEqual(target.read_bytes(), self.content)
                self.assertEqual(target.stat().st_mtime_ns, before_mtime)
                self.assertEqual(unselected.stat().st_mode & 0o777, 0o644)
                self.assertEqual(unselected.read_bytes(), b'unselected')
                self.assertEqual(list(target.parent.glob('*.bak.*')), [])
                self.assertEqual(self.invoke('verify', op['operation_id'])['phase'], 'verified')

    def test_unchanged_claude_global_merge_becomes_private(self):
        os.environ['ANTHROPIC_API_KEY'] = 'sk-secret-canary'
        source = b'{"mcpServers":{}}'
        op = self.fixture_import('claude', '_home/.claude.json', source)
        target = self.home / '.claude.json'
        original = b'{ "keep": "untouched", "mcpServers": {} }\n'
        target.write_bytes(original)
        target.chmod(0o644)
        before_mtime = target.stat().st_mtime_ns
        plan, _, guard = IMPORT.helpers(ROOT)
        operation, state = IMPORT.load_operation(op['operation_id'], guard, plan)
        rc, _ = IMPORT.call_apply(ROOT, state, operation, 'claude', dry=True)
        self.assertEqual(rc, 0)
        self.assertEqual(target.stat().st_mode & 0o777, 0o644)
        self.assertEqual(self.invoke('advance', op['operation_id'], '--approve-runtime')['phase'], 'applied')
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)
        self.assertEqual(target.read_bytes(), original)
        self.assertEqual(target.stat().st_mtime_ns, before_mtime)
        self.assertEqual(list(self.home.glob('.claude.json.bak.*')), [])
        self.assertEqual(self.invoke('verify', op['operation_id'])['phase'], 'verified')

    def test_selected_config_transform_backups_and_sanitized_reports_persist(self):
        op = self.fixture_import('codex', 'config.toml', b'model = "fixture"\nnotify = ["/Applications/old"]\n')
        native = self.home / '.codex/config.toml'
        native.parent.mkdir()
        native.write_text('model = "old"\n')
        before = native.read_bytes()
        self.assertEqual(self.invoke('advance', op['operation_id'], '--approve-runtime')['reason'], 'conflicts')
        result = self.invoke('advance', op['operation_id'], '--approve-runtime', '--overwrite')
        report = result['reports']['codex']
        self.assertIn('source_paths_remain', report['warnings'])
        self.assertEqual(len(report['backups']), 1)
        self.assertEqual(Path(report['backups'][0]).read_bytes(), before)
        self.assertEqual(Path(report['backups'][0]).stat().st_mode & 0o777, 0o600)
        self.assertIn(b'cli_auth_credentials_store = "file"', native.read_bytes())
        self.assertEqual(self.invoke('status', op['operation_id'])['reports'], result['reports'])
        expected = native.read_bytes()
        native.write_bytes(self.content)  # Source bytes are not the expected transformed state.
        self.assertEqual(self.invoke('verify', op['operation_id'])['reason'], 'artifact_verification_failed')
        native.write_bytes(expected)
        with patch.object(IMPORT.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
            verified = self.invoke('verify', op['operation_id'])
        self.assertEqual(verified['phase'], 'verified')
        self.assertEqual(verified['reports'], result['reports'])
        self.assertEqual(IMPORT.sanitized_report({'warnings': ['sk-secret-canary'], 'backups': ['sk-unticked-canary']}, []),
                         {'warnings': ['helper_warning'], 'backups': []})

    def test_unverifiable_runtime_metadata_is_not_silently_skipped(self):
        op = self.fixture_import('claude', 'plugins/installed_plugins.json', b'{"plugins":{}}')
        result = self.invoke('advance', op['operation_id'], '--approve-runtime')
        self.assertEqual(result['reason'], 'runtime_selection_requires_legacy')
        self.assertTrue(Path(op['stage']).exists())
        self.assertFalse((self.home / '.claude').exists())

    def test_verify_requires_selected_keys_and_preserves_stage_on_mismatch(self):
        op = self.fixture_import('keys', 'secrets.env', b"export ALLOWED_KEY='sk-secret-canary'\n", ['ALLOWED_KEY'])
        self.invoke('advance', op['operation_id'])
        keys = self.home / '.agent-keys'
        keys.unlink()
        self.assertEqual(self.invoke('verify', op['operation_id'])['reason'], 'artifact_verification_failed')
        keys.write_text("export ALLOWED_KEY='sk-unticked-canary'\n")
        keys.chmod(0o600)
        self.assertEqual(self.invoke('verify', op['operation_id'])['reason'], 'artifact_verification_failed')
        keys.write_bytes(self.content + self.content)
        self.assertEqual(self.invoke('verify', op['operation_id'])['reason'], 'artifact_verification_failed')
        self.assertTrue(Path(op['stage']).exists())
        # Additional unselected keys are fine; selected assignments must be exact and unique.
        keys.write_bytes(self.content + b"export OTHER='unrelated'\n")
        self.assertEqual(self.invoke('verify', op['operation_id'])['phase'], 'verified')
        self.assertFalse(Path(op['stage']).exists())

    def test_verify_requires_each_selected_file_not_just_parity(self):
        for agent, rel, base, content in (('pi', 'auth.json', '.pi/agent', self.content),
                                          ('codex', 'AGENTS.md', '.codex', b'instructions')):
            with self.subTest(agent=agent):
                op = self.fixture_import(agent, rel, content)
                self.invoke('advance', op['operation_id'], '--approve-runtime')
                target = self.home / base / rel
                target.unlink()
                with patch.object(IMPORT.subprocess, 'run') as parity:
                    self.assertEqual(self.invoke('verify', op['operation_id'])['reason'], 'artifact_verification_failed')
                    parity.assert_not_called()
                target.write_bytes(b'wrong destination')
                target.chmod(0o600)
                self.assertEqual(self.invoke('verify', op['operation_id'])['reason'], 'artifact_verification_failed')
                self.assertTrue(Path(op['stage']).exists())
                target.write_bytes(content)
                with patch.object(IMPORT.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, '', '')):
                    self.assertEqual(self.invoke('verify', op['operation_id'])['phase'], 'verified')

    def test_verify_refuses_missing_receipts_and_skipped_helper_work(self):
        op = self.plan()
        self.stage(op)
        real_apply = IMPORT.call_apply
        def skipped(*args, **kwargs):
            return real_apply(*args, **kwargs) if kwargs.get('dry') else (0, {'ok': True})
        with patch.object(IMPORT, 'call_apply', side_effect=skipped):
            self.invoke('advance', op['operation_id'], '--approve-runtime')
        self.assertEqual(self.invoke('verify', op['operation_id'])['reason'], 'artifact_verification_failed')
        path = Path(op['stage']).parent / 'state.json'
        state = json.loads(path.read_text())
        state.pop('receipts')
        path.write_text(json.dumps(state))
        self.assertEqual(self.invoke('verify', op['operation_id'])['reason'], 'artifact_verification_failed')
        self.assertTrue(Path(op['stage']).exists())

    def process_helper(self):
        _, atomic, guard = IMPORT.helpers(ROOT)
        owned = importlib.import_module('gba_import_process')
        return owned, atomic, guard

    def writer_script(self):
        # A grandchild that ignores TERM demonstrates group-wide KILL and drain.
        child = ('import os, signal, time; from pathlib import Path; '
                 'signal.signal(signal.SIGTERM, signal.SIG_IGN); '
                 'p=Path(os.environ["HOME"])/"heartbeat"; '
                 'p.write_text("ready"); '\
                 '\nwhile True: p.write_text(str(time.monotonic())); time.sleep(0.01)')
        return ('import subprocess, sys, time; '
                'subprocess.Popen([sys.executable,"-c",' + repr(child) + ']); time.sleep(60)')

    def wait_file(self, path, process=None):
        deadline = time.monotonic() + 8
        while not path.exists() and time.monotonic() < deadline:
            if process and process.poll() is not None:
                self.fail('fixture controller stopped before publishing ownership')
            time.sleep(0.02)
        self.assertTrue(path.exists(), 'fixture writer did not start')

    def test_import_timeout_kills_and_drains_helper_process_group(self):
        owned, atomic, guard = self.process_helper()
        op = Path(self.plan()['stage']).parent
        with self.assertRaises(subprocess.TimeoutExpired):
            owned.run([sys.executable, '-c', self.writer_script()], dict(os.environ), op, atomic, guard, timeout=1)
        heartbeat = self.home / 'heartbeat'
        self.assertTrue(heartbeat.exists())
        before = heartbeat.read_bytes()
        time.sleep(0.08)
        self.assertEqual(heartbeat.read_bytes(), before)
        self.assertFalse((op / 'helper.json').exists())

    def launch_writer_controller(self, op):
        script = ('import sys; from pathlib import Path; '
                  f'sys.path[:0]=[{str(ROOT / "scripts")!r},{str(ROOT / "skills/import-agents/scripts")!r}]; '
                  'import os, gba_import, gba_import_process; from import_safety import atomic_write, guard_path; '
                  f'op=Path({str(op)!r}); '\
                  '\nwith gba_import.lock(op.parent, guard_path):'\
                  f'\n gba_import_process.run([sys.executable,"-c",{self.writer_script()!r}],dict(os.environ),op,atomic_write,guard_path)')
        return subprocess.Popen([sys.executable, '-B', '-c', script], env=dict(os.environ),
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def test_parent_sigkill_blocks_retry_and_preserves_working_files(self):
        owned, _, guard = self.process_helper()
        op_data = self.plan()
        self.stage(op_data)
        op = Path(op_data['stage']).parent
        state = json.loads((op / 'state.json').read_text())
        state['phase'] = 'applying'
        (op / 'state.json').write_text(json.dumps(state))
        working = op / 'working'
        working.mkdir()
        (working / 'sentinel').write_text('owned scratch')
        parent = self.launch_writer_controller(op)
        pgid = None
        try:
            self.wait_file(self.home / 'heartbeat', parent)
            pgid = json.loads((op / 'helper.json').read_text())['pgid']
            parent.kill()
            parent.wait(timeout=5)
            self.assertTrue(owned.group_live(pgid))
            for target in (op_data, self.plan()):
                result = self.invoke('advance', target['operation_id'], '--approve-runtime', '--retry-interrupted')
                self.assertEqual(result['reason'], 'previous_writer_active')
            self.assertTrue((working / 'sentinel').is_file())
            self.assertEqual(owned.pending_writer(op.parent, guard), 'previous_writer_active')
        finally:
            if parent.poll() is None:
                parent.kill()
            parent.wait(timeout=5)
            if pgid is None and (op / 'helper.json').exists():
                pgid = json.loads((op / 'helper.json').read_text()).get('pgid')
            if pgid and owned.group_live(pgid):
                os.killpg(pgid, signal.SIGKILL)  # Only this test's freshly created group.
                deadline = time.monotonic() + 5
                while owned.group_live(pgid) and time.monotonic() < deadline:
                    time.sleep(0.02)
        self.assertIsNone(owned.pending_writer(op.parent, guard))
        self.assertEqual(self.invoke('advance', op_data['operation_id'], '--approve-runtime', '--retry-interrupted')['phase'], 'applied')

    def test_parent_sigterm_drains_children_before_unlock(self):
        owned, _, guard = self.process_helper()
        op = Path(self.plan()['stage']).parent
        parent = self.launch_writer_controller(op)
        try:
            self.wait_file(self.home / 'heartbeat', parent)
            pgid = json.loads((op / 'helper.json').read_text())['pgid']
            parent.terminate()
            parent.wait(timeout=8)
            self.assertFalse(owned.group_live(pgid))
            self.assertIsNone(owned.pending_writer(op.parent, guard))
            with IMPORT.lock(op.parent, guard):
                self.assertFalse((op / 'helper.json').exists())
        finally:
            if parent.poll() is None:
                parent.terminate()
                parent.wait(timeout=8)

    def test_helper_gate_prevents_writes_before_ownership_publication(self):
        owned, atomic, guard = self.process_helper()
        op = Path(self.plan()['stage']).parent
        marker = self.home / 'gate-opened'
        real_save = owned.save_owner
        def fail_publication(path, record, writer):
            if record['phase'] == 'running':
                self.assertFalse(marker.exists())
                raise OSError('simulated publication failure')
            real_save(path, record, writer)
        with patch.object(owned, 'save_owner', side_effect=fail_publication), self.assertRaises(OSError):
            owned.run([sys.executable, '-c', f'from pathlib import Path; Path({str(marker)!r}).touch()'],
                      dict(os.environ), op, atomic, guard)
        self.assertFalse(marker.exists())
        self.assertIsNone(owned.pending_writer(op.parent, guard))
        (op / 'helper.json').write_text('{"schema":1,"phase":"launching"}')
        self.assertEqual(owned.pending_writer(op.parent, guard), 'helper_ownership_unknown')

    def test_export_names_bound_to_consent(self):
        self.content = b"export ALLOWED_KEY='sk-secret-canary'\nexport NOT_SELECTED='sk-unticked-canary'\n"
        self.files = [{'agent': 'keys', 'rel': 'secrets.env', 'sha256': hashlib.sha256(self.content).hexdigest(), 'names': ['ALLOWED_KEY']}]
        self.write_inventory()
        self.selection.write_text(json.dumps(['keys:secrets.env']))
        a = self.plan()
        self.stage(a)
        self.assertEqual(self.invoke('advance', a['operation_id'])['reason'], 'unselected_export_name')
        self.assertFalse((self.home / '.agent-keys').exists())


if __name__ == '__main__':
    unittest.main()
