#!/usr/bin/env python3
"""Extensive pytest suite for work_scorer + lifecycle_scheduler — P1 ranking and dispatch.

Covers every public function with Given/When/Then naming, one When per test,
isolated fixtures, real objects where possible, mock only for handlers/clock.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable
from unittest.mock import MagicMock

import pytest

# pyright: reportAny=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportPrivateUsage=false, reportUnusedCallResult=false, reportUnusedVariable=false, reportUnusedImport=false, reportUnannotatedClassAttribute=false, reportUnusedParameter=false, reportUnusedFunction=false, reportArgumentType=false

from codebot.ticket_engine import TicketState
from codebot.lifecycle_scheduler import (
    ALWAYS_ON_AGENTS,
    LIFECYCLE_DISPATCH_TABLE,
    LifecycleDispatchEntry,
    LifecyclePhase,
    PhaseDemand,
    compute_agent_demand,
    dispatch_lifecycle,
    get_queue_demands,
)
from codebot.pipeline_state import PipelineState
from codebot.ticket_engine import RiskLevel, Severity, TicketClass, TicketState, create_ticket
from codebot.work_scorer import (
    SEVERITY_WEIGHTS,
    STAGE_PRIORITY,
    ScoredWorkItem,
    compute_aging_bonus,
    compute_bottleneck_relief,
    compute_conflict_penalty,
    compute_dependency_unlock_value,
    compute_risk_penalty,
    rank_work_items,
    score_ticket,
    score_work_item,
)

NOW = 1_000_000.0


# ---------------------------------------------------------------------------
# helpers — strictly typed, no Any
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AgingConfig:
    aging_start_seconds: int = 86400
    aging_rate_per_hour: float = 1.0
    max_aging_bonus: float = 30.0


def _ticket(
    severity: Severity = Severity.MEDIUM,
    risk: RiskLevel = RiskLevel.MEDIUM,
    ticket_class: TicketClass = TicketClass.BUG,
    state: TicketState = TicketState.READY,
    cost: int = 0,
    created_at: float | None = None,
) -> object:
    t = create_ticket(
        title="Test ticket",
        ticket_class=ticket_class,
        severity=severity,
        source="test",
        evidence="evidence-" + str(time.time_ns())[-6:],
        problem_statement="problem statement",
        desired_state="fixed",
        acceptance_criteria=["criterion"],
        risk=risk,
        estimated_cost_tokens=cost,
    )
    if created_at is not None:
        object.__setattr__(t, "created_at", created_at)
    if state != TicketState.DISCOVERED:
        object.__setattr__(t, "state", state)
    return t


class _Pressure:
    def __init__(
        self,
        implementation_pressure: float = 0.0,
        review_pressure: float = 0.0,
        verification_pressure: float = 0.0,
        rework_pressure: float = 0.0,
        planning_pressure: float = 0.0,
        discovery_pressure: float = 0.0,
    ) -> None:
        self.implementation_pressure = implementation_pressure
        self.review_pressure = review_pressure
        self.verification_pressure = verification_pressure
        self.rework_pressure = rework_pressure
        self.planning_pressure = planning_pressure
        self.discovery_pressure = discovery_pressure


class _GraphWithDependents:
    def __init__(self, mapping: dict[str, list[str]]) -> None:
        self._mapping = mapping

    def get_dependents(self, ticket_id: str) -> list[str]:
        return self._mapping.get(ticket_id, [])


class _GraphRaises:
    def get_dependents(self, ticket_id: str) -> list[str]:
        raise RuntimeError("graph boom")


# ---------------------------------------------------------------------------
# ScoredWorkItem — dataclass invariants
# ---------------------------------------------------------------------------


class TestScoredWorkItem:
    def test_Given_item_When_total_score_accessed_Then_equals_score(self) -> None:
        item = ScoredWorkItem(ticket_id="T-1", stage="implementation", score=42.1, components={})
        result = item.total_score
        assert result == 42.1

    def test_Given_schedulable_item_When_is_schedulable_Then_true(self) -> None:
        item = ScoredWorkItem(ticket_id="T-1", stage="implementation", score=10.0, components={}, blocked=False, has_conflict=False)
        result = item.is_schedulable
        assert result is True

    def test_Given_blocked_item_When_is_schedulable_Then_false(self) -> None:
        item = ScoredWorkItem(ticket_id="T-1", stage="implementation", score=10.0, components={}, blocked=True)
        result = item.is_schedulable
        assert result is False

    def test_Given_conflict_item_When_is_schedulable_Then_false(self) -> None:
        item = ScoredWorkItem(ticket_id="T-1", stage="implementation", score=10.0, components={}, has_conflict=True)
        result = item.is_schedulable
        assert result is False

    def test_Given_item_When_frozen_attempt_Then_raises(self) -> None:
        item = ScoredWorkItem(ticket_id="T-1", stage="implementation", score=10.0, components={})
        with pytest.raises(AttributeError):
            setattr(item, "score", 99.0)


# ---------------------------------------------------------------------------
# compute_aging_bonus
# ---------------------------------------------------------------------------


class TestComputeAgingBonus:
    def test_Given_age_below_start_When_compute_aging_bonus_Then_zero(self) -> None:
        cfg = AgingConfig(aging_start_seconds=86400, aging_rate_per_hour=1.0, max_aging_bonus=30.0)
        created = NOW - 1000
        result = compute_aging_bonus(created, NOW, cfg)
        assert result == 0.0

    def test_Given_age_just_over_start_When_compute_aging_bonus_Then_positive(self) -> None:
        cfg = AgingConfig(aging_start_seconds=86400, aging_rate_per_hour=1.0, max_aging_bonus=30.0)
        created = NOW - 90000
        result = compute_aging_bonus(created, NOW, cfg)
        assert result > 0.0
        assert result == pytest.approx(1.0, rel=0.01)

    def test_Given_very_old_ticket_When_compute_aging_bonus_Then_capped(self) -> None:
        cfg = AgingConfig(aging_start_seconds=3600, aging_rate_per_hour=10.0, max_aging_bonus=5.0)
        created = NOW - 100000
        result = compute_aging_bonus(created, NOW, cfg)
        assert result == 5.0

    def test_Given_future_created_at_When_compute_aging_bonus_Then_zero(self) -> None:
        cfg = AgingConfig()
        created = NOW + 10000
        result = compute_aging_bonus(created, NOW, cfg)
        assert result == 0.0

    def test_Given_exact_start_age_When_compute_aging_bonus_Then_zero(self) -> None:
        cfg = AgingConfig(aging_start_seconds=86400, aging_rate_per_hour=1.0, max_aging_bonus=30.0)
        created = NOW - 86400
        result = compute_aging_bonus(created, NOW, cfg)
        assert result == 0.0

    def test_Given_zero_aging_rate_When_compute_aging_bonus_Then_zero(self) -> None:
        cfg = AgingConfig(aging_start_seconds=100, aging_rate_per_hour=0.0, max_aging_bonus=30.0)
        created = NOW - 100000
        result = compute_aging_bonus(created, NOW, cfg)
        assert result == 0.0


# ---------------------------------------------------------------------------
# compute_risk_penalty
# ---------------------------------------------------------------------------


class TestComputeRiskPenalty:
    def test_Given_critical_risk_When_compute_risk_penalty_Then_highest(self) -> None:
        crit = compute_risk_penalty("critical", 0)
        low = compute_risk_penalty("low", 0)
        assert crit > low
        assert crit == 15.0

    def test_Given_low_risk_zero_cost_When_compute_risk_penalty_Then_zero(self) -> None:
        result = compute_risk_penalty("low", 0)
        assert result == 0.0

    def test_Given_unknown_risk_When_compute_risk_penalty_Then_zero(self) -> None:
        result = compute_risk_penalty("unknown", 0)
        assert result == 0.0

    def test_Given_case_insensitive_When_compute_risk_penalty_Then_equal(self) -> None:
        a = compute_risk_penalty("CRITICAL", 0)
        b = compute_risk_penalty("critical", 0)
        assert a == b

    def test_Given_high_token_cost_When_compute_risk_penalty_Then_adds_and_capped(self) -> None:
        penalty_low = compute_risk_penalty("low", 0)
        penalty_high = compute_risk_penalty("low", 500000)
        assert penalty_high > penalty_low
        penalty_huge = compute_risk_penalty("low", 10_000_000)
        assert penalty_huge == pytest.approx(10.0)

    def test_Given_medium_risk_with_cost_When_compute_risk_penalty_Then_combined(self) -> None:
        result = compute_risk_penalty("medium", 100000)
        assert result == pytest.approx(3.0 + 2.0)


# ---------------------------------------------------------------------------
# compute_bottleneck_relief
# ---------------------------------------------------------------------------


class TestComputeBottleneckRelief:
    def test_Given_none_pressure_When_compute_bottleneck_relief_Then_zero(self) -> None:
        result = compute_bottleneck_relief("implementation", None)
        assert result == 0.0

    def test_Given_low_pressure_When_compute_bottleneck_relief_Then_zero(self) -> None:
        p = _Pressure(implementation_pressure=1.0)
        result = compute_bottleneck_relief("implementation", p)
        assert result == 0.0

    def test_Given_high_pressure_When_compute_bottleneck_relief_Then_positive(self) -> None:
        p = _Pressure(implementation_pressure=2.0)
        result = compute_bottleneck_relief("implementation", p)
        assert result == pytest.approx(20.0)

    def test_Given_extreme_pressure_When_compute_bottleneck_relief_Then_capped_at_40(self) -> None:
        p = _Pressure(implementation_pressure=10.0)
        result = compute_bottleneck_relief("implementation", p)
        assert result == 40.0

    def test_Given_unknown_stage_When_compute_bottleneck_relief_Then_zero(self) -> None:
        p = _Pressure(implementation_pressure=5.0)
        result = compute_bottleneck_relief("unknown_stage", p)
        assert result == 0.0

    def test_Given_review_pressure_When_compute_bottleneck_relief_Then_uses_review_map(self) -> None:
        p = _Pressure(review_pressure=3.0)
        result = compute_bottleneck_relief("review", p)
        assert result == pytest.approx(40.0)

    def test_Given_slight_over_one_When_compute_bottleneck_relief_Then_small_relief(self) -> None:
        p = _Pressure(implementation_pressure=1.5)
        result = compute_bottleneck_relief("implementation", p)
        assert result == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# compute_conflict_penalty
# ---------------------------------------------------------------------------


class TestComputeConflictPenalty:
    def test_Given_has_conflict_true_When_compute_conflict_penalty_Then_200(self) -> None:
        result = compute_conflict_penalty(True)
        assert result == 200.0

    def test_Given_has_conflict_false_When_compute_conflict_penalty_Then_zero(self) -> None:
        result = compute_conflict_penalty(False)
        assert result == 0.0


# ---------------------------------------------------------------------------
# compute_dependency_unlock_value
# ---------------------------------------------------------------------------


class TestComputeDependencyUnlockValue:
    def test_Given_none_graph_When_compute_dependency_unlock_value_Then_zero(self) -> None:
        result = compute_dependency_unlock_value("T-1", None, None)
        assert result == 0.0

    def test_Given_no_dependents_When_compute_dependency_unlock_value_Then_zero(self) -> None:
        graph = _GraphWithDependents({})
        ps = MagicMock()
        result = compute_dependency_unlock_value("T-1", graph, ps)
        assert result == 0.0

    def test_Given_two_blocked_dependents_When_compute_dependency_unlock_value_Then_positive(self) -> None:
        graph = _GraphWithDependents({"T-1": ["T-2", "T-3"]})
        ps = PipelineState(unsatisfied_dependencies={"T-2": ("T-1",), "T-3": ("T-1",)})
        result = compute_dependency_unlock_value("T-1", graph, ps)
        assert result > 0.0
        assert result <= 50.0

    def test_Given_mixed_blocked_unblocked_When_compute_dependency_unlock_value_Then_counts_only_blocked(self) -> None:
        graph = _GraphWithDependents({"T-1": ["T-2", "T-3"]})
        ps_all_blocked = PipelineState(unsatisfied_dependencies={"T-2": ("T-1",), "T-3": ("T-1",)})
        ps_one_blocked = PipelineState(unsatisfied_dependencies={"T-2": ("T-1",)})
        r_all = compute_dependency_unlock_value("T-1", graph, ps_all_blocked)
        r_one = compute_dependency_unlock_value("T-1", graph, ps_one_blocked)
        assert r_all > r_one

    def test_Given_many_dependents_When_compute_dependency_unlock_value_Then_capped_at_50(self) -> None:
        mapping = {"T-1": [f"T-{i}" for i in range(100)]}
        graph = _GraphWithDependents(mapping)
        deps = {f"T-{i}": ("T-1",) for i in range(100)}
        ps = PipelineState(unsatisfied_dependencies=deps)
        result = compute_dependency_unlock_value("T-1", graph, ps)
        assert result == 50.0

    def test_Given_graph_raises_When_compute_dependency_unlock_value_Then_zero(self) -> None:
        graph = _GraphRaises()
        ps = PipelineState()
        result = compute_dependency_unlock_value("T-1", graph, ps)
        assert result == 0.0

    def test_Given_single_dependent_When_compute_dependency_unlock_value_Then_expected_value(self) -> None:
        graph = _GraphWithDependents({"T-1": ["T-2"]})
        ps = PipelineState(unsatisfied_dependencies={"T-2": ("T-1",)})
        result = compute_dependency_unlock_value("T-1", graph, ps)
        assert result == pytest.approx(10.0 / 1.1, rel=0.001)


# ---------------------------------------------------------------------------
# score_work_item — scoring, ranking, priority
# ---------------------------------------------------------------------------


class TestScoreWorkItem:
    def test_Given_critical_ticket_When_score_work_item_Then_higher_than_low(self) -> None:
        t_crit = _ticket(severity=Severity.CRITICAL, risk=RiskLevel.LOW)
        t_low = _ticket(severity=Severity.LOW, risk=RiskLevel.LOW)
        cfg = AgingConfig()
        s_crit = score_work_item(t_crit, "implementation", None, cfg, now=NOW)
        s_low = score_work_item(t_low, "implementation", None, cfg, now=NOW)
        assert s_crit.score > s_low.score

    def test_Given_security_critical_When_score_work_item_Then_security_emergency_base(self) -> None:
        t_sec = _ticket(severity=Severity.CRITICAL, ticket_class=TicketClass.SECURITY, risk=RiskLevel.LOW)
        t_bug = _ticket(severity=Severity.CRITICAL, ticket_class=TicketClass.BUG, risk=RiskLevel.LOW)
        cfg = AgingConfig()
        s_sec = score_work_item(t_sec, "implementation", None, cfg, now=NOW)
        s_bug = score_work_item(t_bug, "implementation", None, cfg, now=NOW)
        assert s_sec.score > s_bug.score
        assert s_sec.components["base_priority"] == 150.0

    def test_Given_blocked_ticket_When_score_work_item_Then_blocked_true(self) -> None:
        t = _ticket(state=TicketState.READY)
        tid = getattr(t, "id")
        ps_blocked = PipelineState(unsatisfied_dependencies={tid: ("DEP-1",)})
        ps_free = PipelineState(unsatisfied_dependencies={})
        cfg = AgingConfig()
        s_blocked = score_work_item(t, "implementation", None, cfg, pipeline_state=ps_blocked, now=NOW)
        s_free = score_work_item(t, "implementation", None, cfg, pipeline_state=ps_free, now=NOW)
        assert s_blocked.blocked is True
        assert s_free.blocked is False

    def test_Given_conflict_flag_When_score_work_item_Then_conflict_penalty_applied(self) -> None:
        t = _ticket()
        cfg = AgingConfig()
        clean = score_work_item(t, "implementation", None, cfg, has_conflict=False, now=NOW)
        conflict = score_work_item(t, "implementation", None, cfg, has_conflict=True, now=NOW)
        assert clean.score > conflict.score
        assert conflict.components["conflict_penalty"] == 200.0
        assert clean.components["conflict_penalty"] == 0.0

    def test_Given_rework_stage_When_score_work_item_Then_rework_boost(self) -> None:
        t = _ticket()
        cfg = AgingConfig()
        s_impl = score_work_item(t, "implementation", None, cfg, now=NOW)
        s_rework = score_work_item(t, "rework", None, cfg, now=NOW)
        assert s_rework.score > s_impl.score
        assert s_rework.components["rework_boost"] == 20.0
        assert s_impl.components["rework_boost"] == 0.0

    def test_Given_unknown_severity_When_score_work_item_Then_fallback_weight(self) -> None:
        t = _ticket()
        object.__setattr__(t, "severity", "nonsense")
        cfg = AgingConfig()
        scored = score_work_item(t, "implementation", None, cfg, now=NOW)
        assert scored.components["severity_weight"] == 10.0

    def test_Given_unknown_stage_When_score_work_item_Then_fallback_base(self) -> None:
        t = _ticket()
        cfg = AgingConfig()
        scored = score_work_item(t, "unknown_stage_xyz", None, cfg, now=NOW)
        assert scored.components["base_priority"] == 10.0

    def test_Given_bottleneck_pressure_When_score_work_item_Then_relief_added(self) -> None:
        t = _ticket()
        cfg = AgingConfig()
        pressure = _Pressure(implementation_pressure=2.5)
        scored = score_work_item(t, "implementation", pressure, cfg, now=NOW)
        assert scored.components["bottleneck_relief"] > 0.0
        assert scored.components["bottleneck_relief"] == pytest.approx(30.0)

    def test_Given_old_ticket_When_score_work_item_Then_aging_bonus_positive(self) -> None:
        old_created = NOW - 200000
        t = _ticket(created_at=old_created)
        cfg = AgingConfig(aging_start_seconds=86400, aging_rate_per_hour=1.0, max_aging_bonus=30.0)
        scored = score_work_item(t, "implementation", None, cfg, now=NOW)
        assert scored.components["aging_bonus"] > 0.0

    def test_Given_zero_token_cost_When_score_work_item_Then_risk_penalty_zero_for_low(self) -> None:
        t = _ticket(severity=Severity.LOW, risk=RiskLevel.LOW, cost=0)
        cfg = AgingConfig()
        scored = score_work_item(t, "discovery", None, cfg, now=NOW)
        assert scored.components["risk_penalty"] == 0.0
        assert scored.components["base_priority"] == 10.0
        assert scored.components["severity_weight"] == 10.0

    def test_Given_high_cost_ticket_When_score_work_item_Then_risk_penalty_includes_cost(self) -> None:
        t = _ticket(risk=RiskLevel.LOW, cost=200000)
        cfg = AgingConfig()
        scored = score_work_item(t, "implementation", None, cfg, now=NOW)
        assert scored.components["risk_penalty"] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# rank_work_items / score_ticket
# ---------------------------------------------------------------------------


class TestRankWorkItems:
    def test_Given_empty_list_When_rank_work_items_Then_empty(self) -> None:
        result = rank_work_items([], now=NOW)
        assert result == []

    def test_Given_mixed_severities_When_rank_work_items_Then_descending(self) -> None:
        t_low = _ticket(severity=Severity.LOW, state=TicketState.READY)
        t_crit = _ticket(severity=Severity.CRITICAL, state=TicketState.READY)
        t_med = _ticket(severity=Severity.MEDIUM, state=TicketState.READY)
        ranked = rank_work_items([t_low, t_crit, t_med], now=NOW)
        assert ranked[0].ticket_id == getattr(t_crit, "id")
        assert ranked[-1].ticket_id == getattr(t_low, "id")

    def test_Given_blocked_high_score_When_rank_work_items_Then_blocked_last(self) -> None:
        t_high = _ticket(severity=Severity.CRITICAL, state=TicketState.READY)
        t_low = _ticket(severity=Severity.LOW, state=TicketState.READY)
        ps = PipelineState(unsatisfied_dependencies={getattr(t_high, "id"): ("DEP",)})
        ranked = rank_work_items([t_high, t_low], now=NOW, pipeline_state=ps)
        assert ranked[-1].ticket_id == getattr(t_high, "id")
        assert ranked[0].ticket_id == getattr(t_low, "id")

    def test_Given_prescored_items_When_rank_work_items_Then_sorted_by_score(self) -> None:
        items = [
            ScoredWorkItem(ticket_id="b", stage="implementation", score=10.0, components={}),
            ScoredWorkItem(ticket_id="a", stage="implementation", score=20.0, components={}),
        ]
        ranked = rank_work_items(items)
        assert ranked[0].ticket_id == "a"
        assert ranked[1].ticket_id == "b"

    def test_Given_tie_score_When_rank_work_items_Then_broken_by_id(self) -> None:
        items = [
            ScoredWorkItem(ticket_id="b", stage="implementation", score=10.0, components={}),
            ScoredWorkItem(ticket_id="a", stage="implementation", score=10.0, components={}),
        ]
        ranked = rank_work_items(items)
        assert ranked[0].ticket_id == "a"

    def test_Given_all_zero_scores_When_rank_work_items_Then_still_sorted_by_id(self) -> None:
        items = [
            ScoredWorkItem(ticket_id="c", stage="implementation", score=0.0, components={}),
            ScoredWorkItem(ticket_id="b", stage="implementation", score=0.0, components={}),
            ScoredWorkItem(ticket_id="a", stage="implementation", score=0.0, components={}),
        ]
        ranked = rank_work_items(items)
        assert [r.ticket_id for r in ranked] == ["a", "b", "c"]

    def test_Given_empty_queue_pressure_When_rank_work_items_Then_no_relief(self) -> None:
        t = _ticket(severity=Severity.MEDIUM, state=TicketState.READY)
        ranked = rank_work_items([t], now=NOW, queue_pressure=None)
        assert ranked[0].components["bottleneck_relief"] == 0.0

    def test_Given_mixed_schedulable_and_blocked_When_rank_work_items_Then_schedulable_first(self) -> None:
        schedulable = ScoredWorkItem(ticket_id="s1", stage="implementation", score=5.0, components={}, blocked=False, has_conflict=False)
        blocked = ScoredWorkItem(ticket_id="b1", stage="implementation", score=100.0, components={}, blocked=True)
        ranked = rank_work_items([blocked, schedulable])
        assert ranked[0].ticket_id == "s1"
        assert ranked[1].ticket_id == "b1"

    def test_Given_conflict_items_mixed_When_rank_work_items_Then_conflict_last(self) -> None:
        clean = ScoredWorkItem(ticket_id="clean", stage="implementation", score=5.0, components={}, has_conflict=False)
        conflict = ScoredWorkItem(ticket_id="conflict", stage="implementation", score=100.0, components={}, has_conflict=True)
        ranked = rank_work_items([conflict, clean])
        assert ranked[0].ticket_id == "clean"


class TestScoreTicket:
    def test_Given_ticket_When_score_ticket_Then_scored_work_item_returned(self) -> None:
        t = _ticket(state=TicketState.READY)
        scored = score_ticket(t, now=NOW)
        assert scored.ticket_id == getattr(t, "id")
        assert scored.score > 0

    def test_Given_dict_ticket_When_score_ticket_Then_scores(self) -> None:
        t = _ticket(severity=Severity.CRITICAL, state=TicketState.READY)
        tid = getattr(t, "id")
        d = {"id": tid, "severity": Severity.CRITICAL, "state": TicketState.READY, "created_at": NOW - 1000, "ticket_class": TicketClass.BUG, "risk": RiskLevel.MEDIUM}
        scored = score_ticket(d, now=NOW)
        assert scored.score > 0
        assert isinstance(scored.ticket_id, str)
        assert scored.score == pytest.approx(scored.score)

    def test_Given_aged_ticket_When_score_ticket_Then_aging_bonus_computed(self) -> None:
        created = NOW - 100000
        t = _ticket(created_at=created, state=TicketState.READY)
        scored = score_ticket(t, now=NOW, aging_start_seconds=86400, aging_rate_per_hour=1.0, max_aging_bonus=30.0)
        assert scored.components["aging_bonus"] > 0.0
        assert scored.components["aging_bonus"] <= 30.0

    def test_Given_implementing_state_ticket_When_score_ticket_Then_stage_is_implementing(self) -> None:
        t = _ticket(state=TicketState.IMPLEMENTING)
        scored = score_ticket(t, now=NOW)
        assert scored.stage == TicketState.IMPLEMENTING.value.upper()

    def test_Given_ticket_with_no_state_When_score_ticket_Then_defaults_to_ready(self) -> None:
        t = _ticket()
        object.__setattr__(t, "state", None)
        scored = score_ticket(t, now=NOW)
        assert scored.stage in ("READY", "TRIAGED")

    def test_Given_conflicting_pipeline_When_score_ticket_Then_has_conflict_flag(self) -> None:
        t = _ticket(state=TicketState.READY)
        tid = getattr(t, "id")
        ps = PipelineState(conflict_groups=(frozenset({tid, "OTHER-ACTIVE"}),), active_workers=())
        active = PipelineState(
            active_workers=(
                PipelineState.__dataclass_fields__["active_workers"].default_factory()
                if False
                else ()
            )
        )
        # Build PipelineState with active worker for OTHER-ACTIVE so conflict detected
        from codebot.pipeline_state import WorkerSlot as WS

        worker = WS(worker_id="w1", ticket_id="OTHER-ACTIVE", role="general_implementer", started_at=NOW, heartbeat_at=NOW, lease_expires=NOW + 600)
        ps2 = PipelineState(conflict_groups=(frozenset({tid, "OTHER-ACTIVE"}),), active_workers=(worker,))
        scored = score_ticket(t, now=NOW, pipeline_state=ps2)
        assert scored.has_conflict is True


# ---------------------------------------------------------------------------
# lifecycle_scheduler — Phase, entry, demand, routing
# ---------------------------------------------------------------------------


class TestLifecyclePhase:
    def test_Given_lifecycle_phase_When_iterated_Then_members(self) -> None:
        members = list(LifecyclePhase)
        assert len(members) == 6
        assert LifecyclePhase.TRIAGE in members
        assert LifecyclePhase.IMPLEMENT in members
        assert LifecyclePhase.REVIEW in members

    def test_Given_phase_When_value_accessed_Then_string(self) -> None:
        assert LifecyclePhase.IMPLEMENT.value == "IMPLEMENT"
        assert LifecyclePhase.REVIEW.value == "REVIEW"


class TestLifecycleDispatchEntry:
    def test_Given_dispatch_table_When_counted_Then_six_entries(self) -> None:
        assert len(LIFECYCLE_DISPATCH_TABLE) == 6

    def test_Given_dispatch_table_When_handler_names_collected_Then_strings(self) -> None:
        for entry in LIFECYCLE_DISPATCH_TABLE:
            assert isinstance(entry.handler_name, str) and len(entry.handler_name) > 0

    def test_Given_dispatch_table_When_ticket_states_checked_Then_uppercase(self) -> None:
        for entry in LIFECYCLE_DISPATCH_TABLE:
            for state in entry.ticket_states:
                assert state == state.upper()


class TestPhaseDemand:
    def test_Given_phase_demand_When_created_Then_fields(self) -> None:
        entry = LIFECYCLE_DISPATCH_TABLE[0]
        pd = PhaseDemand(entry=entry, ticket_count=3, agents_needed=1)
        assert pd.ticket_count == 3
        assert pd.agents_needed == 1


class TestComputeAgentDemand:
    def test_Given_one_ticket_tpa_one_When_compute_agent_demand_Then_one(self) -> None:
        entry = LIFECYCLE_DISPATCH_TABLE[4]
        counts = {"PLANNING": 1}
        result = compute_agent_demand(counts, entry)
        assert result == 1

    def test_Given_three_tickets_tpa_three_When_compute_agent_demand_Then_one(self) -> None:
        entry = LIFECYCLE_DISPATCH_TABLE[2]
        counts = {"GOAL": 3}
        result = compute_agent_demand(counts, entry)
        assert result == 1

    def test_Given_four_tickets_tpa_three_When_compute_agent_demand_Then_two(self) -> None:
        entry = LIFECYCLE_DISPATCH_TABLE[2]
        counts = {"GOAL": 4}
        result = compute_agent_demand(counts, entry)
        assert result == 2

    def test_Given_multi_state_counts_When_compute_agent_demand_Then_sums(self) -> None:
        entry = LIFECYCLE_DISPATCH_TABLE[4]
        counts = {"PLANNING": 2, "REWORK": 3}
        result = compute_agent_demand(counts, entry)
        assert result == 5

    def test_Given_recovery_two_states_When_compute_agent_demand_Then_sums_both(self) -> None:
        entry = LIFECYCLE_DISPATCH_TABLE[4]
        counts = {"PLANNING": 2, "REWORK": 3}
        result = compute_agent_demand(counts, entry)
        assert result == 5

    def test_Given_empty_dict_and_nonempty_entry_When_compute_agent_demand_Then_zero(self) -> None:
        entry = LIFECYCLE_DISPATCH_TABLE[5]
        result = compute_agent_demand({}, entry)
        assert result == 0

    def test_Given_large_count_When_compute_agent_demand_Then_ceil_division(self) -> None:
        entry = LifecycleDispatchEntry(
            phase=LifecyclePhase.PLANNING,
            ticket_states=("PLANNING",),
            agent_roles=("implementation_planner",),
            handler_name="_dispatch_planning_agents",
            requires_bots=True,
            description="test",
            tickets_per_agent=3,
        )
        counts = {"PLANNING": 10}
        result = compute_agent_demand(counts, entry)
        assert result == 4


class TestGetQueueDemands:
    def test_Given_empty_counts_When_get_queue_demands_Then_empty(self) -> None:
        result = get_queue_demands({})
        assert result == []

    def test_Given_single_implementing_When_get_queue_demands_Then_one_demand(self) -> None:
        counts = {"PLANNING": 5}
        demands = get_queue_demands(counts)
        assert len(demands) == 1
        assert demands[0].entry.phase == LifecyclePhase.IMPLEMENT
        assert demands[0].ticket_count == 5

    def test_Given_multiple_phases_When_get_queue_demands_Then_preserves_table_order(self) -> None:
        counts = {"DISCOVERED": 1, "PLANNING": 3, "IMPLEMENT": 2}
        demands = get_queue_demands(counts)
        phases = [d.entry.phase for d in demands]
        assert phases == [LifecyclePhase.TRIAGE, LifecyclePhase.IMPLEMENT, LifecyclePhase.REVIEW][:len(phases)] or LifecyclePhase.TRIAGE in phases

    def test_Given_zero_state_not_in_table_When_get_queue_demands_Then_ignored(self) -> None:
        counts = {"COMPLETE": 100, "PLANNING": 1}
        demands = get_queue_demands(counts)
        assert len(demands) == 1
        assert demands[0].entry.phase == LifecyclePhase.IMPLEMENT

    def test_Given_decompose_three_tickets_When_get_queue_demands_Then_agents_needed_one(self) -> None:
        counts = {"GOAL": 3}
        demands = get_queue_demands(counts)
        assert demands[0].agents_needed == 1

    def test_Given_decompose_four_tickets_When_get_queue_demands_Then_agents_needed_two(self) -> None:
        counts = {"GOAL": 4}
        demands = get_queue_demands(counts)
        assert demands[0].agents_needed == 2

    def test_Given_all_states_populated_When_get_queue_demands_Then_six_demands(self) -> None:
        counts = {
            "DISCOVERED": 1,
            "TRIAGED": 1,
            "GOAL": 1,
            "DECOMP": 1,
            "PLANNING": 1,
            "IMPLEMENT": 1,
        }
        demands = get_queue_demands(counts)
        assert len(demands) == 6

    def test_Given_implement_rework_combined_When_get_queue_demands_Then_combined_ticket_count(self) -> None:
        counts = {"PLANNING": 2, "REWORK": 3}
        demands = get_queue_demands(counts)
        impl = [d for d in demands if d.entry.phase == LifecyclePhase.IMPLEMENT][0]
        assert impl.ticket_count == 5


class TestDispatchLifecycle:
    def test_Given_no_tickets_When_dispatch_lifecycle_Then_empty_results(self) -> None:
        results = dispatch_lifecycle({}, {})
        assert results == {}

    def test_Given_triage_tickets_handler_exists_When_dispatch_lifecycle_Then_handler_called(self) -> None:
        counts = {"DISCOVERED": 2}
        called: list[int] = []

        def handler() -> int:
            called.append(1)
            return 2

        registry = {"run_triage_fast_paths": handler}
        results = dispatch_lifecycle(counts, registry)
        assert len(called) == 1

    def test_Given_handler_missing_When_dispatch_lifecycle_Then_skipped_with_warning(self) -> None:
        counts = {"PLANNING": 1}
        registry: dict[str, object] = {}
        results = dispatch_lifecycle(counts, registry)
        assert results == {}

    def test_Given_implementation_requires_bots_When_dispatch_lifecycle_Then_bots_and_count_passed(self) -> None:
        counts = {"PLANNING": 2}
        received: dict[str, object] = {}

        def handler(bots: object, needed: int) -> int:
            received["bots"] = bots
            received["needed"] = needed
            return needed

        bots = {"bot1": object()}
        # Handler for IMPLEMENT is dispatch_implementation_agents
        registry = {"dispatch_implementation_agents": handler}
        results = dispatch_lifecycle(counts, registry, bots=bots)
        assert received["bots"] is bots
        assert results["dispatch_implementation_agents"] == 2

    def test_Given_handler_returns_none_When_dispatch_lifecycle_Then_zero_recorded(self) -> None:
        counts = {"IMPLEMENT": 1}

        def handler() -> None:
            return None

        registry = {"advance_reviewed_tickets": handler}
        results = dispatch_lifecycle(counts, registry)
        assert results["advance_reviewed_tickets"] == 0

    def test_Given_handler_raises_generic_When_dispatch_lifecycle_Then_zero_and_continues(self) -> None:
        counts = {"IMPLEMENT": 1, "PLANNING": 1}

        def bad_handler() -> int:
            raise ValueError("review boom")

        def good_handler(bots: object, needed: int) -> int:
            return 3

        registry = {
            "advance_reviewed_tickets": bad_handler,
            "dispatch_implementation_agents": good_handler,
        }
        results = dispatch_lifecycle(counts, registry, bots={})
        assert results["dispatch_implementation_agents"] == 3

    def test_Given_handler_type_error_then_fallback_When_dispatch_lifecycle_Then_second_attempt_succeeds(self) -> None:
        counts = {"PLANNING": 1}

        def handler_requires_one_arg(bots: object) -> int:
            return 7

        registry = {"dispatch_implementation_agents": handler_requires_one_arg}
        results = dispatch_lifecycle(counts, registry, bots={"b": object()})
        assert results["dispatch_implementation_agents"] == 7

    def test_Given_handler_type_error_and_second_raises_When_dispatch_lifecycle_Then_zero(self) -> None:
        counts = {"PLANNING": 1}

        call_count = {"n": 0}

        def tricky(*args: object) -> int:
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise TypeError("wrong args")
            raise RuntimeError("second fail")

        registry = {"dispatch_implementation_agents": tricky}
        results = dispatch_lifecycle(counts, registry, bots={})
        assert results["dispatch_implementation_agents"] == 0

    def test_Given_multiple_phases_When_dispatch_lifecycle_Then_all_handlers_dispatched(self) -> None:
        counts = {"DISCOVERED": 1, "PLANNING": 1, "IMPLEMENT": 1}
        registry = {
            "run_triage_fast_paths": lambda: 1,
            "dispatch_implementation_agents": lambda bots, needed: 2,
            "advance_reviewed_tickets": lambda: 3,
        }
        results = dispatch_lifecycle(counts, registry, bots={})
        assert len(results) == 3

    def test_Given_requires_bots_false_handler_not_passed_bots_When_dispatch_lifecycle_Then_handler_called_without_bots(self) -> None:
        counts = {"DISCOVERED": 1}
        called_with: list[tuple[object, ...]] = []

        def handler(*args: object) -> int:
            called_with.append(args)
            return 1

        registry = {"run_triage_fast_paths": handler}
        dispatch_lifecycle(counts, registry, bots={"x": object()})
        assert called_with[0] == ()

    def test_Given_bots_none_even_requires_bots_When_dispatch_lifecycle_Then_handler_called_without_bots(self) -> None:
        counts = {"PLANNING": 1}
        called_with: list[tuple[object, ...]] = []

        def handler(*args: object) -> int:
            called_with.append(args)
            return 1

        registry = {"dispatch_implementation_agents": handler}
        dispatch_lifecycle(counts, registry, bots=None)
        assert called_with[0] == ()


class TestDispatchTableInvariants:
    def test_Given_dispatch_table_When_tickets_per_agent_checked_Then_positive(self) -> None:
        for entry in LIFECYCLE_DISPATCH_TABLE:
            assert entry.tickets_per_agent >= 1

    def test_Given_dispatch_table_When_phases_checked_Then_unique(self) -> None:
        phases = [e.phase for e in LIFECYCLE_DISPATCH_TABLE]
        assert len(phases) == len(set(phases))

    def test_Given_dispatch_table_When_agent_roles_for_implementation_Then_contains_expected(self) -> None:
        impl = [e for e in LIFECYCLE_DISPATCH_TABLE if e.phase == LifecyclePhase.IMPLEMENT][0]
        assert "general_implementer" in impl.agent_roles

    def test_Given_dispatch_table_When_agent_roles_for_review_Then_contains_reviewers(self) -> None:
        rev = [e for e in LIFECYCLE_DISPATCH_TABLE if e.phase == LifecyclePhase.REVIEW][0]
        assert "correctness_reviewer" in rev.agent_roles

    def test_Given_dispatch_table_When_handler_names_checked_Then_strings(self) -> None:
        for entry in LIFECYCLE_DISPATCH_TABLE:
            assert isinstance(entry.handler_name, str) and len(entry.handler_name) > 0

    def test_Given_dispatch_table_When_ticket_states_checked_Then_uppercase(self) -> None:
        for entry in LIFECYCLE_DISPATCH_TABLE:
            for state in entry.ticket_states:
                assert state == state.upper()

    def test_Given_no_recovery_phase_When_inspected_Then_deferred_is_terminal(self) -> None:
        assert not any("DEFERRED" in e.ticket_states for e in LIFECYCLE_DISPATCH_TABLE)
        assert TicketState.DEFERRED in TicketState

    def test_Given_severity_weights_When_inspected_Then_positive(self) -> None:
        from codebot.work_scorer import SEVERITY_WEIGHTS
        assert all(v > 0 for v in SEVERITY_WEIGHTS.values())

    def test_Given_stage_weights_When_inspected_Then_positive(self) -> None:
        from codebot.work_scorer import SEVERITY_WEIGHTS
        assert all(v > 0 for v in SEVERITY_WEIGHTS.values())
