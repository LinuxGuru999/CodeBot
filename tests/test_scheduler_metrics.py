"""Tests for scheduler_metrics.py — throughput and utilization telemetry."""

import json
import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest

from codebot.scheduler_metrics import SchedulerMetrics, CompletionRecord, MetricsAccumulator
from codebot.pipeline_state import PipelineState, WorkerSlot


def make_worker(role="general_implementer", ticket_id="T-001", started_at=None):
    if started_at is None:
        started_at = time.time()
    return WorkerSlot(worker_id=f"w-{role}", ticket_id=ticket_id, role=role, started_at=started_at, heartbeat_at=started_at, lease_expires=started_at + 1000)


def make_ps(**kwargs):
    defaults = dict(
        ready_count=0, implementing_count=0, reviewing_count=0, verifying_count=0,
        rework_count=0, blocked_count=0, candidate_count=0, total_slots=30,
        active_workers=(), stuck_workers=(),
    )
    defaults.update(kwargs)
    ps = PipelineState(**defaults)
    # Add missing attrs expected by MetricsAccumulator.build
    if not hasattr(ps, 'active_count'):
        object.__setattr__(ps, 'active_count', len(ps.active_workers))  # type: ignore[attr-defined]
    return ps


class FakePipelineState:
    def __init__(self, **kwargs):
        self.ready_count = kwargs.get("ready_count", 0)
        self.implementing_count = kwargs.get("implementing_count", 0)
        self.reviewing_count = kwargs.get("reviewing_count", 0)
        self.verifying_count = kwargs.get("verifying_count", 0)
        self.rework_count = kwargs.get("rework_count", 0)
        self.blocked_count = kwargs.get("blocked_count", 0)
        self.candidate_count = kwargs.get("candidate_count", 0)
        self.total_slots = kwargs.get("total_slots", 30)
        self.active_count = kwargs.get("active_count", 0)
        self.active_workers = kwargs.get("active_workers", [])
        self.reviewing_count = kwargs.get("reviewing_count", 0)

    def active_by_role(self):
        counts = {}
        for w in self.active_workers:
            counts[w.role] = counts.get(w.role, 0) + 1
        return counts


class TestSchedulerMetricsDataclass:
    def test_defaults(self):
        m = SchedulerMetrics()
        assert m.slots_total == 30
        assert m.slots_active == 0
        assert m.tickets_completed_per_hour == 0.0

    def test_frozen(self):
        m = SchedulerMetrics()
        with pytest.raises((AttributeError, TypeError)):
            m.slots_total = 10  # type: ignore[misc]

    def test_summary_structure(self):
        m = SchedulerMetrics(slots_total=30, slots_active=5, tickets_ready=10)
        s = m.summary()
        assert "slots" in s
        assert "queues" in s
        assert "throughput" in s
        assert "quality" in s
        assert "discovery" in s
        assert "cost" in s
        assert s["slots"]["total"] == 30
        assert s["queues"]["ready"] == 10


class TestCompletionRecord:
    def test_creation(self):
        r = CompletionRecord(ticket_id="T-001", completed_at=1000.0, created_at=900.0)
        assert r.ticket_id == "T-001"
        assert r.was_productive is True
        assert r.was_rework is False

    def test_rework_flag(self):
        r = CompletionRecord(ticket_id="T", completed_at=1000, created_at=900, was_rework=True)
        assert r.was_rework is True

    def test_non_productive(self):
        r = CompletionRecord(ticket_id="T", completed_at=1000, created_at=900, was_productive=False)
        assert r.was_productive is False


class TestMetricsAccumulatorBasic:
    def test_empty_build(self):
        acc = MetricsAccumulator()
        ps = FakePipelineState(active_count=0, total_slots=30)
        m = acc.build(ps, now=1000000.0)
        assert m.tickets_completed_per_hour == 0
        assert m.tickets_completed_per_day == 0
        assert m.mean_ticket_cycle_time_seconds == 0.0

    def test_record_completion(self):
        acc = MetricsAccumulator()
        now = time.time()
        acc.record_completion("T-001", now, now - 3600, was_rework=False, cost_tokens=1000)
        assert len(acc.completions) == 1
        assert acc.total_cost_tokens == 1000

    def test_record_rework_increments(self):
        acc = MetricsAccumulator()
        now = time.time()
        acc.record_completion("T-001", now, now - 100, was_rework=True)
        assert acc.total_reworks == 1

    def test_record_non_rework_no_increment(self):
        acc = MetricsAccumulator()
        now = time.time()
        acc.record_completion("T-001", now, now - 100, was_rework=False)
        assert acc.total_reworks == 0

    def test_prune_on_overflow(self):
        acc = MetricsAccumulator(max_history=5)
        now = time.time()
        for i in range(10):
            acc.record_completion(f"T-{i}", now, now - 100)
        assert len(acc.completions) == 5
        assert acc.completions[-1].ticket_id == "T-9"

    def test_record_review(self):
        acc = MetricsAccumulator()
        acc.record_review(passed_first=True)
        assert acc.total_reviews == 1
        assert acc.first_pass_reviews == 1
        acc.record_review(passed_first=False)
        assert acc.total_reviews == 2
        assert acc.first_pass_reviews == 1

    def test_record_discovery(self):
        acc = MetricsAccumulator()
        acc.record_discovery(validated=3, duplicates=1)
        assert acc.total_discovery_scans == 1
        assert acc.validated_discoveries == 3
        assert acc.duplicate_discoveries == 1

    def test_record_human_intervention(self):
        acc = MetricsAccumulator()
        acc.record_human_intervention()
        assert acc.human_interventions == 1

    def test_set_hourly_cost(self):
        acc = MetricsAccumulator()
        acc.set_hourly_cost(5.5)
        assert acc.hourly_cost_usd == 5.5


class TestMetricsBuildThroughput:
    def test_throughput_per_hour(self):
        acc = MetricsAccumulator()
        now = 1000000.0
        for i in range(5):
            acc.record_completion(f"T-{i}", now - 100, now - 1000)
        ps = FakePipelineState()
        m = acc.build(ps, now=now)
        assert m.tickets_completed_per_hour == 5.0

    def test_throughput_excludes_old(self):
        acc = MetricsAccumulator()
        now = 1000000.0
        acc.record_completion("T-old", now - 7200, now - 8000)
        acc.record_completion("T-recent", now - 100, now - 1000)
        ps = FakePipelineState()
        m = acc.build(ps, now=now)
        assert m.tickets_completed_per_hour == 1.0

    def test_per_day_count(self):
        acc = MetricsAccumulator()
        now = 1000000.0
        acc.record_completion("T-1", now - 100, now - 1000)
        acc.record_completion("T-old", now - 90000, now - 100000)
        ps = FakePipelineState()
        m = acc.build(ps, now=now)
        assert m.tickets_completed_per_day == 1.0

    def test_mean_cycle_time(self):
        acc = MetricsAccumulator()
        now = 1000000.0
        acc.record_completion("T-1", 1100.0, 1000.0)
        acc.record_completion("T-2", 1200.0, 1000.0)
        ps = FakePipelineState()
        m = acc.build(ps, now=now)
        assert m.mean_ticket_cycle_time_seconds == pytest.approx(150.0)

    def test_mean_cycle_ignores_negative(self):
        acc = MetricsAccumulator()
        now = 1000000.0
        acc.record_completion("T-1", 900.0, 1000.0)
        ps = FakePipelineState()
        m = acc.build(ps, now=now)
        assert m.mean_ticket_cycle_time_seconds == 0.0


class TestMetricsBuildQuality:
    def test_first_pass_rate(self):
        acc = MetricsAccumulator()
        acc.record_review(passed_first=True)
        acc.record_review(passed_first=False)
        acc.record_review(passed_first=True)
        ps = FakePipelineState()
        m = acc.build(ps)
        assert m.first_pass_review_rate == pytest.approx(2/3)

    def test_first_pass_zero_when_no_reviews(self):
        acc = MetricsAccumulator()
        ps = FakePipelineState()
        m = acc.build(ps)
        assert m.first_pass_review_rate == 0.0

    def test_rework_rate(self):
        acc = MetricsAccumulator()
        now = time.time()
        acc.record_completion("T-1", now, now - 100, was_rework=True)
        acc.record_completion("T-2", now, now - 100, was_rework=False)
        ps = FakePipelineState()
        m = acc.build(ps, now=now)
        assert m.rework_rate == pytest.approx(0.5)

    def test_human_intervention_rate(self):
        acc = MetricsAccumulator()
        now = time.time()
        acc.record_completion("T-1", now, now - 100)
        acc.record_completion("T-2", now, now - 100)
        acc.record_human_intervention()
        ps = FakePipelineState()
        m = acc.build(ps, now=now)
        assert m.human_intervention_rate == pytest.approx(0.5)

    def test_discovery_yield(self):
        acc = MetricsAccumulator()
        acc.record_discovery(validated=3, duplicates=1)
        ps = FakePipelineState()
        m = acc.build(ps)
        assert m.discovery_yield_rate == pytest.approx(0.75)

    def test_discovery_yield_zero_when_none(self):
        acc = MetricsAccumulator()
        ps = FakePipelineState()
        m = acc.build(ps)
        assert m.discovery_yield_rate == 0.0


