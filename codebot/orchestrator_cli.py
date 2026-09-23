"""CLI argument parsing and command handling for the orchestrator.

Extracted from orchestrator.py to reduce its size and separate
user-facing CLI concerns from core orchestration logic.
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments."""
    p = argparse.ArgumentParser(description="Bot Orchestrator")
    p.add_argument("--status", action="store_true")
    p.add_argument("--stop-all", action="store_true")
    p.add_argument("--safe-stop", action="store_true")
    p.add_argument("--drain", action="store_true")
    p.add_argument("--clear-drain", action="store_true")
    p.add_argument("--drain-status", action="store_true")
    p.add_argument("--start", nargs="*")
    p.add_argument("--check-interval", type=int, default=10)
    return p.parse_args()


def handle_cli_commands(
    args: argparse.Namespace,
    bots: dict,
    *,
    print_status_fn: Any,
    is_draining_fn: Any,
    drain_status_fn: Any,
    clear_drain_fn: Any,
    safe_stop_all_fn: Any,
    stop_bot_fn: Any,
    start_bot_fn: Any,
) -> bool:
    """Handle CLI commands. Returns True if a command was handled (should exit)."""
    if args.status:
        print_status_fn(bots)
        if is_draining_fn():
            print(f"DRAIN ACTIVE: {drain_status_fn()}")
        return True
    if args.drain_status:
        import pprint
        pprint.pprint(drain_status_fn())
        print_status_fn(bots)
        return True
    if args.clear_drain:
        clear_drain_fn()
        print("Drain cleared.")
        return True
    if args.safe_stop or args.drain:
        safe_stop_all_fn(bots, stop_bot_fn)
        print("Safe stop complete.")
        print_status_fn(bots)
        return True
    if args.stop_all:
        for b in bots.values():
            stop_bot_fn(b, "stop-all")
        return True
    if args.start is not None:
        if is_draining_fn():
            print("Refusing --start while draining", file=sys.stderr)
            sys.exit(4)
        for n in args.start or [b.config.name for b in bots.values()]:
            if n in bots and bots[n].config.enabled:
                start_bot_fn(bots[n])
        if args.start:
            time.sleep(2)
            print_status_fn(bots)
        return True
    return False
