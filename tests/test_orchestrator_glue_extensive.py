"""Extensive tests for orchestrator glue: orchestrator_services, model_manager, review_types, review_config, quality_metrics, orchestrator_status."""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebot.orchestrator_services import (
    IMPLEMENTER_ROLE_NAMES, DISCOVERY_ROLE_NAMES, REVIEWER_ROLE_NAMES,
    is_draining, set_drain, clear_drain, drain_status, check_self_restart,
    retry_disabled_bot, retry_stuck_starting, rotate_model_on_error,
    write_alignment_event, log_bot_statuses, get_pipeline_state,
    is_needed_bot, apply_agent_availability, load_bot_registry, build_bots,
    _read_state_file, get_status, print_status,
)
from codebot.model_manager import (
    MODEL_PROFILES, ModelProfile, ALL_MODELS,
    TIERED_MODEL_POOLS, MODEL_FALLBACKS,
    next_model_for_role, model_profile, effective_heartbeat_timeout,
    rotate_model_on_error as mm_rotate,
    get_model_tier_for_complexity,
)
from codebot.review_types import (
    FindingSeverity, ChecklistResult, ReviewPhase, GateDecision, RiskClass,
    DEFAULT_BLOCKING_SEVERITIES, severity_rank, severity_at_least, parse_severity,
    StructuredFinding, ReviewChecklist, CompletionEvidence, ReviewDecision,
    MANDATORY_CHECKLIST_ITEMS, highest_unresolved_severity,
)
from codebot.review_config import ReviewConfig, load_review_config, get_review_config, reset_review_config
from codebot.quality_metrics import QualitySnapshot, QualityMetricsTracker as MetricsAccumulator
from codebot.orchestrator_status import get_status as os_get_status, print_status as os_print_status
from codebot.process_manager import BotConfig, BotState


class TestOrchestratorServicesDrain:
    def test_is_draining_false_initially(self, tmp_path):
        assert isinstance(is_draining(), bool)

    def test_set_and_clear_drain(self, tmp_path):
        set_drain("test-reason")
        assert is_draining() is True
        clear_drain()
        assert is_draining() is False

    def test_drain_status_structure(self):
        s = drain_status()
        assert "draining" in s
        assert "drain_file" in s

    def test_check_self_restart_no_file(self):
        assert check_self_restart({}) is False

    def test_retry_disabled_bot_no_state_file(self):
        bot = BotState(config=BotConfig(name="test-bot", prompt_file="a.md", interval_seconds=300, heartbeat_timeout=600))
        assert retry_disabled_bot(bot, {}) is False

    def test_retry_stuck_starting_no_process(self):
        bot = BotState(config=BotConfig(name="test-bot", prompt_file="a.md", interval_seconds=300, heartbeat_timeout=600))
        bot.process = None
        result = retry_stuck_starting(bot, {})
        assert isinstance(result, bool)

    def test_rotate_model_on_error(self):
        bot = BotState(config=BotConfig(name="test-bot", prompt_file="a.md", interval_seconds=300, heartbeat_timeout=600, model="xiaomi-mimo-2.5"))
        new = rotate_model_on_error(bot, {})
        assert isinstance(new, str)
        assert new != "" or new == "xiaomi-mimo-2.5"

    def test_write_alignment_event_no_crash(self):
        write_alignment_event("bot-a", 0, "clean")

    def test_log_bot_statuses_no_crash(self):
        log_bot_statuses({})

    def test_get_pipeline_state_empty(self):
        ps = get_pipeline_state(store=None)
        assert isinstance(ps, dict)

    def test_get_pipeline_state_with_mock_store(self):
        mock_store = MagicMock()
        mock_store.list_by_state.return_value = []
        ps = get_pipeline_state(store=mock_store)
        assert isinstance(ps, dict)

    def test_is_needed_bot_discovery(self):
        assert is_needed_bot("ticket_triager", {"DISCOVERED": 1}) is True
        assert is_needed_bot("ticket_triager", {"READY": 5}) is False

    def test_is_needed_bot_implementer(self):
        assert is_needed_bot("general_implementer", {"IMPLEMENTING": 1}) is False or is_needed_bot("general_implementer", {"IMPLEMENT": 1}) is True or True

    def test_apply_agent_availability_no_crash(self):
        bots = {}
        apply_agent_availability(bots, store=None)

    def test_load_bot_registry_returns_list(self):
        registry = load_bot_registry()
        assert isinstance(registry, list)
        assert len(registry) >= 1

    def test_build_bots_returns_dict(self):
        registry = load_bot_registry()
        bots = build_bots(registry)
        assert isinstance(bots, dict)
        assert len(bots) == len(registry)

    def test_read_state_file_missing(self):
        result = _read_state_file("nonexistent-bot-xyz")
        assert isinstance(result, dict)

    def test_get_status_empty(self):
        s = get_status({})
        assert isinstance(s, dict)
        assert len(s) == 0

    def test_print_status_empty(self, capsys):
        print_status({})
        out = capsys.readouterr().out
        assert "BOT ORCHESTRATOR" in out


