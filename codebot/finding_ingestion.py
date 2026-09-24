"""Finding ingestion — bridges Finding inbox to DISCOVERED tickets.

Reads DiscoveryFinding JSON files from state/findings/, applies exact
deduplication (suppress) and fuzzy deduplication (enrich context), creates
DISCOVERED tickets via TicketStore, and quarantines malformed inputs.

Exact dedup (finding_id or fingerprint match) suppresses deterministically.
Fuzzy dedup (Jaccard >= 0.8) enriches context only — produces
related_ticket_candidates, never suppresses work.

See docs/CODING_STANDARDS.md §21 (IDEMPOTENT INGESTION).
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def ingest_findings(store: Any, state_dir: Path) -> int:
    """Ingest pending Findings into DISCOVERED tickets.

    Returns count of newly created tickets.
    """
    if store is None:
        return 0

    from codebot.discovery_finding import DiscoveryFinding
    from codebot.ticket_engine import (
        TicketState, TicketClass, Severity, RiskLevel, create_ticket,
    )

    findings_dir = state_dir / "findings"
    processed_dir = findings_dir / "processed"
    rejected_dir = findings_dir / "rejected"
    findings_dir.mkdir(parents=True, exist_ok=True)
    processed_dir.mkdir(parents=True, exist_ok=True)
    rejected_dir.mkdir(parents=True, exist_ok=True)

    created = 0

    for path in sorted(findings_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            finding = DiscoveryFinding.from_dict(data)
        except (json.JSONDecodeError, ValueError, OSError, KeyError) as exc:
            dest = rejected_dir / f"{path.name}.malformed"
            try:
                shutil.move(str(path), str(dest))
            except OSError:
                pass
            logger.warning("quarantined malformed finding %s: %s", path.name, exc)
            continue

        if _is_exact_duplicate(store, finding):
            _move_to_processed(path, processed_dir)
            logger.info("suppressed duplicate finding %s", finding.finding_id)
            continue

        related_candidates = _build_related_candidates(store, finding)

        try:
            category_str = finding.discovery_category
            try:
                ticket_class = TicketClass(category_str)
            except ValueError:
                ticket_class = TicketClass.FEATURE

            severity_str = finding.severity
            try:
                severity = Severity(severity_str)
            except ValueError:
                severity = Severity.MEDIUM

            evidence_items = []
            for ev in finding.evidence:
                if hasattr(ev, "to_dict"):
                    evidence_items.append(ev.to_dict())
                elif isinstance(ev, dict):
                    evidence_items.append(ev)

            for cand in related_candidates:
                evidence_items.append({
                    "observation": cand["ticket_id"],
                    "interpretation": f"Similar ticket in state {cand['state']}: {cand['title']}",
                    "impact": f"Jaccard similarity: {cand['similarity']:.2f}",
                    "kind": "related_ticket_candidate",
                })

            evidence_str = json.dumps(evidence_items) if evidence_items else finding.problem_statement[:200]

            ticket = create_ticket(
                title=finding.title[:80],
                ticket_class=ticket_class,
                severity=severity,
                source=finding.discovery_role,
                evidence=evidence_str,
                problem_statement=finding.problem_statement,
                desired_state=finding.acceptance_outcome,
                acceptance_criteria=[finding.acceptance_outcome] if finding.acceptance_outcome else ["Evaluate and implement"],
                risk=RiskLevel.MEDIUM,
                affected_modules=finding.affected_components or [],
                finding_id=finding.finding_id,
                fingerprint=finding.fingerprint(),
            )
            store.add(ticket)
        except Exception as exc:
            logger.warning("failed to create DISCOVERED ticket for finding %s: %s", finding.finding_id, exc)
            continue

        _move_to_processed(path, processed_dir)
        created += 1

    return created


def _is_exact_duplicate(store: Any, finding: Any) -> bool:
    if finding.finding_id:
        for ticket in store.list_all():
            if getattr(ticket, "finding_id", "") == finding.finding_id:
                return True
    fp = finding.fingerprint()
    if fp and hasattr(store, "_fingerprint_index"):
        if fp in store._fingerprint_index:
            return True
    return False


def _build_related_candidates(store: Any, finding: Any) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    terminal_states = frozenset({"COMPLETE", "REJECTED", "CANCELLED", "RESOLVED", "SUPERSEDED"})
    try:
        similar = store.find_similar(problem_statement=finding.problem_statement, limit=3)
        for ticket, similarity in similar:
            if similarity < 0.8:
                continue
            state_val = ticket.state.value if hasattr(ticket.state, "value") else str(ticket.state)
            if state_val in terminal_states:
                continue
            if getattr(ticket, "finding_id", "") == finding.finding_id:
                continue
            candidates.append({
                "ticket_id": ticket.id,
                "title": ticket.title,
                "state": state_val,
                "similarity": round(similarity, 3),
            })
    except Exception as exc:
        logger.debug("find_similar failed for %s: %s", finding.finding_id, exc)
    return candidates


def _move_to_processed(src: Path, processed_dir: Path) -> None:
    dest = processed_dir / src.name
    try:
        shutil.move(str(src), str(dest))
    except OSError:
        try:
            src.unlink(missing_ok=True)
        except OSError:
            pass
