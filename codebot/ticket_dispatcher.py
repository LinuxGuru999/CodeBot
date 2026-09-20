#!/usr/bin/env python3
"""Ticket Dispatcher Service — Decoupled dispatch and triage logic.

Purpose
-------
Contains the business logic for dispatching tickets to implementers and
reviewers, and auto-triaging backlog tickets. Extracted from orchestrator.py
to satisfy the 'Thin router, fat service' invariant (Constitution §4).

The orchestrator delegates to this service instead of embedding matching
and triage algorithms directly in its health loop.

Invariants
----------
- No subprocess management or bot lifecycle code here
- Only ticket state transitions and dispatch decisions
- Fails gracefully if ticket_engine is unavailable
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = _CODEBOT_PKG_DIR.parent
STATE_DIR = _project_root / ".codebot" / "state"

MIN_READY_BACKLOG = 5


def dispatch_tickets_to_implementers(bots: dict[str, Any]) -> int:
    """Dispatch READY tickets to available implementer bots.

    Superseded by demand-driven spawning in orchestrator._spawn_demand_agents.
    Kept as a no-op stub so the lifecycle scheduler handler registry can
    reference it without error.

    Returns 0 always — actual dispatch happens via _spawn_demand_agents.
    """
    return 0


def dispatch_tickets_to_reviewers(bots: dict[str, Any]) -> int:
    """Dispatch REVIEWING tickets to available reviewer bots.

    Superseded by demand-driven spawning in orchestrator._spawn_demand_agents.
    Kept as a no-op stub so the lifecycle scheduler handler registry can
    reference it without error.

    Returns 0 always — actual dispatch happens via _spawn_demand_agents.
    """
    return 0


def auto_triage_backlog(state_dir: Path | None = None) -> int:
    """Auto-advance DISCOVERED → VALIDATING → TRIAGED → READY when backlog is low.

    When the READY queue drops below MIN_READY_BACKLOG, this function
    advances tickets through the early pipeline stages to keep implementers
    fed. Caps advancement per tick to avoid flooding the pipeline.

    Returns the number of tickets advanced.
    """
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0

    sdir = state_dir or STATE_DIR
    store_path = sdir / "tickets.json"
    if not store_path.exists():
        store_path = sdir.parent / "state" / "tickets.json"
    if not store_path.exists():
        return 0

    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0

    ready_count = len(ts.list_ready_raw())
    discovered = ts.list_by_state(TicketState.DISCOVERED)
    validating = ts.list_by_state(TicketState.VALIDATING)
    triaged = ts.list_by_state(TicketState.TRIAGED)
    backlog_depth = len(discovered) + len(validating) + len(triaged)
    max_advance_per_tick = max(5, min(backlog_depth, 20))
    advanced = 0

    for state in (TicketState.DISCOVERED, TicketState.VALIDATING, TicketState.TRIAGED):
        if advanced >= max_advance_per_tick:
            break
        for ticket in ts.list_by_state(state):
            if advanced >= max_advance_per_tick:
                break
            target = (
                TicketState.VALIDATING if state == TicketState.DISCOVERED
                else TicketState.TRIAGED if state == TicketState.VALIDATING
                else TicketState.READY
            )
            try:
                ts.transition(ticket.id, target)
                advanced += 1
                logger.info(f"Auto-triaged {ticket.id}: {state.value} -> {target.value}")
            except ValueError:
                pass

    return advanced
