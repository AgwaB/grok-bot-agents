#!/usr/bin/env python3
"""Offline delegate-task regressions: fake CLIs, clean environment, temporary HOME only."""
import json
import os
import signal
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delegate-task/scripts"
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, subprocess, sys, time
if "--help" in sys.argv:
    print("--approve")
    sys.exit(0)
pathlib.Path(os.environ["FAKE_ARGS"]).write_text(json.dumps(sys.argv[1:]))
if os.environ.get("FAKE_TOUCH"):
    pathlib.Path("saved.txt").write_text("keep this work")
if os.environ.get("FAKE_COMMIT"):
    subprocess.run(["git", "add", "saved.txt"], check=True)
    subprocess.run(["git", "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "saved work"], check=True)
if "-o" in sys.argv and "FAKE_RESULT" in os.environ:
    pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(os.environ["FAKE_RESULT"])
print(pathlib.Path(os.environ["FAKE_OUTPUT"]).read_text(), end="", flush=True)
time.sleep(float(os.environ.get("FAKE_DELAY", "0")))
sys.stderr.write(os.environ.get("FAKE_STDERR", ""))
sys.exit(int(os.environ.get("FAKE_RC", "0")))
'''


def message(text="done", stop="stop", error=None):
    return {"type": "message_end", "message": {"role": "assistant", "stopReason": stop,
            "errorMessage": error, "content": [{"type": "text", "text": text}]}}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="delegate-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        home = self.root / "home"
        bin_dir = home / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("pi", "omp", "claude", "codex"):
            cli = bin_dir / name
            cli.write_text(FAKE_CLI)
            cli.chmod(0o700)
        npm = bin_dir / "npm"
        npm.write_text("#!/bin/sh\nexit 0\n")
        npm.chmod(0o700)
        self.runs = self.root / "runs"
        self.env = {"HOME": str(home), "PATH": f"{bin_dir}:/usr/bin:/bin", "LANG": "C.UTF-8",
                    "AGENT_RUNS_ROOT": str(self.runs), "FAKE_ARGS": str(self.root / "args.json"),
                    "FAKE_OUTPUT": str(self.root / "events.jsonl"),
                    "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}
        self.count = 0

    def command(self, script, *args, **kwargs):
        return subprocess.run(["bash", str(SCRIPTS / script), *map(str, args)], env=self.env,
                              text=True, capture_output=True, timeout=20, **kwargs)

    def run_agent(self, agent, events, rc=0, stderr="", result=None, extra=()):
        self.count += 1
        run_id = f"run-{self.count}"
        raw = events if isinstance(events, str) else "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
        Path(self.env["FAKE_OUTPUT"]).write_text(raw)
        self.env.update(FAKE_RC=str(rc), FAKE_STDERR=stderr)
        self.env.pop("FAKE_RESULT", None)
        if result is not None:
            self.env["FAKE_RESULT"] = result
        proc = self.command("run-agent.sh", "--agent", agent, "--id", run_id, "--prompt", "test", *extra)
        meta = json.loads(proc.stdout)
        self.assertEqual(proc.returncode, 0 if meta["status"] == "ok" else 1, proc.stderr)
        self.assertEqual(json.loads((self.runs / run_id / "meta.json").read_text())["status"], meta["status"])
        return meta

    def test_omp_connection_errors_exit_zero(self):
        for error in ("The socket connection was closed unexpectedly", "connection was closed", "closed unexpectedly",
                      "Connection aborted", "timed out", "Request timed out", "error decoding response body",
                      "error sending request", "WebSocket error", "WebSocket idle timeout after 300000ms", "terminated"):
            with self.subTest(error=error):
                m = self.run_agent("omp", [message("partial text about quota", "error", error)])
                self.assertEqual((m["status"], m["error_kind"], m["resumable"], m["exit_code"]),
                                 ("error", "conn_error", True, 0))
                self.assertIn(error, m["summary"])

    def test_codex_terminal_failure_overrides_output_file(self):
        events = [{"type": "thread.started", "thread_id": "thread-1"},
                  {"type": "item.completed", "item": {"type": "agent_message", "text": "partial"}},
                  {"type": "turn.failed", "error": {"message": "error sending request"}}]
        for rc in (0, 1):
            with self.subTest(rc=rc):
                m = self.run_agent("codex", events, rc=rc, result="stale partial answer")
                self.assertEqual((m["status"], m["error_kind"], m["exit_code"], m["session_id"]),
                                 ("error", "conn_error", rc, "thread-1"))
                self.assertIn("error sending request", m["summary"])
                self.assertEqual(Path(m["result_file"]).read_text(), m["summary"])

    def test_codex_completed_ignores_transient_errors(self):
        m = self.run_agent("codex", [{"type": "error", "message": "Reconnecting: error sending request"},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "done"}},
            {"type": "turn.completed", "usage": {}}], stderr="WebSocket failure, retrying", result="")
        self.assertEqual((m["status"], m["summary"]), ("ok", "done"))

    def test_claude_result_is_error_even_when_subtype_success(self):
        for stream in (True, False):
            terminal = {"type": "result", "subtype": "success", "is_error": True,
                        "result": "API Error: Connection dropped (ECONNRESET)", "session_id": "c1"}
            raw = [{"type": "system", "subtype": "init", "session_id": "c1"}, terminal] if stream else json.dumps(terminal, indent=2)
            with self.subTest(stream=stream):
                m = self.run_agent("claude", raw)
                self.assertEqual((m["status"], m["error_kind"], m["session_id"], m["exit_code"]),
                                 ("error", "conn_error", "c1", 0))

    def test_claude_stream_flags_and_failure_errors_array(self):
        m = self.run_agent("claude", [{"type": "result", "subtype": "error_during_execution",
                                      "is_error": True, "errors": ["Request timed out"]}])
        args = json.loads(Path(self.env["FAKE_ARGS"]).read_text())
        self.assertEqual(args[args.index("--output-format") + 1], "stream-json")
        self.assertIn("--verbose", args)
        self.assertIn("--include-partial-messages", args)
        self.assertEqual(m["error_kind"], "conn_error")

    def test_claude_success_requires_explicit_terminal_verdict(self):
        m = self.run_agent("claude", [{"type": "assistant", "message": {"content": [{"type": "text", "text": "network error example"}]}},
                                    {"type": "result", "subtype": "success", "is_error": False, "result": "done"}])
        self.assertEqual((m["status"], m["summary"]), ("ok", "done"))
        m = self.run_agent("claude", [{"type": "result", "result": "partial"}])
        self.assertEqual(m["status"], "error")

    def test_pi_like_truncation_abort_and_retry_exhaustion_fail(self):
        for agent in ("pi", "omp"):
            for stop in ("aborted", "length"):
                with self.subTest(agent=agent, stop=stop):
                    m = self.run_agent(agent, [message("partial", stop)])
                    self.assertEqual((m["status"], m["resumable"]), ("error", False))
            m = self.run_agent(agent, [message("partial", "error", "Request timed out"),
                                      {"type": "auto_retry_end", "success": False, "finalError": "Connection aborted"}])
            self.assertEqual(m["error_kind"], "conn_error")
            self.assertIn("Connection aborted", m["summary"])

    def test_pi_like_warning_on_stop_is_not_failure(self):
        for agent in ("pi", "omp"):
            with self.subTest(agent=agent):
                m = self.run_agent(agent, [message("done", "stop", "Falling back to another credential")])
                self.assertEqual((m["status"], m["summary"], m["error_kind"]), ("ok", "done", None))

    def test_tool_json_cannot_spoof_completion(self):
        for agent in ("pi", "omp"):
            m = self.run_agent(agent, [{"type": "message_end", "message": {"role": "toolResult", "content": [message()]}}])
            self.assertEqual(m["error_kind"], "incomplete_output")

    def test_timeout_beats_even_a_success_event(self):
        self.env["FAKE_DELAY"] = "5"
        m = self.run_agent("pi", [message()], extra=("--timeout", "1"))
        self.assertEqual((m["status"], m["exit_code"]), ("timeout", 124))

    def test_last_pi_like_message_wins_not_text_prefix(self):
        for agent in ("pi", "omp"):
            for text in ("done", "error: this is an example, not a failure", ""):
                with self.subTest(agent=agent, text=text):
                    m = self.run_agent(agent, [message("part", "error", "WebSocket error"), message(text)])
                    self.assertEqual((m["status"], m["summary"], m["last_stop_reason"]), ("ok", text, "stop"))

    def test_missing_terminal_is_not_success(self):
        samples = {"codex": [{"type": "thread.started", "thread_id": "t"}],
                   "claude": [{"type": "assistant", "message": {"content": [{"type": "text", "text": "partial"}]}}],
                   "pi": [message("tool call", "toolUse")], "omp": []}
        for agent, events in samples.items():
            with self.subTest(agent=agent):
                m = self.run_agent(agent, events)
                self.assertEqual((m["status"], m["error_kind"], m["resumable"]), ("error", "incomplete_output", False))
                self.assertTrue(m["summary"])

    def test_new_turn_invalidates_old_success(self):
        for agent, events in {
            "codex": [{"type": "turn.completed"}, {"type": "turn.started"}],
            "pi": [message("done"), {"type": "message_start", "message": {"role": "assistant"}}],
            "omp": [message("done"), {"type": "auto_retry_start"}],
        }.items():
            with self.subTest(agent=agent):
                self.assertEqual(self.run_agent(agent, events)["error_kind"], "incomplete_output")

    def test_jsonl_unicode_and_malformed_noise(self):
        raw = 'garbage\n{broken\nnull\n[]\n' + json.dumps(message("a\u2028b\u0085c"), ensure_ascii=False) + '\n'
        m = self.run_agent("pi", raw)
        self.assertEqual((m["status"], m["summary"]), ("ok", "a\u2028b\u0085c"))

    def test_auth_quota_and_timeout_are_not_transport_retries(self):
        m = self.run_agent("omp", [message("quota in partial", "error", "Invalid API key")], stderr="Earlier WebSocket error")
        self.assertEqual((m["status"], m["resumable"]), ("error", False))
        m = self.run_agent("codex", [{"type": "turn.failed", "error": {"message": "429 Too many requests"}}])
        self.assertEqual((m["status"], m["resumable"]), ("rate_limited", False))
        m = self.run_agent("pi", [message("", "error", "Request timed out")], rc=124)
        self.assertEqual((m["status"], m["resumable"]), ("timeout", False))

    def test_auth_and_cancellation_do_not_resume_old_transport_errors(self):
        for error in ("WebSocket error: 401 Unauthorized", "403 Forbidden: error sending request",
                      "Invalid API key: Request timed out"):
            with self.subTest(error=error):
                m = self.run_agent("omp", [message("", "error", error)])
                self.assertEqual((m["status"], m["resumable"]), ("error", False))
        for rc in (130, 143):
            m = self.run_agent("codex", [], rc=rc, stderr="WebSocket error before cancellation")
            self.assertFalse(m["resumable"])
        m = self.run_agent("pi", [message("", "aborted", "Request timed out")])
        self.assertFalse(m["resumable"])

    def test_nonzero_exit_is_never_hidden_by_success_event(self):
        m = self.run_agent("pi", [message()], rc=7)
        self.assertEqual((m["status"], m["exit_code"]), ("error", 7))

    def test_no_output_error_uses_stderr(self):
        m = self.run_agent("codex", [], rc=1, stderr="error sending request")
        self.assertEqual(m["error_kind"], "conn_error")
        self.assertIn("error sending request", m["summary"])

    def git(self, repo, *args):
        return subprocess.run(["git", "-C", str(repo), *args], env=self.env, text=True,
                              capture_output=True, check=True, timeout=10).stdout.strip()

    def repo(self):
        repo = self.root / "repo"
        repo.mkdir()
        self.git(repo, "init", "-q")
        self.git(repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "init", "--allow-empty")
        return repo

    def test_checkpoint_resume_preserves_wip_in_new_worktree(self):
        repo = self.repo()
        self.env["FAKE_TOUCH"] = "1"
        m = self.run_agent("omp", [message("", "error", "Request timed out")], extra=("--cwd", repo, "--permission", "safe"))
        r = Path(m["run_dir"])
        self.env.pop("FAKE_TOUCH")
        Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(message("resumed")) + "\n")
        p = self.command("resume-run.sh", r)
        self.assertEqual(p.returncode, 0, p.stderr)
        resumed = json.loads(p.stdout)
        self.assertEqual(resumed["permission"], "safe")
        self.assertEqual((Path(resumed["workdir"]) / "saved.txt").read_text(), "keep this work")
        self.assertFalse((repo / "saved.txt").exists())
        self.assertIn("checkpoint before resume", self.git(r / "wt", "log", "-1", "--format=%s"))
        self.assertEqual(self.git(r / "wt", "status", "--porcelain"), "")

    def test_rate_limit_cleanup_preserves_agent_commits(self):
        repo = self.repo()
        self.env.update(FAKE_TOUCH="1", FAKE_COMMIT="1")
        m = self.run_agent("omp", [message("", "error", "429 Too many requests")], extra=("--cwd", repo))
        self.assertEqual(m["status"], "rate_limited")
        self.assertTrue((Path(m["workdir"]) / "saved.txt").exists())
        self.git(repo, "rev-parse", "--verify", m["worktree_branch"])

    def test_rate_limit_cleanup_preserves_ignored_artifacts(self):
        repo = self.repo()
        (repo / ".gitignore").write_text("saved.txt\n")
        self.git(repo, "add", ".gitignore")
        self.git(repo, "-c", "user.name=test", "-c", "user.email=test@example.invalid", "commit", "-qm", "ignore")
        self.env["FAKE_TOUCH"] = "1"
        m = self.run_agent("omp", [message("", "error", "429 Too many requests")], extra=("--cwd", repo))
        self.assertTrue((Path(m["workdir"]) / "saved.txt").exists())

    def test_empty_rate_limited_worktree_is_cleaned(self):
        repo = self.repo()
        m = self.run_agent("omp", [message("", "error", "429 Too many requests")], extra=("--cwd", repo))
        self.assertEqual(m["status"], "rate_limited")
        self.assertFalse(Path(m["workdir"]).exists())

    def test_run_ids_are_exclusive_private_and_cannot_escape(self):
        m = self.run_agent("pi", [message()])
        r = Path(m["run_dir"])
        before = (r / "prompt.md").read_bytes()
        p = self.command("run-agent.sh", "--agent", "pi", "--id", r.name, "--prompt", "overwrite")
        self.assertNotEqual(p.returncode, 0)
        self.assertEqual((r / "prompt.md").read_bytes(), before)
        self.assertEqual(r.stat().st_mode & 0o077, 0)
        p = self.command("run-agent.sh", "--agent", "pi", "--id", "../escaped", "--prompt", "test")
        self.assertNotEqual(p.returncode, 0)
        self.assertFalse((self.root / "escaped").exists())

    def test_resume_preserves_safe_permission_even_without_meta(self):
        for agent in ("codex", "claude", "omp"):
            for keep_meta in (True, False):
                with self.subTest(agent=agent, keep_meta=keep_meta):
                    events = [message("", "error", "Request timed out")] if agent == "omp" else (
                        [{"type": "turn.failed", "error": {"message": "Request timed out"}}] if agent == "codex" else
                        [{"type": "result", "subtype": "success", "is_error": True, "result": "Request timed out"}])
                    m = self.run_agent(agent, events, extra=("--permission", "safe"))
                    r = Path(m["run_dir"])
                    if not keep_meta:
                        (r / "meta.json").unlink()
                    p = self.command("resume-run.sh", r, "--dry-run")
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertIn("--permission safe", p.stdout)

    def test_resume_dry_run_does_not_write_or_remove_files(self):
        m = self.run_agent("pi", [message("", "error", "Request timed out")])
        r = Path(m["run_dir"])
        sentinel = r / "continue-next.md.tmp"
        sentinel.write_text("do not overwrite")
        before = {str(p.relative_to(r)): p.read_bytes() for p in r.rglob("*") if p.is_file()}
        p = self.command("resume-run.sh", r, "--new-id", "next", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        after = {str(p.relative_to(r)): p.read_bytes() for p in r.rglob("*") if p.is_file()}
        self.assertEqual(before, after)

    def test_resume_absolute_run_keeps_its_root(self):
        m = self.run_agent("omp", [message("", "error", "Request timed out")], extra=("--permission", "safe"))
        r = Path(m["run_dir"])
        self.env["AGENT_RUNS_ROOT"] = str(self.root / "other-runs")
        Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(message("resumed")) + "\n")
        p = self.command("resume-run.sh", r)
        self.assertEqual(p.returncode, 0, p.stderr)
        resumed = json.loads(p.stdout)
        self.assertEqual(resumed["run_dir"], str(self.runs / (r.name + "-r2")))
        self.assertEqual(resumed["permission"], "safe")

    def idle_run(self, agent=None):
        self.count += 1
        r = self.runs / f"idle-{self.count}"
        work = r / "work"
        work.mkdir(parents=True)
        p = subprocess.Popen(["timeout", "30", "sleep", "30"], cwd=work, env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)

        def cleanup():
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=5)
        self.addCleanup(cleanup)
        (r / "agent.pid").write_text(str(p.pid))
        if agent:
            (r / "cmd.txt").write_text(f"{agent} -p '<prompt.md>'\n")
        now = int(time.time())
        for name in ("stdout.log", "stderr.log", "prompt.md"):
            (r / name).write_text("test\n")
            os.utime(r / name, (now - 1200, now - 1200))
        (r / "supervise.samples").write_text(f"{now - 1200} 0 {now - 1200}\n")
        (r / "supervise.lastcheck").write_text(str(now))
        return r, p

    def supervise(self, r, *args):
        return self.command("supervise.sh", r, "60", "--new", "--slice", "0", "--interval", "1", *args)

    def test_recycled_timeout_pid_is_not_owned_by_another_run(self):
        _, p = self.idle_run()
        m = self.run_agent("pi", [message("", "error", "Request timed out")])
        r = Path(m["run_dir"])
        (r / "agent.pid").write_text(str(p.pid))
        proc = self.command("resume-run.sh", r, "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertNotIn("would stop", proc.stderr)
        self.assertIsNone(p.poll())
        (r / "meta.json").unlink()
        os.utime(r / "prompt.md", (time.time() - 100, time.time() - 100))
        proc = self.supervise(r)
        self.assertIn("reason=died_no_meta", proc.stdout)

    def test_silent_claude_codex_and_unknown_are_advisory_by_default(self):
        for agent in ("claude", "codex", None):
            with self.subTest(agent=agent):
                r, p = self.idle_run(agent)
                proc = self.supervise(r)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("status=running next=continue", proc.stdout)
                self.assertIn("reason=idle_suspected", proc.stdout)
                self.assertIn("resumable=0", proc.stdout)
                self.assertIsNone(p.poll())

    def test_pi_omp_stall_defaults_to_restart(self):
        for agent in ("pi", "omp"):
            with self.subTest(agent=agent):
                r, p = self.idle_run(agent)
                proc = self.supervise(r)
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("status=stalled next=return", proc.stdout)
                self.assertIn("resumable=1", proc.stdout)
                self.assertIsNone(p.poll())  # the bot, not the supervisor, performs recovery

    def test_explicit_stall_action_overrides_each_cli_default(self):
        for agent in ("pi", "omp", "claude", "codex"):
            with self.subTest(agent=agent):
                r, _ = self.idle_run(agent)
                action = "report" if agent in ("pi", "omp") else "restart"
                proc = self.supervise(r, "--stall-action", action)
                self.assertEqual(proc.returncode, 0 if action == "report" else 2, proc.stderr)
                self.assertIn("resumable=0" if action == "report" else "resumable=1", proc.stdout)
                self.assertIn("reason=idle_suspected" if action == "report" else "status=stalled", proc.stdout)

    def test_recent_session_activity_prevents_stall(self):
        for agent in ("pi", "omp"):
            with self.subTest(agent=agent):
                r, _ = self.idle_run(agent)
                (r / "sessions").mkdir()
                (r / "sessions" / "owned.jsonl").write_text("{}\n")
                proc = self.supervise(r)
                self.assertIn("status=running", proc.stdout)
                self.assertNotIn("idle_suspected", proc.stdout)

    def test_observation_gap_gives_full_wake_grace_without_dmesg(self):
        r, _ = self.idle_run()
        (r / "supervise.lastcheck").write_text(str(int(time.time()) - 900))
        proc = self.supervise(r, "--stall-action", "restart")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=running", proc.stdout)
        self.assertIn("OBSERVATION_GAP", (r / "supervise.log").read_text())

    def test_cpu_counter_drop_is_not_evidence_of_stall(self):
        r, _ = self.idle_run()
        (r / "supervise.samples").write_text(f"{int(time.time()) - 1200} 100000 0\n")
        proc = self.supervise(r, "--stall-action", "restart")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=running", proc.stdout)


if __name__ == "__main__":
    unittest.main()
