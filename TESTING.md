# Testing

## gba CLI review fixes (0.1.0, 2026-10-19)

All new test evidence is **offline**. No real credentials, provider calls, package downloads, global
installs, releases or external writes. Python fixtures use temporary HOME/config/run roots and
fake CLIs/stores; installers use mocked HTTPS artifacts and fake runtimes. Existing historical
live results farther below were **not repeated** and do not certify this new CLI.

Run from the repository root (Python 3.11+, Bash, Git, Node and ShellCheck already installed):

```bash
h=$(mktemp -d); trap 'rm -rf "$h"' EXIT
SC_DIR=$(dirname "$(command -v shellcheck)")
env -i HOME="$h" PATH=/usr/bin:/bin LANG=C.UTF-8 PYTHONDONTWRITEBYTECODE=1 \
  python3 -m unittest discover -s tests -p '*_test.py' -v
env -i HOME="$h" PATH="$SC_DIR:/usr/bin:/bin" PYTHONDONTWRITEBYTECODE=1 \
  bash tests/run_import_tests.sh
env -i HOME="$h" PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
  bash tests/delegate_pi_retry_test.sh
shellcheck -x skills/*/scripts/*.sh tests/*.sh
for f in skills/*/scripts/*.sh tests/*.sh; do bash -n "$f"; done
python3 -X "pycache_prefix=$h/cache" -m py_compile scripts/gba scripts/*.py skills/*/scripts/*.py tests/*.py
git diff --check
```

Latest results:

- **230 Python tests run: 229 passed, 1 skipped** (PowerShell unavailable). This includes
  **66 delegate + 60 import-matrix methods** and 104 CLI/config/contract methods; **33 added** in this review fix.
- `tests/run_import_tests.sh`: **60 tests / 59 passed / 1 skipped**, plus **26 auth Bash checks**;
  embedded-scanner parity, syntax, import ShellCheck and Python compilation passed.
- Standalone `python3 tests/delegate_runner_test.py`: **66 tests passed**.
- `tests/delegate_pi_retry_test.sh`: **26 checks passed**.
- Full `shellcheck -x skills/*/scripts/*.sh tests/*.sh`, Bash syntax, full compilation and
  `git diff --check`: **passed**; plugin version remains **0.1.0**.
- The first full run exposed an obsolete legacy key-overwrite expectation and local harness ignore
  interference. The key test now requires consent/backups; the ignore test uses an isolated Git repo.
  The complete discovery, import runner, Pi retry, syntax, ShellCheck and compile commands then passed.
- Active LSP error probes covered 12 changed Python files with no unsuppressed errors. Dispositioned
  findings are not claimed clean; CLI subprocess integration and compilation test actual resolution.
  Mechanical helper style/file-handle cleanup is isolated from safety fixes; legacy import regressions
  remain green. Guarded-I/O/valid-mode rule false positives were recorded without production ignores.

Current methods: config safety **5**, import controller **30**, setup **19**, run lifecycle **20**,
shared doctor **18**, CLI integration **7**, skill/webhook contracts **5**. Tests cover:

- Write-free planning including bytecode, standalone incomplete-plugin refusal, arbitrary cwd,
  stable JSON/exit codes, argument/parser errors and both-output-stream secret canaries.
- Mixed-provider whole-file refusal, omp sidecar grouping, source/export inventory and hashes,
  forbidden paths, private unique staging, missing/extra/changed/symlink transfers, corrupt state,
  runtime/overwrite preflight, interrupted retry, real keys/settings apply helpers and cleanup.
- Selected-only and repeated installs, preserving preferences/unselected agents, runtime/wrapper
  requirements, checksum/SRI failures, unsafe targets/archives and unsupported platforms.
- Original/custom-ID cap enforcement, simultaneous ledger reservations, native rejection fallback,
  explicit extra attempts, preserved inputs/timeout/permissions, write-free recovery dry-run,
  ownership guards and persistent supervisor handoff/rotation.
