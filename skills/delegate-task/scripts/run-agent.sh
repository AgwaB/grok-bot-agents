#!/usr/bin/env bash
# Run one coding-agent CLI headless, log everything to a run dir, print only a short JSON summary.
# Defaults come from ~/.grok-bot-agents/config.json ($GROK_BOT_AGENTS_CONFIG; legacy ~/.grok-agents / $GROK_AGENTS_CONFIG); args override them.
# Usage:
#   run-agent.sh --agent codex|claude|pi|omp [--cwd DIR] [--input DIR ...] (--prompt-file FILE | --prompt TEXT)
#                [--model M] [--reasoning LEVEL] [--permission full-auto|safe] [--read-only]
#                [--timeout SECONDS] [--id RUN_ID] [--resume SESSION] [--review DIR [--base REF]]
# --cwd is a git repo  -> its own worktree + branch agent/<run-id> (always).
# --review DIR [--base REF] -> read-only review in place (no worktree); fails if DIR changes.
# otherwise / no --cwd -> works in <run-dir>/work; a non-repo --cwd and --input dirs become extra readable dirs.
set -uo pipefail

REVIEW="" BASE="" INPUTS=() AGENT="" CWD="" PROMPT="" PROMPT_FILE="" MODEL="" REASONING="" PERM="" TIMEOUT="" RUN_ID="" READONLY=0 RESUME=""
CONFIG="${GROK_BOT_AGENTS_CONFIG:-${GROK_AGENTS_CONFIG:-$HOME/.grok-bot-agents/config.json}}"
if [ -z "${GROK_BOT_AGENTS_CONFIG:-}${GROK_AGENTS_CONFIG:-}" ] && [ ! -e "$CONFIG" ] && [ -e "$HOME/.grok-agents/config.json" ]; then
  CONFIG="$HOME/.grok-agents/config.json"  # legacy dir from before the rename
fi
# Bot shells often start without the user-level bin dirs, so add them (npm prefix ~/.local, bun ~/.bun).
for d in "$HOME/.local/bin" "$HOME/.bun/bin" "$(npm config get prefix 2>/dev/null)/bin"; do
  [ -d "$d" ] && case ":$PATH:" in *":$d:"*) ;; *) PATH="$d:$PATH";; esac
done; export PATH

die() { python3 -c 'import json,sys; print(json.dumps({"ok":False,"error":sys.argv[1]}))' "$1"; exit "${2:-2}"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --agent) AGENT="$2"; shift 2;;
    --cwd) CWD="$2"; shift 2;;
    --input) INPUTS+=("$2"); shift 2;;
    --prompt) PROMPT="$2"; shift 2;;
    --prompt-file) PROMPT_FILE="$2"; shift 2;;
    --model) MODEL="$2"; shift 2;;
    --reasoning) REASONING="$2"; shift 2;;
    --permission) PERM="$2"; shift 2;;
    --timeout) TIMEOUT="$2"; shift 2;;
    --id) RUN_ID="$2"; shift 2;;
    --read-only) READONLY=1; shift;;
    --review) REVIEW="$2"; READONLY=1; shift 2;;   # review DIR in place, read-only, verified unchanged
    --base) BASE="$2"; shift 2;;
    --resume) RESUME="$2"; shift 2;;
    --worktree) shift;;   # accepted for compatibility; worktrees are always on
    -h|--help) sed -n '2,8p' "$0"; exit 0;;
    *) die "unknown arg: $1";;
  esac
done

[ -n "$AGENT" ] || die "--agent is required"
case "$AGENT" in codex|claude|pi|omp) ;; *) die "unknown agent $AGENT";; esac

# API keys kept out of chat live in ~/.agent-keys (mode 600); load them for non-interactive shells.
[ -f "$HOME/.agent-keys" ] && . "$HOME/.agent-keys"

command -v "$AGENT" >/dev/null || die "$AGENT is not installed; run setup-agents" 3
[ -z "$CWD" ] || [ -d "$CWD" ] || die "cwd not found: $CWD"
for d in "${INPUTS[@]}"; do [ -e "$d" ] || die "input not found: $d"; done

# --- config defaults (args win) ---
CFG_ENABLED="" CFG_MODEL="" CFG_REASONING="" CFG_PERM="full-auto" CFG_TIMEOUT=1800 RUNS_ROOT="/workspace/agent-runs"
if [ -f "$CONFIG" ]; then
  eval "$(python3 - "$CONFIG" "$AGENT" <<'PY'
