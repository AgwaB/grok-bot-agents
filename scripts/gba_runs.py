"""Bounded bot-only run decisions. Never relay provider text or helper diagnostics."""
import argparse
import importlib.util
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}\Z")
RUN_STATES = {"ok", "error", "timeout", "rate_limited"}
ERROR_KINDS = {"conn_error", "incomplete_output", "auth_error", "rate_limited", "timeout"}


def configure(subparsers):
    runs = subparsers.add_parser("runs", help="Inspect and explicitly recover delegated runs")
    commands = runs.add_subparsers(dest="runs_command", required=True)
    listing = commands.add_parser("list")
    listing.add_argument("--open", action="store_true", dest="open_only")
    listing.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    next_cmd = commands.add_parser("next")
    next_cmd.add_argument("id")
    flags = next_cmd.add_mutually_exclusive_group()
    flags.add_argument("--handoff", action="store_true", help="Persist pending supervisor handoff")
    flags.add_argument("--supervisor-started", action="store_true", help="Acknowledge supervisor dispatch")
    flags.add_argument("--rotate", action="store_true", help="Persist a fresh supervisor generation")
    next_cmd.add_argument("--json", action="store_true", help=argparse.SUPPRESS)
    recover = commands.add_parser("recover", help="Explicit mutation: checkpoint and launch a continuation")
    recover.add_argument("id")
    recover.add_argument("--native", action="store_true")
    recover.add_argument("--kill", action="store_true")
    recover.add_argument("--dry-run", action="store_true")
    recover.add_argument("--allow-extra-restart", action="store_true",
                         help="Use only with explicit user approval beyond two restarts")
    recover.add_argument("--new-id")
    recover.add_argument("--json", action="store_true", help=argparse.SUPPRESS)


def result(status, reason, action=None, approval=False, **metadata) -> dict:
    return dict(status=status, reason=reason, next_action=action,
                requires_approval=approval, **metadata)


def load(path):
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 1024 * 1024:
        raise ValueError("invalid metadata")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise ValueError("invalid metadata") from None
    if not isinstance(value, dict):
        raise ValueError("invalid metadata")
    return value


