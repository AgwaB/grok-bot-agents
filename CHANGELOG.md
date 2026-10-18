# Changelog

## 0.1.0 (2026-10-18)

First release.

- `setup-agents`: multi-select install of Claude Code, Codex CLI, Pi and omp; subscription sign-in,
  masked API keys in `~/.agent-keys`, or import; native model/reasoning by default; OK test per CLI.
- `import-agents`: allowlisted import of CLI logins and settings from a registered computer
  (`import_plan.py`, `apply_import.sh`), `--dereference` for symlinked skills (`stage_tar.py`),
  macOS metadata and symlinks skipped, conflicts stop unless `--overwrite`.
- `configure-agents`: central `~/.grok-agents/config.json` (`agents_config.py`).
- `delegate-task`: light/heavy rules, `route.py` auto-routing with rate-limit state and fallback,
  headless runner with worktree-per-task, per-run work dirs, full-auto/safe/read-only modes,
  `--review` read-only review with a change guard, JSON summaries (`ok`, `error`, `timeout`,
  `rate_limited`).
- `agent-bridge-webhook`: webhook routine spec, `bridge.py` intake/reply with validation, dedupe and
  logging, `sanitize_result.py`, and a macOS/Linux `send-task.sh` sender.
