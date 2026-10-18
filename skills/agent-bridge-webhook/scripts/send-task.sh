#!/usr/bin/env bash
# Sender for the LOCAL machine or CI: posts one task to the Grok Bot "Agent bridge" webhook routine.
# Config (never commit it): ~/.config/grok-agents/bridge.env, mode 600, or the same variables in env:
#   GROKBOT_WEBHOOK_URL=...      copied from the routine panel
#   GROKBOT_WEBHOOK_KEY=...      sender key from the routine panel
#   GROKBOT_WEBHOOK_HEADER=...   header name the panel says to send the key in
# Usage: send-task.sh "task" [context-file]   -> prints the request_id
set -euo pipefail
CONF="${GROK_BRIDGE_CONF:-$HOME/.config/grok-agents/bridge.env}"
if [ -f "$CONF" ]; then
  perm=$(stat -c %a "$CONF" 2>/dev/null || stat -f %Lp "$CONF")
  [ "$perm" = 600 ] || { echo "refusing $CONF: chmod 600 it first" >&2; exit 1; }
  set -a; . "$CONF"; set +a
fi
: "${GROKBOT_WEBHOOK_URL:?set GROKBOT_WEBHOOK_URL in $CONF}"
: "${GROKBOT_WEBHOOK_KEY:?set GROKBOT_WEBHOOK_KEY in $CONF}"
: "${GROKBOT_WEBHOOK_HEADER:?set GROKBOT_WEBHOOK_HEADER in $CONF}"
TASK="${1:?usage: send-task.sh \"task\" [context-file]}"
CTX_FILE="${2:-}"
REQ_ID="req-$(date +%Y%m%d-%H%M%S)-$RANDOM"
BODY=$(mktemp); HDR=$(mktemp); trap 'rm -f "$BODY" "$HDR"' EXIT
python3 - "$REQ_ID" "$TASK" "$CTX_FILE" > "$BODY" <<'PY'
import json, sys
ctx = open(sys.argv[3], encoding="utf-8", errors="replace").read(200_000) if sys.argv[3] else ""
print(json.dumps({"request_id": sys.argv[1], "task": sys.argv[2], "context": ctx,
                  "reply": {"type": "outbox"}}))
PY
# key goes through a header file so it doesn't show up in `ps`
printf '%s: %s\n' "$GROKBOT_WEBHOOK_HEADER" "$GROKBOT_WEBHOOK_KEY" > "$HDR"
curl -fsS -X POST "$GROKBOT_WEBHOOK_URL" -H "Content-Type: application/json" -H "@$HDR" --data-binary "@$BODY" >/dev/null
echo "$REQ_ID"
