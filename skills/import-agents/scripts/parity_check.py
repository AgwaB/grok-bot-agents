#!/usr/bin/env python3
"""Box-side runtime parity check: does everything each CLI's config points at exist here?

  parity_check.py [--cli codex,claude,pi,omp] [--no-run]

Read-only. Prints JSON {cli: {"ok": bool, "gaps": [{"item", "impact", "detail"}]}} and exits 1 if any gap is
`breaking`. Impact: breaking (the CLI fails to start or to answer), degraded (a feature, tool or MCP server is
missing), cosmetic (noise only). Never prints file contents or secret values; only paths, names and versions.
Checks: codex MCP server / notify commands; claude statusLine and hook commands, enabledPlugins installed, MCP
commands in ~/.claude.json; pi packages resolved by `pi list`, their node_modules, `pi --list-models` warnings
(skipped with --no-run); omp mcp.json commands.
Auth section ("auth", names only, also counted for "ok"): logins missing on the box, Codex keyring stores (the box has
none), env names that configs reference but neither the environment nor ~/.agent-keys defines, `!command` / op:// /
apiKeyHelper / auth.command references whose tool isn't on the box, remote MCP servers without a token on the box,
expired (Pi) or disabled (omp) provider logins, and Anthropic subscription OAuth used by Pi/omp (ToS flag).
"""
import argparse, json, os, re, shutil, subprocess

HOME = os.path.expanduser("~")
ABS = re.compile(r"(?<![\w.$}])(/(?:home|Users|opt|usr|Applications|Library)/[^\s'\";|&)]+)")


def lj(p):
    try: return json.load(open(p, encoding="utf-8"))
    except Exception: return None


def found(cmd):
    c = os.path.expanduser(cmd or "")
    return (os.path.isabs(c) and os.path.exists(c)) or bool(shutil.which(c))


def run(cmd, timeout=120):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL, cwd="/tmp")
        return r.returncode, r.stdout, r.stderr
    except Exception as e:
        return None, "", str(e)


def mcp_gaps(servers, where):
    g = []
    for name, s in (servers or {}).items():
        if not isinstance(s, dict) or s.get("enabled") is False or s.get("disabled") is True: continue
        cmd = s.get("command")
        if cmd and not found(cmd):
            g.append({"item": f"MCP server {name}", "impact": "degraded", "detail": f"{where}: command not found: {cmd}"})
    return g


def paths_missing(cmd):
    return [p for p in ABS.findall(cmd or "") if not os.path.exists(p)]