- Static no-execution/no-write key-name/parity diagnostics; opted-in version probes, managed integrity
  checks before probe/live execution, and fake live OK versus quota/mismatched output;
  config secrets rejected recursively, router JSON/bulk semantics, Pi `max`, and webhook command
  payloads staying data-only.

### Review regression map

| Finding | Fix and representative regression |
| --- | --- |
| 1 — key overwrite | Private preflight/backup; `test_key_replacement_requires_approval_and_private_backup`, `test_later_duplicate_key_assignment_also_requires_overwrite`; legacy `test_keys_idempotent` also enforces consent. |
| 2 — selection scope | File-scoped deterministic transforms, no broad runtime repairs; `test_partial_import_never_runs_unselected_runtime_repairs`, `test_selected_config_transform_backups_and_sanitized_reports_persist`. Warnings/backup paths survive status/verify. Unsupported overlay/plugin metadata is rejected. |
| 3 — doctor execution | Static by default, `--probe-versions` explicit, integrity before execution; `test_doctor_default_is_static_without_live_or_key_execution`, `test_verify_offline_is_static_and_never_calls_runner`, `test_modified_managed_wrapper_payload_or_receipt_blocks_execution`, `test_omp_static_parity_never_copies_or_writes_database`. |
| 4 — recovery scope | Persist effective inputs; do not add repository checkout; `test_repo_recovery_never_adds_original_checkout_to_inputs`, `test_legacy_launch_restores_explicit_inputs_but_never_guesses_cwd`; live launch argv covered with fake CLIs. |
| 5 — interrupted lineage | Ledger reservations survive missing child receipts; `test_interrupted_custom_reservations_keep_root_after_forward_pointer_overwrite`, `test_conflicting_reservations_and_receipts_fail_closed`, `test_corrupt_ledger_evidence_cannot_reset_cap`. |
| 6 — live writers | Durable group ownership gates launch/retry, drains on timeout/signals; `test_import_timeout_kills_and_drains_helper_process_group`, `test_parent_sigkill_blocks_retry_and_preserves_working_files`, `test_parent_sigterm_drains_children_before_unlock`, `test_helper_gate_prevents_writes_before_ownership_publication`. |
| 7 — verify artifacts | Private expected-destination and selected-key receipts; `test_verify_requires_each_selected_file_not_just_parity`, `test_verify_requires_selected_keys_and_preserves_stage_on_mismatch`, `test_verify_refuses_missing_receipts_and_skipped_helper_work`; transformed-source bytes also fail verification. |
| 8 — omp sidecars | Require DB and complete selected group; `test_sidecars_without_database_are_rejected`, `test_legacy_apply_refuses_orphan_sidecar`; stale destination WAL needs approved backup/deletion and verified absence. |
| 9 — generated state | Narrow tracked ignore rules; `test_generated_agent_run_state_is_ignored_but_config_is_not`; no `.pi/` file is tracked. |

### Remaining gaps / manual acceptance

- No native macOS/Windows store/ACL/junction checks or PowerShell execution. Installation supports
  Linux x64/arm64 glibc ≥2.28; real package downloads, lifecycle-script needs and version compatibility
  still need a separately approved disposable installation test.
- Marketplace/host tool path discovery, CopyToBox/consent UI, masked keys, login, Tailscale and actual
  supervisor dispatch/VM pause recovery were not exercised. The CLI only returns bot handoffs.
- Import operations serialize controller apply calls locally, but no distributed/legacy-writer lock,
  atomic whole-import rollback or exactly-once retry is claimed. Stop source omp before copying.
  Selected-file apply deliberately defers broad runtime repairs. Symlinked/external Pi packages, npm
  overlays and Claude plugin metadata need the reviewed legacy full-consent fallback. Unknown helper
  ownership fails closed for manual inspection; it never authorizes automatic replay.
- Config secret detection is heuristic. Unrecognized values may evade it; high-entropy ordinary text
  may be rejected. No script can prove human consent from a flag or selection file.
