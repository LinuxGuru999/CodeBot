#!/usr/bin/env python3
"""Workforce Dispatch — Compatibility layer for ticket-to-agent assignment.

Purpose
-------
Provides a compatibility wrapper around dispatch_service.dispatch_ready_tickets
for legacy callers. This module no longer contains independent routing logic,
ensuring dispatch_service remains the single source of truth for dispatch
sequencing (Constitution §4).

Why
---
Consolidating routing logic in dispatch_service prevents circular architectural
dependencies and duplication. Callers should migrate to using
dispatch_service.dispatch_ready_tickets directly.

Invariants
----------
- No independent routing logic
- Delegates entirely to dispatch_service
- Maintains backward-compatible signature
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def run_workforce_dispatchers(
    bots: dict[str, Any],
    store: Any,
    start_bot_fn: Any = None,
    stop_bot_fn: Any = None,
    update_state_fn: Any = None,
    skip_route_tids: set[str] | None = None,
) -> None:
    """Dispatch ready tickets to available agents.

    Delegates to dispatch_service.dispatch_ready_tickets to ensure
    single-source routing logic.

    Args:
        bots: Current bot states.
        store: TicketStore instance.
        start_bot_fn: Callable to spawn/start a bot.
        stop_bot_fn: Callable to stop a bot.
        update_state_fn: Callable to update bot state persistence.
        skip_route_tids: Set of ticket IDs to exclude from routing (e.g., recently errored).
    """
    if store is None:
        logger.debug("No store provided to workforce_dispatch; skipping.")
        return

    try:
        from codebot.dispatch_service import dispatch_ready_tickets
    except ImportError as e:
        logger.error(f"Failed to import dispatch_service: {e}")
        return

    try:
        dispatch_ready_tickets(
            bots=bots,
            skip_route_tids=skip_route_tids,
            start_bot_fn=start_bot_fn,
            stop_fn=stop_bot_fn,
            update_state_fn=update_state_fn,
            store=store,
        )
    except Exception as e:
        logger.warning(f"dispatch_ready_tickets failed: {e}")
