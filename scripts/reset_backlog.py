#!/usr/bin/env python3
"""Reset all non-COMPLETE tickets back to DISCOVERED.

Bypasses the forward-only state machine by mutating TicketStore internals
directly (_tickets dict + _state_index). This is intentional: the current
TRANSITIONS graph has no reverse paths to DISCOVERED from most states,
and terminal states (REJECTED, DUPLICATE) have zero exits.

Reads tickets.json, mutates in-memory store, flushes via store.close().
Creates a timestamped backup before any mutation.

Dry-run by default. Pass --execute to apply.

Usage:
  python scripts/reset_backlog.py                          # dry-run
  python scripts/reset_backlog.py --execute                # apply reset
  python scripts/reset_backlog.py --state-dir /path        # custom dir
  python scripts/reset_backlog.py --exclude-states REJECTED,DUPLICATE  # skip terminals
"""

import argparse
import json
import shutil
import sys
import time
from pathlib import Path

DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".codebot" / "state"
DEFAULT_TICKETS_FILE = DEFAULT_STATE_DIR / "tickets.json"
ACTOR = "backlog-reset"


def load_raw_tickets(tickets_file: Path) -> list[dict]:
    if not tickets_file.exists():
        print(f"Error: tickets file not found: {tickets_file}", file=sys.stderr)
        sys.exit(2)
    try:
        data = json.loads(tickets_file.read_text(encoding="utf-8"))
        return data.get("tickets", [])
    except (json.JSONDecodeError, OSError) as e:
        print(f"Error: failed to load tickets: {e}", file=sys.stderr)
        sys.exit(2)


def create_backup(tickets_file: Path) -> Path:
    ts = int(time.time())
    backup_path = tickets_file.with_suffix(f".backup.{ts}.json")
    shutil.copy2(tickets_file, backup_path)
    return backup_path


def run_dry_run(tickets: list[dict], exclude_states: set[str]) -> None:
    from collections import Counter

    resettable = []
    excluded = []
    untouched = []

    for t in tickets:
        state = t.get("state", "")
        tid = t.get("id", "<unknown>")
        if state == "COMPLETE":
            untouched.append(t)
        elif state in exclude_states:
            excluded.append(t)
        elif state == "DISCOVERED":
            pass
        else:
            resettable.append(t)

    state_counts = Counter(t.get("state") for t in resettable)

    print(f"DRY RUN — backlog reset plan:\n")
    print(f"  Total tickets:          {len(tickets)}")
    print(f"  COMPLETE (untouched):   {len(untouched)}")
    print(f"  Already DISCOVERED:     {sum(1 for t in tickets if t.get('state') == 'DISCOVERED')}")
    print(f"  Excluded states:        {len(excluded)} ({', '.join(exclude_states) if exclude_states else 'none'})")
    print(f"  To reset → DISCOVERED:  {len(resettable)}")
    print()

    if state_counts:
        print("  Breakdown of tickets to reset:")
        for state, count in state_counts.most_common():
            print(f"    {state}: {count}")

    if excluded:
        ex_counts = Counter(t.get("state") for t in excluded)
        print(f"\n  Excluded (will NOT be reset):")
        for state, count in ex_counts.most_common():
            print(f"    {state}: {count}")

    print(f"\nRun with --execute to apply. A backup will be created automatically.")


def run_execute(tickets_file: Path, exclude_states: set[str]) -> None:
    from codebot.ticket_engine import TicketStore, TicketState, Ticket

    backup_path = create_backup(tickets_file)
    print(f"Backup created: {backup_path}")

    store = TicketStore(tickets_file)

    target_state = TicketState.DISCOVERED
    reset_count = 0
    skip_count = 0
    error_count = 0
    errors = []

    with store._lock:
        ticket_ids = list(store._tickets.keys())
        for tid in ticket_ids:
            ticket = store._tickets[tid]
            current_state = ticket.state

            if current_state == target_state:
                continue
            if current_state == TicketState.COMPLETE:
                skip_count += 1
                continue
            if current_state.value in exclude_states:
                skip_count += 1
                continue

            old_state = current_state

            try:
                updated_dict = ticket.to_dict()
                updated_dict["state"] = target_state.value
                updated_dict["updated_at"] = time.time()
                new_ticket = Ticket.from_dict(updated_dict)

                if old_state in store._state_index:
                    store._state_index[old_state].discard(tid)
                    if not store._state_index[old_state]:
                        del store._state_index[old_state]
                        store._state_counts.pop(old_state.value, None)
                    else:
                        store._state_counts[old_state.value] = len(store._state_index[old_state])

                store._tickets[tid] = new_ticket
                store._state_index.setdefault(target_state, set()).add(tid)
                store._state_counts[target_state.value] = len(store._state_index[target_state])
                store._dirty_ids.add(tid)

                event = {
                    "ticket_id": tid,
                    "from_state": old_state.value,
                    "to_state": target_state.value,
                    "timestamp": new_ticket.updated_at,
                    "attempts": new_ticket.attempts,
                    "rework_count": new_ticket.rework_count,
                    "queue_age_seconds": 0.0,
                    "actor": ACTOR,
                    "revision": new_ticket.updated_at,
                }
                store._lifecycle_event_buffer.append(event)

                reset_count += 1
            except Exception as e:
                error_count += 1
                errors.append({"ticket_id": tid, "error": str(e)})

    try:
        store.close()
    except Exception as e:
        print(f"WARNING: store close failed: {e}", file=sys.stderr)

    print(f"\nReset complete:")
    print(f"  Tickets reset → DISCOVERED: {reset_count}")
    print(f"  Tickets skipped:            {skip_count}")
    print(f"  Errors:                     {error_count}")
    if errors:
        print(f"\n  Failed tickets:")
        for err in errors[:20]:
            print(f"    {err['ticket_id']}: {err['error']}")
        if len(errors) > 20:
            print(f"    ... and {len(errors) - 20} more")

    if error_count > 0 and reset_count == 0:
        print(f"\nAll resets failed. Restore from backup: {backup_path}")
        sys.exit(1)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset all non-COMPLETE tickets back to DISCOVERED for new lifecycle."
    )
    parser.add_argument("--state-dir", type=Path, default=None)
    parser.add_argument("--tickets-file", type=Path, default=None)
    parser.add_argument("--execute", action="store_true", help="Apply reset (default: dry-run)")
    parser.add_argument(
        "--exclude-states",
        type=str,
        default="",
        help="Comma-separated states to exclude from reset (e.g., REJECTED,DUPLICATE)",
    )
    args = parser.parse_args()

    if args.tickets_file:
        tickets_file = args.tickets_file
    elif args.state_dir:
        tickets_file = args.state_dir / "tickets.json"
    else:
        tickets_file = DEFAULT_TICKETS_FILE

    exclude_states = set()
    if args.exclude_states:
        exclude_states = {s.strip().upper() for s in args.exclude_states.split(",") if s.strip()}

    tickets = load_raw_tickets(tickets_file)

    if args.execute:
        run_execute(tickets_file, exclude_states)
    else:
        run_dry_run(tickets, exclude_states)


if __name__ == "__main__":
    main()
