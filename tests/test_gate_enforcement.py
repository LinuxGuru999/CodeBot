"""Tests for quality gate enforcement in ticket state machine.

Ticket: CB-3814750-10D2
Verifies that tickets cannot transition from VERIFYING to COMPLETE without
gatekeeper approval, failed gates automatically trigger REWORK, gate results
are logged with tickets, and maximum rework attempts are enforced.
"""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.ticket_engine import (
    Ticket,
    TicketState,
    TicketClass,
    Severity,
    RiskLevel,
    create_ticket,
    TicketStore,
)
from codebot.quality_gate import record_gate_results, GateResult, GateEvaluation


def _advance_to_verifying(store: TicketStore, ticket_id: str) -> None:
    """Helper to move a ticket through DISCOVERED -> ... -> VERIFYING."""
    store.transition(ticket_id, TicketState.VALIDATING)
    store.transition(ticket_id, TicketState.TRIAGED)
    store.transition(ticket_id, TicketState.READY)
    store.transition(ticket_id, TicketState.IMPLEMENTING)
    store.transition(ticket_id, TicketState.REVIEWING)
    store.transition(ticket_id, TicketState.VERIFYING)


def _record_gate_pass(state_dir: Path, ticket_id: str) -> None:
    """Record a passing gate result for a ticket."""
    evaluations = [
        GateEvaluation(
            gate_name="build",
            result=GateResult.PASS,
            command="echo ok",
            output="ok",
            duration_seconds=0.1,
            required=True,
        ),
        GateEvaluation(
            gate_name="unit_tests",
            result=GateResult.PASS,
            command="pytest -q",
            output="ok",
            duration_seconds=0.5,
            required=True,
        ),
    ]
    record_gate_results(state_dir, ticket_id, True, evaluations)


def _record_gate_fail(state_dir: Path, ticket_id: str) -> None:
    """Record a failing gate result for a ticket."""
    evaluations = [
        GateEvaluation(
            gate_name="build",
            result=GateResult.FAIL,
            command="echo fail",
            output="build failed",
            duration_seconds=0.1,
            required=True,
            error_message="exit code 1",
        ),
    ]
    record_gate_results(state_dir, ticket_id, False, evaluations)


class TestGateEnforcement:
    """Verify VERIFYING -> COMPLETE requires gatekeeper approval."""

    def test_complete_blocked_without_gate(self, tmp_path: Path) -> None:
        """VERIFYING -> COMPLETE should fail when no gate results exist."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = TicketStore(state_dir / "tickets.json")
        t = create_ticket(
            "no gate", TicketClass.BUG, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
        )
        store.add(t)
        _advance_to_verifying(store, t.id)

        with pytest.raises(ValueError, match="gatekeeper approval"):
            store.transition(t.id, TicketState.COMPLETE)

    def test_complete_blocked_with_failed_gate(self, tmp_path: Path) -> None:
        """VERIFYING -> COMPLETE should fail when gates did not pass."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = TicketStore(state_dir / "tickets.json")
        t = create_ticket(
            "failed gate", TicketClass.BUG, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
        )
        store.add(t)
        _advance_to_verifying(store, t.id)

        _record_gate_fail(state_dir, t.id)

        with pytest.raises(ValueError, match="gatekeeper approval"):
            store.transition(t.id, TicketState.COMPLETE)

    def test_complete_allowed_with_passing_gate(self, tmp_path: Path) -> None:
        """VERIFYING -> COMPLETE should succeed when gates passed."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = TicketStore(state_dir / "tickets.json")
        t = create_ticket(
            "passed gate", TicketClass.BUG, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
        )
        store.add(t)
        _advance_to_verifying(store, t.id)

        _record_gate_pass(state_dir, t.id)

        updated = store.transition(t.id, TicketState.COMPLETE)
        assert updated.state == TicketState.COMPLETE

    def test_rework_still_allowed_without_gate(self, tmp_path: Path) -> None:
        """VERIFYING -> REWORK should still work without gate results."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = TicketStore(state_dir / "tickets.json")
        t = create_ticket(
            "rework without gate", TicketClass.BUG, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
        )
        store.add(t)
        _advance_to_verifying(store, t.id)

        updated = store.transition(t.id, TicketState.REWORK)
        assert updated.state == TicketState.REWORK
        assert updated.rework_count == 1


