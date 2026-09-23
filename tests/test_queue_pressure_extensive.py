#!/usr/bin/env python3
"""Extensive coverage for codebot.queue_pressure — P1 scheduler pressure.

Covers QueuePressure dataclass, SchedulerMode, calculate_pressure,
determine_mode/determine_scheduler_mode, forecast_downstream_demand,
estimate_downstream_demand, compute_implementation_cap.
All edge cases, error paths, and formula branches.
"""

from __future__ import annotations

import types

from codebot.queue_pressure import (
    QueuePressure,
    SchedulerMode,
    calculate_pressure,
    determine_mode,
    determine_scheduler_mode,
    forecast_downstream_demand,
    estimate_downstream_demand,
    compute_implementation_cap,
)


# ---------------------------------------------------------------------------
# Helpers — pure in-memory fakes, no mocks
# ---------------------------------------------------------------------------

class _FakeBacklog:
    low_watermark: int
    target: int

    def __init__(self, low_watermark: int = 20, target: int = 50) -> None:
        self.low_watermark = low_watermark
        self.target = target


class _FakeBacklogLow:
    """Uses 'low' attribute path instead of 'low_watermark'."""
    low: int
    target: int

    def __init__(self, low: int = 20, target: int = 50) -> None:
        self.low = low
        self.target = target


class _FakeConfig:
    backlog: object

    def __init__(self, backlog: object) -> None:
        self.backlog = backlog


class _FakePipeline:
    """Minimal PipelineState-like object for calculate_pressure pipeline_state path."""
    ready_count: int
    implementing_count: int
    reviewing_count: int
    verifying_count: int
    rework_count: int
    planning_count: int
    candidate_count: int
    discovered_count: int
    validating_count: int
    triaged_count: int
    blocked_count: int
    deferred_count: int
    decompose_count: int
    integration_queue_depth: int
    total_slots: int
    _active_by_role: dict[str, int]

    def __init__(
        self,
        ready_count: int = 0,
        implementing_count: int = 0,
        reviewing_count: int = 0,
        verifying_count: int = 0,
        rework_count: int = 0,
        planning_count: int = 0,
        candidate_count: int = 0,
        discovered_count: int = 0,
        validating_count: int = 0,
        triaged_count: int = 0,
        blocked_count: int = 0,
        deferred_count: int = 0,
        decompose_count: int = 0,
        integration_queue_depth: int = 0,
        total_slots: int = 30,
        active_by_role: dict[str, int] | None = None,
        workers_by_category: object | None = None,
        implementation_ready_count: object | None = None,
    ) -> None:
        self.ready_count = ready_count
        self.implementing_count = implementing_count
        self.reviewing_count = reviewing_count
        self.verifying_count = verifying_count
        self.rework_count = rework_count
        self.planning_count = planning_count
        self.candidate_count = candidate_count
        self.discovered_count = discovered_count
        self.validating_count = validating_count
        self.triaged_count = triaged_count
        self.blocked_count = blocked_count
        self.deferred_count = deferred_count
        self.decompose_count = decompose_count
        self.integration_queue_depth = integration_queue_depth
        self.total_slots = total_slots
        if active_by_role is not None:
            self._active_by_role = active_by_role
        if workers_by_category is not None:
            object.__setattr__(self, "workers_by_category", workers_by_category)
        if implementation_ready_count is not None:
            object.__setattr__(self, "implementation_ready_count", implementation_ready_count)

    def active_by_role(self) -> dict[str, int]:
        return getattr(self, "_active_by_role", {})


class _FakePipelineWithWorkersFn:
    """Pipeline where workers_by_category is callable returning dict."""
    ready_count: int
    implementing_count: int
    reviewing_count: int
    verifying_count: int
    rework_count: int
    planning_count: int
    candidate_count: int
    discovered_count: int
    validating_count: int
    triaged_count: int
    blocked_count: int
    deferred_count: int
    decompose_count: int
    integration_queue_depth: int
    total_slots: int
    _cat: dict[str, int]

    def __init__(self, cat_dict: dict[str, int]) -> None:
        self.ready_count = 0
        self.implementing_count = 0
        self.reviewing_count = 0
        self.verifying_count = 0
        self.rework_count = 0
        self.planning_count = 0
        self.candidate_count = 0
        self.discovered_count = 0
        self.validating_count = 0
        self.triaged_count = 0
        self.blocked_count = 0
        self.deferred_count = 0
        self.decompose_count = 0
        self.integration_queue_depth = 0
        self.total_slots = 30

        self._cat = cat_dict

    def workers_by_category(self) -> dict[str, int]:
        return self._cat


class _FakePipelineActiveDict:
    """Pipeline where workers_by_category is a dict attribute (not callable)."""
    ready_count: int
    implementing_count: int
    reviewing_count: int
    verifying_count: int
    rework_count: int
    planning_count: int
    candidate_count: int
    discovered_count: int
    validating_count: int
    triaged_count: int
    blocked_count: int
    deferred_count: int
    decompose_count: int
    integration_queue_depth: int
    total_slots: int
    workers_by_category: dict[str, int]

    def __init__(self, cat_dict: dict[str, int]) -> None:
        self.ready_count = 2
        self.implementing_count = 0
        self.reviewing_count = 0
        self.verifying_count = 0
        self.rework_count = 0
        self.planning_count = 0
        self.candidate_count = 0
        self.discovered_count = 0
        self.validating_count = 0
        self.triaged_count = 0
        self.blocked_count = 0
        self.deferred_count = 0
        self.decompose_count = 0
        self.integration_queue_depth = 0
        self.total_slots = 30
        self.workers_by_category = cat_dict