import json, shlex, sys
c = json.load(open(sys.argv[1])); a = sys.argv[2]; ac = c.get("agents", {}).get(a, {})
out = {"CFG_ENABLED": "1" if a in c.get("enabled", []) else "0",
       "CFG_MODEL": "" if ac.get("model") in (None, "native") else ac["model"],
       "CFG_REASONING": "" if ac.get("reasoning") in (None, "native") else ac["reasoning"],
       "CFG_PERM": c.get("permission_mode", "full-auto"), "CFG_TIMEOUT": str(c.get("timeout_sec", 1800)),
       "RUNS_ROOT": c.get("runs_dir", "/workspace/agent-runs")}
print("\n".join(f"{k}={shlex.quote(v)}" for k, v in out.items()))
PY
)" || die "could not read config $CONFIG"
  [ "$CFG_ENABLED" = 1 ] || die "$AGENT is installed but not enabled in $CONFIG; enable it with configure-agents"
fi
RUNS_ROOT="${AGENT_RUNS_ROOT:-$RUNS_ROOT}"
MODEL="${MODEL:-$CFG_MODEL}"; REASONING="${REASONING:-$CFG_REASONING}"
# "native" (or empty) = pass no flag; the CLI's own config decides (e.g. imported codex model_reasoning_effort).
[ "$MODEL" = native ] && MODEL=""; [ "$REASONING" = native ] && REASONING=""
 PERM="${PERM:-$CFG_PERM}"; TIMEOUT="${TIMEOUT:-$CFG_TIMEOUT}"
case "$PERM" in full-auto|safe) ;; *) die "--permission must be full-auto or safe";; esac
[ "$READONLY" = 1 ] && PERM="read-only"

RUN_ID="${RUN_ID:-$(date +%Y%m%d-%H%M%S)-$AGENT-$RANDOM}"
RUN_DIR="$RUNS_ROOT/$RUN_ID"
mkdir -p "$RUN_DIR"
if [ -n "$PROMPT_FILE" ]; then cp "$PROMPT_FILE" "$RUN_DIR/prompt.md"; else printf '%s\n' "$PROMPT" > "$RUN_DIR/prompt.md"; fi
[ -s "$RUN_DIR/prompt.md" ] || die "empty prompt"

# --- isolation: git repo -> worktree per task (always); otherwise -> <run-dir>/work ---
# --review DIR: no worktree; the agent runs read-only inside DIR, gets review.diff, and DIR is fingerprinted before/after.
WORKTREE="" EXTRA=() FP_BEFORE=""
fingerprint() {  # content hash of the tree (+ git status/HEAD when it's a repo); ignores .git internals
  ( cd "$1" && { git rev-parse HEAD 2>/dev/null; git status --porcelain=v1 2>/dev/null;
    find . -path ./.git -prune -o -type f -print0 | sort -z | xargs -0 sha256sum 2>/dev/null; } | sha256sum | cut -d' ' -f1 )
}
if [ -n "$REVIEW" ]; then
  [ -d "$REVIEW" ] || die "review dir not found: $REVIEW"
  WORKDIR="$(cd "$REVIEW" && pwd)"; KIND="review"
  if git -C "$WORKDIR" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
    if [ -z "$BASE" ]; then
      for b in main master; do git -C "$WORKDIR" rev-parse -q --verify "$b" >/dev/null && { BASE=$(git -C "$WORKDIR" merge-base HEAD "$b"); break; }; done
      BASE="${BASE:-HEAD}"
    fi
    { git -C "$WORKDIR" diff "$BASE"
      git -C "$WORKDIR" ls-files --others --exclude-standard -z | while IFS= read -r -d '' f; do git -C "$WORKDIR" diff --no-index -- /dev/null "$f"; done
    } > "$RUN_DIR/review.diff" 2>/dev/null
    { echo; echo "---"; echo "Review mode: READ-ONLY. Do not edit, create or delete files. The changes under review (vs $BASE, incl. uncommitted and untracked files) are in:"; echo "- $RUN_DIR/review.diff"; } >> "$RUN_DIR/prompt.md"
    EXTRA+=("$RUN_DIR/review.diff")
  fi
  FP_BEFORE=$(fingerprint "$WORKDIR")
elif [ -n "$CWD" ] && git -C "$CWD" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  WORKDIR="$RUN_DIR/wt"; WORKTREE="agent/$RUN_ID"; KIND="repo"
  git -C "$CWD" worktree add -b "agent/$RUN_ID" "$WORKDIR" >>"$RUN_DIR/stderr.log" 2>&1 || die "git worktree add failed; see $RUN_DIR/stderr.log" 4
