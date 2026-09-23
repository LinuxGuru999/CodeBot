#!/usr/bin/env python3
"""Integration tests for Gatekeeper blocking and synthetic scenarios.

Validates:
- Gatekeeper blocks COMPLETE on gate failure.
- Synthetic tickets trigger conditional gates.
- Build/test failures are correctly detected.
"""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Import the modules under test
from codebot.gatekeeper import Gatekeeper
from codebot.quality_gate import QualityGatePolicy, GateStatus, run_quality_gates, GateEvaluation
from codebot.ticket_engine import TicketStore, TicketState


class TestGatekeeperBlocking:
    """Tests verifying that Gatekeeper blocks completion when gates fail."""

    @pytest.fixture
    def temp_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def mock_ticket_store(self, temp_state_dir):
        store_path = temp_state_dir / "tickets.json"
        # Create a minimal ticket store with one ticket
        initial_data = {
            "schema_version": "2.0",
            "updated_at": 0.0,
            "tickets": [
                {
                    "id": "CB-TEST-001",
                    "title": "Test Ticket",
                    "ticket_class": "bug",
                    "state": "VERIFYING",
                    "source": "test",
                    "evidence": "test",
                    "problem_statement": "test",
                    "desired_state": "test",
                    "acceptance_criteria": [],
                    "affected_modules": [],
                    "dependencies": [],
                    "risk": "low",
                    "blast_radius": "",
                    "security_impact": "none",
                    "migration_impact": "none",
                    "required_reviewers": [],
                    "required_tests": [],
                    "documentation_requirements": [],
                    "rollback_strategy": "revert",
                    "estimated_cost_tokens": 0,
                    "created_at": 0.0,
                    "updated_at": 0.0,
                    "schema_version": "2.0",
                    "outcome": "",
                    "final_cost_tokens": 0,
                    "attempts": 0,
                    "rework_count": 0,
                    "assigned_agent": "",
                    "assigned_model": "",
                    "reviewer_feedback": [],
                    "last_gate_result": {},
                    "gate_history": []
                }
            ]
        }
        store_path.write_text(json.dumps(initial_data))
        return TicketStore(store_path)

    def test_blocks_complete_on_gate_failure(self, temp_state_dir, mock_ticket_store):
        """Verify that if quality gates fail, ticket does NOT transition to COMPLETE."""
        gk = Gatekeeper(state_dir=temp_state_dir)
        
        # Create a proper GateEvaluation object
        eval_fail = GateEvaluation(
            gate_name="unit_tests",
            result=GateStatus.FAIL,
            command="pytest",
            output="failed",
            duration_ms=10.0,
            passed=False,
            required=True
        )

        # Mock run_quality_gates_with_cache to return failure
        with patch('codebot.quality_gate.run_quality_gates_with_cache') as mock_run:
            mock_run.return_value = (False, [eval_fail])
            
            result = gk.verify_ticket(
                ticket_id="CB-TEST-001",
                ticket_class="bug",
                changed_files=["codebot/example.py"],
                rework_count=0,
                store=mock_ticket_store
            )

        # Assert decision is REWORK
        assert result["decision"] == "REWORK"
        assert result["passed"] is False

        # Verify ticket state in store is now REWORK, not COMPLETE
        updated_ticket = mock_ticket_store.get("CB-TEST-001")
        assert updated_ticket.state == TicketState.REWORK

    def test_allows_complete_on_gate_success(self, temp_state_dir, mock_ticket_store):
        """Verify that if quality gates pass, ticket transitions to COMPLETE."""
        gk = Gatekeeper(state_dir=temp_state_dir)

        # Mock run_quality_gates_with_cache to return success
        with patch('codebot.quality_gate.run_quality_gates_with_cache') as mock_run:
            mock_run.return_value = (True, [])
            
            result = gk.verify_ticket(
                ticket_id="CB-TEST-001",
                ticket_class="bug",
                changed_files=["codebot/example.py"],
                rework_count=0,
                store=mock_ticket_store
            )

        # Assert decision is COMPLETE
        assert result["decision"] == "COMPLETE"
        assert result["passed"] is True

        # Verify ticket state in store is now COMPLETE
        updated_ticket = mock_ticket_store.get("CB-TEST-001")
        assert updated_ticket.state == TicketState.COMPLETE


