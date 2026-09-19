"""Tests for queue_pressure.py — pressure calculation and bottleneck detection."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock

from codebot.queue_pressure import (
    QueuePressure,
    SchedulerMode,
    calculate_pressure,
    compute_implementation_cap,
    determine_mode,
    estimate_downstream_demand,
    forecast_downstream_demand,
)
from codebot.pipeline_state import PipelineState, WorkerSlot


def make_ps(**overrides):
    defaults = dict(
        ready_count=0, implementing_count=0, reviewing_count=0,
        verifying_count=0, rework_count=0, planning_count=0,
        candidate_count=0, discovered_count=0, total_slots=30,
        active_workers=(), stuck_workers=(), budget_exhausted=False,
    )
    defaults.update(overrides)
    return PipelineState(**defaults)


def make_worker(role, ticket_id="T-001", now=1000.0):
    return WorkerSlot(worker_id=f"w-{role}", ticket_id=ticket_id, role=role, started_at=now, heartbeat_at=now, lease_expires=now+1000)


class TestQueuePressureDataclass:
    def test_defaults(self):
        qp = QueuePressure()
        assert qp.implementation_pressure == 0.0
        assert qp.backlog_ratio == 0.0

    def test_max_pressure(self):
        qp = QueuePressure(implementation_pressure=1.5, review_pressure=2.0, verification_pressure=0.5)
        assert qp.max_pressure == 2.0

    def test_bottleneck_stage(self):
        qp = QueuePressure(implementation_pressure=1.0, review_pressure=3.0)
        assert qp.bottleneck_stage == "review"

    def test_bottleneck_none_when_all_zero(self):
        qp = QueuePressure()
        assert qp.bottleneck_stage == "none"

    def test_to_dict(self):
        qp = QueuePressure(implementation_pressure=1.234, review_pressure=0.5)
        d = qp.to_dict()
        assert "implementation" in d
        assert "bottleneck" in d
        assert "backlog_ratio" in d


class TestCalculatePressure:
    def test_no_demand_no_pressure(self):
        ps = make_ps()
        qp = calculate_pressure(ps)
        assert qp.implementation_pressure == 0.0
        assert qp.review_pressure == 0.0
        assert qp.verification_pressure == 0.0

    def test_ready_creates_implementation_pressure(self):
        ps = make_ps(ready_count=10, total_slots=30)
        qp = calculate_pressure(ps)
        assert qp.implementation_pressure > 0

    def test_explicit_counts(self):
        qp = calculate_pressure(ready_count=10, total_slots=10, active_by_role={})
        assert qp.implementation_pressure > 0

    def test_review_pressure_from_reviewing_plus_implementing(self):
        qp = calculate_pressure(reviewing_count=5, implementing_count=5, total_slots=30, active_by_role={})
        assert qp.review_pressure > 0

    def test_verification_pressure(self):
        qp = calculate_pressure(verifying_count=5, total_slots=30, active_by_role={})
        assert qp.verification_pressure > 0

    def test_rework_pressure(self):
        qp = calculate_pressure(rework_count=5, total_slots=30)
        assert qp.rework_pressure > 0

    def test_integration_pressure(self):
        qp = calculate_pressure(integration_queue_count=5, total_slots=30)
        assert qp.integration_pressure > 0

    def test_integration_pressure_capped(self):
        qp = calculate_pressure(integration_queue_count=1000, total_slots=10)
        assert qp.integration_pressure <= 5.0

    def test_discovery_pressure_high_when_backlog_low(self):
        ps = make_ps(ready_count=0, planning_count=0, total_slots=30)
        qp = calculate_pressure(ps, backlog_low_watermark=20, backlog_target=50, backlog_high_watermark=100)
        assert qp.discovery_pressure >= 1.0

    def test_discovery_pressure_zero_when_backlog_high(self):
        ps = make_ps(ready_count=100, planning_count=10, total_slots=30)
        qp = calculate_pressure(ps, backlog_low_watermark=20, backlog_target=50, backlog_high_watermark=100)
        assert qp.discovery_pressure == 0.0

    def test_backlog_ratio_bounded(self):
        qp = calculate_pressure(ready_count=1000, total_slots=30)
        assert 0.0 <= qp.backlog_ratio <= 1.0

    def test_pressure_non_negative(self):
        qp = calculate_pressure(ready_count=5, implementing_count=3, reviewing_count=2, verifying_count=1, rework_count=1, planning_count=1, integration_queue_count=2, total_slots=30)
        for attr in ("implementation_pressure", "review_pressure", "verification_pressure", "rework_pressure", "planning_pressure", "discovery_pressure", "integration_pressure"):
            assert getattr(qp, attr) >= 0.0

    def test_with_workers_by_category(self):
        ps = make_ps(ready_count=10, implementing_count=5, total_slots=30)
        w = make_worker("general_implementer")
        ps2 = PipelineState(ready_count=10, implementing_count=5, total_slots=30, active_workers=(w,))
        qp = calculate_pressure(ps2)
        assert qp.implementation_pressure >= 0

    def test_capacity_guarded_against_zero(self):
        qp = calculate_pressure(ready_count=5, total_slots=0)
        assert qp.implementation_pressure >= 0

    def test_backlog_intermediate(self):
        ps = make_ps(ready_count=30, planning_count=0, total_slots=30)
        qp = calculate_pressure(ps)
        assert 0.0 <= qp.backlog_ratio <= 1.0
        assert qp.discovery_pressure >= 0

    def test_pipeline_state_with_active_by_role(self):
        ps = make_ps(ready_count=10, implementing_count=5, total_slots=30)
        w1 = make_worker("general_implementer", ticket_id="T-01")
        w2 = make_worker("correctness_reviewer", ticket_id="T-02")
        ps2 = PipelineState(ready_count=10, implementing_count=5, reviewing_count=1, total_slots=30, active_workers=(w1, w2))
        qp = calculate_pressure(ps2)
        assert qp.implementation_pressure >= 0
        assert qp.review_pressure >= 0


class TestDetermineMode:
    def test_budget_throttled(self):
        ps = make_ps()
        qp = QueuePressure()
        assert determine_mode(qp, ps, budget_exhausted=True) == SchedulerMode.BUDGET_THROTTLED

    def test_idle_when_no_work(self):
        ps = make_ps(ready_count=0, implementing_count=0, reviewing_count=0, verifying_count=0, rework_count=0, planning_count=0, discovered_count=0, candidate_count=0, total_slots=30, active_workers=())
        qp = QueuePressure(discovery_pressure=0.5)
        assert determine_mode(qp, ps) == SchedulerMode.IDLE

    def test_discovery_heavy_when_idle_and_pressure_high(self):
        ps = make_ps(ready_count=0, total_slots=30, active_workers=())
        qp = QueuePressure(discovery_pressure=1.5)
        mode = determine_mode(qp, ps)
        assert mode == SchedulerMode.DISCOVERY_HEAVY

    def test_project_saturated_when_work_zero_but_active(self):
        w = make_worker("general_implementer")
        ps = PipelineState(ready_count=0, implementing_count=0, reviewing_count=0, verifying_count=0, rework_count=0, planning_count=0, discovered_count=0, candidate_count=0, total_slots=30, active_workers=(w,))
        qp = QueuePressure()
        assert determine_mode(qp, ps) == SchedulerMode.PROJECT_SATURATED

    def test_recovery_when_many_stuck(self):
        workers = tuple(make_worker(f"w{i}", ticket_id=f"T-{i}") for i in range(12))
        ps = PipelineState(ready_count=5, total_slots=30, active_workers=workers, stuck_workers=tuple(f"w{i}" for i in range(11)))
        qp = QueuePressure()
        assert determine_mode(qp, ps) == SchedulerMode.RECOVERY

    def test_review_heavy(self):
        ps = make_ps(reviewing_count=10, implementing_count=5, ready_count=5)
        qp = QueuePressure(review_pressure=2.0, implementation_pressure=0.5)
        assert determine_mode(qp, ps) == SchedulerMode.REVIEW_HEAVY

    def test_verification_heavy(self):
        ps = make_ps(verifying_count=10, ready_count=5)
        qp = QueuePressure(verification_pressure=2.0, implementation_pressure=0.5, review_pressure=0.3)
        assert determine_mode(qp, ps) == SchedulerMode.VERIFICATION_HEAVY

    def test_implementation_heavy(self):
        ps = make_ps(ready_count=20, reviewing_count=1, verifying_count=0)
        qp = QueuePressure(implementation_pressure=1.5, review_pressure=0.5)
        assert determine_mode(qp, ps) == SchedulerMode.IMPLEMENTATION_HEAVY

    def test_balanced_default(self):
        ps = make_ps(ready_count=5, implementing_count=2, total_slots=30)
        qp = QueuePressure(implementation_pressure=0.5, review_pressure=0.3)
        mode = determine_mode(qp, ps)
        assert mode == SchedulerMode.BALANCED

    def test_rework_heavy(self):
        ps = make_ps(rework_count=10, ready_count=5, total_slots=10)
        qp = QueuePressure(rework_pressure=1.5, implementation_pressure=0.3, review_pressure=0.3)
        assert determine_mode(qp, ps) == SchedulerMode.REWORK_HEAVY


class TestForecastDownstream:
    def test_basic_forecast(self):
        ps = make_ps(implementing_count=10)
        result = forecast_downstream_demand(ps)
        assert "expected_reviews" in result
        assert "expected_verifications" in result
        assert "expected_reworks" in result
        assert "total_downstream" in result
        assert result["expected_reviews"] == 15

    def test_zero_implementing(self):
        ps = make_ps(implementing_count=0)
        result = forecast_downstream_demand(ps)
        assert result["expected_reviews"] == 0

    def test_custom_rates(self):
        ps = make_ps(implementing_count=10)
        result = forecast_downstream_demand(ps, avg_review_per_impl=2.0, avg_verify_per_review=0.5, avg_rework_rate=0.2)
        assert result["expected_reviews"] == 20
        assert result["expected_verifications"] == 10
        assert result["expected_reworks"] == 4


class TestEstimateDownstreamDemand:
    def test_basic(self):
        result = estimate_downstream_demand(10, 5)
        assert "expected_review_demand" in result
        assert "expected_reworks" in result
        assert "expected_verifications" in result
        assert result["expected_review_demand"] == 15.0

    def test_zero(self):
        result = estimate_downstream_demand(0, 0)
        assert result["expected_review_demand"] == 0.0

    def test_first_pass_rate(self):
        r_high = estimate_downstream_demand(10, 0, first_pass_rate=0.9)
        r_low = estimate_downstream_demand(10, 0, first_pass_rate=0.3)
        assert r_low["expected_reworks"] > r_high["expected_reworks"]


class TestComputeImplementationCap:
    def test_basic_cap(self):
        cap = compute_implementation_cap(review_capacity=5, verification_capacity=5, current_reviewing=2, current_verifying=1)
        assert cap >= 0
        assert cap <= 18

    def test_hard_cap_respected(self):
        cap = compute_implementation_cap(review_capacity=100, verification_capacity=100, current_reviewing=0, current_verifying=0, max_impl_fraction=0.5, total_slots=10)
        assert cap <= 5

    def test_review_congestion_reduces_cap(self):
        cap_free = compute_implementation_cap(review_capacity=5, verification_capacity=5, current_reviewing=0, current_verifying=0)
        cap_busy = compute_implementation_cap(review_capacity=5, verification_capacity=5, current_reviewing=10, current_verifying=0)
        assert cap_busy <= cap_free

    def test_always_at_least_zero(self):
        cap = compute_implementation_cap(review_capacity=0, verification_capacity=0, current_reviewing=100, current_verifying=100)
        assert cap >= 0

    def test_minimum_floor(self):
        cap = compute_implementation_cap(review_capacity=2, verification_capacity=2, current_reviewing=0, current_verifying=0, total_slots=30)
        assert cap >= 1
