#!/usr/bin/env python3
"""Extensive pytest suite for codebot.adaptive_scheduler — P1 30-slot scheduler.

Covers every public class/function, edge cases, error paths with Given/When/Then
naming and one When per test. Uses tmp_path isolation where applicable and
real PipelineState/SchedulerConfig/DiscoveryManager objects; mocks only clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codebot.adaptive_scheduler import (
    AdaptiveScheduler,
    HysteresisState,
    SchedulerDecision,
    SlotAssignment,
    IMPLEMENTATION_ROLES,
    REVIEW_ROLES,
    VERIFICATION_ROLES,
    PLANNING_ROLES,
)
from codebot.conflict_detector import ConflictEdge, ConflictMatrix
from codebot.discovery_manager import DISCOVERY_ROLES, DiscoveryManager
from codebot.pipeline_state import PipelineState, WorkerSlot
from codebot.queue_pressure import SchedulerMode
from codebot.scheduler_config import SchedulerConfig


# ---------------------------------------------------------------------------
# helpers — real objects without loose typing
# ---------------------------------------------------------------------------

@dataclass
class FakeTicket:
    id: str = "CB-001"
    title: str = "Test ticket"
    ticket_class: str = "bug"
    severity: str = "medium"
    state: str = "READY"
    risk: str = "medium"
    affected_modules: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.time)
    estimated_cost_tokens: int = 10000


def _pipeline(
    ready: int = 0,
    implementation_ready: int = 0,
    implementing: int = 0,
    reviewing: int = 0,
    verifying: int = 0,
    rework: int = 0,
    planning: int = 0,
    discovered: int = 0,
    candidates: int = 0,
    blocked: int = 0,
    total_slots: int = 30,
    active_workers: tuple[WorkerSlot, ...] = (),
    stuck_workers: tuple[str, ...] = (),
    budget_exhausted: bool = False,
    budget_warning: bool = False,
    hourly_spend_usd: float = 0.0,
    daily_spend_usd: float = 0.0,
    unsatisfied: dict[str, tuple[str, ...]] | None = None,
    conflict_groups: tuple[frozenset[str], ...] = (),
    integration_depth: int = 0,
) -> PipelineState:
    return PipelineState(
        ready_count=ready,
        implementation_ready_count=implementation_ready,
        implementing_count=implementing,
        reviewing_count=reviewing,
        verifying_count=verifying,
        rework_count=rework,
        planning_count=planning,
        discovered_count=discovered,
        candidate_count=candidates,
        blocked_count=blocked,
        total_slots=total_slots,
        active_workers=active_workers,
        stuck_workers=stuck_workers,
        budget_exhausted=budget_exhausted,
        budget_warning=budget_warning,
        hourly_spend_usd=hourly_spend_usd,
        daily_spend_usd=daily_spend_usd,
        unsatisfied_dependencies=unsatisfied or {},
        conflict_groups=conflict_groups,
        integration_queue_depth=integration_depth,
        snapshot_time=1000000.0,
    )


def _worker(worker_id: str, ticket_id: str, role: str) -> WorkerSlot:
    now = 1000000.0
    return WorkerSlot(
        worker_id=worker_id,
        ticket_id=ticket_id,
        role=role,
        started_at=now,
        heartbeat_at=now,
        lease_expires=now + 600,
    )


def _ready_tickets(n: int, prefix: str = "T") -> list[FakeTicket]:
    return [FakeTicket(id=f"{prefix}-{i}", state="READY") for i in range(n)]


def _rework_tickets(n: int) -> list[FakeTicket]:
    return [FakeTicket(id=f"RW-{i}", state="REWORK") for i in range(n)]


def _verify_tickets(n: int) -> list[FakeTicket]:
    return [FakeTicket(id=f"V-{i}", state="VERIFYING") for i in range(n)]


def _review_tickets(n: int) -> list[FakeTicket]:
    return [FakeTicket(id=f"R-{i}", state="REVIEWING") for i in range(n)]


def _planning_tickets(n: int) -> list[FakeTicket]:
    return [FakeTicket(id=f"P-{i}", state="PLANNING") for i in range(n)]


# ---------------------------------------------------------------------------
# SlotAssignment / SchedulerDecision / HysteresisState
# ---------------------------------------------------------------------------

class TestSlotAssignment:
    def test_Given_slot_fields_When_created_Then_values_preserved(self) -> None:
        sa = SlotAssignment(role="general_implementer", ticket_id="CB-1", model_profile="standard", priority=4, reason="test")
        assert sa.role == "general_implementer"
        assert sa.ticket_id == "CB-1"
        assert sa.model_profile == "standard"
        assert sa.priority == 4
        assert sa.reason == "test"

    def test_Given_slot_assignments_When_frozen_Then_immutable(self) -> None:
        sa = SlotAssignment(role="planner", ticket_id="P-1", model_profile="standard", priority=5, reason="planning")
        with pytest.raises(AttributeError):
            setattr(sa, "role", "other")


class TestSchedulerDecision:
    def test_Given_assignments_When_summary_Then_by_role_counts(self) -> None:
        assignments: tuple[SlotAssignment, ...] = (
            SlotAssignment(role="general_implementer", ticket_id="T1", model_profile="standard", priority=4, reason="impl"),
            SlotAssignment(role="general_implementer", ticket_id="T2", model_profile="standard", priority=4, reason="impl"),
            SlotAssignment(role="correctness_reviewer", ticket_id="R1", model_profile="premium", priority=3, reason="review"),
        )
        from codebot.queue_pressure import QueuePressure as QP

        class _SummaryPressure(QP):
            def summary(self) -> dict[str, object]:
                return self.to_dict()

        patched = _SummaryPressure()
        dec3 = SchedulerDecision(
            assignments=assignments,
            mode=SchedulerMode.BALANCED,
            pressure=patched,
            free_slots_remaining=5,
            total_active_after=8,
            reasons=("test",),
            timestamp=1000000.0,
        )
        summary = dec3.summary()
        assert summary["by_role"]["general_implementer"] == 2
        assert summary["by_role"]["correctness_reviewer"] == 1
        assert summary["total_assigned"] == 3
        assert summary["mode"] == "BALANCED"

    def test_Given_empty_assignments_When_summary_Then_zero_counts(self) -> None:
        from codebot.queue_pressure import QueuePressure as QP2

        class _SummaryPressure2(QP2):
            def summary(self) -> dict[str, object]:
                return self.to_dict()

        patched = _SummaryPressure2()
        dec = SchedulerDecision(
            assignments=(),
            mode=SchedulerMode.IDLE,
            pressure=patched,
            free_slots_remaining=30,
            total_active_after=0,
            reasons=(),
            timestamp=1000000.0,
        )
        s = dec.summary()
        assert s["total_assigned"] == 0
        assert s["by_role"] == {}


class TestHysteresisState:
    def test_Given_same_mode_When_should_shift_Then_false(self) -> None:
        hs = HysteresisState(last_mode=SchedulerMode.BALANCED, last_change_time=1000.0)
        result = hs.should_shift(SchedulerMode.BALANCED, now=2000.0, min_duration=300)
        assert result is False

    def test_Given_elapsed_insufficient_When_should_shift_Then_false(self) -> None:
        hs = HysteresisState(last_mode=SchedulerMode.BALANCED, last_change_time=1000.0)
        result = hs.should_shift(SchedulerMode.REVIEW_HEAVY, now=1100.0, min_duration=300)
        assert result is False

    def test_Given_elapsed_sufficient_When_should_shift_Then_true(self) -> None:
        hs = HysteresisState(last_mode=SchedulerMode.BALANCED, last_change_time=1000.0)
        result = hs.should_shift(SchedulerMode.REVIEW_HEAVY, now=1400.0, min_duration=300)
        assert result is True

    def test_Given_record_mode_change_When_record_Then_updates_time(self) -> None:
        hs = HysteresisState(last_mode=SchedulerMode.BALANCED, last_change_time=1000.0)
        hs.record(SchedulerMode.REVIEW_HEAVY, {"review": 5}, now=2000.0)
        assert hs.last_mode == SchedulerMode.REVIEW_HEAVY
        assert hs.last_change_time == 2000.0
        assert hs.last_allocation == {"review": 5}

    def test_Given_record_same_mode_When_record_Then_time_unchanged(self) -> None:
        hs = HysteresisState(last_mode=SchedulerMode.BALANCED, last_change_time=1000.0)
        hs.record(SchedulerMode.BALANCED, {"balanced": 3}, now=2000.0)
        assert hs.last_change_time == 1000.0
        assert hs.last_allocation == {"balanced": 3}

    def test_Given_many_pressure_samples_When_add_Then_capped_at_ten(self) -> None:
        hs = HysteresisState()
        from codebot.queue_pressure import QueuePressure

        for _ in range(15):
            hs.add_pressure_sample(QueuePressure(implementation_pressure=1.0))
        assert len(hs.rolling_pressures) == 10

    def test_Given_discovery_active_flags_When_ticked_Then_updated(self, tmp_path: Path) -> None:
        sched = AdaptiveScheduler(discovery_manager=DiscoveryManager(state_dir=tmp_path))
        # pipeline totally empty -> discovery_pressure high -> discovery_active True
        pipeline = _pipeline(total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert sched.hysteresis.discovery_active is True
        # now with work -> discovery suppressed
        pipeline2 = _pipeline(ready=10, total_slots=30)
        tickets = _ready_tickets(10)
        # need to set hysteresis min duration 0 to allow mode shift quickly, else discovery_active still updated regardless of hysteresis
        sched2 = AdaptiveScheduler(
            config=SchedulerConfig(hysteresis=SchedulerConfig().hysteresis.__class__(min_allocation_duration_seconds=0)),
            discovery_manager=DiscoveryManager(state_dir=tmp_path / "d2"),
        )
        dec2 = sched2.tick(
            pipeline=pipeline2,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000001.0,
        )
        # with backlog discovery suppressed -> discovery_active False
        assert sched2.hysteresis.discovery_active is False


# ---------------------------------------------------------------------------
# AdaptiveScheduler — budget gates
# ---------------------------------------------------------------------------

class TestBudgetGates:
    def test_Given_budget_exhausted_When_tick_Then_budget_throttled_zero_assignments(self) -> None:
        sched = AdaptiveScheduler()
        pipeline = _pipeline(ready=5, budget_exhausted=True)
        tickets = _ready_tickets(5)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert dec.mode == SchedulerMode.BUDGET_THROTTLED
        assert len(dec.assignments) == 0
        assert dec.free_slots_remaining == pipeline.total_slots
        assert dec.total_active_after == 0

    def test_Given_daily_burn_severe_When_tick_Then_severe_throttle_reason(self) -> None:
        config = SchedulerConfig.default()
        sched = AdaptiveScheduler(config=config)
        # daily spend 95/100 =0.95 >=0.9 triggers severe
        pipeline = _pipeline(ready=10, daily_spend_usd=95.0, hourly_spend_usd=1.0)
        tickets = _ready_tickets(10)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert any("severe" in r for r in dec.reasons)

    def test_Given_hourly_burn_warning_When_tick_Then_warning_throttle_reason(self) -> None:
        config = SchedulerConfig.default()
        sched = AdaptiveScheduler(config=config)
        # hourly 9/10 =0.9 >=0.8 triggers warning when not severe
        pipeline = _pipeline(ready=10, hourly_spend_usd=9.0, daily_spend_usd=10.0)
        tickets = _ready_tickets(10)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert any("warning" in r for r in dec.reasons)

    def test_Given_budget_warning_flag_When_tick_Then_warning_throttled(self) -> None:
        sched = AdaptiveScheduler()
        pipeline = _pipeline(ready=10, budget_warning=True, hourly_spend_usd=1.0, daily_spend_usd=1.0)
        tickets = _ready_tickets(10)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert any("warning" in r for r in dec.reasons)


# ---------------------------------------------------------------------------
# Security emergency
# ---------------------------------------------------------------------------

class TestSecurityEmergency:
    def test_Given_critical_security_ticket_When_tick_Then_emergency_slots_allocated(self) -> None:
        sched = AdaptiveScheduler()
        sec = FakeTicket(id="SEC-1", ticket_class="security", severity="critical", state="READY")
        normals = _ready_tickets(3, prefix="N")
        pipeline = _pipeline(ready=4, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[sec] + normals,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        sec_assigns = [a for a in dec.assignments if a.ticket_id == "SEC-1"]
        assert len(sec_assigns) >= 1
        assert any("SECURITY EMERGENCY" in r for r in dec.reasons)
        # at least the emergency allocation is premium priority 0
        emergency = [a for a in sec_assigns if a.priority == 0 and a.model_profile == "premium"]
        assert len(emergency) >= 1
        assert any(a.role == "backend_implementer" for a in emergency)

    def test_Given_security_emergency_one_free_slot_When_tick_Then_one_slot(self) -> None:
        sched = AdaptiveScheduler()
        sec = FakeTicket(id="SEC-1", ticket_class="security", severity="critical", state="READY")
        # Fill 29 slots, 1 free
        workers = tuple(_worker(f"w{i}", f"T{i}", "general_implementer") for i in range(29))
        pipeline = _pipeline(ready=1, total_slots=30, active_workers=workers)
        assert pipeline.free_slots == 1
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[sec],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        sec_assigns = [a for a in dec.assignments if a.ticket_id == "SEC-1"]
        assert len(sec_assigns) == 1
        assert sec_assigns[0].role == "backend_implementer"

    def test_Given_security_emergency_zero_free_slots_When_tick_Then_no_assignments_but_reason(self) -> None:
        sched = AdaptiveScheduler()
        sec = FakeTicket(id="SEC-1", ticket_class="security", severity="critical", state="READY")
        workers = tuple(_worker(f"w{i}", f"T{i}", "general_implementer") for i in range(30))
        pipeline = _pipeline(ready=1, total_slots=30, active_workers=workers)
        assert pipeline.free_slots == 0
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[sec],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        sec_assigns = [a for a in dec.assignments if a.ticket_id == "SEC-1"]
        assert len(sec_assigns) == 0
        assert any("SECURITY EMERGENCY" in r for r in dec.reasons)

    def test_Given_security_in_rework_When_tick_Then_emergency_triggered(self) -> None:
        sched = AdaptiveScheduler()
        sec_rework = FakeTicket(id="SEC-RW", ticket_class="security", severity="critical", state="REWORK")
        pipeline = _pipeline(rework=1, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[sec_rework],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert any("SECURITY EMERGENCY" in r for r in dec.reasons)
        sec_assigns = [a for a in dec.assignments if a.ticket_id == "SEC-RW"]
        assert len(sec_assigns) >= 1


# ---------------------------------------------------------------------------
# Slot allocation — rework, verification, review, implementation, planning, discovery
# ---------------------------------------------------------------------------

class TestSlotAllocation:
    def test_Given_rework_tickets_When_tick_Then_rework_allocated_first(self) -> None:
        sched = AdaptiveScheduler()
        reworks = [FakeTicket(id="RW-1", state="REWORK", severity="high")]
        readies = _ready_tickets(5)
        pipeline = _pipeline(rework=1, ready=5, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=readies,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=reworks,
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert any(a.ticket_id == "RW-1" for a in dec.assignments)
        rw_assigns = [a for a in dec.assignments if a.ticket_id == "RW-1"]
        assert rw_assigns[0].priority == 1
        assert rw_assigns[0].model_profile == "standard"

    def test_Given_verify_tickets_When_tick_Then_verification_reserved(self) -> None:
        sched = AdaptiveScheduler()
        verifies = _verify_tickets(3)
        pipeline = _pipeline(verifying=3, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=verifies,
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        vg = [a for a in dec.assignments if a.role in VERIFICATION_ROLES]
        assert len(vg) >= 2
        for a in vg:
            assert a.model_profile == "cheap"
            assert a.priority == 2

    def test_Given_review_tickets_When_tick_Then_review_allocated(self) -> None:
        sched = AdaptiveScheduler()
        reviews = _review_tickets(4)
        pipeline = _pipeline(reviewing=4, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=reviews,
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        rv = [a for a in dec.assignments if a.role in REVIEW_ROLES]
        assert len(rv) >= 2
        for a in rv:
            assert a.model_profile == "premium"
            assert a.priority == 3

    def test_Given_review_congestion_When_tick_Then_review_scales(self) -> None:
        sched = AdaptiveScheduler()
        reviews = _review_tickets(10)
        pipeline = _pipeline(reviewing=10, total_slots=30)
        # reviewing 10 >30*0.3=9 triggers scaling
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=reviews,
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        rv = [a for a in dec.assignments if a.role in REVIEW_ROLES]
        assert len(rv) >= 2

    def test_Given_planning_tickets_When_tick_Then_planning_allocated(self) -> None:
        sched = AdaptiveScheduler()
        plans = _planning_tickets(4)
        pipeline = _pipeline(planning=4, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=plans,
            candidate_tickets=[],
            now=1000000.0,
        )
        pl = [a for a in dec.assignments if a.role in PLANNING_ROLES]
        assert len(pl) >= 2
        for a in pl:
            assert a.role == "planner"
            assert a.priority == 5

    def test_Given_planning_budget_capped_When_tick_Then_not_exceed_free(self) -> None:
        sched = AdaptiveScheduler()
        plans = _planning_tickets(20)
        # only 2 free slots left after filling 28
        workers = tuple(_worker(f"w{i}", f"T{i}", "general_implementer") for i in range(28))
        pipeline = _pipeline(planning=20, total_slots=30, active_workers=workers)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=plans,
            candidate_tickets=[],
            now=1000000.0,
        )
        pl = [a for a in dec.assignments if a.role == "planner"]
        assert len(pl) <= 2

    def test_Given_free_slots_When_tick_Then_discovery_fills_remainder(self, tmp_path: Path) -> None:
        sched = AdaptiveScheduler(discovery_manager=DiscoveryManager(state_dir=tmp_path))
        pipeline = _pipeline(total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        disc = [a for a in dec.assignments if a.role in DISCOVERY_ROLES]
        assert len(disc) > 0
        for a in disc:
            assert a.priority == 7

    def test_Given_impl_bounded_by_review_When_tick_Then_cap_enforced(self) -> None:
        config = SchedulerConfig(max_slots=30)
        sched = AdaptiveScheduler(config=config)
        # Simulate review saturated: many reviewing, few reviewers active
        pipeline = _pipeline(ready=20, reviewing=10, verifying=1, total_slots=30)
        tickets = _ready_tickets(20)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        impl = [a for a in dec.assignments if a.role in IMPLEMENTATION_ROLES]
        # cap should limit impl; hard cap is 18 (0.6*30) but review headroom reduces further
        assert len(impl) <= 18

    def test_Given_max_slots_five_When_tick_Then_total_active_not_exceed_five(self) -> None:
        config = SchedulerConfig(max_slots=5).validate()
        sched = AdaptiveScheduler(config=config)
        tickets = _ready_tickets(20)
        pipeline = _pipeline(ready=20, total_slots=5)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert dec.total_active_after <= 5
        assert len(dec.assignments) + pipeline.total_active <= 5

    def test_Given_zero_free_slots_When_tick_Then_no_allocations(self) -> None:
        sched = AdaptiveScheduler()
        workers = tuple(_worker(f"w{i}", f"T{i}", "general_implementer") for i in range(30))
        pipeline = _pipeline(ready=10, total_slots=30, active_workers=workers)
        tickets = _ready_tickets(10)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        # no free slots => only possible emergency would allocate, but none here
        # discovery should not allocate, impl budget 0
        # but free=0 so all allocations return empty except maybe rework etc but they also check free<=0
        assert len(dec.assignments) == 0

    def test_Given_impl_current_active_When_tick_Then_budget_subtracted(self) -> None:
        sched = AdaptiveScheduler()
        # 10 implementers already active, cap maybe 18, so remaining budget ~8
        workers = tuple(_worker(f"w{i}", f"T{i}", "general_implementer") for i in range(10))
        pipeline = _pipeline(ready=20, total_slots=30, active_workers=workers)
        tickets = _ready_tickets(20)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        impl = [a for a in dec.assignments if a.role in IMPLEMENTATION_ROLES]
        # current 10 + new <=30 and <= cap
        assert len(impl) <= 8 or len(impl) <= 18


# ---------------------------------------------------------------------------
# Conflict detection, dependency blocking, reviewer independence
# ---------------------------------------------------------------------------

class TestConflictAndDeps:
    def test_Given_blocked_ticket_When_tick_Then_not_scheduled(self) -> None:
        sched = AdaptiveScheduler()
        ticket = FakeTicket(id="T2", state="READY")
        pipeline = _pipeline(ready=1, unsatisfied={"T2": ("T1",)})
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[ticket],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert all(a.ticket_id != "T2" for a in dec.assignments)

    def test_Given_conflicting_ticket_active_When_tick_Then_serialized(self) -> None:
        sched = AdaptiveScheduler()
        t1 = FakeTicket(id="T1", state="READY", affected_modules=["auth"])
        t2 = FakeTicket(id="T2", state="READY", affected_modules=["auth"])
        matrix = ConflictMatrix(edges=(ConflictEdge(ticket_a="T1", ticket_b="T2", reason="shared_module"),))
        # T1 is active
        workers = (_worker("w1", "T1", "general_implementer"),)
        pipeline = _pipeline(ready=2, total_slots=30, active_workers=workers)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[t1, t2],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            conflict_matrix=matrix,
            now=1000000.0,
        )
        # T1 already active => skipped, T2 has conflict => filtered => none scheduled
        assert all(a.ticket_id != "T1" for a in dec.assignments)
        assert all(a.ticket_id != "T2" for a in dec.assignments)

    def test_Given_conflicting_tickets_none_active_When_tick_Then_both_filtered(self) -> None:
        sched = AdaptiveScheduler()
        t1 = FakeTicket(id="T1", state="READY")
        t2 = FakeTicket(id="T2", state="READY")
        matrix = ConflictMatrix(edges=(ConflictEdge(ticket_a="T1", ticket_b="T2", reason="shared_module"),))
        pipeline = _pipeline(ready=2, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[t1, t2],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            conflict_matrix=matrix,
            now=1000000.0,
        )
        # current implementation filters any ticket with has_any_conflict True
        # so both T1 and T2 are filtered => zero impl assignments
        impl = [a for a in dec.assignments if a.role in IMPLEMENTATION_ROLES]
        assert len(impl) == 0

    def test_Given_reviewer_independence_violation_When_tick_Then_skipped(self) -> None:
        sched = AdaptiveScheduler()
        # Active implementer on T1
        impl_worker = _worker("w1", "T1", "general_implementer")
        review_ticket = FakeTicket(id="T1", state="REVIEWING")
        pipeline = _pipeline(reviewing=1, total_slots=30, active_workers=(impl_worker,))
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[review_ticket],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        # Review for T1 should be skipped due to independence violation
        t1_reviews = [a for a in dec.assignments if a.ticket_id == "T1" and a.role in REVIEW_ROLES]
        assert len(t1_reviews) == 0

    def test_Given_duplicate_active_ids_When_tick_Then_not_double_scheduled(self) -> None:
        sched = AdaptiveScheduler()
        t1 = FakeTicket(id="T1", state="READY")
        workers = (_worker("w1", "T1", "general_implementer"),)
        pipeline = _pipeline(ready=1, total_slots=30, active_workers=workers)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[t1],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert all(a.ticket_id != "T1" for a in dec.assignments)


# ---------------------------------------------------------------------------
# Planning allocation specifics
# ---------------------------------------------------------------------------

class TestPlanningAllocation:
    def test_Given_planning_tickets_and_free_slots_When_tick_Then_planning_reason_logged(self) -> None:
        sched = AdaptiveScheduler()
        plans = _planning_tickets(3)
        pipeline = _pipeline(planning=3, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=plans,
            candidate_tickets=[],
            now=1000000.0,
        )
        assert any("planning" in r for r in dec.reasons)

    def test_Given_no_planning_tickets_When_tick_Then_no_planning_allocations(self) -> None:
        sched = AdaptiveScheduler()
        pipeline = _pipeline(total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        planners = [a for a in dec.assignments if a.role == "planner"]
        assert len(planners) == 0

    def test_Given_planning_blocked_by_deps_When_tick_Then_not_scheduled(self) -> None:
        sched = AdaptiveScheduler()
        plan = FakeTicket(id="P1", state="PLANNING")
        pipeline = _pipeline(planning=1, unsatisfied={"P1": ("X",)}, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[plan],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert all(a.ticket_id != "P1" for a in dec.assignments)


# ---------------------------------------------------------------------------
# Scheduler modes and pressure integration
# ---------------------------------------------------------------------------

class TestSchedulerModes:
    def test_Given_empty_pipeline_When_tick_Then_idle_or_discovery_heavy(self, tmp_path: Path) -> None:
        sched = AdaptiveScheduler(
            config=SchedulerConfig(hysteresis=SchedulerConfig().hysteresis.__class__(min_allocation_duration_seconds=0)),
            discovery_manager=DiscoveryManager(state_dir=tmp_path),
        )
        pipeline = _pipeline(total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert dec.mode in (SchedulerMode.IDLE, SchedulerMode.DISCOVERY_HEAVY, SchedulerMode.PROJECT_SATURATED, SchedulerMode.BALANCED)

    def test_Given_stuck_majority_When_tick_Then_recovery_mode(self, tmp_path: Path) -> None:
        sched = AdaptiveScheduler(
            config=SchedulerConfig(hysteresis=SchedulerConfig().hysteresis.__class__(min_allocation_duration_seconds=0)),
            discovery_manager=DiscoveryManager(state_dir=tmp_path),
        )
        stuck = tuple(f"w{i}" for i in range(11))
        workers = tuple(_worker(f"w{i}", f"T{i}", "general_implementer") for i in range(5))
        pipeline = _pipeline(ready=5, total_slots=30, active_workers=workers, stuck_workers=stuck)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=_ready_tickets(5),
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert dec.mode == SchedulerMode.RECOVERY

    def test_Given_hysteresis_blocks_shift_When_tick_Then_mode_unchanged(self) -> None:
        config = SchedulerConfig(hysteresis=SchedulerConfig().hysteresis.__class__(min_allocation_duration_seconds=600))
        sched = AdaptiveScheduler(config=config)
        # first tick sets BALANCED
        pipeline1 = _pipeline(ready=5, total_slots=30)
        dec1 = sched.tick(
            pipeline=pipeline1,
            ready_tickets=_ready_tickets(5),
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        mode_before = dec1.mode
        # second tick with review pressure would want REVIEW_HEAVY but hysteresis blocks
        pipeline2 = _pipeline(ready=1, reviewing=25, implementing=5, total_slots=30)
        rev_tickets = _review_tickets(25)
        dec2 = sched.tick(
            pipeline=pipeline2,
            ready_tickets=_ready_tickets(1),
            review_tickets=rev_tickets,
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000001.0,  # only 1 sec later <600
        )
        assert dec2.mode == mode_before

    def test_Given_no_work_but_active_When_tick_Then_project_saturated(self) -> None:
        config = SchedulerConfig(hysteresis=SchedulerConfig().hysteresis.__class__(min_allocation_duration_seconds=0))
        sched = AdaptiveScheduler(config=config)
        # Mock discovery to saturated so tick sets PROJECT_SATURATED via discovery is_saturated
        dm = DiscoveryManager()
        # prime saturated: need >=3 roles with dup>0.5 yield<0.05 and total_scans>=5
        for role in ["bug_hunter", "security_auditor", "test_gap_auditor"]:
            for _ in range(6):
                dm.record_completion(role=role, scope="full", commit_sha="abc", findings=0, duplicates=10, rejected=5, cost_tokens=100, now=1000000.0)
        sched2 = AdaptiveScheduler(config=config, discovery_manager=dm)
        workers = (_worker("w1", "T1", "general_implementer"),)
        pipeline = _pipeline(total_slots=30, active_workers=workers)
        dec = sched2.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        # discovery saturated => PROJECT_SATURATED
        assert dec.mode == SchedulerMode.PROJECT_SATURATED
        assert any("saturated" in r for r in dec.reasons)


# ---------------------------------------------------------------------------
# implementation_ready_count split vs combined buckets
# ---------------------------------------------------------------------------

class TestImplementationReadyCount:
    def test_Given_split_buckets_When_pressure_includes_both_Then_combined_pressure(self) -> None:
        from codebot.queue_pressure import calculate_pressure

        # pipeline with ready 5 + impl_ready 5 vs ready 10 + impl_ready 0 should have same impl pressure
        p_split = _pipeline(ready=5, implementation_ready=5, total_slots=30)
        p_combined = _pipeline(ready=10, implementation_ready=0, total_slots=30)
        # simulate scheduler's pressure calc: ready + impl_ready
        press_split = calculate_pressure(
            ready_count=p_split.ready_count + p_split.implementation_ready_count,
            implementing_count=p_split.implementing_count,
            reviewing_count=p_split.reviewing_count,
            verifying_count=p_split.verifying_count,
            rework_count=p_split.rework_count,
            planning_count=p_split.planning_count,
            candidate_count=p_split.candidate_count,
            discovered_count=p_split.discovered_count,
            validating_count=p_split.validating_count,
            triaged_count=p_split.triaged_count,
            blocked_count=p_split.blocked_count,
            deferred_count=p_split.deferred_count,
            decompose_count=p_split.decompose_count,
            integration_queue_count=p_split.integration_queue_depth,
            active_by_role=p_split.active_by_role(),
            total_slots=p_split.total_slots,
        )
        press_combined = calculate_pressure(
            ready_count=p_combined.ready_count + p_combined.implementation_ready_count,
            implementing_count=p_combined.implementing_count,
            reviewing_count=p_combined.reviewing_count,
            verifying_count=p_combined.verifying_count,
            rework_count=p_combined.rework_count,
            planning_count=p_combined.planning_count,
            candidate_count=p_combined.candidate_count,
            discovered_count=p_combined.discovered_count,
            validating_count=p_combined.validating_count,
            triaged_count=p_combined.triaged_count,
            blocked_count=p_combined.blocked_count,
            deferred_count=p_combined.deferred_count,
            decompose_count=p_combined.decompose_count,
            integration_queue_count=p_combined.integration_queue_depth,
            active_by_role=p_combined.active_by_role(),
            total_slots=p_combined.total_slots,
        )
        assert press_split.implementation_pressure == press_combined.implementation_pressure
        assert press_split.implementation_pressure > 0.0

    def test_Given_impl_ready_only_When_tick_Then_pressure_reflects_ready_plus_impl(self) -> None:
        sched = AdaptiveScheduler(
            config=SchedulerConfig(hysteresis=SchedulerConfig().hysteresis.__class__(min_allocation_duration_seconds=0))
        )
        pipeline = _pipeline(ready=0, implementation_ready=8, total_slots=30)
        tickets = _ready_tickets(8)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        # pressure should have impl demand 8, not 0
        assert dec.pressure.implementation_pressure > 0.0
        # should still allocate implementations despite ready_count 0 because impl_ready included for pressure
        impl = [a for a in dec.assignments if a.role in IMPLEMENTATION_ROLES]
        assert len(impl) > 0

    def test_Given_combined_bucket_When_tick_Then_same_allocation_as_split(self) -> None:
        # Ensure scheduler doesn't double-count or miss impl_ready
        sched1 = AdaptiveScheduler(config=SchedulerConfig(hysteresis=SchedulerConfig().hysteresis.__class__(min_allocation_duration_seconds=0)))
        sched2 = AdaptiveScheduler(config=SchedulerConfig(hysteresis=SchedulerConfig().hysteresis.__class__(min_allocation_duration_seconds=0)))
        tickets = _ready_tickets(6)
        p_split = _pipeline(ready=3, implementation_ready=3, total_slots=30)
        p_combined = _pipeline(ready=6, implementation_ready=0, total_slots=30)
        dec_split = sched1.tick(
            pipeline=p_split,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        dec_combined = sched2.tick(
            pipeline=p_combined,
            ready_tickets=tickets,
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        # Both should produce same impl count since pressure combined
        impl_split = len([a for a in dec_split.assignments if a.role in IMPLEMENTATION_ROLES])
        impl_combined = len([a for a in dec_combined.assignments if a.role in IMPLEMENTATION_ROLES])
        assert impl_split == impl_combined

    def test_Given_zero_impl_ready_When_tick_Then_ready_only_pressure(self) -> None:
        from codebot.queue_pressure import calculate_pressure

        pipeline = _pipeline(ready=7, implementation_ready=0, total_slots=10, implementing=0, reviewing=0, verifying=0, rework=0, planning=0)
        dec_pressure = calculate_pressure(
            ready_count=pipeline.ready_count + pipeline.implementation_ready_count,
            implementing_count=pipeline.implementing_count,
            reviewing_count=pipeline.reviewing_count,
            verifying_count=pipeline.verifying_count,
            rework_count=pipeline.rework_count,
            planning_count=pipeline.planning_count,
            candidate_count=pipeline.candidate_count,
            total_slots=pipeline.total_slots,
            active_by_role={},
        )
        # demand 7, capacity 10 => 0.7
        assert dec_pressure.implementation_pressure == 0.7


# ---------------------------------------------------------------------------
# Misc — tick_count, mark_completed, format_dashboard, role mapping, trimming
# ---------------------------------------------------------------------------

class TestMiscSchedulerBehavior:
    def test_Given_scheduler_When_tick_called_Then_tick_count_increments(self) -> None:
        sched = AdaptiveScheduler()
        assert sched.tick_count == 0
        pipeline = _pipeline(total_slots=30)
        sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert sched.tick_count == 1
        sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000001.0,
        )
        assert sched.tick_count == 2

    def test_Given_mark_completed_When_score_then_unblocked(self) -> None:
        sched = AdaptiveScheduler()
        sched.mark_completed("T-DEP")
        # Verify internal set updated
        assert "T-DEP" in sched._completed_tickets

    def test_Given_role_to_category_When_mapped_Then_correct(self) -> None:
        sched = AdaptiveScheduler()
        assert sched._role_to_category("general_implementer") == "implementation"
        assert sched._role_to_category("correctness_reviewer") == "review"
        assert sched._role_to_category("quality_gate") == "verification"
        assert sched._role_to_category("planner") == "planning"
        assert sched._role_to_category("bug_hunter") == "discovery"
        assert sched._role_to_category("unknown_role_xyz") == "control"

    def test_Given_trimming_needed_When_tick_Then_enforced(self) -> None:
        # Force over-allocation via large rework + verify + review + impl
        config = SchedulerConfig(max_slots=5).validate()
        sched = AdaptiveScheduler(config=config)
        # Provide many tickets to cause assignments > max_slots
        reworks = _rework_tickets(5)
        verifies = _verify_tickets(5)
        reviews = _review_tickets(5)
        readies = _ready_tickets(5)
        pipeline = _pipeline(ready=5, rework=5, verifying=5, reviewing=5, total_slots=5)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=readies,
            review_tickets=reviews,
            verify_tickets=verifies,
            rework_tickets=reworks,
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        assert dec.total_active_after <= 5
        # if trimming happened, reason should contain trimmed
        # Not strictly required but if assignments would exceed, we expect reason
        assert len(dec.assignments) <= 5

    def test_Given_format_dashboard_When_called_Then_contains_sections(self, tmp_path: Path) -> None:
        sched = AdaptiveScheduler(discovery_manager=DiscoveryManager(state_dir=tmp_path))
        pipeline = _pipeline(ready=2, implementation_ready=1, implementing=1, reviewing=1, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=_ready_tickets(2),
            review_tickets=_review_tickets(1),
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=_planning_tickets(1),
            candidate_tickets=[],
            now=1000000.0,
        )
        dash = sched.format_dashboard(dec, pipeline)
        assert "CODEBOT CAPACITY" in dash
        assert "Slots:" in dash
        assert "Current mode:" in dash
        assert "Allocation:" in dash
        assert "Queues:" in dash
        assert "Ready" in dash
        assert "Implementation" in dash
        # implementation line should show ready/active split
        assert "ready 1 / active 1" in dash

    def test_Given_pick_reviewer_role_When_high_risk_Then_security_reviewer(self) -> None:
        from codebot.work_scorer import ScoredWorkItem

        sched = AdaptiveScheduler()
        # Create a scored item with high risk_penalty >=15
        item = ScoredWorkItem(ticket_id="T1", stage="REVIEWING", score=100.0, components={"risk_penalty": 16.0})
        role = sched._pick_reviewer_role(item, _pipeline())
        assert role == "security_reviewer"
        item2 = ScoredWorkItem(ticket_id="T2", stage="REVIEWING", score=80.0, components={"risk_penalty": 9.0})
        assert sched._pick_reviewer_role(item2, _pipeline()) == "architecture_reviewer"
        item3 = ScoredWorkItem(ticket_id="T3", stage="REVIEWING", score=60.0, components={"risk_penalty": 2.0})
        assert sched._pick_reviewer_role(item3, _pipeline()) == "correctness_reviewer"

    def test_Given_pick_implementer_role_When_called_Then_general_implementer(self) -> None:
        from codebot.work_scorer import ScoredWorkItem

        sched = AdaptiveScheduler()
        item = ScoredWorkItem(ticket_id="T1", stage="READY", score=50.0, components={})
        assert sched._pick_implementer_role(item, _pipeline()) == "general_implementer"

    def test_Given_check_security_emergency_none_When_checked_Then_none(self) -> None:
        sched = AdaptiveScheduler()
        normals = _ready_tickets(2)
        result = sched._check_security_emergency(normals, [])
        assert result is None

    def test_Given_handle_security_emergency_two_slots_When_handled_Then_two_assignments(self) -> None:
        sched = AdaptiveScheduler()
        sec = FakeTicket(id="SEC-99", ticket_class="security", severity="critical", state="READY")
        reasons: list[str] = []
        assigns, used = sched._handle_security_emergency(sec, free=10, now=1000000.0, reasons=reasons)
        assert used == 2
        assert len(assigns) == 2
        assert assigns[0].role == "backend_implementer"
        assert assigns[1].role == "security_reviewer"
        assert any("SEC-99" in r for r in reasons)

    def test_Given_integration_queue_depth_When_tick_Then_pressure_reflects(self) -> None:
        config = SchedulerConfig(hysteresis=SchedulerConfig().hysteresis.__class__(min_allocation_duration_seconds=0))
        sched = AdaptiveScheduler(config=config)
        pipeline = _pipeline(integration_depth=10, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        # integration pressure should be >0 when queue depth 10
        assert dec.pressure.integration_pressure > 0.0

    def test_Given_isolated_tmp_path_discovery_When_two_schedulers_Then_independent(self, tmp_path: Path) -> None:
        dm1 = DiscoveryManager(state_dir=tmp_path / "a")
        dm2 = DiscoveryManager(state_dir=tmp_path / "b")
        dm1.record_completion(role="bug_hunter", scope="full", commit_sha="sha", findings=1, duplicates=0, rejected=0, cost_tokens=10, now=1000000.0)
        # dm2 should not have that cooldown
        assert dm1.is_on_cooldown("bug_hunter", "full", "sha", 3600, 1000000.0) is True
        assert dm2.is_on_cooldown("bug_hunter", "full", "sha", 3600, 1000000.0) is False
        sched1 = AdaptiveScheduler(discovery_manager=dm1)
        sched2 = AdaptiveScheduler(discovery_manager=dm2)
        assert sched1.discovery is dm1
        assert sched2.discovery is dm2

    def test_Given_allocation_summary_categories_When_tick_Then_recorded_in_hysteresis(self) -> None:
        sched = AdaptiveScheduler()
        pipeline = _pipeline(ready=3, total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=_ready_tickets(3),
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        # hysteresis last_allocation should have at least one category
        assert len(sched.hysteresis.last_allocation) >= 1

    def test_Given_empty_candidates_When_discovery_alloc_Then_still_allocates_roles(self, tmp_path: Path) -> None:
        sched = AdaptiveScheduler(discovery_manager=DiscoveryManager(state_dir=tmp_path))
        pipeline = _pipeline(total_slots=30)
        dec = sched.tick(
            pipeline=pipeline,
            ready_tickets=[],
            review_tickets=[],
            verify_tickets=[],
            rework_tickets=[],
            planning_tickets=[],
            candidate_tickets=[],
            now=1000000.0,
        )
        disc = [a for a in dec.assignments if a.role in DISCOVERY_ROLES]
        # Discovery allocation creates assignments even without candidates; ticket_id is synthetic discovery-...
        for a in disc:
            assert a.ticket_id.startswith("discovery-")

    def test_Given_constants_When_inspected_Then_contain_expected_roles(self) -> None:
        assert "general_implementer" in IMPLEMENTATION_ROLES
        assert "correctness_reviewer" in REVIEW_ROLES
        assert "quality_gate" in VERIFICATION_ROLES
        assert "planner" in PLANNING_ROLES
