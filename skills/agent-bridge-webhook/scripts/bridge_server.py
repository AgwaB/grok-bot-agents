#!/usr/bin/env python3
"""Return channel for the agent bridge: serves results and progress to the caller (stdlib only).

Binds to 127.0.0.1 only; reach it over Tailscale (tailscaled userspace mode forwards tailnet
connections to localhost). Every endpoint except /healthz needs  Authorization: Bearer <token>.

  bridge_server.py serve [--port 8787] [--host 127.0.0.1]
  bridge_server.py token                 print the token (for copying into the caller's bridge.env only)
  bridge_server.py client-env OUT [--url URL]
                                         write GROKBOT_RESULT_URL/TOKEN lines to OUT (600) for CopyFromBox
Endpoints:
  GET /healthz                 -> "ok" (no auth)
  GET /ping                    -> {"ok": true} (auth check)
  GET /result/<id>?wait=N      -> 200 {request_id,status,worker,text} when ready;
                                  202 {state:"running"} after intake, 404 {state:"unknown"} before it (N<=600 s long-poll)
  GET /events/<id>?timeout=N   -> text/event-stream: "state", "progress" events, then one "result" event
Reads only <root>/{inbox,outbox,progress}/<id>.* ; root = $GROK_BRIDGE_ROOT (default /workspace/agent-bridge).
Token: $GROK_BRIDGE_TOKEN_FILE (default ~/.grok-bot-agents/bridge-token, 600, generated if missing).
"""
import hmac, ipaddress, json, os, re, secrets, stat, sys, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit, parse_qs

ROOT = os.environ.get("GROK_BRIDGE_ROOT", "/workspace/agent-bridge")
TOKEN_FILE = os.environ.get("GROK_BRIDGE_TOKEN_FILE",
                            os.path.expanduser("~/.grok-bot-agents/bridge-token"))
ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,80}$")  # same as bridge.py
ALLOWED_NETS = [ipaddress.ip_network(n) for n in ("127.0.0.0/8", "::1/128", "100.64.0.0/10",
                                                   "fd7a:115c:a1e0::/48")]
MAX_RESULT = 2_000_000


def load_token():
    os.makedirs(os.path.dirname(TOKEN_FILE), mode=0o700, exist_ok=True)
    if not os.path.exists(TOKEN_FILE):
        fd = os.open(TOKEN_FILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32) + "\n")
    st = os.lstat(TOKEN_FILE)
    if not stat.S_ISREG(st.st_mode):
        sys.exit(f"{TOKEN_FILE} is not a regular file")
    if st.st_mode & 0o077:
        os.chmod(TOKEN_FILE, 0o600)
    tok = open(TOKEN_FILE).read().strip()
    if len(tok) < 20:
        sys.exit(f"{TOKEN_FILE}: token too short")
    return tok


def valid_id(rid):
    return bool(ID_RE.match(rid)) and not rid.startswith(".")


def read_file(*parts, limit=MAX_RESULT):
    """Read <ROOT>/<parts> without following symlinks; None if missing."""
    path = os.path.join(ROOT, *parts)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except (FileNotFoundError, OSError):
        return None
    with os.fdopen(fd, "rb") as f:
        if not stat.S_ISREG(os.fstat(f.fileno()).st_mode):
            return None
        return f.read(limit)


def exists(*parts):
    return os.path.isfile(os.path.join(ROOT, *parts)) and not os.path.islink(os.path.join(ROOT, *parts))


def result(rid):
    """Final result dict, or None while pending. Status file is written after the .md, so wait for it
    (or fall back to 'ok' if the .md is older than 5 s, e.g. replies written before status files existed)."""
    md = read_file("outbox", rid + ".md")
    if md is None:
        return None
    meta_raw = read_file("outbox", rid + ".status.json", limit=10_000)
    if meta_raw is None:
        try:
            if time.time() - os.path.getmtime(os.path.join(ROOT, "outbox", rid + ".md")) < 5:
                return None
        except OSError:
            return None
        meta = {"status": "ok", "worker": "unknown"}
    else:
        try:
            meta = json.loads(meta_raw)
        except ValueError:
            meta = {"status": "unknown", "worker": "unknown"}
    return {"request_id": rid, "status": str(meta.get("status", "ok")),
            "worker": str(meta.get("worker", "unknown")), "text": md.decode("utf-8", "replace")}


def state(rid):
    if exists("outbox", rid + ".md"):
        return "done"
    return "running" if exists("inbox", rid + ".json") else "unknown"


def num(q, key, default, hi):
    try:
        return max(0, min(hi, int(q.get(key, [default])[0])))
    except ValueError:
        return default


