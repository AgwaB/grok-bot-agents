#!/usr/bin/env python3
"""Offline diagnostics, leak canaries, and fake (never provider) live verification."""
import argparse
import contextlib
import importlib
import io
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
support = importlib.import_module("gba_setup_test")
setup = importlib.import_module("gba_setup")
doctor = importlib.import_module("gba_doctor")
CANARY = support.CANARY


class DoctorTests(support.Isolated):
    def invoke(self, command="doctor", agents="codex", live=False):
        parser = argparse.ArgumentParser()
        doctor.configure(parser.add_subparsers(dest="command", required=True))
        argv = ["doctor", "--json"] if command == "doctor" else ["verify", "--agents", agents] + (["--live"] if live else [])
        args = parser.parse_args(argv)
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            response = doctor.handle(args, ROOT)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertNotIn(CANARY, json.dumps(response))
        for key in ("status", "reason", "next_action", "requires_approval"):
            self.assertIn(key, response)
        return response

    def ready(self):
        self.config(["codex"])
        self.executable("codex", '''import pathlib, os, sys
assert sys.argv[1:] == ["--version"], "no offline model requests"
with (pathlib.Path(os.environ["HOME"]) / "agent-starts").open("a") as log: log.write("version\\n")
print("codex-cli 1.2.3")
print("s3cret_CANARY_must_never_appear")
print("s3cret_CANARY_must_never_appear", file=sys.stderr)
''')
        self.fake_runtime()
        (self.home / ".agent-keys").write_text("export OPENAI_API_KEY=" + CANARY + "\n")

    def fake_bash(self, status="ok", summary="OK", exit_code=0):
        payload = json.dumps({"status": status, "summary": summary, "stderr_tail": CANARY,
                              "model": CANARY, "artifacts": [CANARY], "run_dir": CANARY})
        self.executable("bash", '''import json, os, pathlib, sys
args = sys.argv[1:]
assert args[0].endswith("/skills/delegate-task/scripts/run-agent.sh")
assert args[1:] == ["--agent", "codex", "--read-only", "--timeout", "120", "--prompt", "Reply with exactly: OK"]
with (pathlib.Path(os.environ["HOME"]) / "live-starts").open("a") as log: log.write("live\\n")
print(''' + repr(payload) + ''')
print("s3cret_CANARY_must_never_appear", file=sys.stderr)
sys.exit(''' + str(exit_code) + ''')
''')

    def test_doctor_offline_json_and_versions_without_live_or_key_execution(self):
        self.ready()
        marker = self.home / "should-not-exist"
        (self.home / ".agent-keys").write_text("export OPENAI_API_KEY=$(/usr/bin/touch " + str(marker) + ")\n")
        self.fake_bash()
        response = self.invoke()
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["agents"]["codex"]["version"], "1.2.3")
        self.assertEqual(response["agents"]["codex"]["path"], "$HOME/fakebin/codex")
        self.assertTrue(response["keys"]["presence"]["OPENAI_API_KEY"])
        self.assertEqual(response["parity"]["status"], "ok")
        self.assertFalse(response["live"]["requested"])
        self.assertFalse(marker.exists())
        self.assertFalse((self.home / "live-starts").exists())
        self.assertEqual((self.home / "agent-starts").read_text(), "version\n")

    def test_verify_offline_never_calls_runner(self):
        self.ready()
        with mock.patch.object(doctor, "_live_one", side_effect=AssertionError("live must be explicit")):
            response = self.invoke("verify")
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["operation"], "verify")
        self.assertEqual(response["mode"], "offline")

    def test_verify_live_routes_exact_OK_prompt_through_existing_runner(self):
        self.ready()
        self.fake_bash()
        response = self.invoke("verify", live=True)
        self.assertEqual(response["status"], "ok", response)
        self.assertEqual(response["reason"], "verification_ok")
        self.assertEqual(response["live"]["agents"]["codex"]["quota"], "not_limited")
        self.assertEqual((self.home / "live-starts").read_text(), "live\n")

    def test_live_quota_is_distinct_from_install_auth_or_version_failure(self):
        self.ready()
        self.fake_bash(status="rate_limited", summary=CANARY, exit_code=1)
        response = self.invoke("verify", live=True)
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["reason"], "quota_limited")
        self.assertEqual(response["next_action"], "retry_later")
        self.assertEqual(response["live"]["agents"]["codex"]["quota"], "limited")
        self.assertTrue(response["agents"]["codex"]["installed"])
        self.assertEqual(response["parity"]["status"], "ok")

    def test_live_requires_exact_OK_not_just_exit_zero(self):
        self.ready()
        self.fake_bash(summary="OK " + CANARY)
        response = self.invoke("verify", live=True)
        self.assertEqual(response["status"], "blocked")
        self.assertEqual(response["live"]["agents"]["codex"]["reason"], "live_response_mismatch")

    def test_live_refuses_invalid_or_disabled_config(self):
        self.ready()
        cfg = json.loads(setup.config_path().read_text())
        cfg["enabled"] = ["pi"]
        setup.config_path().write_text(json.dumps(cfg))
        self.fake_bash()
        response = self.invoke("verify", live=True)
        self.assertEqual(response["live"]["agents"]["codex"]["reason"], "live_prerequisites_missing")
        self.assertFalse((self.home / "live-starts").exists())
        cfg["enabled"] = [CANARY]
        setup.config_path().write_text(json.dumps(cfg))
        response = self.invoke("verify", live=True)
        self.assertEqual(response["config"]["status"], "config_invalid")
        self.assertFalse((self.home / "live-starts").exists())

    def test_config_missing_and_invalid_return_stable_no_value_diagnostics(self):
        with mock.patch.object(doctor, "_parity", return_value={"status": "ok", "agents": {}}):
            self.assertEqual(self.invoke()["reason"], "config_missing")
            path = setup.config_path()
            path.parent.mkdir(parents=True)
            path.write_text('{"enabled": "' + CANARY + '"}')
            self.assertEqual(self.invoke()["reason"], "config_invalid")

    def test_real_parity_gaps_are_counts_only(self):
        self.ready()
        (self.home / ".agent-keys").unlink()
        (self.home / ".codex").mkdir()
        (self.home / ".codex/config.toml").write_text('[mcp_servers.' + CANARY + ']\ncommand="' + CANARY + '"\n')
        response = self.invoke()
        self.assertEqual(response["status"], "blocked")
        counts = response["parity"]["agents"]["codex"]["counts"]
        self.assertEqual(counts["breaking"], 1)
        self.assertEqual(counts["degraded"], 1)

    def test_malformed_parity_and_live_output_cannot_leak(self):
        self.ready()
        with mock.patch.object(setup, "run_captured", return_value=(1, CANARY.encode(), CANARY.encode(), "ok")):
            parity = doctor._parity(ROOT, ["codex"], [])
            self.assertEqual(parity["reason"], "parity_result_invalid")
            self.fake_bash()
            live = doctor._live_one(ROOT, "codex")
            self.assertEqual(live["reason"], "live_result_invalid")
        self.assertNotIn(CANARY, json.dumps([parity, live]))

    def test_version_has_full_line_allowlist_and_bounded_output(self):
        path = self.executable("codex", 'print("token=' + CANARY + ' 1.2.3")\n')
        self.assertIsNone(setup.probe_version(path)["version"])
        self.executable("codex", 'print("x" * 200000)\n')
        self.assertEqual(setup.probe_version(path)["status"], "command_output_limit")
        path = self.executable("codex", 'import time\ntime.sleep(30)\n')
        rc, out, err, reason = setup.run_captured([str(path), "--version"], timeout=0.05)
        self.assertEqual(reason, "command_timeout")
        self.assertIsNotNone(rc)
        self.assertEqual(out + err, b"")

    def test_key_file_symlink_is_not_read(self):
        self.ready()
        (self.home / ".agent-keys").unlink()
        target = self.home / "secret-file"
        target.write_text("OPENAI_API_KEY=" + CANARY)
        (self.home / ".agent-keys").symlink_to(target)
        presence, readable = doctor._key_presence(["OPENAI_API_KEY"])
        self.assertFalse(readable)
        self.assertFalse(presence["OPENAI_API_KEY"])

    def test_unknown_agent_input_never_echoed(self):
        response = self.invoke("verify", agents=CANARY)
        self.assertEqual(response["reason"], "invalid_agents")


if __name__ == "__main__":
    unittest.main()
