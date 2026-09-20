"""Tests for quality_gate.py."""
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.quality_gate import QualityGatePolicy, GateResult, load_policy, evaluate_gate, run_quality_gates, record_gate_results

class TestQualityGatePolicy:
    def test_default_has_required(self):
        p = QualityGatePolicy.default()
        names = [g["name"] for g in p.required]
        assert "build" in names
        assert "unit_tests" in names

    def test_from_dict(self):
        p = QualityGatePolicy.from_dict({"required": [{"name": "x", "command": "echo ok"}], "conditional": {}})
        assert len(p.required) == 1

class TestLoadPolicy:
    def test_missing_returns_default(self, tmp_path):
        p = load_policy(tmp_path / "nope.yaml")
        assert len(p.required) >= 1

class TestEvaluateGate:
    def test_passing(self, tmp_path):
        ev = evaluate_gate({"name": "ok", "command": "echo pass"}, tmp_path)
        assert ev.result == GateResult.PASS

    def test_failing(self, tmp_path):
        ev = evaluate_gate({"name": "bad", "command": "false"}, tmp_path)
        assert ev.result == GateResult.FAIL

    def test_timeout(self, tmp_path):
        ev = evaluate_gate({"name": "slow", "command": "sleep 10"}, tmp_path, timeout=1)
        assert ev.result == GateResult.ERROR

    def test_quoted_file_context_is_one_argument(self, tmp_path):
        ev = evaluate_gate(
            {"name": "quoted", "command": "echo '{file}'"},
            tmp_path,
            file_context="tests/path with spaces.py",
        )
        assert ev.passed is True
        assert ev.output.strip() == "tests/path with spaces.py"

class TestRunQualityGates:
    def test_all_pass(self, tmp_path):
        policy = QualityGatePolicy(required=[{"name": "ok", "command": "echo pass"}], conditional={})
        passed, evals = run_quality_gates(policy, tmp_path)
        assert passed is True

    def test_fail_blocks(self, tmp_path):
        policy = QualityGatePolicy(required=[{"name": "bad", "command": "false"}], conditional={})
        passed, _ = run_quality_gates(policy, tmp_path)
        assert passed is False

    def test_conditional_by_class(self, tmp_path):
        policy = QualityGatePolicy(required=[{"name": "ok", "command": "echo pass"}], conditional={"security_boundary": [{"name": "sec", "command": "echo sec"}]})
        _, evals = run_quality_gates(policy, tmp_path, ticket_class="security")
        assert any(e.gate_name == "sec" for e in evals)

class TestRecordGateResults:
    def test_appends_jsonl(self, tmp_path):
        from codebot.quality_gate import GateEvaluation
        evals = [GateEvaluation("t", GateResult.PASS, "echo", "ok", 0.1, True)]
        record_gate_results(tmp_path, "CB-1", True, evals)
        lines = (tmp_path / "gate_results.jsonl").read_text().strip().split("\n")
        assert json.loads(lines[0])["ticket_id"] == "CB-1"
