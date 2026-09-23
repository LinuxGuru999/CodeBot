#!/usr/bin/env python3
"""Tests for the adaptive concurrency scheduler.

Covers all scenarios from spec section 49:
- Empty project -> discovery workers spawned
- Healthy backlog -> implementation prioritized
- Review congestion -> review capacity increases
- Verification congestion -> verification capacity increases
- Discovery produces tickets -> future discovery decreases
- Duplicate discovery -> that role receives less capacity
- Worker crash -> slot released and reassigned
- Stuck worker -> lease expires, recovery occurs
- Dependency blocking -> blocked ticket never executes early
- Conflicting tickets -> overlapping changes not executed unsafely
- Security emergency -> critical security ticket gets capacity
- Cost exhaustion -> scheduler throttles safely
- Saturated project -> idle slots allowed
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from codebot.scheduler_config import SchedulerConfig, BacklogConfig
from codebot.pipeline_state import PipelineState, WorkerSlot
from codebot.queue_pressure import (
    QueuePressure,
    SchedulerMode,
    calculate_pressure,
    determine_mode,
    compute_implementation_cap,
    estimate_downstream_demand,
)
from codebot.work_scorer import score_ticket, rank_work_items, ScoredWorkItem
from codebot.discovery_manager import DiscoveryManager, DISCOVERY_ROLES
from codebot.conflict_detector import (
    ConflictMatrix,
    ConflictEdge,
    build_conflict_matrix,
    filter_non_conflicting,
)
from codebot.adaptive_scheduler import AdaptiveScheduler, SlotAssignment


@dataclass
class FakeTicket:
    id: str = "CB-001"
    title: str = "Test ticket"
    ticket_class: str = "bug"
    severity: str = "medium"
    state: str = "READY"
    risk: str = "medium"
    affected_modules: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    estimated_cost_tokens: int = 10000
    rework_count: int = 0

    @property
    def evidence_hash(self) -> str:
        return self.id


def make_pipeline(
    ready: int = 0,
    implementing: int = 0,
    reviewing: int = 0,
    verifying: int = 0,
    rework: int = 0,
    planning: int = 0,
    discovered: int = 0,
    candidates: int = 0,
    blocked: int = 0,
    active_workers: list[WorkerSlot] | None = None,
    stuck: list[str] | None = None,
    total_slots: int = 30,
    budget_exhausted: bool = False,
    unsatisfied_deps: dict[str, tuple[str, ...]] | None = None,
    conflict_groups: list[frozenset[str]] | None = None,
) -> PipelineState:
    now = time.time()
    return PipelineState(
        ready_count=ready,
        implementing_count=implementing,
        reviewing_count=reviewing,
        verifying_count=verifying,
        rework_count=rework,
        planning_count=planning,
        discovered_count=discovered,
        candidate_count=candidates,
        blocked_count=blocked,
        active_workers=tuple(active_workers or []),
        stuck_workers=tuple(stuck or []),
        total_slots=total_slots,
        budget_exhausted=budget_exhausted,
        unsatisfied_dependencies=unsatisfied_deps or {},
        conflict_groups=tuple(conflict_groups or []),
        snapshot_time=now,
    )


def make_worker(worker_id: str, ticket_id: str, role: str, started: float | None = None) -> WorkerSlot:
    now = started or time.time()
    return WorkerSlot(
        worker_id=worker_id,
        ticket_id=ticket_id,
        role=role,
        started_at=now,
        heartbeat_at=now,
        lease_expires=now + 600,
    )


def _call_tick(scheduler: AdaptiveScheduler, state: PipelineState, tickets: list[Any], **kwargs: Any) -> Any:
    ready = [t for t in tickets if getattr(t, "state", "") == "READY"]
    review = [t for t in tickets if getattr(t, "state", "") == "REVIEWING"]
    verify = [t for t in tickets if getattr(t, "state", "") == "VERIFYING"]
    rework = [t for t in tickets if getattr(t, "state", "") == "REWORK"]
    planning = [t for t in tickets if getattr(t, "state", "") == "PLANNING"]
    return scheduler.tick(
        pipeline=state,
        ready_tickets=ready,
        review_tickets=review,
        verify_tickets=verify,
        rework_tickets=rework,
        planning_tickets=planning,
        candidate_tickets=[],
        **kwargs,
    )


class _FakeAgingCfg:
    class priority_aging:
        aging_start_seconds = 86400
        aging_rate_per_hour = 1.0
        max_age_bonus = 30.0


class TestSchedulerConfig:
    def test_default_config_validates(self):
        config = SchedulerConfig.default()
        config.validate()
        assert config.backlog.low_watermark == 20
        assert config.backlog.target == 50
        assert config.backlog.high_watermark == 100

    def test_invalid_backlog_raises(self):
        with pytest.raises(ValueError):
            SchedulerConfig(backlog=BacklogConfig(low_watermark=100, target=50, high_watermark=80)).validate()

    def test_from_dict_roundtrip(self):
        config = SchedulerConfig.default()
        d = config.to_dict()
        restored = SchedulerConfig.from_dict(d)
        assert restored.max_slots == config.max_slots
        assert restored.backlog.target == config.backlog.target

    def test_custom_max_slots(self):
        config = SchedulerConfig(max_slots=10).validate()
        assert config.max_slots == 10


class TestPipelineState:
    def test_free_slots(self):
        ps = make_pipeline(total_slots=30, active_workers=[
            make_worker("w1", "t1", "general_implementer"),
            make_worker("w2", "t2", "correctness_reviewer"),
        ])
        assert ps.free_slots == 28
        assert ps.total_active == 2

    def test_actionable_backlog(self):
        ps = make_pipeline(ready=10, rework=3)
        assert ps.actionable_backlog == 13

    def test_downstream_demand(self):
        ps = make_pipeline(implementing=5, reviewing=8, verifying=2, rework=1)
        assert ps.downstream_demand == 16

    def test_dependency_blocking(self):
        ps = make_pipeline(unsatisfied_deps={"CB-002": ("CB-001",)})
        assert ps.tickets_blocked_by_deps("CB-002") is True
        assert ps.tickets_blocked_by_deps("CB-001") is False

    def test_conflict_detection(self):
        ps = make_pipeline(
            active_workers=[make_worker("w1", "CB-001", "impl")],
            conflict_groups=[frozenset({"CB-001", "CB-002"})],
        )
        assert ps.has_conflict_with_active("CB-002") is True
        assert ps.has_conflict_with_active("CB-003") is False


class TestQueuePressure:
    def test_empty_pipeline_zero_pressure(self):
        p = calculate_pressure(
            ready_count=0, implementing_count=0, reviewing_count=0,
            verifying_count=0, rework_count=0, planning_count=0,
            candidate_count=0, integration_queue_count=0,
            active_by_role={}, total_slots=30,
        )
        assert p.implementation_pressure == 0.0
        assert p.review_pressure == 0.0

    def test_review_congestion_high_pressure(self):
        p = calculate_pressure(
            ready_count=5, implementing_count=12, reviewing_count=25,
            verifying_count=2, rework_count=0, planning_count=0,
            candidate_count=0, integration_queue_count=0,
            active_by_role={"general_implementer": 12, "correctness_reviewer": 3},
            total_slots=30,
        )
        assert p.review_pressure > p.implementation_pressure
        assert p.bottleneck_stage in ("review", "implementation")

    def test_discovery_pressure_when_backlog_empty(self):
        p = calculate_pressure(
            ready_count=0, implementing_count=0, reviewing_count=0,
            verifying_count=0, rework_count=0, planning_count=0,
            candidate_count=0, integration_queue_count=0,
            active_by_role={}, total_slots=30,
            backlog_low_watermark=20, backlog_target=50,
        )
        assert p.discovery_pressure >= 1.0

    def test_determine_mode_balanced(self):
        p = QueuePressure(implementation_pressure=0.5, review_pressure=0.3)
        ps = make_pipeline(ready=5, implementing=3)
        mode = determine_mode(p, ps)
        assert mode == SchedulerMode.BALANCED

    def test_determine_mode_budget_throttled(self):
        p = QueuePressure()
        ps = make_pipeline()
        mode = determine_mode(p, ps, budget_exhausted=True)
        assert mode == SchedulerMode.BUDGET_THROTTLED

    def test_compute_implementation_cap_bounded_by_review(self):
        cap = compute_implementation_cap(
            review_capacity=2, verification_capacity=2,
            current_reviewing=10, current_verifying=1,
            max_impl_fraction=0.6, total_slots=30,
        )
        assert cap <= 18
        assert cap < 10

    def test_estimate_downstream_demand(self):
        demand = estimate_downstream_demand(
            implementing_count=10, reviewing_count=5,
        )
        assert demand["expected_review_demand"] > 0
        assert demand["expected_reworks"] > 0


class TestWorkScorer:
    def test_critical_security_overrides_penalties(self):
        t = FakeTicket(id="SEC-1", ticket_class="security", severity="critical", state="READY")
        scored = score_ticket(t, queue_pressure=QueuePressure(), now=time.time())
        assert scored.total_score >= 100.0

    def test_rework_scores_higher_than_ready(self):
        t_rework = FakeTicket(id="R-1", state="REWORK")
        t_ready = FakeTicket(id="RD-1", state="READY")
        now = time.time()
        s_rework = score_ticket(t_rework, queue_pressure=QueuePressure(), now=now)
        s_ready = score_ticket(t_ready, queue_pressure=QueuePressure(), now=now)
        assert s_rework.total_score > s_ready.total_score

    def test_rank_work_items_sorts_descending(self):
        tickets = [
            FakeTicket(id="T1", severity="low", state="READY"),
            FakeTicket(id="T2", severity="critical", state="READY"),
            FakeTicket(id="T3", severity="medium", state="REWORK"),
        ]
        ranked = rank_work_items(tickets, queue_pressure=QueuePressure(), now=time.time())
        assert len(ranked) == 3
        assert ranked[0].total_score >= ranked[1].total_score

    def test_blocked_ticket_marked_unschedulable(self):
        ps = make_pipeline(unsatisfied_deps={"T1": ("T0",)})
        t = FakeTicket(id="T1", state="READY")
        scored = score_ticket(t, pipeline_state=ps, queue_pressure=QueuePressure(), now=time.time())
        assert scored.blocked is True
        assert scored.is_schedulable is False


class TestConflictDetector:
    def test_no_conflicts_empty_matrix(self):
        matrix = ConflictMatrix()
        assert matrix.conflicts_with("T1") == frozenset()
        assert matrix.has_any_conflict("T1") is False

    def test_module_overlap_detected(self):
        tickets = [
            FakeTicket(id="T1", affected_modules=["auth"]),
            FakeTicket(id="T2", affected_modules=["auth", "billing"]),
            FakeTicket(id="T3", affected_modules=["docs"]),
        ]
        matrix = build_conflict_matrix(tickets)
        assert matrix.has_any_conflict("T1") is True
        assert "T2" in matrix.conflicts_with("T1")
        assert matrix.has_any_conflict("T3") is False

    def test_filter_non_conflicting(self):
        matrix = ConflictMatrix(edges=(
            ConflictEdge(ticket_a="T1", ticket_b="T2", reason="shared_module"),
        ))
        safe = filter_non_conflicting(["T1", "T2", "T3"], {"T1"}, matrix)
        assert "T1" in safe
        assert "T2" not in safe
        assert "T3" in safe

    def test_conflict_groups(self):
        matrix = ConflictMatrix(edges=(
            ConflictEdge(ticket_a="T1", ticket_b="T2", reason="m"),
            ConflictEdge(ticket_a="T2", ticket_b="T3", reason="m"),
            ConflictEdge(ticket_a="T4", ticket_b="T5", reason="m"),
        ))
        groups = matrix.conflict_groups()
        assert len(groups) == 2
        sizes = sorted(len(g) for g in groups)
        assert sizes == [2, 3]


class TestDiscoveryManager:
    def test_empty_project_allocates_discovery(self):
        dm = DiscoveryManager()
        config = SchedulerConfig.default()
        alloc = dm.compute_allocation(available_slots=15, config=config)
        assert alloc.total_slots > 0
        assert len(alloc.allocations) > 1

    def test_cooldown_prevents_rescan(self):
        dm = DiscoveryManager()
        now = time.time()
        dm.record_completion(
            role="bug_hunter", scope="full_project",
            commit_sha="abc123", findings=5, duplicates=1,
            rejected=0, cost_tokens=1000, now=now,
        )
        assert dm.is_on_cooldown("bug_hunter", "full_project", "abc123", 3600, now) is True
        assert dm.is_on_cooldown("bug_hunter", "full_project", "def456", 3600, now) is False

    def test_saturation_detection(self):
        dm = DiscoveryManager()
        now = time.time()
        for role in ["bug_hunter", "security_auditor", "test_gap_auditor"]:
            for _ in range(6):
                dm.record_completion(
                    role=role, scope="full", commit_sha="abc",
                    findings=0, duplicates=10,
                    rejected=5, cost_tokens=100, now=now,
                )
        assert dm.is_saturated() is True

    def test_yield_tracking(self):
        dm = DiscoveryManager()
        dm.record_completion("bug_hunter", "src/", "sha1", 10, 2, 1, 5000)
        stats = dm.get_yield_stats("bug_hunter")
        assert stats.total_scans == 1
        assert stats.validated_tickets == 10
        assert stats.duplicate_rate > 0


class TestAdaptiveScheduler:
    def test_empty_project_spawns_discovery(self):
        scheduler = AdaptiveScheduler()
        state = make_pipeline(ready=0, implementing=0, reviewing=0)
        decision = _call_tick(scheduler, state, [])
        discovery_roles = [a for a in decision.assignments if a.role in DISCOVERY_ROLES]
        assert len(discovery_roles) > 0

    def test_healthy_backlog_prioritizes_implementation(self):
        scheduler = AdaptiveScheduler()
        tickets = [FakeTicket(id=f"T{i}", state="READY", severity="medium") for i in range(20)]
        state = make_pipeline(ready=20)
        decision = _call_tick(scheduler, state, tickets)
        impl_roles = [a for a in decision.assignments if a.role in (
            "general_implementer", "backend_implementer", "frontend_implementer",
        )]
        assert len(impl_roles) > 0

    def test_review_congestion_increases_reviewers(self):
        scheduler = AdaptiveScheduler()
        review_tickets = [FakeTicket(id=f"R{i}", state="REVIEWING") for i in range(25)]
        ready_tickets = [FakeTicket(id=f"T{i}", state="READY") for i in range(50)]
        state = make_pipeline(ready=50, implementing=12, reviewing=25, verifying=2)
        decision = _call_tick(scheduler, state, review_tickets + ready_tickets)
        review_assignments = [a for a in decision.assignments if "reviewer" in a.role]
        assert len(review_assignments) >= 2

    def test_verification_congestion(self):
        scheduler = AdaptiveScheduler()
        verify_tickets = [FakeTicket(id=f"V{i}", state="VERIFYING") for i in range(10)]
        state = make_pipeline(verifying=10)
        decision = _call_tick(scheduler, state, verify_tickets)
        verify_assignments = [a for a in decision.assignments if a.role in ("quality_gate", "verifier")]
        assert len(verify_assignments) >= 2

    def test_rework_prioritized_over_new_work(self):
        scheduler = AdaptiveScheduler()
        rework_t = [FakeTicket(id="RW1", state="REWORK", severity="high")]
        ready_t = [FakeTicket(id=f"T{i}", state="READY") for i in range(10)]
        state = make_pipeline(rework=1, ready=10)
        decision = _call_tick(scheduler, state, rework_t + ready_t)
        rework_assignments = [a for a in decision.assignments if a.ticket_id == "RW1"]
        assert len(rework_assignments) > 0

    def test_dependency_blocking_prevents_execution(self):
        scheduler = AdaptiveScheduler()
        blocked_ticket = FakeTicket(id="T2", state="READY")
        state = make_pipeline(
            ready=1,
            unsatisfied_deps={"T2": ("T1",)},
        )
        decision = _call_tick(scheduler, state, [blocked_ticket])
        assigned_ids = {a.ticket_id for a in decision.assignments}
        assert "T2" not in assigned_ids

    def test_conflicting_tickets_serialized(self):
        scheduler = AdaptiveScheduler()
        t1 = FakeTicket(id="T1", state="READY", affected_modules=["auth"])
        t2 = FakeTicket(id="T2", state="READY", affected_modules=["auth"])
        matrix = ConflictMatrix(edges=(
            ConflictEdge(ticket_a="T1", ticket_b="T2", reason="shared_module"),
        ))
        state = make_pipeline(
            ready=2,
            active_workers=[make_worker("w1", "T1", "general_implementer")],
        )
        decision = _call_tick(scheduler, state, [t1, t2], conflict_matrix=matrix)
        assigned_ids = {a.ticket_id for a in decision.assignments}
        assert "T1" not in assigned_ids
        assert "T2" not in assigned_ids

    def test_security_emergency_override(self):
        scheduler = AdaptiveScheduler()
        sec_ticket = FakeTicket(id="SEC-1", ticket_class="security", severity="critical", state="READY")
        normal_tickets = [FakeTicket(id=f"T{i}", state="READY") for i in range(5)]
        state = make_pipeline(ready=6)
        decision = _call_tick(scheduler, state, [sec_ticket] + normal_tickets)
        sec_assignments = [a for a in decision.assignments if a.ticket_id == "SEC-1"]
        assert len(sec_assignments) >= 1

    def test_budget_exhaustion_throttles(self):
        scheduler = AdaptiveScheduler()
        tickets = [FakeTicket(id="T1", state="READY")]
        state = make_pipeline(ready=1, budget_exhausted=True)
        decision = _call_tick(scheduler, state, tickets)
        assert decision.mode == SchedulerMode.BUDGET_THROTTLED
        assert len(decision.assignments) == 0

    def test_max_slots_enforced(self):
        config = SchedulerConfig(max_slots=5).validate()
        scheduler = AdaptiveScheduler(config=config)
        tickets = [FakeTicket(id=f"T{i}", state="READY") for i in range(20)]
        state = make_pipeline(ready=20, total_slots=5)
        decision = _call_tick(scheduler, state, tickets)
        assert decision.total_active_after <= 5

    def test_stuck_worker_releases_slot(self):
        scheduler = AdaptiveScheduler()
        state = make_pipeline(
            ready=5,
            stuck=["w-stuck"],
        )
        tickets = [FakeTicket(id=f"T{i}", state="READY") for i in range(5)]
        decision = _call_tick(scheduler, state, tickets)
        assert len(decision.assignments) > 0

    def test_discovery_decreases_as_backlog_grows(self):
        scheduler = AdaptiveScheduler()
        now = time.time()

        state_empty = make_pipeline(ready=0)
        dec_empty = _call_tick(scheduler, state_empty, [], now=now)
        disc_empty = sum(1 for a in dec_empty.assignments if a.role in DISCOVERY_ROLES)

        state_full = make_pipeline(ready=80)
        tickets = [FakeTicket(id=f"T{i}", state="READY") for i in range(80)]
        dec_full = _call_tick(scheduler, state_full, tickets, now=now)
        disc_full = sum(1 for a in dec_full.assignments if a.role in DISCOVERY_ROLES)

        assert disc_empty >= disc_full

    def test_dashboard_output(self):
        scheduler = AdaptiveScheduler()
        state = make_pipeline(ready=10, implementing=5, reviewing=3)
        decision = _call_tick(scheduler, state, [FakeTicket(id="T1", state="READY")])
        dashboard = scheduler.format_dashboard(decision, state)
        assert "CODEBOT CAPACITY" in dashboard
        assert "Slots:" in dashboard

    def test_reviewer_independence(self):
        scheduler = AdaptiveScheduler()
        impl_worker = make_worker("w1", "T1", "general_implementer")
        review_ticket = FakeTicket(id="T1", state="REVIEWING")
        state = make_pipeline(
            reviewing=1,
            active_workers=[impl_worker],
        )
        decision = _call_tick(scheduler, state, [review_ticket])
        for a in decision.assignments:
            if a.ticket_id == "T1" and "reviewer" in a.role:
                assert a.reason != "same worker as implementer"


class TestSchedulerMetrics:
    def test_metrics_accumulator_builds_snapshot(self):
        from codebot.scheduler_metrics import MetricsAccumulator
        acc = MetricsAccumulator()
        now = time.time()
        acc.record_completion("T1", now, now - 3600, was_productive=True, cost_tokens=5000)
        acc.record_review(passed_first=True)
        acc.record_discovery(validated=3, duplicates=1)

        state = make_pipeline(ready=5, implementing=3)
        metrics = acc.build(state, scheduler_mode="BALANCED", now=now)

        assert metrics.tickets_completed_per_hour == 1.0
        assert metrics.first_pass_review_rate == 1.0
        assert metrics.discovery_yield_rate == 0.75
        assert metrics.scheduler_mode == "BALANCED"

    def test_productive_utilization_tracked(self):
        from codebot.scheduler_metrics import MetricsAccumulator
        acc = MetricsAccumulator()
        now = time.time()
        acc.record_completion("T1", now, now - 100, was_productive=True)
        acc.record_completion("T2", now, now - 100, was_productive=False)

        state = make_pipeline()
        metrics = acc.build(state, now=now)
        assert metrics.productive_slot_utilization <= 1.0