def codex():
    g = []; p = os.path.join(os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex"), "config.toml")
    if not os.path.isfile(p): return g
    try:
        import tomllib; cfg = tomllib.load(open(p, "rb"))
    except Exception as e:
        return [{"item": "config.toml", "impact": "breaking", "detail": f"not valid TOML: {str(e)[:120]}"}]
    g += mcp_gaps(cfg.get("mcp_servers"), "config.toml")
    n = cfg.get("notify")
    if isinstance(n, list) and n and not found(str(n[0])):
        g.append({"item": "notify", "impact": "cosmetic", "detail": f"program not found: {n[0]}"})
    return g


def claude():
    g = []; d = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    s = lj(os.path.join(d, "settings.json")) or {}
    sl = s.get("statusLine") or {}
    if isinstance(sl, dict) and sl.get("command"):
        first = sl["command"].split()[0].strip("'\"")
        miss = paths_missing(sl["command"]) + ([] if found(first) or first.startswith("$") else [first])
        if miss: g.append({"item": "statusLine", "impact": "cosmetic", "detail": f"missing: {sorted(set(miss))}"})
    hooks_missing = set()
    for groups in (s.get("hooks") or {}).values():
        for grp in groups if isinstance(groups, list) else []:
            for h in (grp or {}).get("hooks") or []:
                if isinstance(h, dict): hooks_missing.update(paths_missing(h.get("command")))
    if hooks_missing:
        g.append({"item": "hooks", "impact": "cosmetic", "detail": f"hook commands point at missing files: {sorted(hooks_missing)[:6]}"})
    inst = (lj(os.path.join(d, "plugins", "installed_plugins.json")) or {}).get("plugins") or {}
    for p, on in (s.get("enabledPlugins") or {}).items():
        if not on: continue
        e = (inst.get(p) or [{}])[0]
        if not (e.get("installPath") and os.path.isdir(e["installPath"])):
            g.append({"item": f"plugin {p}", "impact": "degraded", "detail": "enabled but not installed on the box"})
    cj = lj(os.path.join(HOME, ".claude.json")) or {}
    g += mcp_gaps(cj.get("mcpServers"), "~/.claude.json")
    return g


def pi(no_run):
    g = []; d = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(HOME, ".pi", "agent")
    s = lj(os.path.join(d, "settings.json"))
    if s is None: return g
    if not shutil.which("pi"):
        return [{"item": "pi", "impact": "breaking", "detail": "pi not on PATH"}]
    rc, out, err = run(["pi", "list"])
    listed, cur = {}, None
    for line in out.splitlines():
        if line.startswith("    ") and cur: listed[cur] = line.strip()
        elif line.startswith("  "): cur = line.strip(); listed[cur] = None
    for e in s.get("packages") or []:
        spec = e if isinstance(e, str) else (e or {}).get("source") or (e or {}).get("path") or ""
        path = listed.get(spec)
        if not path or not os.path.isdir(path):
            g.append({"item": f"pi package {spec}", "impact": "breaking", "detail": "not installed / path missing"}); continue
        pj = lj(os.path.join(path, "package.json")) or {}
        if (pj.get("dependencies") or pj.get("devDependencies")) and not os.path.isdir(os.path.join(path, "node_modules")) \
                and not spec.startswith("npm:"):
            g.append({"item": f"pi package {spec}", "impact": "breaking", "detail": "no node_modules (run npm ci in it)"})
    for e in s.get("extensions") or []:
        if isinstance(e, str) and not os.path.exists(os.path.join(d, e)):
            g.append({"item": f"pi extension {e}", "impact": "degraded", "detail": "file missing"})
    mj = lj(os.path.join(d, "mcp.json"))
    if mj: g += mcp_gaps(mj.get("mcpServers"), "pi mcp.json")
    if not no_run:
        rc, out, err = run(["pi", "--list-models"], timeout=180)
        text = out + err
        for w in sorted(set(re.findall(r'No models match pattern "([^"]+)"', text))):
            g.append({"item": f"pi model {w}", "impact": "degraded", "detail": "in enabledModels but not registered"})
        for m in re.findall(r"(?m)^(?:Error|Failed)[^\n]{0,200}", text)[:3]:
            g.append({"item": "pi startup", "impact": "breaking", "detail": m})
    return g


def omp():
    mj = lj(os.path.join(HOME, ".omp", "agent", "mcp.json")) or {}
    return mcp_gaps(mj.get("mcpServers"), "omp mcp.json")


# ---------------------------------------------------------------- auth (names only)
ENVREF = re.compile(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)+$")
INTERP = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-[^}]*)?\}")


def env_names():
    names = set(os.environ)
    try:
        for line in open(os.path.join(HOME, ".agent-keys"), encoding="utf-8", errors="replace"):
            m = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=", line)
            if m: names.add(m.group(1))
    except FileNotFoundError: pass
    return names


def tool_of(cmd):
    w = (str(cmd).lstrip("!").split() or [""])[0].strip("'\"")
    return w if re.match(r"^[A-Za-z0-9_./+-]{1,120}$", w) else None


def ref_gaps(v, where, names, impact="degraded"):
    """One config value: an env name that's unset, or an indirection whose tool is missing. Never prints the value."""
    if not isinstance(v, str) or not v: return []
    if v.startswith("!") or v.startswith("op://"):
        t = "op" if v.startswith("op://") else tool_of(v)
        if t and not found(t):
            return [{"item": where, "impact": impact, "detail": f"!command/op:// reference: `{os.path.basename(t)}` isn't on the box (re-import with resolve:, or set an env name)"}]
        return []
    out = []
    for n in INTERP.findall(v) + ([v] if ENVREF.match(v) else []):
        if n not in names: out.append({"item": where, "impact": impact, "detail": f"env {n} is not set on the box (add it to ~/.agent-keys)"})
    return out