- Download hashes bind HTTPS metadata/artifacts; this is not independent release-signature verification.
  Recovery reservations are conservative and consumed even if startup fails. Hostile same-user
  filesystem races/network filesystems remain outside the tested ownership boundary.
- Only outbox bridge delivery is implemented; git/command reply types remain recorded data.

## Offline delegate runner regressions (0.1.0)

Run from the repository root on Linux with Bash, Python 3, Git and GNU utilities:

```bash
h=$(mktemp -d); trap 'rm -rf "$h"' EXIT
env -i HOME="$h" PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 python3 tests/delegate_runner_test.py
env -i HOME="$h" PATH=/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 bash tests/delegate_pi_retry_test.sh
shellcheck skills/delegate-task/scripts/{run-agent,supervise,resume-run,worktree_guard}.sh
bash -n skills/delegate-task/scripts/run-agent.sh
bash -n skills/delegate-task/scripts/supervise.sh
bash -n skills/delegate-task/scripts/resume-run.sh
bash -n skills/delegate-task/scripts/worktree_guard.sh
```

Use your installed `shellcheck`. No CLI download, provider call or real credential/config home
is needed. The Python suite creates a clean environment, temporary HOME/run roots,
fake versions of **all four CLIs** and npm, and disposable Git repositories. The
existing Bash suite is launched with an empty environment to exclude config overrides.

Historical pre-gba review-fix evidence: **62 delegate Python tests passed**, **26 Pi-retry Bash checks
passed**; ShellCheck reported **zero diagnostics** on all four
changed shell scripts. Shell syntax, temporary-cache Python compilation and `git diff --check`
passed. `bash tests/run_import_tests.sh` also passed again: **60 Python tests run, 59 passed,
one PowerShell skip**, plus **26 Bash auth checks**. All test entry points used `env -i` and
`mktemp` homes; fixtures never read/write real agent homes or credentials. Active LSP probes
found no type errors; numeric-parse advisories are bounded by the guard's error handler or
the supervisor's unknown-evidence fallback. No production suppressions were added.
Version remains **0.1.0**. Coverage includes:

- omp/Pi zero-exit errors; every reported transport phrase; retry recovery/exhaustion;
  auth/quota separation (including WebSocket 401/403); cancelled/aborted turns
  are not auto-retried; truncated turns fail; Unicode JSONL and malformed noise.
- Codex `turn.failed` over stale/empty result files, completed retries, missing terminal
  records; Claude legacy JSON and streamed result with `success + is_error: true`.
- Real GNU timeout around a fake CLI; stream flags; Pi/omp default stall restart versus
  Claude/Codex/unknown report-only, explicit overrides in both directions, owned session
  activity, observation-gap grace, shrinking CPU
  counters and a stale PID pointing at another run's live timeout.
- Exclusive/private run IDs and traversal rejection; permission recovery with/without
  metadata; write-free dry-run; absolute run-root preservation; WIP checkpoint/resume
  in a new worktree; rate-limit cleanup preserving new commits and ignored artifacts.
- Native resume for all four fake CLIs: same cwd/branch/session, WIP checkpoint, live-run
  refusal and `--kill`, safe/read-only preservation, metadata-less recovery, write-free
  dry-run, scope-override rejection, non-repo/space-containing paths, linked session
  activity, repeated native recovery and fresh recovery after a native attempt.
- Missing IDs/files and CLI startup rejection fall back to fresh sessions with retained
  attempt logs. Auth/session-token errors and failures after progress do not replay;
  rate limits never remove the original native worktree. Fake home config sentinels stay unchanged.
- Review regressions: omp `title` before `session`, malformed prefix/bounded header scans;
  Claude/Codex startup auth/transport failures retain the requested ID, even without terminal meta.
  Original-ID native/fresh resumes, direct launches via aliases/different run roots, simultaneous
  resumes and legacy orphan cwd processes cannot bypass ownership; refusal leaves WIP untouched.
  Stale metadata cannot target another workdir; ownership is published before legacy processes drain;
  mocked PID reuse verifies pidfd acquisition precedes identity checks and signals never use bare PIDs.
