"""Tests for implementation_planner.py."""
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from implementation_planner import generate_plan, PlanDepth, determine_plan_depth, PlanStore

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
        from implementation_planner import ImplementationPlan
        p2 = ImplementationPlan.from_dict(json.loads(raw))
        assert p2.ticket_id == "CB-5"

class TestPlanStore:
    def test_save_and_load(self, tmp_path):
        store = PlanStore(tmp_path)
        plan = generate_plan("CB-1", "low", ["a.py"], [], ["ok"])
        store.save(plan)
        loaded = store.load("CB-1")
        assert loaded is not None
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
