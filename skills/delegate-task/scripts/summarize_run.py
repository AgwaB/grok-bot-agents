#!/usr/bin/env python3
"""Turn a run dir into result.md + meta.json and print a short JSON summary (never the full log)."""
import json
import os
import re
import subprocess
import sys

agent, run_dir, rc, secs, workdir = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
res_path = os.path.join(run_dir, "result.md")


def read_log(name):
    path = os.path.join(run_dir, name)
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:
        return ""


def write_artifact(path, text, atomic=False):
    try:
        with open(path + ".tmp" if atomic else path, "w", encoding="utf-8") as f:
            f.write(text)
        if atomic:
            os.replace(path + ".tmp", path)
    except OSError:
        print(json.dumps({"status": "error", "error": "could not write run artifact", "run_dir": run_dir}))
        sys.exit(1)


def events(text):
    """Accept legacy Claude JSON and JSONL. Only LF separates records, not Unicode separators."""
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            yield obj
            return
    except ValueError:
        pass
    for line in text.split("\n"):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            yield obj


def text_value(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(filter(None, (text_value(v) for v in value)))
    if isinstance(value, dict):
        return text_value(value.get("text") or value.get("message"))
    return ""


raw, stderr = read_log("stdout.log"), read_log("stderr.log")
result, session, last_stop = "", None, None
terminal, failure, stream_error = False, "", ""
for ev in events(raw):
    typ = ev.get("type")
    if typ in ("session", "thread.started"):
        session = ev.get("id") or ev.get("thread_id") or session
    if agent == "claude":
        session = ev.get("session_id") or session
        # A result is authoritative; assistant/tool messages and deltas are not a verdict.
        if typ == "result" or ("is_error" in ev and "result" in ev):
            terminal = True
            result = text_value(ev.get("result"))
            failed = ev.get("is_error") is not False or ev.get("subtype") != "success"
            failure = (text_value(ev.get("errors")) or result or "Claude result reported an error") if failed else ""
    elif agent == "codex":
        if typ == "turn.started":
            terminal, failure, stream_error = False, "", ""
        elif typ == "item.completed":
            item = ev.get("item")
            if isinstance(item, dict) and item.get("type") == "agent_message":
                result = text_value(item.get("text"))
        elif typ == "error":
            # Reconnect notices can be followed by a successful turn. Not terminal by themselves.
            stream_error = text_value(ev.get("message"))
        elif typ == "turn.failed":
            terminal = True
            failure = text_value(ev.get("error")) or "Codex turn failed"
        elif typ == "turn.completed":
            terminal, failure, stream_error = True, "", ""
    elif agent in ("pi", "omp"):
        msg = ev.get("message")
        if typ == "auto_retry_start" or (typ == "message_start" and isinstance(msg, dict) and msg.get("role") == "assistant"):
            terminal, failure = False, ""
        elif typ == "message_end" and isinstance(msg, dict) and msg.get("role") == "assistant":
            last_stop = msg.get("stopReason")
            # Replace even an empty answer: an earlier failed attempt must not leak into success.
            content = msg.get("content")
            result = "\n".join(c["text"] for c in content if isinstance(c, dict) and c.get("type") == "text" and isinstance(c.get("text"), str)) if isinstance(content, list) else ""
            failure = str(msg.get("errorMessage") or last_stop or "unknown error") if last_stop in ("error", "aborted") or msg.get("errorMessage") else ""
            if last_stop == "length" and not failure:
                failure = "Response truncated: model output token limit (length)"
            terminal = bool(failure) or last_stop == "stop"
        elif typ == "auto_retry_end" and ev.get("success") is False:
            terminal = True
            failure = text_value(ev.get("finalError")) or failure or "Automatic retries exhausted"

# Process exit and protocol verdict are independent. Preserve the actual process exit_code in meta.
incomplete = rc == 0 and not terminal
status = "timeout" if rc in (124, 137) else ("error" if rc or failure or incomplete else "ok")
if failure:
    partial = result
    result = "error: " + failure[:4000]
    if partial and partial != failure:
        result += "\n\nPartial output before the error:\n" + partial
elif incomplete:
    result = "error: missing terminal completion event" + ("\n\nPartial output:\n" + result if result else "")
elif agent == "codex" and status == "ok":
    result = read_log("result.md").strip() or result
elif status != "ok" and not result:
    result = stream_error or stderr[-4000:] or f"Agent exited with code {rc} without a result"

review_modified = bool(os.environ.get("REVIEW_MODIFIED"))
if review_modified:
    status = "error"
    result = "REVIEW MODE VIOLATION: the reviewed directory changed during a read-only review. Inspect it with git status.\n" + result

# Classify only the final structured error, or diagnostics when there is no such error.
# Historical reconnect stderr and partial prose about quota must not override a final auth/other error.
errtxt = failure or stream_error or stderr[-4000:]
LIMIT_RE = r"hit your (weekly|daily|usage|session) limit|usage limit|rate.?limit|quota|\b429\b|too many requests|insufficient_quota|credit balance is too low"
AUTH_RE = r"\b(401|403)\b|unauthenticated|unauthori[sz]ed|forbidden|invalid[ _-](api[ _-])?key|authentication|authorization|permission denied"
CONN_RE = (r"websocket|idle timeout|\bECONN(RESET|REFUSED|ABORTED)\b|\bETIMEDOUT\b|\bEPIPE\b|\bEAI_AGAIN\b|\bENOTFOUND\b|\bENETUNREACH\b"
           r"|\bEHOSTUNREACH\b|socket hang ?up|fetch failed|network (error|connection|is unreachable)|connection (?:was )?(error|reset|closed|lost|refused|aborted|dropped)"
           r"|closed unexpectedly|\btimed out\b|error (decoding response body|sending request)"
           r"|stream (disconnected|closed|ended|error)|premature close|other side closed|^terminated$|\bUND_ERR|transport failure|provider_transport_failure")
error_kind, resumable = "incomplete_output" if incomplete else None, False
# Cancellation and explicit auth rejection are not retriable connection drops, even when
# their diagnostics mention the transport (e.g. WebSocket 401) or contain old retry noise.
cancelled = rc in (130, 143) or last_stop == "aborted"
if status == "error" and not review_modified and not cancelled and not re.search(AUTH_RE, errtxt, re.I):
    if re.search(LIMIT_RE, errtxt, re.I):
        status, error_kind = "rate_limited", None
        try:
            sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
            import route
            msg = next((line for line in errtxt.splitlines() if re.search(LIMIT_RE, line, re.I)), errtxt[-300:])
            route.mark_limited(agent, message=msg.strip()[:300])
        except Exception:
            print("warning: could not record rate-limit state", file=sys.stderr)
    elif re.search(CONN_RE, errtxt, re.I):
        error_kind, resumable = "conn_error", True

write_artifact(res_path, result)

artifacts = []
if os.environ.get("KIND") == "work":
    for root, dirs, files in os.walk(workdir):
        dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__")]
        for fn in sorted(files):
            artifacts.append(os.path.join(root, fn))
            if len(artifacts) >= 100:
                break
        if len(artifacts) >= 100:
            break

changed = []
try:
    if os.environ.get("KIND") == "repo":
        changed = subprocess.run(["git", "-C", workdir, "status", "--porcelain"], capture_output=True, text=True, timeout=20).stdout.splitlines()[:50]
except (OSError, subprocess.SubprocessError):
    print("warning: could not inspect worktree changes", file=sys.stderr)

meta = {"agent": agent, "status": status, "error_kind": error_kind, "resumable": resumable,
        "last_stop_reason": last_stop, "pi_retry": os.environ.get("PI_RETRY") or None,
        "permission": os.environ.get("PERM"), "model": os.environ.get("MODEL") or None,
        "reasoning": os.environ.get("REASONING") or None, "worktree_branch": os.environ.get("WORKTREE"), "exit_code": rc, "seconds": secs, "run_dir": run_dir,
        "kind": os.environ.get("KIND"), "workdir": workdir, "session_id": session,
        "changed_files": changed, "artifacts": artifacts,
        "review_unchanged": not review_modified if os.environ.get("KIND") == "review" else None, "result_file": res_path}
# A supervisor must never observe a half-written terminal record.
meta_path = os.path.join(run_dir, "meta.json")
write_artifact(meta_path, json.dumps(meta, indent=2), atomic=True)

summary = result.strip()
if len(summary) > 1500:
    summary = summary[:1500] + " …(truncated, see result.md)"
meta["summary"] = summary
if status != "ok":
    meta["stderr_tail"] = stderr[-800:]
print(json.dumps(meta, ensure_ascii=False, indent=2))
sys.exit(0 if status == "ok" else 1)