- Real local TCP sockets in fake agent children: silent-but-connected Pi/omp stay advisory (including
  explicit restart); disconnected/idle trees restart only after a full observed window. Other trees'
  sockets do not count; IPv6 inode joins, unknown/legacy samples and reconnect grace are covered.

`omp --version` reported **omp/18.12.1**. A read-only inspection outside the tests emitted only
record types/top-level keys from one real session: `title` (`pad,title,type,updatedAt,v`) then
`session` (`cwd,id,timestamp,type,version`). `tests/fixtures/omp-title-session.jsonl` uses entirely
invented values; no session content, identifiers, paths or secrets were copied. The runner's
real-provider/native-resume checks were not repeated for these review fixes.

Before fixes, the first 12 parser tests exposed 28 failing subcases; the five supervision
regressions failed; recovery/cleanup regressions reproduced permission expansion,
run-ID overwrite and deletion of agent commits. Historical freeze failure logs
were also replayed through `summarize_run.py` in a temporary HOME: Codex (recorded rc 1),
Claude and omp (forced rc 0 to test exit dishonesty) all yielded nonempty summaries,
`error/conn_error/resumable=true`. Those logs are historical evidence, not new
provider/platform certification. Concurrent same-workdir resumes and stale lock metadata now have
regressions; actual reboot/PID-reuse and native macOS `ps`/`lsof` ownership fallback were not exercised.
Global scheduling, cross-worktree session migration, hostile filesystem races, distributed/network
filesystem locks and macOS/Windows supervision remain untested. Unavailable ownership inspection
fails closed; safe `--kill` needs Linux/Python pidfd support (otherwise stop owners manually).
Unavailable TCP evidence cannot authorize a stall restart. TCP is a heuristic, not provider health
proof; non-TCP waits can still be misjudged and stale sockets can delay recovery.

### Bounded live native-resume check (Linux, 2026-10-19)

One real Pi 1.3.0 check passed using `openai-codex/gpt-6-luna`, low reasoning, a
throwaway Git repo and temporary run/config paths. Existing login was used normally;
extensions, MCP, skills, templates, themes and context-file discovery were disabled
with CLI flags for this check only. No auth files/keys were printed or copied.

- Start: `run-agent.sh --agent pi --id pi-live --cwd <throwaway-repo> --model
  openai-codex/gpt-6-luna --reasoning low --permission full-auto --timeout 180
  --prompt-file <brief>`. The multi-step brief generated a random nonce in tool output
  only, saved a first-phase file without the nonce, then called `sleep 120`.
- Interrupt/recover during that tool call: `resume-run.sh <run-dir> --native --kill
  -- --timeout 90`. Recovery finished `ok` in about 21 seconds, with the **same
  workdir and session ID** and a WIP checkpoint commit.
- The resumed agent recalled the original nonce, verified the saved work/checkpoint,
  and wrote the second-phase file. Its continuation tools did **not** reread session
  files, logs or earlier tool output; the nonce was absent from the saved work file.
- Pi settings/models and the existing plugin config were byte-identical afterwards.
  This is interruption/context-continuity evidence, not a host-suspend or exactly-once guarantee.

Claude/Codex/omp native recovery: **not live-tested** in this bounded pass (no extra
provider runs). Installed help/source checks covered Claude 2.1.295, Codex 0.163.0,
Pi 1.3.0 and omp 18.12.1; fake-CLI tests cover all four. Codex `-C`/`--add-dir` precede
`resume`; `--json`/`-o` are supported on `exec resume`. Pi uses `--session FILE`, omp
uses `--resume FILE`, with the owned session store retained rather than a new empty lookup directory.

## Offline import regression suite (run from the repo root)

