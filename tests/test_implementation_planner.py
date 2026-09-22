"""Tests for implementation_planner.py."""
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.implementation_planner import (
    PlanDepth,
    PlanStore,
    PlanningTelemetry,
    determine_plan_depth,
    evidence_fingerprint,
    generate_plan,
    get_planning_budget,
)

class TestPlanDepth:
    def test_low_risk_summary(self):
        assert determine_plan_depth("low") == PlanDepth.SUMMARY

    def test_medium_risk_standard(self):
        assert determine_plan_depth("medium") == PlanDepth.STANDARD

    def test_high_risk_full(self):
        assert determine_plan_depth("high") == PlanDepth.FULL

    def test_critical_risk_full(self):
        assert determine_plan_depth("critical") == PlanDepth.FULL

    def test_unknown_defaults_standard(self):
        assert determine_plan_depth("unknown") == PlanDepth.STANDARD

class TestGeneratePlan:
    def test_low_risk_minimal(self):
        plan = generate_plan("CB-1", "low", ["lib/x.py"], [], ["tests pass"])
        assert plan.depth == PlanDepth.SUMMARY
        assert plan.ticket_id == "CB-1"
        assert plan.estimated_effort_tokens == 5000

    def test_medium_risk_standard(self):
        plan = generate_plan("CB-2", "medium", ["lib/x.py"], [], ["tests pass"], security_impact="auth check")
        assert plan.depth == PlanDepth.STANDARD
        assert any("Security" in s for s in plan.security_considerations)

    def test_high_risk_full(self):
        plan = generate_plan("CB-3", "critical", ["lib/auth.py"], ["CB-1"], ["no regressions"], ticket_class="security")
        assert plan.depth == PlanDepth.FULL
        assert "Adversarial review required" in plan.security_considerations
        assert len(plan.architectural_implications) > 0

    def test_tests_generated_from_acceptance(self):
        plan = generate_plan("CB-4", "medium", ["lib/x.py"], [], ["feature works", "no crash"])
        assert len(plan.tests_required) >= 2

    def test_serialization_roundtrip(self):
        plan = generate_plan("CB-5", "low", ["a.py"], [], ["ok"])
        raw = plan.to_json()
        from codebot.implementation_planner import ImplementationPlan
        p2 = ImplementationPlan.from_dict(json.loads(raw))
        assert p2.ticket_id == "CB-5"

    def test_low_risk_plan_has_a_lean_budget_and_fingerprint(self):
        plan = generate_plan("CB-6", "low", ["a.py"], [], ["ok"])
        assert plan.investigation_budget == {
            "max_grep_calls": 1,
            "max_read_calls": 0,
            "max_glob_calls": 0,
            "max_steps": 2,
        }
        assert plan.evidence_fingerprint
        assert plan.fresh_verification_required


class TestPlanningBudget:
    def test_budget_scales_with_risk(self):
        assert get_planning_budget("medium").max_read_calls == 1
        assert get_planning_budget("critical").max_glob_calls == 1

    def test_fingerprint_is_order_independent(self):
        first = evidence_fingerprint("medium", ["b.py", "a.py"], ["B", "A"], ["two", "one"])
        second = evidence_fingerprint("medium", ["a.py", "b.py"], ["A", "B"], ["one", "two"])
        assert first == second

class TestPlanStore:
    def test_save_and_load(self, tmp_path):
        store = PlanStore(tmp_path)
        plan = generate_plan("CB-1", "low", ["a.py"], [], ["ok"])
        store.save(plan)
        loaded = store.load("CB-1")
        assert loaded is not None
        assert not isinstance(loaded, dict)
        assert loaded.ticket_id == "CB-1"

    def test_exists(self, tmp_path):
        store = PlanStore(tmp_path)
        assert not store.exists("CB-1")
        plan = generate_plan("CB-1", "low", ["a.py"], [], ["ok"])
        store.save(plan)
        assert store.exists("CB-1")

    def test_missing_returns_none(self, tmp_path):
        store = PlanStore(tmp_path)
        assert store.load("CB-missing") is None

    def test_reuse_creates_a_new_plan_that_requires_fresh_verification(self, tmp_path):
        store = PlanStore(tmp_path)
        source = generate_plan("CB-source", "medium", ["a.py"], [], ["ok"])
        store.save(source)
        reused = store.reuse_for_ticket("CB-target", "medium", ["a.py"], [], ["ok"])
        assert reused is not None
        assert reused.ticket_id == "CB-target"
        assert reused.evidence_reused
        assert reused.reused_from_ticket_id == "CB-source"
        assert reused.fresh_verification_required


class TestPlanningTelemetry:
    def test_summary_reports_duration_and_rework_rate(self, tmp_path):
        telemetry = PlanningTelemetry(tmp_path)
        telemetry.record("CB-1", "low", 2.0, "complete")
        telemetry.record("CB-2", "high", 4.0, "rework", rework_count=1, evidence_reused=True)
        assert telemetry.summary() == {
            "plans": 2,
            "average_duration_seconds": 3.0,
            "rework_rate": 0.5,
        }