class _SimpleStateForMode:
    ready_count: int
    decompose_count: int
    implementing_count: int
    reviewing_count: int
    verifying_count: int
    rework_count: int
    planning_count: int
    discovered_count: int
    candidate_count: int

    def __init__(
        self,
        ready_count: int = 0,
        decompose_count: int = 0,
        implementing_count: int = 0,
        reviewing_count: int = 0,
        verifying_count: int = 0,
        rework_count: int = 0,
        planning_count: int = 0,
        discovered_count: int = 0,
        candidate_count: int = 0,
        active_count: int | None = None,
        total_active: int | None = None,
        stuck_count: int | None = None,
        stuck_workers: tuple[str, ...] | None = None,
        total_slots: int | None = None,
        max_slots: int | None = None,
    ) -> None:
        self.ready_count = ready_count
        self.decompose_count = decompose_count
        self.implementing_count = implementing_count
        self.reviewing_count = reviewing_count
        self.verifying_count = verifying_count
        self.rework_count = rework_count
        self.planning_count = planning_count
        self.discovered_count = discovered_count
        self.candidate_count = candidate_count
        if active_count is not None:
            object.__setattr__(self, "active_count", active_count)
        elif total_active is not None:
            object.__setattr__(self, "total_active", total_active)
        if stuck_count is not None:
            object.__setattr__(self, "stuck_count", stuck_count)
        elif stuck_workers is not None:
            object.__setattr__(self, "stuck_workers", stuck_workers)
        if total_slots is not None:
            object.__setattr__(self, "total_slots", total_slots)
        elif max_slots is not None:
            object.__setattr__(self, "max_slots", max_slots)


class _ForecastState:
    implementing_count: int

    def __init__(self, implementing_count: int) -> None:
        self.implementing_count = implementing_count


# ---------------------------------------------------------------------------
# QueuePressure dataclass
# ---------------------------------------------------------------------------

class TestQueuePressureMaxPressure:
    def test_given_all_zero_when_max_pressure_then_zero(self) -> None:
        qp = QueuePressure()
        result = qp.max_pressure
        assert result == 0.0

    def test_given_mixed_pressures_when_max_pressure_then_returns_highest(self) -> None:
        qp = QueuePressure(
            implementation_pressure=0.5,
            review_pressure=1.2,
            verification_pressure=0.3,
            rework_pressure=0.9,
            planning_pressure=0.1,
            integration_pressure=0.4,
        )
        assert qp.max_pressure == 1.2

    def test_given_discovery_high_but_excluded_when_max_pressure_then_ignores_discovery(self) -> None:
        qp = QueuePressure(discovery_pressure=5.0, implementation_pressure=0.2)
        assert qp.max_pressure == 0.2

    def test_given_integration_highest_when_max_pressure_then_integration(self) -> None:
        qp = QueuePressure(integration_pressure=2.5, review_pressure=1.0)
        assert qp.max_pressure == 2.5


class TestQueuePressureBottleneck:
    def test_given_all_zero_when_bottleneck_stage_then_none(self) -> None:
        qp = QueuePressure()
        assert qp.bottleneck_stage == "none"

    def test_given_single_positive_when_bottleneck_stage_then_that_stage(self) -> None:
        qp = QueuePressure(review_pressure=1.5)
        assert qp.bottleneck_stage == "review"

    def test_given_implementation_highest_when_bottleneck_stage_then_implementation(self) -> None:
        qp = QueuePressure(implementation_pressure=3.0, review_pressure=1.0, verification_pressure=0.5)
        assert qp.bottleneck_stage == "implementation"

    def test_given_tie_when_bottleneck_stage_then_first_in_insertion_order(self) -> None:
        qp = QueuePressure(implementation_pressure=2.0, review_pressure=2.0)
        # insertion order: implementation before review
        assert qp.bottleneck_stage == "implementation"

    def test_given_discovery_high_when_bottleneck_then_not_discovery(self) -> None:
        qp = QueuePressure(discovery_pressure=10.0, review_pressure=0.5)
        # discovery is excluded from bottleneck map
        assert qp.bottleneck_stage == "review"


class TestQueuePressureToDict:
    def test_given_pressures_when_to_dict_then_rounds_and_bottleneck(self) -> None:
        qp = QueuePressure(
            implementation_pressure=0.12345,
            review_pressure=1.23456,
            verification_pressure=0.0,
            rework_pressure=0.0,
            planning_pressure=0.0,
            discovery_pressure=1.9999,
            integration_pressure=0.55555,
            backlog_ratio=0.33333,
        )
        d = qp.to_dict()
        assert d["implementation"] == round(0.12345, 3)
        assert d["review"] == round(1.23456, 3)
        assert d["discovery"] == round(1.9999, 3)
        assert d["integration"] == round(0.55555, 3)
        assert d["backlog_ratio"] == round(0.33333, 3)
        assert d["bottleneck"] == "review"

    def test_given_all_zero_when_to_dict_then_bottleneck_none(self) -> None:
        qp = QueuePressure()
        d = qp.to_dict()
        assert d["bottleneck"] == "none"
        assert d["backlog_ratio"] == 0.0


