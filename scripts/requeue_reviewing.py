#!/usr/bin/env python3
"""Transition all REVIEWING tickets back to IMPLEMENTING via valid state machine paths.

Path: REVIEWING → REWORK → IMPLEMENTATION_READY → IMPLEMENTING

Dry-run by default. Use --execute to apply transitions.
"""

import argparse
import sys
from pathlib import Path

from codebot.ticket_engine import TicketState, TicketStore

STEPS = [
    TicketState.REWORK,
    TicketState.IMPLEMENTATION_READY,
    TicketState.IMPLEMENTING,
]
ACTOR = "requeue-script"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Move REVIEWING tickets back to IMPLEMENTING."
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".codebot/state"),
        help="Path to state directory (default: .codebot/state)",
    )
    parser.add_argument(
        "--execute",
        action="store_true",
        default=False,
        help="Apply transitions (default: dry-run, no mutations)",
    )
    parser.add_argument(
        "--ticket-ids",
        type=str,
        default=None,
        help="Comma-separated ticket IDs to process (default: all REVIEWING tickets)",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_dir: Path = args.state_dir
    tickets_path = state_dir / "tickets.json"

    if not tickets_path.exists():
        print(f"ERROR: tickets file not found: {tickets_path}", file=sys.stderr)
        return 1

    store = TicketStore(tickets_path)

    # Select tickets to process
    all_reviewing = store.list_by_state(TicketState.REVIEWING)
    if args.ticket_ids:
        ids = {tid.strip() for tid in args.ticket_ids.split(",") if tid.strip()}
        reviewing_tickets = [t for t in all_reviewing if t.id in ids]
        missing = ids - {t.id for t in reviewing_tickets}
        for tid in missing:
            print(f"WARNING: {tid} not in REVIEWING state, skipping")
    else:
        reviewing_tickets = all_reviewing

    if not reviewing_tickets:
        print("No REVIEWING tickets found.")
        return 0

    mode = "EXECUTE" if args.execute else "DRY-RUN"
    print(f"[{mode}] Found {len(reviewing_tickets)} REVIEWING ticket(s)\n")

    processed = 0
    transitions_applied = 0
    errors = 0

    for ticket in reviewing_tickets:
        print(f"--- {ticket.id} ---")
        print(f"  Title: {ticket.title[:80]}")
        print(f"  State: {ticket.state.value}")
        print(f"  Rework count: {ticket.rework_count}")
        print(f"  Attempts: {ticket.attempts}")

        if args.execute:
            for step in STEPS:
                try:
                    store.transition(ticket.id, step, actor=ACTOR)
                    transitions_applied += 1
                    print(f"  -> {step.value}")
                except Exception as exc:
                    errors += 1
                    print(f"  ERROR transitioning to {step.value}: {exc}")
                    break  # stop this ticket, continue to next
        else:
            path = " → ".join(s.value for s in STEPS)
            print(f"  Planned: REVIEWING → {path}")

        processed += 1
        print()

    try:
        store.close()
    except Exception as exc:
        print(f"WARNING: store close failed: {exc}", file=sys.stderr)

    unit = "applied" if args.execute else "would be applied"
    print(f"Done: {processed} ticket(s) processed, {transitions_applied} transition(s) {unit}")
    if errors:
        print(f"  ({errors} error(s) occurred)")

    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
