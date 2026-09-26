"""Closed-loop integration tests for context contract enforcement.

These tests exercise the full Step 1 doctrine against a live TicketStore:
missing context → BLOCK → durable diagnosis → no implicit recovery →
context repaired → contract satisfied → ADVANCE → diagnostic retired →
crash recovery (reload from disk).

Also validates §8 MANIFESTO: context values MUST agree with the transition.
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import pytest

from codebot.context_contracts import (
    ContextContractViolation,
    ContextGateFailure,
)
from codebot.ticket_engine import (
    RiskLevel,
    Severity,
    Ticket,
    TicketClass,
    TicketState,
    TicketStore,
)


def _make_discovered_ticket(store: TicketStore, ticket_id: str, **overrides) -> Ticket:
    defaults = dict(
        id=ticket_id,
        title="Integration test ticket",
        ticket_class=TicketClass.FEATURE,
        severity=Severity.LOW,
        state=TicketState.DISCOVERED,
        source="test",
        evidence="",
        problem_statement="Bug in module X causes incorrect output",
        desired_state="Module X produces correct output",
        acceptance_criteria=["Output matches spec"],
        affected_modules=["module_x"],
        dependencies=[],
        risk=RiskLevel.LOW,
        blast_radius="",
        security_impact="",
        migration_impact="",
        required_reviewers=[],
        required_tests=[],
        documentation_requirements=[],
        rollback_strategy="revert",
        estimated_cost_tokens=0,
        created_at=time.time(),
        updated_at=time.time(),
    )
    defaults.update(overrides)
    t = Ticket(**defaults)
    store.add(t)
    return t


class TestMissingContextClosedLoop:
    """Validates the full block→diagnose→repair→advance→retire cycle."""

    def test_step1_first_attempt_blocks_and_creates_artifact(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_discovered_ticket(store, "CB-A01", evidence="")

        with pytest.raises(ContextContractViolation) as exc_info:
            store.transition("CB-A01", TicketState.TRIAGED)

        violation = exc_info.value
        assert violation.ticket_id == "CB-A01"
        assert violation.source_state == "DISCOVERED"
        assert violation.dest_state == "TRIAGED"
        assert "evidence" in violation.missing_fields
        assert violation.failed_invariants == []
        assert isinstance(violation, ValueError)

        ticket = store.get("CB-A01")
        assert ticket.state == TicketState.DISCOVERED

        artifact = ContextGateFailure.load(tmp_path, "CB-A01", "DISCOVERED", "TRIAGED")
        assert artifact is not None
        assert artifact.attempt_count == 1
        assert artifact.repairable is True
        assert artifact.repair_exhausted is False
        assert "evidence" in artifact.missing_fields
        assert artifact.producer_role == "discovery"

    def test_step2_second_attempt_saturates_exhausted(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_discovered_ticket(store, "CB-A02", evidence="")

        with pytest.raises(ContextContractViolation):
            store.transition("CB-A02", TicketState.TRIAGED)
        with pytest.raises(ContextContractViolation):
            store.transition("CB-A02", TicketState.TRIAGED)

        ticket = store.get("CB-A02")
        assert ticket.state == TicketState.DISCOVERED

        artifact = ContextGateFailure.load(tmp_path, "CB-A02", "DISCOVERED", "TRIAGED")
        assert artifact is not None
        assert artifact.attempt_count == 2
        assert artifact.repairable is False
        assert artifact.repair_exhausted is True

    def test_step3_third_attempt_idempotent_saturation(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_discovered_ticket(store, "CB-A03", evidence="")

        with pytest.raises(ContextContractViolation):
            store.transition("CB-A03", TicketState.TRIAGED)
        with pytest.raises(ContextContractViolation):
            store.transition("CB-A03", TicketState.TRIAGED)
        with pytest.raises(ContextContractViolation):
            store.transition("CB-A03", TicketState.TRIAGED)

        ticket = store.get("CB-A03")
        assert ticket.state == TicketState.DISCOVERED

        artifact = ContextGateFailure.load(tmp_path, "CB-A03", "DISCOVERED", "TRIAGED")
        assert artifact.attempt_count == 2
        assert artifact.repair_exhausted is True

    def test_step4_repair_and_advance_cleans_artifact(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_discovered_ticket(store, "CB-A04", evidence="")

        with pytest.raises(ContextContractViolation):
            store.transition("CB-A04", TicketState.TRIAGED)

        assert ContextGateFailure.load(tmp_path, "CB-A04", "DISCOVERED", "TRIAGED") is not None

        current = store.get("CB-A04")
        assert current is not None
        fixed = replace(current, evidence="module_x.py:42 — output mismatch confirmed")
        assert fixed.state == current.state
        store._tickets["CB-A04"] = fixed
        store._dirty_ids.add("CB-A04")

        result = store.transition("CB-A04", TicketState.TRIAGED)
        assert result.state == TicketState.TRIAGED

        assert ContextGateFailure.load(tmp_path, "CB-A04", "DISCOVERED", "TRIAGED") is None

    def test_step5_crash_recovery_persists_correctly(self, tmp_path: Path):
        tickets_file = tmp_path / "tickets.json"
        store1 = TicketStore(tickets_file)
        _make_discovered_ticket(store1, "CB-A05", evidence="")

        with pytest.raises(ContextContractViolation):
            store1.transition("CB-A05", TicketState.TRIAGED)

        current = store1.get("CB-A05")
        fixed = replace(current, evidence="module_x.py:42")
        store1._tickets["CB-A05"] = fixed
        store1._dirty_ids.add("CB-A05")
        store1.transition("CB-A05", TicketState.TRIAGED)

        del store1

        store2 = TicketStore(tickets_file)
        reloaded = store2.get("CB-A05")
        assert reloaded is not None
        assert reloaded.state == TicketState.TRIAGED
        assert reloaded.evidence == "module_x.py:42"
        assert ContextGateFailure.load(tmp_path, "CB-A05", "DISCOVERED", "TRIAGED") is None


class TestValueDisagreement:
    """Validates §8 MANIFESTO: context values MUST agree with the transition."""

    def _make_goal_ticket(self, store: TicketStore, ticket_id: str, disposition: str) -> Ticket:
        t = Ticket(
            id=ticket_id,
            title="Goal test",
            ticket_class=TicketClass.FEATURE,
            severity=Severity.LOW,
            state=TicketState.GOAL,
            source="test",
            evidence="f.py:1",
            problem_statement="Need feature X",
            desired_state="Feature X exists",
            acceptance_criteria=["X works"],
            affected_modules=["mod_a"],
            dependencies=[],
            risk=RiskLevel.LOW,
            blast_radius="",
            security_impact="",
            migration_impact="",
            required_reviewers=[],
            required_tests=[],
            documentation_requirements=[],
            rollback_strategy="revert",
            estimated_cost_tokens=0,
            created_at=time.time(),
            updated_at=time.time(),
            goal_disposition=disposition,
            goal_reason="deferred to next quarter" if disposition != "NOW" else "",
        )
        store.add(t)
        return t

    def test_goal_decomp_blocked_when_disposition_is_later(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        self._make_goal_ticket(store, "CB-B01", "LATER")

        with pytest.raises(ContextContractViolation) as exc_info:
            store.transition("CB-B01", TicketState.DECOMP)

        v = exc_info.value
        assert v.source_state == "GOAL"
        assert v.dest_state == "DECOMP"
        assert "goal_disposition" not in v.missing_fields
        assert len(v.failed_invariants) == 1
        assert "GOAL->DECOMP" in v.failed_invariants[0]

        ticket = store.get("CB-B01")
        assert ticket.state == TicketState.GOAL

    def test_goal_later_succeeds_with_same_context(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        self._make_goal_ticket(store, "CB-B02", "LATER")

        result = store.transition("CB-B02", TicketState.LATER)
        assert result.state == TicketState.LATER

    def test_goal_never_blocked_when_disposition_is_now(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        self._make_goal_ticket(store, "CB-B03", "NOW")

        with pytest.raises(ContextContractViolation) as exc_info:
            store.transition("CB-B03", TicketState.NEVER)

        v = exc_info.value
        assert "GOAL->NEVER" in v.failed_invariants[0]
        assert store.get("CB-B03").state == TicketState.GOAL

    def test_goal_decomp_succeeds_when_disposition_matches(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        self._make_goal_ticket(store, "CB-B04", "NOW")

        result = store.transition("CB-B04", TicketState.DECOMP)
        assert result.state == TicketState.DECOMP