Prerequisites: Python 3.11+, Node, Bash, POSIX utilities and ShellCheck already on PATH.
PowerShell is optional. The runner installs nothing and refuses system-wide agent CLIs
in the conventional global bins: use an isolated checkout/container if it refuses.
Do not run import/apply/post-install scripts against your own home to test them.

```bash
(
  h=$(mktemp -d); trap 'rm -rf "$h"' EXIT
  env -i HOME="$h" PATH="$PATH" bash tests/run_import_tests.sh
)
```

`tests/run_import_tests.sh` runs both suites and static checks, returning nonzero
on failure. Every test subprocess gets a fresh environment, temporary HOME/stage,
and stub stores/installers/CLIs; no real logins, providers or global installs.
`MATRIX_NODE` and `PWSH` may select explicit executable paths when passed through
`env -i`. The matrix's subprocess timeout is 35 seconds. Generated bytecode and
captured output live under temporary directories and are removed on exit.
The runner suppresses diagnostics containing fixture canary markers; the auth
suite never preserves a log containing a canary, even on failure.

### Latest Phase 3 evidence (Linux, v0.1.0)

- Python: **60 tests run, 59 passed, 0 failed, 1 skipped** (no `pwsh`).
- Existing Bash auth flow: **26 checks passed, 0 failed**; PowerShell twin skipped.
- `bash -n` on `skills/*/scripts/*.sh tests/*.sh`: passed.
- `shellcheck -s sh` on detect/bundle/export and `tests/fake_home.sh`: passed.
- `shellcheck -s bash` on apply and both Bash test entry points: passed.
- `python3 -m py_compile skills/*/scripts/*.py tests/*.py`: passed (temporary cache).
- `python3 skills/import-agents/scripts/import_plan.py --lint-scripts`: passed,
  including the shared scanner embedded in `detect_local.ps1`.
- Plugin JSON parse and unchanged **0.1.0** version: checked. PowerShell parsing
  is conditional in the runner; it was **not run** here because `pwsh` is absent.

The baseline WIP had 50 Python tests (48 passed, one failure, one PowerShell skip).
The failing fresh-home verification case is fixed; ten further tests cover runtime
packages/plugins, metadata/overlay/SQLite symlinks, identical-auth verification and
preservation of central config.
See [the 64-case matrix](tests/MATRIX.md) for method-level traceability.
Counts above are test methods/checks, not 64 native-platform certifications.

### Coverage and explicit limits

The suite exercises all four CLIs with absent/partial/file-store fixtures; macOS
and Linux store stubs; overrides; Windows-form source paths; Unicode/quoted homes;
consent defaults, failed helpers, shell-only keys, quoting and idempotent key merges;
SQLite WAL snapshots and disabled/corrupt logins; archive traversal, metadata,
large/binary files, forbidden roots and symlinks; conflict preflight and backups;
Codex file-store pinning, Claude account merge, verification and read-only parity.
Offline runtime fixtures cover npm overlays/version mismatch, installer fallback,
full versus production deps, Pi venv pins and Claude plugin SHA/tool-path repair.

- No `pwsh` or native Windows/macOS execution was available. The Windows scanner
  test and parser check are conditional; Windows ACLs/junctions and real Keychain
  prompts remain manual. JXA/bun tests verify dispatch through stubs, not Foundation.
- Login-shell startup is emulated from a fake rc file, never the OS login profile.
  Native fish/zsh, locked Secret Service, consent UI and rotating OAuth refresh need
  separately approved native checks; status success is not a real model request.
- `sqlite3` is stubbed with Python SQLite (including live WAL fixtures); the missing
  executable path is tested. Native database locking is not certified.
- Runtime installer/git/provider behavior is stubbed. No package downloads, real
  CLI invocations or network writes happen in this suite.
- Concurrent imports/refresh, atomic whole-import rollback, hostile filesystem
  races and arbitrary external package trees remain gaps. Own the staging dirs,
  stop source clients before copying SQLite, and review imported executable code.
