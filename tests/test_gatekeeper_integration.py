"""Integration and synthetic scenario tests for gatekeeper and quality gates.

Resolves ticket CB-1273980-58BD:
- Integration tests verify gatekeeper blocks COMPLETE on gate failure
- Synthetic tickets trigger each conditional gate type
- Test suite covers build/test/lint/type failures
- Security boundary tickets require security_review gate
- Data migration tickets require migration_test and rollback_test
"""
import json
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.gatekeeper import Gatekeeper
from codebot.quality_gate import (
    QualityGatePolicy,
    GateResult,
    run_quality_gates,
    evaluate_gate,
)


class TestGatekeeperBlockingIntegration:
    """Integration tests verifying gatekeeper blocks COMPLETE on gate failure."""

    def _make_policy(self, tmp_path, required_cmds, conditional=None):
        p = tmp_path / "gates.yaml"
        lines = ["required:"]
        for i, cmd in enumerate(required_cmds):
            lines.append(f"  - name: req_{i}\n    command: {cmd}")
        if conditional:
            lines.append("conditional:")
            for cond, gates in conditional.items():
                lines.append(f"  {cond}:")
                for g in gates:
                    lines.append(f"    - name: {g['name']}\n      command: {g['command']}")
        p.write_text("\n".join(lines))
        return p

    def test_blocks_complete_when_build_fails(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        policy = self._make_policy(tmp_path, ["false"])  # build fails
        gk = Gatekeeper(tmp_path / "state", policy_path=policy, workspace=ws)
        r = gk.verify_ticket("CB-BUILD-FAIL", "bug", ["f.py"])
        assert r["decision"] == "REWORK"
        assert r["passed"] is False
        assert "req_0" in r["failed_gates"]

    def test_blocks_complete_when_tests_fail(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        # First passes, second fails
        policy = self._make_policy(tmp_path, ["echo ok", "false"])
        gk = Gatekeeper(tmp_path / "state", policy_path=policy, workspace=ws)
        r = gk.verify_ticket("CB-TEST-FAIL", "bug", ["f.py"])
        assert r["decision"] == "REWORK"
        assert r["passed"] is False

    def test_allows_complete_when_all_pass(self, tmp_path):
        ws = tmp_path / "ws"; ws.mkdir()
        policy = self._make_policy(tmp_path, ["echo ok", "echo ok"])
        gk = Gatekeeper(tmp_path / "state", policy_path=policy, workspace=ws)
        r = gk.verify_ticket("CB-ALL-PASS", "bug", ["f.py"])
        assert r["decision"] == "COMPLETE"
        assert r["passed"] is True
        assert r["failed_gates"] == []


class TestSyntheticConditionalGates:
    """Synthetic tickets trigger each conditional gate type."""

    def test_security_boundary_triggers_security_review(self, tmp_path):
        policy = QualityGatePolicy(
            required=[{"name": "build", "command": "echo ok"}],
            conditional={
                "security_boundary": [
                    {"name": "security_review", "command": "echo sec_pass"}
                ]
            }
        )
        passed, evals = run_quality_gates(policy, tmp_path, ticket_class="security")
        assert passed is True
        gate_names = [e.gate_name for e in evals]
        assert "security_review" in gate_names

    def test_api_change_triggers_contract_tests(self, tmp_path):
        policy = QualityGatePolicy(
            required=[{"name": "build", "command": "echo ok"}],
            conditional={
                "api_change": [
                    {"name": "contract_tests", "command": "echo api_pass"}
                ]
            }
        )
        # "api" in filename triggers api_change
        passed, evals = run_quality_gates(
            policy, tmp_path, changed_files=["codebot/api_runner.py"]
        )
        assert passed is True
        gate_names = [e.gate_name for e in evals]
        assert "contract_tests" in gate_names

    def test_router_change_triggers_contract_tests(self, tmp_path):
        policy = QualityGatePolicy(
            required=[],
            conditional={
                "api_change": [
                    {"name": "contract_tests", "command": "echo api_pass"}
                ]
            }
        )
        passed, evals = run_quality_gates(
            policy, tmp_path, changed_files=["codebot/router.py"]
        )
        gate_names = [e.gate_name for e in evals]
        assert "contract_tests" in gate_names

    def test_data_migration_triggers_migration_and_rollback(self, tmp_path):
        policy = QualityGatePolicy(
            required=[],
            conditional={
                "data_migration": [
                    {"name": "migration_test", "command": "echo mig_pass"},
                    {"name": "rollback_test", "command": "echo roll_pass"}
                ]
            }
        )
        # "migration" in filename
        passed, evals = run_quality_gates(
            policy, tmp_path, changed_files=["codebot/migration_engine.py"]
        )
        gate_names = [e.gate_name for e in evals]
        assert "migration_test" in gate_names
        assert "rollback_test" in gate_names

    def test_store_change_triggers_data_migration(self, tmp_path):
        policy = QualityGatePolicy(
            required=[],
            conditional={
                "data_migration": [
                    {"name": "migration_test", "command": "echo mig_pass"},
                    {"name": "rollback_test", "command": "echo roll_pass"}
                ]
            }
        )
        passed, evals = run_quality_gates(
            policy, tmp_path, changed_files=["codebot/ticket_store.py"]
        )
        gate_names = [e.gate_name for e in evals]
        assert "migration_test" in gate_names
        assert "rollback_test" in gate_names

    def test_performance_sensitive_triggers_benchmark(self, tmp_path):
        policy = QualityGatePolicy(
            required=[],
            conditional={
                "performance_sensitive": [
                    {"name": "benchmark", "command": "echo bench_pass"}
                ]
            }
        )
        passed, evals = run_quality_gates(
            policy, tmp_path, conditions=["performance_sensitive"]
        )
        gate_names = [e.gate_name for e in evals]
        assert "benchmark" in gate_names

    def test_documentation_impact_triggers_doc_review(self, tmp_path):
        policy = QualityGatePolicy(
            required=[],
            conditional={
                "documentation_impact": [
                    {"name": "documentation_review", "command": "echo doc_pass"}
                ]
            }
        )
        passed, evals = run_quality_gates(
            policy, tmp_path, conditions=["documentation_impact"]
        )
        gate_names = [e.gate_name for e in evals]
        assert "documentation_review" in gate_names


class TestSyntheticGateFailures:
    """Test suite covers build/test/lint/type failures via synthetic commands."""

    def test_build_failure_blocks(self, tmp_path):
        ev = evaluate_gate({"name": "build", "command": "false"}, tmp_path)
        assert ev.result == GateResult.FAIL
        assert ev.required is True

    def test_test_failure_blocks(self, tmp_path):
        # Simulate pytest failure
        ev = evaluate_gate({"name": "unit_tests", "command": "false"}, tmp_path)
        assert ev.result == GateResult.FAIL

    def test_lint_failure_blocks(self, tmp_path):
        ev = evaluate_gate({"name": "lint", "command": "false"}, tmp_path)
        assert ev.result == GateResult.FAIL

    def test_type_failure_blocks(self, tmp_path):
        ev = evaluate_gate({"name": "type_check", "command": "false"}, tmp_path)
        assert ev.result == GateResult.FAIL

    def test_conditional_failure_blocks_overall(self, tmp_path):
        policy = QualityGatePolicy(
            required=[{"name": "build", "command": "echo ok"}],
            conditional={
                "security_boundary": [
                    {"name": "security_review", "command": "false"}
                ]
            }
        )
        passed, evals = run_quality_gates(
            policy, tmp_path, ticket_class="security"
        )
        assert passed is False
        failed = [e for e in evals if e.result == GateResult.FAIL]
        assert len(failed) == 1
        assert failed[0].gate_name == "security_review"
