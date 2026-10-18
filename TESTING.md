# Testing

## Static checks (run from the repo root)

```bash
python3 -m json.tool .cursor-plugin/plugin.json >/dev/null
for f in skills/*/SKILL.md; do                      # frontmatter name must match the folder
  n=$(sed -n '2,6s/^name: *//p' "$f"); [ "$n" = "$(basename "$(dirname "$f")")" ] || echo "BAD $f"
done
for f in skills/*/scripts/*.sh; do bash -n "$f"; done
python3 -m py_compile skills/*/scripts/*.py && find . -name __pycache__ -prune -exec rm -rf {} +
```

## Unit-level checks

| Area | How |
|---|---|
| Router | `ROUTE_NOW=… GROK_BOT_AGENTS_STATE=/tmp/s.json python3 skills/delegate-task/scripts/route.py decide …`: light → `self`; bulk → pi; long → omp; hard → claude; review of codex → claude. With claude marked limited: hard → codex, review of codex → pi, `--named claude` → `none`/`ask`. The limit expires at the reset time. pi disabled → omp. `fixed` mode is honored |
| Reset parsing | `mark-limited claude --message "…resets 11pm (Asia/Seoul)"`, `"reset at 10-21 01:00:00 UTC"`, `"retry-after-ms=…"`, unknown text → +1h |
| Review guard | a stub agent that edits the reviewed dir → `status: error`, `review_unchanged: false`; a clean stub → `ok`/`true`; `review.diff` contains uncommitted changes |
| Bridge | `GROK_BRIDGE_ROOT=$(mktemp -d)`: post with `send-task.sh` to a local HTTP listener (refuses a non-600 config, sends the key header), then `bridge.py intake` on the body. Check that bidi, zero-width and control chars are stripped, that a duplicate exits 3, and that `../x`, `.x`, a missing id and non-JSON are rejected. `reply.reply_command`/extra fields are ignored and only reported. `bridge.py reply` sanitizes ANSI/bidi, refuses an existing outbox file or a symlink source; files are 600 |
| Import | `apply_import.sh <cli> --dry-run` on a staged copy: allowlist only, `._*`/`.DS_Store`/`.pyc` and symlinks skipped, conflicts stop unless `--overwrite` |

## End-to-end (real logins on the box, 2026-10-18)

| Scenario | Result |
|---|---|
| Codex, non-repo report | ok, 16 s |
| Codex, repo task (`mul()` + test) in worktree | ok, 9 s, tests pass |
| Native model/reasoning | no flags in `cmd.txt`; Pi used its imported settings |
| Import from macOS: Codex, Pi, omp logins + 19 symlinked skills via `--dereference` | installed, OK runs pass |
| Auto route, light task | stays with the bot |
| Auto route, bulk translation (40 keys) | pi, ok, 51 s, all keys present |
| Auto route, hard task | claude → `rate_limited` (reset recorded, empty worktree removed) → codex, ok, 27 s, tests pass |
| Review of the Codex branch with pi (`--review`) | ok, 27 s, verdict approve with 1 low finding, `review_unchanged: true` |
| Claude Code headless run | not yet: the test account was rate-limited |

## Manual checks after installing from the marketplace

1. Type `/` in Grok Bot: the five skills are listed.
2. "set up codex and pi": multi-select, sign-in choice, OK test.
3. "have pi translate README to Korean", "analyze this log folder" (auto route), "review that branch".
4. "set claude reasoning to max" → `~/.grok-bot-agents/config.json` changes.
