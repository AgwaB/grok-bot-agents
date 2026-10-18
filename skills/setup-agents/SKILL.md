---
name: setup-agents
description: First-time setup of coding-agent CLIs (Claude Code, OpenAI Codex CLI, Pi, Oh My Pi/omp) on the Grok Bot box. Pick agents (multi-select), install only those, sign in with a subscription or an API key without exposing keys in chat, set default model and reasoning level, write the central config ~/.grok-bot-agents/config.json, and verify each with a cheap OK run. Use when the user asks to install, connect, set up or log in to Codex, Claude Code, Pi, pi agent, oh-my-pi or omp, or says "connect Codex to my bot" or "install pi agent with DeepSeek".
---

# Set up agent CLIs on the box

The box is one persistent Linux machine shared by all of the user's bots. Install a CLI once
and every bot can use it. Logins live in each CLI's config dir under `/home/box` and persist.

Config helper (in the sibling skill, same plugin):
`CFG="python3 ../configure-agents/scripts/agents_config.py"` (run from this skill's folder;
`$CFG path` prints the config location).

If `~/.grok-bot-agents/config.json` already exists, show `$CFG show` and ask whether to add
agents or reconfigure. For changes to an existing setup, use `configure-agents`.

## 2-1 · Pick agents (multi-select card)

Check what is already installed:

```bash
export PATH="$HOME/.local/bin:$HOME/.bun/bin:$PATH"
for c in claude codex pi omp; do printf '%-7s ' "$c"; command -v "$c" >/dev/null && "$c" --version 2>&1 | head -1 || echo "not installed"; done
node -v; npm -v; bun -v 2>/dev/null || echo "no bun"
```

Then show **one multiSelect question card** (the app's question tool with multiple selection on).
Don't ask in plain text. Pre-check the ones already installed. Options:
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
  about 30 seconds once. Local `packages/...` paths from another computer are skipped.

Then: `$CFG init --enable <chosen,comma,separated>` (or `$CFG enable <agent>` when adding to an existing config).

## 2-2 · Sign in or set an API key (per agent)

For each chosen agent, ask (one question card) **subscription login**, **API key**, or **import from my computer**.
Import copies the user's existing local login and settings for that CLI. Use the `import-agents` skill for it,
then come back here for 2-3 and verification.

| Option | When |
|---|---|
| Subscription login | Separate login on the box. Safest: no shared tokens. |
| API key | Provider key, entered through the masked input or `~/.agent-keys`. |
| Import from my computer | The user already uses that CLI on a registered computer. Not possible for Claude on macOS (Keychain). |


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

Say once: subscription sessions are stored on the box (cloud) and shared by all bots. The CLI's own
terms of service apply. Don't suggest using the box to get around provider account rules or region limits.

## 2-3 · Default model and reasoning level

Default is **native**: `model` and `reasoning` stay `null`, so the runner passes no flag and each CLI uses its own
settings (its config file, or what was imported). Ask per agent (one card: model and reasoning, preselected
"native / CLI default"). Only set a value when the user picks one. `native` and `null` mean the same.
List models with `pi --list-models <provider>` / `omp models <provider>`. Don't invent model IDs.

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
