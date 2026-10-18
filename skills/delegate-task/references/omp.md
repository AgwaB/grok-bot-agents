# omp (Oh My Pi, can1357/oh-my-pi): headless reference

Checked on omp 18.12.1 (`--help` and the package's TypeScript source).

- Install: `bun install -g @oh-my-pi/pi-coding-agent` or `curl -fsSL https://omp.sh/install | sh`
- Auth: `omp login` · `omp usage` · `omp models [provider]` · `omp token <provider>`
- Files: settings `~/.omp/agent/config.yml` (an old `settings.json` gets migrated) · custom providers `~/.omp/agent/models.yml` (`providers.<name>`: `baseUrl`, `api`, `apiKey` = env var name, optional `discovery`)
- Run: `omp -p "<prompt>"`
  - `--mode text|json|rpc|rpc-ui` · `--model` (fuzzy) · `--smol/--slow/--plan` · `--thinking off..xhigh|max|auto`
  - `--cwd` · `--add-dir` · `--max-time 600|10m|1h` · `--no-session` · `--session-dir` · `--profile NAME` · `--config overlay.yml`
  - `--tools a,b` (e.g. `read grep glob bash edit write lsp task`; `--tools find` was rejected in a -p session, so use `glob`) · `--no-lsp`
  - `--approval-mode always-ask|write|yolo` · `--auto-approve`
- Approvals (from source):
  - `tools.approvalMode` defaults to **yolo**
  - In headless runs, a tool call that needs approval **fails with an error** ("requires approval but no interactive UI"); it doesn't hang. Under `write`, exec-tier tools (most `bash`) fail
  - Provider safety checks always fail closed when there's no UI, even in yolo
- Resume: `-c` · `-r <id-prefix|path>` · `--restore` · import: `--from-claude`, `--from-codex`
- Other: `omp acp` (ACP server) · `omp worktree` · `omp commit` · `omp update`
- `--mode json` event schema is assumed Pi-like (`message_end`); not confirmed with a live run
