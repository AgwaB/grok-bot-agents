# Changelog

## 0.1.0 (2026-10-18)

First release.

- `setup-agents`: multi-select install of Claude Code, Codex CLI, Pi and omp; subscription sign-in,
  masked API keys in `~/.agent-keys`, or import; native model/reasoning by default; OK test per CLI.
- `import-agents`: allowlisted import of CLI logins and settings from a registered computer
  (`import_plan.py`, `apply_import.sh`), `--dereference` for symlinked skills (`stage_tar.py`),
  macOS metadata and symlinks skipped, conflicts stop unless `--overwrite`. `detect_local.sh` (read-only,
  names only) and `bundle_local.sh` (one dereferenced allowlist tarball, one `CopyToBox`) run on the user's computer;
  `import_plan.py --lint-scripts` keeps them in sync with the allowlist.
  Runtime parity: Pi packages (local, npm with local patches, venv pins), Claude plugins pinned to the computer's
  commit, hook/statusLine tool paths, MCP server `uv tool`s, Mac-only Codex MCP/notify disabled (`post_import.py`),
  and a read-only `parity_check.py` per CLI.
- Auth import (general, macOS/Linux/Windows): `detect_local.sh` adds a name-only secret inventory (login files and
  type, Pi OAuth expiry, env names configs reference and whether they're set in the process or login shell,
  `!cmd`/`op://` indirections by tool name, literal keys in configs by key path, package auth files, remote MCP servers
  needing OAuth, Keychain/Secret Service presence); `detect_local.ps1` is its Windows twin with the same scanner.
  One consent card; the new `export_secrets.sh` exports only the ticked items (env values, `!cmd` results, opt-in
  Keychain logins) into 600 files and prints names and sizes only. Claude Code and Codex subscription logins default
  to a box login; copying them (`bundle_local.sh --copy-login`, `keychain:claude`/`keychain:codex`) is an opt-in with
  the two-machine and Anthropic ToS warnings. `apply_import.sh keys` merges exported keys into `~/.agent-keys`
  (no duplicates), Codex is pinned to file credential stores, resolved Pi/omp `!cmd` keys become env-name references,
  a copied Claude login brings `oauthAccount`/`hasCompletedOnboarding`. Pi package auth files are matched by pattern,
  not package name; `import_plan.py` marks rotation, default and sensitive files; `parity_check.py` adds an `auth`
  section (names only). `tests/auth_import_test.sh` checks that no secret value reaches any script output.
- `setup-agents` first run defaults to importing: detect local CLIs, pre-check them, one "import everything from my
  computer" card for all chosen agents, per-agent cards only on request or without a computer, and no model card
  after an import.
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
