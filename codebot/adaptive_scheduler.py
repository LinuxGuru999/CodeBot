#!/usr/bin/env python3
"""Adaptive Concurrency Scheduler for CodeBot.

Purpose
-------
Implements the 30-slot shared worker pool scheduler described in the spec.
Dynamically allocates slots across rework, verification, review,
implementation, planning, validation, and discovery based on real-time
pipeline pressure, backlog depth, dependency constraints, conflict
detection, cost budgets, and historical yield data.

Why
---
The orchestrator uses a single GATEWAY_MAX_CONCURRENT limit (default 30)
with no granular caps per role. This module replaces static allocation
with a self-balancing pipeline that answers: "Which allocation of my 30
workers will move the most legitimate engineering work toward VERIFIED
completion without compromising security, quality, or budget?"

Invariants
----------
- stdlib-only (dataclasses, time, json, logging)
- Pure allocation logic: tick() returns decisions, doesn't spawn processes
- max_slots is enforced globally at every step (§1)
- Implementers never review their own work (§18)
- Blocked tickets never execute (§20)
- Conflicting tickets are serialized (§21)
- Discovery never modifies production code (§8)
- Cost budgets are hard limits, not suggestions (§28)
- Hysteresis prevents oscillation (§36)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from codebot.scheduler_config import SchedulerConfig
from codebot.pipeline_state import PipelineState, WorkerSlot
from codebot.queue_pressure import (
    QueuePressure,
    SchedulerMode,
    calculate_pressure,
    determine_mode,
    determine_scheduler_mode,
    compute_implementation_cap,
    estimate_downstream_demand,
)
from codebot.work_scorer import score_ticket, rank_work_items, ScoredWorkItem
from codebot.discovery_manager import (
    DiscoveryManager,
    DiscoveryAllocation,
    DISCOVERY_ROLES,
)
from codebot.conflict_detector import (
    ConflictMatrix,
    filter_non_conflicting,
)

logger = logging.getLogger("adaptive_scheduler")

# Role classification helpers
IMPLEMENTATION_ROLES = frozenset({
    "general_implementer", "backend_implementer", "frontend_implementer",
    "test_implementer", "migration_implementer", "documentation_implementer",
})
REVIEW_ROLES = frozenset({
    "correctness_reviewer", "security_reviewer", "architecture_reviewer",
    "test_reviewer", "performance_reviewer", "simplicity_reviewer",
    "documentation_reviewer",
})
VERIFICATION_ROLES = frozenset({"quality_gate", "verifier"})
PLANNING_ROLES = frozenset({"planner", "validator"})


@dataclass(frozen=True)
class SlotAssignment:
    """One slot's assignment for the current tick."""
    role: str
    ticket_id: str
    model_profile: str  # "cheap", "standard", "premium"
    priority: int
    reason: str


@dataclass(frozen=True)
class SchedulerDecision:
    """Complete output of one scheduler tick."""
    assignments: tuple[SlotAssignment, ...]
    mode: SchedulerMode
    pressure: QueuePressure
    free_slots_remaining: int
    total_active_after: int
    reasons: tuple[str, ...]
    timestamp: float

    def summary(self) -> dict[str, Any]:
        by_role: dict[str, int] = {}
        for a in self.assignments:
            by_role[a.role] = by_role.get(a.role, 0) + 1
        return {
            "mode": self.mode.value,
            "total_assigned": len(self.assignments),
            "free_remaining": self.free_slots_remaining,
            "by_role": by_role,
            "pressure": self.pressure.summary(),
            "reasons": list(self.reasons),
        }


@dataclass
class HysteresisState:
    """Tracks recent allocations to prevent oscillation (§36)."""
    last_mode: SchedulerMode = SchedulerMode.BALANCED
    last_allocation: dict[str, int] = field(default_factory=dict)
    last_change_time: float = 0.0
    rolling_pressures: list[dict[str, float]] = field(default_factory=list)

    def should_shift(
        self,
        new_mode: SchedulerMode,
        now: float,
        min_duration: int,
    ) -> bool:
        if new_mode == self.last_mode:
            return False
        elapsed = now - self.last_change_time
        return elapsed >= min_duration

    def record(self, mode: SchedulerMode, allocation: dict[str, int], now: float) -> None:
        if mode != self.last_mode:
            self.last_change_time = now
        self.last_mode = mode
        self.last_allocation = dict(allocation)

    def add_pressure_sample(self, pressure: QueuePressure) -> None:
        self.rolling_pressures.append(pressure.to_dict())
        if len(self.rolling_pressures) > 10:
            self.rolling_pressures = self.rolling_pressures[-10:]


