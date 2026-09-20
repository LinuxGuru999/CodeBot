"""Tests for the adversarial review and gatekeeping framework.

Covers:
- review_types: severity ordering, finding blocking, checklist, completion evidence
- review_config: loading, defaults, risk-based routing
- gatekeeper: blocking findings prevent COMPLETE, evidence requirements
"""

import json
import tempfile
from pathlib import Path

import pytest

from codebot.review_types import (
    FindingSeverity,
    ChecklistResult,
    ReviewPhase,
    GateDecision,
    RiskClass,
    StructuredFinding,
    ReviewChecklist,
    CompletionEvidence,
    ReviewDecision,
    DEFAULT_BLOCKING_SEVERITIES,
    severity_at_least,
    severity_rank,
    parse_severity,
    risk_class_for_score,
    highest_unresolved_severity,
    MANDATORY_CHECKLIST_ITEMS,
)
from codebot.review_config import ReviewConfig, load_review_config, reset_review_config


class TestFindingSeverity:
    def test_ordering(self):
        assert severity_rank(FindingSeverity.BLOCKER) < severity_rank(FindingSeverity.CRITICAL)
        assert severity_rank(FindingSeverity.CRITICAL) < severity_rank(FindingSeverity.MAJOR)
        assert severity_rank(FindingSeverity.MAJOR) < severity_rank(FindingSeverity.MINOR)
        assert severity_rank(FindingSeverity.MINOR) < severity_rank(FindingSeverity.NIT)
        assert severity_rank(FindingSeverity.NIT) < severity_rank(FindingSeverity.INFO)

    def test_severity_at_least(self):
        assert severity_at_least(FindingSeverity.BLOCKER, FindingSeverity.MAJOR)
        assert severity_at_least(FindingSeverity.CRITICAL, FindingSeverity.MAJOR)
        assert severity_at_least(FindingSeverity.MAJOR, FindingSeverity.MAJOR)
        assert not severity_at_least(FindingSeverity.MINOR, FindingSeverity.MAJOR)

    def test_parse_severity(self):
        assert parse_severity("BLOCKER") == FindingSeverity.BLOCKER
        assert parse_severity("blocker") == FindingSeverity.BLOCKER
        assert parse_severity("high") == FindingSeverity.MAJOR
        assert parse_severity("medium") == FindingSeverity.MINOR
        assert parse_severity("low") == FindingSeverity.NIT
        assert parse_severity("") == FindingSeverity.INFO
        assert parse_severity("garbage") == FindingSeverity.INFO


class TestStructuredFinding:
    def test_blocking_finding(self):
        f = StructuredFinding(
            severity=FindingSeverity.BLOCKER,
            category="security",
            finding="SQL injection",
            file="api.py",
            location="search() line 42",
            evidence="User input interpolated",
            reproduction="Send payload: ' OR 1=1 --",
            expected="Parameterized query",
            actual="Raw SQL concatenation",
            recommended_fix="Use cursor.execute(sql, params)",
        )
        assert f.is_blocking()
        assert f.is_blocking(DEFAULT_BLOCKING_SEVERITIES)
        assert not f.is_blocking(frozenset({FindingSeverity.BLOCKER})) is False

    def test_resolved_finding_not_blocking(self):
        f = StructuredFinding(
            severity=FindingSeverity.CRITICAL,
            category="test",
            finding="test",
            resolved=True,
        )
        assert not f.is_blocking()

    def test_minor_not_blocking(self):
        f = StructuredFinding(
            severity=FindingSeverity.MINOR,
            category="style",
            finding="naming",
        )
        assert not f.is_blocking()

    def test_serialization_roundtrip(self):
        f = StructuredFinding(
            severity=FindingSeverity.MAJOR,
            category="correctness",
            finding="Off-by-one",
            file="pagination.py",
        )
        d = f.to_dict()
        f2 = StructuredFinding.from_dict(d)
        assert f2.severity == FindingSeverity.MAJOR
        assert f2.finding == "Off-by-one"
        assert f2.file == "pagination.py"


