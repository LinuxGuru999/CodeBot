#!/usr/bin/env python3
"""Task splitter — decomposes oversized tickets into parallelizable sub-tasks.

Purpose
-------
When an agent hits SESSION_TIMEOUT, a fatal error, or a rate limit during
a large ticket, the task splitter breaks the remaining work into smaller
sub-tickets that can be claimed by different workers. Each sub-ticket
references the parent and includes a scratchpad handoff note.

Why
---
Single-agent execution has a hard ceiling (~50 iterations, ~300s timeout).
Large refactors spanning 10+ files cannot complete in one session. The
splitter enables fan-out: one failing agent's partial work becomes N
smaller tickets that complete in parallel.

Invariants
----------
- stdlib-only
- Sub-tickets always depend on parent ticket ID
- Split only happens on explicit trigger (timeout/failure/request)
- Never splits tickets already in COMPLETE or REJECTED state
- Max 10 sub-tickets per split to prevent unbounded fan-out
"""

from __future__ import annotations

import hashlib
import logging
import math
import time
from typing import Any

logger = logging.getLogger("task_splitter")

MAX_SUB_TASKS = 10
MIN_FILES_FOR_SPLIT = 3


def should_split(
    ticket: Any,
    scratchpad: Any | None = None,
    exit_reason: str = "",
) -> bool:
    from codebot.ticket_engine import TicketState
    terminal = {TicketState.COMPLETE, TicketState.REJECTED, TicketState.DUPLICATE}
    if ticket.state in terminal:
        return False
    if exit_reason in ("timeout", "rate_limit", "fatal_error", "token_cap"):
        return True
    if scratchpad and scratchpad.remaining_steps and len(scratchpad.remaining_steps) >= MIN_FILES_FOR_SPLIT:
        return True
    if ticket.affected_modules and len(ticket.affected_modules) >= 5:
        return True
    return False


def split_ticket(
    parent_ticket: Any,
    store: Any,
    scratchpad: Any | None = None,
    exit_reason: str = "",
) -> list[str]:
    from codebot.ticket_engine import create_ticket, TicketClass, Severity, RiskLevel, TicketState
    from codebot.scratchpad import create_handoff_note

    if not should_split(parent_ticket, scratchpad, exit_reason):
        return []

    chunks = compute_chunks(parent_ticket, scratchpad)
    if not chunks:
        return []

    handoff = ""
    if scratchpad:
        handoff = create_handoff_note(scratchpad)

    sub_ids: list[str] = []
    for i, chunk in enumerate(chunks):
        sub_title = f"[SPLIT {i+1}/{len(chunks)}] {parent_ticket.title}"
        sub_evidence = f"Parent: {parent_ticket.id}\nSplit reason: {exit_reason or 'size'}\nChunk: {chunk['description']}"
        if handoff:
            sub_evidence += f"\n\nHandoff:\n{handoff}"

        sub_acceptance = chunk.get("acceptance", parent_ticket.acceptance_criteria[:2])
        if not sub_acceptance:
            sub_acceptance = [f"Complete: {chunk['description']}"]

        try:
            sub = create_ticket(
                title=sub_title[:200],
                ticket_class=parent_ticket.ticket_class,
                severity=parent_ticket.severity,
                source=f"split:{parent_ticket.id}",
                evidence=sub_evidence[:500],
                problem_statement=chunk["description"],
                desired_state=parent_ticket.desired_state,
                acceptance_criteria=sub_acceptance,
                risk=parent_ticket.risk,
                affected_modules=chunk.get("modules", []),
                dependencies=sub_ids,
            )
            store.add(sub)
            store.transition(sub.id, TicketState.VALIDATING)
            store.transition(sub.id, TicketState.TRIAGED)
            store.transition(sub.id, TicketState.READY)
            sub_ids.append(sub.id)
            logger.info("created sub-task %s for parent %s", sub.id, parent_ticket.id)
        except ValueError as e:
            if "duplicate" in str(e).lower():
                logger.debug("skipped duplicate sub-task for %s", parent_ticket.id)
            else:
                logger.warning("failed to create sub-task: %s", e)

    if sub_ids:
        try:
            store.transition(parent_ticket.id, TicketState.BLOCKED)
        except ValueError:
            pass
        logger.info(
            "split %s into %d sub-tasks (reason: %s)",
            parent_ticket.id, len(sub_ids), exit_reason or "size",
        )

    return sub_ids


def compute_chunks(ticket: Any, scratchpad: Any | None) -> list[dict[str, Any]]:
    chunks: list[dict[str, Any]] = []

    if scratchpad and scratchpad.remaining_steps:
        steps = scratchpad.remaining_steps
        chunk_size = max(1, math.ceil(len(steps) / min(MAX_SUB_TASKS, len(steps))))
        for i in range(0, len(steps), chunk_size):
            batch = steps[i:i + chunk_size]
            chunks.append({
                "description": f"Complete steps: {'; '.join(batch[:3])}{'...' if len(batch) > 3 else ''}",
                "modules": ticket.affected_modules[:3] if ticket.affected_modules else [],
                "acceptance": [f"Complete: {s}" for s in batch[:3]],
            })
    elif ticket.affected_modules and len(ticket.affected_modules) >= MIN_FILES_FOR_SPLIT:
        modules = ticket.affected_modules
        chunk_size = max(1, math.ceil(len(modules) / min(MAX_SUB_TASKS, len(modules))))
        for i in range(0, len(modules), chunk_size):
            batch = modules[i:i + chunk_size]
            chunks.append({
                "description": f"Implement changes in: {', '.join(batch)}",
                "modules": batch,
                "acceptance": [f"Tests pass for {m}" for m in batch],
            })
    else:
        if ticket.acceptance_criteria and len(ticket.acceptance_criteria) > 1:
            criteria = ticket.acceptance_criteria
            mid = len(criteria) // 2
            chunks.append({
                "description": f"Part 1: {'; '.join(criteria[:mid])}",
                "modules": ticket.affected_modules,
                "acceptance": criteria[:mid],
            })
            chunks.append({
                "description": f"Part 2: {'; '.join(criteria[mid:])}",
                "modules": ticket.affected_modules,
                "acceptance": criteria[mid:],
            })

    return chunks[:MAX_SUB_TASKS]


_compute_chunks = compute_chunks
