#!/usr/bin/env python3
"""Offline controller tests: synthetic files, isolated HOME and mocked helpers."""
import argparse
import hashlib
import importlib.util
import json
import os
import subprocess
import tempfile
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
        def apply(*args, **kwargs):
            calls.append((args[3], kwargs['dry']))
            return (4, {'ok': False}) if args[3] == 'pi' else (0, {'ok': True})
        with patch.object(IMPORT, 'call_apply', side_effect=apply):
            r = self.invoke('advance', a['operation_id'], '--approve-runtime')
        self.assertEqual(r['reason'], 'conflicts')
        self.assertTrue(all(dry for _, dry in calls))
        self.assertEqual(len(calls), 2)

    def test_interrupted_apply_explicit_retry_and_verify_cleanup(self):
        a = self.plan()
        self.stage(a)
        ident = a['operation_id']
        with patch.object(IMPORT, 'call_apply', side_effect=[(0, {'ok': True}), TimeoutError()]), self.assertRaises(TimeoutError):
            self.invoke('advance', ident, '--approve-runtime')
        self.assertEqual(self.invoke('status', ident)['reason'], 'interrupted_apply')
        self.assertEqual(self.invoke('advance', ident)['reason'], 'interrupted_apply')
        with patch.object(IMPORT, 'call_apply', return_value=(0, {'ok': True})) as apply:
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
        self.invoke('advance', a['operation_id'])
        self.assertEqual(keys.read_bytes(), self.content)
        self.assertEqual(self.invoke('verify', a['operation_id'])['phase'], 'verified')

    def test_real_settings_only_apply_preserves_defaults(self):
        self.content = b'fixture instructions\n'
        self.files = [{'agent': 'codex', 'rel': 'AGENTS.md', 'sha256': hashlib.sha256(self.content).hexdigest()}]
        self.write_inventory()
        self.selection.write_text(json.dumps(['codex:AGENTS.md']))
        cli = self.home / '.local/bin/codex'
        cli.write_text('#!/bin/sh\nexit 0\n')
        cli.chmod(0o700)
        config = self.home / '.grok-bot-agents/config.json'
        config.parent.mkdir()
        config.write_text('{"fixture":"existing overrides stay untouched"}')
        before = config.read_bytes()
        a = self.plan()
        self.stage(a)
        self.assertEqual(self.invoke('advance', a['operation_id'], '--approve-runtime')['phase'], 'applied')
        self.assertEqual((self.home / '.codex/AGENTS.md').read_bytes(), self.content)
        self.assertEqual(config.read_bytes(), before)

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
