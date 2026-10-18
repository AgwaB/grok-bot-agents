#!/usr/bin/env python3
"""Turn a run dir into result.md + meta.json and print a short JSON summary (never the full log)."""
import json, os, re, subprocess, sys

agent, run_dir, rc, secs, workdir = sys.argv[1], sys.argv[2], int(sys.argv[3]), int(sys.argv[4]), sys.argv[5]
out_path = os.path.join(run_dir, "stdout.log")
res_path = os.path.join(run_dir, "result.md")
raw = open(out_path, encoding="utf-8", errors="replace").read() if os.path.exists(out_path) else ""

SESSION = [None]

def last_text_from_jsonl(text):
    """Best effort: walk JSON lines and keep the last assistant-looking text. Event shapes differ per CLI."""
    last = None
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict) and ev.get("type") in ("session", "thread.started") and not SESSION[0]:
            SESSION[0] = ev.get("id") or ev.get("thread_id")
        stack = [ev]
        while stack:
            o = stack.pop()
            if isinstance(o, dict):
                if o.get("role") == "assistant" or o.get("type") in ("agent_message", "assistant", "message", "result"):
                    for k in ("result", "text", "message", "content"):
                        v = o.get(k)
                        if isinstance(v, str) and v.strip():
                            last = v
                        elif isinstance(v, list):
                            parts = [p.get("text") for p in v if isinstance(p, dict) and isinstance(p.get("text"), str)]
                            if parts:
                                last = "\n".join(parts)
                stack.extend(o.values())
            elif isinstance(o, list):
                stack.extend(o)
    return last

def pi_like_final(text):
    """pi/omp --mode json: take text parts of the last message_end with role assistant (pi docs/json.md)."""
    last = None
    for line in text.splitlines():
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict) and ev.get("type") == "message_end":
            m = ev.get("message") or {}
            if m.get("role") == "assistant":
                parts = [c.get("text") for c in m.get("content", []) if isinstance(c, dict) and c.get("type") == "text" and c.get("text")]
                if parts:
                    last = "\n".join(parts)
                elif m.get("stopReason") == "error" or m.get("errorMessage"):
                    # failed turn (quota, auth, bad model): surface the provider error instead of an empty summary
                    last = "error: " + str(m.get("errorMessage") or "unknown error").strip()[:1200]
                    who = "/".join(x for x in (m.get("provider"), m.get("model")) if x)
                    if who: last += f" (model {who})"
    return last

result, session = None, None
if agent == "codex" and os.path.exists(res_path):
    result = open(res_path, encoding="utf-8", errors="replace").read()
elif agent == "claude":
    try:
        j = json.loads(raw)
        result = j.get("result")
        session = j.get("session_id")
        if j.get("is_error"):
            rc = rc or 1
    except Exception:
        pass
if result is None and agent in ("pi", "omp"):
    result = pi_like_final(raw)
if result is None:
    result = last_text_from_jsonl(raw)
if not session:
    for line in raw.splitlines()[:50]:
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if isinstance(ev, dict) and ev.get("type") in ("session", "thread.started"):
            session = ev.get("id") or ev.get("thread_id")
            break
session = session or SESSION[0]
if result is None and rc == 0:
    result = raw[-4000:]  # fallback: tail of stdout

with open(res_path, "w", encoding="utf-8") as f:
    f.write(result or "")

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
except Exception:
    pass

status = "ok" if rc == 0 else ("timeout" if rc in (124, 137) else "error")
if status == "ok" and agent in ("pi", "omp") and (result or "").startswith("error: "):
    status = "error"
if os.environ.get("REVIEW_MODIFIED"):
    status = "error"
    result = "REVIEW MODE VIOLATION: the reviewed directory changed during a read-only review. Inspect it with git status.\n" + (result or "")
# Quota or rate limits (e.g. Claude "You've hit your weekly limit", HTTP 429): tell the bot to fall back.
_errtxt = (result or "") + (open(os.path.join(run_dir, "stderr.log"), encoding="utf-8", errors="replace").read()[-4000:] if os.path.exists(os.path.join(run_dir, "stderr.log")) else "")
if status == "error" and re.search(r"(hit your (weekly|daily|usage|session) limit|usage limit|rate.?limit|quota|\b429\b|too many requests|insufficient_quota|credit balance is too low)", _errtxt, re.I):
    status = "rate_limited"
    try:  # remember it so route.py skips this agent until the reset time
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import route
        _pat = r"(hit your (weekly|daily|usage|session) limit|usage limit|rate.?limit|quota|\b429\b|too many requests|insufficient_quota|credit balance is too low)"
        _msg = next((l for l in ((result or "") + "\n" + _errtxt).splitlines() if re.search(_pat, l, re.I)), _errtxt[-300:])
        route.mark_limited(agent, message=_msg.strip()[:300])
    except Exception:
        pass
meta = {"agent": agent, "status": status, "permission": os.environ.get("PERM"), "model": os.environ.get("MODEL") or None,
        "reasoning": os.environ.get("REASONING") or None, "worktree_branch": os.environ.get("WORKTREE"), "exit_code": rc, "seconds": secs, "run_dir": run_dir,
        "kind": os.environ.get("KIND"), "workdir": workdir, "session_id": session,
        "changed_files": changed, "artifacts": artifacts,
        "review_unchanged": (not os.environ.get("REVIEW_MODIFIED")) if os.environ.get("KIND") == "review" else None, "result_file": res_path}
json.dump(meta, open(os.path.join(run_dir, "meta.json"), "w"), indent=2)

summary = (result or "").strip()
if len(summary) > 1500:
    summary = summary[:1500] + " …(truncated, see result.md)"
meta["summary"] = summary
if status != "ok":
    err = open(os.path.join(run_dir, "stderr.log"), encoding="utf-8", errors="replace").read()[-800:] if os.path.exists(os.path.join(run_dir, "stderr.log")) else ""
    meta["stderr_tail"] = err
print(json.dumps(meta, ensure_ascii=False, indent=2))
sys.exit(0 if status == "ok" else 1)
