#!/usr/bin/env python3
"""Wake CLI decisions and config contracts, without providers or real homes."""
import importlib.util
import json
import signal
import subprocess
import unittest
from pathlib import Path

import delegate_runner_test as fixtures

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("wake_test", Path(__file__).with_name("wake_test.py"))
assert SPEC is not None and SPEC.loader is not None
wake_test = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(wake_test)


class WakeCliTests(wake_test.WakeFixture):
    def gba(self, *args):
        proc = subprocess.run(["python3", str(ROOT / "scripts/gba"), *args], env=self.env,
                              capture_output=True, text=True, timeout=20)
        self.safe(proc.stdout + proc.stderr)
        value = json.loads(proc.stdout)
        self.assertEqual(value["schema"], 1)
        return value

    def test_webhook_next_ends_turn_and_list_surfaces_chosen_mode(self):
        proc, run = self.start()
        decision = self.gba("runs", "next", run.name)
        self.assertEqual((decision["wake_mode"], decision["next_action"]), ("webhook", "end_turn"))
        self.assertNotIn("supervisor_command", decision)
        listing = self.gba("runs", "list", "--open")
        self.assertEqual(listing["runs"][0]["wake_mode"], "webhook")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assertEqual(self.gba("runs", "next", run.name)["next_action"], "review_result")
        self.assertEqual(self.gba("runs", "next", run.name, "--reported")["next_action"], "stay_silent")
        self.assertTrue(self.gba("runs", "next", run.name)["reported"])
        for flag in ("--handoff", "--rotate", "--supervisor-started"):
            self.assertEqual(self.gba("runs", "next", run.name, flag)["reason"], "webhook_already_claimed")
        self.assertEqual(self.gba("runs", "recover", run.name)["reason"], "webhook_already_claimed")
        self.assertEqual(len(self.requests), 1)

    def test_handoff_acknowledge_rotate_disable_webhook_and_dispatch_once(self):
        proc, run = self.start()
        value = self.gba("runs", "next", run.name, "--handoff")
        self.assertEqual((value["wake_mode"], value["next_action"]), ("supervisor", "dispatch_supervisor"))
        value = self.gba("runs", "next", run.name, "--supervisor-started")
        self.assertEqual(value["next_action"], "supervise")
        self.assertTrue(value["supervised"])
        self.assertEqual(self.gba("runs", "next", run.name, "--handoff")["reason"], "supervisor_already_active")
        value = self.gba("runs", "next", run.name, "--rotate")
        self.assertEqual((value["supervisor_generation"], value["next_action"]), (2, "dispatch_supervisor"))
        self.gba("runs", "next", run.name, "--supervisor-started")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assertEqual(len(self.requests), 0)
        state = self.wake_state(run)
        self.assertEqual((state["mode"], state["outcome"], state["reason"]), ("supervisor", "skipped", "supervisor_handoff"))

    def test_supervisor_none_missing_config_and_failed_delivery_actions(self):
        for mode, action in (("supervisor", "dispatch_supervisor"), ("none", "check_manually")):
            proc, run = self.start(mode, mode=mode)
            decision = self.gba("runs", "next", run.name)
            self.assertEqual((decision["wake_mode"], decision["next_action"]), (mode, action))
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
        self.env.pop("GBA_WAKE_HEADER")
        proc, run = self.start("missing")
        self.assertEqual(self.gba("runs", "next", run.name)["next_action"], "check_manually")
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        value = self.gba("runs", "next", run.name)
        self.assertEqual((value["wake"]["reason"], value["next_action"]), ("wake_config_missing", "check_manually"))
        self.assertEqual(len(self.requests), 0)
        self.env.update(GBA_WAKE_HEADER="Bearer " + self.token, FAKE_DELAY="0")
        self.codes = [503]
        meta = self.run_agent("pi", [fixtures.message()], extra=("--wake", "webhook"))
        value = self.gba("runs", "next", Path(meta["run_dir"]).name)
        self.assertEqual((value["wake"]["outcome"], value["next_action"]), ("failed", "check_manually"))

    def test_wake_test_uses_same_transport_and_doctor_is_name_only_and_static(self):
        value = self.gba("wake", "test")
        self.assertEqual((value["status"], value["wake"]["outcome"]), ("ok", "sent"))
        self.assertEqual(len(self.requests), 1)
        self.assertTrue(self.requests[0]["authorized"])
        payload = self.requests[0]["payload"]
        self.assertEqual(set(payload), {"event", "sent_at"})
        self.assertEqual(payload["event"], "wake_test")
        value = self.gba("doctor", "--json")
        self.assertEqual(value["wake"], {"presence": {"GBA_WAKE_URL": True, "GBA_WAKE_HEADER": True}})
        self.assertEqual(len(self.requests), 1)
        self.assertFalse(Path(self.env["FAKE_ARGS"] + ".calls").exists())
        self.env.pop("GBA_WAKE_HEADER")
        value = self.gba("wake", "test", "--json")
        self.assertEqual((value["status"], value["wake"]["reason"]), ("error", "wake_config_missing"))
        self.assertFalse(self.gba("doctor")["wake"]["presence"]["GBA_WAKE_HEADER"])
        self.assertEqual(len(self.requests), 1)
        self.env["GBA_WAKE_HEADER"] = "Bearer " + self.token
        self.codes = [503]
        value = self.gba("wake", "test")
        self.assertEqual((value["status"], value["wake"]["attempts"]), ("error", 3))

    def test_threshold_config_set_validation_and_default(self):
        config_script = ROOT / "skills/configure-agents/scripts/agents_config.py"
        def config(*args):
            proc = subprocess.run(["python3", str(config_script), *args], env=self.env,
                                  capture_output=True, text=True, timeout=5)
            self.safe(proc.stdout + proc.stderr)
            return proc
        self.assertEqual(config("init", "--enable", "pi").returncode, 0)
        path = Path(self.env["HOME"]) / ".grok-bot-agents/config.json"
        self.assertEqual(json.loads(path.read_text())["wake_auto_max_sec"], 600)
        for valid in ("1", "120", "86400"):
            self.assertEqual(config("set", "wake_auto_max_sec", valid).returncode, 0)
        before = path.read_bytes()
        for invalid in ("0", "-1", "86401", "true", "1.5", '"600"', "null"):
            self.assertNotEqual(config("set", "wake_auto_max_sec", invalid).returncode, 0)
            self.assertEqual(path.read_bytes(), before)

    def test_sending_claim_refuses_cli_rotation_without_waiting_for_http(self):
        self.release.clear()
        Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(fixtures.message()))
        proc = self.background("bash", fixtures.SCRIPTS / "run-agent.sh", "--agent", "pi", "--id", "sending",
                               "--wake", "webhook", "--prompt", "test")
        self.assertTrue(self.entered.wait(8))
        decision = self.gba("runs", "next", "sending", "--rotate")
        self.assertEqual(decision["reason"], "webhook_already_claimed")
        self.assertEqual(decision["wake"]["outcome"], "sending")
        self.release.set()
        proc.wait(timeout=10)
        self.assertEqual(len(self.requests), 1)


if __name__ == "__main__":
    unittest.main()
