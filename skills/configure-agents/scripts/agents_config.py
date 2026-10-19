#!/usr/bin/env python3
"""Central config for grok-bot-agents: ~/.grok-bot-agents/config.json (override: $GROK_BOT_AGENTS_CONFIG).
Falls back to the legacy ~/.grok-agents/config.json / $GROK_AGENTS_CONFIG when the new one is absent.

Commands:
  agents_config.py path
  agents_config.py init [--enable claude,codex,pi,omp] [--force]   create with defaults (detects installed CLIs if --enable omitted)
  agents_config.py show                                           human summary (no secrets; key env names only, with set/unset)
  agents_config.py json                                           print raw JSON
  agents_config.py get <dotted.key>
  agents_config.py set <key> <value> [<key> <value> ...]          values parsed as JSON if possible; validated after all pairs
  agents_config.py enable <agent> | disable <agent>
  agents_config.py validate
Never store secret values here: only env var names (key_env).
model / reasoning: null or "native" = pass no flag, so the CLI uses its own (or imported) native setting.
"""
import json
import math
import os
import re
import shutil
import sys
import tempfile
from collections import Counter

AGENTS = ["claude", "codex", "pi", "omp"]
def _default_cfg():
    for env in ("GROK_BOT_AGENTS_CONFIG", "GROK_AGENTS_CONFIG"):  # GROK_AGENTS_CONFIG: legacy name
        if os.environ.get(env):
            return os.path.expanduser(os.environ[env])
    new, old = os.path.expanduser("~/.grok-bot-agents/config.json"), os.path.expanduser("~/.grok-agents/config.json")
    if not os.path.exists(new) and os.path.exists(old):
        return old  # legacy dir from before the rename
    return new
PATH = _default_cfg()
REASONING = {
    "claude": ["low", "medium", "high", "xhigh", "max"],             # claude --effort (verified --help 2.1.197)
    "codex": ["minimal", "low", "medium", "high", "xhigh"],          # -c model_reasoning_effort (key verified via --strict-config; values model-dependent)
    "pi": ["off", "minimal", "low", "medium", "high", "xhigh", "max"],  # pi --thinking (verified --help 1.3.0)
    "omp": ["off", "minimal", "low", "medium", "high", "xhigh", "max", "auto"],  # omp --thinking (verified --help 18.12.1)
}
AUTH = ["oauth", "api_key", "imported"]
PERMISSION = ["full-auto", "safe"]
ROUTING_MODES = ["auto", "ask", "fixed"]

def default_config(enabled):
    return {
        "version": 1,
        "enabled": enabled,
        "permission_mode": "full-auto",
        "worktree_per_task": True,
        "timeout_sec": 1800,
        "agents": {
            "claude": {"auth": "oauth", "model": None, "reasoning": None},
            "codex": {"auth": "oauth", "model": None, "reasoning": None},
            "pi": {"auth": "api_key", "key_env": "DEEPSEEK_API_KEY", "model": None, "reasoning": None},
            "omp": {"auth": "oauth", "model": None, "reasoning": None},
        },
        "routing": {"mode": "auto", "cheap": ["pi", "omp"], "hard": ["claude", "codex"],
                    "review": "other-frontier", "prefer": [], "fixed": None},
        "runs_dir": "/workspace/agent-runs",
    }

def load():
    if not os.path.exists(PATH):
        return None
    try:
        with open(PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        raise ValueError("config unreadable") from None

def save(cfg):
    errs = validate(cfg)
    if errs:
        sys.exit("invalid config:\n- " + "\n- ".join(errs))
    try:
        os.makedirs(os.path.dirname(PATH), exist_ok=True)
    except OSError:
        raise ValueError("config directory unavailable") from None
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(PATH))
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, 0o644)
    os.replace(tmp, PATH)

def nat(v):
    """null / "native" -> no flag; the CLI's own (or imported) setting applies."""
    return "(native)" if v in (None, "", "native") else v

