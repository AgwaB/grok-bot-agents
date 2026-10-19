---
name: configure-agents
description: Inspect or change shared agent models, reasoning, permissions, routing, enabled agents and timeout; guide approved login/key rotation and native config edits. Use for reconfiguration rather than first-time setup.
---

# Configure agents

From this SKILL.md's absolute folder, resolve the complete plugin at `../..` and invoke
`python3 <root>/scripts/gba` (**gba**). A standalone copy needs absolute `GBA_ROOT` pointing at
all plugin scripts/helpers; stop if missing. Never resolve relative to an arbitrary Shell cwd.
The config helper is `<root>/skills/configure-agents/scripts/agents_config.py` (**CFG** below).

Start with `gba doctor --json`; shared `~/.grok-bot-agents/config.json` changes affect the next run.
`python3 <CFG> show` gives native/override preferences; never inspect raw auth/config files for diagnosis.

## Central settings (no secret values)

`python3 <CFG> set KEY VALUE [KEY VALUE …]` validates the entire update before saving.
Secret-looking values are refused in **every field**, including nested values: known key prefixes,
Bearer/JWT/private-key markers and long high-entropy tokens. This is conservative detection, not proof
that arbitrary text is safe; false positives are possible. Success prints a field count, **never raw values**.
`key_env` must be an environment-variable name. Use secure secret entry, never config/argv, for keys.

| Setting | Example |
|---|---|
| Model | `set agents.pi.model deepseek/<verified-model-id>`; `native`/`null` means no runner flag |
| Reasoning | `set agents.pi.reasoning max` |
| Permission | `set permission_mode safe` or `full-auto` |
| Enabled | `enable omp` / `disable codex` |
| Routing | `set routing.mode ask`; `set routing.mode fixed routing.fixed claude` |
| Pools/preference | `set routing.cheap '["pi"]' routing.hard '["claude","codex"]' routing.prefer '["codex"]'` |
| Timeout | `set timeout_sec 3600` |

Reasoning enums: Claude `low/medium/high/xhigh/max`; Codex `minimal/low/medium/high/xhigh`;
Pi `off/minimal/low/medium/high/xhigh/max`; omp adds `auto` to Pi's list. All accept `native`/`null`.
Actual model support varies; discover IDs with the CLI, do not invent them.
`worktree_per_task` must remain true. Before full-auto, explain unrestricted shell/network/login access;
worktrees are not a sandbox. Push, deploy, PRs, messages and spending still need separate approval.

## Login, keys and native files

The user completes `claude auth login`, `codex login --device-auth`, Pi `/login` or `omp login`
through **Take over** in a persistent box terminal. Ask before logout; never request secrets in chat.
API-key rotation uses masked input or takeover + `read -rsp`. Replace only the selected assignment in
`~/.agent-keys`, preserve other names and mode 600; never print values or duplicate assignments.
Record `agents.<a>.auth` and `agents.<a>.key_env` only. See setup-agents for the consent/login boundary.

Only edit native config when requested or unsupported by runner flags; back it up first:
Codex `~/.codex/config.toml`, Claude `~/.claude/settings.json`, Pi `~/.pi/agent/settings.json`,
omp `~/.omp/agent/config.yml` / `models.yml`. Honor the CLI directory overrides.
Central non-native model/reasoning wins over these files. Settings-only imports preserve central overrides;
never silently clear one or claim imported defaults automatically apply.

After a change run `gba verify --agents <changed-agents>` offline. With explicit provider-use permission,
add `--live` for an exact-OK check; report auth/quota/failure separately. Do not dump CLI diagnostics,
auth files, agent.db, secrets.env, ~/.agent-keys or sensitive native configuration.