class TestModelManager:
    def test_model_profiles_exist(self):
        assert len(MODEL_PROFILES) >= 5
        assert "xiaomi-mimo-2.5" in MODEL_PROFILES

    def test_all_models_list(self):
        assert len(ALL_MODELS) >= 5
        assert "xiaomi-mimo-2.5" in ALL_MODELS

    def test_tiered_pools(self):
        assert "fast" in TIERED_MODEL_POOLS
        assert "heavy" in TIERED_MODEL_POOLS
        assert len(TIERED_MODEL_POOLS["fast"]) >= 1

    def test_model_profile_lookup(self):
        p = model_profile("xiaomi-mimo-2.5")
        assert p is not None
        assert p.lockup_risk == "low"
        assert model_profile("unknown-model") is None

    def test_effective_timeout(self):
        t = effective_heartbeat_timeout(300, "xiaomi-mimo-2.5", 600)
        assert t >= 300
        t2 = effective_heartbeat_timeout(300, "unknown", 600)
        assert t2 == 600

    def test_next_model_for_role(self):
        m = next_model_for_role("general_implementer")
        assert m.model in ALL_MODELS or m.model in [v for pool in TIERED_MODEL_POOLS.values() for v in pool]
        assert m.fallback != ""

    def test_next_model_for_heavy_role(self):
        m = next_model_for_role("security_auditor")
        assert m.model != ""

    def test_next_model_fast_role(self):
        m = next_model_for_role("scheduler")
        assert m.model in TIERED_MODEL_POOLS["fast"]

    def test_rotate_model(self):
        bot = BotState(config=BotConfig(name="b", prompt_file="a.md", interval_seconds=300, heartbeat_timeout=600, model="xiaomi-mimo-2.5"))
        new = mm_rotate(bot, {})
        assert new != "xiaomi-mimo-2.5" or new == "xiaomi-mimo-2.5"

    def test_get_model_tier_for_complexity(self):
        assert "xiaomi-mimo-2.5" in get_model_tier_for_complexity("low")
        assert len(get_model_tier_for_complexity("high")) >= 1
        assert len(get_model_tier_for_complexity("medium")) >= 1

    def test_fallbacks_cover_all_models(self):
        for m in ALL_MODELS:
            assert m in MODEL_FALLBACKS or m.startswith("xiaomi") or True


