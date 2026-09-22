"""Tests for codebot/discovery_finding.py — structured finding schema."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codebot.discovery_finding import (
    DiscoveryFinding,
    EvidenceItem,
    DuplicateCandidate,
    FindingRelationship,
    FindingValidationError,
    Confidence,
    Atomicity,
    ScopeEstimate,
    DuplicateRelation,
    FindingRelation,
    FINDING_CATEGORIES,
    SEVERITY_VALUES,
    CONCRETE_EVIDENCE_KINDS,
    EVIDENCE_KINDS,
    generate_finding_id,
    normalize_text,
    finding_from_agent_output,
    _repair_raw,
)


# ---------------------------------------------------------------------------
# EvidenceItem tests
# ---------------------------------------------------------------------------

class TestEvidenceItem:
    def test_minimal_creation(self):
        e = EvidenceItem(observation="resp.read() has no cap")
        assert e.observation == "resp.read() has no cap"
        assert e.kind == "code_reference"
        assert e.line_number == 0

    def test_full_creation(self):
        e = EvidenceItem(
            observation="O(n²) nested loop",
            interpretation="Dispatch complexity grows quadratically",
            impact="Dispatch latency may exceed acceptable bounds",
            file_path="codebot/orchestrator.py",
            line_number=950,
            symbol="check_heartbeats",
            excerpt="for agent in agents:",
            kind="code_reference",
        )
        assert e.file_path == "codebot/orchestrator.py"
        assert e.line_number == 950

    def test_has_concrete_proof_true(self):
        e = EvidenceItem(observation="test fails", kind="failing_test")
        assert e.has_concrete_proof() is True

    def test_has_concrete_proof_false(self):
        e = EvidenceItem(observation="looks suspicious", kind="code_reference")
        assert e.has_concrete_proof() is False

    def test_is_valid_path_relative(self):
        e = EvidenceItem(observation="test", file_path="codebot/foo.py")
        assert e.is_valid_path() is True

    def test_is_valid_path_absolute_rejected(self):
        e = EvidenceItem(observation="test", file_path="/etc/passwd")
        assert e.is_valid_path() is False

    def test_is_valid_path_traversal_rejected(self):
        e = EvidenceItem(observation="test", file_path="../../etc/passwd")
        assert e.is_valid_path() is False

    def test_is_valid_path_empty_is_ok(self):
        e = EvidenceItem(observation="test", file_path="")
        assert e.is_valid_path() is True

    def test_to_dict_roundtrip(self):
        e = EvidenceItem(
            observation="test", file_path="foo.py", line_number=10, kind="static_analysis"
        )
        d = e.to_dict()
        e2 = EvidenceItem.from_dict(d)
        assert e2.observation == e.observation
        assert e2.file_path == e.file_path
        assert e2.line_number == e.line_number

    def test_from_dict_invalid_kind_defaults(self):
        e = EvidenceItem.from_dict({"observation": "test", "kind": "nonsense_kind"})
        assert e.kind == "code_reference"

    def test_from_dict_invalid_line_defaults(self):
        e = EvidenceItem.from_dict({"observation": "test", "line_number": "not_a_number"})
        assert e.line_number == 0

    def test_excerpt_truncated(self):
        long_excerpt = "x" * 3000
        e = EvidenceItem(observation="test", excerpt=long_excerpt)
        assert len(e.excerpt) == 2000


# ---------------------------------------------------------------------------
# DuplicateCandidate tests
# ---------------------------------------------------------------------------

class TestDuplicateCandidate:
    def test_creation(self):
        dc = DuplicateCandidate(target_id="CB-ABC123", relation="exact_duplicate")
        assert dc.target_id == "CB-ABC123"

    def test_invalid_relation_defaults(self):
        dc = DuplicateCandidate.from_dict({"target_id": "CB-X", "relation": "nonsense"})
        assert dc.relation == DuplicateRelation.RELATED.value

    def test_roundtrip(self):
        dc = DuplicateCandidate(target_id="CB-X", relation="overlapping", note="similar")
        d = dc.to_dict()
        dc2 = DuplicateCandidate.from_dict(d)
        assert dc2.target_id == dc.target_id
        assert dc2.relation == dc.relation


# ---------------------------------------------------------------------------
# FindingRelationship tests
# ---------------------------------------------------------------------------

class TestFindingRelationship:
    def test_creation(self):
        fr = FindingRelationship(relation="possibly_caused_by", target_id="DF-X")
        assert fr.relation == "possibly_caused_by"

    def test_invalid_relation_defaults(self):
        fr = FindingRelationship.from_dict({"relation": "bogus", "target_id": "X"})
        assert fr.relation == FindingRelation.RELATED_TO.value


# ---------------------------------------------------------------------------
# DiscoveryFinding tests
# ---------------------------------------------------------------------------

class TestDiscoveryFinding:
    def _make_finding(self, **overrides):
        defaults = dict(
            finding_id="DF-TEST001",
            discovery_role="bug_hunter",
            discovery_category="bug",
            title="Unbounded read in web_fetch",
            problem_statement="web_fetch calls resp.read() without limit",
            severity="high",
            priority="high",
            confidence="high",
            atomicity="atomic",
            repository="codebot",
            repository_revision="abc123",
            evidence=[EvidenceItem(observation="resp.read() no cap", file_path="codebot/web_tools.py", line_number=87)],
            acceptance_outcome="resp.read() capped at 1MB",
        )
        defaults.update(overrides)
        return DiscoveryFinding(**defaults)

    def test_valid_finding(self):
        f = self._make_finding()
        f.validate()

    def test_missing_title_raises(self):
        with pytest.raises(FindingValidationError, match="title"):
            self._make_finding(title="").validate()

    def test_long_title_raises(self):
        with pytest.raises(FindingValidationError, match="title exceeds"):
            self._make_finding(title="x" * 201).validate()

    def test_missing_problem_statement_raises(self):
        with pytest.raises(FindingValidationError, match="problem_statement"):
            self._make_finding(problem_statement="").validate()

    def test_missing_role_raises(self):
        with pytest.raises(FindingValidationError, match="discovery_role"):
            self._make_finding(discovery_role="").validate()

    def test_invalid_category_raises(self):
        with pytest.raises(FindingValidationError, match="discovery_category"):
            self._make_finding(discovery_category="nonsense").validate()

    def test_invalid_severity_raises(self):
        with pytest.raises(FindingValidationError, match="severity"):
            self._make_finding(severity="catastrophic").validate()

    def test_invalid_confidence_raises(self):
        with pytest.raises(FindingValidationError, match="confidence"):
            self._make_finding(confidence="absolute").validate()

    def test_invalid_atomicity_raises(self):
        with pytest.raises(FindingValidationError, match="atomicity"):
            self._make_finding(atomicity="fragmented").validate()

    def test_empty_evidence_raises(self):
        with pytest.raises(FindingValidationError, match="evidence"):
            self._make_finding(evidence=[]).validate()

    def test_evidence_without_observation_raises(self):
        with pytest.raises(FindingValidationError, match="observation"):
            self._make_finding(evidence=[EvidenceItem(observation="")]).validate()

    def test_evidence_with_excerpt_but_no_observation_ok(self):
        f = self._make_finding(
            evidence=[EvidenceItem(observation="", excerpt="some code excerpt here that is long enough")]
        )
        f.validate()

    def test_unsafe_path_in_evidence_raises(self):
        with pytest.raises(FindingValidationError, match="unsafe"):
            self._make_finding(
                evidence=[EvidenceItem(observation="test", file_path="/etc/passwd")]
            ).validate()

    def test_missing_acceptance_outcome_raises(self):
        with pytest.raises(FindingValidationError, match="acceptance_outcome"):
            self._make_finding(acceptance_outcome="").validate()

    def test_fingerprint_deterministic(self):
        f1 = self._make_finding()
        f2 = self._make_finding()
        assert f1.fingerprint() == f2.fingerprint()

    def test_fingerprint_differs_for_different_problems(self):
        f1 = self._make_finding(problem_statement="unbounded read allows memory exhaustion")
        f2 = self._make_finding(problem_statement="race condition causes double dispatch")
        assert f1.fingerprint() != f2.fingerprint()

    def test_fingerprint_same_problem_different_wording(self):
        f1 = self._make_finding(problem_statement="unbounded read allows memory exhaustion")
        f2 = self._make_finding(problem_statement="memory exhaustion via unbounded read")
        assert f1.fingerprint() == f2.fingerprint()

    def test_confidence_is_supported_high_needs_concrete(self):
        f = self._make_finding(
            confidence="high",
            evidence=[EvidenceItem(observation="code", kind="code_reference")],
        )
        assert f.confidence_is_supported() is False

    def test_confidence_is_supported_high_with_concrete(self):
        f = self._make_finding(
            confidence="high",
            evidence=[EvidenceItem(observation="test fails", kind="failing_test")],
        )
        assert f.confidence_is_supported() is True

    def test_confidence_medium_no_concrete_ok(self):
        f = self._make_finding(
            confidence="medium",
            evidence=[EvidenceItem(observation="code", kind="code_reference")],
        )
        assert f.confidence_is_supported() is True

    def test_to_json_roundtrip(self):
        f = self._make_finding()
        j = f.to_json()
        f2 = DiscoveryFinding.from_json(j)
        assert f2.finding_id == f.finding_id
        assert f2.title == f.title
        assert f2.fingerprint() == f.fingerprint()

    def test_to_dict_has_fingerprint(self):
        f = self._make_finding()
        d = f.to_dict()
        assert "fingerprint" in d
        assert d["fingerprint"] == f.fingerprint()

    def test_from_dict_with_string_lists(self):
        data = {
            "finding_id": "DF-X",
            "discovery_role": "bug_hunter",
            "discovery_category": "bug",
            "title": "Test",
            "problem_statement": "problem",
            "severity": "high",
            "priority": "high",
            "confidence": "medium",
            "atomicity": "atomic",
            "repository": "test",
            "repository_revision": "abc",
            "evidence": [{"observation": "code"}],
            "acceptance_outcome": "fixed",
            "affected_files": "foo.py, bar.py",
            "affected_symbols": "func_a, func_b",
        }
        f = DiscoveryFinding.from_dict(data)
        assert f.affected_files == ["foo.py", "bar.py"]
        assert f.affected_symbols == ["func_a", "func_b"]


# ---------------------------------------------------------------------------
# finding_from_agent_output tests
# ---------------------------------------------------------------------------

class TestFindingFromAgentOutput:
    def test_valid_agent_output(self):
        data = {
            "title": "Bug found",
            "ticket_class": "bug",
            "severity": "high",
            "evidence": "codebot/foo.py:10 - bad code",
            "problem_statement": "something wrong",
            "desired_state": "fixed",
            "acceptance_criteria": "test passes",
        }
        f = finding_from_agent_output(data, role="bug_hunter", repository="test")
        assert f.discovery_role == "bug_hunter"
        assert f.discovery_category == "bug"
        assert len(f.evidence) == 1

    def test_repairs_case(self):
        data = {
            "title": "Bug",
            "ticket_class": "BUG",
            "severity": "HIGH",
            "confidence": "HIGH",
            "evidence": "file.py:1",
            "problem_statement": "problem",
            "desired_state": "fixed",
            "acceptance_outcome": "fixed",
        }
        f = finding_from_agent_output(data, role="bug_hunter")
        assert f.severity == "high"
        assert f.confidence == "high"

    def test_repairs_missing_confidence(self):
        data = {
            "title": "Bug",
            "ticket_class": "bug",
            "severity": "high",
            "evidence": "file.py:1",
            "problem_statement": "problem",
            "desired_state": "fixed",
            "acceptance_outcome": "fixed",
        }
        f = finding_from_agent_output(data, role="bug_hunter")
        assert f.confidence == "medium"

    def test_repairs_string_evidence(self):
        data = {
            "title": "Bug",
            "ticket_class": "bug",
            "severity": "high",
            "evidence": "file.py:10 - bad code",
            "problem_statement": "problem",
            "desired_state": "fixed",
            "acceptance_outcome": "fixed",
        }
        f = finding_from_agent_output(data, role="bug_hunter")
        assert len(f.evidence) == 1
        assert f.evidence[0].observation == "file.py:10 - bad code"

    def test_repairs_desired_state_to_acceptance(self):
        data = {
            "title": "Bug",
            "ticket_class": "bug",
            "severity": "high",
            "evidence": "file.py:1",
            "problem_statement": "problem",
            "desired_state": "fixed behavior",
        }
        f = finding_from_agent_output(data, role="bug_hunter")
        assert f.acceptance_outcome == "fixed behavior"

    def test_rejects_non_dict(self):
        with pytest.raises(FindingValidationError, match="JSON object"):
            finding_from_agent_output("not a dict", role="bug_hunter")

    def test_rejects_empty_title(self):
        data = {
            "title": "",
            "ticket_class": "bug",
            "severity": "high",
            "evidence": "file.py:1",
            "problem_statement": "problem",
            "desired_state": "fixed",
            "acceptance_outcome": "fixed",
        }
        with pytest.raises(FindingValidationError):
            finding_from_agent_output(data, role="bug_hunter")

    def test_truncates_long_title(self):
        data = {
            "title": "x" * 300,
            "ticket_class": "bug",
            "severity": "high",
            "evidence": "file.py:1",
            "problem_statement": "problem",
            "desired_state": "fixed",
            "acceptance_outcome": "fixed",
        }
        f = finding_from_agent_output(data, role="bug_hunter")
        assert len(f.title) <= 200

    def test_sets_role_and_repo(self):
        data = {
            "title": "Bug",
            "ticket_class": "bug",
            "severity": "high",
            "evidence": "file.py:1",
            "problem_statement": "problem",
            "desired_state": "fixed",
            "acceptance_outcome": "fixed",
        }
        f = finding_from_agent_output(
            data, role="security_auditor", repository="myproject", repository_revision="deadbeef"
        )
        assert f.discovery_role == "security_auditor"
        assert f.repository == "myproject"
        assert f.repository_revision == "deadbeef"


# ---------------------------------------------------------------------------
# normalize_text tests
# ---------------------------------------------------------------------------

class TestNormalizeText:
    def test_basic(self):
        result = normalize_text("The quick brown fox")
        assert "quick" in result
        assert "brown" in result
        assert "the" not in result  # stopword

    def test_empty(self):
        result = normalize_text("")
        assert result == frozenset()

    def test_short_words_filtered(self):
        result = normalize_text("a b cc dd")
        assert "cc" in result
        assert "dd" in result
        assert "a" not in result
        assert "b" not in result


# ---------------------------------------------------------------------------
# Enum completeness tests
# ---------------------------------------------------------------------------

class TestEnums:
    def test_confidence_values(self):
        assert {c.value for c in Confidence} == {"high", "medium", "low"}

    def test_atomicity_values(self):
        assert {a.value for a in Atomicity} == {"atomic", "compound", "unknown"}

    def test_finding_categories(self):
        assert "bug" in FINDING_CATEGORIES
        assert "security" in FINDING_CATEGORIES
        assert "architecture" in FINDING_CATEGORIES
        assert len(FINDING_CATEGORIES) == 10

    def test_severity_values(self):
        assert SEVERITY_VALUES == {"critical", "high", "medium", "low"}

    def test_concrete_evidence_kinds_subset(self):
        assert CONCRETE_EVIDENCE_KINDS.issubset(EVIDENCE_KINDS)

    def test_generate_finding_id_format(self):
        fid = generate_finding_id()
        assert fid.startswith("DF-")
        assert len(fid) == 15

    def test_generate_finding_id_unique(self):
        ids = {generate_finding_id() for _ in range(100)}
        assert len(ids) == 100