def runs_root():
    override = os.environ.get("AGENT_RUNS_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    config = os.environ.get("GROK_BOT_AGENTS_CONFIG") or os.environ.get("GROK_AGENTS_CONFIG")
    if not config:
        home = Path.home()
        default, legacy = home / ".grok-bot-agents/config.json", home / ".grok-agents/config.json"
        config = default if default.exists() or not legacy.exists() else legacy
    value = load(Path(config)).get("runs_dir") or "/workspace/agent-runs"
    if not isinstance(value, str):
        raise ValueError("invalid config")
    return Path(value).expanduser().resolve()


def lifecycle(root):
    path = root / "skills/delegate-task/scripts/run_lifecycle.py"
    spec = importlib.util.spec_from_file_location("_gba_run_lifecycle", path)
    if spec is None or spec.loader is None:
        raise ValueError("lifecycle unavailable")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run_path(base, run_id):
    if not isinstance(run_id, str) or not SAFE_ID.fullmatch(run_id):
        return None
    run = base / run_id
    if run.is_symlink() or not run.is_dir() or run.resolve().parent != base:
        return None
    return run


def snapshot(run, module):
    meta = load(run / "meta.json")
    state = meta.get("status")
    state = state if state in RUN_STATES else "unknown"
    info = module.lineage(run)
    launch = load(run / "launch.json")
    timeout = launch.get("timeout_sec")
    agent = meta.get("agent") or launch.get("agent")
    return {"id": run.name, "run_status": state,
            "agent": agent if agent in {"pi", "omp", "codex", "claude"} else None,
            "root_id": info["root_id"], "restarts": len(info["attempts"]),
            "timeout_sec": timeout if type(timeout) is int and timeout > 0 else None,
            "error_kind": meta.get("error_kind") if meta.get("error_kind") in ERROR_KINDS else None}


def command(*args):
    return ["${GBA_ROOT}/scripts/gba", "runs", *args]


def supervise_command(run_id, fresh=False, short=False):
    args = ["bash", "${GBA_ROOT}/skills/delegate-task/scripts/supervise.sh", run_id, "1800"]
    if fresh:
        args.append("--new")
    return args + ["--slice", "0" if short else "600"]


def observe(run, root, fresh=False):
    # Slice zero performs one observation, not the helper's ten-minute wait.
    # Captured output is bounded at read time and only fixed tokens are consumed.
    with tempfile.TemporaryFile() as output:
        cmd = ["bash", str(root / "skills/delegate-task/scripts/supervise.sh"),
               str(run), "1800", "--slice", "0"]
        if fresh:
            cmd.append("--new")
        proc = subprocess.run(cmd,
                              stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.DEVNULL,
                              timeout=12, check=False)
        output.seek(0)
        text = output.read(16384).decode("utf-8", "replace")
    if proc.returncode not in (0, 1, 2):
        return {}
    # run_dir can contain spaces; never split and echo it or trust arbitrary reasons.
    fields = {}
    for key in ("status", "next", "reason", "resumable"):
        found = re.search(r"(?:^| )" + key + r"=([^\s]+)", text)
        fields[key] = found[1] if found else None
    return fields


def decide(args, run, root, module):
    details = snapshot(run, module)
    handoff = load(run / "supervisor.handoff.json")
    generation = handoff.get("generation", 0)
    if type(generation) is not int or not 0 <= generation < 1000000:
        return result("blocked", "invalid_handoff")
    if args.handoff or args.rotate or args.supervisor_started:
        if args.supervisor_started and handoff.get("state") != "pending":
            return result("blocked", "handoff_not_pending")
        if args.handoff and handoff.get("state") == "active":
            return result("blocked", "supervisor_already_active")
        if args.rotate or not generation:
            generation += 1
        handoff = {"schema": 1, "state": "active" if args.supervisor_started else "pending",
                   "generation": generation, "updated_at": int(time.time())}
        module.write_json(run / "supervisor.handoff.json", handoff)
    observation = observe(run, root, fresh=args.rotate)
    if not observation:
        return result("blocked", "supervision_unavailable", **details)
    state = observation["status"]
    details["supervisor_generation"] = generation
    if state == "done":
        return result("ok", "run_complete", "review_result", **details)
    if observation["resumable"] == "1":
        capped = details["restarts"] >= module.AUTOMATIC_RESTARTS
        recover = command("recover", run.name)
        if state == "stalled":
            recover.append("--kill")
        if capped:
            recover.append("--allow-extra-restart")
        return result("blocked" if capped else "ok",
                      "restart_approval_required" if capped else "run_recoverable",
                      "request_approval" if capped else "recover", capped,
                      recover_command=recover, **details)
    if state == "failed":
        # No guessing credentials, native identity or transport errors from text.
        return result("blocked", "run_failed_not_recoverable", "review_result", **details)
    if state != "running":
        return result("blocked", "supervision_unavailable", **details)
    if observation["reason"] == "budget_used":
        return result("ok", "supervisor_budget_used", "rotate_supervisor",
                      rotate_command=command("next", run.name, "--rotate"), **details)
    pending = handoff.get("state") == "pending"
    return result("ok", "supervisor_handoff_pending" if pending else "run_running",
                  "dispatch_supervisor" if pending else "supervise",
                  supervisor_command=supervise_command(run.name, fresh=pending),
                  handoff_command=command("next", run.name, "--handoff"),
                  acknowledge_command=command("next", run.name, "--supervisor-started"),
                  **details)


def parse_error(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    return value.get("error") if isinstance(value, dict) else None


def recover(args, run, root, module):
    try:
        planned = module.plan(run, args.new_id, args.allow_extra_restart)
    except module.LifecycleError as error:
        code = str(error)
        return result("blocked", code, "request_approval" if code == "restart_approval_required" else None,
                      code == "restart_approval_required")
    cmd = ["bash", str(root / "skills/delegate-task/scripts/resume-run.sh"), str(run)]
    for flag in ("native", "kill", "dry_run", "allow_extra_restart"):
        if getattr(args, flag):
            cmd.append("--" + flag.replace("_", "-"))
    if args.new_id:
        cmd += ["--new-id", args.new_id]
    # No shell, no argument/exception text in results. Legacy helpers can print
    # prompts/paths/errors: discard stderr and reduce stdout to known error codes.
    with tempfile.TemporaryFile() as output:
        proc = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=output,
                              stderr=subprocess.DEVNULL, check=False)
        output.seek(0)
        raw = output.read(16384)
    helper_error = parse_error(raw)
    known = {"restart_approval_required", "run_id_exists", "invalid_run_id", "invalid_run_lineage",
             "invalid_run_metadata", "run_inventory_limit", "lifecycle_unavailable"}
    if helper_error in known:
        capped = helper_error == "restart_approval_required"
        return result("blocked", helper_error, "request_approval" if capped else None, capped)
    if proc.returncode == 5:
        return result("blocked", "run_owned", "recover", id=run.name, kill_required=True)
    if args.dry_run:
        return result("ok" if proc.returncode == 0 else "blocked",
                      "recovery_dry_run" if proc.returncode == 0 else "recovery_refused",
                      id=run.name, new_id=planned["id"], attempt=planned["attempt"], mutated=False)
    if proc.returncode not in (0, 1):
        return result("blocked", "recovery_refused", id=run.name)
    # Native startup rejection can consume a second reserved attempt. Select the
    # latest ledger receipt, not the originally planned destination.
    attempts = module.lineage(run)["attempts"]
    new_id = attempts[-1]["id"] if attempts else planned["id"]
    target = run_path(run.parent, new_id)
    if target is None:
        return result("blocked", "recovery_refused", id=run.name)
    details = snapshot(target, module)
    if details["root_id"] != planned["root_id"]:
        return result("blocked", "run_lineage_conflict")
    return result("ok" if details["run_status"] == "ok" else "blocked",
                  "recovery_complete" if details["run_status"] == "ok" else "recovery_incomplete",
                  "review_result" if details["run_status"] == "ok" else "supervise", **details)


def handle(args, root: Path):
    try:
        base = runs_root()
        module = lifecycle(Path(root))
        if args.runs_command == "list":
            if not base.exists():
                return result("ok", "runs_listed", runs=[], truncated=False)
            entries = sorted(p for p in base.iterdir() if SAFE_ID.fullmatch(p.name) and p.is_dir() and not p.is_symlink())
            items = []
            for run in entries[:200]:
                details = snapshot(run, module)
                if not args.open_only or details["run_status"] != "ok":
                    items.append(details)
            return result("ok", "runs_listed", runs=items, truncated=len(entries) > 200)
        run = run_path(base, args.id)
        if run is None:
            return result("blocked", "run_not_found")
        if args.runs_command == "next":
            # Old IDs must not supervise stale terminal metadata while their
            # continuation is live. Newest reservations define the lineage head.
            info = module.lineage(run)
            attempts = info["attempts"]
            latest = run_path(base, attempts[-1]["id"]) if attempts else None
            if latest is not None and module.root_run(latest).name != info["root_id"]:
                return result("blocked", "run_lineage_conflict")
            decision = decide(args, latest or run, Path(root), module)
            if latest is not None and latest.name != run.name:
                decision["requested_id"] = run.name
            return decision
        if args.runs_command == "recover":
            return recover(args, run, Path(root), module)
        return result("error", "invalid_runs_command")
    except subprocess.TimeoutExpired:
        return result("blocked", "supervision_timeout")
    except Exception:
        # Metadata, filesystem and legacy helper failures are never an excuse to
        # echo auth, arbitrary JSON values, exception strings or command argv.
        return result("blocked", "run_state_unavailable")