- An active pi-lens probe reviewed 14 broad `unchecked-throwing-call-python`
  findings as intentional propagation to caller-level exception boundaries; they
  were marked false positives, not hidden by production-code ignore comments.

## Additional unit/manual recipes

These broader plugin recipes are not all executed by the offline import runner.
Use disposable fixture homes for import recipes; real-account checks require
separate approval and are outside this test run.

| Area | How |
|---|---|
| Router | `ROUTE_NOW=… GROK_BOT_AGENTS_STATE=/tmp/s.json python3 skills/delegate-task/scripts/route.py decide …`: light → `self`; bulk → pi; long → omp; hard → claude; review of codex → claude. With claude marked limited: hard → codex, review of codex → pi, `--named claude` → `none`/`ask`. The limit expires at the reset time. pi disabled → omp. `fixed` mode is honored |
| Supervisor | mktemp `HOME` + `AGENT_RUNS_ROOT`, fake `pi` on `PATH`: `supervise.sh` reports `done` (meta ok), `failed` (meta timeout; no process and no meta → `died_no_meta`; stale `agent.pid`), `running next=continue` at slice end, `running next=return` when the budget is used, `running reason=idle_suspected` by default for silent Claude/Codex/unknown runs; `stalled` by default for Pi/omp after no output/session writes, low CPU and no process-owned established TCP connection for `--stall-min`. `--stall-action report|restart` chooses the action but does not bypass the connection guard; recent session writes, live TCP sockets or unknown liveness prevent restarts. `resume-run.sh`: refuses a live run without `--kill` and a finished-ok run; `--dry-run` changes nothing; a real resume kills the tree, commits WIP on `agent/<id>`, relaunches `<id>-r2` from that worktree with the same agent, and numbers the next one `-r3` |
| Pi retry + connection errors | `tests/delegate_pi_retry_test.sh`: mktemp `HOME` + `AGENT_RUNS_ROOT`, fake `pi` on `PATH`, 26 checks: a repo run gets `--approve` and a work-dir `.pi/settings.json` of only `{"retry":{"enabled":true}}`, both files stay out of `git status` and are removed afterwards (`pi_retry: on`); `~/.pi/agent/settings.json` is byte-identical; an existing project `.pi/settings.json`, a project or parent `.agents/skills`, or `--review` means no file and no `--approve`; a bare tracked `.pi` dir keeps its files. Exit 0 with last `stopReason: error` and `WebSocket error` (with partial text mentioning quota) → `error`, `error_kind: conn_error`, `resumable: true`, not `rate_limited`; `WebSocket idle timeout` → `conn_error`; `Invalid API key` → plain `error`; an error followed by a retried `stop` → `ok`. `supervise.sh` → `failed reason=conn_error resumable=1` (plain error → `resumable=0`); `resume-run.sh --dry-run` accepts the run |
| Pi freeze, real (2026-10-19) | `run-agent.sh --agent pi --model openai-codex/gpt-6-luna --reasoning low` in a throwaway repo, 3000-word essay, pi SIGSTOPped 180 s mid-stream: the first message ended `WebSocket error`, pi auto-retried after 2 s and finished (`ok`, `last_stop_reason: stop`, `pi_retry: on`, 234 s, 3801 words); no `.pi` left in the worktree; `~/.pi/agent/settings.json` unchanged. Freeze sweep without retry: 20–75 s recover, 90 s–3 min `WebSocket error`, ≥5 min `WebSocket idle timeout after 300000ms`, exit 0 every time |
| Reset parsing | `mark-limited claude --message "…resets 11pm (Asia/Seoul)"`, `"reset at 10-21 01:00:00 UTC"`, `"retry-after-ms=…"`, unknown text → +1h |
| Review guard | a stub agent that edits the reviewed dir → `status: error`, `review_unchanged: false`; a clean stub → `ok`/`true`; `review.diff` contains uncommitted changes |
| Bridge | `GROK_BRIDGE_ROOT=$(mktemp -d)`: post with `send-task.sh` to a local HTTP listener (refuses a non-600 config, sends the key header), then `bridge.py intake` on the body. Check that bidi, zero-width and control chars are stripped, that a duplicate exits 3, and that `../x`, `.x`, a missing id and non-JSON are rejected. `reply.reply_command`/extra fields are ignored and only reported. `bridge.py reply` sanitizes ANSI/bidi, refuses an existing outbox file or a symlink source; files are 600 |
| Return channel | `GROK_BRIDGE_ROOT=$(mktemp -d) GROK_BRIDGE_TOKEN_FILE=…` + `bridge_server.py serve --port 8788`: `/healthz` 200 without auth, `/ping` 401 without/with a wrong token; `../`, `%2F`, `.x` ids 400/404. Start `/events/<id>` (curl -N), `/result/<id>?wait=60` and `send-task.sh --fetch <id>` (stream and `--no-stream`) before intake, then intake → `progress` (with ANSI/bidi) → `reply`: every client gets the progress lines and the sanitized answer; `--status error` exits 1, unknown id times out with 4, wrong token exits 5, `--import-result-env` merges, backs up and deletes the file. Over the tailnet: `curl --socks5-hostname localhost:1055 http://<tailnet IP>:8787/healthz` |
| Tailscale setup | On a logged-in box: `setup-tailscale.sh` prints `STATUS=ready` with no relogin and the same tailscaled pid; `--status` matches; `bridge-serve.sh status` and `/healthz` stay ok. Fresh install in isolation: `HOME=$(mktemp -d) GROK_BRIDGE_PORT=8799 TS_SOCKS5=localhost:1099 setup-tailscale.sh` downloads + sha256-checks, adds the `.bashrc` line once, prints `LOGIN_URL=` and exits 3; a re-run reuses the URL (one pending `up`); `--wait-login 6` exits 4; `--uninstall --purge --dry-run` lists actions only, then for real empties `~/.local/{bin,opt}` and state. `send-task.sh --check` prints the Tailscale hint (not installed / not connected / box unreachable / reachable) |
| Import | `apply_import.sh <cli> --dry-run` on a staged copy: allowlist only, `._*`/`.DS_Store`/`.pyc` and symlinks skipped, conflicts stop unless `--overwrite` |
| Runtime parity | `parity_check.py` on the box: no gaps after an import. `post_import.py` units with stubs: a `pi` stub listing `npm:@sc/a` → staged `npm/node_modules/@sc/{a,core}` patch the same versions (hoisted and nested), a different version is left; a git marketplace with two commits → the plugin is pinned to the computer's SHA and `installed_plugins.json` updated; a statusLine under a missing `$HOME/...` tool path → the box's `which` copy; Codex `/Applications/...` MCP server → `enabled = false`, one-line `notify` commented out |
| Bundle packages | `bundle_local.sh pi` on a home with local packages (repo and plain dir, one with `.venv`) and `npm:` ones: git-tracked files only for the repo, no `node_modules`/`.venv`, the npm overlay keeps `npm/node_modules/<pkg>` files; manifest has `home`, `venv`, `pip`, `uvtool`, `version` lines; `stage_tar.py` maps `home` roots to `pi/_home/` |
| Local detect / bundle | Fake home (`env -i HOME=$(mktemp -d) PATH=<stub bin>:/usr/bin:/bin sh -s < detect_local.sh`) with auth/config files, a symlinked and a dangling skill, `._x`/`.DS_Store`, `~/.ssh/id_rsa`, a hanging `omp --version` stub: valid JSON, names only, the hang is cut at ~10 s. `bundle_local.sh codex claude pi omp`: tarball 600, symlinked skill dereferenced, dangling link/metadata/SSH key left out before tar (`tar_rc` 0; any tar failure now retains consented exports); `stage_tar.py` stages it and `apply_import.sh --dry-run` plans it; unknown CLI exits 2 |

