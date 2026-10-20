#!/usr/bin/env bash
# Restart a run that stalled, died, or ended on a dropped connection (for example after a box VM pause): stop what is left of it,
# commit its work in progress on branch agent/<id>, and relaunch run-agent.sh from that worktree
# with a continuation brief. Start this script itself as the background Shell command; it ends in
# run-agent.sh and prints the same JSON summary.
# Usage:
#   resume-run.sh RUN_ID|RUN_DIR [--native] [--new-id ID] [--kill] [--dry-run] [--allow-extra-restart] [-- overrides...]
#   --native   opt in to the CLI session in the SAME workdir; fresh-session fallback on missing/rejected resume
#   --kill     stop the old process tree if it is still alive (required for a stalled, not dead, run)
#   --new-id   id of the new run (default <id>-r2, -r3, ...)
#   --dry-run  do the checks, print the plan and the run-agent.sh command; change nothing
#   --allow-extra-restart  explicit USER-approved attempt beyond TWO lineage restarts
# Agent, model and reasoning are taken from the old run's meta.json or cmd.txt; overrides after -- win.
set -uo pipefail
umask 077
HERE="$(cd "$(dirname "$0")" && pwd)"
ARG="" NEW_ID="" KILL=0 DRY=0 NATIVE=0 ALLOW_EXTRA=0 EXTRA=()
while [ $# -gt 0 ]; do
  case "$1" in
    --new-id) [ $# -ge 2 ] || exit 64; NEW_ID="$2"; shift 2;;
    --allow-extra-restart) ALLOW_EXTRA=1; shift;;
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

# Read-only planning checks the whole lineage, even when the original ID is reused.
# A second check under the ledger lock below closes races before any checkpoint.
PLAN=$(python3 "$HERE/run_lifecycle.py" plan "$R" "$NEW_ID" "$ALLOW_EXTRA"); RC=$?
[ "$RC" = 0 ] || { printf '%s\n' "$PLAN"; exit "$RC"; }
NEW_ID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["id"])' "$PLAN")
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
[ ! -d "$OLD_WORKDIR" ] || OLD_WORKDIR="$(cd "$OLD_WORKDIR" && pwd -P)"

# --- agent, model, reasoning of the old run ---
RECOVERY=$(python3 - "$R" "$HERE" "$OLD_WORKDIR" 2>/dev/null <<'PY'
import json, os, shlex, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
from session_state import session_info
from run_lifecycle import read_json, recovery_inputs
r = sys.argv[1]; out = {"AGENT": "", "MODEL": "", "REASONING": "", "PERMISSION": "", "TIMEOUT": "1800"}
# New receipts contain the launch-time effective inputs, including a genuinely
# non-repo cwd. Legacy receipts restore explicit inputs only, never inferred cwd.
launch = read_json(Path(r) / "launch.json")
inputs = recovery_inputs(launch)
for key in ("AGENT", "MODEL", "REASONING", "PERMISSION"):
    value = launch.get(key.lower())
    if isinstance(value, str):
        out[key] = value
timeout = launch.get("timeout_sec")
if type(timeout) is int and timeout > 0:
    out["TIMEOUT"] = str(timeout)
try:
    m = json.load(open(os.path.join(r, "meta.json")))
    out.update(AGENT=m.get("agent") or out["AGENT"], MODEL=m.get("model") or out["MODEL"], REASONING=m.get("reasoning") or out["REASONING"], PERMISSION=m.get("permission") or out["PERMISSION"])
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
print("ORIGINAL_INPUTS=(" + " ".join(shlex.quote(p) for p in inputs) + ")")
PY
); RC=$?
[ "$RC" = 0 ] || die "invalid_run_metadata" 6
eval "$RECOVERY"
[ -n "$AGENT" ] || die "cannot tell which agent ran $OLD_ID; pass -- --agent NAME"

if [ -d "$OLD_WORKDIR" ]; then
  KILL_ARG=""; [ "$KILL" = 1 ] && KILL_ARG=--kill
  # Validate inputs and perform a write-free ownership preflight before any
  # handoff. A refused recovery must not silently cancel a working webhook.
  guard_workdir "$OLD_WORKDIR" "$KILL_ARG" 1 || exit $?
  if [ "$DRY" = 0 ]; then
    # Claim BEFORE stopping the old runner; its finalizer uses the same lock.
    HANDOFF=$(python3 "$HERE/run_lifecycle.py" supervisor-handoff "$R"); RC=$?
    [ "$RC" = 0 ] || { printf '%s\n' "$HANDOFF"; exit "$RC"; }
    guard_workdir "$OLD_WORKDIR" "$KILL_ARG" 0 || exit $?
  fi
fi

# Reserve only after ownership checks succeed. Reservations are never recycled:
# startup failures and native-rejection fallbacks are real restart attempts too.
if [ "$DRY" = 0 ]; then
  PLAN=$(python3 "$HERE/run_lifecycle.py" reserve "$R" "$NEW_ID" "$ALLOW_EXTRA"); RC=$?
  [ "$RC" = 0 ] || { printf '%s\n' "$PLAN"; exit "$RC"; }
fi
GBA_RESTART_ROOT=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["root_id"])' "$PLAN")
export GBA_RESTART_ROOT

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
CMD+=(--timeout "$TIMEOUT")
for input in "${ORIGINAL_INPUTS[@]}"; do CMD+=(--input "$input"); done
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
# Publish legacy navigation atomically too; reservations remain authoritative.
python3 - "$HERE" "$R" "$OLD_ID" "$NEW_ID" <<'PY' || die "lifecycle_unavailable" 6
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from run_lifecycle import write_json
write_json(Path(sys.argv[2]) / "resumed.json", {"resumed_from": sys.argv[3], "new_id": sys.argv[4]})
PY
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
    # Approval applies to this requested extra attempt only, not an unbounded
    # automatic fallback after it. The fallback must pass the same cap again.
    exec bash "$HERE/resume-run.sh" "$ROOT/$NEW_ID" -- "${EXTRA[@]}"
  fi
  printf '%s\n' "$SUMMARY"; exit "$RC"
fi
exec "${CMD[@]}"
