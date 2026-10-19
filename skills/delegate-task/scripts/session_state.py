"""Read only run-owned session evidence; never search a user's global CLI homes."""
import json
import re
from itertools import islice
from pathlib import Path


def events(text):
    """Accept legacy Claude JSON and JSONL; only LF separates records."""
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


def read_text(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def session_header(path):
    """omp can prepend a title; bound discovery without loading message bodies."""
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in islice(f, 32):
            for record in events(line):
                if record.get("type") == "session":
                    return record
    return {}


def session_info(agent, run_dir, workdir):
    """Recover IDs even without terminal meta; Pi/omp also need an owned session file."""
    r = Path(run_dir)
    session = None
    for ev in events(read_text(r / "stdout.log")):
        if agent == "claude":
            value = ev.get("session_id")
        elif agent == "codex" and ev.get("type") == "thread.started":
            value = ev.get("thread_id")
        elif agent in ("pi", "omp") and ev.get("type") == "session":
            value = ev.get("id")
        else:
            value = None
        if isinstance(value, str) and value and not value.startswith("-"):
            session = value
    if not session:
        try:
            value = json.loads(read_text(r / "meta.json")).get("session_id")
            if isinstance(value, str) and value and not value.startswith("-"):
                session = value
        except (ValueError, AttributeError):
            pass
    matches = []
    if agent in ("pi", "omp"):
        for path in sorted((r / "sessions").glob("*.jsonl")):
            try:
                header = session_header(path)
                if (header.get("type") == "session" and isinstance(header.get("id"), str)
                        and header["id"] and (not session or header["id"] == session)
                        and Path(header["cwd"]).resolve() == Path(workdir).resolve()):
                    matches.append((header["id"], str(path.resolve())))
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                continue
    session_file = None
    if len(matches) == 1:
        session, session_file = matches[0]
    return {"session_id": session, "session_file": session_file}


def resume_rejected(run_dir):
    """Only retry a clear startup rejection, never a failed/partly executed agent turn."""
    r = Path(run_dir)
    try:
        meta = json.loads(read_text(r / "meta.json"))
    except ValueError:
        return False
    if meta.get("status") != "error" or meta.get("exit_code") in (124, 130, 137, 143):
        return False
    raw = read_text(r / "stdout.log")
    for ev in events(raw):
        typ = ev.get("type")
        if not isinstance(typ, str):
            continue
        if (typ in ("assistant", "user", "turn.started", "agent_start", "message_start", "message_end")
                or typ.startswith(("item.", "tool_execution"))):
            return False
    diagnostics = raw + "\n" + read_text(r / "stderr.log")
    if re.search(r"\b(?:401|403)\b|unauthori[sz]ed|forbidden|authentication|invalid (?:api key|session token)", diagnostics, re.I):
        return False
    # CLI wording varies by release. Deliberately narrow: unrelated auth/model/network errors
    # must not replay the task in a fresh session merely because native resume was requested.
    return bool(re.search(
        r"(?:no|unknown|invalid|missing) (?:saved )?(?:conversation|session)(?: (?:found|id|file))?"
        r"|(?:session|conversation)(?: (?:id|file))?[^\n]{0,180}(?:not found|does not exist|cannot be resumed)"
        r"|(?:could not|cannot|failed to|unable to) (?:find|load|resume) (?:the )?(?:session|conversation)"
        r"|(?:unknown|unrecognized|unexpected|invalid) (?:argument|option)[^\n]{0,80}--(?:resume|session)\b",
        diagnostics, re.I))
