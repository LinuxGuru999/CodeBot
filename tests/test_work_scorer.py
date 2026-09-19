"""Tests for work_scorer.py — urgency/risk/dependency scoring and ordering."""

import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock

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
from codebot.ticket_engine import TicketClass, Severity, RiskLevel, TicketState, create_ticket
from codebot.pipeline_state import PipelineState, WorkerSlot


def make_ticket(severity=Severity.MEDIUM, risk=RiskLevel.MEDIUM, ticket_class=TicketClass.BUG, state=TicketState.READY, cost=0, created_at=None):
    t = create_ticket(
        title="Test ticket",
        ticket_class=ticket_class,
        severity=severity,
        source="test",
        evidence="ev",
        problem_statement="prob",
        desired_state="fixed",
        acceptance_criteria=["ac"],
        risk=risk,
        estimated_cost_tokens=cost,
    )
    if created_at is not None:
        object.__setattr__(t, "created_at", created_at)
    if state != TicketState.DISCOVERED:
        # Use object's transition to set state for testing
        # Direct setattr for state bypass (frozen dataclass workaround via object.__setattr__)
        object.__setattr__(t, "state", state)
    return t


class AgingCfg:
    def __init__(self, start=86400, rate=1.0, max_bonus=30.0):
        self.aging_start_seconds = start
        self.aging_rate_per_hour = rate
        self.max_aging_bonus = max_bonus
        self.max_age_bonus = max_bonus
        self.aging_rate = rate


class TestSeverityWeights:
    def test_critical_highest(self):
        assert SEVERITY_WEIGHTS["critical"] > SEVERITY_WEIGHTS["high"]
        assert SEVERITY_WEIGHTS["high"] > SEVERITY_WEIGHTS["medium"]
        assert SEVERITY_WEIGHTS["medium"] > SEVERITY_WEIGHTS["low"]

    def test_values(self):
        assert SEVERITY_WEIGHTS["critical"] == 100.0
        assert SEVERITY_WEIGHTS["low"] == 10.0


class TestStagePriority:
    def test_security_emergency_highest(self):
        assert STAGE_PRIORITY["security_emergency"] > STAGE_PRIORITY["rework"]
        assert STAGE_PRIORITY["rework"] > STAGE_PRIORITY["verification"]

    def test_discovery_lowest(self):
        assert STAGE_PRIORITY["discovery"] < STAGE_PRIORITY["planning"]
        assert STAGE_PRIORITY["planning"] < STAGE_PRIORITY["implementation"]


class TestComputeAgingBonus:
    def test_no_bonus_before_start(self):
        cfg = AgingCfg(start=86400, rate=1.0, max_bonus=30.0)
        now = 1000000.0
        created = now - 1000
        assert compute_aging_bonus(created, now, cfg) == 0.0

    def test_bonus_after_start(self):
        cfg = AgingCfg(start=86400, rate=1.0, max_bonus=30.0)
        now = 200000.0
        created = now - 90000
        bonus = compute_aging_bonus(created, now, cfg)
        assert bonus > 0.0

    def test_capped_at_max(self):
        cfg = AgingCfg(start=3600, rate=10.0, max_bonus=5.0)
        now = 1000000.0
        created = now - 100000
        assert compute_aging_bonus(created, now, cfg) == 5.0

    def test_future_created_returns_zero(self):
        cfg = AgingCfg()
        now = 1000.0
        created = now + 10000
        assert compute_aging_bonus(created, now, cfg) == 0.0

    def test_exact_start_bonus_zero(self):
        cfg = AgingCfg(start=86400, rate=1.0, max_bonus=30.0)
        now = 1000000.0
        created = now - 86400
        assert compute_aging_bonus(created, now, cfg) == 0.0


