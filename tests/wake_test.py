#!/usr/bin/env python3
"""Offline wake regressions: temporary HOME, stub CLIs, loopback HTTP only."""
import json
import os
import signal
import socket
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import delegate_runner_test as fixtures

ROOT = Path(__file__).resolve().parents[1]


class WakeFixture(unittest.TestCase):
    env: dict[str, str]
    root: Path
    runs: Path
    count: int
    tmp: tempfile.TemporaryDirectory
    codes: list[int | None]
    setup_runner = fixtures.RunnerTests.setUp
    command = fixtures.RunnerTests.command
    run_agent = fixtures.RunnerTests.run_agent
    background = fixtures.RunnerTests.background
    wait_file = fixtures.RunnerTests.wait_file
    next_output = fixtures.RunnerTests.next_output

    def setUp(self):
        self.setup_runner()
        self.env["PYTHONDONTWRITEBYTECODE"] = "1"
        self.token = os.urandom(24).hex()
        self.url_marker = os.urandom(16).hex()
        self.requests = []
        self.codes = [200]
        self.entered, self.release = threading.Event(), threading.Event()
        self.release.set()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
                # Record booleans, never secret bytes, even in assertion output.
                owner.requests.append({"payload": payload,
                                       "authorized": self.headers.get("Authorization") == "Bearer " + owner.token})
                owner.entered.set()
                owner.release.wait(10)
                code = owner.codes.pop(0) if len(owner.codes) > 1 else owner.codes[0]
                if code is None:
                    self.connection.shutdown(socket.SHUT_RDWR)
                    self.connection.close()
                    return
                self.send_response(code)
                if code == 302:
                    self.send_header("Location", "/must-not-follow")
                self.end_headers()

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        thread.start()

        def close():
            self.release.set()
            self.server.shutdown()
            self.server.server_close()
            thread.join(5)
        self.addCleanup(close)
        self.env.update(GBA_WAKE_URL=f"http://127.0.0.1:{self.server.server_port}/" + self.url_marker,
                        GBA_WAKE_HEADER="Authorization: Bearer " + self.token)

    def safe(self, raw):
        self.assertTrue(self.token not in raw and self.url_marker not in raw, "wake credential escaped (redacted)")

    def lifecycle(self, action, run, *args):
        proc = subprocess.run(["python3", str(fixtures.SCRIPTS / "run_lifecycle.py"), action, str(run), *args],
                              env=self.env, capture_output=True, text=True, timeout=18)
        self.safe(proc.stdout + proc.stderr)
        return proc, json.loads(proc.stdout)

    def wake_state(self, run):
        return json.loads((Path(run) / "wake.json").read_text())

    def success_events(self, agent):
        if agent == "claude":
            return [{"type": "result", "is_error": False, "subtype": "success", "result": "done"}]
        if agent == "codex":
            return [{"type": "turn.completed"}]
        return [fixtures.message()]

    def start(self, run_id="active", agent="pi", timeout="60", mode="webhook"):
        self.env["FAKE_DELAY"] = "60"
        Path(self.env["FAKE_OUTPUT"]).write_text("\n".join(json.dumps(e) for e in self.success_events(agent)))
        proc = self.background("bash", fixtures.SCRIPTS / "run-agent.sh", "--agent", agent, "--id", run_id,
                               "--timeout", timeout, "--wake", mode, "--prompt", "private task")
        self.wait_file(self.runs / run_id / "agent.pid")
        self.wait_file(Path(self.env["FAKE_ARGS"] + ".calls"))
        return proc, self.runs / run_id

    def synthetic(self, name="synthetic"):
        run = self.runs / name
        run.mkdir(parents=True)
        (run / "lifecycle.json").write_text(json.dumps({"schema": 1, "id": name, "root_id": name, "attempt": 1}))
        (run / "wake.json").write_text(json.dumps({"schema": 1, "mode": "webhook", "requested_mode": "webhook",
                                                   "claimed": False, "outcome": "pending", "reason": "awaiting_completion"}))
        (run / "meta.json").write_text(json.dumps({"status": "ok", "agent": "pi", "ended_at": "2026-10-19T12:00:00Z"}))
        return run


