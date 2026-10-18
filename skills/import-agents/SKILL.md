---
name: import-agents
description: Import the user's existing Codex, Claude Code, Pi or omp setup from their own registered computer (macOS, Linux or Windows) into the Grok Bot box. Copies an allowlist of config files, static API keys (after one name-only consent card, into ~/.agent-keys) and Pi/omp logins; Claude Code and Codex subscription logins default to a separate login on the box, with copying as an opt-in. Uses ListMachines and CopyToBox (each copy needs the user's approval), never shows secret values, backs up anything it would overwrite, rewrites home paths, updates ~/.grok-bot-agents/config.json and verifies each CLI. Use when the user says "import my codex/claude settings", "copy my login from my laptop", "use my local setup", or picks "import from my computer" during setup-agents.
---

# Import agent setups from the user's computer

The CLIs must already be installed on the box (`setup-agents` 2-1). Scripts live in `scripts/`:
- `import_plan.py --os macos|linux|windows --home <user home> --cli <list> [--json]` prints what to copy and where to stage it. It reads nothing.
- `apply_import.sh <cli> [--src-home <user home>] [--dry-run] [--overwrite] [--force-auth] [--keep-stage] [--no-verify]`
  installs from `/workspace/uploads/agent-import/<cli>/`. Exit 0 ok, 4 conflicts, 5 installed but the box's omp login was kept.
- `stage_tar.py <bundle.tgz> [--cli <list>]` splits a tarball of home-relative paths into those stage dirs.
  Use it for `bundle_local.sh` / `--dereference` bundles made on the user's computer.
- `post_import.py` (run by `apply_import.sh`, not by hand) makes the import run like on the computer: Pi `npm:`
  packages, npm overlay, package venvs; Claude marketplaces + enabledPlugins pinned to the computer's SHA, hook /
  statusLine tool paths; Codex Mac-only MCP servers and `notify` disabled; MCP server binaries the computer had as
  `uv tool`s; a box CLI older than the computer's is flagged.
- `parity_check.py [--cli <list>] [--no-run]` is read-only: does everything each CLI's config points at exist on
  the box? JSON gaps with impact `breaking` / `degraded` / `cosmetic`; exit 1 on a breaking gap. `apply_import.sh`
  runs it per CLI (`parity`).
- `detect_local.sh`, `export_secrets.sh` and `bundle_local.sh` run **on the user's computer** (macOS/Linux, POSIX sh):
  Read the file and pass its content verbatim to one machine Shell call as a heredoc, `sh -s -- <args> <<'EOF'` … `EOF`.
  `detect_local.sh [--no-login-shell] <clis>` is read-only and prints JSON per CLI (`installed`, `version`, `has_auth`,
  `importable_auth`, `has_config`, `login_store` file/keychain/keyring/none, `present` names, `symlinks`, `auth_status`,
  `note`) plus `secrets`, a **name-only** inventory: `inventory.items` (login files with `type` oauth/api_key, Pi
  OAuth providers with `expired`, `!cmd`/`op://` indirections with the tool's first word only, literal keys in configs by
  key path, package auth files, remote MCP servers that need OAuth; each with `rotation` and `default`),
  `inventory.env_refs` (env names configs reference), `env_set` (which candidate names are set, in the process or the
  user's interactive login shell), `stores` (Keychain / Secret Service presence of `Claude Code-credentials`,
  `Codex Auth`, `Codex MCP Credentials`). For omp it reads only provider names and usable/disabled from a temp copy of
  `agent.db` (needs `sqlite3`, built into macOS). **Windows:** `detect_local.ps1` prints the same JSON; pass it as
  `& ([scriptblock]::Create(@'` … `'@)) -Clis claude,codex,pi,omp` in one PowerShell machine Shell call.
  `export_secrets.sh <items>` exports exactly the consented items (see Auth below) into `~/.agent-import-secrets/`
  (600 files) and prints names, status and byte counts only. `bundle_local.sh [--copy-login claude,codex]
  [--include <path>] [--skip <path>] <clis>` tars only the allowlist, dereferenced, plus that dir (then deletes it), into
  `~/agent-import.tgz` (600) and prints size, member count, entry names and `left_out` (what stayed on the computer). For Pi it also takes the packages
  `settings.json` `packages` points at: local ones (git-tracked files of a repo, else all but `node_modules`/`.venv`/`.git`),
  wherever they live under the home dir, and the files of the `npm:` ones Pi installed (local patches included). A
  manifest in the bundle records package roots outside `~/.pi/agent`, venv pins, `uv tool` pins and CLI versions. `import_plan.py --lint-scripts` checks
  that the detect and bundle scripts cover every allowlisted path, and that the `.ps1` embeds the same scanner.
