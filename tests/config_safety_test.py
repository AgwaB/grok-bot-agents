#!/usr/bin/env python3
"""Offline config and router drift regressions; all state is in disposable HOME."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
CFG = ROOT / 'skills/configure-agents/scripts/agents_config.py'
ROUTE = ROOT / 'skills/delegate-task/scripts/route.py'


class ConfigSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.env = {'HOME': str(self.home), 'PATH': '/usr/bin:/bin', 'PYTHONDONTWRITEBYTECODE': '1'}
        self.call('init', '--enable', 'pi')
        self.path = self.home / '.grok-bot-agents/config.json'

    def call(self, *args):
        return subprocess.run(['python3', str(CFG), *args], env=self.env, capture_output=True, text=True)

    def test_secrets_in_any_field_never_saved_or_echoed(self):
        canaries = ['sk-test-canary-value', 'aZ8fQ2vB6hN4pR7tC9jL3wX5eK0mS1uY', 'c0ffee1234567890abcdef1234567890ab', 'Bearer canary']
        before = self.path.read_bytes()
        for key in ['agents.pi.model', 'routing.fixed', 'runs_dir', 'extra', 'enabled']:
            for secret in canaries:
                for value in [secret, json.dumps({'nested': [secret]})]:
                    result = self.call('set', key, value)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertNotIn(secret, result.stdout + result.stderr)
                    self.assertEqual(before, self.path.read_bytes())

    def test_success_does_not_echo_values_and_pi_max(self):
        result = self.call('set', 'agents.pi.model', 'ordinary-private-label', 'agents.pi.reasoning', 'max')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn('ordinary-private-label', result.stdout + result.stderr)
        self.assertEqual(json.loads(self.path.read_text())['agents']['pi']['reasoning'], 'max')

    def test_key_env_is_name_not_value(self):
        self.assertNotEqual(self.call('set', 'agents.pi.key_env', 'not a name').returncode, 0)
        self.assertEqual(self.call('set', 'agents.pi.key_env', 'DEEPSEEK_API_KEY').returncode, 0)

    def test_existing_secret_suppresses_read_output(self):
        data = json.loads(self.path.read_text())
        data['agents']['pi']['model'] = 'sk-canary-preexisting'
        self.path.write_text(json.dumps(data))
        for cmd in [('show',), ('json',), ('get', 'agents.pi.model'), ('validate',)]:
            result = self.call(*cmd)
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn('sk-canary-preexisting', result.stdout + result.stderr)

    def test_router_contract_type_bulk_is_not_heavy_by_itself(self):
        def route(*args):
            r = subprocess.run(['python3', str(ROUTE), 'decide', *args], env=self.env, capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return json.loads(r.stdout)
        light = route('--type', 'bulk')
        self.assertEqual(light['decision'], 'self')
        self.assertIsNone(light['agent'])
        binary = self.home / '.local/bin/pi'
        binary.parent.mkdir(parents=True)
        binary.write_text('#!/bin/sh\nexit 0\n')
        binary.chmod(0o700)
        heavy = route('--type', 'bulk', '--bulk')
        self.assertEqual(heavy['decision'], 'delegate')
        self.assertIsInstance(heavy['skipped'], dict)
        self.call('set', 'routing.mode', 'ask')
        self.assertTrue(route('--bulk')['ask_first'])
        named = route('--named', 'codex')
        self.assertEqual(named['decision'], 'none')
        self.assertIsNone(named['agent'])


if __name__ == '__main__':
    unittest.main()