class TestConditionalGates:
    """Tests verifying synthetic scenarios trigger conditional gates."""

    @pytest.fixture
    def temp_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_security_boundary_triggers_security_review(self, temp_workspace):
        """Security tickets or files should trigger security_review gate."""
        policy = QualityGatePolicy.default()
        
        # Scenario 1: Ticket class is 'security'
        passed, evaluations = run_quality_gates(
            policy=policy,
            workspace=temp_workspace,
            ticket_class="security",
            changed_files=["codebot/some_file.py"],
            test_dirs="tests/"
        )
        
        gate_names = [ev.gate_name for ev in evaluations]
        assert "security_review" in gate_names, "Security ticket should trigger security_review gate"

    def test_data_migration_triggers_migration_and_rollback_tests(self, temp_workspace):
        """Data migration tickets should trigger migration_test and rollback_test."""
        policy = QualityGatePolicy.default()

        passed, evaluations = run_quality_gates(
            policy=policy,
            workspace=temp_workspace,
            ticket_class="feature",
            changed_files=["codebot/store.py"],
            conditions=["data_migration"],
            test_dirs="tests/"
        )

        gate_names = [ev.gate_name for ev in evaluations]
        assert "migration_test" in gate_names, "Data migration should trigger migration_test"
        assert "rollback_test" in gate_names, "Data migration should trigger rollback_test"

    def test_api_change_triggers_contract_tests(self, temp_workspace):
        """API changes should trigger contract_tests."""
        policy = QualityGatePolicy.default()

        passed, evaluations = run_quality_gates(
            policy=policy,
            workspace=temp_workspace,
            ticket_class="feature",
            changed_files=["codebot/api_router.py"],
            conditions=["api_change"],
            test_dirs="tests/"
        )

        gate_names = [ev.gate_name for ev in evaluations]
        assert "contract_tests" in gate_names, "API change should trigger contract_tests"


class TestGateFailureDetection:
    """Tests verifying that actual command failures are detected."""

    @pytest.fixture
    def temp_workspace(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    def test_failing_pytest_detected_as_fail(self, temp_workspace):
        """If pytest fails, gate result should be FAIL."""
        test_file = temp_workspace / "tests"
        test_file.mkdir()
        (test_file / "test_fail.py").write_text("def test_always_fails():\n    assert False\n")

        policy = QualityGatePolicy.default()
        
        passed, evaluations = run_quality_gates(
            policy=policy,
            workspace=temp_workspace,
            ticket_class="bug",
            changed_files=["codebot/dummy.py"],
            test_dirs="tests/test_fail.py"
        )

        test_evals = [ev for ev in evaluations if ev.gate_name == "unit_tests"]
        assert len(test_evals) == 1
        assert test_evals[0].result == GateStatus.FAIL, f"Expected FAIL, got {test_evals[0].result}. Output: {test_evals[0].output}"

    def test_passing_pytest_detected_as_pass(self, temp_workspace):
        """If pytest passes, gate result should be PASS."""
        test_file = temp_workspace / "tests"
        test_file.mkdir()
        (test_file / "test_pass.py").write_text("def test_always_passes():\n    assert True\n")

        policy = QualityGatePolicy.default()
        
        passed, evaluations = run_quality_gates(
            policy=policy,
            workspace=temp_workspace,
            ticket_class="bug",
            changed_files=["codebot/dummy.py"],
            test_dirs="tests/test_pass.py"
        )

        test_evals = [ev for ev in evaluations if ev.gate_name == "unit_tests"]
        assert len(test_evals) == 1
        assert test_evals[0].result == GateStatus.PASS, f"Expected PASS, got {test_evals[0].result}. Output: {test_evals[0].output}"
