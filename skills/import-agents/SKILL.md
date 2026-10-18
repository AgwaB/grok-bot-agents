---
name: import-agents
description: Import the user's existing Codex, Claude Code, Pi or omp setup, logins included, from their own registered computer into the Grok Bot box, so they don't have to log in to each CLI again. Copies only an allowlist of auth and config files with ListMachines and CopyToBox (each copy needs the user's approval), never shows secret contents, backs up anything it would overwrite, rewrites home paths, updates ~/.grok-bot-agents/config.json and verifies each CLI. Use when the user says "import my codex/claude settings", "copy my login from my laptop", "use my local setup", or picks "import from my computer" during setup-agents.
---

# Import agent setups from the user's computer

The CLIs must already be installed on the box (`setup-agents` 2-1). Scripts live in `scripts/`:
- `import_plan.py --os macos|linux|windows --home <user home> --cli <list> [--json]` prints what to copy and where to stage it. It reads nothing.
- `apply_import.sh <cli> [--src-home <user home>] [--dry-run] [--overwrite] [--force-auth] [--keep-stage] [--no-verify]`
  installs from `/workspace/uploads/agent-import/<cli>/`. Exit 0 ok, 4 conflicts, 5 installed but the box's omp login was kept.
- `stage_tar.py <bundle.tgz> [--cli <list>]` splits a tarball of home-relative paths into those stage dirs.
  Use it for `bundle_local.sh` / `--dereference` bundles made on the user's computer.
- `detect_local.sh` and `bundle_local.sh` run **on the user's computer** (macOS/Linux, POSIX sh): Read the file and pass
  its content verbatim to one machine Shell call as a heredoc, `sh -s -- <clis> <<'EOF'` … `EOF`.
  `detect_local.sh` is read-only and prints JSON per CLI (`installed`, `version`, `has_auth`, `importable_auth`,
  `has_config`, `present` names, `symlinks`, `auth_status`, `note`). For omp it reads only provider names and
  usable/disabled from a temp copy of `agent.db` (needs `sqlite3`, built into macOS). `bundle_local.sh` tars only the allowlist, dereferenced, into
  `~/agent-import.tgz` (600) and prints size, member count and entry names. `import_plan.py --lint-scripts` checks
  that both cover every allowlisted path.

## Rules

- Copy only what `import_plan.py` lists. Never copy SSH or GPG keys, cloud credentials (`~/.aws`, `~/.netrc`,
  `~/.git-credentials`), browser profiles or other dotfiles, even if asked in passing. `apply_import.sh` skips
  anything off its allowlist, plus sessions, history, logs and caches.
- Never `cat`, `Read`, print or summarize auth files (`auth.json`, `.credentials.json`, `agent.db`) or `~/.agent-keys`.
  Report only names, sizes and the script's JSON.
- Don't overwrite box config silently. If `apply_import.sh` exits 4 (conflicts), list the files, ask, then rerun
  with `--overwrite`, which saves `<file>.bak.<timestamp>` first.
- Tell the user once, before copying auth files: the same login will then be used on two machines. These CLIs
  refresh OAuth tokens, and a refresh on one side can invalidate the other (Codex has an "OAuth refresh token was
  rejected" error). If one side gets logged out, log in there again. A separate login on the box avoids the risk.

## Steps

1. **Pick the computer.** `ListMachines`. Use a connected desktop (the one the user's message came from by default).
   Phones can't do this. Run `detect_local.sh` there (one machine Shell call); it gives the OS, home dir and what
   exists. Windows: get `$env:USERPROFILE` and use `Test-Path` on the `import_plan.py --os windows` paths.
2. **Pick CLIs** (one multiSelect card): Codex, Claude Code, Pi, omp. Pre-check the ones found locally.
3. **Plan.** Show the user a short table from the detect JSON (`present` names per CLI, what falls back to a login).
   `python3 scripts/import_plan.py --os <os> --home <home> --cli <chosen>` lists the full allowlist and stage paths.
4. **Prepare the source.**
   - **omp:** quit omp on the computer first, since `agent.db` is a live SQLite file. Also copy `agent.db-wal` and `agent.db-shm` if present.
     If detect's `auth_status` shows a provider `disabled`, ask the user to run `omp`, then `/login` on that computer
     before bundling, then rerun detect.
   - **Claude on macOS:** the login is in the Keychain item `Claude Code-credentials`, not a file, so it can't be copied.
     Import settings only, then run `claude auth login` on the box (setup-agents headless recipe).
   - **Windows:** Claude may also keep it in Credential Manager. If `.credentials.json` is missing, fall back to login.
5. **Copy (default: one bundle).** Run `bundle_local.sh <chosen>` on the computer, `CopyToBox` `~/agent-import.tgz` →
   `/workspace/uploads/agent-import.tgz` (one approval), `rm -f ~/agent-import.tgz` on the computer, then
   `python3 scripts/stage_tar.py /workspace/uploads/agent-import.tgz --cli <chosen>` and delete the box copy.
   Per-file fallback (Windows, or `CODEX_HOME`/`CLAUDE_CONFIG_DIR` set): one `CopyToBox` per file, with `box_path` set
   to the plan's `stage` path; the user approves each copy.
6. **Dry run.** `bash scripts/apply_import.sh <cli> --src-home <home> --dry-run`. Show the planned files, skipped
   files and warnings (absolute paths from the other computer, a missing omp WAL file, `planned.npm_deps`, `omp_auth`).
7. **Apply.** Rerun without `--dry-run` (add `--overwrite` only after the user agrees). The script sets files to 600 and the
   CLI dir to 700, makes backups, rewrites `<home>` to the box home in text config, merges only `mcpServers`
   from Claude's `~/.claude.json`, runs the status check, records `auth: imported` and model/reasoning `native` in
   `~/.grok-bot-agents/config.json` (only when the check passes), and deletes the staged copies. It also runs
   `npm ci`/`npm install --omit=dev` for imported extensions/skills that have a `package.json` (`deps_installed`).
   **Exit 5** means everything else was installed, but the box kept its own working omp login because the imported
   one is dead. Relay the warning: the user runs `omp`, `/login` on their computer. Use `--force-auth` only if they insist.
8. **Env-var API keys** aren't in these files (for example Pi or omp providers using `DEEPSEEK_API_KEY`). Ask which env
   names they use, then have the user enter values through the masked secret input, or Take over and use the
   `read -rsp` line from `setup-agents` 2-2, into `~/.agent-keys`. Never in chat.
9. **Verify for real.** Status checks can pass on a stale token (`codex login status` only reads the file). Run
   the setup-agents OK check per CLI:
   `bash ../delegate-task/scripts/run-agent.sh --agent <a> --read-only --timeout 120 --prompt "Reply with exactly: OK"`.
   If it fails with an auth error, fall back to logging in on the box. Status `rate_limited` means the import worked
   but the account is out of quota.
10. **Report:** per CLI, the files imported, backups, warnings, the OK-run result and the two-machine note.
   Mention that MCP servers or hooks pointing at programs only on the user's computer won't run on the box.

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
- **Model and reasoning:** after a successful import the script sets `agents.<a>.model` and `.reasoning` to
  `native`, so the imported settings apply (e.g. Codex `model_reasoning_effort = "xhigh"`, Pi `defaultThinkingLevel`).
  Only set a value via `configure-agents` if the user wants to override, e.g. when the imported default model is out of quota.

## Where things land

| CLI | Box dir | Auth file | Status check |
|---|---|---|---|
| Codex | `~/.codex` (or `$CODEX_HOME`) | `auth.json` | `codex login status` |
| Claude Code | `~/.claude` (or `$CLAUDE_CONFIG_DIR`), plus `~/.claude.json` (mcpServers merge only) | `.credentials.json` (not on macOS) | `claude auth status` |
| Pi | `~/.pi/agent` | `auth.json` | `pi --list-models` |
| omp | `~/.omp/agent` | `agent.db` (SQLite: logins and settings) | `omp usage` |
