#!/usr/bin/env bash
# Restart a run that stalled, died, or ended on a dropped connection (for example after a box VM pause): stop what is left of it,
# commit its work in progress on branch agent/<id>, and relaunch run-agent.sh from that worktree
# with a continuation brief. Start this script itself as the background Shell command; it ends in
# run-agent.sh and prints the same JSON summary.
# Usage:
#   resume-run.sh RUN_ID|RUN_DIR [--new-id ID] [--kill] [--dry-run] [-- run-agent.sh overrides...]
#   --kill     stop the old process tree if it is still alive (required for a stalled, not dead, run)
#   --new-id   id of the new run (default <id>-r2, -r3, ...)
#   --dry-run  do the checks, print the plan and the run-agent.sh command; change nothing
# Agent, model and reasoning are taken from the old run's meta.json or cmd.txt; overrides after -- win.
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ARG="" NEW_ID="" KILL=0 DRY=0 EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --new-id) NEW_ID="$2"; shift 2;;
    --kill) KILL=1; shift;;
    --dry-run) DRY=1; shift;;
    --) shift; EXTRA=("$@"); break;;
    -h|--help) sed -n '2,11p' "$0"; exit 0;;
    -*) echo "unknown arg: $1" >&2; exit 64;;
    *) ARG="$1"; shift;;
  esac
done
[ -n "$ARG" ] || { echo "usage: resume-run.sh RUN_ID|RUN_DIR [--kill] [--new-id ID] [--dry-run] [-- overrides]" >&2; exit 64; }
die() { python3 -c 'import json,sys; print(json.dumps({"ok":False,"error":sys.argv[1]}))' "$1"; exit "${2:-2}"; }

