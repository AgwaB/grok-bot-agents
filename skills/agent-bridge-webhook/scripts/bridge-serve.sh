#!/usr/bin/env bash
# Keep-alive for the box side of the return channel. Idempotent; safe to call from ~/.bashrc.
#   bridge-serve.sh [start]   start tailscaled (userspace) and bridge_server.py if they aren't running
#   bridge-serve.sh status    show what's running
#   bridge-serve.sh stop      stop bridge_server.py (tailscaled is left alone)
# Logs and pid files: ~/.local/state/grok-bot-agents/  (tailscaled log: $TS_STATE/tailscaled.log)
# Env: GROK_BRIDGE_PORT (8787), TS_STATE (~/.local/state/tailscale), TAILSCALED (~/.local/bin/tailscaled)
# tailscaled keeps its login in $TS_STATE, so a restart reconnects without `tailscale up`.
set -u
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PORT="${GROK_BRIDGE_PORT:-8787}"
TS_STATE="${TS_STATE:-$HOME/.local/state/tailscale}"
TAILSCALED="${TAILSCALED:-$HOME/.local/bin/tailscaled}"
TS_SOCK="$TS_STATE/tailscaled.sock"
LOGDIR="$HOME/.local/state/grok-bot-agents"
mkdir -p "$LOGDIR"; chmod 700 "$LOGDIR"
exec 9>"$LOGDIR/serve.lock"
flock -n 9 || { [ "${1:-start}" = start ] && exit 0; }

ts_running() { pgrep -u "$(id -u)" -f "tailscaled .*--socket=$TS_SOCK" >/dev/null; }
srv_running() { curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; }

start_ts() {
  ts_running && return 0
  [ -x "$TAILSCALED" ] || { echo "no $TAILSCALED; skipping tailscaled" >&2; return 0; }
  mkdir -p "$TS_STATE"
  setsid nohup "$TAILSCALED" --tun=userspace-networking --statedir="$TS_STATE" --socket="$TS_SOCK" \
    --socks5-server=localhost:1055 >>"$TS_STATE/tailscaled.log" 2>&1 </dev/null 9>&- &
  echo "$(date '+%F %T') started tailscaled pid $!" >>"$LOGDIR/serve.log"
}

start_srv() {
  srv_running && return 0
  setsid nohup python3 "$HERE/bridge_server.py" serve --port "$PORT" >>"$LOGDIR/bridge-server.log" 2>&1 </dev/null 9>&- &
  echo $! >"$LOGDIR/bridge-server.pid"
  echo "$(date '+%F %T') started bridge_server pid $! port $PORT" >>"$LOGDIR/serve.log"
  for _ in 1 2 3 4 5 6 7 8 9 10; do srv_running && return 0; sleep 0.3; done
  echo "bridge_server did not come up; see $LOGDIR/bridge-server.log" >&2; return 1
}

case "${1:-start}" in
  start) start_ts; start_srv ;;
  status)
    ts_running && echo "tailscaled: running" || echo "tailscaled: not running"
    srv_running && echo "bridge_server: up on 127.0.0.1:$PORT" || echo "bridge_server: down"
    [ -S "$TS_SOCK" ] && "$(dirname "$TAILSCALED")/tailscale" --socket="$TS_SOCK" ip -4 2>/dev/null | sed 's/^/tailnet ip: /' ;;
  stop)
    pkill -u "$(id -u)" -f "bridge_server.py serve --port $PORT" 2>/dev/null
    rm -f "$LOGDIR/bridge-server.pid"; echo "bridge_server stopped" ;;
  *) sed -n '2,8p' "$0"; exit 2 ;;
esac
