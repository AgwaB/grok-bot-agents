"""Static diagnostics, opt-in version probes, and opt-in live OK verification.

Default doctor/verify never start processes or write files. Version probes may
have CLI side effects and require --probe-versions; provider calls require --live.
No raw config, CLI output, helper errors, auth data, or transcripts are returned.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
import re
import runpy
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

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


def _omp_auth(helper, names):
    # The legacy omp auth check copies the DB to a temporary directory. Reuse
    # its config-reference check without that writing branch, then inspect only
    # a stable DB with SQLite's immutable, read-only mode (no locks/sidecars).
    directory = setup.home() / ".omp/agent"
    gaps = []
    models = directory / "models.yml"
    text = models.read_text(encoding="utf-8") if models.is_file() else ""
    stack = []
    for line in text.splitlines():
        match = re.match(r"^(\s*)([\"']?)([A-Za-z0-9_.@-]+)\2\s*:\s*(.*)$", line)
        if not match:
            continue
        indent = len(match.group(1))
        while stack and stack[-1][0] >= indent:
            stack.pop()
        path = [key for _, key in stack]
        if match.group(3) == "apiKey" and len(path) == 2 and path[0] == "providers":
            value = re.sub(r"\s+#.*$", "", match.group(4)).strip().strip("\"'")
            gaps += helper["ref_gaps"](value, "omp models.yml", names)
        stack.append((indent, match.group(3)))
    db = directory / "agent.db"
    if db.is_file():
        wal = directory / "agent.db-wal"
        if wal.exists() and wal.stat().st_size:
            # immutable mode ignores WAL; never report stale auth as verified.
            gaps.append({"impact": "degraded", "item": "omp auth", "detail": "WAL auth status not checked"})
        else:
            connection = sqlite3.connect(db.as_uri() + "?mode=ro&immutable=1", uri=True)
            try:
                rows = connection.execute("select provider, disabled_cause is null from auth_credentials").fetchall()
            finally:
                connection.close()
            enabled = {row[0] for row in rows if row[1]}
            for _provider in {row[0] for row in rows} - enabled:
                gaps.append({"impact": "degraded", "item": "omp auth", "detail": "disabled login"})
            for provider in enabled:
                if re.search(r"anthropic|claude", provider, re.I):
                    gaps.append({"impact": "cosmetic", "item": "omp auth", "detail": "subscription OAuth ToS risk"})
    return gaps


def _static_parity_helper(script):
    # Load the trusted bundled helper in-process, suppressing import bytecode.
    # Its --no-run checks are static except for the legacy omp DB-copy branch.
    old_path, old_bytecode = sys.path[:], sys.dont_write_bytecode
    try:
        sys.path.insert(0, str(script.parent))
        sys.dont_write_bytecode = True
        helper = runpy.run_path(str(script), run_name="gba_static_parity")["check"].__globals__
        helper["HOME"] = str(setup.home())
        helper["shutil"] = SimpleNamespace(which=setup.find_executable)
        helper["auth_omp"] = lambda names: _omp_auth(helper, names)
        return helper
    finally:
        sys.path[:] = old_path
        sys.dont_write_bytecode = old_bytecode


def _parity(root, selected):
    script = root / "skills/import-agents/scripts/parity_check.py"
    if not script.is_file():
        return {"status": "blocked", "reason": "parity_helper_missing", "agents": {}}
    try:
        # Legacy readers can emit warnings containing private paths. Preserve
        # the previous captured-output boundary without starting a subprocess.
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            helper = _static_parity_helper(script)
            payload = {agent: helper["check"](agent, no_run=True) for agent in selected}
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


def _managed_integrity(agent):
    try:
        if setup.managed_state(agent):
            return True, "ok"
        # An incomplete managed install must not become an external executable
        # just because its receipt disappeared.
        if (setup.base_dir() / "agents" / agent).exists():
            return True, "managed_install_invalid"
        wrapper = setup.home() / ".local/bin" / agent
        if wrapper.is_file():
            with wrapper.open("rb") as stream:
                if b"# gba-managed-v1 " in stream.read(256):
                    return True, "managed_install_invalid"
        return False, "not_managed"
    except (setup.SetupError, OSError):
        return True, "managed_install_invalid"


def _version(executable, requested, integrity="not_managed"):
    if not requested:
        return {"status": "not_requested", "version": None}
    if integrity == "managed_install_invalid":
        return {"status": integrity, "version": None}
    return setup.probe_version(executable)


def offline(root, selected=None, probe_versions=False):
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
        # Validate BOTH the wrapper and payload before even --version.
        managed, integrity = _managed_integrity(agent)
        version = _version(executable, probe_versions, integrity)
        agents[agent] = {"installed": bool(executable), "path": setup.location(executable),
                         "version": version["version"], "version_status": version["status"],
                         "enabled": bool(cfg and agent in cfg["enabled"]), "managed": managed,
                         "integrity": integrity, "auth_store_present": auth[agent]}
    runtimes = {}
    for name in ("node", "npm", "bun"):
        executable = setup.find_executable(name)
        runtimes[name] = {"path": setup.location(executable), **_version(executable, probe_versions)}
    parity = _parity(root, chosen)
    ok = (config_state == "ok" and readable and parity["status"] == "ok"
          and all(a["installed"] and (not probe_versions or a["version_status"] == "ok")
                  and a["integrity"] in ("ok", "not_managed") for a in agents.values()))
    reason = "diagnostics_ok" if ok else (config_state if config_state != "ok" else "diagnostics_attention")
    return setup.result("ok" if ok else "blocked", reason, None if ok else "repair_agents",
                        mode="offline", selected=chosen, config={"status": config_state}, agents=agents,
                        version_probes={"requested": probe_versions}, runtimes=runtimes,
                        keys={"presence": presence, "inventory_readable": readable},
                        parity=parity, live={"requested": False, "reason": "live_not_requested"},
                        path_ready=str(setup.home() / ".local/bin") in os.environ.get("PATH", "").split(os.pathsep),
                        runner_path_ready=True)


def _live_one(root, agent):
    # Recheck at the execution boundary, including changes since diagnostics or
    # an explicitly requested version probe. --live never bypasses integrity.
    if _managed_integrity(agent)[1] == "managed_install_invalid":
        return {"status": "blocked", "reason": "managed_install_invalid", "quota": "unknown"}
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


def verify(root, selected, live=False, probe_versions=False):
    report = offline(root, selected, probe_versions)
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
        if entry["integrity"] == "managed_install_invalid":
            observed[a] = {"status": "blocked", "reason": "managed_install_invalid", "quota": "unknown"}
        elif not entry["installed"] or not entry["enabled"]:
            observed[a] = {"status": "blocked", "reason": "live_prerequisites_missing", "quota": "unknown"}
        else:
            observed[a] = _live_one(root, a)
            if observed[a]["reason"] == "managed_install_invalid":
                entry["integrity"] = "managed_install_invalid"
    ok = all(entry["status"] == "ok" for entry in observed.values())
    quota = any(entry["quota"] == "limited" for entry in observed.values())
    report.update(status="ok" if ok and report["status"] == "ok" else "blocked",
                  reason="quota_limited" if quota else ("verification_ok" if ok and report["status"] == "ok" else "verification_attention"),
                  next_action="retry_later" if quota else (None if ok and report["status"] == "ok" else "repair_agents"),
                  mode="live", live={"requested": True, "agents": observed})
    return report


def configure(subparsers):
    doctor = subparsers.add_parser("doctor", help="Static, read-only diagnostics by default (always JSON)")
    doctor.add_argument("--json", action="store_true", help="Compatibility flag; output is always JSON")
    doctor.add_argument("--probe-versions", action="store_true", help="Authorize agent/runtime --version execution (may write files)")
    doctor.set_defaults(command="doctor", diagnostics_action="doctor", _gba_handler=handle)
    verify_parser = subparsers.add_parser("verify", help="Static verification; --live opts into a provider OK call")
    verify_parser.add_argument("--agents", required=True, help="Comma-separated claude,codex,pi,omp")
    verify_parser.add_argument("--live", action="store_true", help="Authorize one provider OK request per selected enabled agent")
    verify_parser.add_argument("--probe-versions", action="store_true", help="Authorize agent/runtime --version execution (may write files)")
    verify_parser.set_defaults(command="verify", diagnostics_action="verify", _gba_handler=handle)


def handle(args, root):
    try:
        if setup.platform.system() != "Linux":
            return setup.result("blocked", "unsupported_platform", "diagnose_on_linux_box")
        action = getattr(args, "diagnostics_action", getattr(args, "command", "doctor"))
        probe_versions = bool(getattr(args, "probe_versions", False))
        if action == "doctor":
            return offline(Path(root), probe_versions=probe_versions)
        if action == "verify":
            return verify(Path(root), setup.selected_agents(getattr(args, "agents", None)),
                          bool(getattr(args, "live", False)), probe_versions)
        return setup.result("error", "invalid_diagnostics_action", "doctor")
    except setup.SetupError as exc:
        return setup.result("blocked", exc.reason, exc.next_action)
    except Exception:
        return setup.result("error", "diagnostics_failed", "doctor")
