#!/usr/bin/env python3
"""Documentation drift contracts and data-only webhook boundary, entirely offline."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class SkillContracts(unittest.TestCase):
    def skill(self, name):
        return (ROOT / 'skills' / name / 'SKILL.md').read_text()

    def test_shared_entrypoint_and_skill_budget(self):
        skills = list((ROOT / 'skills').glob('*/SKILL.md'))
        self.assertEqual(len(skills), 5)
        self.assertLessEqual(sum(len(p.read_text().splitlines()) for p in skills), 350)
        for p in skills:
            self.assertIn('GBA_ROOT', p.read_text())
            self.assertIn('scripts/gba', p.read_text())

    def test_verified_drift_wording(self):
        delegate = self.skill('delegate-task')
        self.assertIn('decision: self|delegate|none', delegate)
        self.assertIn('skipped` object', delegate)
        self.assertIn('--type bulk` alone', delegate)
        configure = self.skill('configure-agents')
        self.assertIn('Pi `off/minimal/low/medium/high/xhigh/max`', configure)
        self.assertIn('never raw values', configure)
        setup = self.skill('setup-agents')
        for env in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'PI_CODING_AGENT_DIR'):
            self.assertIn(env, setup)
        self.assertIn('contents internally', setup)
        self.assertIn('Settings-only imports', setup)
        self.assertIn('whole file unticked', self.skill('import-agents'))
        self.assertIn('Only outbox delivery is implemented', self.skill('agent-bridge-webhook'))

    def test_webhook_reply_command_never_executed(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            marker = home / 'should-not-exist'
            body = home / 'body.json'
            body.write_text(json.dumps({'request_id': 'contract-1', 'task': 'safe test',
                                        'reply': {'type': 'command', 'reply_command': f'touch {marker}'}}))
            env = {'HOME': str(home), 'PATH': '/usr/bin:/bin', 'GROK_BRIDGE_ROOT': str(home / 'bridge'),
                   'GROK_BRIDGE_NO_SERVE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
            r = subprocess.run(['python3', str(ROOT / 'skills/agent-bridge-webhook/scripts/bridge.py'),
                                'intake', str(body)], env=env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0)
            data = json.loads(r.stdout)
            self.assertEqual(data['reply_type'], 'command')
            self.assertTrue(any('ignored fields' in n for n in data['notes']))
            self.assertFalse(marker.exists())

    def test_generated_agent_run_state_is_ignored_but_config_is_not(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = {'HOME': tmp, 'PATH': '/usr/bin:/bin'}
            # Test the tracked ignore alone, without local harness .pi/.gitignore files.
            repo = Path(tmp) / 'repo'
            subprocess.run(['git', 'init', '-q', str(repo)], env=env, check=True, capture_output=True)
            (repo / '.gitignore').write_bytes((ROOT / '.gitignore').read_bytes())
            for path in ('.pi/agent/runs/example/run.json', '.pi/agent/subagent-dispatch/example/receipt.json'):
                ignored = subprocess.run(['git', '-C', str(repo), 'check-ignore', '--no-index', path],
                                         env=env, capture_output=True, text=True)
                self.assertEqual(ignored.returncode, 0)
            config = subprocess.run(['git', '-C', str(repo), 'check-ignore', '--no-index', '.pi/settings.json'],
                                    env=env, capture_output=True)
            self.assertEqual(config.returncode, 1)
            tracked = subprocess.run(['git', '-C', str(ROOT), 'ls-files', '.pi'], env=env, capture_output=True)
            self.assertEqual(tracked.returncode, 0)
            self.assertEqual(tracked.stdout, b'')

    def test_version_never_bumped(self):
        self.assertEqual(json.loads((ROOT / '.cursor-plugin/plugin.json').read_text())['version'], '0.1.0')


if __name__ == '__main__':
    unittest.main()