def auth_codex(names):
    g = []; d = os.environ.get("CODEX_HOME") or os.path.join(HOME, ".codex")
    cfg = {}
    try:
        import tomllib; cfg = tomllib.load(open(os.path.join(d, "config.toml"), "rb"))
    except Exception: pass
    for k in ("cli_auth_credentials_store", "mcp_oauth_credentials_store"):
        if cfg.get(k) not in (None, "file"):
            g.append({"item": f"codex {k}", "impact": "breaking" if k.startswith("cli") else "degraded", "detail": f"= {cfg[k] if cfg[k] in ('keyring', 'auto', 'ephemeral') else 'other'}: the box has no OS keyring; set it to \"file\""})
    if not os.path.isfile(os.path.join(d, "auth.json")) and not ({"OPENAI_API_KEY", "CODEX_API_KEY"} & names):
        g.append({"item": "codex login", "impact": "breaking", "detail": "no auth.json and no API key env: run `codex login --device-auth` on the box"})
    for pid, p in (cfg.get("model_providers") or {}).items():
        if isinstance(p, dict) and p.get("env_key") and p["env_key"] not in names:
            g.append({"item": f"codex model_providers.{pid}", "impact": "degraded", "detail": f"env {p['env_key']} is not set on the box"})
        a = (p or {}).get("auth") if isinstance(p, dict) else None
        if isinstance(a, dict) and a.get("command"):
            c = a["command"]; t = tool_of(c[0] if isinstance(c, list) and c else c)
            if t and not found(t): g.append({"item": f"codex model_providers.{pid}.auth", "impact": "degraded", "detail": f"command `{os.path.basename(t)}` isn't on the box"})
    creds = lj(os.path.join(d, ".credentials.json")) or {}
    for name, s in (cfg.get("mcp_servers") or {}).items():
        if not isinstance(s, dict) or s.get("enabled") is False: continue
        for n in ([s["bearer_token_env_var"]] if s.get("bearer_token_env_var") else []) + list((s.get("env_http_headers") or {}).values()) + list(s.get("env_vars") or []):
            if isinstance(n, str) and n not in names: g.append({"item": f"codex MCP {name}", "impact": "degraded", "detail": f"env {n} is not set on the box"})
        if s.get("url") and not (s.get("bearer_token_env_var") or s.get("env_http_headers") or s.get("http_headers") or s.get("experimental_bearer_token")) \
                and not any(str(k).startswith(name) for k in creds):
            g.append({"item": f"codex MCP {name}", "impact": "degraded", "detail": f"remote server, no token on the box: run `codex mcp login {name}`"})
    return g


def auth_claude(names):
    g = []; d = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(HOME, ".claude")
    s = lj(os.path.join(d, "settings.json")) or {}
    creds = lj(os.path.join(d, ".credentials.json")) or {}
    envs = set((s.get("env") or {}).keys()) | names
    if not creds.get("claudeAiOauth") and not ({"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"} & envs) and not s.get("apiKeyHelper"):
        g.append({"item": "claude login", "impact": "breaking", "detail": "not logged in on the box: run `claude auth login` (setup-agents headless recipe)"})
    if isinstance(s.get("apiKeyHelper"), str):
        t = tool_of(s["apiKeyHelper"])
        if t and not found(t): g.append({"item": "claude apiKeyHelper", "impact": "breaking", "detail": f"`{os.path.basename(t)}` isn't on the box"})
    mo = creds.get("mcpOAuth") or {}
    cj = lj(os.path.join(HOME, ".claude.json")) or {}
    for name, sv in (cj.get("mcpServers") or {}).items():
        if not isinstance(sv, dict): continue
        for k in ("env", "headers"):
            for e, v in (sv.get(k) or {}).items():
                for n in INTERP.findall(v) if isinstance(v, str) else []:
                    if n not in names: g.append({"item": f"claude MCP {name}", "impact": "degraded", "detail": f"env {n} is not set on the box"})
        remote = sv.get("url") or sv.get("type") in ("http", "sse")
        if remote and not sv.get("headers") and not any(str(k).split("|")[0] == name for k in mo):
            g.append({"item": f"claude MCP {name}", "impact": "degraded", "detail": "remote server, no OAuth token on the box: authenticate it with /mcp in claude"})
    return g