else
  WORKDIR="$RUN_DIR/work"; KIND="work"; mkdir -p "$WORKDIR"
  [ -n "$CWD" ] && EXTRA+=("$(cd "$CWD" && pwd)")
fi
for d in "${INPUTS[@]}"; do EXTRA+=("$(cd "$(dirname "$d")" && pwd)/$(basename "$d")"); done
ADD_DIRS=()   # --add-dir for claude/codex/omp (pi: paths are listed in the prompt instead)
for d in "${EXTRA[@]}"; do [ -d "$d" ] && ADD_DIRS+=(--add-dir "$d") || ADD_DIRS+=(--add-dir "$(dirname "$d")"); done
if [ ${#EXTRA[@]} -gt 0 ]; then
  if [ -n "$REVIEW" ]; then
    { echo; echo "Inputs (read only):"; printf -- '- %s\n' "${EXTRA[@]}"; } >> "$RUN_DIR/prompt.md"
  else
    { echo; echo "---"; echo "Inputs (read from here; write outputs to the current working directory $WORKDIR):"; printf -- '- %s\n' "${EXTRA[@]}"; } >> "$RUN_DIR/prompt.md"
  fi
fi

# pi auto-retry for this run only (a VM pause of 90 s or more kills pi's model stream; with retry on, pi
# reconnects about 2 s after waking). pi has no per-run settings flag, so the runner drops a project
# .pi/settings.json with only {"retry":{"enabled":true}} into the work dir (deep-merged over the user's
# ~/.pi/agent/settings.json, which is never touched) and passes --approve so print mode loads it.
# Skipped when the work dir already has trust-gated pi resources (their trust stays the user's call),
# for --review (the reviewed dir must not change), and for a pi without --approve. A self-ignoring
# .pi/.gitignore keeps both files out of the agent's commits; they are removed after the run.
PI_RETRY="" PI_RETRY_FILES=()
pi_retry_setup() {
  local d="$WORKDIR/.pi" e cur home
  [ "$KIND" = review ] && { PI_RETRY="skipped: review runs in place"; return 1; }
  for e in settings.json mcp.json extensions skills prompts themes SYSTEM.md APPEND_SYSTEM.md .gitignore; do
    [ -e "$d/$e" ] && { PI_RETRY="skipped: work dir already has .pi/$e"; return 1; }
  done
  home="$(cd "$HOME" 2>/dev/null && pwd -P)"; cur="$(cd "$WORKDIR" && pwd -P)"
  while :; do   # pi also trust-gates project .agents/skills in the work dir or any parent (not ~/.agents/skills)
    [ -e "$cur/.agents/skills" ] && [ "$cur" != "$home" ] && { PI_RETRY="skipped: $cur/.agents/skills needs project trust"; return 1; }
    [ "$cur" = / ] && break; cur="$(dirname "$cur")"
  done
  pi --help 2>/dev/null | grep -q -- '--approve' || { PI_RETRY="skipped: this pi has no --approve"; return 1; }
  [ -d "$d" ] || { mkdir "$d" && PI_RETRY_FILES+=("$d"); }
  if ! { printf '{"retry": {"enabled": true}}\n' > "$d/settings.json" && printf 'settings.json\n.gitignore\n' > "$d/.gitignore"; }; then
    rm -f "$d/settings.json" "$d/.gitignore"; PI_RETRY="skipped: could not write $d"; return 1
  fi
  PI_RETRY_FILES=("$d/settings.json" "$d/.gitignore" ${PI_RETRY_FILES[@]+"${PI_RETRY_FILES[@]}"})
  PI_RETRY="on"
}
pi_retry_cleanup() {  # files first, then the .pi dir only if the runner created it and it is empty
  local f; for f in ${PI_RETRY_FILES[@]+"${PI_RETRY_FILES[@]}"}; do if [ -d "$f" ]; then rmdir "$f" 2>/dev/null; else rm -f "$f"; fi; done
}

P="$(cat "$RUN_DIR/prompt.md")"
CMD=()
case "$AGENT" in
  codex)
    # full-auto: --dangerously-bypass-approvals-and-sandbox | safe: -s workspace-write | read-only: -s read-only
    if [ -n "$RESUME" ]; then CMD=(codex exec resume); else CMD=(codex exec -C "$WORKDIR" "${ADD_DIRS[@]}"); fi
    CMD+=(--json -o "$RUN_DIR/result.md" --skip-git-repo-check)
    case "$PERM" in
      full-auto) CMD+=(--dangerously-bypass-approvals-and-sandbox);;
      safe) if [ -n "$RESUME" ]; then CMD+=(-c 'sandbox_mode="workspace-write"'); else CMD+=(-s workspace-write); fi;;
      read-only) if [ -n "$RESUME" ]; then CMD+=(-c 'sandbox_mode="read-only"'); else CMD+=(-s read-only); fi;;
    esac
    [ -n "$MODEL" ] && CMD+=(-m "$MODEL")
    [ -n "$REASONING" ] && CMD+=(-c "model_reasoning_effort=\"$REASONING\"")
    [ -n "$RESUME" ] && CMD+=("$RESUME")
    CMD+=("$P");;
  claude)
    CMD=(claude -p --output-format json "${ADD_DIRS[@]}")
    case "$PERM" in
      full-auto) CMD+=(--permission-mode bypassPermissions);;
      safe) CMD+=(--permission-mode acceptEdits);;
      read-only) CMD+=(--permission-mode plan);;
    esac
    [ -n "$MODEL" ] && CMD+=(--model "$MODEL")
    [ -n "$REASONING" ] && CMD+=(--effort "$REASONING")
    [ -n "$RESUME" ] && CMD+=(--resume "$RESUME")
    CMD+=("$P");;
  pi)
    # pi has no approval system: full-auto and safe behave the same; read-only limits tools.
    CMD=(pi -p --mode json --session-dir "$RUN_DIR/sessions")
    [ "$PERM" = read-only ] && CMD+=(--tools read,grep,find,ls)
    [ -n "$MODEL" ] && CMD+=(--model "$MODEL")
    [ -n "$REASONING" ] && CMD+=(--thinking "$REASONING")
    [ -n "$RESUME" ] && CMD+=(--session "$RESUME")
    pi_retry_setup && CMD+=(--approve)
    CMD+=("$P");;
  omp)
    # full-auto: --approval-mode yolo | safe: --approval-mode write (exec-tier calls fail closed headless, no hang)
    CMD=(omp -p --mode json --cwd "$WORKDIR" --max-time "$TIMEOUT" --session-dir "$RUN_DIR/sessions" "${ADD_DIRS[@]}")
    case "$PERM" in
      full-auto) CMD+=(--approval-mode yolo);;
      safe) CMD+=(--approval-mode write);;
      read-only) CMD+=(--approval-mode write --tools read,grep,glob);;
    esac
    [ -n "$MODEL" ] && CMD+=(--model "$MODEL")
    [ -n "$REASONING" ] && CMD+=(--thinking "$REASONING")
    [ -n "$RESUME" ] && CMD+=(--resume "$RESUME")
    CMD+=("$P");;
