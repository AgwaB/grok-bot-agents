#!/usr/bin/env python3
"""Post-install steps for apply_import.sh, so an imported CLI runs like it does on the user's computer.

Called by apply_import.sh after the files are installed (never on --dry-run). Each step reports what it did;
failures become warnings, never exceptions. Never prints file contents.
- pi:     installs missing `npm:` packages (`pi update --extensions`), lays the computer's npm package files over the
          same versions on the box (keeps local patches), recreates package Python venvs from the manifest pins.
- claude: adds missing marketplaces, installs enabledPlugins, pins them to the computer's git SHA, points hook /
          statusLine commands whose absolute tool path is missing at the box's copy of that tool.
- codex:  disables MCP servers and `notify` commands that point at programs missing on the box (e.g. /Applications).
- all:    installs missing MCP server binaries that the computer had as `uv tool`s, flags a box CLI older than the computer's.
Auth helpers (used by apply_import.sh before files are written, so re-imports compare equal):
- merge_agent_keys: staged _secrets/secrets.env (export_secrets.sh) -> ~/.agent-keys (600), same-name lines replaced,
  no duplicates, values never printed; the staged file is overwritten and deleted.
- pin_codex_file_store: the box has no OS keyring, so Codex config.toml gets cli_auth_credentials_store and
  mcp_oauth_credentials_store = "file".
- rewrite_refs: Pi auth.json / models.json and omp models.yml `!command` / op:// keys that export_secrets.sh resolved
  become env-name references (_secrets/rewrites.txt: "<cli> <file> <provider> <ENV>").
"""
import json, os, re, shutil, subprocess
from import_safety import atomic_write, claude_global, guard_path, package_root

HOME = os.path.expanduser("~")
ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=")
STAGED_LINE = re.compile(r"^export ([A-Za-z_][A-Za-z0-9_]*)='(?:[^']|'\\'')*'$")


def shred(path):
    """Overwrite a staged secret with zeros, then delete it (best effort on copy-on-write filesystems)."""
    try:
        n = os.path.getsize(path)
        with open(path, "r+b") as f:
            f.write(b"\0" * n); f.flush(); os.fsync(f.fileno())
    except Exception: pass
    try: os.remove(path)
    except FileNotFoundError: pass


def agent_keys_names(path=None):
    """Env names defined in ~/.agent-keys (names only; values are never returned)."""
    path = path or os.path.join(HOME, ".agent-keys"); names = set()
    try:
        for line in open(path, encoding="utf-8", errors="replace"):
            m = ENV_LINE.match(line)
            if m: names.add(m.group(1))
    except FileNotFoundError: pass
    return names


def merge_agent_keys(stage_root, dry=False, keys_path=None):
    """Merge <stage_root>/_secrets/secrets.env into ~/.agent-keys. Returns names only."""
    src = os.path.join(stage_root, "_secrets", "secrets.env")
    keys_path = keys_path or os.path.join(HOME, ".agent-keys")
    guard_path(src); guard_path(keys_path); guard_path(os.path.join(HOME, ".bashrc"))
    if not os.path.isfile(src): return None
    new, bad = {}, 0
    for line in open(src, encoding="utf-8", errors="replace").read().splitlines():
        if not line.strip(): continue
        m = STAGED_LINE.match(line)
        if m: new[m.group(1)] = line
        else: bad += 1
    cur = open(keys_path, encoding="utf-8", errors="replace").read().splitlines() if os.path.isfile(keys_path) else []
    out, seen, res = [], set(), {"added": [], "replaced": [], "unchanged": [], "rejected_lines": bad}
    for line in cur:
        m = ENV_LINE.match(line); n = m.group(1) if m else None
        if n in new:
            if n in seen: continue  # drop a duplicate of a name we set
            seen.add(n); res["unchanged" if line == new[n] else "replaced"].append(n); out.append(new[n]); continue
        out.append(line)
    for n, line in new.items():
        if n not in seen: out.append(line); res["added"].append(n)
    for k in ("added", "replaced", "unchanged"): res[k] = sorted(res[k])
    res["keys_file"] = keys_path
    if dry: return res
    atomic_write(keys_path, ("\n".join(out) + "\n").encode("utf-8"))
    shred(src)
    rc = os.path.join(HOME, ".bashrc")
    try: has = "agent-keys" in open(rc, encoding="utf-8", errors="replace").read()
    except FileNotFoundError: has = False
    if not has:
        with open(rc, "a", encoding="utf-8") as f: f.write("\n[ -f ~/.agent-keys ] && . ~/.agent-keys\n")
        res["bashrc_sources_it"] = "added"
    return res


