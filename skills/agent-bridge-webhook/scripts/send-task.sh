#!/usr/bin/env bash
# Sender for the LOCAL machine or CI: posts one task to the Grok Bot "Agent bridge" webhook routine and,
# with --wait, gets the answer back in this terminal over the Tailscale return channel.
# Config (never commit it): ~/.config/grok-bot-agents/bridge.env (legacy ~/.config/grok-agents/bridge.env still read),
# mode 600, or the same variables in env. The file is parsed, not sourced; quotes and spaces are fine.
#   GROKBOT_WEBHOOK_URL=...      Webhook URL (routine panel)
#   GROKBOT_WEBHOOK_HEADER=...   the whole "Authorization: Bearer ..." line, or just the header name
#   GROKBOT_WEBHOOK_KEY=...      header value (only needed when HEADER is just the name)
#   GROKBOT_RESULT_URL=...       return channel, e.g. http://100.98.172.38:8787 (for --wait)
#   GROKBOT_RESULT_TOKEN=...     return-channel token (box: ~/.grok-bot-agents/bridge-token)
# Usage:
#   grok-send-task "task" [context-file]                  post; prints the request_id
#   grok-send-task --wait [--timeout S] [--no-stream] "task" [context-file]
#                                                         post, stream progress to stderr, print the answer to stdout
#   grok-send-task --fetch REQUEST_ID [--timeout S] [--no-stream]   wait for / fetch an earlier request
#   grok-send-task --check                                validate config; tests the return channel if set
#   grok-send-task --setup                                ask for the values (secrets hidden), write the config (600), check
#   grok-send-task --import-result-env FILE               merge GROKBOT_RESULT_* from FILE into the config, then delete FILE
# Exit codes with --wait/--fetch: 0 ok, 1 bot reported a non-ok status, 2 usage/config, 4 timeout, 5 channel/auth error.
set -euo pipefail
CONF="${GROK_BRIDGE_CONF:-$HOME/.config/grok-bot-agents/bridge.env}"
if [ -z "${GROK_BRIDGE_CONF:-}" ] && [ ! -e "$CONF" ] && [ -e "$HOME/.config/grok-agents/bridge.env" ]; then
  CONF="$HOME/.config/grok-agents/bridge.env"  # legacy path from before the rename
fi
SELF="$0"

# merge KEY=VALUE pairs (taken from env vars named in $MERGE_KEYS) into $CONF: backup, atomic, 600
merge_conf() {
  python3 - "$CONF" <<'PY'
import os, sys, tempfile, time, shutil
conf = sys.argv[1]
keys = os.environ["MERGE_KEYS"].split()
new = {k: os.environ.get("MERGE_" + k, "") for k in keys}
new = {k: v for k, v in new.items() if v}
lines = open(conf, encoding="utf-8").read().splitlines() if os.path.isfile(conf) else ["# grok-send-task config"]
out, seen = [], set()
for line in lines:
    k = line.split("=", 1)[0].strip().removeprefix("export ").strip() if "=" in line and not line.lstrip().startswith("#") else None
    if k in new:
        if k not in seen:
            out.append(f"{k}={new[k]}"); seen.add(k)
        continue
    out.append(line)
out += [f"{k}={v}" for k, v in new.items() if k not in seen]
d = os.path.dirname(conf); os.makedirs(d, mode=0o700, exist_ok=True)
if os.path.isfile(conf):
    shutil.copy2(conf, conf + ".bak." + time.strftime("%Y%m%d-%H%M%S"))
fd, tmp = tempfile.mkstemp(dir=d, prefix=".bridge.env.")
with os.fdopen(fd, "w") as f:
    f.write("\n".join(out) + "\n")
os.chmod(tmp, 0o600); os.replace(tmp, conf)
print(f"saved {conf} (600): " + ", ".join(sorted(new)), file=sys.stderr)
PY
}

WAIT=0; STREAM=1; TIMEOUT=1800; FETCH=""; CHECK=0; TASK=""; CTX_FILE=""; POS=0
while [ $# -gt 0 ]; do
  case "$1" in
    --setup)
      echo "Paste the values from the Agent bridge routine panel."
      read -rp "Webhook URL: " s_url
      read -rsp "Authorization header (whole line or just the key; input hidden): " s_hdr; echo
      [ -n "$s_url" ] && [ -n "$s_hdr" ] || { echo "both values are required" >&2; exit 2; }
      case "$s_hdr" in *:*) ;; *) s_hdr="Authorization: $s_hdr" ;; esac
      read -rp "Return channel URL for --wait (Enter to skip/keep): " s_rurl
      s_rtok=""; [ -n "$s_rurl" ] && { read -rsp "Return channel token (input hidden): " s_rtok; echo; }
      MERGE_KEYS="GROKBOT_WEBHOOK_URL GROKBOT_WEBHOOK_HEADER GROKBOT_RESULT_URL GROKBOT_RESULT_TOKEN" \
        MERGE_GROKBOT_WEBHOOK_URL="$s_url" MERGE_GROKBOT_WEBHOOK_HEADER="$s_hdr" \
        MERGE_GROKBOT_RESULT_URL="$s_rurl" MERGE_GROKBOT_RESULT_TOKEN="$s_rtok" merge_conf
      unset s_hdr s_rtok
      exec "$SELF" --check ;;
    --import-result-env)
      src="${2:-}"; [ -f "$src" ] || { echo "usage: --import-result-env FILE" >&2; exit 2; }
      r_url=$(sed -n 's/^GROKBOT_RESULT_URL=//p' "$src" | head -n1)
      r_tok=$(sed -n 's/^GROKBOT_RESULT_TOKEN=//p' "$src" | head -n1)
      [ -n "$r_url" ] && [ -n "$r_tok" ] || { echo "$src: needs GROKBOT_RESULT_URL and GROKBOT_RESULT_TOKEN" >&2; exit 2; }
      MERGE_KEYS="GROKBOT_RESULT_URL GROKBOT_RESULT_TOKEN" MERGE_GROKBOT_RESULT_URL="$r_url" \
        MERGE_GROKBOT_RESULT_TOKEN="$r_tok" merge_conf
      unset r_tok; rm -f "$src"; echo "deleted $src" >&2
      exec "$SELF" --check ;;
    --check) CHECK=1 ;;
    --wait) WAIT=1 ;;
    --no-stream) STREAM=0 ;;
    --stream) STREAM=1 ;;
    --timeout) TIMEOUT="${2:-}"; shift ;;
    --fetch) FETCH="${2:-}"; WAIT=1; shift ;;
    --help|-h) sed -n '2,21p' "$SELF"; exit 0 ;;
    --) shift; [ $# -gt 0 ] && { TASK="$1"; shift; }; [ $# -gt 0 ] && CTX_FILE="$1"; break ;;
    -*) echo "unknown option $1 (see --help)" >&2; exit 2 ;;
    *) if [ $POS = 0 ]; then TASK="$1"; else CTX_FILE="$1"; fi; POS=$((POS + 1)) ;;
  esac
  shift
done
if [ -n "$FETCH" ] && ! printf '%s' "$FETCH" | grep -Eq '^[A-Za-z0-9_-][A-Za-z0-9._-]{0,79}$'; then
  echo "invalid request id: $FETCH" >&2; exit 2
fi
case "$TIMEOUT" in ''|*[!0-9]*) echo "--timeout needs seconds" >&2; exit 2 ;; esac
[ "$CHECK" = 1 ] || [ -n "$TASK" ] || [ -n "$FETCH" ] || { echo 'usage: grok-send-task [--wait] "task" [context-file] | --fetch ID | --check | --setup  (--help)' >&2; exit 2; }