class TestReviewChecklist:
    def test_unknown_not_pass(self):
        cl = ReviewChecklist()
        assert cl.result_for("requirement_satisfied") == ChecklistResult.UNKNOWN
        assert not cl.is_complete()

    def test_complete_when_all_filled(self):
        cl = ReviewChecklist()
        for item in MANDATORY_CHECKLIST_ITEMS:
            cl.set(item, ChecklistResult.PASS)
        assert cl.is_complete()

    def test_failed_items(self):
        cl = ReviewChecklist()
        cl.set("requirement_satisfied", ChecklistResult.FAIL, "Not met")
        cl.set("acceptance_criteria_satisfied", ChecklistResult.PASS)
        assert cl.failed_items() == ["requirement_satisfied"]

    def test_unresolved_unknowns(self):
        cl = ReviewChecklist()
        cl.set("requirement_satisfied", ChecklistResult.PASS)
        cl.set("error_paths_tested", ChecklistResult.UNKNOWN)
        assert cl.unresolved_unknowns() == ["error_paths_tested"]

    def test_serialization_roundtrip(self):
        cl = ReviewChecklist()
        cl.set("requirement_satisfied", ChecklistResult.PASS, "Verified")
        cl.set("security_implications_considered", ChecklistResult.FAIL, "Not checked")
        d = cl.to_dict()
        cl2 = ReviewChecklist.from_dict(d)
        assert cl2.result_for("requirement_satisfied") == ChecklistResult.PASS
        assert cl2.result_for("security_implications_considered") == ChecklistResult.FAIL


class TestCompletionEvidence:
    def test_missing_evidence_when_requirement_not_verified(self):
        ev = CompletionEvidence(requirement_verified=False)
        assert ev.has_missing_evidence()

    def test_missing_evidence_when_tests_fail(self):
        ev = CompletionEvidence(
            requirement_verified=True,
            tests_failed=1,
            build_verified=True,
            checklist_complete=True,
        )
        assert ev.has_missing_evidence()

    def test_no_missing_evidence_when_all_good(self):
        ev = CompletionEvidence(
            requirement_verified=True,
            acceptance_passed=5,
            tests_passed=10,
            build_verified=True,
            checklist_complete=True,
        )
        assert not ev.has_missing_evidence()

    def test_blocking_findings_count(self):
        ev = CompletionEvidence(
            finding_counts={"BLOCKER": 1, "MAJOR": 2, "MINOR": 3},
        )
        assert ev.unresolved_blocking_findings() == 3


class TestReviewDecision:
    def test_effective_verdict_overrides_approve_with_blocking(self):
        f = StructuredFinding(
            severity=FindingSeverity.BLOCKER,
            category="test",
            finding="test",
        )
        rd = ReviewDecision(
            verdict="APPROVE",
            phase=ReviewPhase.INDEPENDENT_REVIEW,
            reviewer="test_reviewer",
            findings=[f],
        )
        assert rd.effective_verdict() == "REWORK"

    def test_effective_verdict_passes_clean_approve(self):
        rd = ReviewDecision(
            verdict="APPROVE",
            phase=ReviewPhase.INDEPENDENT_REVIEW,
            reviewer="test_reviewer",
        )
        assert rd.effective_verdict() == "APPROVE"

    def test_serialization_roundtrip(self):
        f = StructuredFinding(
            severity=FindingSeverity.MAJOR,
            category="test",
            finding="test finding",
        )
        rd = ReviewDecision(
            verdict="REWORK",
            phase=ReviewPhase.ADVERSARIAL_REVIEW,
            reviewer="security_reviewer",
            ticket_id="CB-TEST",
            findings=[f],
            summary="Found issues",
        )
        d = rd.to_dict()
        rd2 = ReviewDecision.from_dict(d)
        assert rd2.verdict == "REWORK"
        assert rd2.phase == ReviewPhase.ADVERSARIAL_REVIEW
        assert rd2.reviewer == "security_reviewer"
        assert len(rd2.findings) == 1
        assert rd2.findings[0].severity == FindingSeverity.MAJOR


