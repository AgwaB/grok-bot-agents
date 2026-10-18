---
name: configure-agents
description: Show and change the agent setup in ~/.grok-agents/config.json, including enabled agents, default model, reasoning level, permission mode (full-auto or safe), routing preferences and timeout. Also rotates logins and API keys and edits each CLI's own config (Codex config.toml, Claude settings.json, Pi settings.json, omp config.yml/models.yml). Use when the user asks what's configured, or says "change pi's model", "claude reasoning to max", "switch to safe mode", "prefer codex", "rotate my DeepSeek key", "re-login Claude", or "disable omp".
---

# Configure agents

All bots share one config: `~/.grok-agents/config.json`. `delegate-task` reads it on
every run, so changes apply from the next run. No restart needed.

```bash
CFG="python3 scripts/agents_config.py"      # from this skill's folder
$CFG show                                   # always start by showing the current summary
```

## Change settings (central config)

`set` takes one or more key/value pairs, validates once at the end, and refuses anything that looks
like a secret value.

| Ask | Command |
|---|---|
| Change a model | `$CFG set agents.pi.model deepseek/<model-id>` (`native` or `null` = pass no flag; the CLI's own config decides) |
| Reasoning level | `$CFG set agents.claude.reasoning max` (claude: low/medium/high/xhigh/max · codex: minimal/low/medium/high/xhigh · pi: off/minimal/low/medium/high/xhigh · omp: those + max/auto) | Use `native` to go back to the CLI's own setting.
| Permission mode | `$CFG set permission_mode safe` or `full-auto` |
| Enable / disable an agent | `$CFG enable omp` · `$CFG disable codex` |
| Routing | `$CFG set routing.prefer '["codex"]'` · `$CFG set routing.mode ask` · `$CFG set routing.mode fixed routing.fixed claude` |
| Cheap / hard pools | `$CFG set routing.cheap '["pi"]' routing.hard '["claude","codex"]'` |
| Timeout | `$CFG set timeout_sec 3600` |

`worktree_per_task` can't be turned off. Validation rejects `false`.

Before switching to **full-auto**, remind the user in one line: agents then run every shell command
without asking. Worktrees isolate git, not the system. Push, deploy and PR still need their OK.
Show `$CFG show` after every change.

List valid model IDs before setting one: `pi --list-models <provider>`, `omp models <provider>`.
Don't guess model names.

## Rotate logins and keys

| What | How |
|---|---|
| Claude subscription | `claude auth logout` → `claude auth login` (user finishes via Take over) · check `claude auth status` |
| Codex subscription | `codex logout` → `codex login --device-auth` · check `codex login status` |
| omp login | `omp login` · check `omp usage` |
| API key (any) | User re-enters it through the masked secret input, or Take over + `read -rsp`. Replace the old `export NAME=` line in `~/.agent-keys` (keep `chmod 600`) |
| Switch auth type | `$CFG set agents.pi.auth api_key agents.pi.key_env OPENROUTER_API_KEY` |

Never show key values. `$CFG show` only reports whether each `key_env` is set.

## Native CLI configs (only when the user asks, or a setting has no runner flag)

The runner always passes the permission flags. It passes model and reasoning flags only when they're set here.
At `native`/`null` the CLI's native file decides (e.g. Codex `model_reasoning_effort`, Pi `defaultThinkingLevel`),
so to change those defaults, either set a value here (it overrides) or edit the native file.
Edit native files only for things outside it (providers, MCP servers, tool policies). Back up the file first.

| CLI | File | Notes |
|---|---|---|
| Codex | `~/.codex/config.toml` (`$CODEX_HOME`) | keys checked with `--strict-config`: `model`, `model_reasoning_effort`, `sandbox_mode` (read-only / workspace-write / danger-full-access), `approval_policy` (untrusted / on-failure / on-request / granular / never). Per-run override: `-c key=value` |
| Claude Code | `~/.claude/settings.json` (project: `.claude/settings.json`, `.claude/settings.local.json`) | path confirmed from the Claude Code 2.1.197 binary. `permissions.defaultMode` and `effortLevel` keys exist (full schema not checked). Per-run: `--settings <file\|json>` |
| Pi | `~/.pi/agent/settings.json` (project: `.pi/settings.json`) | from pi docs: `defaultProvider`, `defaultModel`, `defaultThinkingLevel`. Dir override `PI_CODING_AGENT_DIR` |
| omp | `~/.omp/agent/config.yml` (settings; `settings.json` there is legacy and gets migrated) · `~/.omp/agent/models.yml` (custom providers) | from omp 18.12.1 source: `tools.approvalMode` defaults to `yolo`; `models.yml` providers use `baseUrl`, `api`, `apiKey` (env var name). Per-run: `--config <overlay.yml>`, per-bot isolation: `--profile` |

After editing a native file, re-run the cheap OK check from `setup-agents` for that agent.