def secret_looking(value):
    """Conservative heuristic, not a guarantee that arbitrary text is secret-free."""
    if isinstance(value, dict):
        return any(secret_looking(k) or secret_looking(v) for k, v in value.items())
    if isinstance(value, list):
        return any(secret_looking(v) for v in value)
    if not isinstance(value, str):
        return False
    if re.search(r"(?i)(?:\b(?:sk|gsk|ghp|gho|github_pat|xox[baprs])[-_]|\bbearer\s+|-----BEGIN .*PRIVATE KEY|eyJ[\w-]+\.[\w-]+\.)", value):
        return True
    for token in re.findall(r"[A-Za-z0-9+=_-]{32,}", value):
        # Ordinary ALL_CAPS_ENV_NAMES are names, not values. Mixed-case random
        # tokens and long hex keys are rejected even in unknown/nested fields.
        if re.fullmatch(r"[A-Z][A-Z0-9]*(_[A-Z0-9]+)+", token):
            continue
        entropy = -sum((n / len(token)) * math.log2(n / len(token)) for n in Counter(token).values())
        classes = sum(bool(re.search(p, token)) for p in (r"[a-z]", r"[A-Z]", r"[0-9]"))
        if (classes >= 2 and entropy >= 3.5) or (re.fullmatch(r"[a-fA-F0-9]{32,}", token) and entropy >= 3):
            return True
    return False


