#!/usr/bin/env python3
"""Offline runs-controller and lineage policy regressions; fake CLI and temp HOME."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
import signal
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import delegate_runner_test as fixtures

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("gba_runs", ROOT / "scripts/gba_runs.py")
assert SPEC is not None and SPEC.loader is not None
runs = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runs)
SECRET = "CANARY_PRIVATE_VALUE_NEVER_ECHO_97"  # noqa: S105 - synthetic output canary, not a credential

# Deterministic interruption boundaries, installed ONLY in the fixture's fake
# HOME/bin. Production code needs no test hooks and no real CLI is reachable.
INTERRUPT_TOOL = '''#!/usr/bin/python3
import os, pathlib, subprocess, sys, time
stage = os.environ.get("INTERRUPT_STAGE")
target = os.environ.get("INTERRUPT_TARGET")
def pause():
    pathlib.Path(os.environ["INTERRUPT_READY"]).touch()
    while True:
        time.sleep(60)
if pathlib.Path(sys.argv[0]).name == "mkdir":
    rc = subprocess.call(["/bin/mkdir", *sys.argv[1:]])
    if rc == 0 and stage == "allocated" and target in sys.argv[1:]:
        pause()
    sys.exit(rc)
if len(sys.argv) > 3 and pathlib.Path(sys.argv[1]).name == "run_lifecycle.py":
    if stage == "reserved" and sys.argv[2] == "reserve" and sys.argv[4] == pathlib.Path(target).name:
        rc = subprocess.call(["/usr/bin/python3", *sys.argv[1:]])
        if rc == 0:
            pause()
        sys.exit(rc)
    if sys.argv[2] == "record-launch" and sys.argv[3] == target:
        if stage == "command-metadata":
            pause()
        sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parent))
        import run_lifecycle as lifecycle
        original_write = lifecycle.write_json
        def write(path, value):
            original_write(path, value)
            if (stage == "lifecycle" and path.name == "lifecycle.json"
                    or stage == "launch" and path.name == "launch.json"):
                pause()
        lifecycle.write_json = write
        sys.argv = sys.argv[1:]
        sys.exit(lifecycle.main())
os.execv("/usr/bin/python3", ["/usr/bin/python3", *sys.argv[1:]])
'''


class RunsTests(unittest.TestCase):
    env: dict[str, str]
    root: Path
    runs: Path
    count: int
    tmp: tempfile.TemporaryDirectory

    # Reuse the exact env-isolated fake CLIs, process cleanup, and git fixtures
    # without inheriting (and accidentally rerunning) the full runner test suite.
    setUp = fixtures.RunnerTests.setUp
    command = fixtures.RunnerTests.command
    run_agent = fixtures.RunnerTests.run_agent
    next_output = fixtures.RunnerTests.next_output
    session_events = fixtures.RunnerTests.session_events
    native_run = fixtures.RunnerTests.native_run
    background = fixtures.RunnerTests.background
    wait_file = fixtures.RunnerTests.wait_file

    def call(self, *args):
        parser = argparse.ArgumentParser()
        runs.configure(parser.add_subparsers(dest="command", required=True))
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.dict(os.environ, self.env, clear=True), contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            value = runs.handle(parser.parse_args(["runs", *args]), ROOT)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(stderr.getvalue(), "")
        self.assertIn(value["status"], ("ok", "blocked", "error"))
        self.assertRegex(value["reason"], r"^[a-z][a-z0-9_]*$")
        self.assertIsInstance(value["requires_approval"], bool)
        self.assertNotIn(SECRET, json.dumps(value))
        return value

    def failed(self, **kwargs):
        return self.run_agent("pi", [fixtures.message("", "error", "Request timed out")], **kwargs)

    def test_cap_root_reuse_and_custom_ids_need_explicit_approval(self):
        m = self.failed()
        original = Path(m["run_dir"])
        for name in ("first-custom", "second-custom"):
            p = self.command("resume-run.sh", original, "--new-id", name)
            self.assertEqual(p.returncode, 1, p.stderr)
            self.assertEqual(json.loads(p.stdout)["status"], "error")
        before = Path(self.env["FAKE_ARGS"] + ".calls").read_bytes()
        for source in (original, self.runs / "first-custom", self.runs / "second-custom"):
            for suffix in ((), ("--dry-run",)):
                p = self.command("resume-run.sh", source, "--new-id", "bypass", *suffix)
                self.assertEqual(p.returncode, 6, p.stderr)
                self.assertEqual(json.loads(p.stdout)["error"], "restart_approval_required")
        self.assertEqual(Path(self.env["FAKE_ARGS"] + ".calls").read_bytes(), before)
        self.assertFalse((self.runs / "bypass").exists())
        approved = self.command("resume-run.sh", original, "--allow-extra-restart")
        self.assertEqual(approved.returncode, 1, approved.stderr)
        ledger = json.loads((original / "lineage.json").read_text())
        self.assertEqual(len(ledger["attempts"]), 3)
        self.assertTrue(ledger["attempts"][-1]["extra_approved"])
        self.assertEqual(json.loads((Path(json.loads(approved.stdout)["run_dir"]) / "lifecycle.json").read_text())["root_id"], original.name)
        denied = self.command("resume-run.sh", original)
        self.assertEqual(json.loads(denied.stdout)["error"], "restart_approval_required")

    def test_native_rejection_fallback_cannot_exceed_cap(self):
        m = self.native_run()
        p = self.command("resume-run.sh", m["run_dir"], "--native")
        self.assertEqual(p.returncode, 1, p.stderr)
        second = json.loads(p.stdout)["run_dir"]
        self.env["FAKE_NATIVE_REJECT"] = "Session file does not exist"
        p = self.command("resume-run.sh", second, "--native")
        self.assertEqual(p.returncode, 6, p.stderr)
        self.assertEqual(json.loads(p.stdout)["error"], "restart_approval_required")
        self.assertTrue(Path(m["run_dir"] + "-r3/meta.json").is_file())
        self.assertFalse(Path(m["run_dir"] + "-r4").exists())
        self.assertEqual(len(Path(self.env["FAKE_ARGS"] + ".calls").read_text().splitlines()), 3)

    def test_atomic_reservations_across_processes_and_original_id(self):
        m = self.failed()
        code = """
