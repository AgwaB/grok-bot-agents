"""Explicit wake test; transport credentials never cross the JSON boundary."""
import argparse

import gba_runs


def configure(subparsers):
    wake = subparsers.add_parser("wake", help="Test the configured completion webhook")
    commands = wake.add_subparsers(dest="wake_command", required=True)
    test = commands.add_parser("test")
    test.add_argument("--json", action="store_true", help=argparse.SUPPRESS)


def handle(args, root):
    transport = gba_runs.lifecycle(root).wake
    delivery = transport.send_bounded({"event": "wake_test", "sent_at": transport.utc_now()})
    ok = delivery["outcome"] == "sent"
    return {"status": "ok" if ok else "error", "reason": "wake_test_ok" if ok else "wake_test_failed",
            "next_action": None if ok else "check_wake_config", "requires_approval": False, "wake": delivery}
