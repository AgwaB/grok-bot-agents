---
name: setup-agents
description: Install selected Claude Code, Codex, Pi or omp CLIs on the Grok Bot Linux box, import consented settings or help the user sign in, and verify without exposing secrets. Use for first-time setup, install, connect or login requests.
---

# Set up agents

## Entrypoint (all paths are absolute)

Resolve this SKILL.md's folder, not the Shell working directory. The complete plugin root is
`../..` from that folder; invoke `python3 <root>/scripts/gba` (called **gba** below).
For standalone skill copies, use the user's absolute `GBA_ROOT` pointing at a complete plugin copy.
If the shared scripts/helpers are absent, ask to install the complete plugin; do not invent paths.
Python 3.11+ stdlib; mutation runs on the Linux box. JSON `schema:1` actions are instructions
for the bot, not executable payloads. See [CLI contract](../../README.md#gba-cli).

## Choose, plan, install

1. Run `gba doctor --json` on the box (static: no CLI execution or writes).
   If already configured, ask whether to add or reconfigure.
2. `ListMachines`: choose a connected desktop (macOS/Linux/Windows), not a phone.
   Run the import skill's `detect_local.sh` there; Windows uses `detect_local.ps1`.
   Detection reads config/auth contents internally but **emits names, never values**.
   Windows is less tested: say once that box login/masked API-key entry is the fallback.
3. Show one **multiSelect card** for Claude, Codex, Pi and omp; preselect locally found agents.
   Run `gba setup plan --agents <comma-list>`; it is side-effect free. Explain install locations,
   prerequisite/runtime downloads and full-auto risk. After approval run the same `setup apply`.
   Install only selected agents; obey blocked reasons rather than running unchecked curl-pipe-shell installers.
4. Show one sign-in card: **import from my computer** or **choose per agent**.
   Use `import-agents` for imports: whole-file consent, unique operation IDs, CopyToBox approvals,
   preflight and resumable apply are handled by `gba import`. Do not duplicate that procedure here.
   Legacy bundle fallback is unavailable with nondefault `CODEX_HOME`, `CLAUDE_CONFIG_DIR` **or
   `PI_CODING_AGENT_DIR`**; use consented per-file copies. Windows has no POSIX bundle/export twin.

## Sign-in and secret entry stay with the user

| Agent | Separate login on the box | API-key name examples |
|---|---|---|
| Claude | `claude auth login` | `ANTHROPIC_API_KEY` |
| Codex | `codex login --device-auth` | `OPENAI_API_KEY` |
| Pi | interactive `pi`, then `/login` | `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY` |
| omp | `omp login` | provider env names |

Open a persistent box terminal/tmux session for login; let the user **Take over** to finish.
Share only the authorization URL/device code, not terminal output that may contain tokens.
Never ask for passwords, OAuth callback codes or keys in chat. Close the login terminal after success.
Claude/Codex subscription copies are opt-in: explain two-machine refresh conflicts and Claude ToS risk.
Logins live on the shared cloud box and are accessible to its bots; do not suggest bypassing provider rules.

API keys: masked secret input, or user takeover plus `read -rsp` on the box. Create `~/.agent-keys`
only if missing, keep mode 600, replace the selected `export NAME=` assignment without duplicates,
and never print/source it merely to inspect it. The runner loads it for actual runs.
Record only auth type and env **name** with `agents_config.py set`, never the value.

## Preferences and verification

Keep model/reasoning `native` unless the user chooses an override; use configure-agents for supported values.
Settings-only imports do **not** clear existing central overrides. The legacy apply helper resets to native
only after an auth import passes its status check; the controller's offline apply preserves central defaults.
Explain any existing override and ask before clearing it; never promise imported defaults automatically win.
Full-auto is the existing default: unrestricted shell/network/login access. Worktrees are not a sandbox;
external actions still need approval. Offer safe mode, and note Pi has no approval system.

Run `gba verify --agents <selected>` statically; versions remain `null`/`not_requested` by default.
Only after runtime-use approval add `--probe-versions` (CLI/runtime execution may write files or use the network).
With explicit permission for provider use, add `--live` for the exact-OK check; it does not enable version probes.
Managed wrapper/payload integrity must pass before either execution. Quota exhaustion is not a successful OK test.
Fix a bounded cause and retry once only with approval; never change a model silently. Report status and remaining bot actions.
