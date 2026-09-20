"""Scheduler — Ticket dispatch coordination.

Provides high-level dispatch functions for routing tickets to implementers
and reviewers. Extracted from orchestrator.py to satisfy SRP.

This module is a thin facade over ticket_dispatcher and dispatch_service.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("codebot.scheduler")


def dispatch_tickets_to_implementers(bots: dict[str, Any], start_bot_fn: Any = None) -> None:
    """Dispatch ready tickets to available implementer bots.

    Args:
        bots: Dictionary of bot states keyed by bot name.
        start_bot_fn: Callback to start a bot (signature: start_bot(bot, **kwargs)).
    """
    try:
        from codebot.ticket_dispatcher import spawn_demand_agents
        from codebot.process_manager import GATEWAY_MAX_CONCURRENT
        if start_bot_fn is not None:
            spawn_demand_agents(bots, GATEWAY_MAX_CONCURRENT, start_bot_fn=start_bot_fn)
    except Exception as e:
        logger.warning(f"dispatch_tickets_to_implementers failed: {e}")


def dispatch_tickets_to_reviewers(bots: dict[str, Any], start_bot_fn: Any = None) -> None:
    """Dispatch reviewed tickets to available reviewer bots.

    Args:
        bots: Dictionary of bot states keyed by bot name.
        start_bot_fn: Callback to start a bot (signature: start_bot(bot, **kwargs)).
    """
    try:
        from codebot.ticket_dispatcher import advance_reviewed_tickets
        advance_reviewed_tickets(bots)
    except Exception as e:
        logger.warning(f"dispatch_tickets_to_reviewers failed: {e}")
