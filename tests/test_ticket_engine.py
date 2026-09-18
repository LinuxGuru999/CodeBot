"""Tests for ticket_engine.py — schema, state machine, dedup, TicketStore CRUD."""

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
    TRANSITIONS,
    create_ticket,
    generate_ticket_id,
    TicketStore,
    SCHEMA_VERSION,
)


class TestTicketIdGeneration:
    def test_format_prefix(self):
        tid = generate_ticket_id()
        assert tid.startswith("CB-")

    def test_custom_prefix(self):
        tid = generate_ticket_id(prefix="TEST")
        assert tid.startswith("TEST-")

    def test_uniqueness(self):
        ids = {generate_ticket_id() for _ in range(100)}
        assert len(ids) == 100


class TestCreateTicket:
    def test_basic_creation(self):
        t = create_ticket(
            title="Test bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="test",
            evidence="line 42",
            problem_statement="broken thing",
            desired_state="working thing",
            acceptance_criteria=["tests pass"],
        )
        assert t.title == "Test bug"
        assert t.ticket_class == TicketClass.BUG
        assert t.severity == Severity.MEDIUM
        assert t.state == TicketState.DISCOVERED
        assert t.schema_version == SCHEMA_VERSION
        assert t.id.startswith("CB-")
        assert t.created_at > 0
        assert t.updated_at > 0

    def test_empty_title_raises(self):
        with pytest.raises(ValueError, match="title is required"):
            create_ticket("", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])

    def test_whitespace_title_raises(self):
        with pytest.raises(ValueError, match="title is required"):
            create_ticket("   ", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])

    def test_empty_acceptance_criteria_raises(self):
        with pytest.raises(ValueError, match="acceptance criterion"):
            create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", [])

    def test_empty_problem_statement_raises(self):
        with pytest.raises(ValueError, match="problem_statement"):
            create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "", "d", ["a"])

    def test_optional_fields_default(self):
        t = create_ticket("t", TicketClass.FEATURE, Severity.LOW, "s", "e", "p", "d", ["a"])
        assert t.affected_modules == []
        assert t.dependencies == []
        assert t.risk == RiskLevel.MEDIUM
        assert t.security_impact == "none"
        assert t.rollback_strategy == "revert commit"


class TestEvidenceHash:
    def test_deterministic(self):
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "evidence", "problem", "d", ["a"])
        h1 = t.evidence_hash()
        h2 = t.evidence_hash()
        assert h1 == h2

    def test_different_evidence_different_hash(self):
        t1 = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "ev1", "problem", "d", ["a"])
        t2 = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "ev2", "problem", "d", ["a"])
        assert t1.evidence_hash() != t2.evidence_hash()

    def test_same_class_and_problem_different_evidence(self):
        t1 = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "ev1", "same problem", "d", ["a"])
        t2 = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "ev2", "same problem", "d", ["a"])
        assert t1.evidence_hash() != t2.evidence_hash()

    def test_hash_length(self):
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        assert len(t.evidence_hash()) == 16


class TestStateMachine:
    def _make_ticket(self) -> Ticket:
        return create_ticket("t", TicketClass.BUG, Severity.MEDIUM, "s", "e", "p", "d", ["a"])

    def test_valid_transition_discovered_to_validating(self):
        t = self._make_ticket()
        t2 = t.transition(TicketState.VALIDATING)
        assert t2.state == TicketState.VALIDATING
        assert t2.updated_at >= t.updated_at

    def test_invalid_transition_discovered_to_complete(self):
        t = self._make_ticket()
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.COMPLETE)

    def test_full_lifecycle(self):
        t = self._make_ticket()
        for state in [
            TicketState.VALIDATING,
            TicketState.TRIAGED,
            TicketState.READY,
            TicketState.PLANNING,
            TicketState.IMPLEMENTING,
            TicketState.REVIEWING,
            TicketState.VERIFYING,
            TicketState.COMPLETE,
        ]:
            t = t.transition(state)
            assert t.state == state

    def test_rework_increments_counter(self):
        t = self._make_ticket()
        t = t.transition(TicketState.VALIDATING)
        t = t.transition(TicketState.TRIAGED)
        t = t.transition(TicketState.READY)
        t = t.transition(TicketState.IMPLEMENTING)
        t = t.transition(TicketState.REWORK)
        assert t.rework_count == 1

    def test_implementing_increments_attempts(self):
        t = self._make_ticket()
        t = t.transition(TicketState.VALIDATING)
        t = t.transition(TicketState.TRIAGED)
        t = t.transition(TicketState.READY)
        t = t.transition(TicketState.IMPLEMENTING)
        assert t.attempts == 1

    def test_terminal_states_have_no_transitions(self):
        assert TRANSITIONS[TicketState.COMPLETE] == frozenset()
        assert TRANSITIONS[TicketState.REJECTED] == frozenset()
        assert TRANSITIONS[TicketState.DUPLICATE] == frozenset()

    def test_rejected_from_discovered(self):
        t = self._make_ticket()
        t2 = t.transition(TicketState.REJECTED)
        assert t2.state == TicketState.REJECTED

    def test_duplicate_from_discovered(self):
        t = self._make_ticket()
        t2 = t.transition(TicketState.DUPLICATE)
        assert t2.state == TicketState.DUPLICATE

    def test_blocked_from_planning(self):
        t = self._make_ticket()
        t = t.transition(TicketState.VALIDATING)
        t = t.transition(TicketState.TRIAGED)
        t = t.transition(TicketState.READY)
        t = t.transition(TicketState.PLANNING)
        t = t.transition(TicketState.BLOCKED)
        assert t.state == TicketState.BLOCKED

    def test_deferred_from_triaged(self):
        t = self._make_ticket()
        t = t.transition(TicketState.VALIDATING)
        t = t.transition(TicketState.TRIAGED)
        t = t.transition(TicketState.DEFERRED)
        assert t.state == TicketState.DEFERRED

    def test_immutable_original(self):
        t = self._make_ticket()
        t2 = t.transition(TicketState.VALIDATING)
        assert t.state == TicketState.DISCOVERED
        assert t2.state == TicketState.VALIDATING