def auth_pi(names):
    g = []; d = os.environ.get("PI_CODING_AGENT_DIR") or os.path.join(HOME, ".pi", "agent")
    a = lj(os.path.join(d, "auth.json")) or {}
    import time; now = time.time() * 1000
    for p, e in a.items():
        if not isinstance(e, dict): continue
        if e.get("type") == "oauth":
            if isinstance(e.get("expires"), (int, float)) and e["expires"] < now:
                g.append({"item": f"pi login {p}", "impact": "cosmetic", "detail": "access token expired: Pi refreshes it on first use; if that fails, `/login` again"})
            if re.search(r"anthropic|claude", p, re.I):
                g.append({"item": f"pi login {p}", "impact": "cosmetic", "detail": "Anthropic subscription OAuth outside Claude Code: ToS risk (Anthropic allows it only in its own apps)"})
        else:
            g += ref_gaps(e.get("key"), f"pi auth.json {p}", names)
    m = lj(os.path.join(d, "models.json")) or {}
    for p, e in (m.get("providers") or {}).items():
        if isinstance(e, dict): g += ref_gaps(e.get("apiKey"), f"pi models.json {p}", names)
    return g


def auth_omp(names):
    g = []; d = os.path.join(HOME, ".omp", "agent")
    text = ""
    try: text = open(os.path.join(d, "models.yml"), encoding="utf-8").read()
    except FileNotFoundError: pass
    stack = []
    for line in text.splitlines():
        mm = re.match(r"^(\s*)([\"']?)([A-Za-z0-9_.@-]+)\2\s*:\s*(.*)$", line)
        if not mm or re.match(r"^\s*(#|$)", line): continue
        ind = len(mm.group(1))
        while stack and stack[-1][0] >= ind: stack.pop()
        path = [k for _, k in stack]
        if mm.group(3) == "apiKey" and len(path) == 2 and path[0] == "providers":
            g += ref_gaps(re.sub(r"\s+#.*$", "", mm.group(4)).strip().strip("\"'"), f"omp models.yml {path[1]}", names)
        stack.append((ind, mm.group(3)))
    db = os.path.join(d, "agent.db")
    if os.path.isfile(db):
        import sqlite3, tempfile
        t = tempfile.mkdtemp(prefix="omp-parity-")
        try:
            for n in ("agent.db", "agent.db-wal", "agent.db-shm"):
                if os.path.isfile(os.path.join(d, n)): shutil.copyfile(os.path.join(d, n), os.path.join(t, n))
            c = sqlite3.connect(os.path.join(t, "agent.db"))
            try: rows = c.execute("select provider, disabled_cause is null from auth_credentials").fetchall()
            finally: c.close()
            ok = {r[0] for r in rows if r[1]}
            for p in sorted({r[0] for r in rows} - ok):
                g.append({"item": f"omp login {p}", "impact": "degraded", "detail": "disabled: run `omp`, then `/login` (on the box, or on the computer and re-import)"})
            for p in sorted(ok):
                if re.search(r"anthropic|claude", p, re.I):
                    g.append({"item": f"omp login {p}", "impact": "cosmetic", "detail": "Anthropic subscription OAuth outside Claude Code: ToS risk"})
        except Exception: pass
        finally: shutil.rmtree(t, ignore_errors=True)
    return g


def check(cli, no_run=False):
    f = {"codex": codex, "claude": claude, "pi": lambda: pi(no_run), "omp": omp}[cli]
    try: gaps = f()
    except Exception as e: gaps = [{"item": "parity check", "impact": "degraded", "detail": f"check failed: {type(e).__name__}: {str(e)[:120]}"}]
    try: auth = {"codex": auth_codex, "claude": auth_claude, "pi": auth_pi, "omp": auth_omp}[cli](env_names())
    except Exception as e: auth = [{"item": "auth check", "impact": "degraded", "detail": f"check failed: {type(e).__name__}"}]
    return {"ok": not any(x["impact"] == "breaking" for x in gaps + auth), "gaps": gaps, "auth": auth}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cli", default="codex,claude,pi,omp"); ap.add_argument("--no-run", action="store_true")
    a = ap.parse_args()
    os.environ["PATH"] = os.pathsep.join([os.path.join(HOME, ".local/bin"), os.path.join(HOME, ".bun/bin"), os.environ.get("PATH", "")])
    res = {c: check(c, a.no_run) for c in a.cli.split(",") if c}
    print(json.dumps(res, indent=2))
    raise SystemExit(1 if any(not r["ok"] for r in res.values()) else 0)


if __name__ == "__main__":
    main()