runs_root() {  # same rule as run-agent.sh
  local cfg="${GROK_BOT_AGENTS_CONFIG:-${GROK_AGENTS_CONFIG:-$HOME/.grok-bot-agents/config.json}}" root="/workspace/agent-runs"
  if [ -z "${GROK_BOT_AGENTS_CONFIG:-}${GROK_AGENTS_CONFIG:-}" ] && [ ! -e "$cfg" ] && [ -e "$HOME/.grok-agents/config.json" ]; then cfg="$HOME/.grok-agents/config.json"; fi
  [ -f "$cfg" ] && root=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("runs_dir") or "/workspace/agent-runs")' "$cfg" 2>/dev/null || echo /workspace/agent-runs)
  echo "${AGENT_RUNS_ROOT:-$root}"
}
case "$ARG" in /*) R="${ARG%/}";; *) R="$(runs_root)/$ARG";; esac
[ -d "$R" ] || die "run dir not found: $R"
OLD_ID="$(basename "$R")"; ROOT="$(dirname "$R")"
[ -f "$R/prompt.md" ] || die "no prompt.md in $R"
if [ -f "$R/meta.json" ] && python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1])).get("status")!="ok")' "$R/meta.json"; then
  die "run $OLD_ID already finished ok; continue it with --resume <session_id> instead"
fi

# --- old process tree ---
P=$(cat "$R/agent.pid" 2>/dev/null || true)
if [ -z "$P" ]; then
  for c in /proc/[0-9]*; do
    case "$(readlink "$c/cwd" 2>/dev/null)" in "$R/wt"|"$R/work") [ "$(cat "$c/comm" 2>/dev/null)" = timeout ] && { P=${c#/proc/}; break; };; esac
  done
fi
# A pid file can outlive a box restart and name an unrelated process: only trust it if it is still `timeout`.
[ -n "$P" ] && [ "$(cat "/proc/$P/comm" 2>/dev/null)" != timeout ] && P=""
if [ -n "$P" ]; then
  [ "$KILL" = 1 ] || die "run $OLD_ID is still alive (pid $P); pass --kill to stop it first" 5
  desc() { local k; for k in $(pgrep -P "$1"); do desc "$k"; echo "$k"; done; }
  TREE="$(desc "$P") $P"
  if [ "$DRY" = 0 ]; then
    # shellcheck disable=SC2086
    kill -TERM $TREE 2>/dev/null; sleep 5
    # shellcheck disable=SC2086
    kill -KILL $TREE 2>/dev/null
    for _ in 1 2 3 4 5 6 7 8 9 10; do [ -f "$R/meta.json" ] && break; sleep 1; done   # the old runner writes meta.json
  fi
  [ "$DRY" = 1 ] && echo "would stop old process tree: $TREE" >&2 || echo "stopped old process tree: $TREE" >&2
fi

# --- agent, model, reasoning of the old run ---
eval "$(python3 - "$R" <<'PY'
import json, os, shlex, sys
r = sys.argv[1]; out = {"AGENT": "", "MODEL": "", "REASONING": "", "PERMISSION": ""}
try:
    m = json.load(open(os.path.join(r, "meta.json")))
    out.update(AGENT=m.get("agent") or "", MODEL=m.get("model") or "", REASONING=m.get("reasoning") or "", PERMISSION=m.get("permission") or "")
except Exception:
    pass
try:
    t = shlex.split(open(os.path.join(r, "cmd.txt")).read())
    out["AGENT"] = out["AGENT"] or os.path.basename(t[0])
    for i, a in enumerate(t[:-1]):
        n = t[i + 1]
        if a in ("--model", "-m") and not out["MODEL"]: out["MODEL"] = n
        if a in ("--thinking", "--effort") and not out["REASONING"]: out["REASONING"] = n
        if a == "-c" and n.startswith("model_reasoning_effort=") and not out["REASONING"]: out["REASONING"] = n.split("=", 1)[1].strip('"')
        if a == "--permission-mode" and n == "plan" or a == "--tools" or (a == "-s" and n == "read-only"): out["PERMISSION"] = out["PERMISSION"] or "read-only"
except Exception:
    pass
print("\n".join(f"{k}={shlex.quote(v)}" for k, v in out.items()))
PY
)"
[ -n "$AGENT" ] || die "cannot tell which agent ran $OLD_ID; pass -- --agent NAME"

# --- commit work in progress ---
SRC_CWD="" KIND=work
if [ -d "$R/wt" ] && git -C "$R/wt" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  KIND=repo; SRC_CWD="$R/wt"
  if [ -n "$(git -C "$R/wt" status --porcelain)" ]; then
    if [ "$DRY" = 0 ]; then
      ID_ARGS=(); git -C "$R/wt" config user.email >/dev/null || ID_ARGS=(-c user.name=grok-bot-agents -c user.email=grok-bot-agents@localhost)
      { git -C "$R/wt" add -A && git -C "$R/wt" "${ID_ARGS[@]}" commit -q --no-verify -m "wip($OLD_ID): checkpoint before resume"; } \
        || die "could not commit WIP in $R/wt"
    fi
    [ "$DRY" = 1 ] && echo "would commit WIP on $(git -C "$R/wt" rev-parse --abbrev-ref HEAD)" >&2 || echo "committed WIP on $(git -C "$R/wt" rev-parse --abbrev-ref HEAD)" >&2
  fi
elif [ -d "$R/work" ]; then
  SRC_CWD="$R/work"
elif [ -f "$R/review.diff" ] || grep -q '^Review mode: READ-ONLY' "$R/prompt.md"; then
  die "review runs have no progress to keep; rerun the review from scratch"
fi

# --- new id and continuation brief ---
if [ -z "$NEW_ID" ]; then
  base="${OLD_ID%-r[0-9]*}"; n=2
  while [ -e "$ROOT/$base-r$n" ]; do n=$((n + 1)); done
  NEW_ID="$base-r$n"
fi
[ -e "$ROOT/$NEW_ID" ] && die "run dir already exists: $ROOT/$NEW_ID"
BRIEF="$R/continue-$NEW_ID.md"
{
  # The original brief without the blocks run-agent.sh or an earlier resume appended (they are added again).
  awk '/^---$/ {hold=$0; getline nxt; if (nxt ~ /^(Inputs|Review mode|CONTINUATION:)/) exit; print hold; print nxt; next} {print}' "$R/prompt.md"
  echo
  echo "---"
  echo "CONTINUATION: an earlier run of this task ($OLD_ID) was interrupted before it finished (the machine paused, the process stalled, or its model connection dropped)."
  if [ "$KIND" = repo ]; then
    echo "Its work so far is already in this worktree: committed on this branch (see \`git log\` and \`git show --stat HEAD\`)."
  else
    echo "Its partial outputs are in $SRC_CWD (read them from there; write new outputs to the current working directory)."
  fi
  echo "First check what is already done, keep it, and continue from there. Do not start over or redo finished steps."
  echo "Finish with the same final message format the brief asks for, covering the whole task."
} > "$BRIEF.tmp"

CMD=(bash "$HERE/run-agent.sh" --agent "$AGENT" --id "$NEW_ID" --prompt-file "$BRIEF")
[ -n "$SRC_CWD" ] && CMD+=(--cwd "$SRC_CWD")
[ -n "$MODEL" ] && CMD+=(--model "$MODEL")
[ -n "$REASONING" ] && CMD+=(--reasoning "$REASONING")
[ "$PERMISSION" = read-only ] && CMD+=(--read-only)
CMD+=("${EXTRA[@]}")

if [ "$DRY" = 1 ]; then
  echo "plan: agent=$AGENT kind=$KIND old=$OLD_ID new=$NEW_ID brief=$BRIEF" >&2
  echo "brief preview:" >&2; tail -6 "$BRIEF.tmp" >&2; rm -f "$BRIEF.tmp"
  printf '%q ' "${CMD[@]}"; echo
  exit 0
fi
mv "$BRIEF.tmp" "$BRIEF"
echo "{\"resumed_from\": \"$OLD_ID\", \"new_id\": \"$NEW_ID\"}" > "$R/resumed.json"
exec "${CMD[@]}"