def pin_codex_file_store(text):
    """Force file credential stores in a Codex config.toml (top-level keys). Returns (text, changed key names)."""
    m = re.search(r"(?m)^\s*\[", text); top, rest = (text[:m.start()], text[m.start():]) if m else (text, "")
    changed = []
    for key in ("cli_auth_credentials_store", "mcp_oauth_credentials_store"):
        line = f'{key} = "file"  # box: no OS keyring here (grok-bot-agents import)'
        pat = re.compile(r"(?m)^[ \t]*%s[ \t]*=.*$" % key)
        hit = pat.search(top)
        if hit:
            if re.match(r'^[ \t]*%s[ \t]*=[ \t]*"file"' % key, hit.group(0)): continue
            top = top[:hit.start()] + line + top[hit.end():]
        else:
            top = line + "\n" + top
        changed.append(key)
    return top + rest, changed


def read_rewrites(stage_root):
    """[(cli, file, provider, env)] from _secrets/rewrites.txt (names only)."""
    p = os.path.join(stage_root, "_secrets", "rewrites.txt"); res = []
    if not os.path.isfile(p): return res
    for line in open(p, encoding="utf-8", errors="replace"):
        parts = line.split()
        if len(parts) == 4 and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", parts[3]) and re.match(r"^[A-Za-z0-9_.@:+-]+$", parts[2]):
            res.append(tuple(parts))
    return res


def _indirect(v):
    return isinstance(v, str) and (v.startswith("!") or v.startswith("op://"))


def rewrite_refs(cli, rel, data, rewrites):
    """Point resolved `!command` / op:// keys at env names. Returns (bytes, [provider names rewritten])."""
    todo = {p: env for c, f, p, env in rewrites if c == cli and f == rel}
    if not todo: return data, []
    try: text = data.decode("utf-8")
    except UnicodeDecodeError: return data, []
    done = []
    if rel in ("auth.json", "models.json"):
        try: j = json.loads(text)
        except Exception: return data, []
        for p, env in todo.items():
            e = (j.get(p) if rel == "auth.json" else (j.get("providers") or {}).get(p))
            k = "key" if rel == "auth.json" else "apiKey"
            if isinstance(e, dict) and _indirect(e.get(k)):
                e[k] = env; done.append(p)
        return (json.dumps(j, indent=2) + "\n").encode("utf-8") if done else data, done
    if rel == "models.yml":
        lines, stack = text.split("\n"), []
        for i, line in enumerate(lines):
            m = re.match(r"^(\s*)([\"']?)([A-Za-z0-9_.@-]+)\2\s*:\s*(.*)$", line)
            if not m or re.match(r"^\s*(#|$)", line): continue
            ind = len(m.group(1))
            while stack and stack[-1][0] >= ind: stack.pop()
            path = [k for _, k in stack]
            if m.group(3) == "apiKey" and len(path) == 2 and path[0] == "providers" and path[1] in todo:
                v = re.sub(r"\s+#.*$", "", m.group(4)).strip().strip("\"'")
                if _indirect(v):
                    lines[i] = f"{m.group(1)}apiKey: {todo[path[1]]}"; done.append(path[1])
            stack.append((ind, m.group(3)))
        return ("\n".join(lines)).encode("utf-8") if done else data, done
    return data, []


def sh(cmd, timeout=600, cwd=None, env=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd, env=env,
                           shell=isinstance(cmd, str), stdin=subprocess.DEVNULL)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except Exception:  # timeout, missing binary; exceptions can contain secret-bearing argv
        return None, ""


def last_line(text):
    # Helper diagnostics can echo credentials. Never forward even a truncated line.
    return "output suppressed" if text else ""


def read_manifest(stage_root):
    m = {"home": [], "venv": {}, "pip": {}, "uvtool": {}, "version": {}}
    p = os.path.join(stage_root, "_meta", "manifest.txt")
    if not os.path.isfile(p): return m
    for line in open(p, encoding="utf-8", errors="replace"):
        parts = line.rstrip("\n").split(" ")
        k = parts[0]
        if k == "home" and len(parts) >= 2 and package_root(" ".join(parts[1:])): m["home"].append(" ".join(parts[1:]))
        elif k == "venv" and len(parts) >= 3 and package_root(parts[1]): m["venv"][parts[1]] = parts[2]
        elif k == "pip" and len(parts) >= 3 and package_root(parts[1]): m["pip"].setdefault(parts[1], []).append(parts[2])
        elif k == "uvtool" and len(parts) >= 3: m["uvtool"][parts[2]] = parts[1]
        elif k == "version" and len(parts) >= 3: m["version"][parts[1]] = " ".join(parts[2:])
    return m


