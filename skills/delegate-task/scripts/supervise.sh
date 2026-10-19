#!/usr/bin/env bash
# Watch one run-agent.sh run for a bounded slice and print ONE machine-readable status line.
# A supervisor subagent calls this back to back; while it waits in Shell, the box stays awake.
# Usage:
#   supervise.sh RUN_ID|RUN_DIR [MAX_SECONDS] [--new] [--slice S] [--interval S] [--stall-min M]
#   MAX_SECONDS  this supervisor's whole budget (default 1800 = 30 min), kept in <run-dir>/supervise.deadline
#   --new        start a fresh budget (the first call of each new supervisor)
#   --slice S    longest single invocation (default 600; stays under the 780 s Shell call limit)
#   --interval S seconds between checks (default 30)
#   --stall-min M  no output growth and no CPU for M minutes = stalled (default 10)
# Output (stdout, one line):
#   status=running|done|failed|stalled next=continue|return run_dir=DIR reason=... idle=Ns cpu_delta=Ns forks=N remaining=Ns resumable=0|1
#   next=continue: call again.  next=return: stop and hand the line back (budget used, or run ended / stalled).
#   failed reason=conn_error: the run ended on a dropped connection or model stream (meta.json error_kind).
#   resumable=1: restart it with resume-run.sh (conn_error, died_no_meta, stalled).
# Exit: 0 running|done, 1 failed, 2 stalled, 64 usage error. Details go to <run-dir>/supervise.log.
set -uo pipefail

ARG="" MAX=1800 NEW=0 SLICE=600 INTERVAL=30 STALL_MIN=10
while [ $# -gt 0 ]; do
  case "$1" in
    --new) NEW=1; shift;;
    --slice) SLICE="$2"; shift 2;;
    --interval) INTERVAL="$2"; shift 2;;
    --stall-min) STALL_MIN="$2"; shift 2;;
    -h|--help) sed -n '2,16p' "$0"; exit 0;;
    -*) echo "status=failed next=return run_dir= reason=usage:unknown_arg_$1"; exit 64;;
    *) if [ -z "$ARG" ]; then ARG="$1"; else MAX="$1"; fi; shift;;
  esac
done
[ -n "$ARG" ] || { echo "status=failed next=return run_dir= reason=usage:missing_run_id"; exit 64; }
for v in "$MAX" "$SLICE" "$INTERVAL" "$STALL_MIN"; do
  case "$v" in ''|*[!0-9]*) echo "status=failed next=return run_dir= reason=usage:not_a_number_$v"; exit 64;; esac
done
[ "$SLICE" -le 720 ] || SLICE=720   # one invocation must fit in a single Shell call

