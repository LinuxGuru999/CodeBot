#!/usr/bin/env python3
"""Queue pressure calculator and bottleneck detector for the adaptive scheduler.

Purpose
-------
Computes pressure ratios for each pipeline stage by comparing queue depth
against available capacity. The scheduler uses these pressures to determine
which stage is starving or bottlenecking, driving dynamic slot allocation.

Why
---
Spec §6 requires explicit queue-pressure measurements so the scheduler can
answer: "Which stage is currently starving or bottlenecking the pipeline?"
Without pressure signals, allocation is blind — you get 30 implementers
and 0 reviewers, or discovery running while review backlog explodes.

Invariants
----------
- stdlib-only (dataclasses)
- Pure functions: no I/O, no side effects
- Pressure values are floats >= 0.0; >1.0 means demand exceeds capacity
- Bottleneck detection is deterministic given the same PipelineState
- Capacity denominators are guarded against zero (minimum 1)
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class SchedulerMode(str, Enum):
    """Observable scheduler states (§35)."""
    DISCOVERY_HEAVY = "DISCOVERY_HEAVY"
    IMPLEMENTATION_HEAVY = "IMPLEMENTATION_HEAVY"
    REVIEW_HEAVY = "REVIEW_HEAVY"
    VERIFICATION_HEAVY = "VERIFICATION_HEAVY"
    REWORK_HEAVY = "REWORK_HEAVY"
    BALANCED = "BALANCED"
    RECOVERY = "RECOVERY"
    BUDGET_THROTTLED = "BUDGET_THROTTLED"
    SATURATED = "SATURATED"
    PROJECT_SATURATED = "PROJECT_SATURATED"
    IDLE = "IDLE"


@dataclass(frozen=True)
class QueuePressure:
    """Pressure ratios for every pipeline stage.

    A pressure of 1.0 means demand exactly matches capacity.
    Above 1.0 means the stage is congested (bottleneck).
    Below 1.0 means the stage has spare capacity.
    0.0 means no demand at that stage.
    """
    implementation_pressure: float = 0.0
    review_pressure: float = 0.0
    verification_pressure: float = 0.0
    rework_pressure: float = 0.0
    planning_pressure: float = 0.0
    discovery_pressure: float = 0.0
    integration_pressure: float = 0.0

    # Backlog health relative to watermarks
    backlog_ratio: float = 0.0

    @property
    def max_pressure(self) -> float:
        return max(
            self.implementation_pressure,
            self.review_pressure,
            self.verification_pressure,
            self.rework_pressure,
            self.planning_pressure,
            self.integration_pressure,
        )

    @property
    def bottleneck_stage(self) -> str:
        """Return the name of the most congested stage."""
        stages = {
            "implementation": self.implementation_pressure,
            "review": self.review_pressure,
            "verification": self.verification_pressure,
            "rework": self.rework_pressure,
            "planning": self.planning_pressure,
            "integration": self.integration_pressure,
        }
        if not any(v > 0 for v in stages.values()):
            return "none"
        return max(stages, key=lambda k: stages[k])

    def to_dict(self) -> dict[str, Any]:
        return {
            "implementation": round(self.implementation_pressure, 3),
            "review": round(self.review_pressure, 3),
            "verification": round(self.verification_pressure, 3),
            "rework": round(self.rework_pressure, 3),
            "planning": round(self.planning_pressure, 3),
            "discovery": round(self.discovery_pressure, 3),
            "integration": round(self.integration_pressure, 3),
            "backlog_ratio": round(self.backlog_ratio, 3),
            "bottleneck": self.bottleneck_stage,
        }


def calculate_pressure(
    pipeline_state: Any = None,
    config: Any = None,
    *,
    ready_count: int | None = None,
    implementing_count: int | None = None,
    reviewing_count: int | None = None,
    verifying_count: int | None = None,
    rework_count: int | None = None,
    planning_count: int | None = None,
    candidate_count: int | None = None,
    integration_queue_count: int | None = None,
    active_by_role: dict[str, int] | None = None,
    total_slots: int | None = None,
    backlog_low_watermark: int = 20,
    backlog_target: int = 50,
    backlog_high_watermark: int = 100,
) -> QueuePressure:
    if pipeline_state is not None and ready_count is None:
        ps = pipeline_state
        ready_count = ps.ready_count
        implementing_count = ps.implementing_count
        reviewing_count = ps.reviewing_count
        verifying_count = ps.verifying_count
        rework_count = ps.rework_count
        planning_count = ps.planning_count
        candidate_count = ps.candidate_count
        integration_queue_count = getattr(ps, "integration_queue_depth", getattr(ps, "integration_queue_count", 0))
        total_slots = getattr(ps, "total_slots", getattr(ps, "max_slots", 30))
        cats = ps.workers_by_category() if callable(getattr(ps, "workers_by_category", None)) else getattr(ps, "workers_by_category", {})
        if isinstance(cats, dict):
            active_by_role = cats
        else:
            active_by_role = ps.active_by_role() if callable(getattr(ps, "active_by_role", None)) else {}
        if config is not None:
            backlog_low_watermark = getattr(config.backlog, "low_watermark", getattr(config.backlog, "low", 20))
            backlog_target = getattr(config.backlog, "target", 50)

    ready_count = ready_count or 0
    implementing_count = implementing_count or 0
    reviewing_count = reviewing_count or 0
    verifying_count = verifying_count or 0
    rework_count = rework_count or 0
    planning_count = planning_count or 0
    candidate_count = candidate_count or 0
    integration_queue_count = integration_queue_count or 0
    active_by_role = active_by_role or {}
    total = max(total_slots or 30, 1)
    free = max(0, total - sum(active_by_role.values()))

    impl_active = sum(v for k, v in active_by_role.items() if "implementer" in k)
    review_active = sum(v for k, v in active_by_role.items() if "reviewer" in k)
    verify_active = sum(v for k, v in active_by_role.items() if k in ("quality_gate", "verifier"))
    planning_active = sum(v for k, v in active_by_role.items() if k in ("planner", "validator"))

    impl_capacity = max(impl_active + free, 1)
    impl_demand = ready_count + rework_count
    impl_pressure = impl_demand / impl_capacity if impl_demand > 0 else 0.0

    review_demand = reviewing_count + implementing_count
    review_capacity = max(review_active + free, 1)
    review_pressure = review_demand / review_capacity if review_demand > 0 else 0.0

    verify_demand = verifying_count
    verify_capacity = max(verify_active + free, 1)
    verify_pressure = verify_demand / verify_capacity if verify_demand > 0 else 0.0

    rework_demand = rework_count
    rework_capacity = max(total, 1)
    rework_pressure = rework_demand / rework_capacity if rework_demand > 0 else 0.0

    planning_demand = planning_count
    planning_capacity = max(planning_active + free, 1)
    planning_pressure = planning_demand / planning_capacity if planning_demand > 0 else 0.0

    backlog = ready_count + planning_count
    low = backlog_low_watermark
    target = backlog_target
    high = backlog_high_watermark

    if backlog >= high:
        discovery_pressure = 0.0
        backlog_ratio = 1.0
    elif backlog >= target:
        backlog_ratio = (backlog - target) / max(high - target, 1)
        discovery_pressure = 0.1
    elif backlog >= low:
        backlog_ratio = (backlog - low) / max(target - low, 1) * 0.5
        discovery_pressure = 0.5 + 0.3 * (1.0 - backlog_ratio)
    else:
        backlog_ratio = backlog / max(low, 1) * 0.25 if low > 0 else 0.0
        discovery_pressure = 1.0 + (low - backlog) / max(low, 1)

    integration_capacity = max(total // 2, 1)
    integration_pressure = integration_queue_count / integration_capacity if integration_queue_count > 0 else 0.0

    return QueuePressure(
        implementation_pressure=round(max(0.0, impl_pressure), 4),
        review_pressure=round(max(0.0, review_pressure), 4),
        verification_pressure=round(max(0.0, verify_pressure), 4),
        rework_pressure=round(max(0.0, rework_pressure), 4),
        planning_pressure=round(max(0.0, planning_pressure), 4),
        discovery_pressure=round(max(0.0, discovery_pressure), 4),
        integration_pressure=round(max(0.0, min(integration_pressure, 5.0)), 4),
        backlog_ratio=round(max(0.0, min(backlog_ratio, 1.0)), 4),
    )


def determine_mode(
    pressure: QueuePressure,
    pipeline_state: Any,
    budget_exhausted: bool = False,
) -> SchedulerMode:
    """Determine the current scheduler mode from pressure signals (§35).

    Args:
        pressure: computed QueuePressure
        pipeline_state: current PipelineState
        budget_exhausted: whether cost budget is depleted

    Returns:
        SchedulerMode enum value
    """
    if budget_exhausted:
        return SchedulerMode.BUDGET_THROTTLED

    _ac = getattr(pipeline_state, "active_count", getattr(pipeline_state, "total_active", 0))
    _sc = getattr(pipeline_state, "stuck_count", len(getattr(pipeline_state, "stuck_workers", ())))
    _ts = getattr(pipeline_state, "total_slots", getattr(pipeline_state, "max_slots", 30))
    total_work = (
        getattr(pipeline_state, "ready_count", 0)
        + getattr(pipeline_state, "implementing_count", 0)
        + getattr(pipeline_state, "reviewing_count", 0)
        + getattr(pipeline_state, "verifying_count", 0)
        + getattr(pipeline_state, "rework_count", 0)
        + getattr(pipeline_state, "planning_count", 0)
        + getattr(pipeline_state, "discovered_count", 0)
        + getattr(pipeline_state, "candidate_count", 0)
    )

    if _sc > _ts // 3:
        return SchedulerMode.RECOVERY

    if total_work == 0 and _ac == 0:
        if pressure.discovery_pressure >= 1.0:
            return SchedulerMode.DISCOVERY_HEAVY
        return SchedulerMode.IDLE

    if total_work == 0 and _ac > 0:
        return SchedulerMode.PROJECT_SATURATED

    bp = pressure.bottleneck_stage

    if pressure.review_pressure >= 1.5 and pressure.review_pressure >= pressure.max_pressure * 0.8:
        return SchedulerMode.REVIEW_HEAVY

    if pressure.verification_pressure >= 1.5 and pressure.verification_pressure >= pressure.max_pressure * 0.8:
        return SchedulerMode.VERIFICATION_HEAVY

    if pressure.rework_pressure >= 1.0 and pressure.rework_pressure >= pressure.max_pressure * 0.7:
        return SchedulerMode.REWORK_HEAVY

    if pressure.discovery_pressure >= 1.0 and pressure.implementation_pressure < 0.5:
        return SchedulerMode.DISCOVERY_HEAVY

    if pressure.implementation_pressure >= 1.0 and pressure.review_pressure < 1.0:
        return SchedulerMode.IMPLEMENTATION_HEAVY

    return SchedulerMode.BALANCED


def forecast_downstream_demand(
    pipeline_state: Any,
    avg_review_per_impl: float = 1.5,
    avg_verify_per_review: float = 0.3,
    avg_rework_rate: float = 0.15,
) -> dict[str, int]:
    """Estimate downstream work generated by current implementations (§34).

    Args:
        pipeline_state: current PipelineState
        avg_review_per_impl: expected review tasks per implementation
        avg_verify_per_review: expected verification tasks per review
        avg_rework_rate: fraction of reviews that produce rework

    Returns:
        Dict with estimated counts for each downstream stage
    """
    impl_in_flight = pipeline_state.implementing_count
    est_reviews = int(impl_in_flight * avg_review_per_impl)
    est_verifications = int(est_reviews * avg_verify_per_review)
    est_reworks = int(est_reviews * avg_rework_rate)

    return {
        "expected_reviews": est_reviews,
        "expected_verifications": est_verifications,
        "expected_reworks": est_reworks,
        "total_downstream": est_reviews + est_verifications + est_reworks,
    }


def estimate_downstream_demand(
    implementing_count: int,
    reviewing_count: int,
    first_pass_rate: float = 0.7,
    security_review_fraction: float = 0.2,
) -> dict[str, float]:
    """Forecast downstream review/verification/rework demand (§34).

    Every implementation creates review demand. Some reviews create rework.
    This estimates what's coming so the scheduler doesn't start more
    implementations than the downstream pipeline can absorb.

    Args:
        implementing_count: currently active implementations
        reviewing_count: currently in review
        first_pass_rate: estimated fraction passing review on first try
        security_review_fraction: fraction needing additional security review

    Returns:
        Dict with expected_review_demand, expected_security_reviews,
        expected_reworks, expected_verifications
    """
    incoming_reviews = implementing_count * 1.0
    total_reviews = reviewing_count + incoming_reviews
    security_reviews = total_reviews * security_review_fraction
    expected_reworks = total_reviews * (1.0 - first_pass_rate)
    expected_verifications = total_reviews * first_pass_rate

    return {
        "expected_review_demand": round(total_reviews, 1),
        "expected_security_reviews": round(security_reviews, 1),
        "expected_reworks": round(expected_reworks, 1),
        "expected_verifications": round(expected_verifications, 1),
    }


def compute_implementation_cap(
    review_capacity: int,
    verification_capacity: int,
    current_reviewing: int,
    current_verifying: int,
    first_pass_rate: float = 0.7,
    max_impl_fraction: float = 0.60,
    total_slots: int = 30,
) -> int:
    """Determine how many new implementations can safely start (§16).

    Implementation throughput must be bounded by review throughput.
    If review is saturated, starting more implementations just creates
    an unreviewed pile.

    Args:
        review_capacity: number of active reviewer slots
        verification_capacity: number of active verifier slots
        current_reviewing: tickets already in review
        current_verifying: tickets already in verification
        first_pass_rate: estimated pass rate
        max_impl_fraction: hard cap on implementation as fraction of total
        total_slots: max concurrent slots

    Returns:
        Maximum number of new implementation slots to allocate
    """
    hard_cap = int(total_slots * max_impl_fraction)
    review_headroom = max(0, review_capacity * 2 - current_reviewing)
    verify_headroom = max(0, verification_capacity * 2 - current_verifying)
    reviews_per_impl = 1.0 + (1.0 - first_pass_rate)
    review_bound = int(review_headroom / max(reviews_per_impl, 0.1))
    verify_bound = int(verify_headroom / max(first_pass_rate, 0.1))
    # Minimum floor: always allow at least 1 implementation if there are ready tickets
    # This prevents chicken-and-egg where no reviewers exist yet
    result = min(hard_cap, max(1, review_bound), max(1, verify_bound))
    return max(0, result)


# Alias used by adaptive_scheduler.py
determine_scheduler_mode = determine_mode


# Alias used by adaptive_scheduler.py
determine_scheduler_mode = determine_mode


def estimate_downstream_demand(
    implementing_count: int,
    reviewing_count: int,
    first_pass_rate: float = 0.7,
    security_review_fraction: float = 0.2,
) -> dict[str, float]:
    """Forecast downstream review/verification/rework demand (§34).

    Every implementation creates review demand. Some reviews create rework.
    This estimates what's coming so the scheduler doesn't start more
    implementations than the downstream pipeline can absorb.

    Args:
        implementing_count: currently active implementations
        reviewing_count: currently in review
        first_pass_rate: estimated fraction passing review on first try
        security_review_fraction: fraction needing additional security review

    Returns:
        Dict with expected_review_demand, expected_security_reviews,
        expected_reworks, expected_verifications
    """
    incoming_reviews = implementing_count * 1.0
    total_reviews = reviewing_count + incoming_reviews
    security_reviews = total_reviews * security_review_fraction
    expected_reworks = total_reviews * (1.0 - first_pass_rate)
    expected_verifications = total_reviews * first_pass_rate

    return {
        "expected_review_demand": round(total_reviews, 1),
        "expected_security_reviews": round(security_reviews, 1),
        "expected_reworks": round(expected_reworks, 1),
        "expected_verifications": round(expected_verifications, 1),
    }


def compute_implementation_cap(
    review_capacity: int,
    verification_capacity: int,
    current_reviewing: int,
    current_verifying: int,
    first_pass_rate: float = 0.7,
    max_impl_fraction: float = 0.60,
    total_slots: int = 30,
) -> int:
    """Determine how many new implementations can safely start (§16).

    Implementation throughput must be bounded by review throughput.
    If review is saturated, starting more implementations just creates
    an unreviewed pile.

    Args:
        review_capacity: number of active reviewer slots
        verification_capacity: number of active verifier slots
        current_reviewing: tickets already in review
        current_verifying: tickets already in verification
        first_pass_rate: estimated pass rate
        max_impl_fraction: hard cap on implementation as fraction of total
        total_slots: max concurrent slots

    Returns:
        Maximum number of new implementation slots to allocate
    """
    hard_cap = int(total_slots * max_impl_fraction)

    review_headroom = max(0, review_capacity * 2 - current_reviewing)
    verify_headroom = max(0, verification_capacity * 2 - current_verifying)

    reviews_per_impl = 1.0 + (1.0 - first_pass_rate)

    review_bound = int(review_headroom / max(reviews_per_impl, 0.1))
    verify_bound = int(verify_headroom / max(first_pass_rate, 0.1))

    bounded = min(hard_cap, max(0, review_bound), max(0, verify_bound))

    # Minimum floor: always allow at least 1 implementation when ready tickets exist
    # and no review congestion is present. Prevents chicken-and-egg where
    # zero reviewers means zero implementations means zero reviews forever.
    if bounded == 0 and current_reviewing < review_capacity + 2:
        bounded = min(3, hard_cap)

    return bounded
