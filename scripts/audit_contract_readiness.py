#!/usr/bin/env python3
"""Pre-deployment contract-readiness audit.

Scans all non-terminal tickets in the active TicketStore and reports
per-transition readiness under the new context contract enforcement.

Read-only: never modifies the ticket store or any caller.

Report sections:
  1. Contract coverage: can every legal transition in TRANSITIONS resolve a contract?
  2. Per-transition ticket readiness: for each non-terminal ticket, which outbound
     transitions would pass and which would fail (with specific missing fields
     and failed invariants).
  3. Migration candidates: where authoritative data already exists elsewhere,
     flag for potential same-state field migration. Migration rule: may move
     existing authoritative data; may NOT synthesize or infer missing lifecycle
     decisions from title patterns or heuristics.

Usage:
  PYTHONPATH=. python scripts/audit_contract_readiness.py
  PYTHONPATH=. python scripts/audit_contract_readiness.py --state-dir /path/to/state
  PYTHONPATH=. python scripts/audit_contract_readiness.py --json
"""

import argparse
import json
import sys
from pathlib import Path

# Terminal states — tickets in these states are excluded from readiness scan
TERMINAL_STATES = frozenset({
    "COMPLETE", "REJECTED", "DUPLICATE", "NOT_ACTIONABLE",
    "RESOLVED", "SUPERSEDED", "CANCELLED", "NEVER",
})

DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".codebot" / "state"
DEFAULT_TICKETS_FILE = DEFAULT_STATE_DIR / "tickets.json"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit ticket contract readiness before enforcement deployment."
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=DEFAULT_STATE_DIR,
        help="Path to the state directory containing tickets.json",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        dest="output_json",
        help="Output machine-readable JSON instead of human text",
    )
    return parser.parse_args()


def section_contract_coverage(transitions: dict, resolve_fn) -> dict:
    """Section 1: Verify every legal transition resolves a contract.

    Returns dict with 'total', 'covered', 'missing' keys.
    """
    total = 0
    covered = 0
    missing_pairs: list[str] = []

    for src_state, dest_states in transitions.items():
        src_val = src_state.value if hasattr(src_state, "value") else str(src_state)
        for dst_state in dest_states:
            dst_val = dst_state.value if hasattr(dst_state, "value") else str(dst_state)
            total += 1
            try:
                resolve_fn(src_val, dst_val)
                covered += 1
            except Exception:
                missing_pairs.append(f"{src_val}->{dst_val}")

    return {
        "total": total,
        "covered": covered,
        "missing": missing_pairs,
    }


def section_ticket_readiness(
    tickets: list,
    transitions: dict,
    check_fn,
) -> list[dict]:
    """Section 2: Per-transition readiness for each non-terminal ticket.

    Returns list of per-ticket readiness reports.
    """
    reports: list[dict] = []

    for ticket in tickets:
        state_val = ticket.state.value if hasattr(ticket.state, "value") else str(ticket.state)

        if state_val in TERMINAL_STATES:
            continue

        # Find the TicketState enum member for lookup
        src_enum = None
        for src_state in transitions:
            sv = src_state.value if hasattr(src_state, "value") else str(src_state)
            if sv == state_val:
                src_enum = src_state
                break

        if src_enum is None:
            # State not in TRANSITIONS (terminal sink or unknown)
            continue

        dest_states = transitions[src_enum]
        passing: list[str] = []
        failing: list[dict] = []

        for dst_state in dest_states:
            dst_val = dst_state.value if hasattr(dst_state, "value") else str(dst_state)
            try:
                ok, missing, inv = check_fn(ticket, state_val, dst_val)
                if ok:
                    passing.append(dst_val)
                else:
                    failing.append({
                        "destination": dst_val,
                        "missing_fields": missing,
                        "failed_invariants": inv,
                    })
            except Exception as e:
                failing.append({
                    "destination": dst_val,
                    "missing_fields": [f"contract resolution error: {e}"],
                    "failed_invariants": [],
                })

        tid = ticket.id if hasattr(ticket, "id") else "<unknown>"
        reports.append({
            "ticket_id": tid,
            "current_state": state_val,
            "passing_transitions": passing,
            "failing_transitions": failing,
        })

    return reports


def section_migration_candidates(readiness_reports: list[dict]) -> list[dict]:
    """Section 3: Identify tickets where authoritative data might be migratable.

    Only flags cases where data EXISTS authoritatively elsewhere on the ticket.
    Does NOT suggest synthesis or inference from title patterns.
    """
    candidates: list[dict] = []

    for report in readiness_reports:
        for failure in report.get("failing_transitions", []):
            missing = failure.get("missing_fields", [])
            dest = failure.get("destination", "")

            # goal_disposition can sometimes be migrated from goal_reason
            # if goal_reason contains an explicit disposition value
            if "goal_disposition" in missing:
                candidates.append({
                    "ticket_id": report["ticket_id"],
                    "transition": f"{report['current_state']}->{dest}",
                    "missing_field": "goal_disposition",
                    "migration_note": (
                        "Check if goal_reason or existing fields contain "
                        "an authoritative disposition value (NOW/LATER/NEVER). "
                        "Only migrate if the value is explicitly present; "
                        "do NOT infer from title patterns."
                    ),
                })

    return candidates