class TestSchedulerModeEnum:
    def test_given_enum_when_values_then_all_expected_present(self) -> None:
        values = {m.value for m in SchedulerMode}
        assert values == {
            "DISCOVERY_HEAVY",
            "IMPLEMENTATION_HEAVY",
            "REVIEW_HEAVY",
            "VERIFICATION_HEAVY",
            "REWORK_HEAVY",
            "BALANCED",
            "RECOVERY",
            "BUDGET_THROTTLED",
            "SATURATED",
            "PROJECT_SATURATED",
            "IDLE",
        }

    def test_given_string_value_when_enum_then_matches(self) -> None:
        assert SchedulerMode("DISCOVERY_HEAVY") is SchedulerMode.DISCOVERY_HEAVY
        assert SchedulerMode.BALANCED.value == "BALANCED"


# ---------------------------------------------------------------------------
# calculate_pressure — pure kwarg path
# ---------------------------------------------------------------------------

class TestCalculatePressureKwargPath:
    def test_given_no_args_when_calculate_then_discovery_high_backlog_low(self) -> None:
        pressure = calculate_pressure()
        # backlog 0, non_complete_total 0 => discovery 2.0 (1.0 + 20/20)
        assert pressure.discovery_pressure == 2.0
        assert pressure.implementation_pressure == 0.0
        assert pressure.review_pressure == 0.0
        assert pressure.backlog_ratio == 0.0

    def test_given_zero_total_slots_when_calculate_then_guards_to_30(self) -> None:
        pressure = calculate_pressure(ready_count=10, total_slots=0, active_by_role={})
        # total becomes 30, free 30, impl_capacity 30, demand 10 => 0.3333
        assert pressure.implementation_pressure == round(10 / 30, 4)

    def test_given_empty_active_by_role_when_implementation_then_free_equals_total(self) -> None:
        pressure = calculate_pressure(ready_count=15, total_slots=10, active_by_role={})
        # total 10, free 10, impl_capacity 10, demand 15 => 1.5
        assert pressure.implementation_pressure == 1.5

    def test_given_implementer_roles_when_capacity_then_sums_implementer_substring(self) -> None:
        pressure = calculate_pressure(
            ready_count=10,
            total_slots=10,
            active_by_role={"general_implementer": 2, "backend_implementer": 3, "correctness_reviewer": 1},
        )
        # sum=6 free=4 impl_active=5 impl_capacity=9 demand10 => 1.1111
        assert pressure.implementation_pressure == round(10 / 9, 4)

    def test_given_review_capacity_when_review_pressure_then_correct(self) -> None:
        pressure = calculate_pressure(
            implementing_count=12,
            reviewing_count=3,
            total_slots=10,
            active_by_role={"correctness_reviewer": 2},
        )
        # free=8 review_active=2 capacity=10 demand=15 =>1.5
        assert pressure.review_pressure == 1.5

    def test_given_verification_counts_when_verification_then_correct(self) -> None:
        pressure = calculate_pressure(
            verifying_count=6,
            total_slots=10,
            active_by_role={"quality_gate": 1, "verifier": 1},
        )
        # free=8 verify_active=2 capacity=10 demand6 =>0.6
        assert pressure.verification_pressure == 0.6

    def test_given_rework_counts_when_rework_then_based_on_total(self) -> None:
        pressure = calculate_pressure(rework_count=6, total_slots=10, active_by_role={})
        # total 10 capacity 10 demand6 =>0.6
        assert pressure.rework_pressure == 0.6

    def test_given_planning_counts_when_planning_then_correct(self) -> None:
        pressure = calculate_pressure(
            planning_count=4,
            total_slots=10,
            active_by_role={"planner": 1, "validator": 1},
        )
        # free=8 planning_active=2 capacity10 demand4 =>0.4
        assert pressure.planning_pressure == 0.4

    def test_given_integration_queue_when_pressure_then_capped_at_five(self) -> None:
        pressure = calculate_pressure(integration_queue_count=100, total_slots=10, active_by_role={})
        # capacity 5 (10//2) demand100 =>20 but capped 5.0
        assert pressure.integration_pressure == 5.0

    def test_given_integration_zero_when_pressure_then_zero(self) -> None:
        pressure = calculate_pressure(integration_queue_count=0, total_slots=10, active_by_role={})
        assert pressure.integration_pressure == 0.0

    def test_given_zero_demand_when_pressure_then_zero_despite_capacity(self) -> None:
        pressure = calculate_pressure(
            ready_count=0,
            implementing_count=0,
            reviewing_count=0,
            verifying_count=0,
            rework_count=0,
            planning_count=0,
            total_slots=10,
            active_by_role={"general_implementer": 5},
        )
        assert pressure.implementation_pressure == 0.0
        assert pressure.review_pressure == 0.0
        assert pressure.verification_pressure == 0.0
        assert pressure.rework_pressure == 0.0
        assert pressure.planning_pressure == 0.0


