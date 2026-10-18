# Pi (badlogic/pi-mono): headless reference

Checked on pi 0.73.1, re-checked on 1.3.0 (`--thinking` adds `max`; `--help` and the bundled `docs/settings.md` / `docs/json.md`).

- Install: `npm install -g @earendil-works/pi-coding-agent` (Node ≥ 22.19; the old `@mariozechner/...` package is deprecated at 0.73.1) · update: `pi update self`
- Dirs: `~/.pi/agent` (`PI_CODING_AGENT_DIR`) · sessions `PI_CODING_AGENT_SESSION_DIR` / `--session-dir` · creds `~/.pi/agent/auth.json`
- Settings: `~/.pi/agent/settings.json` (project `.pi/settings.json`). Keys `defaultProvider`, `defaultModel`, `defaultThinkingLevel` (off|minimal|low|medium|high|xhigh)
- Auth: provider env vars (`DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`, `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `XAI_API_KEY`, …) or `--api-key`. OAuth via `/login` (interactive)
- Run: `pi -p "<prompt>"`
  - `--mode text|json|rpc` · `--model provider/id[:thinking]` · `--thinking …` · `--tools a,b` · `--no-tools` · `--no-session`
  - `--list-models [search]` · `--skill PATH` · `-e PATH` · `--no-context-files`
- No approval system: tools run without prompts. Restrict with `--tools read,grep,find,ls` (grep/find/ls are off by default)
- `--mode json` events: `{"type":"session","id":…}` first, then `message_start/update/end`, `turn_end`, `agent_end`. The final text is the last `message_end` where `message.role == "assistant"` (text parts)
- Resume: `-c` · `-r` (picker) · `--session <path|id>` · `--fork <path|id>`
- No `--cwd` flag; the runner `cd`s into the workdir
