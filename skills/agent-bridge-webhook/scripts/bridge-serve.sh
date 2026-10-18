#!/usr/bin/env bash
# Keep-alive for the box side of the return channel. Idempotent; safe to call from ~/.bashrc.
#   bridge-serve.sh [start]   start tailscaled (userspace) and bridge_server.py if they aren't running
#   bridge-serve.sh status    show what's running
#   bridge-serve.sh stop      stop bridge_server.py (tailscaled is left alone)
#   bridge-serve.sh ts-start  start only tailscaled and wait until its socket answers (used by setup-tailscale.sh)
#   bridge-serve.sh ts-stop   stop only tailscaled (login state stays in $TS_STATE)
# Logs and pid files: ~/.local/state/grok-bot-agents/  (tailscaled log: $TS_STATE/tailscaled.log)
# Env: GROK_BRIDGE_PORT (8787), TS_STATE (~/.local/state/tailscale), TAILSCALED (~/.local/bin/tailscaled), TS_SOCKS5 (localhost:1055)
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
case "${1:-start}" in
  start) flock -n 9 || exit 0 ;;                       # another start is running: nothing to do
  ts-start|ts-stop) flock -w 30 9 || { echo "serve.lock busy" >&2; exit 1; } ;;
  *) flock -n 9 || true ;;                             # status/stop never block
esac

TS_CLI="$(dirname "$TAILSCALED")/tailscale"
ts_running() { pgrep -u "$(id -u)" -f "tailscaled .*--socket=$TS_SOCK" >/dev/null; }
ts_answers() { [ -S "$TS_SOCK" ] && "$TS_CLI" --socket="$TS_SOCK" status --json >/dev/null 2>&1; }
srv_running() { curl -fsS -m 2 "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; }

start_ts() {
  ts_running && return 0
  [ -x "$TAILSCALED" ] || { echo "no $TAILSCALED; skipping tailscaled" >&2; return 0; }
  mkdir -p "$TS_STATE"
  setsid nohup "$TAILSCALED" --tun=userspace-networking --statedir="$TS_STATE" --socket="$TS_SOCK" \
    --socks5-server="${TS_SOCKS5:-localhost:1055}" >>"$TS_STATE/tailscaled.log" 2>&1 </dev/null 9>&- &
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
  ts-start)
    [ -x "$TAILSCALED" ] || { echo "no $TAILSCALED (run setup-tailscale.sh)" >&2; exit 1; }
    start_ts
    for _ in $(seq 1 50); do ts_answers && exit 0; sleep 0.3; done
    echo "tailscaled did not answer on $TS_SOCK; see $TS_STATE/tailscaled.log" >&2; exit 1 ;;
  ts-stop)
    pkill -u "$(id -u)" -f "tailscaled .*--socket=$TS_SOCK" 2>/dev/null
    for _ in $(seq 1 30); do ts_running || break; sleep 0.3; done
    ts_running && { echo "tailscaled still running" >&2; exit 1; }
    echo "$(date '+%F %T') stopped tailscaled" >>"$LOGDIR/serve.log"; echo "tailscaled stopped" ;;
  status)
    ts_running && echo "tailscaled: running" || echo "tailscaled: not running"
    srv_running && echo "bridge_server: up on 127.0.0.1:$PORT" || echo "bridge_server: down"
    [ -S "$TS_SOCK" ] && "$TS_CLI" --socket="$TS_SOCK" ip -4 2>/dev/null | sed 's/^/tailnet ip: /' ;;
  stop)
    pkill -u "$(id -u)" -f "bridge_server.py serve --port $PORT" 2>/dev/null
    rm -f "$LOGDIR/bridge-server.pid"; echo "bridge_server stopped" ;;
  *) sed -n '2,10p' "$0"; exit 2 ;;
esac