import json, sys
sys.path.insert(0, sys.argv[1])
from run_lifecycle import reserve, LifecycleError
try:
    print(json.dumps(reserve(sys.argv[2], sys.argv[3])))
except LifecycleError as error:
    print(json.dumps({'error': str(error)}))
"""
        processes = [subprocess.Popen(["python3", "-c", code, str(fixtures.SCRIPTS), m["run_dir"], f"race-{i}"],
                                      env=self.env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for i in range(6)]
        outcomes = []
        for proc in processes:
            out, err = proc.communicate(timeout=5)
            self.assertEqual(proc.returncode, 0, err)
            outcomes.append(json.loads(out))
        self.assertEqual(sum("error" not in value for value in outcomes), 2)
        self.assertEqual(sum(value.get("error") == "restart_approval_required" for value in outcomes), 4)
        self.assertEqual(len(json.loads((Path(m["run_dir"]) / "lineage.json").read_text())["attempts"]), 2)

    def test_initial_launch_with_restart_style_id_keeps_own_root(self):
        original = Path(self.failed()["run_dir"])
        initial_id = original.name + "-r2"
        p = self.command("run-agent.sh", "--agent", "pi", "--id", initial_id, "--prompt", "test")
        self.assertEqual(p.returncode, 1, p.stderr)
        receipt = json.loads((self.runs / initial_id / "lifecycle.json").read_text())
        self.assertEqual((receipt["root_id"], receipt["attempt"]), (initial_id, 1))
        for source in (original, self.runs / initial_id):
            planned = self.lifecycle_command("plan", source, "fresh-id")
            self.assertEqual(planned.returncode, 0, planned.stderr)
            info = json.loads(planned.stdout)
            self.assertEqual((info["root_id"], info["attempt"]), (source.name, 2))

    def test_custom_id_reservation_is_exclusive_across_lineage_roots(self):
        sources = [Path(self.failed()["run_dir"]) for _ in range(2)]
        processes = [subprocess.Popen(["python3", str(fixtures.SCRIPTS / "run_lifecycle.py"),
                                       "reserve", str(source), "shared-custom", "0"], env=self.env,
                                      stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for source in sources]
        outcomes = []
        for proc in processes:
            out, err = proc.communicate(timeout=10)
            self.assertIn(proc.returncode, (0, 6), err)
            outcomes.append(json.loads(out))
        self.assertEqual(sum(value.get("id") == "shared-custom" for value in outcomes), 1)
        self.assertEqual(sum(value.get("error") == "run_id_exists" for value in outcomes), 1)
        self.assertFalse((self.runs / "shared-custom").exists())
        for source in sources:
            p = self.lifecycle_command("plan", source, "next-unique")
            self.assertEqual(p.returncode, 0, p.stderr)

    def lifecycle_command(self, action, source, new_id="", allow=False):
        return subprocess.run(["python3", str(fixtures.SCRIPTS / "run_lifecycle.py"), action,
                               str(source), new_id, "1" if allow else "0"],
                              env=self.env, text=True, capture_output=True, timeout=10)

    def test_interrupted_custom_reservations_keep_root_after_forward_pointer_overwrite(self):
        for name in ("python3", "mkdir"):
            tool = Path(self.env["HOME"]) / "bin" / name
            tool.write_text(INTERRUPT_TOOL)
            tool.chmod(0o700)
        for stage in ("reserved", "allocated", "command-metadata", "lifecycle", "launch"):
            with self.subTest(stage=stage):
                original = Path(self.failed()["run_dir"])
                child = self.runs / f"interrupted-{stage}"
                ready = self.root / f"ready-{stage}"
                self.env.update(INTERRUPT_STAGE=stage, INTERRUPT_TARGET=str(child), INTERRUPT_READY=str(ready))
                active = self.background("bash", fixtures.SCRIPTS / "resume-run.sh", original,
                                         "--new-id", child.name)
                def stop_group(pid=active.pid):
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pid, signal.SIGKILL)
                try:
                    self.wait_file(ready)
                    ledger = json.loads((original / "lineage.json").read_text())
                    self.assertEqual([a["id"] for a in ledger["attempts"]], [child.name])
                    self.assertEqual(child.exists(), stage != "reserved")
                    self.assertEqual((child / "lifecycle.json").exists(), stage in ("lifecycle", "launch"))
                    self.assertEqual((child / "launch.json").exists(), stage == "launch")
                    if stage in ("command-metadata", "lifecycle", "launch"):
                        self.assertTrue((child / "cmd.txt").is_file())
                        self.assertTrue((child / "workdir.path").is_file())
                finally:
                    stop_group()
                    active.wait(timeout=5)
                    for key in ("INTERRUPT_STAGE", "INTERRUPT_TARGET", "INTERRUPT_READY"):
                        self.env.pop(key, None)
                # No receipt/directory is needed to own the custom ID globally.
                unrelated = Path(self.failed()["run_dir"])
                collision = self.lifecycle_command("plan", unrelated, child.name)
                self.assertEqual(json.loads(collision.stdout)["error"], "run_id_exists")
                later = self.command("resume-run.sh", original, "--new-id", f"later-{stage}")
                self.assertEqual(later.returncode, 1, later.stderr)
                self.assertEqual(json.loads((original / "resumed.json").read_text())["new_id"], f"later-{stage}")
                calls = Path(self.env["FAKE_ARGS"] + ".calls").read_bytes()
                for source in (original, self.runs / f"later-{stage}", *([child] if child.exists() else [])):
                    denied = self.lifecycle_command("plan", source, "cap-bypass")
                    self.assertEqual(denied.returncode, 6, denied.stderr)
                    self.assertEqual(json.loads(denied.stdout)["error"], "restart_approval_required")
                    if (source / "prompt.md").exists():
                        for flags in ((), ("--dry-run",)):
                            denied = self.command("resume-run.sh", source, "--new-id", "cap-bypass", *flags)
                            self.assertEqual(denied.returncode, 6, denied.stderr)
                            self.assertEqual(json.loads(denied.stdout)["error"], "restart_approval_required")
                self.assertEqual(Path(self.env["FAKE_ARGS"] + ".calls").read_bytes(), calls)
                # Approval remains charged to the original ledger even when the
                # selected child has no lifecycle receipt or command metadata.
                source = child if child.exists() else original
                if (source / "prompt.md").exists():
                    approved = self.command("resume-run.sh", source, "--new-id", f"approved-{stage}",
                                            "--allow-extra-restart")
                    self.assertEqual(approved.returncode, 1, approved.stderr)
                    receipt = json.loads((self.runs / f"approved-{stage}" / "lifecycle.json").read_text())
                else:
                    approved = self.lifecycle_command("reserve", source, f"approved-{stage}", allow=True)
                    self.assertEqual(approved.returncode, 0, approved.stderr)
                    receipt = json.loads(approved.stdout)
                self.assertEqual((receipt["root_id"], receipt["attempt"]), (original.name, 4))
                self.assertTrue(receipt["extra_approved"])
                self.assertEqual(len(json.loads((original / "lineage.json").read_text())["attempts"]), 3)
                denied = self.lifecycle_command("plan", source, "still-capped")
                self.assertEqual(json.loads(denied.stdout)["error"], "restart_approval_required")
                self.assertFalse((child / "lineage.json").exists())

    def test_conflicting_reservations_and_receipts_fail_closed(self):
        original = Path(self.failed()["run_dir"])
        unrelated = Path(self.failed()["run_dir"])
        child = self.runs / "pending-child"
        reserved = self.lifecycle_command("reserve", original, child.name)
        self.assertEqual(reserved.returncode, 0, reserved.stderr)
        child.mkdir()
        (child / "prompt.md").write_text("test")
        receipt = dict(json.loads(reserved.stdout), schema=1)
        for evidence in ("duplicate-owner", "initial-owner", "wrong-attempt", "wrong-parent", "legacy-owner", "approval-mismatch"):
            with self.subTest(evidence=evidence):
                if evidence == "duplicate-owner":
                    other = dict(receipt, root_id=unrelated.name, parent_id=unrelated.name)
                    (unrelated / "lineage.json").write_text(json.dumps(
                        {"schema": 1, "root_id": unrelated.name, "attempts": [other]}))
                elif evidence == "legacy-owner":
                    (unrelated / "resumed.json").write_text(json.dumps({"new_id": child.name}))
                else:
                    changed = dict(receipt)
                    if evidence == "initial-owner":
                        changed.update(root_id=child.name, attempt=1)
                        changed.pop("parent_id")
                    elif evidence == "wrong-attempt":
                        changed["attempt"] = 3
                    elif evidence == "approval-mismatch":
                        changed["extra_approved"] = True
                    else:
                        changed["parent_id"] = unrelated.name
                    (child / "lifecycle.json").write_text(json.dumps(changed))
                for source in (original, unrelated, child):
                    denied = self.lifecycle_command("plan", source, "must-not-launch", allow=True)
                    self.assertEqual(denied.returncode, 6, denied.stderr)
                    self.assertEqual(json.loads(denied.stdout)["error"], "invalid_run_lineage")
                for path in (child / "lifecycle.json", unrelated / "lineage.json", unrelated / "resumed.json"):
                    path.unlink(missing_ok=True)
        # Normal initial launches may not claim an already reserved custom ID.
        (child / "prompt.md").unlink()
        child.rmdir()
        before = Path(self.env["FAKE_ARGS"] + ".calls").read_bytes()
        p = self.command("run-agent.sh", "--agent", "pi", "--id", child.name, "--prompt", "test")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["error"], "lifecycle_unavailable")
        self.assertEqual(Path(self.env["FAKE_ARGS"] + ".calls").read_bytes(), before)
        self.assertFalse((child / "lifecycle.json").exists())

    def test_corrupt_ledger_evidence_cannot_reset_cap(self):
        original = Path(self.failed()["run_dir"])
        reserved = self.lifecycle_command("reserve", original, "custom")
        self.assertEqual(reserved.returncode, 0, reserved.stderr)
        ledger_path = original / "lineage.json"
        good = json.loads(ledger_path.read_text())
        corruptions = ["{broken", "{}", json.dumps(dict(good, root_id="not-the-owner")),
                       json.dumps(dict(good, attempts="not-attempts")),
                       json.dumps(dict(good, attempts=good["attempts"] * 2))]
        for key, value in (("id", "../invalid"), ("parent_id", ["unhashable"]),
                           ("parent_id", "unknown-parent"), ("attempt", 3), ("attempt", True)):
            attempt = dict(good["attempts"][0])
            attempt[key] = value
            corruptions.append(json.dumps(dict(good, attempts=[attempt])))
        before = Path(self.env["FAKE_ARGS"] + ".calls").read_bytes()
        for corrupted in corruptions:
            with self.subTest(corrupted=corrupted):
                ledger_path.write_text(corrupted)
                p = self.command("resume-run.sh", original, "--allow-extra-restart")
                self.assertEqual(p.returncode, 6, p.stderr)
                self.assertIn(json.loads(p.stdout)["error"], ("invalid_run_metadata", "invalid_run_lineage"))
                self.assertEqual(ledger_path.read_text(), corrupted)
        self.assertEqual(Path(self.env["FAKE_ARGS"] + ".calls").read_bytes(), before)
        self.assertFalse((self.runs / "custom").exists())

    def test_timeout_and_inputs_survive_config_change_and_native_chain(self):
        source = self.root / "readable-input"
        source.mkdir()
        (source / "value").write_text(SECRET)
        m = self.native_run()
        # Start a separate run with explicitly non-default original inputs.
        m = self.run_agent("pi", self.session_events("pi"), extra=("--timeout", "7", "--input", source, "--permission", "safe"))
        config = Path(self.env["HOME"]) / ".grok-bot-agents/config.json"
        config.parent.mkdir(exist_ok=True)
        config.write_text(json.dumps({"enabled": ["pi"], "timeout_sec": 1, "permission_mode": "full-auto"}))
        for _ in range(2):
            p = self.command("resume-run.sh", m["run_dir"], "--native")
            self.assertEqual(p.returncode, 1, p.stderr)
            m = json.loads(p.stdout)
            launch = json.loads((Path(m["run_dir"]) / "launch.json").read_text())
            self.assertEqual(launch["timeout_sec"], 7)
            self.assertEqual(launch["permission"], "safe")
            self.assertIn(str(source), launch["inputs"])
            self.assertEqual((Path(m["run_dir"]) / "launch.json").stat().st_mode & 0o077, 0)
        self.assertEqual(self.call("next", Path(m["run_dir"]).name)["next_action"], "request_approval")

    def test_private_launch_metadata_recovers_without_meta_or_command_log(self):
        m = self.failed(extra=("--timeout", "11", "--model", "test-model", "--reasoning", "low", "--permission", "safe"))
        run = Path(m["run_dir"])
        (run / "meta.json").unlink()
        (run / "cmd.txt").unlink()
        p = self.command("resume-run.sh", run, "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("--timeout 11", p.stdout)
        self.assertIn("--model test-model", p.stdout)
        self.assertIn("--reasoning low", p.stdout)
        self.assertIn("--permission safe", p.stdout)
        self.assertEqual(self.call("list")["runs"][0]["agent"], "pi")

    def test_dry_run_is_write_free_at_whole_home_and_runs_root(self):
        m = self.failed(extra=("--timeout", "9"))
        before = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        value = self.call("recover", Path(m["run_dir"]).name, "--dry-run")
        after = {str(p): p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        self.assertEqual(value["reason"], "recovery_dry_run")
        self.assertFalse(value["mutated"])
        self.assertEqual(before, after)
        self.assertFalse((Path(m["run_dir"]) / ".lineage.lock").exists())

    def test_lifecycle_json_filters_secret_fields_and_helper_output(self):
        self.env.update(FAKE_STDERR=SECRET)
        m = self.run_agent("pi", [fixtures.message(SECRET, "error", "Request timed out " + SECRET)], stderr=SECRET)
        run = Path(m["run_dir"])
        value = self.call("list", "--open", "--json")
        self.assertEqual(value["runs"][0]["id"], run.name)
        self.assertEqual(self.call("next", run.name)["next_action"], "recover")
        self.next_output()
        Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(fixtures.message(SECRET)) + "\n")
        self.env["FAKE_STDERR"] = SECRET
        recovered = self.call("recover", run.name)
        self.assertEqual(recovered["reason"], "recovery_complete")
        self.assertEqual(recovered["next_action"], "review_result")
        # Root-ID lookup follows the continuation, not its old transport error.
        decision = self.call("next", run.name)
        self.assertEqual(decision["next_action"], "review_result")
        self.assertEqual(decision["id"], recovered["id"])
        self.assertEqual(decision["requested_id"], run.name)
        listing = self.call("list", "--open")
        self.assertNotIn(recovered["id"], [item["id"] for item in listing["runs"]])

    def test_bad_metadata_and_missing_ids_are_stable_and_secret_free(self):
        m = self.failed()
        run = Path(m["run_dir"])
        (run / "meta.json").write_text(SECRET + "{broken")
        self.assertEqual(self.call("list")["reason"], "run_state_unavailable")
        self.assertEqual(self.call("next", "../" + SECRET)["reason"], "run_not_found")
        self.assertEqual(self.call("recover", SECRET)["reason"], "run_not_found")

    def test_recover_guard_and_cli_options_do_not_launch_live_calls(self):
        self.env.update(FAKE_DELAY="60", FAKE_SESSION_ID="session-123")
        Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(fixtures.message("", "error", "Request timed out")) + "\n")
        active = self.background("bash", fixtures.SCRIPTS / "run-agent.sh", "--agent", "pi", "--id", "live",
                                 "--prompt", SECRET, "--timeout", "90")
        self.wait_file(self.runs / "live/agent.pid")
        before = Path(self.env["FAKE_ARGS"] + ".calls")
        self.wait_file(before)
        calls = before.read_bytes()
        decision = self.call("next", "live")
        self.assertEqual(decision["next_action"], "supervise")
        refused = self.call("recover", "live", "--native")
        self.assertEqual(refused["reason"], "run_owned")
        self.assertIsNone(active.poll())
        self.assertEqual(before.read_bytes(), calls)
        self.next_output()
        recovered = self.call("recover", "live", "--native", "--kill")
        self.assertEqual(recovered["reason"], "recovery_complete")
        active.wait(timeout=5)
        self.assertEqual(recovered["timeout_sec"], 90)

    def test_handoff_persists_pending_active_and_rotation_without_waiting(self):
        run = self.runs / "pending"
        run.mkdir(parents=True)
        (run / "prompt.md").write_text(SECRET)
        (run / "runner.pid").write_text(str(os.getpid()))
        decision = self.call("next", run.name, "--handoff")
        self.assertEqual(decision["next_action"], "dispatch_supervisor")
        self.assertIn("--new", decision["supervisor_command"])
        self.assertEqual(self.call("next", run.name)["next_action"], "dispatch_supervisor")
        active = self.call("next", run.name, "--supervisor-started")
        self.assertEqual(active["next_action"], "supervise")
        self.assertNotIn("--new", active["supervisor_command"])
        (run / "supervise.deadline").write_text("1")
        expired = self.call("next", run.name)
        self.assertEqual(expired["next_action"], "rotate_supervisor")
        rotated = self.call("next", run.name, "--rotate")
        self.assertEqual(rotated["next_action"], "dispatch_supervisor")
        self.assertEqual(rotated["supervisor_generation"], 2)
        receipt = json.loads((run / "supervisor.handoff.json").read_text())
        self.assertEqual(set(receipt), {"schema", "state", "generation", "updated_at"})
        self.assertEqual(receipt["state"], "pending")
        self.assertEqual((run / "supervisor.handoff.json").stat().st_mode & 0o077, 0)

    def test_next_cap_and_recover_override_are_explicit(self):
        m = self.failed()
        run = Path(m["run_dir"])
        for _ in range(2):
            value = self.call("recover", run.name)
            self.assertEqual(value["reason"], "recovery_incomplete")
        capped = self.call("next", run.name)
        self.assertEqual(capped["next_action"], "request_approval")
        self.assertTrue(capped["requires_approval"])
        self.assertEqual(self.call("recover", run.name)["reason"], "restart_approval_required")
        dry = self.call("recover", run.name, "--allow-extra-restart", "--dry-run")
        self.assertEqual(dry["attempt"], 4)
        self.next_output()
        override = self.call("recover", run.name, "--allow-extra-restart")
        self.assertEqual(override["reason"], "recovery_complete")
        self.assertEqual(override["restarts"], 3)

    def test_legacy_suffix_and_custom_receipts_import_existing_attempts(self):
        origin = self.runs / "legacy"
        origin.mkdir(parents=True)
        for name in ("legacy-r2", "custom"):
            child = self.runs / name
            child.mkdir()
            (child / "prompt.md").write_text("test")
        (origin / "resumed.json").write_text(json.dumps({"resumed_from": "legacy", "new_id": "custom"}))
        (origin / "prompt.md").write_text("test")
        p = self.command("resume-run.sh", self.runs / "custom", "--dry-run")
        self.assertEqual(p.returncode, 6, p.stderr)
        self.assertEqual(json.loads(p.stdout)["error"], "restart_approval_required")

    def test_reserved_id_collision_never_follows_a_different_lineage(self):
        original = Path(self.failed()["run_dir"])
        unrelated = Path(self.failed()["run_dir"])
        # Simulate a cross-lineage destination mkdir race: reservation was made
        # but an unrelated initial launch won exclusive directory allocation.
        (original / "lineage.json").write_text(json.dumps({"schema": 1, "root_id": original.name,
            "attempts": [{"id": unrelated.name, "parent_id": original.name, "attempt": 2}]}))
        value = self.call("next", original.name)
        # The lifecycle reader now rejects conflicting evidence before a head
        # can be selected; the controller sanitizes that exception generically.
        self.assertEqual(value["reason"], "run_state_unavailable")
        self.assertIsNone(value["next_action"])

    def test_empty_inventory_does_not_create_runs_root(self):
        self.assertEqual(self.call("list", "--open")["runs"], [])
        self.assertFalse(self.runs.exists())

    def test_direct_internal_native_entry_requires_reservation(self):
        m = self.native_run()
        p = self.command("run-agent.sh", "--agent", "pi", "--id", "direct", "--prompt", "test",
                         "--cwd", m["workdir"], "--resume", m["session_file"], "--resume-in-place")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual(json.loads(p.stdout)["error"], "resume_requires_recovery")
        self.assertEqual(len(Path(self.env["FAKE_ARGS"] + ".calls").read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