class TestComputeRiskPenalty:
    def test_critical_highest(self):
        assert compute_risk_penalty("critical", 0) > compute_risk_penalty("high", 0)
        assert compute_risk_penalty("high", 0) > compute_risk_penalty("medium", 0)
        assert compute_risk_penalty("medium", 0) > compute_risk_penalty("low", 0)

    def test_low_is_zero(self):
        assert compute_risk_penalty("low", 0) == 0.0

    def test_cost_adds_penalty(self):
        assert compute_risk_penalty("low", 100000) > compute_risk_penalty("low", 0)

    def test_cost_capped(self):
        assert compute_risk_penalty("low", 10_000_000) <= 10.0 + 0.01 or True
        assert compute_risk_penalty("low", 10_000_000) == pytest.approx(10.0)

    def test_case_insensitive(self):
        assert compute_risk_penalty("CRITICAL", 0) == compute_risk_penalty("critical", 0)

    def test_unknown_risk_zero(self):
        assert compute_risk_penalty("unknown", 0) == 0.0


class TestComputeBottleneckRelief:
    def test_no_pressure_no_relief(self):
        assert compute_bottleneck_relief("implementation", None) == 0.0

    def test_low_pressure_no_relief(self):
        pressure = type("P", (), {"implementation_pressure": 0.5})()
        assert compute_bottleneck_relief("implementation", pressure) == 0.0

    def test_high_pressure_relief(self):
        pressure = type("P", (), {"implementation_pressure": 2.0, "review_pressure": 0, "verification_pressure": 0, "rework_pressure": 0, "planning_pressure": 0, "discovery_pressure": 0})()
        assert compute_bottleneck_relief("implementation", pressure) > 0

    def test_relief_capped(self):
        pressure = type("P", (), {"implementation_pressure": 10.0, "review_pressure": 0, "verification_pressure": 0, "rework_pressure": 0, "planning_pressure": 0, "discovery_pressure": 0})()
        assert compute_bottleneck_relief("implementation", pressure) == 40.0

    def test_unknown_stage_no_relief(self):
        pressure = type("P", (), {"implementation_pressure": 5.0})()
        assert compute_bottleneck_relief("unknown_stage", pressure) == 0.0


class TestComputeConflictPenalty:
    def test_conflict_heavy(self):
        assert compute_conflict_penalty(True) == 200.0

    def test_no_conflict_zero(self):
        assert compute_conflict_penalty(False) == 0.0


class TestScoreWorkItem:
    def test_basic_scoring(self):
        t = make_ticket(severity=Severity.CRITICAL, risk=RiskLevel.LOW)
        item = score_work_item(t, "implementation", None, AgingCfg(), now=time.time())
        assert item.score > 0
        assert item.ticket_id == t.id
        assert "base_priority" in item.components
        assert "severity_weight" in item.components

    def test_critical_scores_higher_than_low(self):
        now = time.time()
        t_crit = make_ticket(severity=Severity.CRITICAL, risk=RiskLevel.LOW)
        t_low = make_ticket(severity=Severity.LOW, risk=RiskLevel.LOW)
        s_crit = score_work_item(t_crit, "implementation", None, AgingCfg(), now=now)
        s_low = score_work_item(t_low, "implementation", None, AgingCfg(), now=now)
        assert s_crit.score > s_low.score

    def test_security_emergency_boost(self):
        now = time.time()
        t = make_ticket(severity=Severity.CRITICAL, ticket_class=TicketClass.SECURITY, risk=RiskLevel.LOW)
        item = score_work_item(t, "implementation", None, AgingCfg(), now=now)
        t2 = make_ticket(severity=Severity.CRITICAL, ticket_class=TicketClass.BUG, risk=RiskLevel.LOW)
        item2 = score_work_item(t2, "implementation", None, AgingCfg(), now=now)
        assert item.score > item2.score

    def test_blocked_flag(self):
        now = time.time()
        ps = PipelineState(unsatisfied_dependencies={})
        t = make_ticket(state=TicketState.READY)
        ps2 = PipelineState(unsatisfied_dependencies={t.id: ("dep1",)})
        item_blocked = score_work_item(t, "implementation", None, AgingCfg(), dependency_graph=None, pipeline_state=ps2, now=now)
        item_unblocked = score_work_item(t, "implementation", None, AgingCfg(), dependency_graph=None, pipeline_state=ps, now=now)
        assert item_blocked.blocked is True
        assert item_unblocked.blocked is False

    def test_conflict_penalty_applied(self):
        now = time.time()
        t = make_ticket()
        item_clean = score_work_item(t, "implementation", None, AgingCfg(), has_conflict=False, now=now)
        item_conflict = score_work_item(t, "implementation", None, AgingCfg(), has_conflict=True, now=now)
        assert item_clean.score > item_conflict.score

    def test_rework_boost(self):
        now = time.time()
        t = make_ticket()
        item_impl = score_work_item(t, "implementation", None, AgingCfg(), now=now)
        item_rework = score_work_item(t, "rework", None, AgingCfg(), now=now)
        assert item_rework.score > item_impl.score

    def test_is_schedulable(self):
        t = make_ticket()
        item = ScoredWorkItem(ticket_id=t.id, stage="implementation", score=10.0, components={}, blocked=False, has_conflict=False)
        assert item.is_schedulable is True
        item_blocked = ScoredWorkItem(ticket_id=t.id, stage="implementation", score=10.0, components={}, blocked=True)
        assert item_blocked.is_schedulable is False
        item_conflict = ScoredWorkItem(ticket_id=t.id, stage="implementation", score=10.0, components={}, has_conflict=True)
        assert item_conflict.is_schedulable is False


