# Changelog

## 0.1.0 (2026-10-18)

First release.

- Fix delegate recovery review findings: discover omp session headers after title records
  (bounded 32-record scan; synthetic format fixture), retain requested Claude/Codex IDs
  through early native failures, and lock canonical workdirs across checkpoint/launch
  with process-start/boot identity, pidfd-safe signals and independent cwd scans. Require a
  full disconnected, low-CPU/no-write window before stall restart; established process-tree TCP sockets or
  unknown visibility stay advisory. Add isolated ownership races and loopback-socket tests.

- Add opt-in `resume-run.sh --native` for Claude/Codex/Pi/omp: checkpoint WIP and
  continue the CLI session in the same worktree, with fresh run logs, preserved permissions,
  interrupted-tool verification guidance and explicit fresh-session fallback on missing
  sessions or startup rejection. Keep default recovery fresh-session/new-worktree.
  Recover IDs from run-owned logs/files even without terminal metadata; record session
  files and observe their writes across native continuations. Add isolated fake-CLI tests.

- Generalize the runner research's experiment descriptions and remove machine-local
  paths/internal investigation notes while retaining findings and source citations.

- Delegate runner hardening (still **0.1.0**), informed by
  research into existing open-source agent runners:
  - Parse Claude streamed results, Codex terminal failures, and Pi/omp final stop reasons;
    preserve actual exit codes, reject missing/truncated completion, classify transport errors
    without mistaking partial output or old retry diagnostics for the final error.
  - Stream Claude partial progress; default stall action is `restart` for Pi/omp (no output,
    owned session writes, CPU progress or live TCP connection), `report` for Claude/Codex and unknown runs.
    Explicit `--stall-action` overrides either default; retain wake/observation-gap grace
    and atomic metadata publication.
  - Preserve restart permissions/run roots; make dry-run write-free; reject reused/unsafe run IDs,
    keep run data private, and check timeout cwd before trusting a PID. Rate-limit cleanup now
    retains agent commits and ignored artifacts instead of treating a clean index as no work.
  - Add isolated fake-CLI regression coverage; native cross-worktree session recovery and real
    host-suspend guarantees remain out of scope. See `TESTING.md` for evidence and limitations.

- Long delegated runs survive box idle pauses (still **0.1.0**): `delegate-task` adds
  `scripts/supervise.sh` (bounded watch slices, one status line: running, done, failed or
  stalled) for supervisor subagents rotated every 30 minutes, and `scripts/resume-run.sh`
  (commit WIP on `agent/<id>`, relaunch with a continuation brief). `run-agent.sh` writes
  `agent.pid`/`runner.pid`. New "Known limits" section: idle VM pauses, a suspected ~1 hour
  subagent cap.
- Pi survives idle pauses better (still **0.1.0**): `run-agent.sh` turns pi's auto-retry on per run
  (a work-dir `.pi/settings.json` with only `retry.enabled`, `--approve`, a self-ignoring `.pi/.gitignore`,
  removed after the run; `~/.pi/agent/settings.json` is never touched; skipped for `--review` and for work dirs
  with their own trust-gated pi files). `summarize_run.py` now uses the last message's `stopReason` even when
  it has partial text (pi exits 0 on errors) and flags dropped connections/streams as `error_kind: conn_error`,
  `resumable: true`; `supervise.sh` reports `failed reason=conn_error resumable=1` and the bot resumes such runs
  with `resume-run.sh` (same 2-restart cap). Known limits: freezes up to ~75 s are fine, 90 s+ break the stream;
  a real 180 s freeze recovered via retry.
- Windows import is supported but less tested than macOS/Linux; the docs and the
  import flow now say it may not go smoothly and point to box login as the fallback.
- Import regression coverage (still **0.1.0**): research on eight open-source
  projects, a 64-scenario matrix, and an isolated offline runner
  (`tests/run_import_tests.sh`). 60 Python tests (59 passed, PowerShell skipped
  when unavailable) plus 26 existing Bash auth checks; random canaries guard both
  output streams, all homes are temporary, and CLIs/stores/installers are stubbed.
- Import safety/correctness fixes from the matrix:
  - `bundle_local.sh`: parse Codex auth before choosing the static-key default;
    fail closed on unknown formats, respect directory deselections and overrides,
    exclude forbidden symlink targets, and retain exports after tar failure.
  - `export_secrets.sh`: reject failed/partial/malformed store and indirection
    results; never fall back to an arbitrary Codex keyring account.
  - `stage_tar.py` / `import_safety.py`: reject traversal, links, special files,
    unsafe package roots and symlink destinations; bound extraction and write
    through unique private temporary files.
  - `apply_import.sh`: preflight conflicts before merging secrets, preserve working
    omp auth on corrupt/disabled imports, guard database sidecars/auxiliary paths,
    keep distinct backups, merge identical Claude login metadata, and initialize
    or update central config only after successful verification (including identical
    auth on a retry after `--no-verify`).
  - `post_import.py`: literal-only key merges, idempotent top-level Codex store
    pinning/MCP disabling, guarded atomic npm overlays, spaced Pi venv roots and
    quoted Claude hook/status-line paths; suppress raw helper/parser diagnostics.
  - `detect_local.sh` / `detect_local.ps1` / `parity_check.py`: honor relocated
    configs, sanitize version/status output, aggregate omp enabled rows correctly,
    and make `--no-run` parity avoid invoking Pi.
  Native Windows/macOS stores/ACLs, real refresh/installers and concurrent imports
  remain explicitly unverified; see `TESTING.md` and `tests/MATRIX.md`.

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