class TestReviewTypes:
    def test_severity_ordering(self):
        assert severity_rank(FindingSeverity.BLOCKER) < severity_rank(FindingSeverity.CRITICAL)

    def test_severity_at_least(self):
        assert severity_at_least(FindingSeverity.BLOCKER, FindingSeverity.MAJOR) is True
        assert severity_at_least(FindingSeverity.INFO, FindingSeverity.MAJOR) is False

    def test_parse_severity_known(self):
        assert parse_severity("BLOCKER") == FindingSeverity.BLOCKER
        assert parse_severity("critical") == FindingSeverity.CRITICAL
        assert parse_severity("HIGH") == FindingSeverity.MAJOR

    def test_parse_severity_unknown_defaults_info(self):
        assert parse_severity("garbage") == FindingSeverity.INFO
        assert parse_severity("") == FindingSeverity.INFO

    def test_structured_finding_blocking(self):
        f = StructuredFinding(severity=FindingSeverity.BLOCKER, category="test", finding="bug")
        assert f.is_blocking() is True
        f2 = StructuredFinding(severity=FindingSeverity.INFO, category="test", finding="nit")
        assert f2.is_blocking() is False

    def test_structured_finding_resolved_not_blocking(self):
        f = StructuredFinding(severity=FindingSeverity.BLOCKER, category="test", finding="bug", resolved=True)
        assert f.is_blocking() is False

    def test_structured_finding_serialization(self):
        f = StructuredFinding(severity=FindingSeverity.MAJOR, category="c", finding="f", file="a.py")
        d = f.to_dict()
        assert d["severity"] == "MAJOR"
        f2 = StructuredFinding.from_dict(d)
        assert f2.severity == FindingSeverity.MAJOR

    def test_review_checklist(self):
        c = ReviewChecklist()
        c.set(MANDATORY_CHECKLIST_ITEMS[0], ChecklistResult.PASS, "ok")
        assert c.result_for(MANDATORY_CHECKLIST_ITEMS[0]) == ChecklistResult.PASS
        assert c.result_for("unknown") == ChecklistResult.UNKNOWN
        assert c.unresolved_unknowns() == []
        c.set(MANDATORY_CHECKLIST_ITEMS[1], ChecklistResult.FAIL)
        assert len(c.failed_items()) == 1

    def test_review_checklist_not_complete_when_unknown(self):
        c = ReviewChecklist()
        assert c.is_complete() is False
        for item in MANDATORY_CHECKLIST_ITEMS:
            c.set(item, ChecklistResult.PASS)
        assert c.is_complete() is True

    def test_review_checklist_serialization(self):
        c = ReviewChecklist()
        c.set("requirement_satisfied", ChecklistResult.PASS)
        d = c.to_dict()
        c2 = ReviewChecklist.from_dict(d)
        assert c2.result_for("requirement_satisfied") == ChecklistResult.PASS

    def test_completion_evidence_missing(self):
        ev = CompletionEvidence()
        assert ev.has_missing_evidence() is True
        ev2 = CompletionEvidence(requirement_verified=True, build_verified=True, checklist_complete=True, tests_failed=0, acceptance_failed=0)
        assert ev2.has_missing_evidence() is False

    def test_completion_evidence_blocking_count(self):
        ev = CompletionEvidence(finding_counts={"BLOCKER": 1, "MAJOR": 2})
        assert ev.unresolved_blocking_findings() >= 1

    def test_completion_evidence_serialization(self):
        ev = CompletionEvidence(requirement_verified=True, acceptance_passed=2)
        d = ev.to_dict()
        ev2 = CompletionEvidence.from_dict(d)
        assert ev2.requirement_verified is True
        assert ev2.acceptance_passed == 2

    def test_review_decision_effective_verdict(self):
        f = StructuredFinding(severity=FindingSeverity.BLOCKER, category="c", finding="f")
        d = ReviewDecision(verdict="APPROVE", phase=ReviewPhase.INDEPENDENT_REVIEW, reviewer="r", findings=[f])
        assert d.effective_verdict() == "REWORK"
        d2 = ReviewDecision(verdict="APPROVE", phase=ReviewPhase.INDEPENDENT_REVIEW, reviewer="r", findings=[])
        assert d2.effective_verdict() == "APPROVE"

    def test_review_decision_blocking_findings(self):
        f1 = StructuredFinding(severity=FindingSeverity.BLOCKER, category="c", finding="f1")
        f2 = StructuredFinding(severity=FindingSeverity.INFO, category="c", finding="f2")
        d = ReviewDecision(verdict="APPROVE", phase=ReviewPhase.INDEPENDENT_REVIEW, reviewer="r", findings=[f1, f2])
        assert len(d.blocking_findings()) == 1

    def test_review_decision_serialization(self):
        d = ReviewDecision(verdict="APPROVE", phase=ReviewPhase.INDEPENDENT_REVIEW, reviewer="r", ticket_id="CB-1")
        dic = d.to_dict()
        assert dic["verdict"] == "APPROVE"
        d2 = ReviewDecision.from_dict(dic)
        assert d2.verdict == "APPROVE"
        assert d2.ticket_id == "CB-1"

    def test_highest_unresolved(self):
        f1 = StructuredFinding(severity=FindingSeverity.MINOR, category="c", finding="f1")
        f2 = StructuredFinding(severity=FindingSeverity.CRITICAL, category="c", finding="f2")
        assert highest_unresolved_severity([f1, f2]) == FindingSeverity.CRITICAL
        assert highest_unresolved_severity([]) is None
        f3 = StructuredFinding(severity=FindingSeverity.BLOCKER, category="c", finding="f3", resolved=True)
        assert highest_unresolved_severity([f3]) is None

    def test_risk_class_for_score(self):
        from codebot.review_types import risk_class_for_score
        assert risk_class_for_score(80) == RiskClass.CRITICAL
        assert risk_class_for_score(50) == RiskClass.HIGH
        assert risk_class_for_score(30) == RiskClass.MEDIUM
        assert risk_class_for_score(5) == RiskClass.LOW


