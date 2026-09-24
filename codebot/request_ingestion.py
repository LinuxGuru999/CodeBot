"""Request ingestion — bridges UserRequest inbox to REQUESTED tickets.

Reads pending UserRequest JSON files from state/requests/, creates
REQUESTED tickets via TicketStore, handles crash recovery, and drives
reply reconciliation (DEFERRED -> REQUESTED when human responds).

This module owns quarantine. UserRequest is a pure data object.
See docs/CODING_STANDARDS.md §20 (ONE INGRESS CONTRACT).
"""
from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def ingest_requests(store: Any, state_dir: Path) -> int:
    """Ingest pending UserRequests into REQUESTED tickets.

    Returns count of newly created tickets.
    """
    if store is None:
        return 0

    from codebot.user_request import UserRequest, UserRequestStatus
    from codebot.ticket_engine import TicketState, TicketClass, Severity, RiskLevel, create_ticket

    UserRequest.ensure_directories(state_dir)
    requests_dir = state_dir / "requests"
    processed_dir = requests_dir / "processed"
    rejected_dir = requests_dir / "rejected"

    created = 0

    for path in sorted(requests_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            req = UserRequest.from_dict(data)
        except (json.JSONDecodeError, ValueError, OSError) as exc:
            dest = rejected_dir / f"{path.name}.malformed"
            try:
                shutil.move(str(path), str(dest))
            except OSError:
                pass
            logger.warning("quarantined malformed request %s: %s", path.name, exc)
            continue

        existing_ticket = _find_existing_ticket(store, req.request_id)

        if existing_ticket is not None:
            if req.status == UserRequestStatus.PENDING:
                updated = req.update_status(UserRequestStatus.RUNNING)
                _write_request(processed_dir / path.name, updated)
                _safe_unlink(path)
                logger.info("reconciled crashed ingestion for %s", req.request_id)
            elif req.status == UserRequestStatus.RUNNING:
                if path.parent != processed_dir:
                    _write_request(processed_dir / path.name, req)
                    _safe_unlink(path)
            continue

        if req.status != UserRequestStatus.PENDING:
            continue

        try:
            ticket = create_ticket(
                title=req.message[:80],
                ticket_class=TicketClass.REQUEST,
                severity=Severity.MEDIUM,
                source="user_agent",
                evidence=f"user-request:{req.request_id}",
                problem_statement=req.message,
                desired_state="Evaluated and decomposed by user_agent",
                acceptance_criteria=["user_agent evaluates this request"],
                risk=RiskLevel.MEDIUM,
                affected_modules=[],
                origin_id=req.request_id,
                origin_type="user_request",
            )
            store.add(ticket)
        except Exception as exc:
            logger.warning("failed to create REQUESTED ticket for %s: %s", req.request_id, exc)
            continue

        updated = req.update_status(UserRequestStatus.RUNNING)
        _write_request(processed_dir / path.name, updated)
        _safe_unlink(path)
        created += 1

    created += _reconcile_replies(store, processed_dir)

    return created


def cleanup_stale_deferred_requests(store: Any, state_dir: Path, max_age_seconds: float = 604800.0) -> int:
    """Cancel REQUESTED-origin DEFERRED tickets older than max_age_seconds."""
    if store is None:
        return 0

    import time
    from codebot.ticket_engine import TicketState

    cancelled = 0
    now = time.time()

    try:
        deferred = store.list_by_state(TicketState.DEFERRED)
    except Exception:
        return 0

    for ticket in deferred:
        if getattr(ticket, "origin_type", "") != "user_request":
            continue
        age = now - float(getattr(ticket, "updated_at", now))
        if age > max_age_seconds:
            try:
                store.transition(ticket.id, TicketState.CANCELLED)
                logger.info("auto-cancelled stale DEFERRED request ticket %s (age=%.0fs)", ticket.id, age)
                cancelled += 1
            except Exception as exc:
                logger.debug("failed to cancel stale ticket %s: %s", ticket.id, exc)

    return cancelled


def _reconcile_replies(store: Any, processed_dir: Path) -> int:
    """Drive DEFERRED->REQUESTED transitions when humans reply."""
    from codebot.user_request import UserRequest, UserRequestStatus
    from codebot.ticket_engine import TicketState

    transitions = 0

    for path in sorted(processed_dir.glob("*.json")):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            req = UserRequest.from_dict(data)
        except (json.JSONDecodeError, ValueError, OSError):
            continue

        if req.status != UserRequestStatus.PENDING:
            continue
        if not req.response:
            continue

        ticket = _find_existing_ticket(store, req.request_id)
        if ticket is None:
            continue
        if ticket.state != TicketState.DEFERRED:
            continue

        try:
            store.transition(ticket.id, TicketState.REQUESTED)
            logger.info("reconciled reply: DEFERRED->REQUESTED for %s", ticket.id)
            transitions += 1
        except Exception as exc:
            logger.warning("failed to transition %s DEFERRED->REQUESTED: %s", ticket.id, exc)

    return transitions


def _find_existing_ticket(store: Any, request_id: str) -> Any:
    for ticket in store.list_all():
        if getattr(ticket, "origin_type", "") == "user_request" and getattr(ticket, "origin_id", "") == request_id:
            return ticket
    return None


def _write_request(dest: Path, req: Any) -> None:
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(req.to_json(), encoding="utf-8")
    tmp.replace(dest)


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass
