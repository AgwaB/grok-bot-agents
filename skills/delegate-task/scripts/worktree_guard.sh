#!/usr/bin/env bash
# Sourced by both entry points. fd 9 stays open across checkpoint, launch and cleanup.
guard_workdir() {
  local workdir="$1" kill_arg="${2:-}" dry="${3:-0}" helper lock
  helper="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/worktree_guard.py"
  if [ "$dry" = 1 ]; then
    python3 "$helper" check "$workdir" "$$" "$kill_arg"
    return $?
  fi
  if ! python3 "$helper" inherited "$workdir"; then
    lock=$(python3 "$helper" path "$workdir") || return 5
    mkdir -p "$(dirname "$lock")" || return 5
    exec 9>>"$lock" || return 5
  fi
  python3 "$helper" acquire "$workdir" "$$" "$kill_arg" || return $?
  export DELEGATE_LOCK_WORKDIR="$workdir"
}