class AdaptiveScheduler:
    """Core scheduler integrating all subsystems.

    Usage:
        scheduler = AdaptiveScheduler(config)
        decision = scheduler.tick(pipeline_state, tickets, dep_graph, conflict_matrix)
        # Apply decision.assignments to spawn workers
    """

    def __init__(
        self,
        config: SchedulerConfig | None = None,
        discovery_manager: DiscoveryManager | None = None,
    ) -> None:
        self.config = config or SchedulerConfig.default()
        self.discovery = discovery_manager or DiscoveryManager()
        self.hysteresis = HysteresisState()
        self.tick_count = 0
        self._completed_tickets: set[str] = set()
        self._reviewer_assignments: dict[str, str] = {}  # ticket_id -> reviewer_worker_id

    def tick(
        self,
        pipeline: PipelineState,
        ready_tickets: list[Any],
        review_tickets: list[Any],
        verify_tickets: list[Any],
        rework_tickets: list[Any],
        planning_tickets: list[Any],
        candidate_tickets: list[Any],
        conflict_matrix: ConflictMatrix | None = None,
        dependency_graph: Any = None,
        now: float | None = None,
    ) -> SchedulerDecision:
        """Execute one scheduler tick (§4).

        Returns a SchedulerDecision with slot assignments. Does NOT spawn
        workers — the caller applies the assignments.

        Algorithm (§43):
        STEP 1: Reserve required slots for rework, verification, review
        STEP 2: Allocate implementation bounded by review capacity
        STEP 3: Allocate planning/validation capacity
        STEP 4: Fill remaining slots with discovery
        STEP 5: If discovery backlog sufficient, shift back to execution
        """
        now = now or time.time()
        self.tick_count += 1
        reasons: list[str] = []
        assignments: list[SlotAssignment] = []

        # Budget gate (§28)
        if pipeline.budget_exhausted:
            mode = SchedulerMode.BUDGET_THROTTLED
            pressure = QueuePressure()
            decision = SchedulerDecision(
                assignments=tuple(assignments),
                mode=mode,
                pressure=pressure,
                free_slots_remaining=pipeline.total_slots,
                total_active_after=0,
                reasons=("budget exhausted",),
                timestamp=now,
            )
            return decision

        # Compute pressure
        active_by_role = pipeline.active_by_role()
        pressure = calculate_pressure(
            ready_count=pipeline.ready_count,
            implementing_count=pipeline.implementing_count,
            reviewing_count=pipeline.reviewing_count,
            verifying_count=pipeline.verifying_count,
            rework_count=pipeline.rework_count,
            planning_count=pipeline.planning_count,
            candidate_count=pipeline.candidate_count,
            integration_queue_count=getattr(pipeline, "integration_queue_count", getattr(pipeline, "integration_queue_depth", 0)),
            active_by_role=active_by_role,
            total_slots=pipeline.total_slots,
            backlog_low_watermark=self.config.backlog.low_watermark,
            backlog_target=self.config.backlog.target,
        )

        # Determine mode
        mode = determine_mode(
            pressure,
            pipeline,
            pipeline.budget_exhausted,
        )

        # Hysteresis check (§36)
        self.hysteresis.add_pressure_sample(pressure)
        if not self.hysteresis.should_shift(
            mode, now, self.config.hysteresis.min_allocation_duration_seconds
        ):
            mode = self.hysteresis.last_mode

        free_slots = pipeline.free_slots
        active_ticket_ids = {w.ticket_id for w in pipeline.active_workers}
        conflict_matrix = conflict_matrix or ConflictMatrix()

        # Security emergency override (§41)
        security_emergency = self._check_security_emergency(ready_tickets, rework_tickets)
        if security_emergency:
            emergency_assignments, slots_used = self._handle_security_emergency(
                security_emergency, free_slots, now, reasons
            )
            assignments.extend(emergency_assignments)
            free_slots -= slots_used

        # STEP 1: Reserve for rework, verification, review (§43)
        rework_assign, free_slots = self._allocate_rework(
            rework_tickets, free_slots, active_ticket_ids, conflict_matrix,
            pipeline, dependency_graph, now, reasons,
        )
        assignments.extend(rework_assign)

        verify_assign, free_slots = self._allocate_verification(
            verify_tickets, free_slots, active_ticket_ids, conflict_matrix,
            pipeline, dependency_graph, now, reasons,
        )
        assignments.extend(verify_assign)

        review_assign, free_slots = self._allocate_review(
            review_tickets, free_slots, active_ticket_ids, conflict_matrix,
            pipeline, dependency_graph, now, reasons,
        )
        assignments.extend(review_assign)

        # STEP 2: Implementation bounded by review capacity (§16)
        impl_cap = compute_implementation_cap(
            review_capacity=sum(1 for a in assignments if a.role in REVIEW_ROLES)
                          + sum(1 for w in pipeline.active_workers if w.role in REVIEW_ROLES),
            verification_capacity=sum(1 for a in assignments if a.role in VERIFICATION_ROLES)
                                  + sum(1 for w in pipeline.active_workers if w.role in VERIFICATION_ROLES),
            current_reviewing=pipeline.reviewing_count,
            current_verifying=pipeline.verifying_count,
            max_impl_fraction=self.config.implementation.maximum_fraction,
            total_slots=pipeline.total_slots,
        )
        # Don't exceed already-active implementers + cap
        current_impl = sum(1 for w in pipeline.active_workers if w.role in IMPLEMENTATION_ROLES)
        impl_budget = max(0, impl_cap - current_impl)

        impl_assign, free_slots = self._allocate_implementation(
            ready_tickets, min(free_slots, impl_budget), active_ticket_ids,
            conflict_matrix, pipeline, dependency_graph, now, reasons,
        )
        assignments.extend(impl_assign)

        # STEP 3: Planning / validation
        plan_assign, free_slots = self._allocate_planning(
            planning_tickets, free_slots, active_ticket_ids, conflict_matrix,
            pipeline, dependency_graph, now, reasons,
        )
        assignments.extend(plan_assign)

        # STEP 4 & 5: Discovery fill with diversity (§8, §9)
        if free_slots > 0:
            disc_alloc = self.discovery.compute_allocation(
                available_slots=free_slots,
                config=self.config,
                now=now,
            )
            if disc_alloc.is_saturated:
                reasons.append("project saturated — allowing idle slots")
                mode = SchedulerMode.PROJECT_SATURATED
            else:
                disc_assign, free_slots = self._apply_discovery_allocation(
                    disc_alloc, free_slots, candidate_tickets, now, reasons,
                )
                assignments.extend(disc_assign)

        # Enforce global max_slots (§1)
        if len(assignments) + pipeline.total_active > pipeline.total_slots:
            excess = len(assignments) + pipeline.total_active - pipeline.total_slots
            # Trim lowest-priority assignments from the end
            assignments = assignments[:-excess] if excess > 0 else assignments
            reasons.append(f"trimmed {excess} assignments to enforce max_slots={pipeline.total_slots}")
            free_slots = max(0, pipeline.total_slots - pipeline.active_count - len(assignments))

        # Record hysteresis
        alloc_summary: dict[str, int] = {}
        for a in assignments:
            cat = self._role_to_category(a.role)
            alloc_summary[cat] = alloc_summary.get(cat, 0) + 1
        self.hysteresis.record(mode, alloc_summary, now)

        return SchedulerDecision(
            assignments=tuple(assignments),
            mode=mode,
            pressure=pressure,
            free_slots_remaining=free_slots,
            total_active_after=pipeline.total_active + len(assignments),
            reasons=tuple(reasons),
            timestamp=now,
        )

    # --- Allocation steps ---

    def _allocate_rework(
        self, tickets: list[Any], free: int, active_ids: set[str],
        conflicts: ConflictMatrix, pipeline: PipelineState,
        dep_graph: Any, now: float, reasons: list[str],
    ) -> tuple[list[SlotAssignment], int]:
        """STEP 1a: Rework has highest priority after security emergencies (§24)."""
        if not tickets or free <= 0:
            return [], free

        scored = self._score_and_filter(
            tickets, "REWORK", pipeline, dep_graph, conflicts, active_ids, now
        )
        assignments: list[SlotAssignment] = []
        used = 0
        for item in scored:
            if used >= free:
                break
            role = self._pick_implementer_role(item, pipeline)
            assignments.append(SlotAssignment(
                role=role, ticket_id=item.ticket_id,
                model_profile="standard", priority=1,
                reason="rework priority",
            ))
            active_ids.add(item.ticket_id)
            used += 1

        if used > 0:
            reasons.append(f"allocated {used} rework slots")
        return assignments, free - used

    def _allocate_verification(
        self, tickets: list[Any], free: int, active_ids: set[str],
        conflicts: ConflictMatrix, pipeline: PipelineState,
        dep_graph: Any, now: float, reasons: list[str],
    ) -> tuple[list[SlotAssignment], int]:
        """STEP 1b: Verification minimum reservation (§19)."""
        if free <= 0:
            return [], free

        min_verify = self.config.verification.minimum_when_pending
        needed = max(min_verify, len(tickets)) if tickets else 0
        budget = min(free, needed)

        if budget <= 0 or not tickets:
            return [], free

        scored = self._score_and_filter(
            tickets, "VERIFYING", pipeline, dep_graph, conflicts, active_ids, now
        )
        assignments: list[SlotAssignment] = []
        used = 0
        for item in scored:
            if used >= budget:
                break
            assignments.append(SlotAssignment(
                role="quality_gate", ticket_id=item.ticket_id,
                model_profile="cheap", priority=2,
                reason="verification reservation",
            ))
            active_ids.add(item.ticket_id)
            used += 1

        if used > 0:
            reasons.append(f"allocated {used} verification slots")
        return assignments, free - used

    def _allocate_review(
        self, tickets: list[Any], free: int, active_ids: set[str],
        conflicts: ConflictMatrix, pipeline: PipelineState,
        dep_graph: Any, now: float, reasons: list[str],
    ) -> tuple[list[SlotAssignment], int]:
        """STEP 1c: Review minimum reservation + bottleneck response (§15, §16, §19)."""
        if free <= 0:
            return [], free

        min_review = self.config.review.minimum_when_pending
        needed = max(min_review, len(tickets)) if tickets else 0
        # Scale up when review is the bottleneck
        if pipeline.reviewing_count > pipeline.total_slots * 0.3:
            needed = min(free, needed + pipeline.reviewing_count // 3)
        budget = min(free, needed)

        if budget <= 0 or not tickets:
            return [], free

        scored = self._score_and_filter(
            tickets, "REVIEWING", pipeline, dep_graph, conflicts, active_ids, now
        )
        assignments: list[SlotAssignment] = []
        used = 0
        for item in scored:
            if used >= budget:
                break
            role = self._pick_reviewer_role(item, pipeline)
            # §18: reviewer independence
            if self._is_independence_violation(item.ticket_id, role, pipeline):
                continue
            assignments.append(SlotAssignment(
                role=role, ticket_id=item.ticket_id,
                model_profile="premium", priority=3,
                reason="review allocation",
            ))
            active_ids.add(item.ticket_id)
            used += 1

        if used > 0:
            reasons.append(f"allocated {used} review slots")
        return assignments, free - used

    def _allocate_implementation(
        self, tickets: list[Any], budget: int, active_ids: set[str],
        conflicts: ConflictMatrix, pipeline: PipelineState,
        dep_graph: Any, now: float, reasons: list[str],
    ) -> tuple[list[SlotAssignment], int]:
        """STEP 2: Implementation bounded by review capacity (§16)."""
        if budget <= 0 or not tickets:
            return [], budget

        scored = self._score_and_filter(
            tickets, "READY", pipeline, dep_graph, conflicts, active_ids, now
        )
        assignments: list[SlotAssignment] = []
        used = 0
        for item in scored:
            if used >= budget:
                break
            role = self._pick_implementer_role(item, pipeline)
            assignments.append(SlotAssignment(
                role=role, ticket_id=item.ticket_id,
                model_profile="standard", priority=4,
                reason="implementation from ready backlog",
            ))
            active_ids.add(item.ticket_id)
            used += 1

        if used > 0:
            reasons.append(f"allocated {used} implementation slots (budget={budget})")
        return assignments, budget - used

    def _allocate_planning(
        self, tickets: list[Any], free: int, active_ids: set[str],
        conflicts: ConflictMatrix, pipeline: PipelineState,
        dep_graph: Any, now: float, reasons: list[str],
    ) -> tuple[list[SlotAssignment], int]:
        """STEP 3: Planning and validation (§43)."""
        if free <= 0 or not tickets:
            return [], free

        budget = min(free, max(2, len(tickets)))
        scored = self._score_and_filter(
            tickets, "PLANNING", pipeline, dep_graph, conflicts, active_ids, now
        )
        assignments: list[SlotAssignment] = []
        used = 0
        for item in scored:
            if used >= budget:
                break
            assignments.append(SlotAssignment(
                role="planner", ticket_id=item.ticket_id,
                model_profile="standard", priority=5,
                reason="planning allocation",
            ))
            active_ids.add(item.ticket_id)
            used += 1

        if used > 0:
            reasons.append(f"allocated {used} planning slots")
        return assignments, free - used

    def _apply_discovery_allocation(
        self, alloc: DiscoveryAllocation, free: int,
        candidates: list[Any], now: float, reasons: list[str],
    ) -> tuple[list[SlotAssignment], int]:
        """STEP 4: Fill remaining slots with diverse discovery (§8, §9)."""
        assignments: list[SlotAssignment] = []
        used = 0
        for role, count in alloc.allocations.items():
            for _ in range(min(count, free - used)):
                if used >= free:
                    break
                cost_class = "cheap" if role in ("test_gap_auditor", "documentation_auditor", "dependency_auditor") else "standard"
                assignments.append(SlotAssignment(
                    role=role, ticket_id=f"discovery-{role}-{int(now)}-{used}",
                    model_profile=cost_class, priority=7,
                    reason=f"discovery fill ({alloc.reason})",
                ))
                used += 1

        if used > 0:
            reasons.append(f"allocated {used} discovery slots: {alloc.reason}")
        return assignments, free - used

    # --- Helper methods ---

    def _score_and_filter(
        self, tickets: list[Any], stage: str, pipeline: PipelineState,
        dep_graph: Any, conflicts: ConflictMatrix, active_ids: set[str],
        now: float,
    ) -> list[ScoredWorkItem]:
        """Score tickets and remove blocked/conflicting ones."""
        pressure = calculate_pressure(
            ready_count=pipeline.ready_count,
            implementing_count=pipeline.implementing_count,
            reviewing_count=pipeline.reviewing_count,
            verifying_count=pipeline.verifying_count,
            rework_count=pipeline.rework_count,
            planning_count=pipeline.planning_count,
            candidate_count=pipeline.candidate_count,
            integration_queue_count=getattr(pipeline, "integration_queue_count", getattr(pipeline, "integration_queue_depth", 0)),
            active_by_role=pipeline.active_by_role(),
            total_slots=pipeline.total_slots,
        )

        completed_ids = self._completed_tickets
        all_ids = {getattr(t, "id", "") for t in tickets if getattr(t, "id", "")}

        scored = []
        for t in tickets:
            tid = getattr(t, "id", "")
            if not tid or tid in active_ids:
                continue
            # §20: dependency blocking
            if pipeline.tickets_blocked_by_deps(tid):
                continue
            # §21: conflict detection
            has_conflict = conflicts.has_any_conflict(tid)
            item = score_ticket(
                t, now=now, dependency_graph=dep_graph,
                completed_ids=completed_ids, all_ready_ids=all_ids,
                queue_pressure=pressure, pipeline_state=pipeline,
                aging_start_seconds=self.config.priority_aging.aging_start_seconds,
                aging_rate_per_hour=self.config.priority_aging.aging_rate,
                max_aging_bonus=self.config.priority_aging.max_age_bonus,
            )
            if not has_conflict:
                scored.append(item)

        scored.sort(key=lambda s: s.total_score, reverse=True)
        return scored

    def _pick_implementer_role(self, item: ScoredWorkItem, pipeline: PipelineState) -> str:
        """Select appropriate implementer role based on ticket class."""
        # Default mapping; could be enhanced with ticket_class inspection
        return "general_implementer"

    def _pick_reviewer_role(self, item: ScoredWorkItem, pipeline: PipelineState) -> str:
        """Select appropriate reviewer role based on risk (§17)."""
        risk_pen = item.components.get("risk_penalty", 0)
        if risk_pen >= 15.0:
            return "security_reviewer"
        if risk_pen >= 8.0:
            return "architecture_reviewer"
        return "correctness_reviewer"

    def _is_independence_violation(
        self, ticket_id: str, reviewer_role: str, pipeline: PipelineState
    ) -> bool:
        """Check §18: implementer cannot review their own work."""
        for w in pipeline.active_workers:
            if w.ticket_id == ticket_id and w.role in IMPLEMENTATION_ROLES:
                if reviewer_role in REVIEW_ROLES:
                    # Same worker reviewing own implementation
                    return True
        return False

    def _check_security_emergency(
        self, ready_tickets: list[Any], rework_tickets: list[Any]
    ) -> Any | None:
        """Detect critical security tickets that override normal scheduling (§41)."""
        for t in rework_tickets + ready_tickets:
            tc = getattr(t, "ticket_class", None)
            sev = getattr(t, "severity", None)
            tc_str = tc.value if hasattr(tc, "value") else str(tc or "")
            sev_str = sev.value if hasattr(sev, "value") else str(sev or "")
            if tc_str == "security" and sev_str == "critical":
                return t
        return None

    def _handle_security_emergency(
        self, ticket: Any, free: int, now: float, reasons: list[str]
    ) -> tuple[list[SlotAssignment], int]:
        """Allocate dedicated capacity for critical security issue (§41)."""
        assignments: list[SlotAssignment] = []
        tid = getattr(ticket, "id", "security-emergency")
        used = 0
        if free >= 1:
            assignments.append(SlotAssignment(
                role="backend_implementer", ticket_id=tid,
                model_profile="premium", priority=0,
                reason="SECURITY EMERGENCY override",
            ))
            used += 1
        if free >= 2:
            assignments.append(SlotAssignment(
                role="security_reviewer", ticket_id=tid,
                model_profile="premium", priority=0,
                reason="SECURITY EMERGENCY independent review",
            ))
            used += 1
        reasons.append(f"SECURITY EMERGENCY: allocated {used} slots for {tid}")
        return assignments, used

    def _role_to_category(self, role: str) -> str:
        if role in IMPLEMENTATION_ROLES:
            return "implementation"
        if role in REVIEW_ROLES:
            return "review"
        if role in VERIFICATION_ROLES:
            return "verification"
        if role in PLANNING_ROLES:
            return "planning"
        if role in DISCOVERY_ROLES:
            return "discovery"
        return "control"

    def mark_completed(self, ticket_id: str) -> None:
        """Record a ticket as completed for dependency tracking."""
        self._completed_tickets.add(ticket_id)

    def format_dashboard(self, decision: SchedulerDecision, pipeline: PipelineState) -> str:
        """Generate human-readable dashboard output (§47)."""
        cats = {"implementation": 0, "review": 0, "verification": 0,
                "rework": 0, "planning": 0, "discovery": 0}
        for a in decision.assignments:
            cat = self._role_to_category(a.role)
            cats[cat] = cats.get(cat, 0) + 1

        lines = [
            "CODEBOT CAPACITY",
            "",
            f"Slots: {decision.total_active_after} / {pipeline.total_slots} productive",
            f"Current mode: {decision.mode.value}",
            "",
            "Allocation:",
            f"  Implementation  {cats['implementation']}",
            f"  Review          {cats['review']}",
            f"  Verification    {cats['verification']}",
            f"  Rework          {cats['rework']}",
            f"  Planning        {cats['planning']}",
            f"  Discovery       {cats['discovery']}",
            "",
            "Queues:",
            f"  Ready           {pipeline.ready_count}",
            f"  Review          {pipeline.reviewing_count}",
            f"  Verify          {pipeline.verifying_count}",
            f"  Rework          {pipeline.rework_count}",
            f"  Candidates      {pipeline.candidate_count}",
            "",
            f"Pressure: {decision.pressure.to_dict()}",
        ]
        if decision.reasons:
            lines.append("")
            lines.append("Decisions:")
            for r in decision.reasons:
                lines.append(f"  - {r}")
        return "\n".join(lines)
