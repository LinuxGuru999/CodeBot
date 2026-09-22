"""Extensive coverage for scheduler, config, and metrics.

Covers codebot/scheduler_config.py, codebot/scheduler_metrics.py,
and codebot/adaptive_scheduler.py (scheduler) plus queue_pressure decisions.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codebot.adaptive_scheduler import (
    AdaptiveScheduler,
    HysteresisState,
    SchedulerDecision,
    SlotAssignment,
)
from codebot.discovery_manager import DiscoveryManager
from codebot.pipeline_state import PipelineState, WorkerSlot
from codebot.queue_pressure import (
    QueuePressure,
    SchedulerMode,
    calculate_pressure,
    compute_implementation_cap,
    determine_mode,
    estimate_downstream_demand,
)
from codebot.scheduler_config import (
    BacklogConfig,
    CostConfig,
    DiscoveryConfig,
    HysteresisConfig,
    ImplementationConfig,
    IntegrationConfig,
    LifecycleEfficiencyConfig,
    PriorityAgingConfig,
    ReviewConfig,
    SchedulerConfig,
    VerificationConfig,
    WorkerConfig,
    WorkforceConfig,
    _parse_scalar,
    _parse_simple_yaml,
    load_scheduler_config,
)
from codebot.scheduler_metrics import CompletionRecord, MetricsAccumulator, SchedulerMetrics

FIXED_NOW: float = 1_700_000_000.0


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
    created_at: float = field(default_factory=lambda: FIXED_NOW - 3600)
    estimated_cost_tokens: int = 10000
    rework_count: int = 0

    @property
    def evidence_hash(self) -> str:
        return self.id


def make_worker(
    worker_id: str,
    ticket_id: str,
    role: str,
    started: float | None = None,
) -> WorkerSlot:
    now = started or FIXED_NOW
    return WorkerSlot(
        worker_id=worker_id,
        ticket_id=ticket_id,
        role=role,
        started_at=now,
        heartbeat_at=now,
        lease_expires=now + 600,
    )


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
    budget_warning: bool = False,
    hourly_spend: float = 0.0,
    daily_spend: float = 0.0,
    unsatisfied_deps: dict[str, tuple[str, ...]] | None = None,
    conflict_groups: list[frozenset[str]] | None = None,
) -> PipelineState:
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
        budget_warning=budget_warning,
        hourly_spend_usd=hourly_spend,
        daily_spend_usd=daily_spend,
        unsatisfied_dependencies=unsatisfied_deps or {},
        conflict_groups=tuple(conflict_groups or []),
        snapshot_time=FIXED_NOW,
    )


@pytest.fixture
def fixed_now() -> float:
    return FIXED_NOW


@pytest.fixture
def default_config() -> SchedulerConfig:
    return SchedulerConfig(max_slots=30, scheduler_interval_seconds=30).validate()


@pytest.fixture
def empty_pipeline() -> PipelineState:
    return make_pipeline()


@pytest.fixture
def accumulator() -> MetricsAccumulator:
    return MetricsAccumulator()


# ---------------------------------------------------------------------------
# Config validation
# ---------------------------------------------------------------------------


class TestBacklogConfigValidation:
    def test_GivenValidWatermarks_WhenValidate_ThenNoError(self) -> None:
        cfg = BacklogConfig(low_watermark=10, target=50, high_watermark=100)
        cfg.validate()

    def test_GivenLowAboveTarget_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            BacklogConfig(low_watermark=60, target=50, high_watermark=100).validate()

    def test_GivenZeroLow_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            BacklogConfig(low_watermark=0, target=50, high_watermark=100).validate()


class TestDiscoveryConfigValidation:
    def test_GivenFloorAboveMax_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            DiscoveryConfig(maximum_fraction=0.3, floor_fraction=0.5).validate()

    def test_GivenNegativeMinimum_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            DiscoveryConfig(minimum_slots=-1).validate()

    def test_GivenValidFractions_WhenValidate_ThenNoError(self) -> None:
        DiscoveryConfig(minimum_slots=1, maximum_fraction=0.5, floor_fraction=0.1).validate()


class TestWorkforceConfigValidation:
    def test_GivenNegativeFloor_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            WorkforceConfig(review_floor=-1).validate()

    def test_GivenImplFractionOutOfRange_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            WorkforceConfig(implementation_maximum_fraction=1.5).validate()

    def test_GivenZeroDrainSeconds_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            WorkforceConfig(target_drain_seconds=0).validate()

    def test_GivenValidWorkforce_WhenValidate_ThenNoError(self) -> None:
        WorkforceConfig().validate()


class TestLifecycleEfficiencyValidation:
    def test_GivenReworkThresholdOutOfRange_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            LifecycleEfficiencyConfig(pilot_rework_rate_threshold=1.5).validate()

    def test_GivenAuditThresholdNegative_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            LifecycleEfficiencyConfig(pilot_audit_reopen_threshold=-0.1).validate()

    def test_GivenValidLifecycle_WhenValidate_ThenNoError(self) -> None:
        LifecycleEfficiencyConfig().validate()


class TestRemainingConfigsValidation:
    def test_GivenZeroHeartbeat_WhenValidateWorker_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            WorkerConfig(heartbeat_timeout_seconds=0).validate()

    def test_GivenZeroHourlyLimit_WhenValidateCost_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            CostConfig(hourly_limit_usd=0).validate()

    def test_GivenZeroShiftThreshold_WhenValidateHysteresis_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            HysteresisConfig(shift_threshold=0).validate()

    def test_GivenNegativeAgingRate_WhenValidatePriority_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            PriorityAgingConfig(aging_rate=-0.1).validate()

    def test_GivenNegativeReviewMinimum_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            ReviewConfig(minimum_when_pending=-1).validate()

    def test_GivenNegativeVerificationMinimum_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            VerificationConfig(minimum_when_pending=-1).validate()

    def test_GivenZeroImplFraction_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            ImplementationConfig(maximum_fraction=0).validate()


class TestSchedulerConfigCore:
    def test_GivenDefault_WhenValidate_ThenBacklogDefaults(self, default_config: SchedulerConfig) -> None:
        assert default_config.backlog.low_watermark == 20
        assert default_config.backlog.target == 50
        assert default_config.backlog.high_watermark == 100

    def test_GivenZeroSlots_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            SchedulerConfig(max_slots=0).validate()

    def test_GivenZeroInterval_WhenValidate_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            SchedulerConfig(scheduler_interval_seconds=0).validate()

    def test_GivenFrozenConfig_WhenMutate_ThenRaises(self, default_config: SchedulerConfig) -> None:
        with pytest.raises((AttributeError, TypeError)):
            setattr(default_config, "max_slots", 99)

    def test_GivenDict_WhenFromDictRoundtrip_ThenEqual(self, default_config: SchedulerConfig) -> None:
        d = default_config.to_dict()
        restored = SchedulerConfig.from_dict(d)
        assert restored.max_slots == default_config.max_slots
        assert restored.backlog.target == default_config.backlog.target
        assert restored.cost.hourly_limit_usd == default_config.cost.hourly_limit_usd

    def test_GivenJson_WhenFromJsonRoundtrip_ThenEqual(self, default_config: SchedulerConfig) -> None:
        raw = default_config.to_json()
        restored = SchedulerConfig.from_json(raw)
        assert restored.scheduler_interval_seconds == default_config.scheduler_interval_seconds

    def test_GivenPartialDict_WhenFromDict_ThenDefaultsFilled(self) -> None:
        cfg = SchedulerConfig.from_dict({"max_slots": 12})
        assert cfg.max_slots == 12
        assert cfg.backlog.low_watermark == 20

    def test_GivenInvalidBacklogInDict_WhenFromDict_ThenRaises(self) -> None:
        with pytest.raises(ValueError):
            SchedulerConfig.from_dict({"backlog": {"low_watermark": 100, "target": 50, "high_watermark": 80}})

    def test_GivenNestedDict_WhenFromDict_ThenNestedApplied(self) -> None:
        cfg = SchedulerConfig.from_dict({"backlog": {"low_watermark": 5, "target": 10, "high_watermark": 20}})
        assert cfg.backlog.low_watermark == 5
        assert cfg.backlog.high_watermark == 20

    def test_GivenConfig_WhenToDict_ThenContainsAllSections(self, default_config: SchedulerConfig) -> None:
        d = default_config.to_dict()
        assert "workforce" in d
        assert "lifecycle_efficiency" in d
        assert "hysteresis" in d
        assert "integration" in d
        assert d["workforce"]["review_floor"] == 2
        assert d["integration"]["rebase_required"] is True


# ---------------------------------------------------------------------------
# YAML / loading
# ---------------------------------------------------------------------------


class TestParseScalar:
    def test_GivenTrueString_WhenParse_ThenTrue(self) -> None:
        assert _parse_scalar("true") is True
        assert _parse_scalar("yes") is True

    def test_GivenFalseString_WhenParse_ThenFalse(self) -> None:
        assert _parse_scalar("false") is False
        assert _parse_scalar("no") is False

    def test_GivenNullString_WhenParse_ThenNone(self) -> None:
        assert _parse_scalar("null") is None
        assert _parse_scalar("~") is None

    def test_GivenIntString_WhenParse_ThenInt(self) -> None:
        result = _parse_scalar("42")
        assert result == 42
        assert isinstance(result, int)

    def test_GivenFloatString_WhenParse_ThenFloat(self) -> None:
        result = _parse_scalar("3.14")
        assert result == pytest.approx(3.14)

    def test_GivenQuotedString_WhenParse_ThenStripped(self) -> None:
        assert _parse_scalar('"hello"') == "hello"
        assert _parse_scalar("'hello'") == "hello"

    def test_GivenPlainString_WhenParse_ThenString(self) -> None:
        assert _parse_scalar("hello") == "hello"


class TestParseSimpleYaml:
    def test_GivenBasicYaml_WhenParse_ThenScalars(self) -> None:
        d = _parse_simple_yaml("key: value\nnumber: 42\n")
        assert d["key"] == "value"
        assert d["number"] == 42

    def test_GivenNestedYaml_WhenParse_ThenNestedDict(self) -> None:
        d = _parse_simple_yaml("outer:\n    inner: 123\n    name: hello\n")
        assert d["outer"]["inner"] == 123
        assert d["outer"]["name"] == "hello"

    def test_GivenComments_WhenParse_ThenIgnored(self) -> None:
        d = _parse_simple_yaml("# comment\nkey: value\n# another\n")
        assert d["key"] == "value"
        assert len(d) == 1

    def test_GivenEmptyLines_WhenParse_ThenIgnored(self) -> None:
        d = _parse_simple_yaml("a: 1\n\nb: 2\n")
        assert d["a"] == 1
        assert d["b"] == 2

    def test_GivenSchedulerWrapper_WhenParse_ThenWrapperPresent(self) -> None:
        d = _parse_simple_yaml("scheduler:\n    max_slots: 42\n")
        assert "scheduler" in d
        assert d["scheduler"]["max_slots"] == 42


class TestLoadSchedulerConfig:
    def test_GivenNoFile_WhenLoad_ThenDefault(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        cfg = load_scheduler_config()
        assert cfg.scheduler_interval_seconds == 30

    def test_GivenJsonFile_WhenLoad_ThenValuesUsed(self, tmp_path: Path) -> None:
        p = tmp_path / "sched.json"
        p.write_text(json.dumps({"max_slots": 18, "scheduler_interval_seconds": 60}), encoding="utf-8")
        cfg = load_scheduler_config(p)
        assert cfg.max_slots == 18
        assert cfg.scheduler_interval_seconds == 60

    def test_GivenYamlFile_WhenLoad_ThenValuesUsed(self, tmp_path: Path) -> None:
        p = tmp_path / "sched.yaml"
        p.write_text("max_slots: 22\nscheduler_interval_seconds: 45\n", encoding="utf-8")
        cfg = load_scheduler_config(p)
        assert cfg.max_slots == 22

    def test_GivenYamlWithSchedulerKey_WhenLoad_ThenUnwrapped(self, tmp_path: Path) -> None:
        p = tmp_path / "sched.yaml"
        p.write_text("scheduler:\n    max_slots: 31\n", encoding="utf-8")
        cfg = load_scheduler_config(p)
        assert cfg.max_slots == 31

    def test_GivenMissingPath_WhenLoad_ThenDefault(self, tmp_path: Path) -> None:
        cfg = load_scheduler_config(tmp_path / "missing.yaml")
        assert cfg.scheduler_interval_seconds == 30

    def test_GivenStandardLocation_WhenLoadWithoutPath_ThenFound(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.chdir(tmp_path)
        codebot_dir = tmp_path / ".codebot"
        codebot_dir.mkdir()
        (codebot_dir / "scheduler.yaml").write_text("max_slots: 27\n", encoding="utf-8")
        cfg = load_scheduler_config()
        assert cfg.max_slots == 27


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------


class TestMetricsAccumulatorBasics:
    def test_GivenEmptyAccumulator_WhenBuild_ThenZeroThroughput(self, accumulator: MetricsAccumulator, fixed_now: float) -> None:
        ps = make_pipeline(total_slots=30)
        m = accumulator.build(ps, now=fixed_now)
        assert m.tickets_completed_per_hour == 0.0
        assert m.mean_ticket_cycle_time_seconds == 0.0
        assert m.first_pass_review_rate == 0.0

    def test_GivenOneCompletion_WhenRecord_ThenCounted(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now, fixed_now - 100, was_productive=True, cost_tokens=100)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.tickets_completed_per_hour == 1.0
        assert m.tickets_completed_per_day == 1.0

    def test_GivenTwoCompletionsOneOld_WhenBuild_ThenOnlyRecentCountedHour(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now - 100, fixed_now - 200, was_productive=True)
        acc.record_completion("TOLD", fixed_now - 7200, fixed_now - 7300, was_productive=True)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.tickets_completed_per_hour == 1.0
        assert m.tickets_completed_per_day == 2.0

    def test_GivenReviews_WhenBuild_ThenFirstPassRate(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_review(passed_first=True)
        acc.record_review(passed_first=False)
        acc.record_review(passed_first=True)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.first_pass_review_rate == pytest.approx(2 / 3)

    def test_GivenDiscoveryScans_WhenBuild_ThenYield(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_discovery(validated=3, duplicates=1)
        acc.record_discovery(validated=1, duplicates=1)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        # validated 4, duplicates 2 => yield 4/6
        assert m.discovery_yield_rate == pytest.approx(4 / 6)

    def test_GivenOnlyDuplicates_WhenBuild_ThenYieldZero(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_discovery(validated=0, duplicates=5)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.discovery_yield_rate == 0.0

    def test_GivenHumanInterventions_WhenBuild_ThenRate(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now, fixed_now - 10, was_productive=True)
        acc.record_completion("T2", fixed_now, fixed_now - 10, was_productive=True)
        acc.record_human_intervention()
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.human_intervention_rate == pytest.approx(0.5)

    def test_GivenReworkFlag_WhenRecord_ThenReworkRate(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now, fixed_now - 10, was_rework=True)
        acc.record_completion("T2", fixed_now, fixed_now - 10, was_rework=False)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.rework_rate == pytest.approx(0.5)

    def test_GivenHourlyCost_WhenBuild_ThenPerTicket(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now, fixed_now - 10, was_productive=True)
        acc.record_completion("T2", fixed_now, fixed_now - 10, was_productive=True)
        acc.set_hourly_cost(10.0)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.cost_per_ticket_usd == pytest.approx(5.0)
        assert m.cost_per_hour_usd == pytest.approx(10.0)

    def test_GivenNoCompletions_WhenBuild_ThenCostZero(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.set_hourly_cost(10.0)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.cost_per_ticket_usd == 0.0

    def test_GivenCycleTimes_WhenBuild_ThenMean(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now, fixed_now - 100, was_productive=True)
        acc.record_completion("T2", fixed_now, fixed_now - 300, was_productive=True)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.mean_ticket_cycle_time_seconds == pytest.approx(200.0)

    def test_GivenPruneThreshold_WhenExceedMaxHistory_ThenTrimmed(self, fixed_now: float) -> None:
        acc = MetricsAccumulator(max_history=3)
        for i in range(5):
            acc.record_completion(f"T{i}", fixed_now, fixed_now - 10, was_productive=True)
        assert len(acc.completions) == 3
        assert acc.completions[0].ticket_id == "T2"

    def test_GivenPipelineWithSlots_WhenBuild_ThenUtilization(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()

        @dataclass
        class StubPS:
            active_count: int = 2
            total_slots: int = 10
            ready_count: int = 0
            implementing_count: int = 0
            reviewing_count: int = 0
            verifying_count: int = 0
            rework_count: int = 0
            blocked_count: int = 0
            candidate_count: int = 0
            hourly_spend_usd: float = 0.0
            daily_spend_usd: float = 0.0
            budget_exhausted: bool = False
            budget_warning: bool = False

            def active_by_role(self) -> dict[str, int]:
                return {"general_implementer": 2}

        ps = StubPS()
        m = acc.build(ps, now=fixed_now)
        assert m.slot_utilization == pytest.approx(0.2)
        assert m.slots_active == 2
        assert m.slots_idle == 8
        assert m.slots_total == 10

    def test_GivenProductiveCompletions_WhenBuild_ThenProductiveHigh(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now, fixed_now - 10, was_productive=True)
        acc.record_completion("T2", fixed_now, fixed_now - 10, was_productive=True)
        ps = make_pipeline(active_workers=[make_worker("w1", "T1", "general_implementer")], total_slots=30)
        m = acc.build(ps, now=fixed_now)
        assert m.productive_slot_utilization == pytest.approx(1.0)

    def test_GivenNonProductiveCompletions_WhenBuild_ThenProductiveLower(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now, fixed_now - 10, was_productive=False)
        ps = make_pipeline()
        m = acc.build(ps, now=fixed_now)
        assert m.productive_slot_utilization == pytest.approx(0.0)

    def test_GivenNoCompletions_WhenBuild_ThenProductiveEstimated(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()

        @dataclass
        class StubPS2:
            active_count: int = 3
            total_slots: int = 30
            ready_count: int = 0
            implementing_count: int = 0
            reviewing_count: int = 0
            verifying_count: int = 0
            rework_count: int = 0
            blocked_count: int = 0
            candidate_count: int = 0
            hourly_spend_usd: float = 0.0
            daily_spend_usd: float = 0.0
            budget_exhausted: bool = False
            budget_warning: bool = False

            def active_by_role(self) -> dict[str, int]:
                return {"general_implementer": 3}

        ps = StubPS2()
        m = acc.build(ps, now=fixed_now)
        assert m.productive_slot_utilization == pytest.approx(0.09)

    def test_GivenReviewWorkers_WhenBuild_ThenQueueAge(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        start = fixed_now - 500
        w = make_worker("w1", "T1", "correctness_reviewer", started=start)
        ps = make_pipeline(reviewing=1, active_workers=[w])
        m = acc.build(ps, now=fixed_now)
        assert m.review_queue_age_seconds == pytest.approx(500.0)

    def test_GivenNoReviewWorkers_WhenBuild_ThenAgeZero(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        ps = make_pipeline(reviewing=2, active_workers=[make_worker("w1", "T1", "general_implementer")])
        m = acc.build(ps, now=fixed_now)
        assert m.review_queue_age_seconds == 0.0


class TestMetricsBudget:
    def test_GivenExhaustedBudget_WhenBuild_ThenStop(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        ps = make_pipeline(budget_exhausted=True, daily_spend=90.0)
        m = acc.build(ps, cost_config=CostConfig(hourly_limit_usd=10.0, daily_limit_usd=100.0), now=fixed_now)
        assert m.budget_state == "stop"
        assert m.budget_exhausted is True

    def test_GivenHighDailyBurn_WhenBuild_ThenShedTier3(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        ps = make_pipeline(daily_spend=95.0, budget_warning=False)
        m = acc.build(ps, cost_config=CostConfig(daily_limit_usd=100.0, hourly_limit_usd=10.0), now=fixed_now)
        assert m.budget_state == "shed_tier3"
        assert m.daily_burn_rate == pytest.approx(0.95)

    def test_GivenModerateBurn_WhenBuild_ThenWarn(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        ps = make_pipeline(daily_spend=85.0, budget_warning=False)
        m = acc.build(ps, cost_config=CostConfig(daily_limit_usd=100.0, hourly_limit_usd=10.0), now=fixed_now)
        assert m.budget_state == "warn"

    def test_GivenLowSpend_WhenBuild_ThenOk(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        ps = make_pipeline(hourly_spend=2.0, daily_spend=10.0)
        m = acc.build(ps, cost_config=CostConfig(hourly_limit_usd=10.0, daily_limit_usd=100.0), now=fixed_now)
        assert m.budget_state == "ok"
        assert m.hourly_burn_rate == pytest.approx(0.2)
        assert m.daily_burn_rate == pytest.approx(0.1)

    def test_GivenSchedulerConfig_WhenBuild_ThenLimitsFromNested(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        ps = make_pipeline(hourly_spend=5.0, daily_spend=50.0)
        cfg = SchedulerConfig(cost=CostConfig(hourly_limit_usd=10.0, daily_limit_usd=100.0))
        m = acc.build(ps, cost_config=cfg, now=fixed_now)
        assert m.hourly_limit_usd == 10.0
        assert m.daily_limit_usd == 100.0
        assert m.remaining_hourly_usd == pytest.approx(5.0)
        assert m.remaining_daily_usd == pytest.approx(50.0)

    def test_GivenOverspend_WhenBuild_ThenRemainingClamped(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        ps = make_pipeline(hourly_spend=20.0, daily_spend=200.0)
        m = acc.build(ps, cost_config=CostConfig(hourly_limit_usd=10.0, daily_limit_usd=100.0), now=fixed_now)
        assert m.remaining_hourly_usd == 0.0
        assert m.remaining_daily_usd == 0.0


class TestMetricsPersistence:
    def test_GivenAccumulator_WhenSaveAndLoad_ThenEqual(self, tmp_path: Path, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now, fixed_now - 100, was_rework=False, was_productive=True, cost_tokens=500)
        acc.record_review(passed_first=True)
        acc.record_discovery(validated=2, duplicates=1)
        acc.record_human_intervention()
        acc.set_hourly_cost(7.5)
        p = tmp_path / "metrics.json"
        acc.save(p)
        loaded = MetricsAccumulator.load(p)
        assert loaded.total_reviews == 1
        assert loaded.first_pass_reviews == 1
        assert loaded.validated_discoveries == 2
        assert loaded.duplicate_discoveries == 1
        assert loaded.human_interventions == 1
        assert loaded.hourly_cost_usd == pytest.approx(7.5)
        assert loaded.completions[0].ticket_id == "T1"
        assert loaded.completions[0].cost_tokens == 500

    def test_GivenMissingFile_WhenLoad_ThenEmpty(self, tmp_path: Path) -> None:
        acc = MetricsAccumulator.load(tmp_path / "missing.json")
        assert acc.completions == []
        assert acc.total_reviews == 0

    def test_GivenCorruptFile_WhenLoad_ThenEmpty(self, tmp_path: Path) -> None:
        p = tmp_path / "bad.json"
        p.write_text("not json", encoding="utf-8")
        acc = MetricsAccumulator.load(p)
        assert acc.completions == []

    def test_GivenMetrics_WhenSummary_ThenKeysPresent(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        acc.record_completion("T1", fixed_now, fixed_now - 10, was_productive=True)
        ps = make_pipeline(ready=5, implementing=2, reviewing=1)
        m = acc.build(ps, scheduler_mode="BALANCED", now=fixed_now)
        s = m.summary()
        assert "slots" in s
        assert "queues" in s
        assert "throughput" in s
        assert "quality" in s
        assert "economics" in s
        assert s["mode"] == "BALANCED"
        assert s["slots"]["total"] == 30

    def test_GivenMetrics_WhenSlotsGrouped_ThenByRole(self, fixed_now: float) -> None:
        acc = MetricsAccumulator()
        w1 = make_worker("w1", "T1", "general_implementer")
        w2 = make_worker("w2", "T2", "correctness_reviewer")
        ps = make_pipeline(active_workers=[w1, w2])
        m = acc.build(ps, now=fixed_now)
        assert m.slots_by_role["general_implementer"] == 1
        assert m.slots_by_role["correctness_reviewer"] == 1


# ---------------------------------------------------------------------------
# Pressure calculations
# ---------------------------------------------------------------------------


class TestPressureCalculations:
    def test_GivenEmptyPipeline_WhenCalculate_ThenDiscoveryHigh(self) -> None:
        p = calculate_pressure(ready_count=0, implementing_count=0, reviewing_count=0, verifying_count=0, rework_count=0, planning_count=0, candidate_count=0, integration_queue_count=0, active_by_role={}, total_slots=30)
        assert p.discovery_pressure >= 1.0
        assert p.implementation_pressure == 0.0

    def test_GivenReviewCongestion_WhenCalculate_ThenReviewDominates(self) -> None:
        p = calculate_pressure(ready_count=5, implementing_count=12, reviewing_count=25, verifying_count=2, rework_count=0, planning_count=0, candidate_count=0, integration_queue_count=0, active_by_role={"general_implementer": 12, "correctness_reviewer": 3}, total_slots=30)
        assert p.review_pressure > p.implementation_pressure
        assert p.bottleneck_stage == "review"

    def test_GivenHighReady_WhenCalculate_ThenBacklogRatioHigh(self) -> None:
        p = calculate_pressure(ready_count=100, implementing_count=0, reviewing_count=0, verifying_count=0, rework_count=0, planning_count=0, candidate_count=0, integration_queue_count=0, active_by_role={}, total_slots=30, backlog_low_watermark=20, backlog_target=50, backlog_high_watermark=100)
        assert p.backlog_ratio == 1.0
        assert p.discovery_pressure == 0.0

    def test_GivenNoWork_WhenCalculate_ThenBottleneckNone(self) -> None:
        p = QueuePressure()
        assert p.bottleneck_stage == "none"
        assert p.max_pressure == 0.0

    def test_GivenPressure_WhenToDict_ThenRounded(self) -> None:
        p = QueuePressure(implementation_pressure=0.123456, review_pressure=1.5)
        d = p.to_dict()
        assert d["implementation"] == pytest.approx(0.123)
        assert d["review"] == pytest.approx(1.5)
        assert d["bottleneck"] == "review"

    def test_GivenIntegrationQueue_WhenCalculate_ThenIntegrationPressure(self) -> None:
        p = calculate_pressure(integration_queue_count=10, total_slots=30, active_by_role={})
        assert p.integration_pressure > 0.0

    def test_GivenDiscoverySuppressedWhenWorkExists_WhenCalculate_ThenZeroDiscovery(self) -> None:
        p = calculate_pressure(ready_count=10, implementing_count=0, reviewing_count=0, verifying_count=0, rework_count=0, planning_count=0, candidate_count=0, integration_queue_count=0, active_by_role={}, total_slots=30)
        assert p.discovery_pressure == 0.0


class TestDetermineMode:
    def test_GivenBudgetExhausted_WhenDetermine_ThenThrottled(self) -> None:
        p = QueuePressure()
        ps = make_pipeline(budget_exhausted=True)
        assert determine_mode(p, ps, budget_exhausted=True) == SchedulerMode.BUDGET_THROTTLED

    def test_GivenManyStuck_WhenDetermine_ThenRecovery(self) -> None:
        p = QueuePressure()
        ps = make_pipeline(stuck=[f"w{i}" for i in range(11)], total_slots=30)
        assert determine_mode(p, ps) == SchedulerMode.RECOVERY

    def test_GivenEmptyAndDiscoveryPressure_WhenDetermine_ThenDiscoveryHeavy(self) -> None:
        p = QueuePressure(discovery_pressure=1.5)
        ps = make_pipeline(ready=0, implementing=0, reviewing=0, verifying=0, rework=0, planning=0, discovered=0, candidates=0)
        # need total_work ==0 and active 0
        assert determine_mode(p, ps) == SchedulerMode.DISCOVERY_HEAVY

    def test_GivenEmptyNoDiscovery_WhenDetermine_ThenIdle(self) -> None:
        p = QueuePressure(discovery_pressure=0.0)
        ps = make_pipeline()
        assert determine_mode(p, ps) == SchedulerMode.IDLE

    def test_GivenReviewHeavy_WhenDetermine_ThenReview(self) -> None:
        p = QueuePressure(review_pressure=2.0, implementation_pressure=0.5, verification_pressure=0.1)
        ps = make_pipeline(ready=5, reviewing=10)
        assert determine_mode(p, ps) == SchedulerMode.REVIEW_HEAVY

    def test_GivenBalanced_WhenDetermine_ThenBalanced(self) -> None:
        p = QueuePressure(implementation_pressure=0.5, review_pressure=0.3)
        ps = make_pipeline(ready=5, implementing=3)
        assert determine_mode(p, ps) == SchedulerMode.BALANCED

    def test_GivenZeroWorkWithActiveWorkers_WhenDetermine_ThenSaturated(self) -> None:
        p = QueuePressure()
        ps = make_pipeline(active_workers=[make_worker("w1", "T1", "general_implementer")])
        assert determine_mode(p, ps) == SchedulerMode.PROJECT_SATURATED


class TestImplementationCapAndDemand:
    def test_GivenReviewBounded_WhenComputeCap_ThenLimited(self) -> None:
        cap = compute_implementation_cap(review_capacity=2, verification_capacity=2, current_reviewing=10, current_verifying=1, max_impl_fraction=0.6, total_slots=30)
        assert cap <= 18
        assert cap < 10

    def test_GivenZeroReviewCapacity_WhenComputeCap_ThenAtLeastOne(self) -> None:
        cap = compute_implementation_cap(review_capacity=0, verification_capacity=0, current_reviewing=0, current_verifying=0, total_slots=30)
        assert cap >= 1

    def test_GivenEstimateDemand_WhenCalled_ThenPositive(self) -> None:
        d = estimate_downstream_demand(implementing_count=10, reviewing_count=5)
        assert d["expected_review_demand"] > 0
        assert d["expected_reworks"] > 0


# ---------------------------------------------------------------------------
# Scheduler decision logic
# ---------------------------------------------------------------------------


class TestHysteresis:
    def test_GivenSameMode_WhenShouldShift_ThenFalse(self, fixed_now: float) -> None:
        hs = HysteresisState(last_mode=SchedulerMode.BALANCED, last_change_time=fixed_now)
        assert hs.should_shift(SchedulerMode.BALANCED, fixed_now + 1000, 300) is False

    def test_GivenDifferentModeAndElapsed_WhenShouldShift_ThenTrue(self, fixed_now: float) -> None:
        hs = HysteresisState(last_mode=SchedulerMode.BALANCED, last_change_time=fixed_now - 400)
        assert hs.should_shift(SchedulerMode.REVIEW_HEAVY, fixed_now, 300) is True

    def test_GivenDifferentModeButNotElapsed_WhenShouldShift_ThenFalse(self, fixed_now: float) -> None:
        hs = HysteresisState(last_mode=SchedulerMode.BALANCED, last_change_time=fixed_now)
        assert hs.should_shift(SchedulerMode.REVIEW_HEAVY, fixed_now + 100, 300) is False

    def test_GivenRecord_WhenCalled_ThenUpdates(self, fixed_now: float) -> None:
        hs = HysteresisState(last_mode=SchedulerMode.BALANCED, last_change_time=fixed_now - 500)
        hs.record(SchedulerMode.REVIEW_HEAVY, {"review": 2}, fixed_now)
        assert hs.last_mode == SchedulerMode.REVIEW_HEAVY
        assert hs.last_allocation["review"] == 2

    def test_GivenPressureSamples_WhenAdded_ThenBounded(self) -> None:
        hs = HysteresisState()
        for _ in range(15):
            hs.add_pressure_sample(QueuePressure(implementation_pressure=0.5))
        assert len(hs.rolling_pressures) <= 10


class TestSlotAssignmentAndDecision:
    def test_GivenDecision_WhenSummary_ThenByRole(self, fixed_now: float) -> None:
        decision = SchedulerDecision(assignments=(SlotAssignment(role="general_implementer", ticket_id="T1", model_profile="standard", priority=4, reason="test"), SlotAssignment(role="correctness_reviewer", ticket_id="T2", model_profile="premium", priority=3, reason="test")), mode=SchedulerMode.BALANCED, pressure=QueuePressure(), free_slots_remaining=5, total_active_after=2, reasons=("a",), timestamp=fixed_now)
        by_role: dict[str, int] = {}
        for a in decision.assignments:
            by_role[a.role] = by_role.get(a.role, 0) + 1
        assert len(decision.assignments) == 2
        assert by_role["general_implementer"] == 1
        assert decision.mode.value == "BALANCED"
        assert decision.free_slots_remaining == 5


class TestAdaptiveSchedulerTick:
    def _tick(self, scheduler: AdaptiveScheduler, pipeline: PipelineState, tickets: list[FakeTicket], now: float | None = None) -> SchedulerDecision:
        ready = [t for t in tickets if t.state == "READY"]
        review = [t for t in tickets if t.state == "REVIEWING"]
        verify = [t for t in tickets if t.state == "VERIFYING"]
        rework = [t for t in tickets if t.state == "REWORK"]
        planning = [t for t in tickets if t.state == "PLANNING"]
        return scheduler.tick(pipeline=pipeline, ready_tickets=ready, review_tickets=review, verify_tickets=verify, rework_tickets=rework, planning_tickets=planning, candidate_tickets=[], now=now or FIXED_NOW)

    def test_GivenEmptyProject_WhenTick_ThenDiscovery(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        scheduler.config = SchedulerConfig(max_slots=30, hysteresis=HysteresisConfig(min_allocation_duration_seconds=0)).validate()
        decision = self._tick(scheduler, make_pipeline(ready=0), [])
        from codebot.discovery_manager import DISCOVERY_ROLES
        disc = [a for a in decision.assignments if a.role in DISCOVERY_ROLES]
        assert len(disc) > 0

    def test_GivenHealthyBacklog_WhenTick_ThenImplementation(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        tickets = [FakeTicket(id=f"T{i}", state="READY", severity="medium") for i in range(20)]
        decision = self._tick(scheduler, make_pipeline(ready=20), tickets)
        impl = [a for a in decision.assignments if "implementer" in a.role]
        assert len(impl) > 0

    def test_GivenBudgetExhausted_WhenTick_ThenThrottledAndEmpty(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        decision = self._tick(scheduler, make_pipeline(ready=5, budget_exhausted=True), [FakeTicket(id="T1", state="READY")])
        assert decision.mode == SchedulerMode.BUDGET_THROTTLED
        assert len(decision.assignments) == 0
        assert decision.free_slots_remaining == 30

    def test_GivenReworkTicket_WhenTick_ThenReworkAllocated(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        rework = [FakeTicket(id="RW1", state="REWORK", severity="high")]
        ready = [FakeTicket(id=f"T{i}", state="READY") for i in range(3)]
        decision = self._tick(scheduler, make_pipeline(ready=3, rework=1), rework + ready)
        assert any(a.ticket_id == "RW1" for a in decision.assignments)

    def test_GivenBlockedTicket_WhenTick_ThenNotAssigned(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        t = FakeTicket(id="T2", state="READY")
        pipeline = make_pipeline(ready=1, unsatisfied_deps={"T2": ("T1",)})
        decision = self._tick(scheduler, pipeline, [t])
        assert "T2" not in {a.ticket_id for a in decision.assignments}

    def test_GivenActiveConflict_WhenTick_ThenNotAssigned(self) -> None:
        from codebot.conflict_detector import ConflictMatrix, ConflictEdge
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        t1 = FakeTicket(id="T1", state="READY", affected_modules=["auth"])
        t2 = FakeTicket(id="T2", state="READY", affected_modules=["auth"])
        matrix = ConflictMatrix(edges=(ConflictEdge(ticket_a="T1", ticket_b="T2", reason="shared_module"),))
        pipeline = make_pipeline(ready=2, active_workers=[make_worker("w1", "T1", "general_implementer")])
        ready = [t1, t2]
        # tick with conflict matrix should suppress T1/T2 because T1 conflicts
        decision = scheduler.tick(pipeline=pipeline, ready_tickets=ready, review_tickets=[], verify_tickets=[], rework_tickets=[], planning_tickets=[], candidate_tickets=[], conflict_matrix=matrix, now=FIXED_NOW)
        assert "T1" not in {a.ticket_id for a in decision.assignments}

    def test_GivenSecurityCritical_WhenTick_ThenEmergencyAllocated(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        sec = FakeTicket(id="SEC-1", ticket_class="security", severity="critical", state="READY")
        normal = [FakeTicket(id=f"T{i}", state="READY") for i in range(2)]
        decision = self._tick(scheduler, make_pipeline(ready=3), [sec] + normal)
        assert any(a.ticket_id == "SEC-1" for a in decision.assignments)
        assert any("SECURITY EMERGENCY" in r for r in decision.reasons)

    def test_GivenMaxSlots_WhenTick_ThenNotExceeded(self) -> None:
        cfg = SchedulerConfig(max_slots=5).validate()
        scheduler = AdaptiveScheduler(config=cfg)
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        tickets = [FakeTicket(id=f"T{i}", state="READY") for i in range(20)]
        decision = self._tick(scheduler, make_pipeline(ready=20, total_slots=5), tickets)
        assert decision.total_active_after <= 5

    def test_GivenReviewCongestion_WhenTick_ThenReviewAllocated(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        review_tickets = [FakeTicket(id=f"R{i}", state="REVIEWING") for i in range(8)]
        pipeline = make_pipeline(reviewing=8, ready=5, total_slots=30)
        decision = self._tick(scheduler, pipeline, review_tickets + [FakeTicket(id="T1", state="READY")])
        assert any("reviewer" in a.role for a in decision.assignments)

    def test_GivenVerificationTickets_WhenTick_ThenVerificationAllocated(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        verify = [FakeTicket(id="V1", state="VERIFYING")]
        pipeline = make_pipeline(verifying=1)
        decision = self._tick(scheduler, pipeline, verify)
        assert any(a.role == "quality_gate" for a in decision.assignments)

    def test_GivenPlanningTickets_WhenTick_ThenPlanningAllocated(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        planning = [FakeTicket(id="P1", state="PLANNING")]
        pipeline = make_pipeline(planning=1)
        decision = self._tick(scheduler, pipeline, planning)
        assert any(a.role == "planner" for a in decision.assignments)

    def test_GivenEconomicWarning_WhenTick_ThenThrottledReason(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30, cost=CostConfig(hourly_limit_usd=10.0, daily_limit_usd=100.0)).validate())
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        pipeline = make_pipeline(ready=10, hourly_spend=9.0, daily_spend=85.0, budget_warning=True)
        tickets = [FakeTicket(id=f"T{i}", state="READY") for i in range(10)]
        decision = self._tick(scheduler, pipeline, tickets)
        assert any("budget throttled" in r for r in decision.reasons)

    def test_GivenEconomicSevere_WhenTick_ThenNoDiscoverySlots(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30, cost=CostConfig(hourly_limit_usd=10.0, daily_limit_usd=100.0)).validate())
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        pipeline = make_pipeline(ready=5, daily_spend=95.0)
        decision = self._tick(scheduler, pipeline, [FakeTicket(id=f"T{i}", state="READY") for i in range(5)])
        assert any("severe" in r for r in decision.reasons)

    def test_GivenSaturatedDiscovery_WhenTick_ThenProjectSaturatedMode(self) -> None:
        dm = DiscoveryManager()
        now = FIXED_NOW
        for role in ["bug_hunter", "security_auditor", "test_gap_auditor"]:
            for _ in range(6):
                dm.record_completion(role=role, scope="full", commit_sha="abc", findings=0, duplicates=10, rejected=5, cost_tokens=100, now=now)
        cfg = SchedulerConfig(max_slots=30).validate()
        scheduler = AdaptiveScheduler(config=cfg, discovery_manager=dm)
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        # need enough free slots to trigger discovery fill
        pipeline = make_pipeline(ready=0, planning=0, implementing=0, reviewing=0, verifying=0, rework=0)
        decision = self._tick(scheduler, pipeline, [])
        # saturated discovery should set mode to PROJECT_SATURATED
        assert decision.mode == SchedulerMode.PROJECT_SATURATED
        assert any("saturated" in r for r in decision.reasons)

    def test_GivenTick_WhenFormatDashboard_ThenContainsSlots(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        scheduler.hysteresis.last_change_time = FIXED_NOW - 1000
        pipeline = make_pipeline(ready=4, implementing=2, reviewing=1)
        decision = self._tick(scheduler, pipeline, [FakeTicket(id="T1", state="READY")])
        dash = scheduler.format_dashboard(decision, pipeline)
        assert "Slots:" in dash
        assert "CODEBOT CAPACITY" in dash

    def test_GivenMarkCompleted_WhenTickThenDependencyCleared_ThenSchedulable(self) -> None:
        scheduler = AdaptiveScheduler(config=SchedulerConfig(max_slots=30).validate())
        scheduler.mark_completed("T0")
        assert "T0" in scheduler._completed_tickets

    def test_GivenReviewerIndependenceViolation_WhenCheck_ThenDetected(self) -> None:
        scheduler = AdaptiveScheduler()
        worker = make_worker("w1", "T1", "general_implementer")
        pipeline = make_pipeline(active_workers=[worker])
        assert scheduler._is_independence_violation("T1", "correctness_reviewer", pipeline) is True
        assert scheduler._is_independence_violation("T1", "correctness_reviewer", make_pipeline()) is False

    def test_GivenRoleCategory_WhenMapped_ThenCorrect(self) -> None:
        scheduler = AdaptiveScheduler()
        assert scheduler._role_to_category("general_implementer") == "implementation"
        assert scheduler._role_to_category("correctness_reviewer") == "review"
        assert scheduler._role_to_category("quality_gate") == "verification"
        assert scheduler._role_to_category("planner") == "planning"
        # discovery role
        from codebot.discovery_manager import DISCOVERY_ROLES
        disc_role = next(iter(DISCOVERY_ROLES))
        assert scheduler._role_to_category(disc_role) == "discovery"