def validate(cfg):
    # Run before all interpolated diagnostics; a misplaced key must not appear
    # in an error, even when it is a dictionary key or an enabled-agent value.
    if secret_looking(cfg):
        return ["secret-looking value refused; use secure credential entry, not config"]
    e = []
    if cfg.get("version") != 1:
        e.append("version must be 1")
    for a in cfg.get("enabled", []):
        if a not in AGENTS:
            e.append(f"enabled: unknown agent {a}")
    if cfg.get("permission_mode") not in PERMISSION:
        e.append(f"permission_mode must be one of {PERMISSION}")
    if cfg.get("worktree_per_task") is not True:
        e.append("worktree_per_task must stay true")
    if not isinstance(cfg.get("timeout_sec"), int) or cfg["timeout_sec"] <= 0:
        e.append("timeout_sec must be a positive int")
    for a, ac in cfg.get("agents", {}).items():
        if a not in AGENTS:
            e.append(f"agents: unknown agent {a}")
            continue
        if ac.get("auth") not in AUTH:
            e.append(f"agents.{a}.auth must be one of {AUTH}")
        r = ac.get("reasoning")
        if r is not None and r != "native" and r not in REASONING[a]:
            e.append(f"agents.{a}.reasoning must be one of {REASONING[a]}, \"native\" or null")
        for k in ac:
            if k.lower() in ("key", "api_key", "token", "secret", "password"):
                e.append(f"agents.{a}.{k}: secrets are not allowed in config; use key_env")
        if ac.get("key_env") is not None and (not isinstance(ac["key_env"], str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,127}", ac["key_env"])):
            e.append(f"agents.{a}.key_env must be an environment variable name")
        if ac.get("auth") == "api_key" and not ac.get("key_env"):
            e.append(f"agents.{a}.key_env required for api_key auth")
    r = cfg.get("routing", {})
    if r.get("mode") not in ROUTING_MODES:
        e.append(f"routing.mode must be one of {ROUTING_MODES}")
    if r.get("mode") == "fixed" and r.get("fixed") not in AGENTS:
        e.append("routing.fixed must name an agent when mode is fixed")
    return e

def dotted(cfg, key, value=None, set_=False):
    parts = key.split(".")
    cur = cfg
    for p in parts[:-1]:
        cur = cur.setdefault(p, {}) if set_ else cur[p]
    if set_:
        cur[parts[-1]] = value
    return cur[parts[-1]]

USER_BINS = [os.path.expanduser("~/.local/bin"), os.path.expanduser("~/.bun/bin")]

def installed(a):
    # Bot shells may lack the user-level bin dirs on PATH; look there too (same dirs run-agent.sh adds).
    path = os.pathsep.join(USER_BINS + [os.environ.get("PATH", "")])
    return shutil.which(a, path=path) is not None

def show(cfg):
    print(f"config: {PATH}")
    print(f"permission_mode: {cfg['permission_mode']}   worktree_per_task: {cfg['worktree_per_task']}   timeout: {cfg['timeout_sec']}s")
    r = cfg["routing"]
    print(f"routing: mode={r['mode']} prefer={r.get('prefer') or '-'} cheap={r.get('cheap')} hard={r.get('hard')} review={r.get('review')}" + (f" fixed={r.get('fixed')}" if r['mode'] == 'fixed' else ""))
    print(f"{'agent':<7} {'enabled':<8} {'installed':<10} {'auth':<8} {'key_env':<22} {'model':<28} reasoning")
    for a in AGENTS:
        ac = cfg["agents"].get(a, {})
        ke = ac.get("key_env") or "-"
        if ke != "-":
            ke += " (set)" if os.environ.get(ke) else " (unset)"
        print(f"{a:<7} {('yes' if a in cfg['enabled'] else 'no'):<8} {('yes' if installed(a) else 'no'):<10} {ac.get('auth','-'):<8} {ke:<22} {str(nat(ac.get('model'))):<28} {nat(ac.get('reasoning'))}")

def main(argv):
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return
    cmd = argv[0]
    if cmd == "path":
        print(PATH)
        return
    if cmd == "init":
        if os.path.exists(PATH) and "--force" not in argv:
            sys.exit(f"exists: {PATH} (use --force to overwrite)")
        en = [a for a in AGENTS if installed(a)]
        if "--enable" in argv:
            en = [x for x in argv[argv.index("--enable") + 1].split(",") if x]
        save(default_config(en))
        print(f"wrote {PATH}")
        return
    cfg = load()
    if cfg is None:
        sys.exit(f"no config at {PATH}; run: agents_config.py init")
    if secret_looking(cfg):
        sys.exit("secret-looking value in config; output suppressed")
    if cmd == "show":
        show(cfg)
    elif cmd == "json":
        print(json.dumps(cfg, indent=2, ensure_ascii=False))
    elif cmd == "get":
        v = dotted(cfg, argv[1])
        print(json.dumps(v) if not isinstance(v, str) else v)
    elif cmd == "set":
        pairs = argv[1:]
        if not pairs or len(pairs) % 2:
            sys.exit("usage: set <key> <value> [<key> <value> ...]")
        for k, raw in zip(pairs[::2], pairs[1::2], strict=True):
            try:
                val = json.loads(raw)
            except ValueError:
                val = raw
            if secret_looking(k) or secret_looking(val):
                sys.exit("secret-looking value refused; output suppressed")
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*", k):
                sys.exit("invalid field name")
            if any(p.lower() in ("key", "api_key", "token", "secret", "password") for p in k.split(".")):
                sys.exit("secret fields refused; use key_env")
            dotted(cfg, k, val, set_=True)
        save(cfg)  # validated once, after all pairs; never echo any raw value
        print(f"updated {len(pairs) // 2} field(s)")
    elif cmd in ("enable", "disable"):
        a = argv[1]
        if a not in AGENTS:
            sys.exit(f"unknown agent {a}")
        en = [x for x in cfg["enabled"] if x != a]
        if cmd == "enable":
            en.append(a)
        cfg["enabled"] = [x for x in AGENTS if x in en]
        save(cfg)
        print(f"enabled: {cfg['enabled']}")
    elif cmd == "validate":
        errs = validate(cfg)
        print("ok" if not errs else "\n".join(errs))
        sys.exit(1 if errs else 0)
    else:
        sys.exit(__doc__)

if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except (ValueError, KeyError, TypeError, AttributeError, IndexError, OSError):
        sys.exit("config operation failed; details suppressed")
