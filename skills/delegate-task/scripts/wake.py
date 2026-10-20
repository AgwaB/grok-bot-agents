"""Private, bounded completion transport. No CLI arguments or responses contain secrets."""
import contextlib
import datetime
import ipaddress
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

NAMES = ("GBA_WAKE_URL", "GBA_WAKE_HEADER")
MODES = {"auto", "webhook", "supervisor", "none"}
NOTE = "Read the run with gba runs next using the validated run_id."


def utc_now():
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def presence():
    return {name: bool(os.environ.get(name)) for name in NAMES}


def settings():
    """Values stay in memory; never persist or interpolate them into diagnostics."""
    url, header = (os.environ.get(name, "") for name in NAMES)
    if not url or not header:
        return None, "wake_config_missing"
    try:
        if len(url) > 8192 or len(header) > 8192 or any(ord(c) < 32 or ord(c) == 127 for c in url + header):
            raise ValueError
        parsed = urllib.parse.urlsplit(url)
        local = False
        with contextlib.suppress(ValueError):
            local = ipaddress.ip_address(parsed.hostname or "").is_loopback
        if (not parsed.hostname or parsed.username or parsed.password or parsed.fragment
                or parsed.scheme not in {"http", "https"} or (parsed.scheme == "http" and not local)):
            raise ValueError
        # Force validation of malformed ports without printing the URL.
        _ = parsed.port
        if header.lower().startswith("authorization:"):
            header = header.split(":", 1)[1].strip()
        if not header or ":" in header:
            raise ValueError
        header.encode("ascii")
        return (url, header), None
    except (ValueError, UnicodeError):
        return None, "wake_config_invalid"


def select(requested, timeout, config):
    threshold = config.get("wake_auto_max_sec", 600)
    if type(threshold) is not int or not 1 <= threshold <= 86400:
        raise ValueError("invalid wake threshold")
    if requested not in MODES:
        raise ValueError("invalid wake mode")
    if requested == "auto":
        return "webhook" if all(presence().values()) and timeout <= threshold else "supervisor"
    return requested


def outcome(state, reason, http_status=None, attempts=0):
    return {"outcome": state, "reason": reason, "http_status": http_status, "attempts": attempts}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def send(payload):
    config, error = settings()
    if config is None:
        return outcome("skipped", error or "wake_config_missing")
    url, header = config
    # Never forward credentials on redirects or consult ambient proxy settings.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
    request = urllib.request.Request(url, data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
                                     headers={"Authorization": header, "Content-Type": "application/json"}, method="POST")
    for attempt in range(1, 4):
        try:
            with opener.open(request, timeout=3) as response:
                code = response.status
                # Do not read/log an untrusted (possibly secret-echoing) response body.
            return outcome("sent", "delivered", code, attempt)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            # Only explicit HTTP rejection is retryable. A lost acknowledgement
            # may already have woken the bot, so transport exceptions are NOT retried.
            if code not in {429, 500, 502, 503, 504} or attempt == 3:
                return outcome("failed", "http_rejected", code, attempt)
        except Exception:
            return outcome("failed", "delivery_unknown", attempts=attempt)
        time.sleep(0.25 * 2 ** (attempt - 1))
    return outcome("failed", "delivery_unknown")


def send_bounded(payload):
    # The wall-clock bound includes DNS (urllib's socket timeout alone does not).
    # Only non-sensitive JSON goes over stdin; URL/header are inherited env only.
    try:
        proc = subprocess.run([sys.executable, str(os.path.abspath(__file__))],
                              input=json.dumps(payload), text=True, capture_output=True,
                              timeout=12, check=False)
        value = json.loads(proc.stdout)
        if proc.returncode == 0 and value.get("outcome") in {"sent", "failed", "skipped"}:
            return value
    except Exception:
        return outcome("failed", "delivery_unknown")
    return outcome("failed", "delivery_unknown")


if __name__ == "__main__":
    try:
        value = send(json.loads(sys.stdin.read(8192)))
    except Exception:
        value = outcome("failed", "delivery_unknown")
    print(json.dumps(value, separators=(",", ":")))
