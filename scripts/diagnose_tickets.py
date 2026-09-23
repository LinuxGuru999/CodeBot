#!/usr/bin/env python3
"""Diagnostic script to scan CodeBot tickets for errors and inconsistencies.

Read-only: never modifies the ticket store.

Checks:
  - Invalid states (not in TicketState enum)
  - Stuck tickets (non-terminal, updated_at older than threshold)
  - Rework loops (rework_count >= max)
  - Missing required fields
  - Orphaned dependencies (referenced ticket IDs not in store)
  - Schema version drift
  - Duplicate evidence hashes

Usage:
  python scripts/diagnose_tickets.py
  python scripts/diagnose_tickets.py --state-dir /path/to/state
  python scripts/diagnose_tickets.py --tickets-file /path/to/tickets.json
  python scripts/diagnose_tickets.py --max-age-hours 48 --json
"""

import argparse
import hashlib
import json
import sys
import time
from collections import defaultdict
from pathlib import Path

# Valid ticket states from codebot.ticket_engine.TicketState
VALID_STATES = frozenset({
    "DISCOVERED", "VALIDATING", "TRIAGED", "READY", "DECOMPOSE",
    "PLANNING", "IMPLEMENTATION_READY", "IMPLEMENTING", "REVIEWING",
    "VERIFYING", "COMPLETE", "BLOCKED", "REWORK", "REJECTED",
    "DUPLICATE", "DEFERRED",
})

# Terminal states with no outgoing transitions
TERMINAL_STATES = frozenset({"COMPLETE", "REJECTED", "DUPLICATE"})

# Non-terminal states where a ticket can get stuck
NON_TERMINAL_STATES = VALID_STATES - TERMINAL_STATES

# Required fields that must be present and non-empty
REQUIRED_FIELDS = (
    "title", "problem_statement", "evidence", "source",
    "ticket_class", "severity", "risk",
)

CURRENT_SCHEMA_VERSION = "2.0"
DEFAULT_MAX_AGE_HOURS = 24
DEFAULT_REWORK_MAX = 3
DEFAULT_STATE_DIR = Path(__file__).resolve().parent.parent / ".codebot" / "state"
DEFAULT_TICKETS_FILE = DEFAULT_STATE_DIR / "tickets.json"


def evidence_hash(ticket: dict) -> str:
    canonical = f"{ticket.get('ticket_class', '')}:{ticket.get('problem_statement', '')}:{ticket.get('evidence', '')}"
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


def load_tickets(tickets_file: Path) -> list[dict]:
    if not tickets_file.exists():
        print(f"Error: tickets file not found: {tickets_file}", file=sys.stderr)
        sys.exit(2)
    try:
        data = json.loads(tickets_file.read_text(encoding="utf-8"))
        return data.get("tickets", [])
    except json.JSONDecodeError as e:
        print(f"Error: failed to parse {tickets_file}: {e}", file=sys.stderr)
        sys.exit(2)
    except OSError as e:
        print(f"Error: failed to read {tickets_file}: {e}", file=sys.stderr)
        sys.exit(2)


def check_invalid_states(tickets: list[dict]) -> list[dict]:
    findings = []
    for t in tickets:
        state = t.get("state", "")
        if state not in VALID_STATES:
            findings.append({
                "ticket_id": t.get("id", "<unknown>"),
                "state": state,
                "issue": f"Invalid state '{state}' not in TicketState enum",
            })
    return findings


def check_stuck_tickets(tickets: list[dict], max_age_hours: float) -> list[dict]:
    findings = []
    now = time.time()
    max_age_seconds = max_age_hours * 3600
    for t in tickets:
        state = t.get("state", "")
        if state not in NON_TERMINAL_STATES:
            continue
        updated_at = t.get("updated_at", 0)
        if not updated_at:
            continue
        age_hours = (now - updated_at) / 3600
        if age_hours > max_age_hours:
            findings.append({
                "ticket_id": t.get("id", "<unknown>"),
                "state": state,
                "issue": f"Stuck in {state} for {age_hours:.1f}h (threshold: {max_age_hours}h)",
            })
    return findings


def check_rework_loops(tickets: list[dict], max_rework: int) -> list[dict]:
    findings = []
    for t in tickets:
        rework_count = t.get("rework_count", 0)
        if rework_count >= max_rework:
            findings.append({
                "ticket_id": t.get("id", "<unknown>"),
                "state": t.get("state", ""),
                "issue": f"Rework count {rework_count} >= max {max_rework}",
            })
    return findings


def check_missing_fields(tickets: list[dict]) -> list[dict]:
    findings = []
    for t in tickets:
        tid = t.get("id", "<unknown>")
        state = t.get("state", "")
        for field in REQUIRED_FIELDS:
            val = t.get(field)
            if val is None or (isinstance(val, str) and not val.strip()):
                findings.append({
                    "ticket_id": tid,
                    "state": state,
                    "issue": f"Missing required field: '{field}'",
                })
        # acceptance_criteria must be a non-empty list
        ac = t.get("acceptance_criteria")
        if not ac or not isinstance(ac, list):
            findings.append({
                "ticket_id": tid,
                "state": state,
                "issue": "Missing or empty acceptance_criteria (must be non-empty list)",
            })
    return findings