def vtuple(s):
    m = re.search(r"(\d+)\.(\d+)(?:\.(\d+))?", s or "")
    return tuple(int(x or 0) for x in m.groups()) if m else None


def check_version(cli, man, warnings):
    src = man["version"].get(cli)
    if not src or not shutil.which(cli): return None
    _, out = sh([cli, "--version"], timeout=30)
    a, b = vtuple(src), vtuple(out)
    src = ".".join(map(str, a)) if a else "unknown"
    box = ".".join(map(str, b)) if b else "unknown"
    res: dict[str, object] = {"computer": src, "box": box}
    if a and b and b < a:
        warnings.append(f"{cli} on the box ({box}) is older than on the computer ({src}); newer settings or plugins may "
                        f"not load. Update it on the box (setup-agents install step, pinned to that version).")
        res["older_on_box"] = True
    return res


def which_cmd(cmd):
    if not cmd: return True
    c = os.path.expanduser(cmd)
    return bool(os.path.isabs(c) and os.path.exists(c)) or bool(shutil.which(c))


def install_uvtools(commands, man, warnings):
    """commands: missing MCP server commands. Installs the uv tool the computer had for them."""
    done = []
    for cmd in sorted(set(commands)):
        base = os.path.basename(cmd)
        spec = man["uvtool"].get(base)
        if not spec: continue
        if not shutil.which("uv"):
            warnings.append(f"uv not found: can't install {spec} for MCP command {base}"); break
        rc, out = sh(["uv", "tool", "install", spec], timeout=900)
        ok = rc == 0 or "already installed" in out
        done.append({"tool": spec, "for": base, "ok": ok})
        if not ok: warnings.append(f"uv tool install {spec} failed: {last_line(out)}")
    return done


# ---------------------------------------------------------------- pi
def pi_list():
    """{spec: resolved_path or None} from `pi list`."""
    rc, out = sh(["pi", "list"], timeout=120)
    res, cur = {}, None
    for line in out.splitlines():
        if line.startswith("    ") and cur is not None:
            res[cur] = line.strip(); continue
        if line.startswith("  ") and not line.startswith("    "):
            cur = line.strip(); res[cur] = None
    return res


def npm_name(spec):
    n = spec[4:] if spec.startswith("npm:") else spec
    if n.startswith("@"):
        return n if n.count("@") == 1 else n.rsplit("@", 1)[0]
    return n.split("@", 1)[0]


def pkg_version(d):
    try: return json.load(open(os.path.join(d, "package.json"), encoding="utf-8")).get("version")
    except Exception: return None


