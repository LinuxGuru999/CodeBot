"""Tests for Task 5: extended quality-gate cache key binding."""

import json
import time

from codebot.quality_gate import (
    QualityGatePolicy,
    _policy_revision,
    _review_evidence_hash,
    _workspace_revision,
    run_quality_gates_with_cache,
)


def _simple_policy():
    return QualityGatePolicy(
        required=[{"name": "build", "command": "python3 -m py_compile {file}"}],
        conditional={},
    )


class TestPolicyRevision:
    def test_identical_policies_same_revision(self):
        p1 = _simple_policy()
        p2 = _simple_policy()
        assert _policy_revision(p1) == _policy_revision(p2)

    def test_different_command_different_revision(self):
        p1 = QualityGatePolicy(
            required=[{"name": "build", "command": "cmd1"}], conditional={},
        )
        p2 = QualityGatePolicy(
            required=[{"name": "build", "command": "cmd2"}], conditional={},
        )
        assert _policy_revision(p1) != _policy_revision(p2)

    def test_different_timeout_different_revision(self):
        p1 = QualityGatePolicy(
            required=[{"name": "build", "command": "cmd", "timeout": 60}], conditional={},
        )
        p2 = QualityGatePolicy(
            required=[{"name": "build", "command": "cmd", "timeout": 120}], conditional={},
        )
        assert _policy_revision(p1) != _policy_revision(p2)

    def test_added_conditional_different_revision(self):
        p1 = QualityGatePolicy(required=[], conditional={})
        p2 = QualityGatePolicy(
            required=[],
            conditional={"security_boundary": [{"name": "sec", "command": "echo ok"}]},
        )
        assert _policy_revision(p1) != _policy_revision(p2)


class TestWorkspaceRevision:
    def test_no_git_returns_mtime(self, tmp_path):
        rev = _workspace_revision(tmp_path)
        assert rev != ""
        assert rev != "unknown"

    def test_nonexistent_returns_no_git(self, tmp_path):
        rev = _workspace_revision(tmp_path / "nonexistent")
        assert rev == "no-git"


class TestReviewEvidenceHash:
    def test_no_review_files_empty_hash(self, tmp_path):
        h = _review_evidence_hash(tmp_path, "CB-1")
        assert isinstance(h, str)
        assert len(h) == 16

    def test_review_file_for_ticket_changes_hash(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        h1 = _review_evidence_hash(state, "CB-1")
        review = state / "correctness_review.json"
        review.write_text(json.dumps({"ticket_id": "CB-1", "verdict": "APPROVE"}))
        h2 = _review_evidence_hash(state, "CB-1")
        assert h1 != h2

    def test_review_for_different_ticket_no_change(self, tmp_path):
        state = tmp_path / "state"
        state.mkdir()
        review = state / "correctness_review.json"
        review.write_text(json.dumps({"ticket_id": "CB-OTHER", "verdict": "APPROVE"}))
        h1 = _review_evidence_hash(state, "CB-1")
        h_empty = _review_evidence_hash(state, "CB-NONEXISTENT")
        assert h1 == h_empty


class TestCacheDimensionBinding:
    def test_ticket_revision_change_invalidates_cache(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.py").write_text("x = 1\n")
        state = tmp_path / "state"
        state.mkdir()
        policy = _simple_policy()
        files = ["a.py"]
        passed1, _ = run_quality_gates_with_cache(
            policy, ws, state, "CB-REV1", changed_files=files, ticket_revision=100.0,
        )
        assert passed1 is True
        passed2, evals2 = run_quality_gates_with_cache(
            policy, ws, state, "CB-REV1", changed_files=files, ticket_revision=100.0,
        )
        assert passed2 is True
        assert evals2[0].gate_name == "cached-pass"
        passed3, evals3 = run_quality_gates_with_cache(
            policy, ws, state, "CB-REV1", changed_files=files, ticket_revision=200.0,
        )
        assert passed3 is True
        assert evals3[0].gate_name == "build"

    def test_policy_change_invalidates_cache(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.py").write_text("x = 1\n")
        state = tmp_path / "state"
        state.mkdir()
        p1 = _simple_policy()
        files = ["a.py"]
        passed1, _ = run_quality_gates_with_cache(
            p1, ws, state, "CB-POL1", changed_files=files,
        )
        assert passed1 is True
        passed2, evals2 = run_quality_gates_with_cache(
            p1, ws, state, "CB-POL1", changed_files=files,
        )
        assert evals2[0].gate_name == "cached-pass"
        p2 = QualityGatePolicy(
            required=[{"name": "build", "command": "python3 -c 'pass'"}],
            conditional={},
        )
        passed3, evals3 = run_quality_gates_with_cache(
            p2, ws, state, "CB-POL1", changed_files=files,
        )
        assert evals3[0].gate_name != "cached-pass"

    def test_conditions_change_invalidates_cache(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.py").write_text("x = 1\n")
        state = tmp_path / "state"
        state.mkdir()
        policy = QualityGatePolicy(
            required=[{"name": "build", "command": "python3 -m py_compile {file}"}],
            conditional={
                "api_change": [{"name": "contract", "command": "python3 -c 'pass'"}],
            },
        )
        files = ["a.py"]
        passed1, _ = run_quality_gates_with_cache(
            policy, ws, state, "CB-COND1", changed_files=files, conditions=[],
        )
        assert passed1 is True
        passed2, evals2 = run_quality_gates_with_cache(
            policy, ws, state, "CB-COND1", changed_files=files, conditions=[],
        )
        assert evals2[0].gate_name == "cached-pass"
        passed3, evals3 = run_quality_gates_with_cache(
            policy, ws, state, "CB-COND1", changed_files=files, conditions=["api_change"],
        )
        assert evals3[0].gate_name != "cached-pass"
