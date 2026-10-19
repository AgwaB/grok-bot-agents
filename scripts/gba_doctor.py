"""Sanitized offline diagnostics and an explicitly gated live OK verification.

No raw config, CLI output, helper errors, auth data, or live transcripts are
returned. The existing parity/runner helpers retain their ownership guards.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import gba_setup as setup

KEY_NAMES = (
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "CODEX_API_KEY", "DEEPSEEK_API_KEY", "OPENROUTER_API_KEY",
    "XAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY", "GROQ_API_KEY",
    "MISTRAL_API_KEY", "CEREBRAS_API_KEY", "AZURE_OPENAI_API_KEY",
)
OK_PROMPT = "Reply with exactly: OK"


def _key_names(cfg):
    names: set[str] = set(KEY_NAMES)
    if cfg:
        for entry in cfg["agents"].values():
            name = entry.get("key_env")
            if isinstance(name, str) and setup.ENV_NAME.fullmatch(name):
                names.add(name)
    return sorted(names)


def _key_presence(names):
    # Parse assignment names only. Never source .agent-keys or return its values.
    present = {name for name in names if bool(os.environ.get(name))}
    path = setup.home() / ".agent-keys"
    readable = True
    try:
        if path.exists():
            if path.is_symlink() or path.stat().st_size > 1024 * 1024:
                readable = False
            else:
                with path.open(encoding="utf-8", errors="replace") as stream:
                    for line in stream:
                        match = re.match(r"^\s*(?:export\s+)?([A-Z_][A-Z0-9_]*)=", line)
                        if match and match.group(1) in names:
                            present.add(match.group(1))
    except OSError:
        readable = False
    return {n: n in present for n in names}, readable


def _offline_env(names):
    env = setup.clean_env()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Parity only needs the presence of env names. Do not forward their values.
    for name in names:
        if os.environ.get(name):
            env[name] = "present"
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "PI_CODING_AGENT_DIR"):
        if os.environ.get(name):
            env[name] = os.environ[name]
    return env


def _parity(root, selected, names):
    script = root / "skills/import-agents/scripts/parity_check.py"
    if not script.is_file():
        return {"status": "blocked", "reason": "parity_helper_missing", "agents": {}}
    rc, out, _, code = setup.run_captured(
        [sys.executable, str(script), "--cli", ",".join(selected), "--no-run"],
        timeout=30, env=_offline_env(names), cwd=root)
    if code != "ok" or rc not in (0, 1):
        return {"status": "blocked", "reason": "parity_check_failed", "agents": {}}
    try:
        payload = json.loads(out)
        agents = {}
        for agent in selected:
            entry = payload[agent]
            if not isinstance(entry, dict) or type(entry.get("ok")) is not bool:
                raise ValueError
            counts = dict.fromkeys(("breaking", "degraded", "cosmetic"), 0)
            for key in ("gaps", "auth"):
                if not isinstance(entry.get(key, []), list):
                    raise ValueError
                for gap in entry.get(key, []):
                    if not isinstance(gap, dict) or gap.get("impact") not in counts:
                        raise ValueError
                    counts[gap["impact"]] += 1
            agents[agent] = {"ok": entry["ok"] and counts["breaking"] == 0, "counts": counts}
        ok = all(entry["ok"] for entry in agents.values())
        if rc != 0 and ok:
            return {"status": "blocked", "reason": "parity_check_failed", "agents": agents}
        return {"status": "ok" if ok else "blocked", "reason": "parity_ok" if ok else "parity_gaps",
                "agents": agents}
    except Exception:
        return {"status": "blocked", "reason": "parity_result_invalid", "agents": {}}


def _auth_presence():
    home = setup.home()
    paths = {
        "claude": Path(os.environ.get("CLAUDE_CONFIG_DIR") or home / ".claude") / ".credentials.json",
        "codex": Path(os.environ.get("CODEX_HOME") or home / ".codex") / "auth.json",
        "pi": Path(os.environ.get("PI_CODING_AGENT_DIR") or home / ".pi/agent") / "auth.json",
        "omp": home / ".omp/agent/agent.db",
    }
    return {a: p.is_file() for a, p in paths.items()}


def offline(root, selected=None):
    config_state = "ok"
    cfg = None
    try:
        cfg = setup.read_config(root)
        if cfg is None:
            config_state = "config_missing"
    except setup.SetupError as exc:
        config_state = exc.reason
    chosen = selected or (cfg["enabled"] if cfg and cfg["enabled"] else list(setup.AGENTS))
    names = _key_names(cfg)
    presence, readable = _key_presence(names)
    auth = _auth_presence()
    agents = {}
    for agent in chosen:
        executable = setup.find_executable(agent)
        version = setup.probe_version(executable)
        managed = False
        integrity = "not_managed"
        try:
            if setup.managed_state(agent):
                managed, integrity = True, "ok"
        except setup.SetupError:
            managed, integrity = True, "managed_install_invalid"
        agents[agent] = {"installed": bool(executable), "path": setup.location(executable),
                         "version": version["version"], "version_status": version["status"],
                         "enabled": bool(cfg and agent in cfg["enabled"]), "managed": managed,
                         "integrity": integrity, "auth_store_present": auth[agent]}
    runtimes = {}
    for name in ("node", "npm", "bun"):
        executable = setup.find_executable(name)
        runtimes[name] = {"path": setup.location(executable), **setup.probe_version(executable)}
    parity = _parity(root, chosen, names)
    ok = (config_state == "ok" and readable and parity["status"] == "ok"
          and all(a["installed"] and a["version_status"] == "ok"
                  and a["integrity"] in ("ok", "not_managed") for a in agents.values()))
    reason = "diagnostics_ok" if ok else (config_state if config_state != "ok" else "diagnostics_attention")
    return setup.result("ok" if ok else "blocked", reason, None if ok else "repair_agents",
                        mode="offline", selected=chosen, config={"status": config_state}, agents=agents,
                        runtimes=runtimes, keys={"presence": presence, "inventory_readable": readable},
                        parity=parity, live={"requested": False, "reason": "live_not_requested"},
                        path_ready=str(setup.home() / ".local/bin") in os.environ.get("PATH", "").split(os.pathsep),
                        runner_path_ready=True)


def _live_one(root, agent):
    runner = root / "skills/delegate-task/scripts/run-agent.sh"
    if not runner.is_file():
        return {"status": "blocked", "reason": "runner_missing", "quota": "unknown"}
    bash = setup.find_executable("bash")
    if not bash:
        return {"status": "blocked", "reason": "bash_missing", "quota": "unknown"}
    # Only this explicit --live branch forwards credentials. The runner sources
    # the user's key file and retains its normal supervision/worktree guards.
    env = dict(os.environ)
    env["PATH"] = setup.user_path()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    rc, out, _, code = setup.run_captured(
        [bash, str(runner), "--agent", agent, "--read-only", "--timeout", "120", "--prompt", OK_PROMPT],
        timeout=150, env=env, cwd=root)
    if code != "ok":
        return {"status": "blocked", "reason": "live_check_failed", "quota": "unknown"}
    try:
        payload = json.loads(out)
        if not isinstance(payload, dict):
            raise ValueError
    except Exception:
        return {"status": "blocked", "reason": "live_result_invalid", "quota": "unknown"}
    if payload.get("status") == "rate_limited":
        return {"status": "blocked", "reason": "quota_limited", "quota": "limited"}
    summary = payload.get("summary")
    if rc == 0 and payload.get("status") == "ok" and isinstance(summary, str) and summary.strip() == "OK":
        return {"status": "ok", "reason": "live_ok", "quota": "not_limited"}
    return {"status": "blocked", "reason": "live_response_mismatch" if payload.get("status") == "ok"
            else "live_agent_failed", "quota": "unknown"}


def verify(root, selected, live=False):
    report = offline(root, selected)
    report["operation"] = "verify"
    if report["config"]["status"] == "ok" and any(not a["enabled"] for a in report["agents"].values()):
        report.update(status="blocked", reason="agents_not_enabled", next_action="configure_agents")
    if not live:
        return report
    # An invalid config cannot be trusted by the runner, even with --live.
    if report["config"]["status"] != "ok":
        report["live"] = {"requested": True, "reason": "live_prerequisites_missing", "agents": {}}
        return report
    observed = {}
    for a in selected:
        entry = report["agents"][a]
        if not entry["installed"] or not entry["enabled"] or entry["integrity"] == "managed_install_invalid":
            observed[a] = {"status": "blocked", "reason": "live_prerequisites_missing", "quota": "unknown"}
        else:
            observed[a] = _live_one(root, a)
    ok = all(entry["status"] == "ok" for entry in observed.values())
    quota = any(entry["quota"] == "limited" for entry in observed.values())
    report.update(status="ok" if ok and report["status"] == "ok" else "blocked",
                  reason="quota_limited" if quota else ("verification_ok" if ok and report["status"] == "ok" else "verification_attention"),
                  next_action="retry_later" if quota else (None if ok and report["status"] == "ok" else "repair_agents"),
                  mode="live", live={"requested": True, "agents": observed})
    return report


def configure(subparsers):
    doctor = subparsers.add_parser("doctor", help="Offline, name-only diagnostics (always JSON)")
    doctor.add_argument("--json", action="store_true", help="Compatibility flag; output is always JSON")
    doctor.set_defaults(command="doctor", diagnostics_action="doctor", _gba_handler=handle)
    verify_parser = subparsers.add_parser("verify", help="Offline verification; --live opts into a provider OK call")
    verify_parser.add_argument("--agents", required=True, help="Comma-separated claude,codex,pi,omp")
    verify_parser.add_argument("--live", action="store_true", help="Authorize one provider OK request per selected enabled agent")
    verify_parser.set_defaults(command="verify", diagnostics_action="verify", _gba_handler=handle)


def handle(args, root):
    try:
        if setup.platform.system() != "Linux":
            return setup.result("blocked", "unsupported_platform", "diagnose_on_linux_box")
        action = getattr(args, "diagnostics_action", getattr(args, "command", "doctor"))
        if action == "doctor":
            return offline(Path(root))
        if action == "verify":
            return verify(Path(root), setup.selected_agents(getattr(args, "agents", None)), bool(getattr(args, "live", False)))
        return setup.result("error", "invalid_diagnostics_action", "doctor")
    except setup.SetupError as exc:
        return setup.result("blocked", exc.reason, exc.next_action)
    except Exception:
        return setup.result("error", "diagnostics_failed", "doctor")