class TestTotalScore:
    """Regression tests for CB-2987638-E5C0: single total_score property returning .score."""

    @pytest.mark.parametrize("score", [0.0, 10.0, 42.1, 87.3, -5.5, 150.75])
    def test_total_score_equals_score(self, score):
        item = ScoredWorkItem(ticket_id="T-001", stage="implementation", score=score, components={})
        assert item.total_score == item.score
        assert item.total_score == score

    def test_total_score_matches_scored_item(self):
        t = make_ticket(severity=Severity.CRITICAL, risk=RiskLevel.LOW)
        item = score_work_item(t, "implementation", None, AgingCfg(), now=time.time())
        assert item.total_score == item.score

    def test_single_total_score_definition(self):
        source = Path(__file__).parent.parent.joinpath("codebot", "work_scorer.py").read_text()
        assert source.count("def total_score") == 1


class TestDependencyUnlock:
    def test_no_graph_returns_zero(self):
        assert compute_dependency_unlock_value("T-001", None, None) == 0.0

    def test_no_dependents_returns_zero(self):
        graph = MagicMock()
        graph.get_dependents.return_value = []
        ps = MagicMock()
        assert compute_dependency_unlock_value("T-001", graph, ps) == 0.0

    def test_with_dependents_positive(self):
        graph = MagicMock()
        graph.get_dependents.return_value = ["T-002", "T-003"]
        ps = MagicMock()
        ps.tickets_blocked_by_deps.return_value = True
        val = compute_dependency_unlock_value("T-001", graph, ps)
        assert val > 0
        assert val <= 50.0

    def test_capped_at_50(self):
        graph = MagicMock()
        graph.get_dependents.return_value = [f"T-{i}" for i in range(100)]
        ps = MagicMock()
        ps.tickets_blocked_by_deps.return_value = True
        assert compute_dependency_unlock_value("T-001", graph, ps) == 50.0


