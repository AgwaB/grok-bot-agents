---
name: setup-agents
description: First-time setup of coding-agent CLIs (Claude Code, OpenAI Codex CLI, Pi, Oh My Pi/omp) on the Grok Bot box. By default it detects the CLIs already set up on the user's registered computer and imports them in one go (settings, consented API keys, Pi/omp logins; Claude Code and Codex get their own box login unless the user opts in to copying); otherwise it signs in with a subscription or an API key without exposing keys in chat. Picks agents (multi-select), installs only those, keeps model and reasoning native, writes the central config ~/.grok-bot-agents/config.json, and verifies each with a cheap OK run. Use when the user asks to install, connect, set up or log in to Codex, Claude Code, Pi, pi agent, oh-my-pi or omp, or says "connect Codex to my bot", "use my local setup" or "install pi agent with DeepSeek".
---

# Set up agent CLIs on the box

The box is one persistent Linux machine shared by all of the user's bots. Install a CLI once
and every bot can use it. Logins live in each CLI's config dir under `/home/box` and persist.

Config helper (in the sibling skill, same plugin):
`CFG="python3 ../configure-agents/scripts/agents_config.py"` (run from this skill's folder;
`$CFG path` prints the config location).

If `~/.grok-bot-agents/config.json` already exists, show `$CFG show` and ask whether to add
agents or reconfigure. For changes to an existing setup, use `configure-agents`.

## 0 · Detect the user's local setup (before 2-1)

