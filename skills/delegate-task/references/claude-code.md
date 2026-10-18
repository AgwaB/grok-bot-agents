# Claude Code: headless reference

Checked on Claude Code 2.1.197 (`--help`, real `-p` output, binary strings).

- Install: `npm install -g @anthropic-ai/claude-code` (or `claude install` for the native build)
- Login: `claude auth login|logout|status` · headless long-lived token: `claude setup-token` (subscription) · API: `ANTHROPIC_API_KEY`
- Run: `claude -p "<prompt>"`
  - `--output-format json` gives one object with `type:"result"`, `is_error`, `result`, `session_id`, `total_cost_usd`
  - `--permission-mode acceptEdits|auto|bypassPermissions|default|dontAsk|plan` (**full-auto** = `bypassPermissions`)
  - `--dangerously-skip-permissions` is refused under root/sudo. The box user is uid 1000, so it's fine
  - `--effort low|medium|high|xhigh|max` · `--model` · `--fallback-model` · `--max-budget-usd` · `--json-schema`
  - `--allowed-tools` / `--disallowed-tools` · `--add-dir` · `--mcp-config` · `--settings <file|json>` · `-w/--worktree` · `--bg`
- Resume: `-r/--resume <session-id>` · `-c/--continue` · `--fork-session`
- Settings: `~/.claude/settings.json`, project `.claude/settings.json` and `.claude/settings.local.json`. Path and the keys `permissions.defaultMode` and `effortLevel` appear in the binary; full schema not checked
- `--bare` skips OAuth/keychain (API key only). Don't use it with subscription logins