def pi_post(stage, dest_root, man, warnings):
    res = {}
    if not shutil.which("pi"):
        warnings.append("pi not found on PATH: package steps skipped"); return res
    listed = pi_list()
    missing = [s for s, p in listed.items() if s.startswith(("npm:", "git:", "http")) and not (p and os.path.isdir(p))]
    if missing:
        rc, out = sh(["pi", "update", "--extensions"], timeout=900)
        res["pi_update"] = {"for": missing, "rc": rc, "last": last_line(out)}
        listed = pi_list()
        still = [s for s in missing if not (listed.get(s) and os.path.isdir(listed[s]))]
        if still: warnings.append(f"pi packages still not installed: {still}; run `pi install <spec>` on the box")
    # overlay the computer's npm package files over the same versions (keeps local patches)
    ov_root = os.path.join(stage, "npm", "node_modules")
    if os.path.isdir(ov_root):
        resolved = {npm_name(s): p for s, p in listed.items() if s.startswith("npm:") and p}
        hoist = set()
        for p in resolved.values():
            d = p
            while d not in ("/", "") and os.path.basename(d) != "node_modules": d = os.path.dirname(d)
            if d not in ("/", ""): hoist.add(d)
        pkgs = []
        for n in sorted(os.listdir(ov_root)):
            if n.startswith("@") and os.path.isdir(os.path.join(ov_root, n)):
                pkgs += [f"{n}/{x}" for x in sorted(os.listdir(os.path.join(ov_root, n)))]
            else: pkgs.append(n)
        report = []
        for name in pkgs:
            src = os.path.join(ov_root, name); sv = pkg_version(src)
            cands = ([resolved[name]] if name in resolved else []) + \
                    [os.path.join(p, "node_modules", name) for p in resolved.values()] + \
                    [os.path.join(h, name) for h in sorted(hoist)] + [os.path.join(HOME, ".pi/agent/npm/node_modules", name)]
            tgt = next((c for c in cands if os.path.isdir(c) and pkg_version(c) == sv), None)
            if not tgt:
                other = next((c for c in cands if os.path.isdir(c)), None)
                if other: warnings.append(f"npm overlay {name}: box has {pkg_version(other)}, computer {sv}; left as is")
                continue
            changed = 0
            for root, _, files in os.walk(src):
                for f in files:
                    s = os.path.join(root, f); t = os.path.join(tgt, os.path.relpath(s, src))
                    data = open(s, "rb").read()
                    if os.path.isfile(t) and open(t, "rb").read() == data: continue
                    os.makedirs(os.path.dirname(t), exist_ok=True)
                    with open(t, "wb") as fh: fh.write(data)
                    changed += 1
            if changed: report.append({"package": name, "version": sv, "files_patched": changed, "into": tgt})
        res["npm_overlay"] = report
    # python venvs of local packages
    venvs = []
    for root, pyver in man["venv"].items():
        d = os.path.join(HOME, root)
        if not os.path.isdir(d): continue
        pins = man["pip"].get(root, [])
        py = os.path.join(d, ".venv", "bin", "python")
        step = {"dir": root, "python": pyver, "pins": len(pins)}
        if not os.path.exists(py):
            rc, out = (sh(["uv", "venv", "--python", pyver, ".venv"], cwd=d, timeout=600) if shutil.which("uv")
                       else sh(["python3", "-m", "venv", ".venv"], cwd=d, timeout=600))
            if rc != 0: warnings.append(f"{root}: venv creation failed: {last_line(out)}"); venvs.append(dict(step, ok=False)); continue
        if pins:
            req = os.path.join(d, ".venv", "import-requirements.txt")
            open(req, "w").write("\n".join(p for p in pins if not p.startswith(("pip==", "setuptools==", "wheel=="))) + "\n")
            cmd = (["uv", "pip", "install", "--python", py, "-r", req] if shutil.which("uv") else [py, "-m", "pip", "install", "-r", req])
            rc, out = sh(cmd, cwd=d, timeout=1200)
            step["ok"] = rc == 0
            if rc != 0: warnings.append(f"{root}: installing the computer's Python pins failed: {last_line(out)}")
        else: step["ok"] = True
        venvs.append(step)
    if venvs:
        res["venvs"] = venvs
        warnings.append("Python venvs were rebuilt from pinned versions. Packages that download browsers or models on "
                        "first use (e.g. `<venv>/bin/scrapling install`) need that step once on the box.")
    return res