- `apply_import.sh keys [--dry-run]` merges the staged `_secrets/secrets.env` into `~/.agent-keys` (names only in the
  output). The first real `apply_import.sh <cli>` also does it on its own.

## Auth: what moves, and how

**Default strategy per kind** (the detect inventory gives each item's `default`):

| Auth kind | Default | Opt-in |
|---|---|---|
| Static API keys in CLI auth files (Pi/omp `/login` keys, a Codex API-key `auth.json`) | Copy the file | — |
| Env-var API keys (names configs reference, plus well-known provider names set in the login shell) | Export the consented names into `secrets.env`, merged into the box `~/.agent-keys` (600, no duplicates) | Masked secret input / `read -rsp` on the box (setup-agents 2-2) |
| `!command` / `op://` keys (Pi `auth.json`, `models.json`, omp `models.yml`) | If the tool works on the box (e.g. `op` signed in), keep it. Otherwise `resolve:` on the computer into an env name, and the box config is rewritten to that name | — |
| Literal keys inside config files | Ride along in the (sensitive) config file; report the key paths only | — |
| **Claude Code subscription** (any OS) | **Box login:** `claude auth login` in a box tmux, finished via Take over (setup-agents recipe) | `claude setup-token` → `CLAUDE_CODE_OAUTH_TOKEN` via masked input. **Copy (advanced):** `--copy-login claude` (Linux/Windows file), or `keychain:claude` on macOS. Also carries MCP OAuth |
| **Codex ChatGPT login** | **Box login:** `codex login --device-auth` | **Copy:** `--copy-login codex` (file, plus `.credentials.json` MCP OAuth), or `keychain:codex` when `login_store` is `keyring` |
| Codex / Claude MCP OAuth | `codex mcp login <server>` / `/mcp` in claude, on the box | With the login copy above |
| Pi / omp OAuth providers | Copy, with the per-provider check (`expired` in Pi, `disabled` in omp: re-login there first) | omp auth broker on the box (`omp auth-broker serve`) |
| Pi package auth files (`*auth*.json`, `*token*.json`, `*credential*.json`, `web-search*.json`) | Copy | `.env` in the agent dir: off unless `--include .pi/agent/.env` |
| Browser-profile/cookie logins (NotebookLM etc.), SSH, GPG, cloud credentials (`~/.aws`, gcloud) | 🚫 never: log in on the box | — |

**One consent card** (a multiSelect question card, before export and bundling). One row per secret that would move:
name, source (file / Keychain / env / `!cmd` tool), box destination, strategy. Pre-tick the defaults above; leave the
opt-in copies unticked. Show names only, never values. Nothing moves that isn't ticked. On macOS the Keychain also
asks "allow `security`" per item, a second, OS-level consent. Then map the ticks to arguments:
- ticked env names → `export_secrets.sh env:NAME …`; ticked `!cmd` keys → `resolve:<pi|omp>:<file>:<provider>=<ENV_NAME>`
  (pick a clear name such as `DEEPSEEK_API_KEY`); macOS/Linux store logins → `keychain:claude` / `keychain:codex`.
- ticked Claude/Codex login files → `bundle_local.sh --copy-login claude,codex`; unticked default items → `--skip <path>`;
  ticked off-by-default files → `--include <path>`.
- Nothing ticked for export → skip `export_secrets.sh`.

**Say on the card when a rotating login copy is offered:** the same login then lives on two machines. These CLIs refresh
OAuth tokens, and whichever side refreshes first can log the other out (seen with omp `refresh_token_reused`; OpenAI
says not to share one `auth.json` across machines: https://developers.openai.com/codex/auth/ci-cd-auth/). For Claude,
also: Anthropic allows subscription sign-in only through its own flow
(https://code.claude.com/docs/en/legal-and-compliance#authentication-and-credential-use), so copying its token is a
grey zone; the box login is the clean path. Flag Anthropic subscription OAuth used inside Pi or omp (`tos_flag`) as a ToS risk; don't promote it.

Login callbacks on the box (`claude auth login`, `codex login`, `codex mcp login`) finish in the box's own browser or
terminal via **Take over**; the localhost callback works because the browser is on the box.

## Rules

- Copy only what `import_plan.py` lists. Never copy SSH or GPG keys, cloud credentials (`~/.aws`, `~/.netrc`,
  `~/.git-credentials`), browser profiles or other dotfiles, even if asked in passing. `apply_import.sh` skips
  anything off its allowlist, plus sessions, history, logs and caches.
- Never `cat`, `Read`, print or summarize auth files (`auth.json`, `.credentials.json`, `agent.db`, Pi package auth
  files, `secrets.env`), files `import_plan.py` marks `sensitive` (configs that can hold keys), or `~/.agent-keys`.
  Report only names, sizes and the scripts' JSON. Never run `security … -w`, `secret-tool lookup`, `printenv NAME` or a
  `!command` yourself: only `export_secrets.sh` does, with output going straight into 600 files.
- Export only what the user ticked on the consent card. `export_secrets.sh` refuses anything off its item grammar.
- Don't overwrite box config silently. If `apply_import.sh` exits 4 (conflicts), list the files, ask, then rerun
  with `--overwrite`, which saves `<file>.bak.<timestamp>` first.
- Claude Code and Codex subscription logins default to a box login. Copy them only when the user ticks the opt-in,
  after the two-machine and (Claude) ToS note above. Pi/omp logins are copied; the same two-machine note applies.

## Steps

1. **Pick the computer.** `ListMachines`. Use a connected desktop (the one the user's message came from by default).
   Phones can't do this. Run `detect_local.sh` there (one machine Shell call; `detect_local.ps1` on Windows); it gives
   the OS, home dir, what exists and the name-only secret inventory.
   **Windows:** supported, but less tested than macOS/Linux (no native Windows run yet). Tell the user once, in one
   line, that import from Windows may not go smoothly and that setup-agents' box login or API key is the fallback
   if a step fails. Then continue normally.
2. **Pick CLIs** (one multiSelect card): Codex, Claude Code, Pi, omp. Pre-check the ones found locally.
3. **Plan + consent card.** Show a short table from the detect JSON (`present` names per CLI; Claude/Codex get a box
   login by default). Then the one consent card (Auth above) built from `secrets.inventory`, `env_refs` and `env_set`:
   env names that are set, `!cmd` keys to resolve, package auth files, and the unticked login-copy options.
   `python3 scripts/import_plan.py --os <os> --home <home> --cli <chosen>` lists the full allowlist and stage paths.
4. **Prepare the source.**
   - **omp:** quit omp on the computer first, since `agent.db` is a live SQLite file. Also copy `agent.db-wal` and `agent.db-shm` if present.
     If detect's `auth_status` shows a provider `disabled`, ask the user to run `omp`, then `/login` on that computer
     before bundling, then rerun detect.
   - **Claude / Codex:** by default nothing of their login moves; plan the box login (setup-agents headless recipe).
     On opt-in: macOS Claude is the Keychain item `Claude Code-credentials` (`keychain:claude`); Linux and Windows use
     the file `~/.claude/.credentials.json` (`--copy-login claude`). Codex with `login_store: keyring` → `keychain:codex`.
   - **Exports:** if anything was ticked for export, run `export_secrets.sh <items>` (one machine Shell call). Relay
     its per-item status (`ok`/`missing`); don't retry a denied Keychain prompt.
5. **Copy (default: one bundle).** Run `bundle_local.sh [--copy-login …] [--skip …] [--include …] <chosen>` on the
   computer (it also bundles and deletes `~/.agent-import-secrets`), `CopyToBox` `~/agent-import.tgz` →
   `/workspace/uploads/agent-import.tgz` (one approval), `rm -f ~/agent-import.tgz` on the computer, then
   `python3 scripts/stage_tar.py /workspace/uploads/agent-import.tgz --cli <chosen>` and delete the box copy.
   Per-file fallback (Windows, or `CODEX_HOME`/`CLAUDE_CONFIG_DIR` set): one `CopyToBox` per file, with `box_path` set
   to the plan's `stage` path; the user approves each copy. Copy only consented auth files; for Windows env keys
   use the masked input on the box (step 8), since there is no `export_secrets` for Windows yet.
6. **Dry run.** `bash scripts/apply_import.sh keys --dry-run` (env names to add/replace in `~/.agent-keys`), then
   `bash scripts/apply_import.sh <cli> --src-home <home> --dry-run`. Show the planned files, skipped files and warnings
   (absolute paths from the other computer, a missing omp WAL file, `planned.npm_deps`, `omp_auth`, `auth`).
7. **Apply.** `apply_import.sh keys`, then each CLI without `--dry-run` (add `--overwrite` only after the user agrees).
   The script merges pending keys into `~/.agent-keys`, sets files to 600 and the CLI dir to 700, makes backups, rewrites
   `<home>` to the box home in text config, pins Codex to file credential stores, rewrites resolved `!cmd` keys to env
   names (`auth.refs_to_env_names`), merges `mcpServers` from Claude's `~/.claude.json` (plus `oauthAccount` and
   `hasCompletedOnboarding` when the Claude login was copied), runs the status check with `~/.agent-keys` loaded, records `auth: imported` and model/reasoning `native` in
   `~/.grok-bot-agents/config.json` (only when the check passes), and deletes the staged copies. It also runs
   `npm ci`/`npm install --omit=dev` for imported extensions/skills that have a `package.json`, and a full `npm ci`
   for Pi packages (`deps_installed`). Then `post_import.py` (`post_install`) and `parity_check.py` (`parity`) run.
   Relay `parity` gaps and `parity.auth` items that are `breaking` or `degraded` (names only: unset env names, missing
   `!cmd` tools, MCP servers to authenticate on the box, expired/disabled provider logins, the ToS flag), plus the
   `post_install.version` warning if the box CLI is older. Then do the box logins for Claude/Codex (setup-agents recipe).
   **Exit 5** means everything else was installed, but the box kept its own working omp login because the imported
   one is dead. Relay the warning: the user runs `omp`, `/login` on their computer. Use `--force-auth` only if they insist.
8. **Env-var API keys left over** (unset on the computer's login shell, Windows, or `parity.auth` still lists an unset
   name): have the user enter values through the masked secret input, or Take over and use the `read -rsp` line from
   `setup-agents` 2-2, into `~/.agent-keys`. Never in chat.
9. **Verify for real.** Status checks can pass on a stale token (`codex login status` only reads the file). Run
   the setup-agents OK check per CLI:
   `bash ../delegate-task/scripts/run-agent.sh --agent <a> --read-only --timeout 120 --prompt "Reply with exactly: OK"`.
   If it fails with an auth error, fall back to logging in on the box. Status `rate_limited` means the import worked
   but the account is out of quota.
10. **Report:** per CLI, the files imported, backups, warnings, the `parity` gaps and `auth` items, the env names added
   to `~/.agent-keys` (names only), the OK-run result and the two-machine note when a login was copied. Mention that MCP servers or hooks pointing at programs only on the user's computer won't run on
   the box (Codex ones are disabled there with a comment). Afterwards `rm -rf /workspace/uploads/agent-import`.

## Lessons from real imports

- **macOS bundles** contain `._*` AppleDouble files and `.DS_Store`. The script drops them.
- **Symlinked skills** (e.g. `~/.codex/skills/x -> ~/.agents/skills/x`) are skipped by a per-file copy, because their
  targets aren't imported. `bundle_local.sh` already dereferences them. For per-file imports use `import_plan.py --os <os> --home <home> --cli <clis> --dereference`. It prints the
  commands to list the links and bundle them with `tar czhf` (dereferenced, `-T` file list, `COPYFILE_DISABLE=1`) on the user's
  computer. Then `CopyToBox` the tarball (one approval), delete it on the computer, run `stage_tar.py` and `apply_import.sh`.
  Links that are dangling on the computer itself stay links and are skipped. `stage_tar.py` also works for a full
  home-relative bundle of all CLI configs.
- **Desktop-app configs** (the Codex app's `config.toml`) carry project trust entries, `/Applications/...` MCP
  servers, hooks and plugin paths. After the home-path rewrite, Codex runs fine headless. The script warns about
  the leftover paths; report the MCP servers and hooks that point at programs missing on the box.
- **The imported default model may be out of quota.** That's the user's own default, e.g. a token-plan provider in omp.
  The OK run then returns `rate_limited`. Retry once with another model the same login can use
  (`omp models <provider>`, `--model <provider>/<id>`) to show the import worked. Suggest
  `configure-agents` to change the default; don't change it silently.
- **Pi extensions with their own `package.json`** (e.g. `extensions/mermaid-renderer`) arrive without `node_modules`,
  because bundles exclude it. One missing module (`Cannot find module 'beautiful-mermaid'`) stops Pi from starting at all.
  `apply_import.sh` installs those deps now. If `deps_installed` has a failure, run npm in that dir by hand or remove the extension.
- **A dead omp login can sit in the source `agent.db`.** Once the box refreshed a token imported earlier, the computer's
  copy got `disabled_cause` `refresh_token_reused`. Overwriting replaced a working box login with it. Now detect flags it
  and `apply_import.sh` keeps the box's `agent.db*` (exit 5). It treats `agent.db` + `-wal` + `-shm` as one unit
  (logins can sit only in the WAL), and removes a stale box WAL when it replaces the db.
- **Pi** auto-installs the `npm:` packages from the imported `settings.json` on first start, and they may need a newer Pi.
  Make sure Pi is the `@earendil-works` build on Node ≥ 22.19 (setup-agents).
- **Pi `packages` are runtime code, not settings.** Before they were bundled, the box Pi ran without web search,
  subagents, workflows or scrapling, and one missing local package (`Cannot find module 'typescript'` in a package
  that loads a dev dep) stops Pi from answering at all. So packages get a full `npm ci`, not `--omit=dev`.
- **Locally patched npm packages.** The computer's `@cortexkit/pi-anthropic-auth@1.23.0` had extra models patched
  into `dist/`; the registry copy of the same version didn't, so the box warned `No models match pattern
  "anthropic/claude-sonnet-5-5"`. The npm overlay keeps such patches (same version only).
- **Claude plugins** live in `plugins/cache`, which isn't copied. `post_import.py` adds the marketplaces from
  `extraKnownMarketplaces`, installs each enabled plugin and pins it to the computer's `gitCommitSha`, because the
  statusLine can point at a versioned path (`.../claude-hud/0.0.10/src/index.ts`). LSP plugins also need their
  servers (`typescript-language-server`, `gopls`, `rust-analyzer`) on the box; `parity_check.py` doesn't install those.
- **Terminal-app integrations** (herdr, Orca) add hooks and extensions on the computer. They exit quietly without
  their env vars, so they're harmless on the box; their hook scripts are imported so the commands don't fail.
- **Configs that call computer-only tools.** Pi keys like `!security find-generic-password …`, `op://…` refs, Claude
  `apiKeyHelper` scripts and Codex `auth.command` work on the computer and fail silently on the box. Detect lists them
  (`indirect`, tool name only); `resolve:` turns Pi/omp ones into env names. `apiKeyHelper` / `auth.command` are
  reported (`default: manual`): set an API key on the box instead, or install and sign in to that tool there.
- **A keyring-mode Codex config breaks the box** (no Secret Service there). `apply_import.sh` pins
  `cli_auth_credentials_store` and `mcp_oauth_credentials_store` to `"file"` in the box `config.toml`.
- **Model and reasoning:** after a successful import the script sets `agents.<a>.model` and `.reasoning` to
  `native`, so the imported settings apply (e.g. Codex `model_reasoning_effort = "xhigh"`, Pi `defaultThinkingLevel`).
  Only set a value via `configure-agents` if the user wants to override, e.g. when the imported default model is out of quota.

## Where things land

| CLI | Box dir | Auth file | Status check |
|---|---|---|---|
| Codex | `~/.codex` (or `$CODEX_HOME`), file credential stores | `auth.json` (API key; ChatGPT login only on opt-in), `.credentials.json` (MCP OAuth, opt-in) | `codex login status` |
| Claude Code | `~/.claude` (or `$CLAUDE_CONFIG_DIR`), plus `~/.claude.json` (mcpServers; account fields with a copied login) | `.credentials.json` (opt-in; default `claude auth login` on the box) | `claude auth status` |
| Pi | `~/.pi/agent` | `auth.json` | `pi --list-models` |
| omp | `~/.omp/agent` | `agent.db` (SQLite: logins and settings) | `omp usage` |
| Env keys | `~/.agent-keys` (600, sourced by `~/.bashrc` and `run-agent.sh`) | `export NAME='…'` lines | `parity_check.py` (`auth`) |