## Auth import: no secret in any output

`bash tests/auth_import_test.sh` (exit 0 = PASS). Fake Codex/Claude/Pi/omp homes (`tests/fake_home.sh`) in temp
dirs, with every secret value containing a random canary string, a stub `security` (fake Keychain: attributes without
`-w`, the canary with `-w`), stub `op`/`pass`, a fake `uname` for the macOS path, and a key exported only in the fake `~/.bashrc`
(stubbed login-shell startup; no system profiles). It never touches real box or computer logins. It runs detect (macOS and Linux mode),
`export_secrets.sh` (a refused item writes nothing; consented `env:`, `keychain:`, `resolve:` items), `bundle_local.sh`
with defaults and with `--copy-login`/`--include`, `stage_tar.py`, `apply_import.sh keys` (dry and real),
`apply_import.sh <cli>` for all four (opt-in box and default box) and `parity_check.py`, and then checks:
- the canary appears nowhere in the captured stdout + stderr of every script;
- detect reports env names (process / login shell) and Keychain presence; default bundles leave the Claude/Codex
  logins and `.pi/agent/.env` out and say so in `left_out`; no SSH key; bundle and export files are 600;
- `~/.agent-keys` is 600, same-name lines replaced without duplicates, other lines kept, a re-merge is a no-op, values
  match the source (compared in a subshell); staged `secrets.env` is gone;
