"""Tests for ticket_engine.py — schema, state machine, dedup, TicketStore CRUD."""

import json
import time
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

    def test_rapid_fire_1000_unique_after_cold_start(self):
        """Verify 1000 rapid-fire creates produce unique IDs (CB-9226297-8627)."""
        # Simulate cold start by reimporting the module
        import importlib
        from codebot import ticket_engine
        importlib.reload(ticket_engine)
        
        # Generate 1000 IDs rapidly
        ids = {ticket_engine.generate_ticket_id() for _ in range(1000)}
        assert len(ids) == 1000, f"Expected 1000 unique IDs, got {len(ids)}"

    def test_module_reimport_no_collision(self):
        """Verify module reimport does not cause ID collision (CB-9226297-8627)."""
        import importlib
        from codebot import ticket_engine
        
        # Generate some IDs before reimport
        ids_before = {ticket_engine.generate_ticket_id() for _ in range(100)}
        
        # Reimport module (simulates process restart)
        importlib.reload(ticket_engine)
        
        # Generate IDs after reimport
        ids_after = {ticket_engine.generate_ticket_id() for _ in range(100)}
        
        # No collisions between before and after
        assert ids_before.isdisjoint(ids_after), "ID collision detected after module reimport"
        assert len(ids_before) == 100
        assert len(ids_after) == 100


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
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "same evidence", "same problem", "d", ["a"], risk=RiskLevel.LOW)
        store.add(t1)
        for state in [TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY,
                       TicketState.IMPLEMENTING, TicketState.REVIEWING, TicketState.VERIFYING, TicketState.COMPLETE]:
            if state == TicketState.COMPLETE:
                # need gate approval for COMPLETE - use record_gate_result to update cache
                store.record_gate_result(t1.id, True, gates=[])
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

    def test_summary_performance_scales(self, tmp_path):
        """summary() and list_by_state() should use state index for O(1)/O(k) lookup.

        Regression test for CB-4418574-630E: previously these methods
        iterated all tickets (O(n)) on every call.
        """
        store = TicketStore(tmp_path / "tickets.json")
        # Add 200 tickets in various states
        tickets = []
        for i in range(200):
            t = create_ticket(
                f"ticket-{i}", TicketClass.BUG, Severity.LOW,
                "test", f"evidence-{i}", "problem", "desired", ["crit"],
            )
            store.add(t)
            tickets.append(t)

        # DISCOVERED -> VALIDATING (100 tickets)
        for t in tickets[:100]:
            store.transition(t.id, TicketState.VALIDATING)
        # VALIDATING -> TRIAGED (50 tickets)
        for t in tickets[:50]:
            store.transition(t.id, TicketState.TRIAGED)
        # TRIAGED -> READY (50 tickets)
        for t in tickets[:50]:
            store.transition(t.id, TicketState.READY)

        # Verify list_by_state correctness
        discovered = store.list_by_state(TicketState.DISCOVERED)
        validating = store.list_by_state(TicketState.VALIDATING)
        ready = store.list_by_state(TicketState.READY)
        assert len(discovered) == 100   # 200 - 100
        assert len(validating) == 50    # 100 - 50
        assert len(ready) == 50

        # Verify summary correctness
        s = store.summary()
        assert s.get("DISCOVERED", 0) == 100
        assert s.get("VALIDATING", 0) == 50
        assert s.get("READY", 0) == 50

        # Verify correctness after persistence reload
        store2 = TicketStore(tmp_path / "tickets.json")
        assert store2.summary().get("DISCOVERED", 0) == 100
        assert store2.summary().get("VALIDATING", 0) == 50
        assert store2.summary().get("READY", 0) == 50
        assert len(store2.list_by_state(TicketState.READY)) == 50

    def test_state_index_consistency_after_transition(self, tmp_path):
        """State index must stay consistent across rapid transitions."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.MEDIUM, "s", "ev", "p", "d", ["a"],
                          risk=RiskLevel.LOW)
        store.add(t)
        assert len(store.list_by_state(TicketState.DISCOVERED)) == 1

        # Transition through valid lifecycle (LOW risk skips planning prerequisite)
        for state in [TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY,
                      TicketState.IMPLEMENTING, TicketState.REVIEWING]:
            store.transition(t.id, state)
            assert len(store.list_by_state(state)) == 1
            # Old state should be empty (ticket left it)
        assert store.summary().get("DISCOVERED", 0) == 0


class TestGatekeeperEnforcement:
    """Tests for VERIFYING -> COMPLETE requiring gatekeeper approval (CB-3814750-10D2)."""

    def _make_store(self, tmp_path):
        return TicketStore(tmp_path / "tickets.json")

    def _add_ticket(self, store, risk=RiskLevel.LOW):
        t = create_ticket(
            title="Test ticket",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=risk,
        )
        store.add(t)
        return t

    def _move_to_verifying(self, store, ticket_id):
        """Move a ticket through states to VERIFYING."""
        from codebot.implementation_planner import PlanStore
        # Ensure READY->IMPLEMENTING passes planning check if needed (use LOW risk or create plan)
        # If ticket is medium risk, create plan on the fly
        t = store.get(ticket_id)
        if t and t.risk != RiskLevel.LOW:
            try:
                plan_store = PlanStore(store._path.parent)
                if not plan_store.exists(ticket_id):
                    plan_store.save(ticket_id, {"steps": ["auto plan for test"]})
            except Exception:
                pass
        store.transition(ticket_id, TicketState.VALIDATING)
        store.transition(ticket_id, TicketState.TRIAGED)
        store.transition(ticket_id, TicketState.READY)
        store.transition(ticket_id, TicketState.IMPLEMENTING)
        store.transition(ticket_id, TicketState.REVIEWING)
        store.transition(ticket_id, TicketState.VERIFYING)

    def _write_gate_pass(self, state_dir, ticket_id, store=None):
        """Write a passing gate result for the given ticket via record_gate_result."""
        if store is not None:
            store.record_gate_result(ticket_id, True, gates=[])
        else:
            import os
            gate_path = state_dir / "gate_results.jsonl"
            record = {
                "ticket_id": ticket_id,
                "passed": True,
                "timestamp": time.time(),
                "gates": [],
            }
            line = json.dumps(record) + "\n"
            fd = os.open(str(gate_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)

    def _write_gate_fail(self, state_dir, ticket_id, store=None):
        """Write a failing gate result for the given ticket via record_gate_result."""
        if store is not None:
            store.record_gate_result(ticket_id, False, gates=[{"gate_name": "test", "result": "fail"}])
        else:
            import os
            gate_path = state_dir / "gate_results.jsonl"
            record = {
                "ticket_id": ticket_id,
                "passed": False,
                "timestamp": time.time(),
                "gates": [{"gate_name": "test", "result": "fail"}],
            }
            line = json.dumps(record) + "\n"
            fd = os.open(str(gate_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
            try:
                os.write(fd, line.encode("utf-8"))
            finally:
                os.close(fd)

    def test_verifying_to_complete_blocked_without_gate(self, tmp_path):
        """VERIFYING -> COMPLETE is blocked without gatekeeper approval."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_verifying(store, t.id)

        # No gate_results.jsonl exists -> should raise
        with pytest.raises(ValueError, match="gatekeeper"):
            store.transition(t.id, TicketState.COMPLETE)

    def test_verifying_to_complete_allowed_with_gate_pass(self, tmp_path):
        """VERIFYING -> COMPLETE is allowed with passing gate result."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_verifying(store, t.id)
        self._write_gate_pass(tmp_path, t.id, store=store)

        updated = store.transition(t.id, TicketState.COMPLETE)
        assert updated.state == TicketState.COMPLETE

    def test_verifying_to_complete_blocked_with_gate_fail(self, tmp_path):
        """VERIFYING -> COMPLETE is blocked with failing gate result."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_verifying(store, t.id)
        self._write_gate_fail(tmp_path, t.id, store=store)

        with pytest.raises(ValueError, match="gatekeeper"):
            store.transition(t.id, TicketState.COMPLETE)

    def test_verifying_to_rework_allowed_without_gate(self, tmp_path):
        """VERIFYING -> REWORK is allowed even without gatekeeper approval."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_verifying(store, t.id)

        # REWORK should be allowed without gate
        updated = store.transition(t.id, TicketState.REWORK)
        assert updated.state == TicketState.REWORK

    def test_rework_to_implementing_not_gated(self, tmp_path):
        """REWORK -> IMPLEMENTING is not affected by gatekeeper enforcement."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_verifying(store, t.id)
        store.transition(t.id, TicketState.REWORK)

        updated = store.transition(t.id, TicketState.IMPLEMENTING)
        assert updated.state == TicketState.IMPLEMENTING


