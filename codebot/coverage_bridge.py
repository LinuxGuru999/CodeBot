#!/usr/bin/env python3
"""Coverage-to-ticket bridge — converts low-coverage modules into test tickets.

Purpose
-------
Consumes a CoverageReport and generates prioritized TicketStore entries
for modules below the coverage threshold. Each ticket targets a specific
module's uncovered lines, assigned to the test_implementer role.

Why
---
Static analysis (test_gap_auditor) can only guess at gaps by checking
whether test files exist. Measured coverage provides exact line-level
gap data, enabling precise test-writing directives instead of broad
"add tests for module X" instructions.

Invariants
----------
- stdlib-only
- Never creates duplicate tickets (dedup via evidence hash)
- Tickets reference specific uncovered line ranges, not entire modules
- Priority scales inversely with coverage (lower coverage = higher priority)
- Respects constitution-protected paths (never creates tickets for protected files)
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger("coverage_bridge")

DEFAULT_MIN_COVERAGE_PCT = 60.0
CRITICAL_THRESHOLD_PCT = 30.0
HIGH_THRESHOLD_PCT = 50.0
MEDIUM_THRESHOLD_PCT = 70.0

MAX_LINES_PER_TICKET = 50


def generate_coverage_tickets(
    report: Any,
    store: Any,
    min_coverage_pct: float = DEFAULT_MIN_COVERAGE_PCT,
    protected_paths: set[str] | None = None,
    max_tickets: int = 20,
) -> list[str]:
    """Create test tickets for modules below coverage threshold.

    Args:
        report: CoverageReport instance from coverage_runner.
        store: TicketStore instance for creating tickets.
        min_coverage_pct: Minimum acceptable coverage percentage.
        protected_paths: Set of path prefixes to skip.
        max_tickets: Maximum tickets to create per run.

    Returns:
        List of created ticket IDs.
    """
    if report.error or not report.modules:
        logger.info("no coverage data available, skipping ticket generation")
        return []

    protected = protected_paths or set()
    below = report.below_threshold(min_coverage_pct)
    created_ids: list[str] = []

    for mod in below:
        if len(created_ids) >= max_tickets:
            break
        if _is_protected(mod.path, protected):
            continue
        if mod.statements == 0:
            continue

        chunks = _chunk_missing_lines(mod.missing, MAX_LINES_PER_TICKET)
        for chunk in chunks:
            if len(created_ids) >= max_tickets:
                break
            ticket_id = _create_test_ticket(store, mod, chunk, report.total_coverage_pct)
            if ticket_id:
                created_ids.append(ticket_id)

    logger.info("generated %d coverage tickets from %d below-threshold modules", len(created_ids), len(below))
    return created_ids


def _is_protected(filepath: str, protected: set[str]) -> bool:
    for prefix in protected:
        if filepath.startswith(prefix):
            return True
    return False


def _chunk_missing_lines(missing: list[int], max_lines: int) -> list[list[int]]:
    if not missing:
        return [[]]
    chunks: list[list[int]] = []
    current: list[int] = []
    for line in sorted(missing):
        current.append(line)
        if len(current) >= max_lines:
            chunks.append(current)
            current = []
    if current:
        chunks.append(current)
    return chunks if chunks else [[]]


def _severity_for_coverage(pct: float) -> str:
    if pct < CRITICAL_THRESHOLD_PCT:
        return "critical"
    if pct < HIGH_THRESHOLD_PCT:
        return "high"
    if pct < MEDIUM_THRESHOLD_PCT:
        return "medium"
    return "low"


def _risk_for_coverage(pct: float) -> str:
    if pct < CRITICAL_THRESHOLD_PCT:
        return "critical"
    if pct < HIGH_THRESHOLD_PCT:
        return "high"
    return "medium"


def _format_line_ranges(lines: list[int]) -> str:
    if not lines:
        return "all"
    ranges: list[str] = []
    start = lines[0]
    prev = lines[0]
    for line in lines[1:]:
        if line == prev + 1:
            prev = line
        else:
            if start == prev:
                ranges.append(str(start))
            else:
                ranges.append(f"{start}-{prev}")
            start = line
            prev = line
    if start == prev:
        ranges.append(str(start))
    else:
        ranges.append(f"{start}-{prev}")
    return ", ".join(ranges)


def _create_test_ticket(store: Any, mod: Any, lines: list[int], fleet_avg: float) -> str | None:
    from codebot.ticket_engine import create_ticket, TicketClass, Severity, RiskLevel, TicketState

    severity_str = _severity_for_coverage(mod.coverage_pct)
    risk_str = _risk_for_coverage(mod.coverage_pct)

    severity_map = {"critical": Severity.CRITICAL, "high": Severity.HIGH, "medium": Severity.MEDIUM, "low": Severity.LOW}
    risk_map = {"critical": RiskLevel.CRITICAL, "high": RiskLevel.HIGH, "medium": RiskLevel.MEDIUM, "low": RiskLevel.LOW}

    severity = severity_map.get(severity_str, Severity.MEDIUM)
    risk = risk_map.get(risk_str, RiskLevel.MEDIUM)

    line_ranges = _format_line_ranges(lines)
    title = f"Add tests for {mod.path} lines {line_ranges} ({mod.coverage_pct:.0f}% covered)"

    evidence_hash = hashlib.sha256(f"cov:{mod.path}:{line_ranges}".encode()).hexdigest()[:16]

    problem = (
        f"Module {mod.path} has {mod.coverage_pct:.1f}% coverage "
        f"({mod.covered}/{mod.statements} statements). "
        f"Uncovered lines: {line_ranges}. "
        f"Fleet average: {fleet_avg:.1f}%."
    )

    desired = f"All public functions in {mod.path} lines {line_ranges} have passing tests."

    acceptance = []
    for line_range in line_ranges.split(", "):
        acceptance.append(f"Tests exercise code at lines {line_range}")
    acceptance.append(f"pytest passes for {mod.path}")
    if not acceptance:
        acceptance = [f"pytest passes for {mod.path}"]

    try:
        ticket = create_ticket(
            title=title,
            ticket_class=TicketClass.TEST,
            severity=severity,
            source="coverage_bridge",
            evidence=evidence_hash,
            problem_statement=problem,
            desired_state=desired,
            acceptance_criteria=acceptance,
            risk=risk,
            affected_modules=[mod.path],
        )
        store.add(ticket)
        store.transition(ticket.id, TicketState.VALIDATING)
        store.transition(ticket.id, TicketState.TRIAGED)
        store.transition(ticket.id, TicketState.READY)
        logger.info("created coverage ticket %s for %s (%.1f%%)", ticket.id, mod.path, mod.coverage_pct)
        return ticket.id
    except ValueError as e:
        if "duplicate" in str(e).lower():
            logger.debug("skipped duplicate coverage ticket for %s", mod.path)
            return None
        logger.warning("failed to create coverage ticket for %s: %s", mod.path, e)
        return None


def coverage_delta_score(
    old_report: Any | None,
    new_report: Any,
    module_path: str | None = None,
) -> float:
    """Compute coverage improvement score for alignment reward.

    Returns a value between -0.1 and +0.1 representing coverage change.
    Positive means improvement, negative means regression.
    """
    if old_report is None or old_report.error:
        return 0.0
    if new_report.error:
        return 0.0

    if module_path:
        old_mod = old_report.modules.get(module_path)
        new_mod = new_report.modules.get(module_path)
        if old_mod is None or new_mod is None:
            return 0.0
        delta = new_mod.coverage_pct - old_mod.coverage_pct
    else:
        delta = new_report.total_coverage_pct - old_report.total_coverage_pct

    return max(-0.1, min(0.1, delta / 100.0))
