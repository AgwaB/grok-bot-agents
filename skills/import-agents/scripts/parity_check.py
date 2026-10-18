#!/usr/bin/env python3
"""Box-side runtime parity check: does everything each CLI's config points at exist here?

  parity_check.py [--cli codex,claude,pi,omp] [--no-run]

Read-only. Prints JSON {cli: {"ok": bool, "gaps": [{"item", "impact", "detail"}]}} and exits 1 if any gap is
`breaking`. Impact: breaking (the CLI fails to start or to answer), degraded (a feature, tool or MCP server is
missing), cosmetic (noise only). Never prints file contents or secret values; only paths, names and versions.
Checks: codex MCP server / notify commands; claude statusLine and hook commands, enabledPlugins installed, MCP
commands in ~/.claude.json; pi packages resolved by `pi list`, their node_modules, `pi --list-models` warnings
(skipped with --no-run); omp mcp.json commands.
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


def check(cli, no_run=False):
    f = {"codex": codex, "claude": claude, "pi": lambda: pi(no_run), "omp": omp}[cli]
    try: gaps = f()
    except Exception as e: gaps = [{"item": "parity check", "impact": "degraded", "detail": f"check failed: {type(e).__name__}: {str(e)[:120]}"}]
    return {"ok": not any(x["impact"] == "breaking" for x in gaps), "gaps": gaps}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--cli", default="codex,claude,pi,omp"); ap.add_argument("--no-run", action="store_true")
    a = ap.parse_args()
    os.environ["PATH"] = os.pathsep.join([os.path.join(HOME, ".local/bin"), os.path.join(HOME, ".bun/bin"), os.environ.get("PATH", "")])
    res = {c: check(c, a.no_run) for c in a.cli.split(",") if c}
    print(json.dumps(res, indent=2))
    raise SystemExit(1 if any(not r["ok"] for r in res.values()) else 0)


if __name__ == "__main__":
    main()