class TestRiskClass:
    def test_risk_class_for_score(self):
        assert risk_class_for_score(80) == RiskClass.CRITICAL
        assert risk_class_for_score(50) == RiskClass.HIGH
        assert risk_class_for_score(30) == RiskClass.MEDIUM
        assert risk_class_for_score(10) == RiskClass.LOW


class TestReviewConfig:
    def test_defaults(self):
        cfg = ReviewConfig()
        assert FindingSeverity.BLOCKER in cfg.blocking_severities
        assert FindingSeverity.CRITICAL in cfg.blocking_severities
        assert FindingSeverity.MAJOR in cfg.blocking_severities
        assert FindingSeverity.MINOR not in cfg.blocking_severities
        assert cfg.blind_review_enabled is True
        assert cfg.adversarial_review_enabled is True
        assert cfg.reviewers_for_risk(RiskClass.LOW) == 1
        assert cfg.reviewers_for_risk(RiskClass.HIGH) == 2
        assert cfg.reviewers_for_risk(RiskClass.CRITICAL) == 2

    def test_specialized_reviewers(self):
        cfg = ReviewConfig()
        assert cfg.specialized_reviewers_for_risk(RiskClass.LOW) == []
        assert "security_reviewer" in cfg.specialized_reviewers_for_risk(RiskClass.HIGH)
        assert "security_reviewer" in cfg.specialized_reviewers_for_risk(RiskClass.CRITICAL)
        assert "architecture_reviewer" in cfg.specialized_reviewers_for_risk(RiskClass.CRITICAL)

    def test_trivial_path_detection(self):
        cfg = ReviewConfig()
        assert cfg.is_trivial_path_set(["docs/foo.md"])
        assert cfg.is_trivial_path_set(["README.md", "CHANGELOG.md"])
        assert not cfg.is_trivial_path_set(["codebot/x.py"])
        assert not cfg.is_trivial_path_set(["docs/foo.md", "codebot/x.py"])

    def test_load_from_yaml(self, tmp_path):
        yaml_content = """
review:
  blind_review_enabled: false
  adversarial_review_enabled: true
reviewers_per_risk:
  LOW: 1
  HIGH: 3
  CRITICAL: 4
gatekeeper:
  blocking_severities: BLOCKER,CRITICAL
rework:
  max_cycles: 5
"""
        yaml_file = tmp_path / "review.yaml"
        yaml_file.write_text(yaml_content)
        reset_review_config()
        cfg = load_review_config(yaml_file)
        assert cfg.blind_review_enabled is False
        assert cfg.adversarial_review_enabled is True
        assert cfg.reviewers_for_risk(RiskClass.HIGH) == 3
        assert cfg.reviewers_for_risk(RiskClass.CRITICAL) == 4
        assert FindingSeverity.MAJOR not in cfg.blocking_severities
        assert cfg.max_rework_cycles == 5
        reset_review_config()


class TestHighestUnresolvedSeverity:
    def test_returns_none_when_clean(self):
        assert highest_unresolved_severity([]) is None

    def test_returns_most_severe(self):
        findings = [
            StructuredFinding(severity=FindingSeverity.MINOR, category="t", finding="t"),
            StructuredFinding(severity=FindingSeverity.CRITICAL, category="t", finding="t"),
            StructuredFinding(severity=FindingSeverity.MAJOR, category="t", finding="t"),
        ]
        assert highest_unresolved_severity(findings) == FindingSeverity.CRITICAL

    def test_skips_resolved(self):
        findings = [
            StructuredFinding(severity=FindingSeverity.BLOCKER, category="t", finding="t", resolved=True),
            StructuredFinding(severity=FindingSeverity.MINOR, category="t", finding="t"),
        ]
        assert highest_unresolved_severity(findings) == FindingSeverity.MINOR
