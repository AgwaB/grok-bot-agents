# Changelog

## 0.1.0 (2026-10-18)

First release.

- `setup-agents`: multi-select install of Claude Code, Codex CLI, Pi and omp; subscription sign-in,
  masked API keys in `~/.agent-keys`, or import; native model/reasoning by default; OK test per CLI.
- `import-agents`: allowlisted import of CLI logins and settings from a registered computer
  (`import_plan.py`, `apply_import.sh`), `--dereference` for symlinked skills (`stage_tar.py`),
  macOS metadata and symlinks skipped, conflicts stop unless `--overwrite`.
- `configure-agents`: central `~/.grok-bot-agents/config.json` (`agents_config.py`).
- `delegate-task`: light/heavy rules, `route.py` auto-routing with rate-limit state and fallback,
  headless runner with worktree-per-task, per-run work dirs, full-auto/safe/read-only modes,
  `--review` read-only review with a change guard, JSON summaries (`ok`, `error`, `timeout`,
  `rate_limited`).
- `agent-bridge-webhook`: webhook routine spec, `bridge.py` intake/reply with validation, dedupe and
  logging, `sanitize_result.py`, and a macOS/Linux `send-task.sh` sender. Tailscale return channel:
  `bridge_server.py` (token-auth long-poll + SSE), `bridge.py progress`, `bridge-serve.sh` keep-alive,
  `grok-send-task --wait/--fetch` streams progress and prints the answer in the caller's terminal.
  `setup-tailscale.sh` installs (sha256-checked static build, no root), starts and logs in Tailscale on the
  box; the user only approves the printed `LOGIN_URL`. `grok-send-task --check` checks Tailscale on the caller.
- Named `grok-bot-agents` (was `grok-agents` during development). Scripts still read the legacy
  `~/.grok-agents/config.json`, `$GROK_AGENTS_CONFIG`, `$GROK_AGENTS_STATE` and
  `~/.config/grok-agents/bridge.env` when the new ones are absent.
