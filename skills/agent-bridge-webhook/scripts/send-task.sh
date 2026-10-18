#!/usr/bin/env bash
# Sender for the LOCAL machine or CI: posts one task to the Grok Bot "Agent bridge" webhook routine.
# Config (never commit it): ~/.config/grok-agents/bridge.env, mode 600, or the same variables in env.
# Paste values straight from the routine panel; quotes and spaces are fine (the file is parsed, not sourced):
#   GROKBOT_WEBHOOK_URL=...      Webhook URL
#   GROKBOT_WEBHOOK_HEADER=...   either the whole "Authorization: Bearer ..." line, or just the header name
#   GROKBOT_WEBHOOK_KEY=...      header value (only needed when HEADER is just the name)
# Usage: send-task.sh "task" [context-file]   -> prints the request_id
#        send-task.sh --check                 -> validates the config without sending
set -euo pipefail
CONF="${GROK_BRIDGE_CONF:-$HOME/.config/grok-agents/bridge.env}"
if [ -f "$CONF" ]; then
  perm=$(stat -c %a "$CONF" 2>/dev/null || stat -f %Lp "$CONF")
  [ "$perm" = 600 ] || { echo "refusing $CONF: chmod 600 it first" >&2; exit 1; }
fi
BODY=$(mktemp); HDR=$(mktemp); URLF=$(mktemp); trap 'rm -f "$BODY" "$HDR" "$URLF"' EXIT
CHECK=0; [ "${1:-}" = "--check" ] && CHECK=1
TASK="${1:-}"; CTX_FILE="${2:-}"
[ "$CHECK" = 1 ] || [ -n "$TASK" ] || { echo 'usage: grok-send-task "task" [context-file] | --check' >&2; exit 2; }
REQ_ID="req-$(date +%Y%m%d-%H%M%S)-$RANDOM"
python3 - "$CONF" "$HDR" "$URLF" "$BODY" "$REQ_ID" "$TASK" "$CTX_FILE" "$CHECK" <<'PY'
import json, os, re, sys
conf, hdr_out, url_out, body_out, req_id, task, ctx_file, check = sys.argv[1:]
vals = {}
if os.path.isfile(conf):
    for line in open(conf, encoding="utf-8"):
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip().removeprefix("export ").strip()
        v = v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        vals[k] = v
def get(k):
    return os.environ.get(k) or vals.get(k, "")
url, header, key = get("GROKBOT_WEBHOOK_URL"), get("GROKBOT_WEBHOOK_HEADER"), get("GROKBOT_WEBHOOK_KEY")
errs = []
if not re.match(r"^https://\S+$", url):
    errs.append("GROKBOT_WEBHOOK_URL is missing or not an https URL")
if ":" in header:
    name, value = header.split(":", 1)
    name, value = name.strip(), value.strip() or key
else:
    name, value = header.strip() or "Authorization", key
if not re.match(r"^[A-Za-z0-9-]+$", name):
    errs.append("GROKBOT_WEBHOOK_HEADER: header name looks wrong")
if not value:
    errs.append("no key: set GROKBOT_WEBHOOK_KEY, or put the whole 'Authorization: ...' line in GROKBOT_WEBHOOK_HEADER")
if errs:
    sys.exit("bridge.env: " + "; ".join(errs))
open(hdr_out, "w").write(f"{name}: {value}\n")
open(url_out, "w").write(url)
if check == "1":
    print(f"config ok (header {name}, key {len(value)} chars)", file=sys.stderr)
    sys.exit(0)
ctx = open(ctx_file, encoding="utf-8", errors="replace").read(200_000) if ctx_file else ""
open(body_out, "w").write(json.dumps({"request_id": req_id, "task": task, "context": ctx,
                                      "reply": {"type": "outbox"}}))
PY
[ "$CHECK" = 1 ] && exit 0
# key goes through a header file so it doesn't show up in `ps`
curl -fsS -X POST "$(cat "$URLF")" -H "Content-Type: application/json" -H "@$HDR" --data-binary "@$BODY" >/dev/null
echo "$REQ_ID"