# ---------------------------------------------------------------- claude
def claude_post(stage, dest_root, man, warnings):
    res = {}
    if not shutil.which("claude"):
        warnings.append("claude not found on PATH: plugin steps skipped"); return res
    sp = os.path.join(dest_root, "settings.json")
    try: settings = json.load(open(sp, encoding="utf-8"))
    except Exception: settings = {}
    pdir = os.path.join(dest_root, "plugins")
    def load(n):
        try: return json.load(open(os.path.join(pdir, n), encoding="utf-8"))
        except Exception: return {}
    enabled = [k for k, v in (settings.get("enabledPlugins") or {}).items() if v]
    known = load("known_marketplaces.json")
    added = []
    need = {p.split("@", 1)[1] for p in enabled if "@" in p}
    for name, m in (settings.get("extraKnownMarketplaces") or {}).items():
        if name in known or name not in need: continue
        s = (m or {}).get("source") or {}
        src = s.get("repo") if s.get("source") == "github" else s.get("url") if s.get("source") == "git" else None
        if not src:
            warnings.append(f"marketplace {name} is a {s.get('source')} source on the other computer; its plugins can't be installed here"); continue
        rc, out = sh(["claude", "plugin", "marketplace", "add", src], timeout=300)
        added.append({"marketplace": name, "ok": rc == 0})
        if rc != 0: warnings.append(f"claude plugin marketplace add {src}: {last_line(out)}")
    if added: res["marketplaces_added"] = added
    known = load("known_marketplaces.json")
    inst = (load("installed_plugins.json").get("plugins") or {})
    installed = []
    for p in enabled:
        e = (inst.get(p) or [{}])[0]
        if e.get("installPath") and os.path.isdir(e["installPath"]): continue
        if p.split("@", 1)[-1] not in known: continue
        rc, out = sh(["claude", "plugin", "install", p], timeout=600)
        installed.append({"plugin": p, "ok": rc == 0})
        if rc != 0: warnings.append(f"claude plugin install {p}: {last_line(out)}")
    if installed: res["plugins_installed"] = installed
    # pin to the computer's commit
    mac = {}
    mp = os.path.join(stage, "plugins", "installed_plugins.json")
    try: mac = (json.load(open(mp, encoding="utf-8")).get("plugins") or {})
    except Exception: pass
    data = load("installed_plugins.json"); inst = data.get("plugins") or {}
    pinned, changed = [], False
    for p in enabled:
        me, be = (mac.get(p) or [{}])[0], (inst.get(p) or [None])[0]
        sha = me.get("gitCommitSha")
        if not be or not isinstance(sha, str) or not sha or be.get("gitCommitSha") == sha: continue
        plugin, mkt = p.split("@", 1)
        loc = (known.get(mkt) or {}).get("installLocation")
        if not loc or not os.path.isdir(os.path.join(loc, ".git")):
            warnings.append(f"{p}: box has {be.get('version')}, computer {me.get('version')}; can't pin (marketplace isn't a git checkout)"); continue
        if sh(["git", "-C", loc, "cat-file", "-e", sha + "^{commit}"])[0] != 0:
            sh(["git", "-C", loc, "fetch", "-q", "--unshallow"], timeout=300)
            if sh(["git", "-C", loc, "cat-file", "-e", sha + "^{commit}"])[0] != 0: sh(["git", "-C", loc, "fetch", "-q", "origin", sha], timeout=300)
        rc, mj = sh(["git", "-C", loc, "show", f"{sha}:.claude-plugin/marketplace.json"])
        try: entry = next(x for x in json.loads(mj)["plugins"] if x.get("name") == plugin)
        except Exception:
            warnings.append(f"{p}: commit {sha[:12]} of the computer isn't available on the box; kept {be.get('version')}"); continue
        source = entry.get("source")
        if not isinstance(source, str) or not source.startswith("./"):
            warnings.append(f"{p}: plugin source isn't inside the marketplace repo; kept {be.get('version')} (computer: {me.get('version')})"); continue
        sub = os.path.normpath(source[2:]) if source not in ("./", ".") else ""
        version = me.get("version")
        ver = version if isinstance(version, str) and version != "unknown" else sha[:12]
        dest = os.path.join(pdir, "cache", mkt, plugin, ver)
        if not os.path.isdir(dest):
            os.makedirs(dest, exist_ok=True)
            arch = subprocess.run(["git", "-C", loc, "archive", sha] + ([sub] if sub else []), capture_output=True)
            x = subprocess.run(["tar", "x", "-C", dest] + ([f"--strip-components={sub.count('/') + 1}"] if sub else []), input=arch.stdout, capture_output=True)
            if arch.returncode or x.returncode:
                shutil.rmtree(dest, ignore_errors=True); warnings.append(f"{p}: extracting {sha[:12]} failed; kept {be.get('version')}"); continue
        be.update(version=ver, gitCommitSha=sha, installPath=dest); changed = True
        pinned.append({"plugin": p, "version": ver, "sha": sha[:12]})
    if changed:
        atomic_write(os.path.join(pdir, "installed_plugins.json"), json.dumps(data, indent=2).encode("utf-8"))
    if pinned: res["plugins_pinned"] = pinned
    # hook / statusLine commands whose absolute tool path is missing: use the box's copy of that tool
    fixes = []
    def fix(cmd):
        m = re.match(r"^(['\"]?)(/[^\s'\"]+)\1(?=\s|$)", cmd or "")
        if not m or os.path.exists(m.group(2)) or not m.group(2).startswith(HOME + "/"): return cmd
        alt = shutil.which(os.path.basename(m.group(2)))
        if not alt: return cmd
        fixes.append({"from": m.group(2), "to": alt})
        return alt + cmd[m.end():]
    sl = settings.get("statusLine")
    if isinstance(sl, dict) and sl.get("command"): sl["command"] = fix(sl["command"])
    for groups in (settings.get("hooks") or {}).values():
        for g in groups if isinstance(groups, list) else []:
            for h in (g or {}).get("hooks") or []:
                if isinstance(h, dict) and h.get("command"): h["command"] = fix(h["command"])
    if fixes:
        atomic_write(sp, json.dumps(settings, indent=2).encode("utf-8"))
        res["tool_paths_fixed"] = fixes
    try: cj = json.load(open(claude_global(HOME), encoding="utf-8"))
    except Exception: cj = {}
    miss = [v.get("command") for v in (cj.get("mcpServers") or {}).values() if isinstance(v, dict) and v.get("command") and not which_cmd(v.get("command"))]
    tools = install_uvtools(miss, man, warnings)
    if tools: res["mcp_tools_installed"] = tools
    return res


