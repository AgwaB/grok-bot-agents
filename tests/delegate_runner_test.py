#!/usr/bin/env python3
"""Offline delegate-task regressions: fake CLIs, clean environment, temporary HOME only."""
import json
import os
import signal
import socket
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
with open(os.environ["FAKE_ARGS"] + ".calls", "a") as f:
    f.write(json.dumps({"args": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
native = "--session" in sys.argv or "--resume" in sys.argv or "resume" in sys.argv
if native and os.environ.get("FAKE_NATIVE_REJECT"):
    sys.stderr.write(os.environ["FAKE_NATIVE_REJECT"])
    sys.exit(2)
if os.environ.get("FAKE_SESSION_ID") and "--session-dir" in sys.argv:
    directory = pathlib.Path(sys.argv[sys.argv.index("--session-dir") + 1])
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / ("timestamp_" + os.environ["FAKE_SESSION_ID"] + ".jsonl")
    if not path.exists():
        records = [{"type": "session", "id": os.environ["FAKE_SESSION_ID"], "cwd": os.getcwd()}]
        if pathlib.Path(sys.argv[0]).name == "omp":
            # Synthetic title/header fixture: only the real record types/keys were inspected.
            records = [json.loads(line) for line in pathlib.Path(os.environ["FAKE_OMP_FIXTURE"]).read_text().splitlines()]
            records[1].update(id=os.environ["FAKE_SESSION_ID"], cwd=os.getcwd())
        path.write_text("".join(json.dumps(record) + "\n" for record in records))
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
                    "FAKE_OMP_FIXTURE": str(Path(__file__).resolve().parent / "fixtures/omp-title-session.jsonl"),
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

    def session_events(self, agent, success=False):
        sid = "session-123"
        if agent == "claude":
            return [{"type": "system", "subtype": "init", "session_id": sid},
                    {"type": "result", "subtype": "success", "is_error": not success,
                     "result": "continued" if success else "Request timed out", "session_id": sid}]
        if agent == "codex":
            return [{"type": "thread.started", "thread_id": sid},
                    {"type": "turn.completed"} if success else
                    {"type": "turn.failed", "error": {"message": "Request timed out"}}]
        return [{"type": "session", "id": sid},
                message("continued" if success else "", "stop" if success else "error",
                        None if success else "Request timed out")]

    def native_run(self, agent="pi", repo=None, permission="safe"):
        self.env["FAKE_SESSION_ID"] = "session-123"
        extra = ("--cwd", repo) if repo else ()
        extra += ("--read-only",) if permission == "read-only" else ("--permission", permission)
        return self.run_agent(agent, self.session_events(agent), extra=extra)

    def next_output(self, agent="pi", success=True):
        Path(self.env["FAKE_OUTPUT"]).write_text("".join(json.dumps(e) + "\n" for e in self.session_events(agent, success)))
        self.env.update(FAKE_RC="0", FAKE_STDERR="", FAKE_DELAY="0")

    def test_native_resume_all_clis_keep_worktree_session_and_permissions(self):
        repo = self.repo()
        for agent in ("claude", "codex", "pi", "omp"):
            with self.subTest(agent=agent):
                self.env["FAKE_TOUCH"] = "1"
                m = self.native_run(agent, repo)
                self.env.pop("FAKE_TOUCH")
                r = Path(m["run_dir"])
                old_prompt = (r / "prompt.md").read_bytes()
                self.assertEqual(m["session_id"], "session-123")
                worktrees = [line for line in self.git(repo, "worktree", "list", "--porcelain").splitlines()
                             if line.startswith(("worktree ", "branch "))]
                self.next_output(agent)
                p = self.command("resume-run.sh", r, "--native")
                self.assertEqual(p.returncode, 0, p.stderr)
                resumed = json.loads(p.stdout)
                self.assertEqual((resumed["workdir"], resumed["worktree_branch"], resumed["permission"],
                                  resumed["session_id"], resumed["resume_mode"]),
                                 (m["workdir"], m["worktree_branch"], "safe", "session-123", "native"))
                self.assertEqual(worktrees, [line for line in self.git(repo, "worktree", "list", "--porcelain").splitlines()
                                             if line.startswith(("worktree ", "branch "))])
                self.assertEqual(self.git(r / "wt", "status", "--porcelain"), "")
                self.assertIn("checkpoint before resume", self.git(r / "wt", "log", "-1", "--format=%s"))
                self.assertEqual((r / "prompt.md").read_bytes(), old_prompt)
                new = Path(resumed["run_dir"])
                self.assertIn("verify its actual state and side effects", (new / "prompt.md").read_text())
                self.assertIn("commit or install", (new / "prompt.md").read_text())
                args = json.loads(Path(self.env["FAKE_ARGS"]).read_text())
                call = json.loads(Path(self.env["FAKE_ARGS"] + ".calls").read_text().splitlines()[-1])
                self.assertEqual(call["cwd"], m["workdir"])
                if agent in ("pi", "omp"):
                    flag = "--session" if agent == "pi" else "--resume"
                    self.assertEqual(args[args.index(flag) + 1], m["session_file"])
                    self.assertEqual(Path(args[args.index("--session-dir") + 1]).resolve(), (r / "sessions").resolve())
                    self.assertEqual(resumed["session_file"], m["session_file"])
                elif agent == "claude":
                    self.assertEqual(args[args.index("--resume") + 1], m["session_id"])
                else:
                    self.assertIn("resume", args)
                    self.assertLess(args.index("-C"), args.index("resume"))
                    self.assertIn("--json", args)
                    self.assertIn("-o", args)
                    self.assertIn('sandbox_mode="workspace-write"', args)
                    self.assertEqual(args[-2], m["session_id"])

    def test_omp_header_discovery_is_bounded_and_skips_non_header_records(self):
        m = self.native_run("omp")
        r, session = Path(m["run_dir"]), Path(m["session_file"])
        records = session.read_text().splitlines()
        self.assertEqual([json.loads(line)["type"] for line in records], ["title", "session"])
        (r / "stdout.log").write_text("truncated\n")
        (r / "meta.json").unlink()
        # Non-object records and malformed noise must not hide a nearby header.
        session.write_text("null\n{broken\n" + "\n".join(records) + "\n")
        p = self.command("resume-run.sh", r, "--native", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("--resume-in-place", p.stdout)
        # Do not search an arbitrary transcript for a header-shaped message.
        session.write_text((records[0] + "\n") * 32 + records[1] + "\n")
        p = self.command("resume-run.sh", r, "--native", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertNotIn("--resume-in-place", p.stdout)
        self.assertIn("missing session id, owned session file or workdir", p.stderr)

    def test_native_read_only_permissions_remain_restricted(self):
        for agent, expected in (("pi", "--tools"), ("omp", "--tools"),
                                ("claude", "plan"), ("codex", 'sandbox_mode="read-only"')):
            with self.subTest(agent=agent):
                m = self.native_run(agent, permission="read-only")
                self.next_output(agent)
                p = self.command("resume-run.sh", m["run_dir"], "--native")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(json.loads(p.stdout)["permission"], "read-only")
                self.assertIn(expected, json.loads(Path(self.env["FAKE_ARGS"]).read_text()))

    def test_native_nonrepo_reuses_work_directory_and_does_not_touch_home_config(self):
        self.runs = self.root / "runs with spaces ü"
        self.env["AGENT_RUNS_ROOT"] = str(self.runs)
        home = Path(self.env["HOME"])
        sentinels = []
        for name in (".claude", ".codex", ".pi", ".omp", ".grok-bot-agents"):
            d = home / name
            d.mkdir()
            f = d / "sentinel"
            f.write_text("unchanged\n")
            sentinels.append(f)
        keys = home / ".agent-keys"
        keys.write_text("# fake home only\n")
        sentinels.append(keys)
        before = {p: p.read_bytes() for p in sentinels}
        m = self.native_run()
        self.next_output()
        p = self.command("resume-run.sh", m["run_dir"], "--native")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["workdir"], m["workdir"])
        self.assertEqual(before, {p: p.read_bytes() for p in sentinels})

    def test_native_missing_session_id_falls_back_to_fresh(self):
        repo = self.repo()
        m = self.run_agent("claude", [], rc=1, extra=("--cwd", repo))
        self.next_output("claude")
        p = self.command("resume-run.sh", m["run_dir"], "--native")
        self.assertEqual(p.returncode, 0, p.stderr)
        resumed = json.loads(p.stdout)
        self.assertIn("falling back to fresh-session", p.stderr)
        self.assertNotEqual(resumed["workdir"], m["workdir"])
        self.assertEqual(resumed["resume_mode"], "fresh")
        self.assertNotIn("--resume", json.loads(Path(self.env["FAKE_ARGS"]).read_text()))

    def test_native_missing_or_wrong_project_session_file_falls_back(self):
        for wrong_cwd in (False, True):
            with self.subTest(wrong_cwd=wrong_cwd):
                m = self.native_run()
                path = Path(m["session_file"])
                if wrong_cwd:
                    path.write_text(json.dumps({"type": "session", "id": m["session_id"], "cwd": str(self.root)}) + "\n")
                else:
                    path.unlink()
                self.next_output()
                p = self.command("resume-run.sh", m["run_dir"], "--native")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertIn("missing session id, owned session file or workdir", p.stderr)
                self.assertEqual(json.loads(p.stdout)["resume_mode"], "fresh")

    def test_native_startup_rejection_falls_back_once_for_each_cli(self):
        repo = self.repo()
        for agent, reason in (("claude", "No conversation found with session ID: session-123"),
                              ("codex", "Session session-123 not found"),
                              ("pi", "Session file does not exist"),
                              ("omp", "error: unknown option '--resume'")):
            with self.subTest(agent=agent):
                m = self.native_run(agent, repo)
                self.next_output(agent)
                self.env["FAKE_NATIVE_REJECT"] = reason
                p = self.command("resume-run.sh", m["run_dir"], "--native", "--", "--timeout", "10")
                self.env.pop("FAKE_NATIVE_REJECT")
                self.assertEqual(p.returncode, 0, p.stderr)
                resumed = json.loads(p.stdout)
                self.assertIn("CLI rejected native resume", p.stderr)
                self.assertTrue(resumed["run_dir"].endswith("-r3"))
                self.assertEqual(resumed["resume_mode"], "fresh")
                self.assertNotEqual(resumed["workdir"], m["workdir"])
                self.assertEqual(resumed["permission"], "safe")
                self.assertTrue(Path(m["workdir"]).exists())
                attempt = Path(m["run_dir"] + "-r2")
                self.assertEqual(json.loads((attempt / "meta.json").read_text())["status"], "error")

    def test_native_auth_and_task_errors_do_not_trigger_fresh_replay(self):
        for progress in (False, True):
            with self.subTest(progress=progress):
                m = self.native_run()
                if progress:
                    Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(message("", "error", "Session file does not exist")) + "\n")
                else:
                    self.env["FAKE_NATIVE_REJECT"] = "Invalid API key"
                p = self.command("resume-run.sh", m["run_dir"], "--native")
                self.env.pop("FAKE_NATIVE_REJECT", None)
                self.assertEqual(p.returncode, 1, p.stderr)
                self.assertNotIn("falling back", p.stderr)
                self.assertEqual(json.loads(p.stdout)["resume_mode"], "native")
                self.assertFalse(Path(m["run_dir"] + "-r3").exists())

    def test_native_startup_failure_preserves_requested_claude_codex_identity(self):
        for agent in ("claude", "codex"):
            for reason in ("Invalid API key", "error sending request"):
                with self.subTest(agent=agent, reason=reason):
                    m = self.native_run(agent)
                    self.env["FAKE_NATIVE_REJECT"] = reason
                    p = self.command("resume-run.sh", m["run_dir"], "--native")
                    self.env.pop("FAKE_NATIVE_REJECT")
                    self.assertEqual(p.returncode, 1, p.stderr)
                    failed = json.loads(p.stdout)
                    self.assertEqual(failed["session_id"], m["session_id"])
                    self.assertNotIn("falling back", p.stderr)
                    attempt = Path(failed["run_dir"])
                    self.assertEqual((attempt / "stdout.log").read_text(), "")
                    # Even an interrupted summarizer must not break the next resume.
                    (attempt / "meta.json").unlink()
                    self.next_output(agent)
                    p = self.command("resume-run.sh", attempt, "--native")
                    self.assertEqual(p.returncode, 0, p.stderr)
                    self.assertEqual(json.loads(p.stdout)["resume_mode"], "native")
                    args = json.loads(Path(self.env["FAKE_ARGS"]).read_text())
                    self.assertIn(m["session_id"], args)

    def test_observed_session_id_overrides_requested_identity(self):
        for agent in ("claude", "codex"):
            with self.subTest(agent=agent):
                m = self.run_agent(agent, self.session_events(agent, success=True),
                                   extra=("--resume", "requested-old-id"))
                self.assertEqual(m["session_id"], "session-123")

    def test_native_invalid_login_session_is_not_resume_rejection(self):
        m = self.native_run()
        self.env["FAKE_NATIVE_REJECT"] = "401 Unauthorized: invalid session token"
        p = self.command("resume-run.sh", m["run_dir"], "--native")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertNotIn("falling back", p.stderr)
        self.assertFalse(Path(m["run_dir"] + "-r3").exists())

    def test_native_recovers_session_without_terminal_meta(self):
        for agent in ("claude", "codex", "pi", "omp"):
            with self.subTest(agent=agent):
                m = self.native_run(agent)
                r = Path(m["run_dir"])
                (r / "meta.json").unlink()
                if agent in ("pi", "omp"):
                    (r / "stdout.log").write_text("truncated\n")  # recover from owned header
                self.next_output(agent)
                p = self.command("resume-run.sh", r, "--native")
                self.assertEqual(p.returncode, 0, p.stderr)
                self.assertEqual(json.loads(p.stdout)["resume_mode"], "native")

    def test_native_dry_run_preserves_wip_and_rejects_scope_overrides(self):
        m = self.native_run(repo=self.repo())
        r = Path(m["run_dir"])
        (Path(m["workdir"]) / "wip.txt").write_text("uncommitted")
        before = {str(p): p.read_bytes() for p in r.rglob("*") if p.is_file()}
        p = self.command("resume-run.sh", r, "--native", "--dry-run")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn("--resume-in-place", p.stdout)
        self.assertIn("would commit WIP", p.stderr)
        self.assertEqual(before, {str(p): p.read_bytes() for p in r.rglob("*") if p.is_file()})
        for flag in ("--agent", "--cwd", "--id", "--resume", "--review", "--prompt"):
            p = self.command("resume-run.sh", r, "--native", "--", flag, "other")
            self.assertNotEqual(p.returncode, 0)
            self.assertIn("unsupported --native override", p.stdout)
        self.assertEqual(self.git(r / "wt", "status", "--porcelain"), "?? wip.txt")

    def test_native_live_run_refuses_without_kill_then_checkpoints_and_resumes(self):
        repo = self.repo()
        self.env.update(FAKE_SESSION_ID="session-123", FAKE_TOUCH="1", FAKE_DELAY="60")
        self.next_output(success=False)
        self.env["FAKE_DELAY"] = "60"
        r = self.runs / "live"
        p = subprocess.Popen(["bash", str(SCRIPTS / "run-agent.sh"), "--agent", "pi", "--id", "live",
                              "--cwd", str(repo), "--prompt", "multi-step test"], env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        def cleanup():
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
            p.wait(timeout=5)
        self.addCleanup(cleanup)
        deadline = time.monotonic() + 5
        while not (r / "wt/saved.txt").exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue((r / "wt/saved.txt").exists())
        refused = self.command("resume-run.sh", r, "--native")
        self.assertEqual(refused.returncode, 5, refused.stderr)
        self.assertIn("still alive", refused.stdout)
        self.assertIsNone(p.poll())
        self.assertEqual(self.git(r / "wt", "status", "--porcelain"), "?? saved.txt")
        self.env.pop("FAKE_TOUCH")
        self.next_output()
        resumed = self.command("resume-run.sh", r, "--native", "--kill")
        self.assertEqual(resumed.returncode, 0, resumed.stderr)
        p.wait(timeout=5)
        self.assertEqual(json.loads(resumed.stdout)["workdir"], str(r / "wt"))
        self.assertEqual(self.git(r / "wt", "status", "--porcelain"), "")
        self.assertIn("checkpoint before resume", self.git(r / "wt", "log", "-1", "--format=%s"))

    def background(self, *args, cwd=None):
        p = subprocess.Popen(list(map(str, args)), cwd=cwd, env=self.env,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        def cleanup():
            # timeout creates its own process group; use its normal TERM forwarding first.
            if p.poll() is None:
                p.terminate()
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(p.pid, signal.SIGKILL)
                p.wait(timeout=5)
        self.addCleanup(cleanup)
        return p

    def wait_file(self, path):
        deadline = time.monotonic() + 5
        while not path.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(path.exists(), str(path))

    def test_original_id_cannot_race_a_live_native_continuation(self):
        m = self.native_run(repo=self.repo())
        original, work = Path(m["run_dir"]), Path(m["workdir"])
        self.env.update(FAKE_DELAY="60", FAKE_TOUCH="1")
        active = self.background("bash", SCRIPTS / "resume-run.sh", original, "--native", "--new-id", "active")
        self.wait_file(work / "saved.txt")
        head = self.git(work, "rev-parse", "HEAD")
        calls = Path(self.env["FAKE_ARGS"] + ".calls").read_bytes()
        # Both native AND fresh resumes of the original ID must honor its current owner.
        for flags in (("--native",), (), ("--native", "--dry-run")):
            refused = self.command("resume-run.sh", original, "--new-id", "blocked", *flags)
            self.assertEqual(refused.returncode, 5, refused.stderr)
            self.assertIn("still alive", refused.stdout)
        # Direct in-place launches must use the same lock, including alias paths/run roots.
        alias = self.root / "work-alias"
        alias.symlink_to(work, target_is_directory=True)
        self.env["AGENT_RUNS_ROOT"] = str(self.root / "other-runs")
        refused = self.command("run-agent.sh", "--agent", "pi", "--id", "direct", "--prompt", "test",
                               "--cwd", alias, "--resume", m["session_file"], "--resume-in-place")
        self.assertEqual(refused.returncode, 5, refused.stderr)
        self.assertEqual(self.git(work, "rev-parse", "HEAD"), head)
        self.assertEqual(self.git(work, "status", "--porcelain"), "?? saved.txt")
        self.assertEqual(Path(self.env["FAKE_ARGS"] + ".calls").read_bytes(), calls)
        self.assertIsNone(active.poll())
        self.env.pop("FAKE_TOUCH")
        self.next_output()
        p = self.command("resume-run.sh", original, "--native", "--kill", "--new-id", "recovered")
        self.assertEqual(p.returncode, 0, p.stderr)
        active.wait(timeout=5)
        self.assertEqual(json.loads(p.stdout)["workdir"], str(work))
        self.assertEqual(self.git(work, "status", "--porcelain"), "")

    def test_stale_pid_does_not_skip_legacy_workdir_process_scan(self):
        m = self.native_run(repo=self.repo())
        r, work = Path(m["run_dir"]), Path(m["workdir"])
        subdir = work / "nested"
        subdir.mkdir()
        p = self.background("timeout", "60", "sleep", "60", cwd=subdir)
        (r / "agent.pid").write_text("999999999")
        (work / "saved.txt").write_text("legacy orphan work")
        refused = self.command("resume-run.sh", r, "--native")
        self.assertEqual(refused.returncode, 5, refused.stderr)
        self.assertEqual(self.git(work, "status", "--porcelain"), "?? saved.txt")
        self.assertIsNone(p.poll())
        self.next_output()
        recovered = self.command("resume-run.sh", r, "--native", "--kill")
        self.assertEqual(recovered.returncode, 0, recovered.stderr)
        p.wait(timeout=5)
        self.assertEqual(self.git(work, "status", "--porcelain"), "")

    def test_simultaneous_native_resumes_have_one_owner(self):
        m = self.native_run()
        self.env["FAKE_DELAY"] = "2"
        first = self.background("bash", SCRIPTS / "resume-run.sh", m["run_dir"], "--native", "--new-id", "first")
        second = self.background("bash", SCRIPTS / "resume-run.sh", m["run_dir"], "--native", "--new-id", "second")
        results = sorted([first.wait(timeout=10), second.wait(timeout=10)])
        self.assertEqual(results, [1, 5])  # one fake transport error, one ownership refusal
        calls = Path(self.env["FAKE_ARGS"] + ".calls").read_text().splitlines()
        self.assertEqual(len(calls), 2)  # original plus exactly one continuation

    def test_stale_owner_metadata_cannot_kill_a_different_workdir(self):
        m = self.native_run()
        _, unrelated = self.idle_run("pi")
        lock = next((Path(self.env["HOME"]) / ".grok-bot-agents/worktree-locks").glob("*.lock"))
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        start = Path(f"/proc/{unrelated.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        lock.write_text(json.dumps({"pid": unrelated.pid, "start": f"{boot}:{start}"}))
        # A new/legacy holder has the lock but has not replaced stale owner metadata.
        ready = self.root / "lock-ready"
        holder = self.background("python3", "-c", """
import fcntl, os, pathlib, sys, time
fd = os.open(sys.argv[1], os.O_RDWR)
os.dup2(fd, 9)
fcntl.flock(9, fcntl.LOCK_EX)
pathlib.Path(sys.argv[2]).touch()
time.sleep(30)
""", lock, ready, cwd=m["workdir"])
        self.wait_file(ready)
        self.next_output()
        p = self.command("resume-run.sh", m["run_dir"], "--native", "--kill")
        self.assertEqual(p.returncode, 0, p.stderr)
        holder.wait(timeout=5)
        self.assertIsNone(unrelated.poll())

    def test_new_lock_owner_is_published_before_legacy_processes_drain(self):
        m = self.native_run()
        lock = next((Path(self.env["HOME"]) / ".grok-bot-agents/worktree-locks").glob("*.lock"))
        ready = self.root / "legacy-ready"
        legacy = self.background("python3", "-c", """
import pathlib, signal, sys, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
pathlib.Path(sys.argv[1]).touch()
time.sleep(30)
""", ready, cwd=m["workdir"])
        self.wait_file(ready)
        recovery = self.background("bash", SCRIPTS / "resume-run.sh", m["run_dir"], "--native", "--kill")
        owner = {}
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                owner = json.loads(lock.read_text())
            except ValueError:
                continue
            if owner.get("pid") == recovery.pid:
                break
            time.sleep(0.02)
        self.assertEqual(owner.get("pid"), recovery.pid)
        self.assertIsNone(legacy.poll())  # published before the 3-second TERM grace expires
        self.assertEqual(recovery.wait(timeout=10), 1)  # fake CLI's transport failure
        legacy.wait(timeout=5)

    def test_kill_binds_pidfd_before_identity_check_and_never_uses_numeric_kill(self):
        p = subprocess.run(["python3", "-c", """
import signal, sys
from unittest.mock import patch
sys.path.insert(0, sys.argv[1])
import worktree_guard as guard
calls = []
def opened(pid):
    calls.append("open")
    return 42
def snapshot():
    calls.append("identity")
    return {123: (1, "original")}
def sent(fd, sig):
    assert fd == 42 and sig == signal.SIGTERM
    calls.append("signal")
with patch.object(guard.os, "pidfd_open", side_effect=opened), \\
     patch.object(guard, "processes", side_effect=snapshot), \\
     patch.object(guard.signal, "pidfd_send_signal", side_effect=sent) as send, \\
     patch.object(guard.os, "close"), patch.object(guard.os, "kill") as numeric:
    guard.signal_same_processes({123: "original"}, signal.SIGTERM)
    assert calls == ["open", "identity", "signal"], calls
    guard.signal_same_processes({123: "recycled"}, signal.SIGTERM)
    assert send.call_count == 1
    numeric.assert_not_called()
""", str(SCRIPTS)], env=self.env, text=True, capture_output=True, timeout=5)
        self.assertEqual(p.returncode, 0, p.stderr)

    def test_stale_lock_identity_never_targets_reused_pid(self):
        m = self.native_run()
        locks = list((Path(self.env["HOME"]) / ".grok-bot-agents/worktree-locks").glob("*.lock"))
        self.assertEqual(len(locks), 1)
        owner = json.loads(locks[0].read_text())
        self.assertIn("start", owner)  # Linux: boot ID + process start ticks
        owner.update(pid=os.getpid(), start="different-boot:0")
        locks[0].write_text(json.dumps(owner))
        # A stale file is not a live kernel lock; never signal its unverified PID.
        self.next_output()
        p = self.command("resume-run.sh", m["run_dir"], "--native", "--kill")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["resume_mode"], "native")

    def test_native_chain_and_fresh_resume_from_native_keep_saved_work(self):
        m = self.native_run(repo=self.repo())
        original = m["workdir"]
        for _ in range(2):
            (Path(original) / "saved.txt").write_text("keep chain work")
            p = self.command("resume-run.sh", m["run_dir"], "--native")
            self.assertEqual(p.returncode, 1, p.stderr)  # fake continues to report a transport failure
            m = json.loads(p.stdout)
            self.assertEqual(m["workdir"], original)
            self.assertTrue(Path(m["session_file"]).is_file())
        self.next_output()
        blocked = self.command("resume-run.sh", m["run_dir"])
        self.assertEqual(blocked.returncode, 6, blocked.stderr)
        self.assertEqual(json.loads(blocked.stdout)["error"], "restart_approval_required")
        p = self.command("resume-run.sh", m["run_dir"], "--allow-extra-restart")
        self.assertEqual(p.returncode, 0, p.stderr)
        fresh = json.loads(p.stdout)
        self.assertNotEqual(fresh["workdir"], original)
        self.assertEqual((Path(fresh["workdir"]) / "saved.txt").read_text(), "keep chain work")
        self.assertEqual(fresh["resume_mode"], "fresh")

    def test_native_rate_limit_never_removes_original_worktree(self):
        m = self.native_run(repo=self.repo())
        Path(self.env["FAKE_OUTPUT"]).write_text(json.dumps(message("", "error", "429 Too many requests")) + "\n")
        p = self.command("resume-run.sh", m["run_dir"], "--native")
        self.assertEqual(p.returncode, 1, p.stderr)
        self.assertEqual(json.loads(p.stdout)["status"], "rate_limited")
        self.assertTrue(Path(m["workdir"]).exists())

    def idle_run(self, agent=None, connected=False):
        self.count += 1
        r = self.runs / f"idle-{self.count}"
        work = r / "work"
        work.mkdir(parents=True)
        command = ["sleep", "30"]
        if connected:
            server = socket.socket()
            self.addCleanup(server.close)
            server.bind(("127.0.0.1", 0))
            server.listen()
            command = ["python3", "-c", """
import pathlib, socket, sys, time
s = socket.create_connection(("127.0.0.1", int(sys.argv[1])))
r = pathlib.Path(sys.argv[2])
(r / "connected").touch()
while not (r / "disconnect").exists():
    time.sleep(0.05)
s.close()
(r / "disconnected").touch()
time.sleep(30)
""", str(server.getsockname()[1]), str(r)]
        p = subprocess.Popen(["timeout", "30", *command], cwd=work, env=self.env,
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
        if connected:
            self.wait_file(r / "connected")
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        start = Path(f"/proc/{p.pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        state = "connected" if connected else "disconnected"
        (r / "supervise.samples").write_text(f"{now - 1200} 0 {now - 1200} {state} {boot}:{p.pid}:{start}\n")
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

    def test_silent_connected_pi_omp_are_advisory_even_with_explicit_restart(self):
        for agent in ("pi", "omp"):
            with self.subTest(agent=agent):
                r, p = self.idle_run(agent, connected=True)
                for flags in ((), ("--stall-action", "restart")):
                    proc = self.supervise(r, *flags)
                    self.assertEqual(proc.returncode, 0, proc.stderr)
                    self.assertIn("reason=waiting_on_model", proc.stdout)
                    self.assertIn("resumable=0", proc.stdout)
                    self.assertIn("tcp=connected", (r / "supervise.log").read_text())
                    self.assertIsNone(p.poll())

    def test_unowned_tcp_connection_does_not_hide_disconnected_idle(self):
        self.idle_run("pi", connected=True)  # same network namespace, different process tree
        r, _ = self.idle_run("omp")
        proc = self.supervise(r)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("resumable=1", proc.stdout)
        self.assertIn("tcp=disconnected", (r / "supervise.log").read_text())

    def test_disconnect_needs_a_full_stall_window_before_restart(self):
        r, _ = self.idle_run("pi", connected=True)
        (r / "disconnect").touch()
        self.wait_file(r / "disconnected")
        proc = self.supervise(r)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("resumable=0", proc.stdout)
        self.assertIn("tcp=disconnected tcp_idle=0", (r / "supervise.log").read_text())
        # Advance the observed disconnected window without a ten-minute wall-clock sleep.
        sample = (r / "supervise.samples").read_text().splitlines()[0].split()
        sample[3] = "disconnected"
        (r / "supervise.samples").write_text(" ".join(sample) + "\n")
        proc = self.supervise(r)
        self.assertEqual(proc.returncode, 2, proc.stderr)
        self.assertIn("no_connection_10m", proc.stdout)

    def test_legacy_and_unknown_tcp_samples_do_not_authorize_restart(self):
        for evidence in ("", "unknown", "connected"):
            with self.subTest(evidence=evidence):
                r, _ = self.idle_run("pi")
                sample = (r / "supervise.samples").read_text().split()
                if evidence:
                    sample[3] = evidence
                else:
                    sample = sample[:3]  # old supervisor format
                (r / "supervise.samples").write_text(" ".join(sample) + "\n")
                proc = self.supervise(r)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertIn("resumable=0", proc.stdout)

    def test_tcp_inode_join_ipv6_and_unavailable_visibility(self):
        proc = self.root / "proc"
        fds, net = proc / "123/fd", proc / "123/net"
        fds.mkdir(parents=True)
        net.mkdir()
        (fds / "9").symlink_to("socket:[42]")
        def state():
            p = subprocess.run(["python3", "-c", "import sys; sys.path.insert(0, sys.argv[1]); "
                                "from process_activity import tcp_state; from pathlib import Path; "
                                "print(tcp_state([123], Path(sys.argv[2])))", str(SCRIPTS), str(proc)],
                               env=self.env, capture_output=True, text=True, check=True, timeout=5)
            return p.stdout.strip()
        row = "0: 00000000:0000 00000000:0000 01 0:0 0:0 0 0 0 42\n"
        (net / "tcp").write_text("header\n" + row.replace("42", "99"))
        (net / "tcp6").write_text("header\n")
        self.assertEqual(state(), "disconnected")
        (net / "tcp6").write_text("header\n" + row)
        self.assertEqual(state(), "connected")
        (net / "tcp6").unlink()  # IPv6 disabled; IPv4 evidence is still valid.
        self.assertEqual(state(), "disconnected")
        (net / "tcp").unlink()
        self.assertEqual(state(), "unknown")

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

    def test_native_linked_session_writes_prevent_stall_but_global_writes_do_not(self):
        r, _ = self.idle_run("pi")
        owned = self.root / "owned-sessions"
        owned.mkdir()
        (r / "sessions").symlink_to(owned, target_is_directory=True)
        global_sessions = Path(self.env["HOME"]) / ".pi/agent/sessions"
        global_sessions.mkdir(parents=True)
        (global_sessions / "other-task.jsonl").write_text("{}\n")
        proc = self.supervise(r)
        self.assertIn("status=stalled", proc.stdout)
        (owned / "owned.jsonl").write_text("{}\n")
        proc = self.supervise(r)
        self.assertEqual(proc.returncode, 0, proc.stderr)
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
        sample = (r / "supervise.samples").read_text().split()
        sample[1] = "100000"
        (r / "supervise.samples").write_text(" ".join(sample) + "\n")
        proc = self.supervise(r, "--stall-action", "restart")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("status=running", proc.stdout)


if __name__ == "__main__":
    unittest.main()
