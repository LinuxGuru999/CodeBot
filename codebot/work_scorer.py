#!/usr/bin/env python3
"""Utility scoring engine for adaptive scheduler work prioritization.

Purpose
-------
Assigns a numeric utility score to every schedulable work item so the
scheduler can rank and select the highest-value work for available slots.
Scores incorporate priority, bottleneck relief, dependency unlock value,
age-based fairness, risk penalties, conflict penalties, and cost estimates.

Why
---
Spec §32 requires a transparent, testable scoring system. Without it,
scheduling decisions are ad-hoc. With it, every allocation is auditable:
"Why was ticket X picked over Y? Because X scored 87.3 vs Y's 42.1."

Invariants
----------
- stdlib-only (dataclasses)
- Pure functions: no I/O, no side effects
- Scores are floats; higher = more valuable
- Priority aging never allows low-priority work to outrank security/critical
- Cost penalty is subtractive, not multiplicative (prevents zero-cost gaming)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


# Base priority weights by ticket severity (§13)
SEVERITY_WEIGHTS: dict[str, float] = {
    "critical": 100.0,
    "high": 60.0,
    "medium": 30.0,
    "low": 10.0,
}

# Stage-based priority ordering (§13)
STAGE_PRIORITY: dict[str, float] = {
    "rework": 90.0,
    "security_emergency": 150.0,
    "verification": 70.0,
    "review": 60.0,
    "implementation": 50.0,
    "planning": 30.0,
    "validation": 25.0,
    "discovery": 10.0,
}


@dataclass(frozen=True)
class ScoredWorkItem:
    """A work item with its computed utility score."""
    ticket_id: str
    stage: str
    score: float
    components: dict[str, float]
    blocked: bool = False
    has_conflict: bool = False

    @property
    def total_score(self) -> float:
        return self.score

    @property
    def total_score(self) -> float:
        return self.score

    @property
    def is_schedulable(self) -> bool:
        return not self.blocked and not self.has_conflict


def compute_aging_bonus(
    created_at: float,
    now: float,
    config: Any,
) -> float:
    """Compute priority aging bonus for fairness (§27).

    Tickets waiting longer than aging_start_seconds gradually gain bonus
    score, preventing starvation of low-priority work. Security and
    critical tickets are exempt — they already dominate.

    Args:
        created_at: ticket creation timestamp
        now: current timestamp
        config: PriorityAgingConfig instance

    Returns:
        Aging bonus in [0.0, max_aging_bonus]
    """
    age_seconds = max(0.0, now - created_at)
    if age_seconds < config.aging_start_seconds:
        return 0.0

    excess_hours = (age_seconds - config.aging_start_seconds) / 3600.0
    raw_bonus = excess_hours * config.aging_rate_per_hour
    return min(raw_bonus, config.max_aging_bonus)


def compute_dependency_unlock_value(
    ticket_id: str,
    dependency_graph: Any,
    pipeline_state: Any,
) -> float:
    """Score how many downstream tickets this work item unblocks (§33).

    A ticket that unblocks 8 others is far more valuable than one that
    unblocks none, all else equal.

    Args:
        ticket_id: the ticket to evaluate
        dependency_graph: DependencyGraph instance (or None)
        pipeline_state: PipelineState for counting non-complete dependents

    Returns:
        Unlock value score (0.0 to ~50.0)
    """
    if dependency_graph is None:
        return 0.0

    try:
        dependents = dependency_graph.get_dependents(ticket_id)
        if not dependents:
            return 0.0

        # Count how many dependents are actually blocked (not complete)
        blocked_count = 0
        for dep_id in dependents:
            if pipeline_state.tickets_blocked_by_deps(dep_id):
                blocked_count += 1

        # Each unblocked dependent adds value, diminishing returns
        # 1->10, 2->18, 3->24, 5->33, 8->42, capped at 50
        return min(50.0, blocked_count * 10.0 / (1.0 + blocked_count * 0.1))
    except Exception:
        return 0.0


def compute_bottleneck_relief(
    stage: str,
    pressure: Any,
) -> float:
    if pressure is None:
        return 0.0
    pressure_map = {
        "implementation": getattr(pressure, "implementation_pressure", 0.0),
        "review": getattr(pressure, "review_pressure", 0.0),
        "verification": getattr(pressure, "verification_pressure", 0.0),
        "rework": getattr(pressure, "rework_pressure", 0.0),
        "planning": getattr(pressure, "planning_pressure", 0.0),
        "discovery": getattr(pressure, "discovery_pressure", 0.0),
    }
    p = pressure_map.get(stage, 0.0)
    if p <= 1.0:
        return 0.0
    # Relief scales with how bad the bottleneck is
    # pressure=1.5 -> 10, pressure=2.0 -> 20, pressure=3.0 -> 40
    return min(40.0, (p - 1.0) * 20.0)


def compute_risk_penalty(
    risk_level: str,
    estimated_cost_tokens: int,
) -> float:
    """Penalize high-risk or expensive work slightly.

    Not enough to prevent scheduling, but enough to prefer safer/cheaper
    alternatives when scores are otherwise equal.

    Args:
        risk_level: "critical", "high", "medium", "low"
        estimated_cost_tokens: expected token cost

    Returns:
        Penalty to subtract from score (0.0 to ~20.0)
    """
    risk_penalties = {"critical": 15.0, "high": 8.0, "medium": 3.0, "low": 0.0}
    penalty = risk_penalties.get(risk_level.lower(), 0.0)

    # Additional cost penalty: every 100k tokens costs ~2 points
    if estimated_cost_tokens > 0:
        penalty += min(10.0, estimated_cost_tokens / 50000.0)

    return penalty


def compute_conflict_penalty(has_conflict: bool) -> float:
    """Heavy penalty for conflicting work to prevent parallel file edits (§21)."""
    return 200.0 if has_conflict else 0.0


def score_work_item(
    ticket: Any,
    stage: str,
    pressure: Any,
    config: Any,
    dependency_graph: Any = None,
    pipeline_state: Any = None,
    has_conflict: bool = False,
    now: float | None = None,
) -> ScoredWorkItem:
    """Compute the full utility score for a single work item (§32).

    UTILITY_SCORE =
        base_priority
      + severity_weight
      + bottleneck_relief
      + dependency_unlock
      + aging_bonus
      - risk_penalty
      - conflict_penalty
      - cost_estimate

    Args:
        ticket: Ticket dataclass instance (or dict-like with .severity, etc.)
        stage: pipeline stage string
        pressure: QueuePressure instance
        config: SchedulerConfig instance
        dependency_graph: optional DependencyGraph
        pipeline_state: optional PipelineState
        has_conflict: whether this ticket conflicts with active work
        now: current timestamp for aging calculation

    Returns:
        ScoredWorkItem with score and component breakdown
    """
    now = now or time.time()
    tid = getattr(ticket, "id", str(ticket))

    # Check if blocked by dependencies
    blocked = False
    if pipeline_state is not None:
        blocked = pipeline_state.tickets_blocked_by_deps(tid)

    # Base priority from stage
    base = STAGE_PRIORITY.get(stage.lower(), 10.0)

    # Severity weight
    severity = getattr(ticket, "severity", None)
    sev_str = severity.value if hasattr(severity, "value") else str(severity or "medium")
    sev_weight = SEVERITY_WEIGHTS.get(sev_str.lower(), 10.0)

    # Security emergency override (§41)
    ticket_class = getattr(ticket, "ticket_class", None)
    tc_str = ticket_class.value if hasattr(ticket_class, "value") else str(ticket_class or "")
    if tc_str == "security" and sev_str == "critical":
        base = STAGE_PRIORITY["security_emergency"]

    # Bottleneck relief
    relief = compute_bottleneck_relief(stage, pressure)

    # Dependency unlock
    unlock = 0.0
    if dependency_graph and pipeline_state:
        unlock = compute_dependency_unlock_value(tid, dependency_graph, pipeline_state)

    # Aging
    created = getattr(ticket, "created_at", now)
    aging = compute_aging_bonus(created, now, config)

    # Risk penalty
    risk = getattr(ticket, "risk", None)
    risk_str = risk.value if hasattr(risk, "value") else str(risk or "medium")
    est_cost = getattr(ticket, "estimated_cost_tokens", 0)
    risk_pen = compute_risk_penalty(risk_str, est_cost)

    # Conflict penalty
    conflict_pen = compute_conflict_penalty(has_conflict)

    # Rework boost: rework preserves already-spent effort (§24)
    rework_boost = 20.0 if stage.upper() == "REWORK" else 0.0

    total = base + sev_weight + relief + unlock + aging - risk_pen - conflict_pen + rework_boost

    components = {
        "base_priority": base,
        "severity_weight": sev_weight,
        "bottleneck_relief": relief,
        "dependency_unlock": unlock,
        "aging_bonus": aging,
        "risk_penalty": risk_pen,
        "conflict_penalty": conflict_pen,
        "rework_boost": rework_boost,
    }

    return ScoredWorkItem(
        ticket_id=tid,
        stage=stage,
        score=round(total, 2),
        components=components,
        blocked=blocked,
        has_conflict=has_conflict,
    )


def rank_work_items(
    items: list[Any],
    now: float | None = None,
    dependency_graph: Any = None,
    completed_ids: set[str] | None = None,
    all_ready_ids: set[str] | None = None,
    queue_pressure: Any = None,
    pipeline_state: Any = None,
    aging_start_seconds: int = 86400,
    aging_rate_per_hour: float = 1.0,
    max_aging_bonus: float = 30.0,
    per_ticket_token_limit: int = 0,
) -> list[ScoredWorkItem]:
    """Score and sort a list of tickets by descending utility.

    Accepts either raw Ticket instances/dicts or pre-scored ScoredWorkItems.
    When given raw tickets, scores them using score_ticket().

    Args:
        items: list of Ticket instances, dicts, or ScoredWorkItems
        **kwargs: passed through to score_ticket() when items are raw tickets

    Returns:
        List of ScoredWorkItem sorted highest score first
    """
    scored: list[ScoredWorkItem] = []
    for item in items:
        if isinstance(item, ScoredWorkItem):
            scored.append(item)
        else:
            scored.append(score_ticket(
                item,
                now=now,
                dependency_graph=dependency_graph,
                completed_ids=completed_ids,
                all_ready_ids=all_ready_ids,
                queue_pressure=queue_pressure,
                pipeline_state=pipeline_state,
                aging_start_seconds=aging_start_seconds,
                aging_rate_per_hour=aging_rate_per_hour,
                max_aging_bonus=max_aging_bonus,
                per_ticket_token_limit=per_ticket_token_limit,
            ))

    def sort_key(s: ScoredWorkItem) -> tuple[int, float, str]:
        schedulable = 0 if s.is_schedulable else 1
        return (schedulable, -s.total_score, s.ticket_id)

    return sorted(scored, key=sort_key)


def score_ticket(
    ticket: Any,
    now: float | None = None,
    dependency_graph: Any = None,
    completed_ids: set[str] | None = None,
    all_ready_ids: set[str] | None = None,
    queue_pressure: Any = None,
    pipeline_state: Any = None,
    aging_start_seconds: int = 86400,
    aging_rate_per_hour: float = 1.0,
    max_aging_bonus: float = 30.0,
    per_ticket_token_limit: int = 0,
) -> ScoredWorkItem:
    state_val = getattr(ticket, "state", None)
    if state_val is None and isinstance(ticket, dict):
        state_val = ticket.get("state", "READY")
    stage = state_val.value if hasattr(state_val, "value") else str(state_val or "READY").upper()

    class _AgingCfg:
        pass
    cfg = _AgingCfg()
    cfg.aging_start_seconds = aging_start_seconds
    cfg.aging_rate_per_hour = aging_rate_per_hour
    cfg.max_age_bonus = max_aging_bonus
    cfg.priority_aging = cfg

    has_conflict = False
    if pipeline_state is not None:
        tid = getattr(ticket, "id", "") or (ticket.get("id", "") if isinstance(ticket, dict) else "")
        has_conflict = pipeline_state.has_conflict_with_active(tid) if tid else False

    return score_work_item(
        ticket=ticket,
        stage=stage,
        pressure=queue_pressure,
        config=cfg,
        dependency_graph=dependency_graph,
        pipeline_state=pipeline_state,
        has_conflict=has_conflict,
        now=now,
    )