class WakeTests(WakeFixture):
    def test_all_agents_success_failure_timeout_once_and_private_payload(self):
        for agent in ("claude", "codex", "pi", "omp"):
            for status, rc, delay, timeout in (("ok", 0, "0", "30"), ("error", 7, "0", "30"), ("timeout", 0, "3", "1")):
                with self.subTest(agent=agent, status=status):
                    self.env["FAKE_DELAY"] = delay
                    before = len(self.requests)
                    meta = self.run_agent(agent, self.success_events(agent), rc=rc, extra=("--timeout", timeout, "--wake", "auto"))
                    run = Path(meta["run_dir"])
                    self.assertEqual(meta["status"], status)
                    self.assertEqual(len(self.requests), before + 1)
                    request = self.requests[-1]
                    self.assertTrue(request["authorized"])
                    payload = request["payload"]
                    self.assertEqual(set(payload), {"event", "run_id", "root_id", "agent", "status", "reason", "ended_at", "note"})
                    self.assertEqual((payload["event"], payload["agent"], payload["status"]), ("run_done", agent, status))
                    self.assertEqual((payload["run_id"], payload["root_id"]), (run.name, run.name))
                    self.assertEqual(payload["ended_at"], meta["ended_at"])
                    self.assertRegex(payload["ended_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
                    self.assertEqual(self.wake_state(run)["outcome"], "sent")
                    self.assertEqual(self.wake_state(run)["http_status"], 200)
                    for filename in ("launch.json", "lifecycle.json"):
                        self.assertEqual(json.loads((run / filename).read_text())["wake_mode"], "webhook")
                    self.lifecycle("finish-wake", run)
                    self.assertEqual(len(self.requests), before + 1)

    def test_auto_selection_overrides_and_validated_threshold(self):
        cases = [("auto", 600, True, "webhook"), ("auto", 601, True, "supervisor"),
                 ("auto", 1, False, "supervisor"), ("none", 1, True, "none"),
                 ("supervisor", 1, True, "supervisor"), ("webhook", 601, True, "webhook")]
        header = self.env["GBA_WAKE_HEADER"]
        for mode, timeout, configured, expected in cases:
            with self.subTest(mode=mode, timeout=timeout, configured=configured):
                self.env["GBA_WAKE_HEADER"] = header if configured else ""
                meta = self.run_agent("pi", [fixtures.message()], extra=("--wake", mode, "--timeout", str(timeout)))
                self.assertEqual(self.wake_state(meta["run_dir"])["mode"], expected)
        config = Path(self.env["HOME"]) / ".grok-bot-agents/config.json"
        config.parent.mkdir(exist_ok=True)
        for threshold in (2, True, 0, -1, 86401, "600", 2.5):
            config.write_text(json.dumps({"enabled": ["pi"], "wake_auto_max_sec": threshold}))
            if type(threshold) is int and threshold == 2:
                for timeout, expected in ((2, "webhook"), (3, "supervisor")):
                    meta = self.run_agent("pi", [fixtures.message()], extra=("--timeout", str(timeout)))
                    self.assertEqual(self.wake_state(meta["run_dir"])["mode"], expected)
            else:
                proc = self.command("run-agent.sh", "--agent", "pi", "--prompt", "test")
                self.assertNotEqual(proc.returncode, 0)
                self.assertEqual(json.loads(proc.stdout)["error"], "invalid wake configuration")

    def test_runner_kill_and_stall_terminal_finalize_after_stopping_worker(self):
        for agent in ("claude", "codex", "pi", "omp"):
            for sig, reason in ((signal.SIGTERM, "killed"), (signal.SIGUSR1, "stall_terminal")):
                with self.subTest(agent=agent, reason=reason):
                    proc, run = self.start(agent + "-" + reason, agent)
                    proc.send_signal(sig)
                    proc.wait(timeout=10)
                    self.assertEqual(json.loads((run / "meta.json").read_text())["error_kind"], reason)
                    self.assertEqual(self.requests[-1]["payload"]["reason"], reason)
                    self.assertEqual(self.wake_state(run)["outcome"], "sent")
                    self.assertTrue((run / "result.md").is_file())

    def test_secret_absent_from_ps_argv_logs_and_state_and_child_env(self):
        for name in ("pi", "omp", "claude", "codex"):
            stub = Path(self.env["HOME"]) / "bin" / name
            stub.write_text(stub.read_text().replace('if "--help" in sys.argv:',
                'assert not os.environ.get("GBA_WAKE_URL") and not os.environ.get("GBA_WAKE_HEADER")\nif "--help" in sys.argv:'))
            proc, run = self.start("secret-" + name, name)
            ps = subprocess.run(["ps", "-eo", "args"], env=self.env, capture_output=True, text=True, check=True)
            self.safe(ps.stdout + ps.stderr)
            proc.send_signal(signal.SIGTERM)
            proc.wait(timeout=10)
            for path in run.rglob("*"):
                if path.is_file():
                    self.safe(path.read_text(errors="replace"))
            self.safe(Path(self.env["FAKE_ARGS"]).read_text())
            self.assertEqual(self.wake_state(run)["outcome"], "sent")

    def test_retries_rejection_failed_recorded_and_results_precede_network(self):
        self.codes = [503]
        self.release.clear()
        # No worker delay: hold the HTTP response, not result publication.
        Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(fixtures.message()))
        proc = self.background("bash", fixtures.SCRIPTS / "run-agent.sh", "--agent", "pi", "--id", "rejected",
                               "--wake", "webhook", "--prompt", "test")
        self.assertTrue(self.entered.wait(8))
        run = self.runs / "rejected"
        self.assertTrue((run / "meta.json").is_file() and (run / "result.md").is_file())
        self.assertEqual(self.wake_state(run)["outcome"], "sending")
        self.release.set()
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)
        state = self.wake_state(run)
        self.assertEqual((state["outcome"], state["http_status"], state["attempts"]), ("failed", 503, 3))
        self.assertEqual(len(self.requests), 3)
        self.lifecycle("finish-wake", run)
        self.assertEqual(len(self.requests), 3)

    def test_retry_then_success_and_bare_header(self):
        self.codes = [503, 429, 204]
        self.env["GBA_WAKE_HEADER"] = "Bearer " + self.token
        meta = self.run_agent("pi", [fixtures.message()], extra=("--wake", "webhook"))
        state = self.wake_state(meta["run_dir"])
        self.assertEqual((state["outcome"], state["http_status"], state["attempts"]), ("sent", 204, 3))
        self.assertTrue(all(req["authorized"] for req in self.requests))

    def test_missing_invalid_config_and_redirect_never_leak_or_send_elsewhere(self):
        good_url, good_header = self.env["GBA_WAKE_URL"], self.env["GBA_WAKE_HEADER"]
        for url, header, reason in ((good_url, "", "wake_config_missing"),
                                    (good_url, good_header + "\r\nX-Evil: yes", "wake_config_invalid"),
                                    ("file:///tmp/not-http", good_header, "wake_config_invalid"),
                                    ("http://example.invalid/", good_header, "wake_config_invalid")):
            self.env.update(GBA_WAKE_URL=url, GBA_WAKE_HEADER=header)
            meta = self.run_agent("pi", [fixtures.message()], extra=("--wake", "webhook"))
            state = self.wake_state(meta["run_dir"])
            self.assertEqual((state["outcome"], state["reason"]), ("skipped", reason))
        self.assertEqual(len(self.requests), 0)
        self.env.update(GBA_WAKE_URL=good_url, GBA_WAKE_HEADER=good_header)
        self.codes = [302]
        meta = self.run_agent("pi", [fixtures.message()], extra=("--wake", "webhook"))
        self.assertEqual(self.wake_state(meta["run_dir"])["http_status"], 302)
        self.assertEqual(len(self.requests), 1)

    def test_lost_acknowledgement_is_not_retried(self):
        self.codes = [None]
        meta = self.run_agent("pi", [fixtures.message()], extra=("--wake", "webhook"))
        state = self.wake_state(meta["run_dir"])
        self.assertEqual((state["outcome"], state["reason"], state["attempts"]), ("failed", "delivery_unknown", 1))
        self.assertEqual(len(self.requests), 1)
        self.lifecycle("finish-wake", meta["run_dir"])
        self.assertEqual(len(self.requests), 1)

    def test_concurrent_finalizers_claim_once_and_late_handoff_is_refused(self):
        run = self.synthetic()
        processes = [self.background("python3", fixtures.SCRIPTS / "run_lifecycle.py", "finish-wake", run) for _ in range(8)]
        for proc in processes:
            proc.wait(timeout=15)
            self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(self.requests), 1)
        proc, reply = self.lifecycle("supervisor-handoff", run)
        self.assertEqual((proc.returncode, reply["error"]), (6, "webhook_already_claimed"))
        proc, reply = self.lifecycle("reserve", run, "new-attempt", "0")
        self.assertEqual(reply["error"], "webhook_already_claimed")

    def test_handoff_and_finalizer_race_has_only_one_owner(self):
        for n in range(5):
            run = self.synthetic("race-" + str(n))
            before = len(self.requests)
            actions = ("supervisor-handoff", "finish-wake") if n % 2 else ("finish-wake", "supervisor-handoff")
            processes = [self.background("python3", fixtures.SCRIPTS / "run_lifecycle.py", action, run) for action in actions]
            for proc in processes:
                proc.wait(timeout=15)
                self.assertIn(proc.returncode, (0, 6))
            state = self.wake_state(run)
            if state["mode"] == "supervisor":
                self.assertEqual(len(self.requests), before)
                self.assertFalse(state["claimed"])
            else:
                self.assertEqual(len(self.requests), before + 1)
                self.assertFalse((run / "supervisor.handoff.json").exists())

    def test_recovery_disables_webhook_before_kill_and_keeps_lineage(self):
        proc, run = self.start("recovering")
        self.env["FAKE_DELAY"] = "0"
        resumed = self.command("resume-run.sh", run, "--kill")
        self.safe(resumed.stdout + resumed.stderr)
        self.assertEqual(resumed.returncode, 0)
        proc.wait(timeout=10)
        child = Path(json.loads(resumed.stdout)["run_dir"])
        receipt = json.loads((child / "lifecycle.json").read_text())
        self.assertEqual((receipt["root_id"], receipt["wake_mode"]), (run.name, "supervisor"))
        self.assertEqual(len(self.requests), 0)
        for source in (run, child):
            self.lifecycle("finish-wake", source)
        self.assertEqual(len(self.requests), 0)

    def test_only_lineage_head_can_send_even_with_concurrent_old_attempts(self):
        run = self.synthetic("origin")
        child = self.runs / "continuation"
        child.mkdir()
        receipt = {"root_id": run.name, "id": child.name, "parent_id": run.name, "attempt": 2, "extra_approved": False}
        (run / "lineage.json").write_text(json.dumps({"schema": 1, "root_id": run.name, "attempts": [receipt]}))
        (child / "lifecycle.json").write_text(json.dumps(dict(receipt, schema=1)))
        (child / "meta.json").write_bytes((run / "meta.json").read_bytes())
        processes = [self.background("python3", fixtures.SCRIPTS / "run_lifecycle.py", "finish-wake", source)
                     for source in (run, child, run, child)]
        for proc in processes:
            proc.wait(timeout=15)
            self.assertEqual(proc.returncode, 0)
        self.assertEqual(len(self.requests), 1)
        self.assertEqual((self.requests[0]["payload"]["run_id"], self.requests[0]["payload"]["root_id"]), (child.name, run.name))

    def test_early_handoff_survives_launch_initialization(self):
        ready, release = self.root / "ready", self.root / "release"
        self.env.update(INIT_READY=str(ready), INIT_RELEASE=str(release))
        wrapper = Path(self.env["HOME"]) / "bin/python3"
        wrapper.write_text('''#!/usr/bin/python3
import os, pathlib, sys, time
if len(sys.argv) > 2 and pathlib.Path(sys.argv[1]).name == "run_lifecycle.py" and sys.argv[2] == "record-launch":
    pathlib.Path(os.environ["INIT_READY"]).touch()
    while not pathlib.Path(os.environ["INIT_RELEASE"]).exists():
        time.sleep(0.01)
os.execv("/usr/bin/python3", ["/usr/bin/python3", *sys.argv[1:]])
''')
        wrapper.chmod(0o700)
        Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(fixtures.message()))
        proc = self.background("bash", fixtures.SCRIPTS / "run-agent.sh", "--agent", "pi", "--id", "early",
                               "--timeout", "30", "--wake", "auto", "--prompt", "test")
        self.wait_file(ready)
        run = self.runs / "early"
        handoff, _ = self.lifecycle("supervisor-handoff", run)
        self.assertEqual(handoff.returncode, 0)
        release.touch()
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.wake_state(run)["mode"], "supervisor")
        self.assertEqual(json.loads((run / "launch.json").read_text())["wake_mode"], "supervisor")
        self.assertEqual(len(self.requests), 0)

    def test_refused_recovery_does_not_disable_webhook_or_stop_worker(self):
        proc, run = self.start("refused")
        state = (run / "wake.json").read_bytes()
        refused = self.command("resume-run.sh", run)
        self.assertEqual(refused.returncode, 5)
        self.assertEqual((run / "wake.json").read_bytes(), state)
        self.assertFalse((run / "supervisor.handoff.json").exists())
        launch = run / "launch.json"
        original = launch.read_bytes()
        corrupted = json.loads(original)
        corrupted["effective_inputs"] = None
        launch.write_text(json.dumps(corrupted))
        refused = self.command("resume-run.sh", run, "--kill")
        self.assertEqual(refused.returncode, 6)
        self.assertEqual((run / "wake.json").read_bytes(), state)
        self.assertIsNone(proc.poll())
        launch.write_bytes(original)
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
        self.assertEqual(len(self.requests), 1)

    def test_post_reap_signal_does_not_target_stale_agent_or_change_result(self):
        self.release.clear()
        Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(fixtures.message()))
        proc = self.background("bash", fixtures.SCRIPTS / "run-agent.sh", "--agent", "pi", "--id", "reaped",
                               "--wake", "webhook", "--prompt", "test")
        self.assertTrue(self.entered.wait(8))
        ps = subprocess.run(["ps", "-eo", "args"], env=self.env, capture_output=True, text=True, check=True)
        self.safe(ps.stdout + ps.stderr)
        proc.send_signal(signal.SIGTERM)
        self.release.set()
        proc.wait(timeout=10)
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(self.requests[0]["payload"]["status"], "ok")
        self.assertEqual(self.wake_state(self.runs / "reaped")["outcome"], "sent")

    def test_unresponsive_http_is_bounded_and_does_not_change_result(self):
        self.release.clear()
        meta = self.run_agent("pi", [fixtures.message()], extra=("--wake", "webhook"))
        self.release.set()
        self.assertEqual(meta["status"], "ok")
        state = self.wake_state(meta["run_dir"])
        self.assertEqual((state["outcome"], state["attempts"]), ("failed", 1))
        self.assertEqual(len(self.requests), 1)

    def test_interrupted_claim_never_replays(self):
        run = self.synthetic()
        code = '''import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import run_lifecycle as m
m.wake.send_bounded = lambda payload: sys.exit(99)
m.finish_wake(Path(sys.argv[2]))
'''
        proc = subprocess.run(["python3", "-c", code, str(fixtures.SCRIPTS), str(run)], env=self.env,
                              capture_output=True, text=True, timeout=5)
        self.safe(proc.stdout + proc.stderr)
        self.assertEqual(proc.returncode, 99)
        self.assertEqual(self.wake_state(run)["outcome"], "sending")
        self.lifecycle("finish-wake", run)
        self.assertEqual(len(self.requests), 0)
        self.assertTrue(self.wake_state(run)["claimed"])


if __name__ == "__main__":
    unittest.main()
