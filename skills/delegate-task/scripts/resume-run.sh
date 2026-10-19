#!/usr/bin/env bash
# Restart a run that stalled, died, or ended on a dropped connection (for example after a box VM pause): stop what is left of it,
# commit its work in progress on branch agent/<id>, and relaunch run-agent.sh from that worktree
# with a continuation brief. Start this script itself as the background Shell command; it ends in
# run-agent.sh and prints the same JSON summary.
# Usage:
#   resume-run.sh RUN_ID|RUN_DIR [--native] [--new-id ID] [--kill] [--dry-run] [-- run-agent.sh overrides...]
#   --native   opt in to the CLI session in the SAME workdir; fresh-session fallback on missing/rejected resume
#   --kill     stop the old process tree if it is still alive (required for a stalled, not dead, run)
#   --new-id   id of the new run (default <id>-r2, -r3, ...)
#   --dry-run  do the checks, print the plan and the run-agent.sh command; change nothing
# Agent, model and reasoning are taken from the old run's meta.json or cmd.txt; overrides after -- win.
set -uo pipefail
umask 077
HERE="$(cd "$(dirname "$0")" && pwd)"
ARG="" NEW_ID="" KILL=0 DRY=0 NATIVE=0 EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --new-id) NEW_ID="$2"; shift 2;;
    --kill) KILL=1; shift;;
    --native) NATIVE=1; shift;;
    --dry-run) DRY=1; shift;;
    --) shift; EXTRA=("$@"); break;;
    -h|--help) sed -n '2,12p' "$0"; exit 0;;
    -*) echo "unknown arg: $1" >&2; exit 64;;
    *) ARG="$1"; shift;;
  esac
done
[ -n "$ARG" ] || { echo "usage: resume-run.sh RUN_ID|RUN_DIR [--native] [--kill] [--new-id ID] [--dry-run] [-- overrides]" >&2; exit 64; }
die() { python3 -c 'import json,sys; print(json.dumps({"ok":False,"error":sys.argv[1]}))' "$1"; exit "${2:-2}"; }