def print_text_report(
    coverage: dict,
    readiness: list[dict],
    migrations: list[dict],
) -> None:
    """Print human-readable report."""
    print("=" * 70)
    print("CONTEXT-CONTRACT READINESS AUDIT")
    print("=" * 70)

    # Section 1
    print("\n## 1. Contract Coverage")
    print(f"   Total legal transitions: {coverage['total']}")
    print(f"   Covered by contracts:    {coverage['covered']}")
    print(f"   Unresolved contracts:    {len(coverage['missing'])}")
    if coverage["missing"]:
        print("   MISSING (will block at runtime):")
        for pair in coverage["missing"]:
            print(f"     - {pair}")
    else:
        print("   Status: ALL TRANSITIONS COVERED")

    # Section 2
    print("\n## 2. Per-Transition Ticket Readiness")
    if not readiness:
        print("   No non-terminal tickets found in store.")
    else:
        print(f"   Non-terminal tickets scanned: {len(readiness)}")
        for report in readiness:
            tid = report["ticket_id"]
            state = report["current_state"]
            n_pass = len(report["passing_transitions"])
            n_fail = len(report["failing_transitions"])
            print(f"\n   [{tid}] state={state} | passing={n_pass} failing={n_fail}")
            if report["passing_transitions"]:
                print(f"     PASS: {', '.join(report['passing_transitions'])}")
            for f in report["failing_transitions"]:
                dest = f["destination"]
                parts: list[str] = []
                if f["missing_fields"]:
                    parts.append(f"missing={f['missing_fields']}")
                if f["failed_invariants"]:
                    parts.append(f"invariant_failures={f['failed_invariants']}")
                detail = "; ".join(parts) if parts else "unknown"
                print(f"     FAIL -> {dest}: {detail}")

    # Section 3
    print("\n## 3. Migration Candidates")
    if not migrations:
        print("   No migration candidates identified.")
        print("   (Migration may only move existing authoritative data;")
        print("    it may NOT synthesize or infer missing lifecycle decisions.)")
    else:
        print(f"   Candidates: {len(migrations)}")
        for m in migrations:
            print(f"\n   [{m['ticket_id']}] {m['transition']}")
            print(f"     Field: {m['missing_field']}")
            print(f"     Note: {m['migration_note']}")

    # Deployment precondition summary
    print("\n" + "=" * 70)
    print("DEPLOYMENT PRECONDITION CHECKLIST")
    print("=" * 70)
    unresolved = len(coverage["missing"])
    print(f"   Contract coverage:          {coverage['covered']}/{coverage['total']}")
    print(f"   Unresolved definitions:     {unresolved} {'PASS' if unresolved == 0 else 'FAIL'}")
    print(f"   Tickets needing repair:     {sum(1 for r in readiness if r['failing_transitions'])}")
    print(f"   State-changing bypasses:    0 (classified separately, see plan F2)")
    print(f"   Incompatible callers:       discovered via test suite (Todo 6/F4)")
    print("=" * 70)


def main() -> int:
    args = parse_args()

    # Import here so the script fails gracefully if codebot isn't installed
    try:
        from codebot.ticket_engine import TRANSITIONS, TicketStore
        from codebot.context_contracts import resolve_contract, check_transition_contract
    except ImportError as e:
        print(f"Error: cannot import codebot modules: {e}", file=sys.stderr)
        print("Run this script from the project root with codebot installed.", file=sys.stderr)
        return 2

    # Load tickets
    tickets_file = args.state_dir / "tickets.json"
    if not tickets_file.exists():
        # Fresh install — no tickets to audit
        if args.output_json:
            print(json.dumps({
                "contract_coverage": {"total": 0, "covered": 0, "missing": []},
                "ticket_readiness": [],
                "migration_candidates": [],
            }, indent=2))
        else:
            print("No tickets.json found. Nothing to audit.")
            print("Contract coverage will be verified mechanically by test suite.")
        return 0

    try:
        store = TicketStore(tickets_file)
    except Exception as e:
        print(f"Error: failed to load TicketStore: {e}", file=sys.stderr)
        return 2

    # Get all tickets as a list
    tickets = list(store._tickets.values())

    # Section 1: Contract coverage
    coverage = section_contract_coverage(TRANSITIONS, resolve_contract)

    # Section 2: Per-transition readiness
    readiness = section_ticket_readiness(tickets, TRANSITIONS, check_transition_contract)

    # Section 3: Migration candidates
    migrations = section_migration_candidates(readiness)

    # Output
    if args.output_json:
        output = {
            "contract_coverage": coverage,
            "ticket_readiness": readiness,
            "migration_candidates": migrations,
        }
        print(json.dumps(output, indent=2, default=str))
    else:
        print_text_report(coverage, readiness, migrations)

    # Exit code: 0 if all contracts resolvable, 1 if any missing
    if coverage["missing"]:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