# ---------------------------------------------------------------- codex
def codex_post(stage, dest_root, man, warnings):
    res = {}
    p = os.path.join(dest_root, "config.toml")
    if not os.path.isfile(p): return res
    try:
        import tomllib
        cfg = tomllib.load(open(p, "rb"))
    except Exception:
        warnings.append("config.toml not parsed: MCP checks skipped (details suppressed)"); return res
    text = open(p, encoding="utf-8").read(); orig = text
    disabled, miss = [], []
    for name, s in (cfg.get("mcp_servers") or {}).items():
        cmd = (s or {}).get("command")
        if not cmd or s.get("enabled") is False or which_cmd(cmd): continue
        if os.path.isabs(os.path.expanduser(cmd)) or "/" in cmd:
            hdr = re.compile(r"(?m)^\[mcp_servers\.(?:\"%s\"|%s)\]\s*$" % (re.escape(name), re.escape(name)))
            m = hdr.search(text)
            if m:
                tail = text[m.end():]
                next_header = re.search(r"(?m)^\[", tail)
                end = m.end() + next_header.start() if next_header else len(text)
                block = text[m.end():end]
                line = "enabled = false  # box: the program isn't on this machine (grok-bot-agents import)"
                if re.search(r"(?m)^[ \t]*enabled[ \t]*=", block):
                    block = re.sub(r"(?m)^[ \t]*enabled[ \t]*=.*$", line, block, count=1)
                else: block = "\n" + line + "\n" + block
                text = text[:m.end()] + block + text[end:]
                disabled.append(name)
        else: miss.append(cmd)
    n = cfg.get("notify")
    if isinstance(n, list) and n and os.path.isabs(str(n[0])) and not os.path.exists(n[0]):
        new = re.sub(r"(?m)^(notify\s*=\s*\[[^\n]*\]\s*)$", r"# box: disabled, the program isn't on this machine (grok-bot-agents import)\n# \1", text, count=1)
        if new != text: text = new; res["notify_disabled"] = True
        else: warnings.append(f"codex notify points at a missing program ({n[0]}); comment it out in config.toml")
    if text != orig:
        atomic_write(p, text.encode("utf-8"))
    if disabled:
        res["mcp_disabled"] = disabled
        warnings.append(f"codex MCP servers disabled on the box (their program exists only on the other computer): {disabled}")
    tools = install_uvtools(miss, man, warnings)
    if tools: res["mcp_tools_installed"] = tools
    return res


def omp_post(stage, dest_root, man, warnings):
    try: mj = json.load(open(os.path.join(dest_root, "mcp.json"), encoding="utf-8"))
    except Exception: return {}
    miss = [v.get("command") for v in (mj.get("mcpServers") or {}).values() if isinstance(v, dict) and v.get("command") and not which_cmd(v.get("command"))]
    tools = install_uvtools(miss, man, warnings)
    return {"mcp_tools_installed": tools} if tools else {}


def run(cli, stage, dest_root, warnings):
    man = read_manifest(os.path.dirname(stage.rstrip("/")))
    fn = {"pi": lambda: pi_post(stage, dest_root, man, warnings), "claude": lambda: claude_post(stage, dest_root, man, warnings),
          "codex": lambda: codex_post(stage, dest_root, man, warnings), "omp": lambda: omp_post(stage, dest_root, man, warnings)}[cli]
    try: res = fn()
    except Exception as e:
        warnings.append(f"post-install steps for {cli} stopped: {type(e).__name__} (details suppressed)"); res = {}
    v = check_version(cli, man, warnings)
    if v: res["version"] = v
    return res
