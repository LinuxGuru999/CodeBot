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

    def test_save_dict_with_depth(self, tmp_path):
        """save(dict) with depth+ticket_id writes primary and loads as ImplementationPlan."""
        store = PlanStore(tmp_path)
        plan_dict = generate_plan("CB-DICT-1", "medium", ["b.py"], [], ["dict test"]).to_dict()
        store.save(plan_dict)
        loaded = store.load("CB-DICT-1")
        assert loaded is not None
        assert not isinstance(loaded, dict)
        assert loaded.ticket_id == "CB-DICT-1"
        assert loaded.depth == PlanDepth.STANDARD
        # Verify legacy sync
        assert (tmp_path / "plans" / "CB-DICT-1.plan.json").exists()
        assert (tmp_path / "plans" / "CB-DICT-1.json").exists()

    def test_save_dict_without_depth_wrapper_fallback(self, tmp_path):
        """save(dict) without depth uses fallback/wrapper logic."""
        store = PlanStore(tmp_path)
        # Bare dict without depth/ticket_id structure expected by ImplPlan
        bare_dict = {"custom_key": "value", "ticket_id": "CB-BARE-1"}
        store.save(bare_dict)
        loaded = store.load("CB-BARE-1")
        assert loaded is not None
        # Bare dicts are stored as-is or wrapped depending on branch; load returns dict if not ImplPlan
        assert isinstance(loaded, dict) or (hasattr(loaded, 'ticket_id') and loaded.ticket_id == "CB-BARE-1")
        
        # Wrapper style {ticket_id, plan: {...}}
        wrapper_dict = {"ticket_id": "CB-WRAP-1", "plan": {"foo": "bar"}}
        store.save(wrapper_dict)
        loaded_wrap = store.load("CB-WRAP-1")
        assert loaded_wrap is not None
        # Verify files exist
        assert (tmp_path / "plans" / "CB-BARE-1.plan.json").exists()
        assert (tmp_path / "plans" / "CB-WRAP-1.plan.json").exists()
        # Check legacy sync for these paths (if implemented/fixed)
        assert (tmp_path / "plans" / "CB-BARE-1.json").exists()
        assert (tmp_path / "plans" / "CB-WRAP-1.json").exists()

    def test_save_two_arg_dict_with_depth(self, tmp_path):
        """save(ticket_id, dict) with depth writes primary+legacy."""
        store = PlanStore(tmp_path)
        plan_dict = generate_plan("CB-2ARG-D", "high", ["c.py"], [], ["two arg dict"]).to_dict()
        store.save("CB-2ARG-D", plan_dict)
        loaded = store.load("CB-2ARG-D")
        assert loaded is not None
        assert not isinstance(loaded, dict)
        assert loaded.ticket_id == "CB-2ARG-D"
        assert (tmp_path / "plans" / "CB-2ARG-D.plan.json").exists()
        assert (tmp_path / "plans" / "CB-2ARG-D.json").exists()

    def test_save_two_arg_dict_without_depth(self, tmp_path):
        """save(ticket_id, dict) without depth creates wrapper."""
        store = PlanStore(tmp_path)
        arbitrary_dict = {"step": 1, "action": "do something"}
        store.save("CB-2ARG-W", arbitrary_dict)
        loaded = store.load("CB-2ARG-W")
        assert loaded is not None
        # Load returns the inner plan dict or wrapper depending on implementation
        # The ticket requires legacy sync for this path too
        assert (tmp_path / "plans" / "CB-2ARG-W.plan.json").exists()
        assert (tmp_path / "plans" / "CB-2ARG-W.json").exists()

    def test_save_two_arg_implementation_plan(self, tmp_path):
        """save(ticket_id, ImplementationPlan) writes primary+legacy."""
        store = PlanStore(tmp_path)
        plan = generate_plan("CB-2ARG-IP", "low", ["d.py"], [], ["two arg ip"])
        store.save("CB-2ARG-IP", plan)
        loaded = store.load("CB-2ARG-IP")
        assert loaded is not None
        assert not isinstance(loaded, dict)
        assert loaded.ticket_id == "CB-2ARG-IP"
        assert (tmp_path / "plans" / "CB-2ARG-IP.plan.json").exists()
        assert (tmp_path / "plans" / "CB-2ARG-IP.json").exists()

    def test_save_raises_type_error(self, tmp_path):
        """save raises TypeError on unsupported types."""
        store = PlanStore(tmp_path)
        with pytest.raises(TypeError):
            store.save(123)
        with pytest.raises(TypeError):
            store.save(None)
        with pytest.raises(TypeError):
            store.save("CB-ID", 123)
        with pytest.raises(TypeError):
            store.save("CB-ID", "bad")

    def test_legacy_path_sync_on_all_save_paths(self, tmp_path):
        """Comprehensive check that legacy .json is synced for all valid save signatures."""
        store = PlanStore(tmp_path)
        
        # 1. ImplementationPlan single arg
        p1 = generate_plan("CB-SYNC-1", "low", ["e.py"], [], ["sync 1"])
        store.save(p1)
        assert (tmp_path / "plans" / "CB-SYNC-1.json").exists()
        
        # 2. Dict with depth single arg
        p2_dict = generate_plan("CB-SYNC-2", "medium", ["f.py"], [], ["sync 2"]).to_dict()
        store.save(p2_dict)
        assert (tmp_path / "plans" / "CB-SYNC-2.json").exists()
        
        # 3. Two-arg ImplementationPlan
        p3 = generate_plan("CB-SYNC-3", "high", ["g.py"], [], ["sync 3"])
        store.save("CB-SYNC-3", p3)
        assert (tmp_path / "plans" / "CB-SYNC-3.json").exists()
        
        # 4. Two-arg dict with depth
        p4_dict = generate_plan("CB-SYNC-4", "critical", ["h.py"], [], ["sync 4"]).to_dict()
        store.save("CB-SYNC-4", p4_dict)
        assert (tmp_path / "plans" / "CB-SYNC-4.json").exists()
        
        # 5. Two-arg dict without depth (wrapper style)
        store.save("CB-SYNC-5", {"action": "test"})
        assert (tmp_path / "plans" / "CB-SYNC-5.json").exists()


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