# ---------------------------------------------------------------------------
# calculate_pressure — discovery / backlog branches
# ---------------------------------------------------------------------------

class TestCalculatePressureDiscovery:
    def test_given_non_empty_pipeline_when_discovery_then_suppressed(self) -> None:
        pressure = calculate_pressure(
            ready_count=5,
            implementing_count=0,
            reviewing_count=0,
            verifying_count=0,
            rework_count=0,
            planning_count=0,
            candidate_count=0,
            total_slots=30,
            active_by_role={},
        )
        assert pressure.discovery_pressure == 0.0

    def test_given_empty_pipeline_when_discovery_then_high(self) -> None:
        pressure = calculate_pressure(
            ready_count=0,
            implementing_count=0,
            reviewing_count=0,
            verifying_count=0,
            rework_count=0,
            planning_count=0,
            candidate_count=0,
            total_slots=30,
            active_by_role={},
        )
        assert pressure.discovery_pressure == 2.0
        assert pressure.backlog_ratio == 0.0

    def test_given_discovery_currently_active_below_threshold_when_emptyish_then_not_suppressed(self) -> None:
        # discovery_active True, non_complete_total=1 (<20) => not suppressed, backlog small
        pressure = calculate_pressure(
            ready_count=1,
            total_slots=30,
            active_by_role={},
            discovery_currently_active=True,
            discovery_close_threshold=20,
        )
        assert pressure.discovery_pressure > 0.0

    def test_given_discovery_active_above_threshold_when_non_complete_then_suppressed(self) -> None:
        pressure = calculate_pressure(
            ready_count=10,
            implementing_count=10,
            total_slots=30,
            active_by_role={},
            discovery_currently_active=True,
            discovery_close_threshold=20,
        )
        # non_complete_total=20 >= threshold => suppressed
        assert pressure.discovery_pressure == 0.0

    def test_given_backlog_above_high_when_suppressed_then_ratio_one(self) -> None:
        pressure = calculate_pressure(
            ready_count=100,
            decompose_count=0,
            planning_count=5,
            total_slots=30,
            active_by_role={},
        )
        # backlog 105 >= high 100, and non_complete_total>0 => suppressed, ratio1
        assert pressure.backlog_ratio == 1.0
        assert pressure.discovery_pressure == 0.0

    def test_given_backlog_between_target_and_high_when_suppressed_then_ratio_fraction(self) -> None:
        # backlog 75, target50 high100 => ratio (75-50)/50=0.5
        pressure = calculate_pressure(
            ready_count=70,
            planning_count=5,
            total_slots=30,
            active_by_role={},
        )
        assert pressure.backlog_ratio == round((75 - 50) / 50, 4)
        assert pressure.discovery_pressure == 0.0

    def test_given_backlog_between_low_and_target_when_suppressed_then_half_scaled(self) -> None:
        # backlog 35, low20 target50 => (35-20)/30*0.5=0.25
        pressure = calculate_pressure(
            ready_count=35,
            total_slots=30,
            active_by_role={},
        )
        assert pressure.backlog_ratio == round((35 - 20) / 30 * 0.5, 4)

    def test_given_backlog_below_low_when_suppressed_then_quarter_scaled(self) -> None:
        # backlog 10, low20 => 10/20*0.25=0.125
        pressure = calculate_pressure(
            ready_count=10,
            total_slots=30,
            active_by_role={},
        )
        # but non_complete_total>0 so suppressed path
        assert pressure.backlog_ratio == round(10 / 20 * 0.25, 4)

    def test_given_unsuppressed_backlog_between_target_and_high_when_discovery_then_low_discovery(self) -> None:
        pressure = calculate_pressure(
            ready_count=60,
            decompose_count=0,
            planning_count=0,
            candidate_count=0,
            discovered_count=0,
            validating_count=0,
            triaged_count=0,
            implementing_count=0,
            reviewing_count=0,
            verifying_count=0,
            rework_count=0,
            blocked_count=0,
            deferred_count=0,
            total_slots=30,
            active_by_role={},
            discovery_currently_active=True,
            discovery_close_threshold=100,
        )
        # non_complete=60 <100 => not suppressed, backlog 60 >=target50 <high100
        # ratio (60-50)/50=0.2 discovery 0.1
        assert pressure.backlog_ratio == round((60 - 50) / 50, 4)
        assert pressure.discovery_pressure == 0.1

    def test_given_unsuppressed_backlog_between_low_and_target_when_discovery_then_mid(self) -> None:
        pressure = calculate_pressure(
            ready_count=30,
            total_slots=30,
            active_by_role={},
            discovery_currently_active=True,
            discovery_close_threshold=100,
        )
        # backlog30 low20 target50 => ratio (30-20)/30*0.5=0.1666..., discovery 0.5+0.3*(1-ratio)=0.5+0.3*0.8333=0.75
        expected_ratio = (30 - 20) / 30 * 0.5
        expected_disc = 0.5 + 0.3 * (1.0 - expected_ratio)
        assert pressure.backlog_ratio == round(expected_ratio, 4)
        assert pressure.discovery_pressure == round(expected_disc, 4)

    def test_given_unsuppressed_backlog_below_low_when_discovery_then_high_discovery(self) -> None:
        pressure = calculate_pressure(
            ready_count=5,
            total_slots=30,
            active_by_role={},
            discovery_currently_active=True,
            discovery_close_threshold=100,
        )
        # backlog5 low20 => ratio 5/20*0.25=0.0625 discovery 1.0+(20-5)/20=1.75
        assert pressure.backlog_ratio == round(5 / 20 * 0.25, 4)
        assert pressure.discovery_pressure == round(1.0 + (20 - 5) / 20, 4)

    def test_given_zero_low_watermark_when_unsuppressed_then_avoids_div_zero(self) -> None:
        pressure = calculate_pressure(
            ready_count=0,
            total_slots=30,
            active_by_role={},
            backlog_low_watermark=0,
            backlog_target=50,
            backlog_high_watermark=100,
            discovery_currently_active=True,
            discovery_close_threshold=100,
        )
        assert pressure.backlog_ratio == 0.0
        assert pressure.discovery_pressure == 0.8

    def test_given_low_equals_target_high_equals_target_when_ratio_then_guarded_by_one(self) -> None:
        pressure = calculate_pressure(
            ready_count=10,
            total_slots=30,
            active_by_role={},
            backlog_low_watermark=10,
            backlog_target=10,
            backlog_high_watermark=10,
            discovery_currently_active=True,
            discovery_close_threshold=100,
        )
        # backlog10 >=high10 => but unsuppressed path backlog>=high -> ratio1, discovery0
        # Wait discovery_active True with non_complete 10 <100 => not suppressed, backlog10 >=10 => discovery0 ratio1
        assert pressure.backlog_ratio == 1.0
        assert pressure.discovery_pressure == 0.0