esac

# Log the command without the prompt body.
printf '%q ' "${CMD[@]:0:${#CMD[@]}-1}" > "$RUN_DIR/cmd.txt"; echo '<prompt.md>' >> "$RUN_DIR/cmd.txt"

START=$(date +%s)
# agent.pid / runner.pid let supervise.sh find the process tree (stall checks, restart after a VM pause).
( cd "$WORKDIR" && exec timeout --kill-after=30 "$TIMEOUT" "${CMD[@]}" ) >"$RUN_DIR/stdout.log" 2>>"$RUN_DIR/stderr.log" </dev/null &
AGENT_PID=$!
echo "$AGENT_PID" > "$RUN_DIR/agent.pid"; echo "$$" > "$RUN_DIR/runner.pid"
wait "$AGENT_PID"
RC=$?
END=$(date +%s)
pi_retry_cleanup

REVIEW_MODIFIED=""
if [ -n "$REVIEW" ]; then [ "$(fingerprint "$WORKDIR")" = "$FP_BEFORE" ] || REVIEW_MODIFIED=1; fi
SUMMARY=$(PI_RETRY="$PI_RETRY" REVIEW_MODIFIED="$REVIEW_MODIFIED" AGENT="$AGENT" PERM="$PERM" MODEL="$MODEL" REASONING="$REASONING" WORKTREE="$WORKTREE" KIND="$KIND" \
  python3 "$(dirname "$0")/summarize_run.py" "$AGENT" "$RUN_DIR" "$RC" "$((END-START))" "$WORKDIR"); SRC=$?
# A rate-limited repo run that changed nothing: drop its empty worktree and branch so a retry starts clean.
if [ "$KIND" = repo ] && printf '%s' "$SUMMARY" | grep -q '"status": "rate_limited"' && [ -z "$(git -C "$WORKDIR" status --porcelain 2>/dev/null)" ]; then
  git -C "$CWD" worktree remove --force "$WORKDIR" >/dev/null 2>&1 && git -C "$CWD" branch -D "$WORKTREE" >/dev/null 2>&1
fi
printf '%s\n' "$SUMMARY"; exit $SRC