runs_root() {  # same rule as run-agent.sh
  local cfg="${GROK_BOT_AGENTS_CONFIG:-${GROK_AGENTS_CONFIG:-$HOME/.grok-bot-agents/config.json}}" root="/workspace/agent-runs"
  if [ -z "${GROK_BOT_AGENTS_CONFIG:-}${GROK_AGENTS_CONFIG:-}" ] && [ ! -e "$cfg" ] && [ -e "$HOME/.grok-agents/config.json" ]; then cfg="$HOME/.grok-agents/config.json"; fi
  [ -f "$cfg" ] && root=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("runs_dir") or "/workspace/agent-runs")' "$cfg" 2>/dev/null || echo /workspace/agent-runs)
  echo "${AGENT_RUNS_ROOT:-$root}"
}
case "$ARG" in /*) R="${ARG%/}";; *) R="$(runs_root)/$ARG";; esac
[ -d "$R" ] || die "run dir not found: $R"
R="$(cd "$R" && pwd -P)"
OLD_ID="$(basename "$R")"; ROOT="$(dirname "$R")"
[ -f "$R/prompt.md" ] || die "no prompt.md in $R"
if [ -f "$R/meta.json" ] && python3 -c 'import json,sys; sys.exit(json.load(open(sys.argv[1])).get("status")!="ok")' "$R/meta.json"; then
  die "run $OLD_ID already finished ok; continue it with --resume <session_id> instead"
fi

# Validate the destination before killing processes or checkpointing work.
if [ -z "$NEW_ID" ]; then
  base="${OLD_ID%-r[0-9]*}"; n=2
  while [ -e "$ROOT/$base-r$n" ]; do n=$((n + 1)); done
  NEW_ID="$base-r$n"
fi
case "$NEW_ID" in [!a-zA-Z0-9]*|*[!a-zA-Z0-9._-]*) die "invalid new run id";; esac
[ -e "$ROOT/$NEW_ID" ] && die "run dir already exists: $ROOT/$NEW_ID"
# Native recovery must not switch agent, workdir, session, run id or brief through overrides.
if [ "$NATIVE" = 1 ]; then
  i=0
  while [ "$i" -lt "${#EXTRA[@]}" ]; do
    case "${EXTRA[$i]}" in
      --model|--reasoning|--permission|--timeout|--input)
        [ $((i + 1)) -lt "${#EXTRA[@]}" ] || die "missing native override value"
        i=$((i + 2));;
      --read-only) i=$((i + 1));;
      *) die "unsupported --native override: ${EXTRA[$i]}";;
    esac
  done
fi
[ -f "$R/review.diff" ] || grep -q '^Review mode: READ-ONLY' "$R/prompt.md" && die "review runs have no progress to keep; rerun the review from scratch"
OLD_WORKDIR=$(cat "$R/workdir.path" 2>/dev/null || true)
if [ -z "$OLD_WORKDIR" ]; then
  if [ -d "$R/wt" ]; then OLD_WORKDIR="$R/wt"; elif [ -d "$R/work" ]; then OLD_WORKDIR="$R/work"; fi
fi

# Hold ownership across checkpoint and launch, even when an older ID points at a
# live native continuation. Always scan cwd owners; agent.pid is not authoritative.
# shellcheck source-path=SCRIPTDIR
# shellcheck source=worktree_guard.sh
source "$HERE/worktree_guard.sh"
if [ -d "$OLD_WORKDIR" ]; then
  OLD_WORKDIR="$(cd "$OLD_WORKDIR" && pwd -P)"
  KILL_ARG=""; [ "$KILL" = 1 ] && KILL_ARG=--kill
  guard_workdir "$OLD_WORKDIR" "$KILL_ARG" "$DRY" || exit $?
fi

# --- agent, model, reasoning of the old run ---
eval "$(python3 - "$R" "$HERE" "$OLD_WORKDIR" <<'PY'
import json, os, shlex, sys
sys.path.insert(0, sys.argv[2])
from session_state import session_info
r = sys.argv[1]; out = {"AGENT": "", "MODEL": "", "REASONING": "", "PERMISSION": ""}
try:
    m = json.load(open(os.path.join(r, "meta.json")))
    out.update(AGENT=m.get("agent") or "", MODEL=m.get("model") or "", REASONING=m.get("reasoning") or "", PERMISSION=m.get("permission") or "")
except Exception:
    pass
try:
    t = shlex.split(open(os.path.join(r, "cmd.txt")).read())
    out["AGENT"] = out["AGENT"] or os.path.basename(t[0])
    permission = ""
    for i, a in enumerate(t[:-1]):
        n = t[i + 1]
        if a in ("--model", "-m") and not out["MODEL"]: out["MODEL"] = n
        if a in ("--thinking", "--effort") and not out["REASONING"]: out["REASONING"] = n
        if a == "-c" and n.startswith("model_reasoning_effort=") and not out["REASONING"]: out["REASONING"] = n.split("=", 1)[1].strip('"')
        if a == "--permission-mode": permission = {"plan": "read-only", "acceptEdits": "safe", "bypassPermissions": "full-auto"}.get(n, permission)
        if a == "--approval-mode": permission = {"write": "safe", "yolo": "full-auto"}.get(n, permission)
        if a == "--dangerously-bypass-approvals-and-sandbox": permission = "full-auto"
        if a == "-s": permission = {"read-only": "read-only", "workspace-write": "safe"}.get(n, permission)
        if a == "-c" and n.startswith("sandbox_mode="):
            permission = {"read-only": "read-only", "workspace-write": "safe"}.get(n.split("=", 1)[1].strip('"'), permission)
        if a == "--tools": permission = "read-only"
    out["PERMISSION"] = out["PERMISSION"] or permission
except Exception:
    pass
# Unknown old permissions must not silently expand to the config's full-auto default.
out["PERMISSION"] = out["PERMISSION"] or "safe"
session = session_info(out["AGENT"], r, sys.argv[3])
out.update(SESSION_ID=session["session_id"] or "", SESSION_FILE=session["session_file"] or "")
print("\n".join(f"{k}={shlex.quote(v)}" for k, v in out.items()))
PY
)"
[ -n "$AGENT" ] || die "cannot tell which agent ran $OLD_ID; pass -- --agent NAME"

# --- commit work in progress ---
SRC_CWD="" KIND=work
[ -d "$OLD_WORKDIR" ] && SRC_CWD="$(cd "$OLD_WORKDIR" && pwd -P)"
if [ -n "$SRC_CWD" ] && git -C "$SRC_CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  KIND=repo
  if [ -n "$(git -C "$SRC_CWD" status --porcelain)" ]; then
    if [ "$DRY" = 0 ]; then
      ID_ARGS=(); git -C "$SRC_CWD" config user.email >/dev/null || ID_ARGS=(-c user.name=grok-bot-agents -c user.email=grok-bot-agents@localhost)
      { git -C "$SRC_CWD" add -A && git -C "$SRC_CWD" "${ID_ARGS[@]}" commit -q --no-verify -m "wip($OLD_ID): checkpoint before resume"; } \
        || die "could not commit WIP in $SRC_CWD"
    fi
    [ "$DRY" = 1 ] && echo "would commit WIP on $(git -C "$SRC_CWD" rev-parse --abbrev-ref HEAD)" >&2 || echo "committed WIP on $(git -C "$SRC_CWD" rev-parse --abbrev-ref HEAD)" >&2
  fi
fi
SESSION="$SESSION_ID"
case "$AGENT" in pi|omp) SESSION="$SESSION_FILE";; esac
if [ "$NATIVE" = 1 ] && { [ -z "$SESSION_ID" ] || [ -z "$SESSION" ] || [ -z "$SRC_CWD" ]; }; then
  echo "native resume unavailable: missing session id, owned session file or workdir; falling back to fresh-session resume in a new workdir" >&2
  NATIVE=0
fi

# --- continuation brief (dry-run never writes even a temporary file) ---
BRIEF="$R/continue-$NEW_ID.md"
continuation_brief() {
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
  echo "Before repeating any interrupted tool action (such as a commit or install), verify its actual state and side effects: it may have completed even if its result was not recorded."
  echo "Finish with the same final message format the brief asks for, covering the whole task."
}

CMD=(bash "$HERE/run-agent.sh" --agent "$AGENT" --id "$NEW_ID" --prompt-file "$BRIEF")
[ -n "$SRC_CWD" ] && CMD+=(--cwd "$SRC_CWD")
[ -n "$MODEL" ] && CMD+=(--model "$MODEL")
[ -n "$REASONING" ] && CMD+=(--reasoning "$REASONING")
if [ "$PERMISSION" = read-only ]; then CMD+=(--read-only); else CMD+=(--permission "$PERMISSION"); fi
CMD+=("${EXTRA[@]}")
[ "$NATIVE" = 1 ] && CMD+=(--resume "$SESSION" --resume-in-place)

if [ "$DRY" = 1 ]; then
  echo "plan: agent=$AGENT kind=$KIND old=$OLD_ID new=$NEW_ID native=$NATIVE brief=$BRIEF" >&2
  echo "brief preview:" >&2; continuation_brief | tail -6 >&2
  printf 'AGENT_RUNS_ROOT=%q ' "$ROOT"; printf '%q ' "${CMD[@]}"; echo
  exit 0
fi
if ! { continuation_brief > "$BRIEF.tmp" && mv "$BRIEF.tmp" "$BRIEF"; }; then die "could not write continuation brief"; fi
python3 -c 'import json,sys; print(json.dumps({"resumed_from":sys.argv[1],"new_id":sys.argv[2]}))' "$OLD_ID" "$NEW_ID" > "$R/resumed.json"
export AGENT_RUNS_ROOT="$ROOT"
if [ "$NATIVE" = 1 ]; then
  SUMMARY=$("${CMD[@]}"); RC=$?
  if [ "$RC" != 0 ] && python3 - "$HERE" "$ROOT/$NEW_ID" <<'PY'
import sys
sys.path.insert(0, sys.argv[1])
from session_state import resume_rejected
sys.exit(0 if resume_rejected(sys.argv[2]) else 1)
PY
  then
    echo "CLI rejected native resume before doing work; falling back to fresh-session resume in a new workdir (attempt logs: $ROOT/$NEW_ID)" >&2
    exec bash "$HERE/resume-run.sh" "$ROOT/$NEW_ID" -- "${EXTRA[@]}"
  fi
  printf '%s\n' "$SUMMARY"; exit "$RC"
fi
exec "${CMD[@]}"