# ---------------------------------------------------------------------------
# calculate_pressure — pipeline_state path
# ---------------------------------------------------------------------------

class TestCalculatePressurePipelineState:
    def test_given_pipeline_state_when_calculate_then_extracts_counts(self) -> None:
        ps = _FakePipeline(
            ready_count=4,
            implementing_count=2,
            reviewing_count=1,
            verifying_count=1,
            rework_count=1,
            planning_count=1,
            candidate_count=2,
            total_slots=20,
            active_by_role={"general_implementer": 1},
        )
        pressure = calculate_pressure(ps)
        # non_complete includes ready etc => >0 => suppressed discovery 0
        assert pressure.discovery_pressure == 0.0
        # impl demand 4+1=5 capacity impl_active (1) + free (19) =20 =>0.25
        assert pressure.implementation_pressure == round(5 / 20, 4)

    def test_given_pipeline_with_implementation_ready_count_when_calculate_then_adds(self) -> None:
        ps = _FakePipeline(
            ready_count=5,
            total_slots=10,
            active_by_role={},
            implementation_ready_count=10,
        )
        pressure = calculate_pressure(ps)
        # impl_demand 5+0+10=15 capacity10 =>1.5
        assert pressure.implementation_pressure == 1.5

    def test_given_pipeline_with_bad_implementation_ready_when_calculate_then_ignores(self) -> None:
        ps = _FakePipeline(
            ready_count=5,
            total_slots=10,
            active_by_role={},
            implementation_ready_count="not_a_number",
        )
        pressure = calculate_pressure(ps)
        assert pressure.implementation_pressure == round(5 / 10, 4)

    def test_given_pipeline_with_workers_callable_when_calculate_then_uses_it(self) -> None:
        ps = _FakePipelineWithWorkersFn({"general_implementer": 2})
        # When explicit ready_count supplied, pipeline_state extraction is skipped → uses explicit count
        pressure_explicit = calculate_pressure(ps, ready_count=5)
        assert pressure_explicit.implementation_pressure == round(5 / 30, 4)
        p2 = calculate_pressure(ps)
        # impl_active via workers_by_category not used for active_by_role? Let's trace: cats = ps.workers_by_category() => {"general_implementer":2}
        # active_by_role = cats => {"general_implementer":2}
        # total 30 free 28 impl_active 2 capacity30 demand0 =>0
        assert p2.implementation_pressure == 0.0

    def test_given_pipeline_with_workers_dict_when_calculate_then_uses_dict(self) -> None:
        ps = _FakePipelineActiveDict({"general_implementer": 3})
        p = calculate_pressure(ps)
        # active_by_role = {"general_implementer":3} free 27 impl_active 3 capacity30 demand2? ready2 =>2/30=0.0667
        assert p.implementation_pressure == round(2 / 30, 4)

    def test_given_pipeline_and_config_when_calculate_then_uses_config_watermarks(self) -> None:
        ps = _FakePipeline(ready_count=10, total_slots=30, active_by_role={})
        cfg = _FakeConfig(_FakeBacklog(low_watermark=5, target=15))
        p = calculate_pressure(ps, cfg)
        # backlog10 >=? with low5 target15 => backlog between low and target => ratio (10-5)/(15-5)*0.5=0.25
        # non_complete 10>0 suppressed => discovery0
        assert p.backlog_ratio == round((10 - 5) / (15 - 5) * 0.5, 4)

    def test_given_pipeline_and_config_low_attr_when_calculate_then_fallback_low(self) -> None:
        ps = _FakePipeline(ready_count=10, total_slots=30, active_by_role={})
        cfg = _FakeConfig(_FakeBacklogLow(low=5, target=15))
        p = calculate_pressure(ps, cfg)
        assert p.backlog_ratio == round((10 - 5) / (15 - 5) * 0.5, 4)

    def test_given_pipeline_with_integration_queue_depth_attr_when_calculate_then_uses_depth(self) -> None:
        ps = _FakePipeline(integration_queue_depth=9, total_slots=10, active_by_role={})
        p = calculate_pressure(ps)
        # capacity 5 demand9 =>1.8
        assert p.integration_pressure == 1.8

    def test_given_pipeline_with_max_slots_fallback_when_calculate_then_uses_max_slots(self) -> None:
        ps = types.SimpleNamespace(
            ready_count=5,
            implementing_count=0,
            reviewing_count=0,
            verifying_count=0,
            rework_count=0,
            planning_count=0,
            candidate_count=0,
            discovered_count=0,
            validating_count=0,
            triaged_count=0,
            blocked_count=0,
            deferred_count=0,
            decompose_count=0,
            max_slots=10,
            workers_by_category={},
        )
        p = calculate_pressure(ps)
        # total 10, demand5 capacity10 =>0.5 suppressed? non_complete 5>0 => discovery0
        assert p.implementation_pressure == 0.5

    def test_given_pipeline_with_none_implementation_ready_when_calculate_then_ignored(self) -> None:
        ps2 = types.SimpleNamespace(
            ready_count=5,
            implementing_count=0,
            reviewing_count=0,
            verifying_count=0,
            rework_count=0,
            planning_count=0,
            candidate_count=0,
            discovered_count=0,
            validating_count=0,
            triaged_count=0,
            blocked_count=0,
            deferred_count=0,
            decompose_count=0,
            integration_queue_depth=0,
            total_slots=10,
            workers_by_category={},
            implementation_ready_count=None,
        )
        p = calculate_pressure(ps2)
        # TypeError on int(None) caught => demand stays 5
        assert p.implementation_pressure == 0.5