The default path is **import everything from the user's computer**: settings, plugins and packages, the API keys the
user ticks on one consent card, and Pi/omp logins. Claude Code and Codex subscription logins get **their own login on
the box** by default (no two-machine refresh conflict; Anthropic's ToS wants its own sign-in flow); copying them is an opt-in.
1. `ListMachines`. Use a connected desktop (default: the one the user's last message was sent from). No connected
   desktop, or only phones: skip to 2-1 and use the per-agent path in 2-2.
2. Run **one** read-only machine Shell call with `../import-agents/scripts/detect_local.sh`: Read the file and pass
   its content verbatim as a heredoc, `sh -s -- claude codex pi omp <<'DETECT'` … `DETECT`. It only tests whether
   the `import_plan.py` paths exist (names, never contents) and runs `<cli> --version`. It prints one JSON line:
   `os`, `home`, per CLI `installed`, `version`, `has_auth`, `importable_auth`, `has_config`, `login_store`, `present`,
   `symlinks`, `auth_status`, `note`, and `secrets` (a name-only inventory for the consent card). Windows:
   `../import-agents/scripts/detect_local.ps1`, passed as `& ([scriptblock]::Create(@'` … `'@)) -Clis claude,codex,pi,omp`.
3. A CLI counts as **found locally** when it has `importable_auth` or `has_config`. For Claude and Codex the login
   itself isn't imported by default (`login_store` says where it lives); their settings are.

## 2-1 · Pick agents (multi-select card)

Check what is already on the box:

```bash
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
for c in claude codex pi omp; do printf '%-7s ' "$c"; command -v "$c" >/dev/null && "$c" --version 2>&1 | head -1 || echo "not installed"; done
node -v; npm -v; bun -v 2>/dev/null || echo "no bun"
```

Then show **one multiSelect question card** (the app's question tool with multiple selection on).
Don't ask in plain text. Pre-check the ones found locally (step 0) or installed on the box, and mark the
locally found ones "(내 컴퓨터에 있음)". Options:
- **Claude Code**: hard implementation, design, tricky bugs (Claude plan or API key)
- **Codex CLI**: implementation, review, second opinion (ChatGPT plan or API key)
- **Pi**: light and fast, for repetitive or bulk work on cheap models (provider API key)
- **omp (Oh My Pi)**: long runs with LSP, debugger and subagents (API key or Claude OAuth)

Install **only** the chosen ones:

| Agent | Install | Verify |
|---|---|---|
| Claude Code | `npm install -g @anthropic-ai/claude-code` | `claude --version` |
| Codex CLI | `npm install -g @openai/codex` | `codex --version` |
| Pi | `npm install -g @earendil-works/pi-coding-agent` (needs Node ≥ 22.19, see below) | `pi --version` |
| omp | `bun install -g @oh-my-pi/pi-coding-agent` (or `curl -fsSL https://omp.sh/install \| sh`) | `omp --version` |

- If `npm install -g` fails with EACCES, run `npm config set prefix ~/.local`.
- Bot shells usually start without `~/.local/bin` (npm prefix) or `~/.bun/bin` (bun globals) on PATH.
  Always run `export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"` before checking, and persist it once:
  `grep -q '.bun/bin' ~/.bashrc || echo 'export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"' >> ~/.bashrc`.
  `run-agent.sh` adds both dirs itself.
- Unset a leftover `BUN_INSTALL` first, or bun installs omp somewhere else.
- npm 11 and bun warn that they blocked postinstall scripts (koffi, protobufjs, onnxruntime-node). All four CLIs
  still run without them. Don't approve scripts unless the verify step fails.
- `pi --version` prints to stderr, which is why the check uses `2>&1`.
- **Pi package:** the old `@mariozechner/pi-coding-agent` is deprecated and frozen at 0.73.1. Use `@earendil-works/pi-coding-agent`.
  Current releases need Node ≥ 22.19. On Node 20, npm quietly installs the `legacy-node20` build (0.74.x),
  and many Pi packages (pi-web-access, pi-lens and others) need newer versions. If `node -v` is below 22.19, install a user-level Node 22
  (no root) and a wrapper:
  ```bash
  V=$(curl -fsSL https://nodejs.org/dist/index.json | python3 -c 'import json,sys;print(next(r["version"] for r in json.load(sys.stdin) if r["version"].startswith("v22.")))')
  mkdir -p ~/.local/opt && cd ~/.local/opt && curl -fsSLO https://nodejs.org/dist/$V/node-$V-linux-x64.tar.xz \
    && curl -fsSL https://nodejs.org/dist/$V/SHASUMS256.txt | grep " node-$V-linux-x64.tar.xz$" | sha256sum -c - \
    && tar -xJf node-$V-linux-x64.tar.xz && rm node-$V-linux-x64.tar.xz && ln -sfn node-$V-linux-x64 node22
  PATH=~/.local/opt/node22/bin:$PATH npm install -g --prefix ~/.local/opt/pi @earendil-works/pi-coding-agent@latest
  printf '#!/usr/bin/env bash\nexport PATH="$HOME/.local/opt/node22/bin:$PATH"\nexec "$HOME/.local/opt/node22/bin/node" "$HOME/.local/opt/pi/lib/node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js" "$@"\n' > ~/.local/bin/pi && chmod +x ~/.local/bin/pi
  ```
- On first start Pi installs the `npm:` entries in `settings.json` `packages` into the global npm prefix. That takes
  about 30 seconds once. Local packages from another computer come with the import bundle (`import-agents`).

Then: `$CFG init --enable <chosen,comma,separated>` (or `$CFG enable <agent>` when adding to an existing config).

## 2-2 · Sign in: import from the computer by default

Ask **one** question card for all chosen agents together (not one card per agent):
- **내 컴퓨터에서 전부 가져오기 (추천)**: first option, shown when any chosen CLI was found locally. List which
  CLIs it covers, and which log in on the box: Claude Code and Codex subscriptions (default), and CLIs not found locally.
- **에이전트별로 고르기**: then one card per agent with subscription login, API key or import.

Without a connected computer, or when nothing was found locally, skip this card and go straight to the per-agent cards.

### Import everything (default)

**Default per login** (details and the full table: `import-agents`, "Auth"):

| Login | Default | Opt-in on the consent card |
|---|---|---|
| Claude Code subscription | `claude auth login` on the box (headless recipe below) | Copy: the file (Linux/Windows) or the macOS Keychain item. Warn: two-machine logout + Anthropic ToS grey zone |
| Codex ChatGPT login | `codex login --device-auth` on the box | Copy `auth.json` (or the keyring item). Warn: two-machine logout (OpenAI: don't share one `auth.json`) |
| Codex API-key `auth.json`, Pi/omp `/login` keys and OAuth | Copied (Pi expiry / omp disabled checks) | — |
| Env-var API keys, `!cmd` / `op://` keys | Exported by name after consent into the box `~/.agent-keys` | — |
| Browser-profile logins, SSH, cloud credentials | Never | — |

1. Install the chosen CLIs that are missing on the box (2-1 table). For omp, ask the user to quit omp on the computer.
   If detect shows an omp provider `disabled` in `auth_status`, ask them to run `omp`, then `/login` there first.
2. **One consent card** (multiSelect), built from detect's `secrets`: env names that are set, `!cmd` keys to resolve,
   package auth files, and, unticked, "Claude 로그인도 복사" / "Codex 로그인도 복사" with the warnings above. Names only,
   never values. Only ticked items move (`import-agents` maps ticks to script arguments).
3. **Export on the computer** (only if something was ticked for export): one machine Shell call with
   `../import-agents/scripts/export_secrets.sh <items>`, passed verbatim the same way. Values go straight into 600
   files; it prints names and byte counts only. macOS shows its own Keychain prompt for a `keychain:` item.
4. **Bundle on the computer** (one machine Shell call): pass `../import-agents/scripts/bundle_local.sh` verbatim the same
   way, `sh -s -- [--copy-login claude,codex] [--skip P] [--include P] <found clis> <<'BUNDLE'` … `BUNDLE`. It tars only
   the allowlisted paths, dereferenced (symlinked skills become real files), plus the exports (then deletes them), into
   `~/agent-import.tgz` (600), and prints only size, member count, entry names and `left_out`.
   Skip it for a CLI whose note says `CODEX_HOME`/`CLAUDE_CONFIG_DIR` is set; import that one per file (`import-agents`).
5. **One `CopyToBox`**: `~/agent-import.tgz` → `/workspace/uploads/agent-import.tgz`. Then delete it on the computer
   with one more machine Shell call: `rm -f ~/agent-import.tgz`. Each machine Shell and CopyToBox call needs the user's
   approval; tell them up front that it's about four or five approvals in total (detect, export if needed, bundle, copy,
   delete), not one per file.
6. On the box: `python3 ../import-agents/scripts/stage_tar.py /workspace/uploads/agent-import.tgz --cli <clis>`, then
   `rm -f /workspace/uploads/agent-import.tgz`, then `bash ../import-agents/scripts/apply_import.sh keys` (merges the
   exported env keys into `~/.agent-keys`; prints names only).
7. Per CLI: `bash ../import-agents/scripts/apply_import.sh <cli> --src-home <home> --dry-run`, then without
   `--dry-run`. On exit 4 (conflicts), list the files, ask, and rerun with `--overwrite` (it backs them up). The script
   records `auth: imported` and model/reasoning `native` when the status check passes, installs npm deps for
   imported Pi extensions and packages, then makes the CLI run like on the computer (`post_install`: Pi packages and
   venvs, Claude plugins pinned to the computer's commit, Mac-only Codex MCP servers disabled) and checks it (`parity`).
   Relay `breaking`/`degraded` parity gaps (and `parity.auth`, names only) and a box CLI older than the computer's.
   Exit 5: the box kept its own working omp login because the imported one is dead; relay the warning.
8. **Box logins:** Claude Code and Codex (unless their copy was ticked), and CLIs not found locally, get a subscription
   login on the box (headless recipe below) or an API key. Env names still unset (`parity.auth`, or Windows, which has
   no export script yet): have the user enter values through the masked secret input (API key steps below).

Details, rules and lessons: the `import-agents` skill. Never print, `cat` or summarize auth files; copy only the allowlist.

### Per agent (when chosen, or without a connected computer)

For each chosen agent, ask (one question card) **subscription login**, **API key**, or **import from my computer**
(the `import-agents` skill for that CLI).

| Option | When |
|---|---|
| Subscription login | Separate login on the box. Safest: no shared tokens. |
| API key | Provider key, entered through the masked input or `~/.agent-keys`. |
| Import from my computer | The user already uses that CLI on a registered computer. Claude/Codex: settings come over and the login is a box login unless the user opts in to copying it. |

| Agent | Subscription login (user finishes via Take over) | API key env | Check |
|---|---|---|---|
| Claude Code | `claude auth login` · headless long-lived token: `claude setup-token` | `ANTHROPIC_API_KEY` | `claude auth status` |
| Codex | `codex login --device-auth` (or `codex login`) | `OPENAI_API_KEY`, then `printenv OPENAI_API_KEY \| codex login --with-api-key` | `codex login status` |
| Pi | `/login` inside interactive `pi` (OAuth-capable providers only) | `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY`, … | `pi --list-models <provider>` |
| omp | `omp login` (e.g. Claude subscription OAuth) | same provider env vars, or `~/.omp/agent/models.yml` with `apiKey: ENV_NAME` | `omp usage` · `omp models <provider>` |

**Subscription login:** start the command in a box terminal, then tell the user to **Take over** and
finish in the browser or terminal. Never ask for a password in chat.

Headless recipe (keeps the code out of chat):
```bash
tmux new-session -d -s claude-login -x 250 -y 50 "bash -lc 'claude auth login; exec bash'"
sleep 5; tmux capture-pane -p -J -t claude-login     # shows the authorize URL (public, PKCE) and "Paste code here"
DISPLAY=$DISPLAY xfce4-terminal -x tmux attach -t claude-login &   # optional: a desktop window for Take over
```
Give the user the URL. They open it in their own browser, approve, copy the code from the
callback page, then **Take over** and paste it into that terminal (or run `tmux attach -t claude-login`).
Never ask for the code in chat. Check with `claude auth status` (`loggedIn: true`), then kill the session.

Codex works the same way with `codex login --device-auth` in session `codex-login`. It prints
`https://auth.openai.com/codex/device` and a one-time code (valid 15 min). The user types that code on
OpenAI's page, so it's fine to show it. Nothing has to be pasted back: the terminal finishes by itself.
Check with `codex login status`.
Prefer `claude auth login`: it stores a refreshable login in `~/.claude`. `claude setup-token` prints a
long-lived token the user would then have to store as `CLAUDE_CODE_OAUTH_TOKEN` in `~/.agent-keys`.

**API key: never in chat.**
1. Create the key file once: `install -m 600 /dev/null ~/.agent-keys` (if missing), and make shells load it:
   `grep -q agent-keys ~/.bashrc || echo '[ -f ~/.agent-keys ] && . ~/.agent-keys' >> ~/.bashrc`.
   `run-agent.sh` also sources it.
2. Have the user enter the key through the app's masked secret input if one is available. Otherwise
   have them Take over a box terminal and run (input is hidden):
   `read -rsp 'DEEPSEEK_API_KEY: ' k && printf 'export DEEPSEEK_API_KEY=%q\n' "$k" >> ~/.agent-keys && unset k`
3. Keep `chmod 600 ~/.agent-keys`. Never `cat` it, echo a key, or write a key into config or skill files.

Record the choice: `$CFG set agents.<a>.auth oauth` or `$CFG set agents.<a>.auth api_key agents.<a>.key_env DEEPSEEK_API_KEY` (an import sets `imported` itself).

Say once: logins are stored on the box (cloud) and shared by all bots. The CLI's own
terms of service apply. Don't suggest using the box to get around provider account rules or region limits.

## 2-3 · Default model and reasoning level

Default is **native**: `model` and `reasoning` stay `null` (or `native`), so the runner passes no flag and each CLI uses
its own settings (its config file, or what was imported). After an import, **skip this card**: say in one line that the
imported model and reasoning settings are kept, and that `configure-agents` changes them. Otherwise ask per agent (one
card: model and reasoning, preselected "native / CLI default"), and only set a value when the user picks one, or when
they ask. List models with `pi --list-models <provider>` / `omp models <provider>`. Don't invent model IDs.

| Agent | Reasoning values (flag the runner passes) |
|---|---|
| Claude Code | `low` `medium` `high` `xhigh` `max` (`--effort`) |
| Codex | `minimal` `low` `medium` `high` `xhigh` (`-c model_reasoning_effort=…`; which values work depends on the model) |
| Pi | `off` `minimal` `low` `medium` `high` `xhigh` `max` (`--thinking`; `max` needs pi ≥ 1.x) |
| omp | `off` `minimal` `low` `medium` `high` `xhigh` `max` `auto` (`--thinking`) |

`$CFG set agents.pi.model deepseek/<model-id> agents.pi.reasoning low`

Permission mode defaults to **full-auto** (approve everything). Worktree-per-task isolation is always on.
Mention once that safe mode exists (`configure-agents`).

## Verify (cheap OK run)

For each enabled agent, run through the runner (no `--cwd`, so it uses a fresh run work dir), so the run uses the config's model,
reasoning and permission:

```bash
bash ../delegate-task/scripts/run-agent.sh --agent <a> --read-only --timeout 120 --prompt "Reply with exactly: OK"
```

Report pass or fail per agent in one short table. On failure, read `stderr_tail`, fix the cause
(auth, wrong model id, PATH), and re-run once. Finish with `$CFG show`.