class H(BaseHTTPRequestHandler):
    server_version = "grok-bridge/0.1"
    sys_version = ""

    def log_message(self, fmt, *args):
        if self.path == "/healthz":
            return
        sys.stderr.write("%s %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), self.client_address[0],
                                         fmt % args))

    def send(self, code, obj=None, text=None):
        body = (text if text is not None else json.dumps(obj, ensure_ascii=False)).encode()
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8" if text is not None
                         else "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def authed(self):
        h = self.headers.get("Authorization", "")
        given = h[7:].strip() if h.lower().startswith("bearer ") else ""
        return hmac.compare_digest(given.encode(), self.server.token.encode())

    def do_GET(self):
        try:
            ip = ipaddress.ip_address(self.client_address[0].split("%")[0])
        except ValueError:
            return self.send(403, {"error": "forbidden"})
        if not any(ip in n for n in ALLOWED_NETS):
            return self.send(403, {"error": "forbidden"})
        u = urlsplit(self.path)
        q = parse_qs(u.query)
        if u.path == "/healthz":
            return self.send(200, text="ok\n")
        if not self.authed():
            return self.send(401, {"error": "missing or wrong token"})
        if u.path == "/ping":
            return self.send(200, {"ok": True})
        m = re.match(r"^/(result|events)/([^/]+)$", u.path)
        if not m or not valid_id(m.group(2)):
            return self.send(404 if not m else 400, {"error": "not found" if not m else "invalid request_id"})
        kind, rid = m.groups()
        if kind == "result":
            return self.get_result(rid, num(q, "wait", 0, 600))
        return self.events(rid, num(q, "timeout", 1800, 3600), num(q, "from", 0, 10**9))

    def get_result(self, rid, wait):
        end = time.time() + wait
        while True:
            r = result(rid)
            if r:
                return self.send(200, r)
            if time.time() >= end:
                st = state(rid)
                return self.send(202 if st != "unknown" else 404, {"request_id": rid, "state": st})
            time.sleep(1)

    def events(self, rid, timeout, offset):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()
        self.close_connection = True

        def emit(event, data, eid=None):
            msg = (f"id: {eid}\n" if eid is not None else "") + f"event: {event}\n"
            msg += "".join(f"data: {line}\n" for line in data.split("\n")) + "\n"
            self.wfile.write(msg.encode())
            self.wfile.flush()

        def drain(off):
            raw = read_file("progress", rid + ".log", limit=1_000_000) or b""
            for line in raw[off:].split(b"\n")[:-1]:  # complete lines only
                off += len(line) + 1
                text = line.decode("utf-8", "replace").replace("\r", "").strip()
                if text:
                    emit("progress", text, eid=off)
            return off

        end, last_state, last_ping = time.time() + timeout, None, time.time()
        try:
            while True:
                offset = drain(offset)
                st = state(rid)
                if st != last_state:
                    emit("state", st)
                    last_state = st
                r = result(rid)
                if r:
                    # flush any progress written together with the reply
                    offset = drain(offset)
                    emit("result", json.dumps(r, ensure_ascii=False))
                    return
                if time.time() >= end:
                    emit("timeout", json.dumps({"request_id": rid, "state": st}))
                    return
                if time.time() - last_ping > 15:
                    self.wfile.write(b": keepalive\n\n")
                    self.wfile.flush()
                    last_ping = time.time()
                time.sleep(1)
        except (BrokenPipeError, ConnectionResetError):
            return


def main(a):
    if a[:1] == ["token"]:
        print(load_token())
        return
    if a[:1] == ["client-env"] and len(a) >= 2:
        url = a[a.index("--url") + 1] if "--url" in a else "http://100.98.172.38:8787"
        fd = os.open(a[1], os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(f"GROKBOT_RESULT_URL={url}\nGROKBOT_RESULT_TOKEN={load_token()}\n")
        os.chmod(a[1], 0o600)
        print(f"wrote {a[1]} (600); delete it after copying")
        return
    if a[:1] == ["serve"]:
        port = int(a[a.index("--port") + 1]) if "--port" in a else int(os.environ.get("GROK_BRIDGE_PORT", 8787))
        host = a[a.index("--host") + 1] if "--host" in a else "127.0.0.1"
        srv = ThreadingHTTPServer((host, port), H)
        srv.daemon_threads = True
        srv.token = load_token()
        sys.stderr.write(f"bridge_server on {host}:{port}, root {ROOT}, token file {TOKEN_FILE}\n")
        srv.serve_forever()
        return
    sys.exit(__doc__)


if __name__ == "__main__":
    main(sys.argv[1:])