class TestGateResultLogging:
    """Verify gate results are recorded with ticket context."""

    def test_gate_results_contain_ticket_id(self, tmp_path: Path) -> None:
        """Gate results should be associated with a ticket ID."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        _record_gate_pass(state_dir, "CB-TEST-001")

        gate_results_path = state_dir / "gate_results.jsonl"
        assert gate_results_path.exists()

        lines = gate_results_path.read_text().strip().split("\n")
        record = json.loads(lines[-1])
        assert record["ticket_id"] == "CB-TEST-001"
        assert record["passed"] is True
        assert "gates" in record
        assert "timestamp" in record

    def test_multiple_gate_records_for_ticket(self, tmp_path: Path) -> None:
        """A ticket can have multiple gate result records (rework cycles)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        _record_gate_fail(state_dir, "CB-TEST-002")
        _record_gate_pass(state_dir, "CB-TEST-002")

        gate_results_path = state_dir / "gate_results.jsonl"
        lines = gate_results_path.read_text().strip().split("\n")
        records = [json.loads(l) for l in lines if l.strip()]
        ticket_records = [r for r in records if r["ticket_id"] == "CB-TEST-002"]
        assert len(ticket_records) == 2
        assert ticket_records[0]["passed"] is False
        assert ticket_records[1]["passed"] is True


class TestReworkEnforcement:
    """Verify maximum rework attempts are enforced."""

    def test_max_rework_attempts_constant(self) -> None:
        """MAX_REWORK_ATTEMPTS should be 3."""
        from codebot.gatekeeper import MAX_REWORK_ATTEMPTS
        assert MAX_REWORK_ATTEMPTS == 3

    def test_gatekeeper_logs_rework_count(self, tmp_path: Path) -> None:
        """Gatekeeper should track rework count in its log."""
        from codebot.gatekeeper import Gatekeeper

        ws = tmp_path / "ws"
        ws.mkdir()
        policy_path = tmp_path / "gates.yaml"
        policy_path.write_text("required:\n  - name: t\n    command: false\n")

        gk = Gatekeeper(tmp_path / "state", policy_path=policy_path, workspace=ws)
        r = gk.verify_ticket("CB-REWORK", "bug", ["f.py"], rework_count=2)
        assert r["decision"] == "REWORK"
        assert r["rework_count"] == 2
        assert r["evolve_prompt"] is False

    def test_gatekeeper_evolve_prompt_at_max(self, tmp_path: Path) -> None:
        """Gatekeeper should flag prompt evolution when max rework reached."""
        from codebot.gatekeeper import Gatekeeper, MAX_REWORK_ATTEMPTS

        ws = tmp_path / "ws"
        ws.mkdir()
        policy_path = tmp_path / "gates.yaml"
        policy_path.write_text("required:\n  - name: t\n    command: false\n")

        gk = Gatekeeper(tmp_path / "state", policy_path=policy_path, workspace=ws)
        r = gk.verify_ticket("CB-EVOLVE", "bug", ["f.py"], rework_count=MAX_REWORK_ATTEMPTS)
        assert r["decision"] == "REWORK"
        assert r["evolve_prompt"] is True


class TestOtherTransitionsUnaffected:
    """Non-COMPLETE transitions should not require gate results."""

    def test_verifying_to_rework_unaffected(self, tmp_path: Path) -> None:
        """VERIFYING -> REWORK should work without gate results."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = TicketStore(state_dir / "tickets.json")
        t = create_ticket(
            "rework test", TicketClass.BUG, Severity.LOW,
            "test", "ev", "prob", "desired", ["ac"],
        )
        store.add(t)
        _advance_to_verifying(store, t.id)

        updated = store.transition(t.id, TicketState.REWORK)
        assert updated.state == TicketState.REWORK

    def test_reviewing_to_verifying_unaffected(self, tmp_path: Path) -> None:
        """REVIEWING -> VERIFYING should work without gate results."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store = TicketStore(state_dir / "tickets.json")
        t = create_ticket(
            "verify test", TicketClass.BUG, Severity.LOW,
            "test", "ev", "prob", "desired", ["ac"],
        )
        store.add(t)
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        store.transition(t.id, TicketState.IMPLEMENTING)
        store.transition(t.id, TicketState.REVIEWING)

        updated = store.transition(t.id, TicketState.VERIFYING)
        assert updated.state == TicketState.VERIFYING
