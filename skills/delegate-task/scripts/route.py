#!/usr/bin/env python3
"""Pick who does a task: the bot itself (light work) or one enabled CLI (heavy work), per delegate-task SKILL.md.

  route.py decide [--named AGENT] [--type bulk|long|hard|review|general] [--review-of AGENT]
                  [--files N] [--input-kb N] [--steps N] [--minutes N] [--loop] [--bulk] [--scheduled]
  route.py mark-limited AGENT (--until ISO | --message TEXT)   # record a rate limit (run-agent does this itself)
  route.py state | route.py clear [AGENT]

Prints JSON: {"decision": "self"|"delegate"|"none", "agent", "type", "reason", "heavy_because", "skipped": {...}}
Availability = enabled in ~/.grok-agents/config.json, installed, and not rate-limited (state.json, until > now).
"""
import argparse, datetime as dt, json, os, re, shutil, sys

CFG = os.path.expanduser(os.environ.get("GROK_AGENTS_CONFIG", "~/.grok-agents/config.json"))
STATE = os.environ.get("GROK_AGENTS_STATE") or os.path.join(os.path.dirname(CFG), "state.json")
BINS = [os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.bun/bin")]
FRONTIER = ["claude", "codex"]
UTC = dt.timezone.utc

def now():
    o = os.environ.get("ROUTE_NOW")  # for tests
    return dt.datetime.fromisoformat(o) if o else dt.datetime.now(UTC)

def load(p, default):
    try: return json.load(open(p))
    except Exception: return default

def save_state(st):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"; json.dump(st, open(tmp, "w"), indent=2); os.replace(tmp, STATE)

def parse_reset(msg, ref=None):
    """Best-effort reset time from a provider message. Falls back to ref+1h."""
    ref = ref or now(); m = msg or ""
    r = re.search(r"retry-after-ms=(\d+)", m)
    if r: return ref + dt.timedelta(milliseconds=int(r.group(1)))
    r = re.search(r"reset(?:s)? at (\d{1,2})-(\d{1,2}) (\d{1,2}):(\d{2})(?::(\d{2}))? UTC", m)
    if r:
        mo, d, h, mi, s = (int(x or 0) for x in r.groups())
        t = dt.datetime(ref.year, mo, d, h, mi, s, tzinfo=UTC)
        return t if t > ref else t.replace(year=ref.year + 1)
    r = re.search(r"resets? (?:at )?(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s*\(([^)]+)\)", m, re.I)
    if r:
        from zoneinfo import ZoneInfo
        h = int(r.group(1)) % 12 if r.group(3) else int(r.group(1)); mi = int(r.group(2) or 0)
        if (r.group(3) or "").lower() == "pm": h += 12
        try: tz = ZoneInfo(r.group(4).strip())
        except Exception: tz = UTC
        loc = ref.astimezone(tz); t = loc.replace(hour=h, minute=mi, second=0, microsecond=0)
        if t <= loc: t += dt.timedelta(days=1)
        return t.astimezone(UTC)
    return ref + dt.timedelta(hours=1)

def mark_limited(agent, until=None, message=None):
    st = load(STATE, {}); t = dt.datetime.fromisoformat(until) if until else parse_reset(message)
    st.setdefault("rate_limited", {})[agent] = {"until": t.astimezone(UTC).isoformat(), "message": (message or "")[:300]}
    save_state(st); return st["rate_limited"][agent]

def limited(st, a):
    x = (st.get("rate_limited") or {}).get(a)
    return x if x and dt.datetime.fromisoformat(x["until"]) > now() else None

def installed(a):
    return shutil.which(a, path=os.pathsep.join(BINS + [os.environ.get("PATH", "")])) is not None

HEAVY = [("files", 5, "touches ~5+ files or a whole folder"), ("input_kb", 50, "input over ~50 KB"),
         ("steps", 10, "~10+ tool/shell steps"), ("minutes", 5, "takes 5+ minutes")]

def decide(a):
    cfg = load(CFG, None)
    if cfg is None: return {"decision": "self", "agent": None, "reason": "no ~/.grok-agents/config.json; offer setup-agents for heavy work"}
    st = load(STATE, {}); enabled = cfg.get("enabled", []); rt = cfg.get("routing", {})
    skipped = {}
    def avail(x):
        if x not in enabled: skipped[x] = "not enabled"; return False
        if not installed(x): skipped[x] = "not installed"; return False
        l = limited(st, x)
        if l: skipped[x] = f"rate_limited until {l['until']}"; return False
        return True
    if a.named:
        ok = avail(a.named)
        return {"decision": "delegate" if ok else "none", "agent": a.named if ok else None, "type": "named",
                "reason": "user named this CLI" + ("" if ok else f"; unavailable ({skipped.get(a.named)}): tell the user and ask before switching"),
                "skipped": skipped}
    why = [txt for k, lim, txt in HEAVY if (getattr(a, k) or 0) >= lim]
    if a.loop: why.append("needs a run-check-fix loop")
    if a.bulk: why.append("bulk / token-heavy generation")
    if a.scheduled: why.append("routine or scheduled repetition")
    if a.type in ("long",): why.append("long or unattended")
    if a.type == "review": why.append("review or second opinion")
    if a.type == "hard" and not why: why.append("hard reasoning on real code (frontier CLI)")
    if not why:
        return {"decision": "self", "agent": None, "type": "light", "reason": "light work: do it yourself", "heavy_because": []}
    t = a.type if a.type != "general" else ("bulk" if a.bulk else "hard")
    cheap, hard = rt.get("cheap", ["pi", "omp"]), rt.get("hard", FRONTIER)
    if rt.get("mode") == "fixed" and rt.get("fixed"): order = [rt["fixed"]]
    elif t == "bulk": order = cheap + hard
    elif t == "long": order = ["omp"] + hard + [x for x in cheap if x != "omp"]
    elif t == "review":
        other = [x for x in FRONTIER if x != a.review_of] if a.review_of in FRONTIER else FRONTIER
        order = other + [x for x in cheap if x != a.review_of] + ([a.review_of] if a.review_of else [])
    else: order = hard + cheap
    prefer = [x for x in rt.get("prefer", []) if x in order]
    order = prefer + [x for x in order if x not in prefer]
    seen = []
    for x in order:
        if x in seen: continue
        seen.append(x)
        if avail(x):
            note = "" if x == order[0] else f" (fallback; first choice {order[0]} skipped: {skipped.get(order[0], 'n/a')})"
            res = {"decision": "delegate", "agent": x, "type": t, "reason": f"{t} work -> {x}{note}",
                   "heavy_because": why, "order": seen + [y for y in order if y not in seen], "skipped": skipped}
            if rt.get("mode") == "ask": res["ask_first"] = True
            if t == "review" and x == a.review_of: res["reason"] += "; same agent: use a fresh session, read-only"
            if t == "bulk" and x in FRONTIER: res["hint"] = "frontier fallback for bulk work: pass --reasoning low"
            return res
    return {"decision": "self", "agent": None, "type": t, "reason": "no CLI available; do it yourself in chunks and offer setup",
            "heavy_because": why, "skipped": skipped}

def main():
    ap = argparse.ArgumentParser(); sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("decide")
    d.add_argument("--named"); d.add_argument("--type", default="general", choices=["general", "bulk", "long", "hard", "review"])
    d.add_argument("--review-of"); d.add_argument("--loop", action="store_true"); d.add_argument("--bulk", action="store_true")
    d.add_argument("--scheduled", action="store_true")
    for k in ("files", "input-kb", "steps", "minutes"): d.add_argument("--" + k, type=float, default=0)
    m = sub.add_parser("mark-limited"); m.add_argument("agent"); m.add_argument("--until"); m.add_argument("--message")
    sub.add_parser("state"); c = sub.add_parser("clear"); c.add_argument("agent", nargs="?")
    a = ap.parse_args()
    if a.cmd == "decide": print(json.dumps(decide(a), indent=2))
    elif a.cmd == "mark-limited": print(json.dumps(mark_limited(a.agent, a.until, a.message)))
    elif a.cmd == "state": print(json.dumps(load(STATE, {}), indent=2))
    elif a.cmd == "clear":
        st = load(STATE, {}); rl = st.get("rate_limited", {})
        if a.agent: rl.pop(a.agent, None)
        else: rl.clear()
        save_state(st); print("cleared")

if __name__ == "__main__":
    main()