def check_orphaned_dependencies(tickets: list[dict]) -> list[dict]:
    findings = []
    all_ids = {t.get("id") for t in tickets if t.get("id")}
    for t in tickets:
        deps = t.get("dependencies", [])
        if not isinstance(deps, list):
            continue
        for dep in deps:
            if dep and dep not in all_ids:
                findings.append({
                    "ticket_id": t.get("id", "<unknown>"),
                    "state": t.get("state", ""),
                    "issue": f"Orphaned dependency: '{dep}' not found in store",
                })
    return findings


def check_schema_drift(tickets: list[dict]) -> list[dict]:
    findings = []
    for t in tickets:
        sv = t.get("schema_version", "")
        if sv != CURRENT_SCHEMA_VERSION:
            findings.append({
                "ticket_id": t.get("id", "<unknown>"),
                "state": t.get("state", ""),
                "issue": f"Schema version '{sv}' != expected '{CURRENT_SCHEMA_VERSION}'",
            })
    return findings


def check_duplicate_evidence(tickets: list[dict]) -> list[dict]:
    findings = []
    hash_map: dict[str, list[str]] = defaultdict(list)
    for t in tickets:
        h = evidence_hash(t)
        hash_map[h].append(t.get("id", "<unknown>"))
    for h, ids in hash_map.items():
        if len(ids) > 1:
            for tid in ids:
                # Find state for this ticket
                state = ""
                for t in tickets:
                    if t.get("id") == tid:
                        state = t.get("state", "")
                        break
                findings.append({
                    "ticket_id": tid,
                    "state": state,
                    "issue": f"Duplicate evidence hash '{h}' shared with: {', '.join(i for i in ids if i != tid)}",
                })
    return findings


def run_diagnostics(
    tickets_file: Path,
    max_age_hours: float,
    max_rework: int,
) -> tuple[list[dict], int]:
    tickets = load_tickets(tickets_file)
    total = len(tickets)

    categories = {
        "invalid_states": check_invalid_states(tickets),
        "stuck_tickets": check_stuck_tickets(tickets, max_age_hours),
        "rework_loops": check_rework_loops(tickets, max_rework),
        "missing_fields": check_missing_fields(tickets),
        "orphaned_dependencies": check_orphaned_dependencies(tickets),
        "schema_drift": check_schema_drift(tickets),
        "duplicate_evidence": check_duplicate_evidence(tickets),
    }

    all_findings = []
    for cat_name, findings in categories.items():
        for f in findings:
            f["category"] = cat_name
            all_findings.append(f)

    active_categories = sum(1 for v in categories.values() if v)
    return all_findings, total, active_categories, categories


def print_text(findings: list[dict], total: int, active_categories: int, categories: dict) -> None:
    if not findings:
        print(f"{total} tickets scanned, 0 issues found across 0 categories")
        return

    for cat_name, cat_findings in categories.items():
        if not cat_findings:
            continue
        header = cat_name.replace("_", " ").title()
        print(f"\n=== {header} ({len(cat_findings)}) ===")
        for f in cat_findings:
            print(f"  [{f['ticket_id']}] state={f['state']} — {f['issue']}")

    print(f"\n{total} tickets scanned, {len(findings)} issues found across {active_categories} categories")


def print_json(findings: list[dict], total: int, active_categories: int) -> None:
    output = {
        "tickets_scanned": total,
        "issues_found": len(findings),
        "categories_with_issues": active_categories,
        "findings": findings,
    }
    print(json.dumps(output, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Diagnose CodeBot ticket store for errors and inconsistencies.")
    parser.add_argument("--state-dir", type=Path, default=None, help="Path to state directory (default: .codebot/state/)")
    parser.add_argument("--tickets-file", type=Path, default=None, help="Path to tickets.json (overrides --state-dir)")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS, help=f"Hours before a non-terminal ticket is considered stuck (default: {DEFAULT_MAX_AGE_HOURS})")
    parser.add_argument("--max-rework", type=int, default=DEFAULT_REWORK_MAX, help=f"Max rework cycles before flagging (default: {DEFAULT_REWORK_MAX})")
    parser.add_argument("--json", action="store_true", dest="output_json", help="Output as JSON instead of text")
    args = parser.parse_args()

    if args.tickets_file:
        tickets_file = args.tickets_file
    elif args.state_dir:
        tickets_file = args.state_dir / "tickets.json"
    else:
        tickets_file = DEFAULT_TICKETS_FILE

    findings, total, active_categories, categories = run_diagnostics(
        tickets_file=tickets_file,
        max_age_hours=args.max_age_hours,
        max_rework=args.max_rework,
    )

    if args.output_json:
        print_json(findings, total, active_categories)
    else:
        print_text(findings, total, active_categories, categories)

    sys.exit(1 if findings else 0)


if __name__ == "__main__":
    main()