class TestRankWorkItems:
    def test_sorted_descending(self):
        now = time.time()
        t1 = make_ticket(severity=Severity.LOW, state=TicketState.READY)
        t2 = make_ticket(severity=Severity.CRITICAL, state=TicketState.READY)
        t3 = make_ticket(severity=Severity.MEDIUM, state=TicketState.READY)
        ranked = rank_work_items([t1, t2, t3], now=now)
        assert ranked[0].ticket_id == t2.id
        assert ranked[-1].ticket_id == t1.id

    def test_blocked_last(self):
        now = time.time()
        t1 = make_ticket(severity=Severity.CRITICAL, state=TicketState.READY)
        t2 = make_ticket(severity=Severity.LOW, state=TicketState.READY)
        ps = PipelineState(unsatisfied_dependencies={t1.id: ("dep",)})
        ranked = rank_work_items([t1, t2], now=now, pipeline_state=ps)
        assert ranked[-1].ticket_id == t1.id
        assert ranked[0].ticket_id == t2.id

    def test_pre_scored_passthrough(self):
        items = [
            ScoredWorkItem(ticket_id="a", stage="implementation", score=10.0, components={}),
            ScoredWorkItem(ticket_id="b", stage="implementation", score=20.0, components={}),
        ]
        ranked = rank_work_items(items)
        assert ranked[0].ticket_id == "b"

    def test_tie_broken_by_id(self):
        items = [
            ScoredWorkItem(ticket_id="b", stage="implementation", score=10.0, components={}),
            ScoredWorkItem(ticket_id="a", stage="implementation", score=10.0, components={}),
        ]
        ranked = rank_work_items(items)
        assert ranked[0].ticket_id == "a"

    def test_empty_list(self):
        assert rank_work_items([]) == []

    def test_score_ticket_wrapper(self):
        now = time.time()
        t = make_ticket(state=TicketState.READY)
        scored = score_ticket(t, now=now)
        assert scored.ticket_id == t.id
        assert scored.score > 0

    def test_score_ticket_dict_input(self):
        now = time.time()
        t = make_ticket(severity=Severity.CRITICAL, state=TicketState.READY)
        d = {"id": t.id, "severity": Severity.CRITICAL, "state": TicketState.READY, "created_at": now - 1000, "ticket_class": TicketClass.BUG, "risk": RiskLevel.MEDIUM}
        scored = score_ticket(d, now=now)
        assert scored.score > 0

    def test_per_ticket_token_limit_enforced(self):
        """Verify per_ticket_token_limit penalizes tickets exceeding the limit."""
        now = time.time()
        # Ticket with high estimated cost
        t_high_cost = make_ticket(severity=Severity.MEDIUM, state=TicketState.READY, cost=500000)
        # Ticket with low estimated cost
        t_low_cost = make_ticket(severity=Severity.MEDIUM, state=TicketState.READY, cost=10000)
        
        # With a low token limit, high-cost ticket should score lower
        scored_high = score_ticket(t_high_cost, now=now, per_ticket_token_limit=100000)
        scored_low = score_ticket(t_low_cost, now=now, per_ticket_token_limit=100000)
        
        # High cost ticket should be penalized more than low cost ticket
        assert scored_low.score > scored_high.score

    def test_per_ticket_token_limit_in_rank_work_items(self):
        """Verify rank_work_items respects per_ticket_token_limit when sorting."""
        now = time.time()
        t1 = make_ticket(severity=Severity.HIGH, state=TicketState.READY, cost=500000)
        t2 = make_ticket(severity=Severity.HIGH, state=TicketState.READY, cost=10000)
        
        # With strict token limit, lower-cost ticket should rank higher despite same severity
        ranked = rank_work_items([t1, t2], now=now, per_ticket_token_limit=100000)
        
        # t2 (low cost) should rank before t1 (high cost) due to token limit penalty
        assert ranked[0].ticket_id == t2.id
        assert ranked[1].ticket_id == t1.id

    def test_score_ticket_with_aged_ticket_no_attribute_error(self):
        """Test that score_ticket works with aged tickets without AttributeError.
        
        Regression test for CB-2978742-4AE6: max_age_bonus vs max_aging_bonus mismatch.
        """
        now = 200000.0
        # Create a ticket older than aging_start_seconds (86400s = 24h)
        created_at = now - 100000  # ~27.8 hours old
        t = make_ticket(created_at=created_at, state=TicketState.READY)
        
        # This should not raise AttributeError
        scored = score_ticket(t, now=now, aging_start_seconds=86400, aging_rate_per_hour=1.0, max_aging_bonus=30.0)
        
        assert scored.ticket_id == t.id
        assert scored.score > 0
        # Verify aging bonus was actually computed (should be > 0 since ticket is older than start threshold)
        assert scored.components["aging_bonus"] > 0.0
        # Verify it's capped at max_aging_bonus
        assert scored.components["aging_bonus"] <= 30.0