# Same run-dir root as run-agent.sh: $AGENT_RUNS_ROOT > config runs_dir > /workspace/agent-runs.
runs_root() {
  local cfg="${GROK_BOT_AGENTS_CONFIG:-${GROK_AGENTS_CONFIG:-$HOME/.grok-bot-agents/config.json}}" root="/workspace/agent-runs"
  if [ -z "${GROK_BOT_AGENTS_CONFIG:-}${GROK_AGENTS_CONFIG:-}" ] && [ ! -e "$cfg" ] && [ -e "$HOME/.grok-agents/config.json" ]; then cfg="$HOME/.grok-agents/config.json"; fi
  if [ -f "$cfg" ]; then
    root=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("runs_dir") or "/workspace/agent-runs")' "$cfg" 2>/dev/null || echo /workspace/agent-runs)
  fi
  echo "${AGENT_RUNS_ROOT:-$root}"
}
case "$ARG" in /*) R="${ARG%/}";; *) R="$(runs_root)/$ARG";; esac
LOG="$R/supervise.log" SAMPLES="$R/supervise.samples" DLF="$R/supervise.deadline"
STALL=$((STALL_MIN * 60))
TCK=$(getconf CLK_TCK 2>/dev/null || echo 100)
# Language servers that agents keep running idle; they don't count as "busy" CPU.
IGNORE_RE="${SUPERVISE_IGNORE_RE:-marksman|language-?server|langserver|tsserver|gopls|rust-analyzer|pyright|clangd}"

now() { date +%s; }
ts() { date '+%F %T'; }

emit() {  # status next reason [exit]
  local res=0
  case "$1:$3" in stalled:*|failed:conn_error|failed:died_no_meta) res=1;; esac
  local line="status=$1 next=$2 run_dir=$R reason=$3 idle=${IDLE:-0}s cpu_delta=${CPUD:--1}s forks=${FORKS:--1} remaining=${REM:-0}s resumable=$res"
  [ -d "$R" ] && echo "$(ts) $line" >> "$LOG"
  [ "$2" = return ] && rm -f "$DLF"
  echo "$line"
  case "$1" in running|done) exit 0;; stalled) exit 2;; *) exit 1;; esac
}

[ -d "$R" ] || emit failed return no_run_dir

fork_count() { dmesg 2>/dev/null | grep -c 'virtual machine fork' || true; }

# Agent process: <run-dir>/agent.pid (written by run-agent.sh), else the process whose cwd is the run's work dir.
agent_pid() {
  local p c
  if [ -f "$R/agent.pid" ]; then   # trust it only while it is still `timeout` (pids are reused after a box restart)
    p=$(cat "$R/agent.pid" 2>/dev/null); [ -n "$p" ] && [ "$(cat "/proc/$p/comm" 2>/dev/null)" = timeout ] && echo "$p"; return
  fi
  for c in /proc/[0-9]*; do
    p=${c#/proc/}
    case "$(readlink "$c/cwd" 2>/dev/null)" in "$R/wt"|"$R/work") ;; *) continue;; esac
    [ "$(cat "$c/comm" 2>/dev/null)" = timeout ] && { echo "$p"; return; }
  done
}

# CPU seconds (x100) used so far by the agent's process tree, ignoring idle language servers below the agent.
tree_cpu() {
  python3 - "$1" "$TCK" "$IGNORE_RE" <<'PY'
import os, re, sys
root, tck, ign = int(sys.argv[1]), int(sys.argv[2]), re.compile(sys.argv[3])
procs = {}
for d in os.listdir("/proc"):
    if not d.isdigit(): continue
    try:
        s = open(f"/proc/{d}/stat").read(); rest = s[s.rindex(")") + 2:].split()
        cmd = open(f"/proc/{d}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "replace")
        procs[int(d)] = (int(rest[1]), int(rest[11]) + int(rest[12]), cmd)
    except Exception: pass
kids = {}
for p, (pp, _, _) in procs.items(): kids.setdefault(pp, []).append(p)
tot, busy, stack = 0, 0, [(root, 0)]
while stack:
    p, depth = stack.pop()
    if p not in procs: continue
    _, t, cmd = procs[p]
    idle_ls = depth >= 2 and ign.search(cmd)
    if not idle_ls:
        tot += t
        if depth >= 2: busy += 1
    stack += [(k, depth + 1) for k in kids.get(p, [])]
print(tot * 100 // tck, busy)
PY
}

out_mtime() {  # newest write among the run's logs and session files
  { stat -c %Y "$R/stdout.log" "$R/stderr.log" 2>/dev/null
    [ -d "$R/sessions" ] && find "$R/sessions" -type f -printf '%T@\n' 2>/dev/null | cut -d. -f1
    stat -c %Y "$R/prompt.md" 2>/dev/null; } | sort -n | tail -1
}

T0=$(now)
if [ "$NEW" = 1 ] || [ ! -f "$DLF" ]; then echo $((T0 + MAX)) > "$DLF"; fi
DEADLINE=$(cat "$DLF" 2>/dev/null || echo $((T0 + MAX)))
SLICE_END=$((T0 + SLICE))
FORKS=$(fork_count)
echo "$(ts) slice start max=${MAX}s deadline=$(date -d "@$DEADLINE" '+%T') stall=${STALL}s forks=$FORKS" >> "$LOG"

while :; do
  T=$(now); REM=$((DEADLINE - T)); [ "$REM" -lt 0 ] && REM=0
  if [ -f "$R/meta.json" ]; then
    # status, or error_kind when set (conn_error = dropped connection/stream, resumable)
    st=$(python3 -c 'import json,sys; m=json.load(open(sys.argv[1])); print(m.get("error_kind") or m.get("status") or "error")' "$R/meta.json" 2>/dev/null || echo error)
    [ "$st" = ok ] && emit "done" return ok
    emit failed return "$st"
  fi
  F=$(fork_count)
  if [ -n "$F" ] && [ "${FORKS:-0}" -ge 0 ] && [ "$F" -gt "${FORKS:-0}" ]; then
    echo "$(ts) VM_RESUME forks ${FORKS}->$F" >> "$LOG"; echo "$T" > "$R/supervise.lastwake"
  fi
  FORKS=$F
  P=$(agent_pid)
  MT=$(out_mtime); MT=${MT:-$T}
  WAKE=$(cat "$R/supervise.lastwake" 2>/dev/null || echo 0)
  # Stall clock restarts at a VM resume, so a run gets a full window to recover after waking.
  REF=$MT; [ "$WAKE" -gt "$REF" ] && REF=$WAKE
  IDLE=$((T - REF))
  if [ -z "$P" ]; then
    # Runner may still be summarizing (meta.json lands a moment later), or the run just started.
    RP=$(cat "$R/runner.pid" 2>/dev/null || true)
    if [ -n "$RP" ] && [ -d "/proc/$RP" ]; then :
    elif [ $((T - $(stat -c %Y "$R/prompt.md" 2>/dev/null || echo "$T"))) -lt 60 ]; then :
    else sleep 3; [ -f "$R/meta.json" ] && continue; CPUD=-1; emit failed return died_no_meta
    fi
    CPU=0 BUSY=0
  else
    read -r CPU BUSY < <(tree_cpu "$P")
  fi
  [ -n "$P" ] && echo "$T $CPU $MT" >> "$SAMPLES"
  # CPU used over the stall window: compare with the newest sample at least STALL seconds old.
  OLD=""; [ -n "$P" ] && OLD=$(awk -v lim=$((T - STALL)) '$1 <= lim {c=$2} END {print c}' "$SAMPLES")
  if [ -n "$OLD" ]; then CPUD=$(( (CPU - OLD) / 100 )); else CPUD=-1; fi
  echo "$(ts) pid=${P:-none} idle=${IDLE}s cpu=$((CPU / 100))s cpu_delta=${CPUD}s busy_kids=$BUSY forks=$FORKS remaining=${REM}s" >> "$LOG"
  if [ -n "$P" ] && [ "$IDLE" -ge "$STALL" ] && [ -n "$OLD" ] && [ $((CPU - OLD)) -lt 200 ]; then
    emit stalled return "no_output_no_cpu_${STALL_MIN}m"
  fi
  [ "$T" -ge "$DEADLINE" ] && emit running return budget_used
  [ $((T + INTERVAL)) -gt "$SLICE_END" ] && emit running continue slice_end
  sleep "$INTERVAL"
done
