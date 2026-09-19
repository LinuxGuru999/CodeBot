#!/usr/bin/env python3
"""Lifecycle-driven agent scheduler for CodeBot.

Purpose
-------
Maps each ticket lifecycle state to the specific agent roles that process it,
and dispatches work demand-driven: agents are only spawned when their
corresponding queue has tickets. Always-on control agents run regardless.

Why
---
The orchestrator previously called every dispatch function unconditionally on
each tick. This module replaces that with a table-driven approach where each
lifecycle state declares its handler, required agent category, and transition
target. The scheduler reads queue depths and only invokes handlers for states
with pending work.

Invariants
----------
- stdlib-only (dataclasses, enum, logging)
- Pure dispatch table: no I/O, no subprocess spawning
- Always-on agents are declared separately from lifecycle-triggered ones
- Each lifecycle entry maps to exactly one existing orchestrator handler
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable

logger = logging.getLogger("lifecycle_scheduler")


class LifecyclePhase(str, Enum):
    TRIAGE = "TRIAGE"
    READY_GATE = "READY_GATE"
    DECOMPOSE = "DECOMPOSE"
    PLANNING = "PLANNING"
    IMPLEMENTATION = "IMPLEMENTATION"
    REVIEW = "REVIEW"
    VERIFICATION = "VERIFICATION"
    REWORK = "REWORK"
    RECOVERY = "RECOVERY"


@dataclass(frozen=True)
class LifecycleDispatchEntry:
    phase: LifecyclePhase
    ticket_states: tuple[str, ...]
    agent_roles: tuple[str, ...]
    handler_name: str
    requires_bots: bool
    description: str


LIFECYCLE_DISPATCH_TABLE: tuple[LifecycleDispatchEntry, ...] = (
    LifecycleDispatchEntry(
        phase=LifecyclePhase.TRIAGE,
        ticket_states=("DISCOVERED", "VALIDATING", "TRIAGED"),
        agent_roles=("ticket_triager",),
        handler_name="_auto_triage_backlog",
        requires_bots=False,
        description="Advance tickets through triage pipeline to READY",
    ),
    LifecycleDispatchEntry(
        phase=LifecyclePhase.READY_GATE,
        ticket_states=("READY",),
        agent_roles=(),
        handler_name="_route_ready_tickets",
        requires_bots=False,
        description="Route all READY tickets into DECOMPOSE",
    ),
    LifecycleDispatchEntry(
        phase=LifecyclePhase.DECOMPOSE,
        ticket_states=("DECOMPOSE",),
        agent_roles=("decomposer",),
        handler_name="_dispatch_decompose_agents",
        requires_bots=True,
        description="Decompose tickets into sub-tickets, advance to PLANNING when done",
    ),
    LifecycleDispatchEntry(
        phase=LifecyclePhase.PLANNING,
        ticket_states=("PLANNING",),
        agent_roles=("implementation_planner",),
        handler_name="_dispatch_planning_agents",
        requires_bots=True,
        description="Generate implementation plans, advance to IMPLEMENTING when done",
    ),
    LifecycleDispatchEntry(
        phase=LifecyclePhase.IMPLEMENTATION,
        ticket_states=("IMPLEMENTING",),
        agent_roles=(
            "general_implementer", "backend_implementer", "frontend_implementer",
            "test_implementer", "migration_implementer", "documentation_implementer",
        ),
        handler_name="_dispatch_tickets_to_implementers",
        requires_bots=True,
        description="Dispatch IMPLEMENTING tickets to role-matched implementers",
    ),
    LifecycleDispatchEntry(
        phase=LifecyclePhase.REVIEW,
        ticket_states=("REVIEWING",),
        agent_roles=(
            "correctness_reviewer", "security_reviewer", "architecture_reviewer",
            "test_reviewer", "performance_reviewer", "simplicity_reviewer",
            "documentation_reviewer",
        ),
        handler_name="_dispatch_tickets_to_reviewers",
        requires_bots=True,
        description="Dispatch REVIEWING tickets to role-matched reviewers",
    ),
    LifecycleDispatchEntry(
        phase=LifecyclePhase.VERIFICATION,
        ticket_states=("VERIFYING",),
        agent_roles=("quality_gate",),
        handler_name="_gatekeeper_verify_tickets",
        requires_bots=False,
        description="Run quality gates on VERIFYING tickets, transition to COMPLETE or REWORK",
    ),
    LifecycleDispatchEntry(
        phase=LifecyclePhase.REWORK,
        ticket_states=("REWORK",),
        agent_roles=(
            "general_implementer", "backend_implementer", "frontend_implementer",
            "test_implementer", "migration_implementer", "documentation_implementer",
        ),
        handler_name="_process_rework_tickets",
        requires_bots=True,
        description="Re-dispatch REWORK tickets back to IMPLEMENTING or PLANNING based on risk",
    ),
    LifecycleDispatchEntry(
        phase=LifecyclePhase.RECOVERY,
        ticket_states=("DEFERRED", "BLOCKED"),
        agent_roles=("conflict_resolver",),
        handler_name="_recover_deferred_tickets",
        requires_bots=False,
        description="Recover deferred/blocked tickets back into the pipeline",
    ),
)

ALWAYS_ON_AGENTS: frozenset[str] = frozenset({
    "scheduler",
    "conflict_resolver",
    "budget_controller",
})


def get_queue_demands(ticket_counts: dict[str, int]) -> list[LifecycleDispatchEntry]:
    entries_with_work: list[LifecycleDispatchEntry] = []
    for entry in LIFECYCLE_DISPATCH_TABLE:
        total = sum(ticket_counts.get(state, 0) for state in entry.ticket_states)
        if total > 0:
            entries_with_work.append(entry)
    return entries_with_work


def dispatch_lifecycle(
    ticket_counts: dict[str, int],
    handler_registry: dict[str, Callable[..., Any]],
    bots: dict[str, Any] | None = None,
) -> dict[str, int]:
    demands = get_queue_demands(ticket_counts)
    results: dict[str, int] = {}

    for entry in demands:
        handler = handler_registry.get(entry.handler_name)
        if handler is None:
            logger.warning("No handler registered for %s", entry.handler_name)
            continue

        try:
            if entry.requires_bots and bots is not None:
                count = handler(bots)
            else:
                count = handler()
            results[entry.handler_name] = count or 0
            if count:
                logger.info(
                    "%s: dispatched %d ticket(s) via %s",
                    entry.phase.value, count, entry.handler_name,
                )
        except Exception as e:
            logger.warning("%s handler %s failed: %s", entry.phase.value, entry.handler_name, e)
            results[entry.handler_name] = 0

    return results