# ---------------------------------------------------------------------------
# determine_mode
# ---------------------------------------------------------------------------

class TestDetermineMode:
    def test_given_budget_exhausted_when_determine_mode_then_budget_throttled(self) -> None:
        pressure = QueuePressure()
        state = _SimpleStateForMode()
        mode = determine_mode(pressure, state, budget_exhausted=True)
        assert mode is SchedulerMode.BUDGET_THROTTLED

    def test_given_stuck_over_third_when_determine_mode_then_recovery(self) -> None:
        pressure = QueuePressure()
        state = _SimpleStateForMode(stuck_count=11, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.RECOVERY

    def test_given_stuck_exact_third_when_determine_mode_then_not_recovery(self) -> None:
        pressure = QueuePressure(implementation_pressure=0.1)
        state = _SimpleStateForMode(
            ready_count=5,
            stuck_count=10,
            total_slots=30,
        )
        mode = determine_mode(pressure, state)
        assert mode is not SchedulerMode.RECOVERY

    def test_given_no_work_no_active_low_discovery_when_determine_mode_then_idle(self) -> None:
        pressure = QueuePressure(discovery_pressure=0.5)
        state = _SimpleStateForMode(active_count=0, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.IDLE

    def test_given_no_work_no_active_high_discovery_when_determine_mode_then_discovery_heavy(self) -> None:
        pressure = QueuePressure(discovery_pressure=1.5)
        state = _SimpleStateForMode(active_count=0, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.DISCOVERY_HEAVY

    def test_given_no_work_with_active_when_determine_mode_then_project_saturated(self) -> None:
        pressure = QueuePressure()
        state = _SimpleStateForMode(active_count=5, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.PROJECT_SATURATED

    def test_given_no_work_with_active_via_total_active_fallback_when_determine_mode_then_project_saturated(self) -> None:
        pressure = QueuePressure()
        state = _SimpleStateForMode(total_active=2, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.PROJECT_SATURATED

    def test_given_stuck_workers_tuple_fallback_when_determine_mode_then_recovery(self) -> None:
        pressure = QueuePressure()
        state = _SimpleStateForMode(stuck_workers=tuple(f"w{i}" for i in range(11)), total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.RECOVERY

    def test_given_review_congestion_when_determine_mode_then_review_heavy(self) -> None:
        pressure = QueuePressure(review_pressure=2.0, implementation_pressure=0.5)
        state = _SimpleStateForMode(ready_count=10, implementing_count=5, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.REVIEW_HEAVY

    def test_given_review_high_but_not_dominant_when_determine_mode_then_not_review_heavy(self) -> None:
        # review 1.5, max 2.0 from verification, 1.5 < 2.0*0.8=1.6 => not review heavy
        pressure = QueuePressure(review_pressure=1.5, verification_pressure=2.0)
        state = _SimpleStateForMode(ready_count=5, total_slots=30)
        mode = determine_mode(pressure, state)
        assert mode is not SchedulerMode.REVIEW_HEAVY

    def test_given_verification_congestion_when_determine_mode_then_verification_heavy(self) -> None:
        pressure = QueuePressure(verification_pressure=2.0, review_pressure=0.5)
        state = _SimpleStateForMode(ready_count=5, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.VERIFICATION_HEAVY

    def test_given_rework_congestion_when_determine_mode_then_rework_heavy(self) -> None:
        pressure = QueuePressure(rework_pressure=1.2, review_pressure=0.5, implementation_pressure=0.5)
        state = _SimpleStateForMode(ready_count=5, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.REWORK_HEAVY

    def test_given_rework_high_but_not_dominant_when_determine_mode_then_not_rework_heavy(self) -> None:
        pressure = QueuePressure(rework_pressure=1.0, review_pressure=2.0)
        state = _SimpleStateForMode(ready_count=5, total_slots=30)
        assert determine_mode(pressure, state) is not SchedulerMode.REWORK_HEAVY

    def test_given_discovery_high_and_impl_low_when_determine_mode_then_discovery_heavy(self) -> None:
        pressure = QueuePressure(discovery_pressure=1.5, implementation_pressure=0.3)
        state = _SimpleStateForMode(ready_count=5, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.DISCOVERY_HEAVY

    def test_given_discovery_high_but_impl_high_when_determine_mode_then_not_discovery_heavy(self) -> None:
        pressure = QueuePressure(discovery_pressure=1.5, implementation_pressure=0.8)
        state = _SimpleStateForMode(ready_count=5, total_slots=30)
        assert determine_mode(pressure, state) is not SchedulerMode.DISCOVERY_HEAVY

    def test_given_implementation_high_review_low_when_determine_mode_then_implementation_heavy(self) -> None:
        pressure = QueuePressure(implementation_pressure=1.5, review_pressure=0.5)
        state = _SimpleStateForMode(ready_count=10, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.IMPLEMENTATION_HEAVY

    def test_given_implementation_high_but_review_high_when_determine_mode_then_not_impl_heavy(self) -> None:
        pressure = QueuePressure(implementation_pressure=1.5, review_pressure=1.5)
        state = _SimpleStateForMode(ready_count=10, total_slots=30)
        assert determine_mode(pressure, state) is not SchedulerMode.IMPLEMENTATION_HEAVY

    def test_given_balanced_when_determine_mode_then_balanced(self) -> None:
        pressure = QueuePressure(implementation_pressure=0.5, review_pressure=0.3, verification_pressure=0.2)
        state = _SimpleStateForMode(ready_count=5, total_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.BALANCED

    def test_given_max_slots_fallback_when_determine_mode_then_recovery_evaluated(self) -> None:
        pressure = QueuePressure()
        state = _SimpleStateForMode(stuck_count=11, max_slots=30)
        assert determine_mode(pressure, state) is SchedulerMode.RECOVERY

    def test_given_determine_scheduler_mode_alias_when_called_then_same_as_determine_mode(self) -> None:
        pressure = QueuePressure(implementation_pressure=0.5, review_pressure=0.3)
        state = _SimpleStateForMode(ready_count=5, total_slots=30)
        assert determine_scheduler_mode(pressure, state) is determine_mode(pressure, state)


# ---------------------------------------------------------------------------
# forecast_downstream_demand
# ---------------------------------------------------------------------------

class TestForecastDownstreamDemand:
    def test_given_zero_implementing_when_forecast_then_all_zero(self) -> None:
        state = _ForecastState(0)
        result = forecast_downstream_demand(state)
        assert result["expected_reviews"] == 0
        assert result["expected_verifications"] == 0
        assert result["expected_reworks"] == 0
        assert result["total_downstream"] == 0

    def test_given_two_implementing_when_forecast_then_calculated(self) -> None:
        state = _ForecastState(2)
        result = forecast_downstream_demand(state, avg_review_per_impl=1.5, avg_verify_per_review=0.3, avg_rework_rate=0.15)
        # est_reviews int(2*1.5)=3, verif int(3*0.3)=0, reworks int(3*0.15)=0, total 3
        assert result["expected_reviews"] == 3
        assert result["expected_verifications"] == 0
        assert result["expected_reworks"] == 0
        assert result["total_downstream"] == 3

    def test_given_ten_implementing_when_forecast_then_proportional(self) -> None:
        state = _ForecastState(10)
        result = forecast_downstream_demand(state)
        assert result["expected_reviews"] == 15
        assert result["expected_verifications"] == int(15 * 0.3)
        assert result["expected_reworks"] == int(15 * 0.15)
        assert result["total_downstream"] == result["expected_reviews"] + result["expected_verifications"] + result["expected_reworks"]

    def test_given_custom_rates_when_forecast_then_uses_rates(self) -> None:
        state = _ForecastState(4)
        result = forecast_downstream_demand(state, avg_review_per_impl=2.0, avg_verify_per_review=0.5, avg_rework_rate=0.5)
        assert result["expected_reviews"] == 8
        assert result["expected_verifications"] == 4
        assert result["expected_reworks"] == 4


# ---------------------------------------------------------------------------
# estimate_downstream_demand
# ---------------------------------------------------------------------------

class TestEstimateDownstreamDemand:
    def test_given_zero_counts_when_estimate_then_all_zero(self) -> None:
        result = estimate_downstream_demand(0, 0)
        assert result["expected_review_demand"] == 0.0
        assert result["expected_security_reviews"] == 0.0
        assert result["expected_reworks"] == 0.0
        assert result["expected_verifications"] == 0.0

    def test_given_implementing_and_reviewing_when_estimate_then_formula(self) -> None:
        result = estimate_downstream_demand(implementing_count=10, reviewing_count=5, first_pass_rate=0.7, security_review_fraction=0.2)
        # incoming 10, total 15, security 3.0, reworks 4.5, verifs 10.5
        assert result["expected_review_demand"] == 15.0
        assert result["expected_security_reviews"] == 3.0
        assert result["expected_reworks"] == 4.5
        assert result["expected_verifications"] == 10.5

    def test_given_high_first_pass_when_estimate_then_fewer_reworks(self) -> None:
        low = estimate_downstream_demand(5, 5, first_pass_rate=0.5)
        high = estimate_downstream_demand(5, 5, first_pass_rate=0.9)
        assert high["expected_reworks"] < low["expected_reworks"]
        assert high["expected_verifications"] > low["expected_verifications"]

    def test_given_security_fraction_zero_when_estimate_then_no_security(self) -> None:
        result = estimate_downstream_demand(5, 0, security_review_fraction=0.0)
        assert result["expected_security_reviews"] == 0.0


# ---------------------------------------------------------------------------
# compute_implementation_cap
# ---------------------------------------------------------------------------

class TestComputeImplementationCap:
    def test_given_review_saturated_when_cap_then_bounded(self) -> None:
        cap = compute_implementation_cap(
            review_capacity=2,
            verification_capacity=2,
            current_reviewing=10,
            current_verifying=1,
            max_impl_fraction=0.6,
            total_slots=30,
        )
        assert cap <= 18
        assert cap < 10

    def test_given_no_reviewers_no_load_when_cap_then_allows_floor(self) -> None:
        cap = compute_implementation_cap(
            review_capacity=0,
            verification_capacity=0,
            current_reviewing=0,
            current_verifying=0,
            max_impl_fraction=0.6,
            total_slots=30,
        )
        # hard_cap 18, review_headroom 0 => review_bound0, verify_bound0 => bounded0 => floor 3
        assert cap == 3

    def test_given_hard_cap_limits_when_cap_then_not_exceed_hard(self) -> None:
        cap = compute_implementation_cap(
            review_capacity=100,
            verification_capacity=100,
            current_reviewing=0,
            current_verifying=0,
            max_impl_fraction=0.1,
            total_slots=30,
        )
        assert cap == 3  # hard_cap 3, even with huge headroom bounded min is 3 due to floor? Actually 100*2=200 headroom => review 153 verify 285 => min hard 3 =>3
        assert cap <= 3

    def test_given_verification_saturated_when_cap_then_verification_bounded(self) -> None:
        cap2 = compute_implementation_cap(
            review_capacity=100,
            verification_capacity=1,
            current_reviewing=200,
            current_verifying=10,
            max_impl_fraction=0.6,
            total_slots=30,
        )
        assert cap2 == 0

    def test_given_first_pass_rate_low_when_cap_then_review_bound_tighter(self) -> None:
        cap_low2 = compute_implementation_cap(
            review_capacity=5,
            verification_capacity=100,
            current_reviewing=0,
            current_verifying=0,
            first_pass_rate=0.2,
            total_slots=30,
        )
        cap_high2 = compute_implementation_cap(
            review_capacity=5,
            verification_capacity=100,
            current_reviewing=0,
            current_verifying=0,
            first_pass_rate=0.9,
            total_slots=30,
        )
        assert cap_low2 < cap_high2

    def test_given_zero_total_slots_when_cap_then_hard_cap_zero(self) -> None:
        cap = compute_implementation_cap(
            review_capacity=5,
            verification_capacity=5,
            current_reviewing=0,
            current_verifying=0,
            total_slots=0,
        )
        assert cap == 0

    def test_given_small_total_slots_when_cap_then_respects_fraction(self) -> None:
        cap = compute_implementation_cap(
            review_capacity=10,
            verification_capacity=10,
            current_reviewing=0,
            current_verifying=0,
            max_impl_fraction=0.5,
            total_slots=10,
        )
        assert cap == 5

    def test_given_floor_condition_met_when_cap_then_returns_three(self) -> None:
        cap = compute_implementation_cap(
            review_capacity=1,
            verification_capacity=1,
            current_reviewing=2,
            current_verifying=2,
            total_slots=30,
        )
        # review_headroom 0, verify 0 => bounded0, current_reviewing 2 < 3 => floor 3
        assert cap == 3

    def test_given_floor_not_met_when_cap_then_zero(self) -> None:
        cap = compute_implementation_cap(
            review_capacity=1,
            verification_capacity=1,
            current_reviewing=5,
            current_verifying=0,
            total_slots=30,
        )
        # bounded 0, current_reviewing 5 >=3 => stays 0
        assert cap == 0