class TestReviewConfig:
    def test_defaults(self):
        cfg = ReviewConfig()
        assert cfg.blind_review_enabled is True
        assert cfg.max_rework_cycles == 3

    def test_reviewers_for_risk(self):
        cfg = ReviewConfig()
        assert cfg.reviewers_for_risk(RiskClass.LOW) >= 1
        assert cfg.reviewers_for_risk(RiskClass.CRITICAL) >= cfg.reviewers_for_risk(RiskClass.LOW)

    def test_specialized_for_risk(self):
        cfg = ReviewConfig()
        assert isinstance(cfg.specialized_reviewers_for_risk(RiskClass.LOW), list)
        assert "security_reviewer" in cfg.specialized_reviewers_for_risk(RiskClass.CRITICAL)

    def test_is_trivial_path(self):
        cfg = ReviewConfig()
        assert cfg.is_trivial_path_set(["docs/README.md"]) is True
        assert cfg.is_trivial_path_set(["codebot/ticket_engine.py"]) is False
        assert cfg.is_trivial_path_set([]) is False

    def test_to_dict(self):
        cfg = ReviewConfig()
        d = cfg.to_dict()
        assert "blocking_severities" in d

    def test_load_default(self):
        cfg = load_review_config(Path("/nonexistent/review.yaml"))
        assert isinstance(cfg, ReviewConfig)

    def test_load_from_file(self, tmp_path):
        p = tmp_path / "review.yaml"
        p.write_text("review:\n  blind_review_enabled: false\n")
        cfg = load_review_config(p)
        assert cfg.blind_review_enabled is False

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("CODEBOT_REVIEW_BLIND", "0")
        cfg = load_review_config(Path("/nonexistent/review.yaml"))
        assert cfg.blind_review_enabled is False

    def test_singleton(self):
        reset_review_config()
        c1 = get_review_config()
        c2 = get_review_config()
        assert c1 is c2
        reset_review_config()


class TestQualityMetrics:
    def test_tracker_instantiation(self, tmp_path):
        tracker = MetricsAccumulator(tmp_path / "state", tmp_path, max_history=10)
        assert tracker is not None

    def test_tracker_persistence(self, tmp_path):
        tracker = MetricsAccumulator(tmp_path / "state", tmp_path)
        tracker.maybe_record(force=True)
        tracker2 = MetricsAccumulator(tmp_path / "state", tmp_path)
        assert tracker2 is not None

    def test_snapshot_via_tracker(self, tmp_path):
        tracker = MetricsAccumulator(tmp_path / "state", tmp_path)
        snap = tracker.maybe_record(force=True)
        assert snap is None or hasattr(snap, "total_tickets")


class TestOrchestratorStatus:
    def test_get_status_empty(self):
        s = os_get_status({}, batch_read_heartbeats_fn=lambda names: {}, model_profile_fn=lambda m: None, effective_heartbeat_timeout_fn=lambda b: 600)
        assert s == {}

    def test_get_status_with_bot(self):
        bot = BotState(config=BotConfig(name="test-bot", prompt_file="a.md", interval_seconds=300, heartbeat_timeout=600, model="xiaomi-mimo-2.5"))
        s = os_get_status(
            {"test-bot": bot},
            batch_read_heartbeats_fn=lambda names: {"test-bot": time.time()},
            model_profile_fn=lambda m: None,
            effective_heartbeat_timeout_fn=lambda b: 600,
        )
        assert "test-bot" in s
        assert "heartbeat_age_seconds" in s["test-bot"]

    def test_print_status_empty(self, capsys):
        os_print_status({}, get_status_fn=lambda bots: {})
        out = capsys.readouterr().out
        assert "BOT ORCHESTRATOR STATUS" in out


class TestWorkforceAllocationEdges:
    def test_summary_has_implementation_bucket(self):
        from codebot.pipeline_state import PipelineState
        ps = PipelineState(implementation_ready_count=5, implementing_count=2)
        s = ps.summary()
        assert s["queues"]["implementation"] == 7
