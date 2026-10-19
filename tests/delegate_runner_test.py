#!/usr/bin/env python3
"""Offline delegate-task regressions: fake CLIs, clean environment, temporary HOME only."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "skills/delegate-task/scripts"
FAKE_CLI = r'''#!/usr/bin/env python3
import json, os, pathlib, sys
if "--help" in sys.argv:
    print("--approve")
    sys.exit(0)
pathlib.Path(os.environ["FAKE_ARGS"]).write_text(json.dumps(sys.argv[1:]))
if "-o" in sys.argv and "FAKE_RESULT" in os.environ:
    pathlib.Path(sys.argv[sys.argv.index("-o") + 1]).write_text(os.environ["FAKE_RESULT"])
print(pathlib.Path(os.environ["FAKE_OUTPUT"]).read_text(), end="", flush=True)
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
                      "error sending request", "WebSocket error", "WebSocket idle timeout after 300000ms"):
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

    def test_nonzero_exit_is_never_hidden_by_success_event(self):
        m = self.run_agent("pi", [message()], rc=7)
        self.assertEqual((m["status"], m["exit_code"]), ("error", 7))

    def test_no_output_error_uses_stderr(self):
        m = self.run_agent("codex", [], rc=1, stderr="error sending request")
        self.assertEqual(m["error_kind"], "conn_error")
        self.assertIn("error sending request", m["summary"])


if __name__ == "__main__":
    unittest.main()