class TestMetricsBuildUtilization:
    def test_slot_utilization(self):
        acc = MetricsAccumulator()
        ps = FakePipelineState(active_count=15, total_slots=30)
        m = acc.build(ps)
        assert m.slot_utilization == pytest.approx(0.5)

    def test_slot_utilization_zero_slots(self):
        acc = MetricsAccumulator()
        ps = FakePipelineState(active_count=0, total_slots=0)
        m = acc.build(ps)
        assert m.slot_utilization == 0.0

    def test_productive_utilization_with_completions(self):
        acc = MetricsAccumulator()
        now = 1000000.0
        acc.record_completion("T-1", now - 100, now - 1000, was_productive=True)
        acc.record_completion("T-2", now - 200, now - 1000, was_productive=False)
        ps = FakePipelineState(active_count=2, total_slots=10)
        m = acc.build(ps, now=now)
        assert 0.0 <= m.productive_slot_utilization <= 1.0

    def test_productive_capped_at_one(self):
        acc = MetricsAccumulator()
        now = 1000000.0
        for i in range(10):
            acc.record_completion(f"T-{i}", now - 100, now - 1000, was_productive=True)
        ps = FakePipelineState(active_count=5, total_slots=10)
        m = acc.build(ps, now=now)
        assert m.productive_slot_utilization <= 1.0


class TestMetricsBuildCost:
    def test_cost_per_ticket(self):
        acc = MetricsAccumulator()
        now = 1000000.0
        acc.set_hourly_cost(10.0)
        for i in range(5):
            acc.record_completion(f"T-{i}", now - 100, now - 1000)
        ps = FakePipelineState()
        m = acc.build(ps, now=now)
        assert m.cost_per_ticket_usd == pytest.approx(2.0)

    def test_cost_zero_when_no_completions(self):
        acc = MetricsAccumulator()
        acc.set_hourly_cost(10.0)
        ps = FakePipelineState()
        m = acc.build(ps, now=1000000.0)
        assert m.cost_per_ticket_usd == 0.0

    def test_review_queue_age_with_reviewers(self):
        acc = MetricsAccumulator()
        now = 1000000.0
        w = make_worker(role="correctness_reviewer", started_at=now - 500)
        ps = FakePipelineState(reviewing_count=2, active_workers=[w], active_count=1)
        m = acc.build(ps, now=now)
        assert m.review_queue_age_seconds == pytest.approx(500.0)

    def test_review_queue_age_no_reviewers(self):
        acc = MetricsAccumulator()
        ps = FakePipelineState(reviewing_count=2, active_workers=[], active_count=0)
        m = acc.build(ps, now=1000000.0)
        assert m.review_queue_age_seconds == 0.0

    def test_scheduler_mode_preserved(self):
        acc = MetricsAccumulator()
        ps = FakePipelineState()
        m = acc.build(ps, scheduler_mode="REVIEW_HEAVY")
        assert m.scheduler_mode == "REVIEW_HEAVY"


class TestMetricsSaveLoad:
    def test_save_and_load_roundtrip(self, tmp_path):
        p = tmp_path / "metrics.json"
        acc = MetricsAccumulator()
        now = time.time()
        acc.record_completion("T-001", now, now - 100, was_rework=False, cost_tokens=500)
        acc.record_review(passed_first=True)
        acc.record_discovery(validated=3, duplicates=1)
        acc.set_hourly_cost(5.0)
        acc.save(p)
        assert p.exists()
        acc2 = MetricsAccumulator.load(p)
        assert len(acc2.completions) == 1
        assert acc2.completions[0].ticket_id == "T-001"
        assert acc2.total_reviews == 1
        assert acc2.validated_discoveries == 3
        assert acc2.hourly_cost_usd == 5.0

    def test_load_missing_returns_empty(self, tmp_path):
        acc = MetricsAccumulator.load(tmp_path / "missing.json")
        assert acc.completions == []
        assert acc.total_reviews == 0

    def test_load_corrupt_returns_empty(self, tmp_path):
        p = tmp_path / "metrics.json"
        p.write_text("not json", encoding="utf-8")
        acc = MetricsAccumulator.load(p)
        assert acc.completions == []

    def test_save_creates_parent_dirs(self, tmp_path):
        p = tmp_path / "a" / "b" / "metrics.json"
        acc = MetricsAccumulator()
        acc.save(p)
        assert p.exists()

    def test_save_atomic(self, tmp_path):
        p = tmp_path / "metrics.json"
        acc = MetricsAccumulator()
        acc.record_completion("T-001", time.time(), time.time() - 100)
        acc.save(p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert "completions" in data
        assert len(list(tmp_path.glob("*.tmp"))) == 0

    def test_load_preserves_completion_fields(self, tmp_path):
        p = tmp_path / "metrics.json"
        acc = MetricsAccumulator()
        now = time.time()
        acc.record_completion("T-001", now, now - 100, was_rework=True, was_productive=False, cost_tokens=999)
        acc.save(p)
        acc2 = MetricsAccumulator.load(p)
        c = acc2.completions[0]
        assert c.was_rework is True
        assert c.was_productive is False
        assert c.cost_tokens == 999