class TestSerialization:
    def test_roundtrip_json(self):
        t = create_ticket("t", TicketClass.SECURITY, Severity.HIGH, "scanner", "xss found", "fix xss", "no xss", ["test passes"])
        raw = t.to_json()
        t2 = Ticket.from_json(raw)
        assert t2.id == t.id
        assert t2.title == t.title
        assert t2.ticket_class == TicketClass.SECURITY
        assert t2.severity == Severity.HIGH
        assert t2.state == TicketState.DISCOVERED

    def test_roundtrip_dict(self):
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        d = t.to_dict()
        assert d["ticket_class"] == "bug"
        assert d["severity"] == "low"
        assert d["state"] == "DISCOVERED"
        t2 = Ticket.from_dict(d)
        assert t2.ticket_class == TicketClass.BUG


class TestTicketStore:
    def test_add_and_get(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)
        assert store.get(t.id) is not None
        assert store.get(t.id).title == "t"

    def test_count(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        assert store.count() == 0
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)
        assert store.count() == 1

    def test_persistence(self, tmp_path):
        path = tmp_path / "tickets.json"
        store1 = TicketStore(path)
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store1.add(t)
        store2 = TicketStore(path)
        assert store2.count() == 1
        assert store2.get(t.id) is not None

    def test_dedup_blocks_duplicate(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "same evidence", "same problem", "d", ["a"])
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "same evidence", "same problem", "d", ["a"])
        store.add(t1)
        with pytest.raises(ValueError, match="duplicate ticket"):
            store.add(t2)

    def test_dedup_allows_after_complete(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "same evidence", "same problem", "d", ["a"])
        store.add(t1)
        for state in [TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY,
                       TicketState.IMPLEMENTING, TicketState.REVIEWING, TicketState.VERIFYING, TicketState.COMPLETE]:
            store.transition(t1.id, state)
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "same evidence", "same problem", "d", ["a"])
        store.add(t2)
        assert store.count() == 2

    def test_transition_via_store(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)
        updated = store.transition(t.id, TicketState.VALIDATING)
        assert updated.state == TicketState.VALIDATING
        assert store.get(t.id).state == TicketState.VALIDATING

    def test_transition_nonexistent_raises(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        with pytest.raises(KeyError):
            store.transition("CB-fake", TicketState.VALIDATING)

    def test_list_by_state(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"])
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["a"])
        store.add(t1)
        store.add(t2)
        store.transition(t1.id, TicketState.VALIDATING)
        discovered = store.list_by_state(TicketState.DISCOVERED)
        validating = store.list_by_state(TicketState.VALIDATING)
        assert len(discovered) == 1
        assert len(validating) == 1

    def test_list_ready_sorted_by_severity(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        low = create_ticket("low", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"])
        crit = create_ticket("crit", TicketClass.BUG, Severity.CRITICAL, "s", "e2", "p", "d", ["a"])
        med = create_ticket("med", TicketClass.BUG, Severity.MEDIUM, "s", "e3", "p", "d", ["a"])
        for t in (low, crit, med):
            store.add(t)
            store.transition(t.id, TicketState.VALIDATING)
            store.transition(t.id, TicketState.TRIAGED)
            store.transition(t.id, TicketState.READY)
        ready = store.list_ready()
        assert ready[0].severity == Severity.CRITICAL
        assert ready[-1].severity == Severity.LOW

    def test_summary(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"])
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["a"])
        store.add(t1)
        store.add(t2)
        store.transition(t1.id, TicketState.VALIDATING)
        s = store.summary()
        assert s["DISCOVERED"] == 1
        assert s["VALIDATING"] == 1

    def test_corrupt_file_resets(self, tmp_path):
        path = tmp_path / "tickets.json"
        path.write_text("not valid json{{{")
        store = TicketStore(path)
        assert store.count() == 0
