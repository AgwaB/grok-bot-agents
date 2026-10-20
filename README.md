<p align="center"><img src="assets/logo.png" width="96" alt="grok-bot-agents logo"></p>

# grok-bot-agents

A Grok Bot skills plugin with a thin Python `gba` controller that installs, signs in and drives **Claude Code, Codex CLI, Pi and
omp (Oh My Pi)** on the Grok Bot box (the bot's cloud computer).

Grok Bot keeps light work for itself and hands **heavy work, coding or not,** to a CLI agent:
research and analysis, data and file processing, bulk transforms, long drafts and translations, log
analysis, long or unattended jobs. You can name the CLI ("have codex fix this") or let the bot route
by cost and difficulty. Runs are headless and isolated, and come back as a short summary plus
artifact paths.

한국어: [README.ko.md](README.ko.md)

## Skills

| Skill | What it does |
|---|---|
| `setup-agents` | Detect the CLIs already set up on your computer, pick CLIs (multi-select), install only those, import them all by default (settings, consented API keys, Pi/omp logins; Claude Code and Codex log in on the box unless you opt in to copying), or sign in per agent (subscription takeover, API key via masked input), keep model/reasoning native, write the config, run an OK test per CLI |
| `import-agents` | Copy existing CLI settings, consented API keys and logins from your registered computer (macOS, Linux; Windows works but is less tested) to the box through a consent-bound, resumable per-file controller, with your approval for each copy |
| `configure-agents` | Change models, reasoning, permission mode, routing, logins/keys and each CLI's native config |
| `delegate-task` | Decide light vs heavy, route to a CLI (or the one you named), write a standalone brief, run it headless, report back. Includes read-only code review, and supervisors that keep long runs alive through box idle pauses |
| `agent-bridge-webhook` | Two-way handoff with agents on another machine or CI through a webhook routine; answers stream back to the caller's terminal over Tailscale (`grok-send-task --wait`), with sanitizing in both directions |

## Flow

1. **Install** the plugin from the Cursor marketplace (Settings → plugins in Grok Bot).
2. **Set up** (`/setup-agents`): the bot checks which CLIs your computer already has, you choose CLIs, and
   offers imports, and moves only files selected on a whole-file consent card, with approval for each transfer:
   settings, approved API-key exports (into `~/.agent-keys`, mode 600), and selected Pi/omp auth files.
   Claude Code and Codex subscriptions get their own login on the box by default; copying them is an opt-in. Keys
   and tokens **never go into chat**. Model and reasoning default to
   **native**: no flag is passed and each CLI's own (or imported) settings apply.
3. **Delegate** (`/delegate-task`): "have pi translate the README", "analyze this log folder", "fix this bug".
4. **Change settings** (`/configure-agents`): "set claude reasoning to max", "turn off omp".

## Light vs heavy

A task is heavy if **any** of these hold; otherwise the bot does it itself:
- about 5+ files, or a whole repo or folder
- about 50 KB+ of input (big logs, CSV/JSON, long PDFs)
- about 10+ tool or shell steps, or a run → check → fix loop
- 5+ minutes, unattended or overnight, or a recurring routine
- token-heavy bulk generation

## Routing

`skills/delegate-task/scripts/route.py decide` applies the rules, config and rate-limit state.
It returns `decision`, nullable `agent`, optional `ask_first`, and optional object-shaped `skipped`.
`--type bulk` alone is not heavy: also pass `--bulk` or a threshold.


| Heavy-work type | First choice | Fallback |
|---|---|---|
| Bulk / repetitive / token-heavy | `routing.cheap` (Pi, then omp) | a frontier agent |
| Long or unattended | omp | Claude Code / Codex |
| Hard reasoning, final code, nuanced writing | `routing.hard` (Claude Code, then Codex) | omp / Pi, strongest model |
| Review / second opinion | the frontier agent that didn't produce it | cheap agent, read-only |

A named CLI always wins (or the bot asks if it's unavailable). When a run hits a subscription or rate
limit, the runner records the reset time in `~/.grok-bot-agents/state.json` and the router skips that CLI
until then.

## Isolation

- **git repo**: every run gets its own worktree and branch `agent/<run-id>`. A rate-limited run that
  changed nothing removes its own worktree.
- **no repo / non-coding**: runs in `/workspace/agent-runs/<run-id>/work`; inputs are added read-only
  with `--input`.
- **review**: `--review DIR` runs read-only in place with a generated `review.diff` (uncommitted and
  untracked files included). A before/after fingerprint fails the run if anything changed.

## Permission modes

| | full-auto (default) | safe | read-only |
|---|---|---|---|
| Codex | `--dangerously-bypass-approvals-and-sandbox` | `-s workspace-write` | `-s read-only` |
| Claude Code | `--permission-mode bypassPermissions` | `acceptEdits` | `plan` |
| omp | `--approval-mode yolo` | `--approval-mode write` | `write` + `--tools read,grep,glob` |
| Pi | no approval system | same | `--tools read,grep,find,ls` |

Push, merge, PR, deploy, publishing and sending messages always need your OK, even in full-auto.
Full-auto gives the agent unrestricted shell, network and login-session access on the box. Worktrees
isolate files, not the system.

## Config: `~/.grok-bot-agents/config.json`

Shared by every bot on the box. Keys are never stored, only env var names (`key_env`).

```json
{
  "version": 1,
  "enabled": ["claude", "codex", "pi"],
  "permission_mode": "full-auto",
  "worktree_per_task": true,
  "timeout_sec": 1800,
  "wake_auto_max_sec": 600,
  "agents": {
    "claude": {"auth": "oauth", "model": null, "reasoning": null},
    "codex":  {"auth": "imported", "model": "native", "reasoning": "native"},
    "pi":     {"auth": "api_key", "key_env": "DEEPSEEK_API_KEY", "model": "deepseek/<model-id>", "reasoning": "low"},
    "omp":    {"auth": "imported", "model": "native", "reasoning": "native"}
  },
  "routing": {"mode": "auto", "cheap": ["pi", "omp"], "hard": ["claude", "codex"], "review": "other-frontier", "prefer": [], "fixed": null},
  "runs_dir": "/workspace/agent-runs"
}
```

## Importing from your computer

| CLI | Imported | Notes |
|---|---|---|
| Codex | `config.toml` (pinned to file credential stores), `AGENTS.md`, `prompts/`, `skills/`, `rules/`; an API-key `auth.json` | ChatGPT login: `codex login --device-auth` on the box by default; copying `auth.json` (or the keyring item) is opt-in |
| Claude Code | `settings.json`, `CLAUDE.md`, `agents/`, `commands/`, `skills/`, `hooks/`, plugins, `mcpServers` from `~/.claude.json` | Login: `claude auth login` on the box by default; copying (file, or the macOS Keychain item) is opt-in |
| Pi | `~/.pi/agent/auth.json`, package auth files (`*auth*.json`, `*token*.json`, `web-search*.json`, …), `settings.json`, `models.json`, packages (+ `AGENTS.md`, `prompts/`, `skills/`, `extensions/`) | Needs `@earendil-works/pi-coding-agent` (Node ≥ 22.19) |
| omp | `~/.omp/agent/agent.db`, `config.yml`, `models.yml`, `mcp.json` | Quit omp first; `-wal`/`-shm` come along |

Scanners read config/auth contents internally but emit only names (`detect_local.ps1` on Windows).
Consent is **whole-file**, including every provider/secret inside an auth/config file; omp DB/WAL/SHM is one unit.
Partial provider selections are rejected. Env/indirection exports select names before creating the export file.
The controller uses unique private staging, SHA256 receipts and approved per-file CopyToBox transfers.
Legacy bundles remain an explicit fallback for fully consented symlinked skills/external packages, not partial
selections; nondefault `CODEX_HOME`, `CLAUDE_CONFIG_DIR` or `PI_CODING_AGENT_DIR` requires per-file copying.
Windows is less tested and has no POSIX bundle/export twin: use per-file import or box login/masked keys.
Never import browser logins, SSH/GPG/cloud credentials, history, caches or unrelated files.
Sharing OAuth across machines can log one out on refresh; Claude/Codex default to separate box login.

Settings-only imports preserve central overrides. Direct `apply_import.sh` resets model/reasoning to native
only after imported auth passes its status check. `gba import` applies offline (`--no-verify`) and preserves
central defaults; inspect existing overrides before claiming imported preferences apply.

## Answers back in your terminal (Tailscale)

`grok-send-task --wait "task"` on your Mac (or inside Claude Code / Codex) streams progress and prints the answer in the
same terminal, over your tailnet. The bot sets it all up: `setup-tailscale.sh` installs Tailscale on the box (no root,
sha256-checked static build), starts it in userspace mode, and prints a login link. You approve that link, the bot waits
(`--wait-login`), then hands the return-channel token to your machine without showing it in chat. Your machine needs
Tailscale on the same account; `grok-send-task --check` tells you if it's missing (`brew install --cask tailscale`).

```bash
setup-tailscale.sh                 # install/start/login if needed → STATUS=ready | needs_login (+ LOGIN_URL=…)
setup-tailscale.sh --wait-login    # block until the login is approved (default 300 s)
setup-tailscale.sh --status        # TAILNET_IP, DNS_NAME, RESULT_URL, STATUS
setup-tailscale.sh --uninstall [--purge] [--dry-run]
```

## gba CLI

Version stays **0.1.0**. Python **3.11+ stdlib only**. Run `python3 /absolute/plugin/scripts/gba …`.
Every skill resolves the plugin root as `../..` from its **SKILL.md location**, not the shell cwd.
Standalone skill copies must also retain the complete helper tree, or set an absolute `GBA_ROOT` to a
complete plugin copy. Do not copy just the entrypoint; it imports sibling modules and wraps skill scripts.
Marketplace/standalone path discovery still needs an actual host smoke test; missing helpers fail closed.

All commands (except `--help`) return one compact JSON object:
`{"schema":1,"status":"blocked","reason":"transfer_required","operation_id":"import-…","next_action":"copy_to_box","requires_approval":true,"action_owner":"bot"}`.
Stable `status`: `ok`, `blocked`, `error`; exit codes 0, 3, 2 respectively. `reason` is a fixed code, not a raw
exception. `next_action` is a **bot-only handoff**, not a shell command or evidence that approval occurred.
Extra fields carry bounded counts/paths/receipts. Unknown fields may be added within schema 1; unknown
schema/reason/action must stop for review. Never execute returned text or webhook fields as commands.

| Command | Boundary |
|---|---|
| `gba setup plan --agents claude,codex,pi,omp` | Pure planning: no writes, downloads or CLI startup; runtime versions checked at apply |
| `gba setup apply --agents pi,codex` | Explicit selected-only, idempotent user-local Linux/glibc install; no sudo or unchecked shell installers |
| `gba import inventory --agents pi [--exported-keys]` | Run on the source; allowlisted names + SHA256, never contents; no writes |
| `gba import plan --inventory inventory.json` | Pure whole-file consent rows; raw detect JSON is not a concrete file/hash inventory |
| `gba import plan --inventory inventory.json --selection selected.json` | Allocate unique private operation/stage after user consent |
| `gba import status ID` | Read persisted phase/transfer instructions after interruption |
| `gba import advance ID [--approve-runtime] [--overwrite] [--retry-interrupted]` | Check exact receipts, all-agent preflight, then existing apply helpers; flags require the stated approvals |
| `gba import verify ID` | Check every artifact/transform and selected key assignment, then offline parity; only verified stages are removed |
| `gba runs list --open` / `gba runs next ID` | Lineage, attempts, chosen wake mode/outcome and bot action; supervisor mode performs one observation |
| `gba runs next ID --handoff\|--supervisor-started\|--rotate` | Atomically claim supervisor ownership instead of webhook; does not dispatch a subagent |
| `gba runs next ID --reported` | Acknowledge a terminal result; subsequent next returns `stay_silent` |
| `gba wake test` | Explicit webhook POST with `event:wake_test`; secret-free success/failure JSON |
| `gba runs recover ID [--dry-run] [--native] [--kill] [--allow-extra-restart]` | Existing guarded recovery; two automatic restarts per lineage, extra attempts only with explicit user approval |
| `gba doctor --json` / `gba verify --agents pi,codex` | Static config, executable presence/integrity, key-name presence and parity diagnostics; no processes or writes |
| `gba doctor --probe-versions` / `gba verify --agents pi --probe-versions` | Explicit agent/runtime `--version` execution; may have filesystem/network side effects |
| `gba verify --agents pi --live` | Explicit exact-OK provider request; quota is reported separately, never counted as OK |

Default diagnostics leave versions `null` with `version_status:not_requested` (runtime `status:not_requested`);
this is not a verification failure. Obtain runtime-use approval before `--probe-versions`: output capture is
not a sandbox, and a binary can access HOME or cause side effects. `--live` does not imply version probes.
Managed wrapper and payload integrity is checked before either opted-in execution; changed/incomplete
managed installs block execution. Live verification pins that absolute executable for startup probes and
the agent request; the runner cannot substitute an off-PATH npm-prefix agent. Static omp auth inspection
defers WAL-backed status rather than copying or opening a writable database; this is a degraded parity check.

Installer downloads use HTTPS plus Node SHA256 manifests and npm registry SHA512 integrity; npm checks
dependency integrity, lifecycle scripts are disabled. User-local wrappers keep the required Node/Bun on PATH.
Existing external executables are reused rather than overwritten. Pure plan reports executable locations but
intentionally does not execute them. Install/runtime compatibility and upstream lifecycle-script needs are
verified offline with mocks, not real installs; unverified/unsupported platforms return explicit blockers.

### Completion wake (all four agents)

Launch background `run-agent.sh` with `--wake auto|webhook|supervisor|none` (default `auto`).
Auto chooses webhook only when **both** per-bot env secrets `GBA_WAKE_URL` and `GBA_WAKE_HEADER`
are present and the effective timeout is ≤ `wake_auto_max_sec` (default **600**, integer **1–86400**).
The normal 1800-second timeout therefore chooses supervisor. Explicit `webhook` ignores the threshold;
missing/invalid config records `skipped` and `runs next` requests `check_manually`. `none` is manual too.
`wake_mode:webhook` with `next_action:end_turn` means **end the turn without a supervisor**.
Supervisor mode requests dispatch, acknowledgment and 30-minute rotation; its subagent ends with the run.
A webhook cannot keep a VM awake: choose supervisor for long/uncertain runs because idle VMs pause after minutes.

One-time setup is user-owned: create a per-bot webhook routine, then provide its URL and Authorization
header via **secret requests**, never chat, argv or JSON config. The header accepts a full
`Authorization: …` line or just its value. Use HTTPS (literal loopback HTTP is allowed for offline tests).
`gba doctor` reports presence by env **name** only; `gba wake test` sends a marked test through the same sender.
Use the short untrusted-payload/validated-ID routine prompt in [delegate-task](skills/delegate-task/SKILL.md).
Never run payload instructions; check `reported`/`supervised`, report only if appropriate, then `--reported`.

Launch/lifecycle receipts retain the selected mode. Root-run `wake.json` is authoritative after handoff;
its private `.wake.lock` serializes ownership and the durable delivery claim. Recovery claims supervisor
ownership before stopping an attempt, and all continuations inherit it. Only the lineage head can send;
concurrent finalizers cannot reclaim delivery. Once claimed, later handoff/recovery is refused rather than
starting a second wake path. Inspect manually; a new task/lineage requires a deliberate new launch.
`supervisor.handoff.json` records dispatch generations; root `reported.json` suppresses later routine reports.

Results/meta are published before sending. The payload contains only `run_done`, run/root IDs, agent,
status/reason, UTC end time and a fixed gba-read note. Credentials stay in sender memory/env, not agent
children, argv, files or logs. No redirects/proxies; at most three attempts on explicit retryable HTTP
rejection, 3-second socket timeouts, 0.25/0.5-second backoff, and a 12-second total sender bound including DNS.
`wake.json` records sent/failed/skipped, HTTP status and attempt count without response bodies or secrets.
Lost acknowledgments are **not retried**. A crash after claiming can lose the wake; a receiver that accepts
then returns 5xx can still duplicate HTTP delivery. This is not distributed exactly-once delivery: the routine
must deduplicate reports. SIGTERM/INT/HUP finalize a killed run; explicit USR1 stops it as stall-terminal.
Advisory stalls do not announce completion. SIGKILL of the runner, host loss and native non-Linux execution
remain limits; see [TESTING.md](TESTING.md). Live verification uses `--wake none` to avoid unsolicited wakes.

### Import inputs and interruption

`inventory` output is directly consumable by `plan`. Without source Python/plugin, assemble the same **metadata**
from the existing scanner plus file-name enumeration and SHA256 (e.g. PowerShell `Get-FileHash`):

```json
{"schema":1,"os":"macos","home":"/Users/alice","files":[
  {"agent":"pi","rel":"auth.json","sha256":"<64 lowercase hex characters>"}
]}
```

Each row is one concrete allowlisted regular file, never a directory/provider selector. Optional `source` is an
absolute source path ending in the same allowlisted relative path (for CLI-dir overrides or exported logins).
`agent:"keys", rel:"secrets.env"` additionally requires `names:["OPENAI_API_KEY"]`; actual exported names must
match exactly. `keys:rewrites.txt` is allowed only for selected destination files and selected exported names.
`selected.json` is an array such as `["pi:auth.json"]`, not a provider sub-selection. Use all existing omp sidecars;
a sidecar inventory without `agent.db` is rejected.
Inventory and selection files must not contain values. Hashes bind bytes, not user approval or source identity;
the bot must present the card and use the approved machine/path. Never manufacture consent from JSON.

State is private under `~/.grok-bot-agents/imports/import-<uuid>/`; transfers target its `stage/`.
Missing/changed/extra receipts block before apply. `--approve-runtime` acknowledges executable packages/hooks
and config code; allowlisting is not a trust review. Selected-file apply never runs broad post-install repairs
or dependency installers; it imports selected files with deterministic home/key-reference/file-store transforms.
Runtime repairs are deferred in sanitized `reports` instead of rewriting unselected settings. Plugin metadata,
npm overlays and external Pi `_home` manifests require a separately reviewed full-consent legacy operation.
File/key replacements and stale destination omp sidecar removal require `--overwrite`, with private backups.
Selected omp groups are reconciled even when the DB bytes match; omitted WAL/SHM must remain absent,
including after retries. Real apply also makes byte-identical selected files private (0600, or 0700 for
ordinary executable files), without rewriting their contents; dry-run leaves permissions untouched.
Claude global inventory and merge both use `$CLAUDE_CONFIG_DIR/.claude.json` when configured, otherwise
`~/.claude.json`; they do not silently substitute another profile.
Warnings and backup paths persist in `status`/`verify`; secret values and private verification hashes do not.
An interrupted helper leaves phase `applying`; inspect effects and explicitly use `--retry-interrupted`.
Helper process-group ownership is recorded before execution. Live writers block all controller imports;
timeouts/handled signals drain the whole group before unlock or scratch cleanup. Unknown launch ownership
fails closed for manual inspection. Legacy scripts/external writers are not covered by this local lock.
Every selected destination/transform and key assignment must still match private preflight receipts before
staging cleanup, including keys-only imports; missing evidence retains staging. Retries are not whole-import
rollback or exactly-once execution. Keep source exports private until verified, then delete only this import's exports.

Run recovery stores original inputs/timeout and a durable lineage ledger; resuming the original ID or choosing
a different continuation ID cannot reset the cap, even before child receipt publication. Conflicting ledger
ownership fails closed. Native/fresh fallback consumes the same budget. Effective inputs are recorded at launch;
recovery never adds the original repository checkout implicitly. Legacy receipts restore explicit inputs only.
Supervisors must still be real bot subagents rotated every 30 minutes; background processes alone do not keep
this VM awake. Recovery checks interrupted effects before replay and retains existing worktree/process guards.

`agents_config.py set` rejects known key markers and long high-entropy values recursively, validates env names,
and prints only a count. Detection is heuristic (false positives/negatives remain possible), not secret storage.
Bridge `git`/`command` reply types are recorded only; **outbox is the only implemented delivery** and payload
`reply_command` fields are never executed. Routine/transport setup remains a bot procedure, not a new gba command.

## Layout

```
.cursor-plugin/plugin.json
assets/logo.svg, logo.png
scripts/gba, gba_{import,import_process,setup,runs,doctor}.py
skills/
  setup-agents/SKILL.md
  import-agents/       SKILL.md, scripts/{import_plan.py, detect_local.sh, bundle_local.sh, stage_tar.py, apply_import.sh}
  configure-agents/    SKILL.md, scripts/agents_config.py
  delegate-task/       SKILL.md, scripts/{run-agent.sh, route.py, summarize_run.py, supervise.sh, resume-run.sh}, references/
  agent-bridge-webhook/ SKILL.md, scripts/{bridge.py, bridge_server.py, bridge-serve.sh, setup-tailscale.sh, sanitize_result.py, send-task.sh, bridge.env.example}
```

## Testing and release

See [TESTING.md](TESTING.md) for the checks and the end-to-end matrix, and [CHANGELOG.md](CHANGELOG.md).
To publish: push to a public GitHub repo, submit at https://cursor.com/marketplace/publish and tick
Grok Bot as a target.

## Open questions

- Whether `minClientVersions` needs a Grok Bot key
- Whether the app's multi-select card and masked secret input work from plugin skills
- Actual marketplace/standalone installation smoke test for absolute `GBA_ROOT` helper discovery
- Claude Code end-to-end on a non-rate-limited account (Codex, Pi and omp are verified)

## License

MIT © AgwaB
