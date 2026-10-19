#!/usr/bin/env python3
"""Cross-module JSON contract and plugin-root discovery in an isolated HOME."""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANARY = 'sk-CLI_CANARY_NEVER_ECHO_836'


class CLIContracts(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin',
                    'AGENT_RUNS_ROOT': str(self.home / 'runs')}

    def invoke(self, *args, entry=None):
        p = subprocess.run(['/usr/bin/python3', str(entry or ROOT / 'scripts/gba'), *args],
                           cwd=self.home, env=self.env, capture_output=True, text=True, timeout=30)
        self.assertEqual(p.stderr, '')
        self.assertNotIn(CANARY, p.stdout)
        result = json.loads(p.stdout)
        self.assertEqual(result['schema'], 1)
        self.assertEqual(p.returncode, {'ok': 0, 'blocked': 3, 'error': 2}[result['status']])
        self.assertRegex(result['reason'], r'^[a-z][a-z0-9_]*$')
        self.assertIsInstance(result['requires_approval'], bool)
        self.assertEqual(result['action_owner'], 'bot' if result['next_action'] else None)
        return result

    def test_all_command_families_and_unrelated_cwd(self):
        for args in (('setup', 'plan', '--agents', 'pi'), ('import', 'status', 'missing'),
                     ('runs', 'list', '--open', '--json'), ('runs', 'next', 'missing'),
                     ('doctor', '--json'), ('verify', '--agents', 'pi')):
            with self.subTest(args=args):
                self.invoke(*args)

    def test_argument_errors_never_echo_values(self):
        for args in ((), (CANARY,), ('setup', CANARY), ('import', 'status', CANARY),
                     ('setup', 'plan', '--agents', CANARY), ('runs', 'recover', 'missing', '--token', CANARY)):
            with self.subTest(args=args):
                self.assertIn(self.invoke(*args)['status'], ('blocked', 'error'))

    def test_planning_writes_neither_home_nor_plugin_bytecode(self):
        plugin = self.home / 'plugin'
        shutil.copytree(ROOT / 'scripts', plugin / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'skills', plugin / 'skills', ignore=shutil.ignore_patterns('__pycache__'))
        before = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
        result = self.invoke('setup', 'plan', '--agents', 'codex,pi', entry=plugin / 'scripts/gba')
        self.assertEqual(result['reason'], 'setup_plan_ready')
        after = {str(p): (p.stat().st_mtime_ns, p.read_bytes()) for p in self.home.rglob('*') if p.is_file()}
        self.assertEqual(before, after)
        self.assertFalse(list(plugin.rglob('__pycache__')))

    def test_incomplete_standalone_copy_fails_closed(self):
        entry = self.home / 'plugin/scripts/gba'
        entry.parent.mkdir(parents=True)
        shutil.copy2(ROOT / 'scripts/gba', entry)
        self.assertEqual(self.invoke('setup', 'apply', '--agents', 'pi', entry=entry)['reason'], 'plugin_incomplete')
        self.assertFalse((self.home / '.local').exists())

    def test_malformed_inventory_has_no_exception_echo(self):
        inventory = self.home / 'bad.json'
        inventory.write_text('{"secret":"' + CANARY + '", broken')
        result = self.invoke('import', 'plan', '--inventory', str(inventory))
        self.assertEqual(result['reason'], 'invalid_input_or_state')
        self.assertFalse((self.home / '.grok-bot-agents').exists())


if __name__ == '__main__':
    unittest.main()