class TestPlanningPrerequisite:
    """Tests for enforcing implementation plan prerequisite before IMPLEMENTING state."""

    def _make_store(self, tmp_path):
        return TicketStore(tmp_path / "tickets.json")

    def _add_medium_risk_ticket(self, store):
        t = create_ticket(
            title="Medium risk ticket",
            ticket_class=TicketClass.FEATURE,
            severity=Severity.MEDIUM,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.MEDIUM,
        )
        store.add(t)
        return t

    def _add_low_risk_ticket(self, store):
        t = create_ticket(
            title="Low risk ticket",
            ticket_class=TicketClass.BUG,
            severity=Severity.LOW,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        return t

    def _move_to_ready(self, store, ticket_id):
        """Move a ticket through states to READY."""
        store.transition(ticket_id, TicketState.VALIDATING)
        store.transition(ticket_id, TicketState.TRIAGED)
        store.transition(ticket_id, TicketState.READY)

    def test_medium_risk_requires_plan_before_implementing(self, tmp_path):
        """Medium risk tickets cannot transition to IMPLEMENTING without a plan."""
        store = self._make_store(tmp_path)
        t = self._add_medium_risk_ticket(store)
        self._move_to_ready(store, t.id)

        with pytest.raises(ValueError, match="requires.*implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENTING)

    def test_high_risk_requires_plan_before_implementing(self, tmp_path):
        """High risk tickets cannot transition to IMPLEMENTING without a plan."""
        store = self._make_store(tmp_path)
        t = create_ticket(
            title="High risk ticket",
            ticket_class=TicketClass.SECURITY,
            severity=Severity.HIGH,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.HIGH,
        )
        store.add(t)
        self._move_to_ready(store, t.id)

        with pytest.raises(ValueError, match="requires.*implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENTING)

    def test_critical_risk_requires_plan_before_implementing(self, tmp_path):
        """Critical risk tickets cannot transition to IMPLEMENTING without a plan."""
        store = self._make_store(tmp_path)
        t = create_ticket(
            title="Critical risk ticket",
            ticket_class=TicketClass.SECURITY,
            severity=Severity.CRITICAL,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.CRITICAL,
        )
        store.add(t)
        self._move_to_ready(store, t.id)

        with pytest.raises(ValueError, match="requires.*implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENTING)

    def test_low_risk_allows_implementing_without_plan(self, tmp_path):
        """Low risk tickets can transition to IMPLEMENTING without a plan."""
        store = self._make_store(tmp_path)
        t = self._add_low_risk_ticket(store)
        self._move_to_ready(store, t.id)

        # Should not raise
        updated = store.transition(t.id, TicketState.IMPLEMENTING)
        assert updated.state == TicketState.IMPLEMENTING

    def test_medium_risk_with_plan_allows_implementing(self, tmp_path):
        """Medium risk tickets with a plan can transition to IMPLEMENTING."""
        from codebot.implementation_planner import PlanStore

        store = self._make_store(tmp_path)
        t = self._add_medium_risk_ticket(store)
        self._move_to_ready(store, t.id)

        # Create a plan
        plans_dir = tmp_path / "plans"
        plan_store = PlanStore(tmp_path)
        plan_store.save(t.id, {"steps": ["step1", "step2"]})

        # Should not raise
        updated = store.transition(t.id, TicketState.IMPLEMENTING)
        assert updated.state == TicketState.IMPLEMENTING

    def test_transition_from_other_states_not_enforced(self, tmp_path):
        """Planning prerequisite only enforced for READY -> IMPLEMENTING."""
        store = self._make_store(tmp_path)
        t = self._add_medium_risk_ticket(store)
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        store.transition(t.id, TicketState.PLANNING)

        # From PLANNING -> IMPLEMENTING should work without external plan file
        # (the act of being in PLANNING implies plan generation)
        updated = store.transition(t.id, TicketState.IMPLEMENTING)
        assert updated.state == TicketState.IMPLEMENTING

    def test_rework_to_implementing_not_enforced(self, tmp_path):
        """REWORK -> IMPLEMENTING transition is not subject to planning check."""
        from codebot.implementation_planner import PlanStore

        store = self._make_store(tmp_path)
        t = self._add_medium_risk_ticket(store)
        self._move_to_ready(store, t.id)

        # Create plan to get past first gate
        plan_store = PlanStore(tmp_path)
        plan_store.save(t.id, {"steps": ["step1"]})
        store.transition(t.id, TicketState.IMPLEMENTING)
        store.transition(t.id, TicketState.REVIEWING)
        store.transition(t.id, TicketState.REWORK)

        # Delete the plan to simulate it being removed
        plan_store.delete(t.id)

        # REWORK -> IMPLEMENTING should work without plan
        updated = store.transition(t.id, TicketState.IMPLEMENTING)
        assert updated.state == TicketState.IMPLEMENTING
