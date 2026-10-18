# Codex CLI: headless reference

Checked on codex-cli 0.163.0 (`--help` plus `--strict-config` probes).

- Install: `npm install -g @openai/codex` · update: `codex update` · health check: `codex doctor`
- Login: `codex login` · headless: `codex login --device-auth` · API key: `printenv OPENAI_API_KEY | codex login --with-api-key` · `codex login status` · `codex logout`
- Run: `codex exec [OPTIONS] [PROMPT]`
  - `-C DIR` · `-s read-only|workspace-write|danger-full-access` · `-m MODEL` · `--json` · `-o FILE` (last message)
  - `--dangerously-bypass-approvals-and-sandbox` (**full-auto** in this plugin) · `--approve-for-me`
  - `--output-schema FILE` · `--skip-git-repo-check` · `--ephemeral` · `--worktree`
  - There is no `--full-auto` flag in 0.163.0
- Config `~/.codex/config.toml` (`$CODEX_HOME`); per run `-c key=value`. Checked with `--strict-config` (unknown keys are rejected):
  - `model_reasoning_effort` is a known key. The value isn't validated locally. Strings in the binary include `minimal low medium high xhigh`; which ones work depends on the model
  - `sandbox_mode`: `read-only` | `workspace-write` | `danger-full-access`
  - `approval_policy`: `untrusted` | `on-failure` | `on-request` | `granular` | `never`
- Resume: `codex exec resume [SESSION_ID] [PROMPT]` / `--last`. No `-C`/`-s`; use `-c sandbox_mode=...` or `--dangerously-bypass-approvals-and-sandbox`
- JSONL event schema for `--json`: not checked. The runner uses `-o` for the final text