# --check: is Tailscale installed and up on this machine, and does the box answer over the tailnet? Hints only, never fatal.
ts_check() {
  local ts="" url host os
  os=$(uname -s)
  if command -v tailscale >/dev/null 2>&1; then ts=tailscale
  elif [ -x /Applications/Tailscale.app/Contents/MacOS/Tailscale ]; then ts=/Applications/Tailscale.app/Contents/MacOS/Tailscale
  fi
  url="${GROKBOT_RESULT_URL:-}"
  [ -z "$url" ] && [ -f "$CONF" ] && url=$(sed -n 's/^[[:space:]]*\(export[[:space:]]*\)\{0,1\}GROKBOT_RESULT_URL[[:space:]]*=[[:space:]]*//p' "$CONF" | head -n1 | tr -d "\"'")
  if [ -z "$ts" ]; then
    if [ "$os" = Darwin ]; then
      echo "tailscale: not installed. For --wait answers: brew install --cask tailscale (or Tailscale from the Mac App Store), then sign in with the same account as the box" >&2
    else
      echo "tailscale: not installed. For --wait answers: see https://tailscale.com/download/linux, then sign in with the same account as the box" >&2
    fi
    return 0
  fi
  if ! "$ts" status >/dev/null 2>&1; then
    if [ "$os" = Darwin ]; then
      echo "tailscale: installed but not connected. Open the Tailscale app and sign in / turn it on (same account as the box)" >&2
    else
      echo "tailscale: installed but not connected. Run: sudo tailscale up (same account as the box)" >&2
    fi
    return 0
  fi
  [ -n "$url" ] || { echo "tailscale: connected" >&2; return 0; }
  url=${url%/}
  # source of truth: the return channel's own /healthz (no auth, no proxy)
  if command -v curl >/dev/null 2>&1; then
    if curl -fsS --noproxy '*' -m 8 "$url/healthz" >/dev/null 2>&1; then
      echo "tailscale: connected, box reachable ($url)" >&2
    else
      echo "tailscale: connected, but $url/healthz doesn't answer. Box on this tailnet and its server running? (ask the bot to run setup-tailscale.sh --status)" >&2
    fi
    return 0
  fi
  # no curl: Tailscale-layer ping as a rough check (the app-bundled CLI can be slow)
  host=${url#*://}; host=${host%%/*}; host=${host%:*}
  if "$ts" ping --c 3 --timeout 10s "$host" >/dev/null 2>&1; then
    echo "tailscale: connected, box $host answers tailscale ping" >&2
  else
    echo "tailscale: connected; tailscale ping to $host got no answer (the return-channel check below is authoritative)" >&2
  fi
}
[ "$CHECK" = 1 ] && ts_check

if [ -f "$CONF" ]; then
  perm=$(stat -c %a "$CONF" 2>/dev/null || stat -f %Lp "$CONF")
  [ "$perm" = 600 ] || { echo "refusing $CONF: chmod 600 it first" >&2; exit 1; }
fi
BODY=$(mktemp); HDR=$(mktemp); URLF=$(mktemp); RES=$(mktemp); trap 'rm -f "$BODY" "$HDR" "$URLF" "$RES"' EXIT
REQ_ID="${FETCH:-req-$(date +%Y%m%d-%H%M%S)-$RANDOM}"
NEED_WEBHOOK=1; [ -n "$FETCH" ] && NEED_WEBHOOK=0
python3 - "$CONF" "$HDR" "$URLF" "$BODY" "$REQ_ID" "$TASK" "$CTX_FILE" "$CHECK" "$WAIT" "$RES" "$NEED_WEBHOOK" <<'PY'
import json, os, re, sys, urllib.request, urllib.error
conf, hdr_out, url_out, body_out, req_id, task, ctx_file, check, wait, res_out, need_wh = sys.argv[1:]
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
errs = []
if need_wh == "1":
    url, header, key = get("GROKBOT_WEBHOOK_URL"), get("GROKBOT_WEBHOOK_HEADER"), get("GROKBOT_WEBHOOK_KEY")
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
rurl, rtok = get("GROKBOT_RESULT_URL").rstrip("/"), get("GROKBOT_RESULT_TOKEN").strip()
if rtok.lower().startswith("bearer "):
    rtok = rtok[7:].strip()
if wait == "1" or rurl or rtok:
    if not re.match(r"^https?://[^\s/]+$", rurl):
        errs.append("GROKBOT_RESULT_URL is missing or not like http://100.x.y.z:8787")
    if len(rtok) < 20:
        errs.append("GROKBOT_RESULT_TOKEN is missing or too short")
if errs:
    sys.exit("bridge.env: " + "; ".join(errs))
if rurl:
    open(res_out, "w").write(f"{rurl}\n{rtok}\n")
if need_wh == "1":
    open(hdr_out, "w").write(f"{name}: {value}\n")
    open(url_out, "w").write(url)
if check == "1":
    if need_wh == "1":
        print(f"config ok (header {name}, key {len(value)} chars)", file=sys.stderr)
    if rurl:
        op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        try:
            op.open(rurl + "/healthz", timeout=8).read()
        except Exception as e:
            sys.exit(f"return channel unreachable at {rurl}: {e} (Tailscale up? box server running?)")
        try:
            op.open(urllib.request.Request(rurl + "/ping", headers={"Authorization": "Bearer " + rtok}), timeout=8).read()
        except urllib.error.HTTPError as e:
            sys.exit(f"return channel rejected the token (HTTP {e.code})")
        print(f"return channel ok ({rurl})", file=sys.stderr)
    else:
        print("return channel not configured (--wait unavailable)", file=sys.stderr)
    sys.exit(0)
if need_wh == "1":
    ctx = open(ctx_file, encoding="utf-8", errors="replace").read(200_000) if ctx_file else ""
    open(body_out, "w").write(json.dumps({"request_id": req_id, "task": task, "context": ctx,
                                          "reply": {"type": "outbox"}}))
PY
[ "$CHECK" = 1 ] && exit 0
if [ "$NEED_WEBHOOK" = 1 ]; then
  # key goes through a header file so it doesn't show up in `ps`
  curl -fsS -X POST "$(cat "$URLF")" -H "Content-Type: application/json" -H "@$HDR" --data-binary "@$BODY" >/dev/null
  if [ "$WAIT" = 0 ]; then echo "$REQ_ID"; exit 0; fi
fi

# --wait / --fetch: progress -> stderr, answer -> stdout. Token is read from a 600 temp file, never argv.
rm -f "$BODY" "$HDR" "$URLF"; trap - EXIT   # exec skips the trap; python deletes $RES after reading it
exec python3 - "$RES" "$REQ_ID" "$TIMEOUT" "$STREAM" <<'PY'
import json, os, socket, sys, time, urllib.request, urllib.error
res, rid, timeout, stream = sys.argv[1], sys.argv[2], int(sys.argv[3]), sys.argv[4] == "1"
rurl, tok = open(res).read().split("\n")[:2]
os.unlink(res)
op = urllib.request.build_opener(urllib.request.ProxyHandler({}))
H = {"Authorization": "Bearer " + tok}
deadline = time.time() + timeout
def err(msg):
    print(f"[grok] {msg}", file=sys.stderr, flush=True)
def finish(r):
    text = r.get("text", "")
    sys.stdout.write(text if text.endswith("\n") else text + "\n")
    sys.stdout.flush()
    st = r.get("status", "ok")
    err(f"{rid}: {st} (worker {r.get('worker', '?')})")
    sys.exit(0 if st == "ok" else 1)
def http_fatal(e):
    if e.code in (401, 403):
        err(f"return channel rejected the token (HTTP {e.code})"); sys.exit(5)
    if e.code == 400:
        err("invalid request id"); sys.exit(2)
err(f"waiting for {rid} (up to {timeout}s; Ctrl-C stops waiting, not the task)")
said_waiting, offset, fails = False, 0, 0
try:
    while stream and time.time() < deadline and fails < 3:
        left = int(deadline - time.time()) + 1
        try:
            resp = op.open(urllib.request.Request(f"{rurl}/events/{rid}?timeout={left}&from={offset}", headers=H), timeout=60)
            fails = 0
            ev, data = None, []
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith(":"):
                    continue
                if line.startswith("event:"):
                    ev = line[6:].strip()
                elif line.startswith("data:"):
                    data.append(line[5:].lstrip(" ") if line[5:6] == " " else line[5:])
                elif line.startswith("id:"):
                    try: offset = int(line[3:].strip())
                    except ValueError: pass
                elif line == "" and ev:
                    payload = "\n".join(data)
                    if ev == "progress":
                        err(payload)
                    elif ev == "state" and payload == "unknown" and not said_waiting:
                        err("waiting for the bot to pick up the request..."); said_waiting = True
                    elif ev == "result":
                        finish(json.loads(payload))
                    elif ev == "timeout":
                        err(f"timed out; fetch later with: grok-send-task --fetch {rid}"); sys.exit(4)
                    ev, data = None, []
            time.sleep(1)  # stream closed without a result: reconnect from the last offset
        except urllib.error.HTTPError as e:
            http_fatal(e); fails += 1; time.sleep(3)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            fails += 1
            if fails == 1:
                err(f"stream interrupted ({getattr(e, 'reason', e)}); retrying")
            time.sleep(3)
    if stream and fails >= 3:
        err("streaming unavailable; falling back to polling")
    while time.time() < deadline:
        w = max(1, min(60, int(deadline - time.time())))
        try:
            r = op.open(urllib.request.Request(f"{rurl}/result/{rid}?wait={w}", headers=H), timeout=w + 30)
            if r.status == 200:
                finish(json.loads(r.read()))
        except urllib.error.HTTPError as e:
            http_fatal(e)
            if e.code != 404:
                time.sleep(3)
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as e:
            err(f"return channel unreachable ({getattr(e, 'reason', e)}); retrying"); time.sleep(5)
    err(f"timed out; fetch later with: grok-send-task --fetch {rid}"); sys.exit(4)
except KeyboardInterrupt:
    err(f"stopped waiting; the bot keeps working. fetch later with: grok-send-task --fetch {rid}"); sys.exit(130)
PY