- Codex `config.toml` has file credential stores; Pi `models.json` / omp `models.yml` `!cmd` keys point at env names;
  a copied Claude login merged `oauthAccount` + `hasCompletedOnboarding` (and nothing else from `~/.claude.json`);
  the default import warns "log in on the box" with no two-machine warning;
- `parity_check.py` `auth` reports names only: unset env names, missing `!cmd` tools, remote MCP servers to log in, the ToS flag.

Windows twin: `env -i HOME=<fake> USERPROFILE=<fake> PATH=<stubs>:/usr/bin:/bin pwsh -NoProfile -File
skills/import-agents/scripts/detect_local.ps1` on the same fake home must give the same inventory, no canary in the output,
also when wrapped as `& ([scriptblock]::Create(@'…'@)) -Clis codex,pi`. PowerShell was unavailable in the Phase 3 run. Not yet run on real Windows or real macOS
(Keychain prompt, JXA Foundation fallback when neither node nor bun is installed).

## Historical end-to-end (real logins on the box, 2026-10-18)

Historical evidence only; these checks were **not** rerun during the isolated
import-matrix work. Never use the offline test runner to touch real accounts.

| Scenario | Result |
|---|---|
| Codex, non-repo report | ok, 16 s |
| Codex, repo task (`mul()` + test) in worktree | ok, 9 s, tests pass |
| Native model/reasoning | no flags in `cmd.txt`; Pi used its imported settings |
| Import from macOS: Codex, Pi, omp logins + 19 symlinked skills via `--dereference` | installed, OK runs pass |
| Auto route, light task | stays with the bot |
| Auto route, bulk translation (40 keys) | pi, ok, 51 s, all keys present |
| Auto route, hard task | claude → `rate_limited` (reset recorded, empty worktree removed) → codex, ok, 27 s, tests pass |
| Review of the Codex branch with pi (`--review`) | ok, 27 s, verdict approve with 1 low finding, `review_unchanged: true` |
| Claude Code headless run | not yet: the test account was rate-limited |
| Re-import parity audit from macOS (Pi packages, Claude plugins, Codex Mac-only MCP) | Pi loads 9 packages (scrapling, subagent, workflows, pi-doctor tools), no model warnings, OK 9 s; Codex OK 4 s; omp OK 3 s; `claude mcp list` all connected |

## Manual checks after installing from the marketplace

1. Type `/` in Grok Bot: the five skills are listed.
2. "set up codex and pi": multi-select, sign-in choice, OK test.
3. "have pi translate README to Korean", "analyze this log folder" (auto route), "review that branch".
4. "set claude reasoning to max" → `~/.grok-bot-agents/config.json` changes.
