#!/usr/bin/env python3
"""Migrate QUEUE.md items into CodeBot TicketStore.

Purpose
-------
One-time migration script that parses the legacy Monitor QUEUE.md format
and creates normalized tickets in the CodeBot ticket engine. Run once during
big-bang cutover, then delete.

Usage:
    python3 -m codebot.migrate_queue --project /path/to/monitor --queue docs/triage/QUEUE.md
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from codebot.ticket_engine import create_ticket, TicketClass, Severity, RiskLevel, TicketStore, TicketState


SEVERITY_MAP = {
    "critical": Severity.CRITICAL,
    "high": Severity.HIGH,
    "medium": Severity.MEDIUM,
    "low": Severity.LOW,
}

CLASS_MAP = {
    "bug": TicketClass.BUG,
    "feature": TicketClass.FEATURE,
    "security": TicketClass.SECURITY,
    "performance": TicketClass.PERFORMANCE,
    "documentation": TicketClass.DOCUMENTATION,
    "test": TicketClass.TEST,
    "refactor": TicketClass.REFACTOR,
    "dependency": TicketClass.DEPENDENCY,
    "infrastructure": TicketClass.INFRASTRUCTURE,
}

RISK_MAP = {
    "critical": RiskLevel.CRITICAL,
    "high": RiskLevel.HIGH,
    "medium": RiskLevel.MEDIUM,
    "low": RiskLevel.LOW,
}

# Matches patterns like: **P0 [T4] [CRITICAL]**: T4 Exit Criterion 1 — description
ITEM_RE = re.compile(
    r"\d+\.\s+\*\*(?:DONE\s+)?(?:P\d+\s+)?(?:\[(\w+)\]\s+)?(?:\[(\w+)\]\)?)?\*\*:\s*(.+?)(?:\s*$)",
    re.MULTILINE,
)

# Matches key: value lines under an item
FIELD_RE = re.compile(r"^\s+(\w[\w\s]*?):\s+(.+)$", re.MULTILINE)


def parse_queue_md(text: str) -> list[dict]:
    items = []
    blocks = re.split(r"\n(?=\d+\.\s+\*\*)", text)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        m = ITEM_RE.match(block)
        if not m:
            continue
        tier = m.group(1) or ""
        severity_str = (m.group(2) or "medium").lower()
        title = m.group(3).strip().rstrip("—").strip()
        fields = {}
        for fm in FIELD_RE.finditer(block):
            key = fm.group(1).strip().lower().replace(" ", "_")
            val = fm.group(2).strip()
            fields[key] = val
        if not title:
            continue
        items.append({
            "title": title[:200],
            "tier": tier,
            "severity": severity_str,
            "fields": fields,
            "raw": block[:500],
        })
    return items


def migrate(queue_path: Path, state_dir: Path, dry_run: bool = False) -> int:
    if not queue_path.exists():
        print(f"Queue file not found: {queue_path}", file=sys.stderr)
        return 1

    text = queue_path.read_text(encoding="utf-8")
    items = parse_queue_md(text)
    print(f"Parsed {len(items)} items from {queue_path.name}")

    store = TicketStore(state_dir / "codebot_tickets.json")
    migrated = 0
    skipped = 0

    for item in items:
        title = item["title"]
        sev_str = item["severity"]
        severity = SEVERITY_MAP.get(sev_str, Severity.MEDIUM)
        risk = RISK_MAP.get(sev_str, RiskLevel.MEDIUM)
        fields = item["fields"]

        status = fields.get("status", "").upper()
        if "DONE" in status or "COMPLETE" in status or item["raw"].startswith("**DONE"):
            skipped += 1
            continue

        ticket_class_str = fields.get("class", "feature").lower()
        ticket_class = CLASS_MAP.get(ticket_class_str, TicketClass.FEATURE)

        acceptance_raw = fields.get("acceptance", fields.get("acceptance_criteria", ""))
        acceptance = [a.strip() for a in acceptance_raw.split(";") if a.strip()] if acceptance_raw else [f"Verify: {title}"]
        if not acceptance:
            acceptance = [f"Verify: {title}"]

        affected_raw = fields.get("affected_modules", fields.get("depends_on", ""))
        affected = [a.strip() for a in affected_raw.split(",") if a.strip()] if affected_raw else []

        deps_raw = fields.get("dependencies", fields.get("depends_on", ""))
        deps = [d.strip() for d in deps_raw.split(",") if d.strip()] if deps_raw else []

        evidence = item["raw"][:500]
        problem = fields.get("problem_statement", fields.get("description", f"Implement: {title}"))
        desired = fields.get("desired_state", fields.get("implementation_notes", title))

        if dry_run:
            print(f"  [DRY RUN] {severity.value.upper():8s} | {ticket_class.value:12s} | {title[:60]}")
            migrated += 1
            continue

        try:
            ticket = create_ticket(
                title=title,
                ticket_class=ticket_class,
                severity=severity,
                source="queue_migration",
                evidence=evidence,
                problem_statement=problem,
                desired_state=desired,
                acceptance_criteria=acceptance,
                risk=risk,
                affected_modules=affected,
                dependencies=deps,
            )
            store.add(ticket)
            store.transition(ticket.id, TicketState.VALIDATING)
            store.transition(ticket.id, TicketState.TRIAGED)
            store.transition(ticket.id, TicketState.READY)
            migrated += 1
            print(f"  Migrated: {ticket.id} | {severity.value.upper():8s} | {title[:50]}")
        except ValueError as e:
            if "duplicate" in str(e).lower():
                skipped += 1
            else:
                print(f"  SKIP: {title[:40]}... ({e})", file=sys.stderr)
                skipped += 1

    print(f"\nMigration complete: {migrated} migrated, {skipped} skipped/done/duplicate")
    if not dry_run:
        print(f"Ticket store: {state_dir / 'codebot_tickets.json'}")
        print(f"Total in store: {store.count()}")
        store.flush()
        store.close()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Migrate QUEUE.md to CodeBot TicketStore")
    parser.add_argument("--project", type=str, default=".", help="Project root path")
    parser.add_argument("--queue", type=str, default="docs/triage/QUEUE.md", help="Relative path to QUEUE.md")
    parser.add_argument("--dry-run", action="store_true", help="Parse only, don't write tickets")
    args = parser.parse_args()

    project_root = Path(args.project).resolve()
    queue_path = project_root / args.queue
    state_dir = project_root / ".codebot" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)

    sys.exit(migrate(queue_path, state_dir, dry_run=args.dry_run))


if __name__ == "__main__":
    main()
