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

    def fake_diagnostics_tools(self):
        directory = self.home / 'fakebin'
        directory.mkdir()
        for name in ('claude', 'codex', 'pi', 'omp', 'node', 'npm', 'bun', 'bash'):
            path = directory / name
            path.write_text('#!/usr/bin/python3\nimport os, pathlib, sys\n'
                            'with (pathlib.Path(os.environ["HOME"]) / "executed-tools").open("a") as log: '
                            'log.write(' + repr(name) + ' + " " + " ".join(sys.argv[1:]) + "\\n")\n'
                            'print("1.2.3")\n')
            path.chmod(0o755)
        self.env['PATH'] = str(directory)
        config = self.home / '.grok-bot-agents/config.json'
        config.parent.mkdir()
        config.write_text(json.dumps({'version': 1, 'enabled': ['codex'], 'timeout_sec': 1800,
                                      'runs_dir': str(self.home / 'runs'), 'permission_mode': 'safe',
                                      'worktree_per_task': True, 'routing': {'mode': 'ask'},
                                      'agents': {'codex': {'auth': 'api_key', 'key_env': 'OPENAI_API_KEY'}}}))
        (self.home / '.agent-keys').write_text('export OPENAI_API_KEY=' + CANARY + '\n')

    def test_static_doctor_and_verify_write_neither_home_nor_plugin_bytecode(self):
        plugin = self.home / 'plugin'
        shutil.copytree(ROOT / 'scripts', plugin / 'scripts', ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(ROOT / 'skills', plugin / 'skills', ignore=shutil.ignore_patterns('__pycache__'))
        self.fake_diagnostics_tools()

        def snapshot():
            return {str(p): (p.stat().st_mtime_ns, p.read_bytes() if p.is_file() else None)
                    for p in self.home.rglob('*')}

        before = snapshot()
        for args in (('doctor', '--json'), ('verify', '--agents', 'codex')):
            with self.subTest(args=args):
                result = self.invoke(*args, entry=plugin / 'scripts/gba')
                self.assertEqual(result['status'], 'ok', result)
                self.assertEqual(result['agents']['codex']['version_status'], 'not_requested')
                self.assertFalse(result['version_probes']['requested'])
                self.assertEqual(before, snapshot())
        self.assertFalse((self.home / 'executed-tools').exists())
        self.assertFalse(list(plugin.rglob('__pycache__')))

    def test_version_probe_flag_is_explicit_for_doctor_and_verify(self):
        self.fake_diagnostics_tools()
        for index, args in enumerate((('doctor', '--json', '--probe-versions'),
                                      ('verify', '--agents', 'codex', '--probe-versions')), 1):
            with self.subTest(args=args):
                result = self.invoke(*args)
                self.assertEqual(result['status'], 'ok', result)
                self.assertTrue(result['version_probes']['requested'])
                self.assertEqual(result['agents']['codex']['version'], '1.2.3')
                self.assertEqual((self.home / 'executed-tools').read_text(),
                                 'codex --version\nnode --version\nnpm --version\nbun --version\n' * index)
                self.assertFalse(result['live']['requested'])

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
