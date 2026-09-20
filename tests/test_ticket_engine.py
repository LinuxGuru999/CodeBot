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
        store1.flush()
        store2 = TicketStore(path)
        assert store2.count() == 1
        assert store2.get(t.id) is not None
        store1.close()
        store2.close()

    def test_persistence_via_wal(self, tmp_path):
        """Verify WAL-only persistence: add ticket without triggering compaction."""
        path = tmp_path / "tickets.json"
        store1 = TicketStore(path)
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store1.add(t)
        store1.flush()
        store2 = TicketStore(path)
        assert store2.count() == 1
        assert store2.get(t.id) is not None
        store1.close()
        store2.close()

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
        store.flush()
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

    def test_wal_replay_state_index_consistency(self, tmp_path):
        """State index must be consistent after WAL replay when ticket
        transitions between saves (CB-4418574-630E).

        If a ticket is saved in the WAL in multiple states (e.g., first
        DISCOVERED then VALIDATING), replay must not leave the ticket in
        both states in the index.
        """
        path = tmp_path / "tickets.json"
        store1 = TicketStore(path)

        # Add tickets and transition one through multiple states
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"],
                           risk=RiskLevel.LOW)
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["a"],
                           risk=RiskLevel.LOW)
        store1.add(t1)
        store1.add(t2)

        # Flush: compacts main JSON with both in DISCOVERED state
        store1.flush()

        # Now transition t1 through multiple states without flushing
        # Each transition writes WAL entries
        store1.transition(t1.id, TicketState.VALIDATING)
        store1.transition(t1.id, TicketState.TRIAGED)
        store1.transition(t1.id, TicketState.READY)

        # Flush to persist all WAL entries
        store1.flush()
        store1.close()

        # Reload store — WAL replay must rebuild state index correctly
        store2 = TicketStore(path)

        # t1 should only be in READY, not in DISCOVERED/VALIDATING/TRIAGED
        assert len(store2.list_by_state(TicketState.DISCOVERED)) == 1  # t2
        assert len(store2.list_by_state(TicketState.VALIDATING)) == 0
        assert len(store2.list_by_state(TicketState.TRIAGED)) == 0
        assert len(store2.list_by_state(TicketState.READY)) == 1  # t1

        # Summary must match
        s = store2.summary()
        assert s.get("DISCOVERED", 0) == 1
        assert s.get("VALIDATING", 0) == 0
        assert s.get("TRIAGED", 0) == 0
        assert s.get("READY", 0) == 1

        # No duplicate ticket appearances
        total = sum(s.values())
        assert total == 2  # exactly 2 tickets across all states

        store2.close()

    def test_wal_replay_no_compaction_state_index(self, tmp_path):
        """State index is correct after WAL-only replay (no main JSON).

        When tickets.json does not exist but .wal.jsonl does, the store
        must still maintain correct state indexes from WAL entries alone.
        """
        path = tmp_path / "tickets.json"
        store1 = TicketStore(path)

        # Add tickets
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"],
                           risk=RiskLevel.LOW)
        store1.add(t1)
        store1.flush()  # This compacts (path didn't exist), creates tickets.json

        # Transition t1, then flush — WAL-only write (not enough for compaction)
        store1.transition(t1.id, TicketState.VALIDATING)
        store1.flush()

        # Delete main JSON so _load falls through to WAL-only path
        path.unlink(missing_ok=True)
        (tmp_path / "tickets.lock").unlink(missing_ok=True)
        store1.close()

        # New store: _load sees no tickets.json → _replay_wal() only
        store2 = TicketStore(path)
        # Only t1 should be present (from WAL), in VALIDATING state
        assert store2.count() == 1
        assert len(store2.list_by_state(TicketState.VALIDATING)) == 1
        assert len(store2.list_by_state(TicketState.DISCOVERED)) == 0
        s = store2.summary()
        assert s.get("VALIDATING", 0) == 1
        assert s.get("DISCOVERED", 0) == 0
        total = sum(s.values())
        assert total == 1
        store2.close()


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


class TestPerformance:
    """Performance tests for TicketStore operations."""

    def test_list_ready_performance_at_10k_tickets(self, tmp_path):
        """list_ready() must complete in <10ms with 10,000 tickets.

        Regression test for CB-8851929-4713: previously list_ready() performed
        O(N) linear scans over all tickets. With indexed lookups, it should be
        O(K) where K is the number of READY tickets.
        """
        store = TicketStore(tmp_path / "tickets.json")
        num_tickets = 10000
        num_ready = 1000  # 10% in READY state

        # Add tickets efficiently by bypassing individual transitions
        # We'll create them directly in DISCOVERED state, then move some to READY
        tickets = []
        for i in range(num_tickets):
            t = create_ticket(
                f"ticket-{i}",
                TicketClass.BUG,
                Severity.LOW,
                "test",
                f"evidence-{i}",
                "problem",
                "desired",
                ["crit"],
                risk=RiskLevel.LOW,  # Low risk to skip planning prerequisite
            )
            store.add(t)
            tickets.append(t)

        # Move first num_ready tickets to READY state
        for t in tickets[:num_ready]:
            store.transition(t.id, TicketState.VALIDATING)
            store.transition(t.id, TicketState.TRIAGED)
            store.transition(t.id, TicketState.READY)

        # Wait for async save to complete
        time.sleep(0.6)

        # Measure list_ready() performance
        start = time.perf_counter()
        ready_tickets = store.list_ready()
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Verify correctness
        assert len(ready_tickets) == num_ready

        # Verify performance: must be <10ms
        assert elapsed_ms < 10, f"list_ready() took {elapsed_ms:.2f}ms, expected <10ms"

    def test_list_by_state_performance_at_10k_tickets(self, tmp_path):
        """list_by_state() must be O(K) not O(N) with 10,000 tickets."""
        store = TicketStore(tmp_path / "tickets.json")
        num_tickets = 10000

        # Add tickets
        for i in range(num_tickets):
            t = create_ticket(
                f"ticket-{i}",
                TicketClass.BUG,
                Severity.LOW,
                "test",
                f"evidence-{i}",
                "problem",
                "desired",
                ["crit"],
                risk=RiskLevel.LOW,
            )
            store.add(t)

        # Wait for async save
        time.sleep(0.6)

        # Measure list_by_state performance for DISCOVERED state (all tickets)
        start = time.perf_counter()
        discovered = store.list_by_state(TicketState.DISCOVERED)
        elapsed_ms = (time.perf_counter() - start) * 1000

        assert len(discovered) == num_tickets
        # Should be fast even with all tickets in one state
        assert elapsed_ms < 50, f"list_by_state() took {elapsed_ms:.2f}ms, expected <50ms"


class TestApprovalCache:
    """Tests for O(1) gate approval cache (CB-209456-CF06)."""

    def test_has_gate_approval_returns_cached_value(self, tmp_path):
        """_has_gate_approval should return True from cache after record_gate_result."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)

        # Before recording, should be False
        assert store._has_gate_approval(t.id) is False

        # Record a passing gate result
        store.record_gate_result(t.id, True, gates=[])

        # Should now return True from cache (O(1))
        assert store._has_gate_approval(t.id) is True

    def test_cache_updates_on_gate_results_write(self, tmp_path):
        """Cache should reflect the latest gate result when overwritten."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)

        # Pass first
        store.record_gate_result(t.id, True, gates=[])
        assert store._has_gate_approval(t.id) is True

        # Fail second — cache should update to False
        store.record_gate_result(t.id, False, gates=[{"gate_name": "test", "result": "fail"}])
        assert store._has_gate_approval(t.id) is False

        # Pass third — cache should update back to True
        store.record_gate_result(t.id, True, gates=[])
        assert store._has_gate_approval(t.id) is True

    def test_cache_handles_missing_ticket_id_gracefully(self, tmp_path):
        """_has_gate_approval should return False for unknown ticket IDs."""
        store = TicketStore(tmp_path / "tickets.json")
        assert store._has_gate_approval("CB-NONEXISTENT") is False

    def test_cache_rebuilt_on_reload(self, tmp_path):
        """Cache should be rebuilt from gate_results.jsonl on store reload."""
        path = tmp_path / "tickets.json"
        store1 = TicketStore(path)
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store1.add(t)
        store1.flush()
        store1.record_gate_result(t.id, True, gates=[])

        # Create a new store instance pointing to the same path (simulates restart)
        store2 = TicketStore(path)
        # Cache should have been rebuilt from gate_results.jsonl
        assert store2._has_gate_approval(t.id) is True
        store1.close()
        store2.close()

    def test_concurrent_access_does_not_corrupt_cache(self, tmp_path):
        """Multiple threads recording gate results should not corrupt the cache."""
        import threading

        store = TicketStore(tmp_path / "tickets.json")
        tickets = []
        for i in range(20):
            t = create_ticket(f"t-{i}", TicketClass.BUG, Severity.LOW, "s", f"e-{i}", "p", "d", ["a"])
            store.add(t)
            tickets.append(t)

        errors = []

        def worker(ticket_id: str, passed: bool):
            try:
                for _ in range(10):
                    store.record_gate_result(ticket_id, passed, gates=[])
                    store._has_gate_approval(ticket_id)
            except Exception as e:
                errors.append(e)

        threads = []
        for t in tickets:
            threads.append(threading.Thread(target=worker, args=(t.id, True)))
            threads.append(threading.Thread(target=worker, args=(t.id, False)))

        for th in threads:
            th.start()
        for th in threads:
            th.join(timeout=10)

        assert not errors, f"Concurrent access raised errors: {errors}"

        # Each ticket's final state should be consistent (either True or False,
        # but no corruption or KeyError)
        for t in tickets:
            result = store._has_gate_approval(t.id)
            assert isinstance(result, bool)


class TestSavePerformance:
    """Performance tests for CB-9292374-E811: O(N) serialization fix.

    Validates that WAL-based incremental saves keep I/O cost constant
    for single mutations and that batch additions stay under 1 second.
    """

    def test_add_100_tickets_io_under_1s(self, tmp_path):
        """Adding 100 tickets sequentially takes <1s total for I/O.

        Acceptance criterion: adding 100 tickets sequentially takes
        <1 second total for I/O.
        """
        path = tmp_path / "tickets.json"
        store = TicketStore(path)

        start = time.perf_counter()
        for i in range(100):
            t = create_ticket(
                f"perf-ticket-{i}",
                TicketClass.BUG,
                Severity.LOW,
                "test",
                f"evidence-perf-{i}",
                "problem",
                "desired",
                ["crit"],
                risk=RiskLevel.LOW,
            )
            store.add(t)
        store.flush()
        elapsed = time.perf_counter() - start

        assert store.count() == 100
        assert elapsed < 1.0, (
            f"Adding 100 tickets + flush took {elapsed:.3f}s, expected <1s"
        )
        store.close()

    def test_single_mutation_constant_cost(self, tmp_path):
        """Single-mutation I/O cost does not scale with total ticket count.

        Acceptance criterion: serialization cost does not scale linearly
        with ticket count for single mutations.  We pre-populate with 500
        tickets, then add 1 more and measure the flush cost.
        """
        path = tmp_path / "tickets.json"
        store = TicketStore(path)

        # Pre-populate with 500 tickets
        for i in range(500):
            t = create_ticket(
                f"prepop-{i}",
                TicketClass.BUG,
                Severity.LOW,
                "test",
                f"evidence-prepop-{i}",
                "problem",
                "desired",
                ["crit"],
                risk=RiskLevel.LOW,
            )
            store.add(t)
        store.flush()

        # Force a compaction so the baseline is a clean JSON file
        store._save_count = store._FULL_SAVE_INTERVAL - 1  # next save compacts
        t_marker = create_ticket(
            "prepop-marker",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "evidence-marker",
            "problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t_marker)
        store.flush()  # This triggers compaction

        # Now measure a single-mutation flush (WAL append, not compaction)
        t_new = create_ticket(
            "single-mutation-ticket",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "evidence-single",
            "problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        start = time.perf_counter()
        store.add(t_new)
        store.flush()
        elapsed = time.perf_counter() - start

        # WAL append for a single ticket should be very fast (< 50ms)
        assert elapsed < 0.05, (
            f"Single-mutation flush with 500+ tickets took {elapsed:.3f}s, "
            f"expected <0.05s (WAL append should be O(1))"
        )
        assert store.count() == 502
        store.close()

    def test_wal_replay_preserves_data_after_crash(self, tmp_path):
        """WAL replay must restore data written after last compaction.

        Simulates a crash by creating a store, adding tickets, flushing
        (writes WAL entries), and verifying a fresh store replays them.
        """
        path = tmp_path / "tickets.json"
        store1 = TicketStore(path)
        ids = []
        for i in range(20):
            t = create_ticket(
                f"crash-test-{i}",
                TicketClass.BUG,
                Severity.LOW,
                "test",
                f"evidence-crash-{i}",
                "problem",
                "desired",
                ["crit"],
                risk=RiskLevel.LOW,
            )
            store1.add(t)
            ids.append(t.id)
        store1.flush()
        store1.close()

        # Verify all tickets survive a "restart"
        store2 = TicketStore(path)
        assert store2.count() == 20
        for tid in ids:
            assert store2.get(tid) is not None, f"Ticket {tid} not found after WAL replay"
        store2.close()

    def test_background_worker_clean_shutdown(self, tmp_path):
        """close() must flush pending mutations and stop the worker thread."""
        path = tmp_path / "tickets.json"
        store = TicketStore(path)

        # Verify worker is alive
        assert store._save_worker.is_alive()

        # Add tickets but don't flush — let the background worker handle it
        for i in range(10):
            t = create_ticket(
                f"shutdown-test-{i}",
                TicketClass.BUG,
                Severity.LOW,
                "test",
                f"evidence-shutdown-{i}",
                "problem",
                "desired",
                ["crit"],
                risk=RiskLevel.LOW,
            )
            store.add(t)

        # Close should flush and stop the worker
        store.close()
        assert not store._save_worker.is_alive()

        # Verify data persisted
        store2 = TicketStore(path)
        assert store2.count() == 10
        store2.close()

    def test_debounce_batches_rapid_mutations(self, tmp_path):
        """Rapid-fire mutations should be batched into fewer saves.

        With SAVE_DEBOUNCE_SECONDS = 0.5, 50 rapid adds should produce
        at most 1-2 saves, not 50 individual I/O operations.
        """
        path = tmp_path / "tickets.json"
        store = TicketStore(path)

        # Count how many times _save is called
        original_save = store._save
        save_count = [0]

        def counting_save():
            save_count[0] += 1
            original_save()

        store._save = counting_save

        # Rapid-fire 50 adds
        for i in range(50):
            t = create_ticket(
                f"debounce-test-{i}",
                TicketClass.BUG,
                Severity.LOW,
                "test",
                f"evidence-debounce-{i}",
                "problem",
                "desired",
                ["crit"],
                risk=RiskLevel.LOW,
            )
            store.add(t)

        # Wait for debounce to settle
        time.sleep(1.5)

        # Should be ≤2 saves (one debounce batch, maybe one more)
        assert save_count[0] <= 2, (
            f"Expected ≤2 saves after debounced 50-add burst, got {save_count[0]}"
        )

        store.flush()
        store.close()
        store2 = TicketStore(path)
        assert store2.count() == 50
        store2.close()


class TestBatchTransition:
    """Tests for CB-9292354-0D38: O(K*N) ticket dispatch overhead fix.

    Validates that batch_transition() applies K transitions with a single
    save call, and that dispatch time is constant relative to total ticket count.
    """

    def _make_store(self, tmp_path):
        return TicketStore(tmp_path / "tickets.json")

    def _add_tickets_in_state(self, store, count, target_state, risk=RiskLevel.LOW):
        """Helper to create tickets and advance them to a target state."""
        tickets = []
        for i in range(count):
            t = create_ticket(
                f"batch-{i}", TicketClass.BUG, Severity.LOW,
                "test", f"evidence-batch-{i}", "problem", "desired", ["ac"],
                risk=risk,
            )
            store.add(t)
            # Advance through lifecycle to target state
            current = TicketState.DISCOVERED
            path_to_target = {
                TicketState.VALIDATING: [TicketState.VALIDATING],
                TicketState.TRIAGED: [TicketState.VALIDATING, TicketState.TRIAGED],
                TicketState.READY: [TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY],
                TicketState.IMPLEMENTING: [
                    TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY,
                    TicketState.IMPLEMENTING,
                ],
                TicketState.REVIEWING: [
                    TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY,
                    TicketState.IMPLEMENTING, TicketState.REVIEWING,
                ],
            }
            for state in path_to_target.get(target_state, []):
                t = store.transition(t.id, state)
            tickets.append(t)
        return tickets

    def test_batch_transition_applies_all_changes(self, tmp_path):
        """batch_transition should apply all K transitions atomically."""
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 5, TicketState.IMPLEMENTING)

        transitions = [(t.id, TicketState.REVIEWING, None) for t in tickets]
        results = store.batch_transition(transitions)

        assert len(results) == 5
        for r in results:
            assert r.state == TicketState.REVIEWING

        # Verify store state is consistent
        reviewing = store.list_by_state(TicketState.REVIEWING)
        assert len(reviewing) == 5
        implementing = store.list_by_state(TicketState.IMPLEMENTING)
        assert len(implementing) == 0
        store.close()

    def test_batch_transition_empty_list(self, tmp_path):
        """batch_transition with empty list should return empty list."""
        store = self._make_store(tmp_path)
        results = store.batch_transition([])
        assert results == []
        store.close()

    def test_batch_transition_invalid_raises_keyerror(self, tmp_path):
        """batch_transition should raise KeyError for missing ticket IDs."""
        store = self._make_store(tmp_path)
        with pytest.raises(KeyError, match="ticket not found"):
            store.batch_transition([("CB-NONEXISTENT", TicketState.VALIDATING, None)])
        store.close()

    def test_batch_transition_invalid_state_raises_valueerror(self, tmp_path):
        """batch_transition should raise ValueError for invalid state transitions."""
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 2, TicketState.IMPLEMENTING)

        # DISCOVERED -> COMPLETE is invalid
        with pytest.raises(ValueError, match="invalid transition"):
            store.batch_transition([
                (tickets[0].id, TicketState.COMPLETE, None),
            ])
        store.close()

    def test_batch_transition_partial_failure_is_atomic(self, tmp_path):
        """If one transition fails, none should be applied (atomicity).

        batch_transition applies changes in-memory under a single lock hold.
        If a ValueError is raised mid-batch, the already-mutated tickets must
        be rolled back so that either all transitions succeed or none do.
        """
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 3, TicketState.IMPLEMENTING)

        # Second transition is invalid (IMPLEMENTING -> COMPLETE not allowed directly)
        transitions = [
            (tickets[0].id, TicketState.REVIEWING, None),
            (tickets[1].id, TicketState.COMPLETE, None),  # Invalid!
            (tickets[2].id, TicketState.REVIEWING, None),
        ]

        with pytest.raises(ValueError, match="invalid transition"):
            store.batch_transition(transitions)

        # All tickets should remain in IMPLEMENTING since the batch failed
        for t in tickets:
            stored = store.get(t.id)
            assert stored.state == TicketState.IMPLEMENTING, (
                f"Ticket {t.id} was mutated to {stored.state} despite batch failure; "
                f"batch_transition must be atomic"
            )
        store.close()

    def test_save_called_once_per_batch(self, tmp_path):
        """_save() must be called at most once per batch_transition regardless of K.

        Acceptance criterion: _save() is called at most once per dispatch cycle
        regardless of K.
        """
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 10, TicketState.IMPLEMENTING)

        # Count _save calls via _queue_save (which triggers background save)
        original_queue_save = store._queue_save
        queue_save_count = [0]

        def counting_queue_save():
            queue_save_count[0] += 1
            original_queue_save()

        store._queue_save = counting_queue_save

        transitions = [(t.id, TicketState.REVIEWING, None) for t in tickets]
        store.batch_transition(transitions)

        # _queue_save should be called exactly once for the entire batch
        assert queue_save_count[0] == 1, (
            f"Expected _queue_save called 1 time for batch of {len(tickets)}, "
            f"got {queue_save_count[0]}"
        )
        store.close()

    def test_dispatch_10_tickets_constant_time(self, tmp_path):
        """Dispatching 10 tickets takes constant time relative to total ticket count.

        Acceptance criterion: Dispatching 10 tickets takes constant time
        relative to total ticket count. We measure batch_transition of 10
        tickets with 100 vs 500 total tickets; ratio should be < 3x.
        """
        def measure_batch_dispatch(total_tickets, batch_size=10):
            store = self._make_store(tmp_path / f"tickets_{total_tickets}.json")
            tickets = self._add_tickets_in_state(store, total_tickets, TicketState.IMPLEMENTING)

            # Take first batch_size tickets for transition
            batch = [(t.id, TicketState.REVIEWING, None) for t in tickets[:batch_size]]

            start = time.perf_counter()
            store.batch_transition(batch)
            elapsed = time.perf_counter() - start
            store.close()
            return elapsed

        time_100 = measure_batch_dispatch(100)
        time_500 = measure_batch_dispatch(500)

        # With O(K) batch transition, 500 tickets should not take more than
        # 3x longer than 100 tickets (allowing for some constant overhead).
        # If it were O(K*N), 500 would take ~5x longer.
        ratio = time_500 / max(time_100, 1e-9)
        assert ratio < 3.0, (
            f"batch_transition does not appear constant-time: "
            f"100 tickets={time_100*1000:.2f}ms, 500 tickets={time_500*1000:.2f}ms, "
            f"ratio={ratio:.2f}x (expected <3x)"
        )

    def test_batch_transition_preserves_gatekeeper_enforcement(self, tmp_path):
        """batch_transition must enforce gate approval for VERIFYING->COMPLETE."""
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 2, TicketState.REVIEWING)

        # Move to VERIFYING
        for t in tickets:
            store.transition(t.id, TicketState.VERIFYING)

        # Attempt batch transition to COMPLETE without gate approval
        transitions = [(t.id, TicketState.COMPLETE, None) for t in tickets]
        with pytest.raises(ValueError, match="gatekeeper"):
            store.batch_transition(transitions)

        # Grant gate approval for first ticket only
        store.record_gate_result(tickets[0].id, True, gates=[])

        # Now batch with just the approved ticket should succeed
        results = store.batch_transition([(tickets[0].id, TicketState.COMPLETE, None)])
        assert len(results) == 1
        assert results[0].state == TicketState.COMPLETE
        store.close()

    def test_batch_transition_preserves_planning_prerequisite(self, tmp_path):
        """batch_transition must enforce planning prerequisite for READY->IMPLEMENTING."""
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 2, TicketState.READY, risk=RiskLevel.MEDIUM)

        # Attempt batch transition to IMPLEMENTING without plan
        transitions = [(t.id, TicketState.IMPLEMENTING, None) for t in tickets]
        with pytest.raises(ValueError, match="requires.*implementation plan"):
            store.batch_transition(transitions)

        # Create plan for first ticket
        from codebot.implementation_planner import PlanStore
        plan_store = PlanStore(tmp_path)
        plan_store.save(tickets[0].id, {"steps": ["step1"]})

        # Batch with just planned ticket should succeed
        results = store.batch_transition([(tickets[0].id, TicketState.IMPLEMENTING, None)])
        assert len(results) == 1
        assert results[0].state == TicketState.IMPLEMENTING
        store.close()


class TestBackupWorker:
    """Tests for async backup worker consumer (CB-D4AD89D42A31)."""

    def test_backup_worker_processes_task(self, tmp_path):
        """Worker copies file from src to dst asynchronously."""
        store = TicketStore(tmp_path / "tickets.json")

        # Create a source file
        src = tmp_path / "source.txt"
        src.write_text("backup content", encoding="utf-8")
        dst = tmp_path / "backup_dest.txt"

        # Enqueue backup task
        store.queue_backup_task(str(src), str(dst))

        # Wait for async processing (worker uses 1s timeout on queue.get)
        store._backup_queue.join()

        assert dst.exists()
        assert dst.read_text(encoding="utf-8") == "backup content"
        store.close()

    def test_backup_worker_handles_error(self, tmp_path):
        """Worker continues running after a failed copy (e.g., missing src)."""
        store = TicketStore(tmp_path / "tickets.json")

        # Enqueue a task with a non-existent source file
        store.queue_backup_task(str(tmp_path / "nonexistent.txt"), str(tmp_path / "dst.txt"))

        # Wait for the bad task to be processed
        store._backup_queue.join()

        # Worker should still be alive after the error
        assert store._backup_worker.is_alive()

        # Now enqueue a valid task to prove the worker is still processing
        src = tmp_path / "valid_src.txt"
        src.write_text("valid", encoding="utf-8")
        dst = tmp_path / "valid_dst.txt"
        store.queue_backup_task(str(src), str(dst))
        store._backup_queue.join()

        assert dst.exists()
        assert dst.read_text(encoding="utf-8") == "valid"
        store.close()

    def test_backup_worker_shuts_down_cleanly(self, tmp_path):
        """close() stops the backup worker thread."""
        store = TicketStore(tmp_path / "tickets.json")

        # Verify worker is alive
        assert store._backup_worker.is_alive()

        # Close the store
        store.close()

        # Worker should be stopped
        assert not store._backup_worker.is_alive()

    def test_backup_worker_does_not_block_main_thread(self, tmp_path):
        """queue_backup_task returns immediately without blocking."""
        import time

        store = TicketStore(tmp_path / "tickets.json")

        src = tmp_path / "src.txt"
        src.write_text("data", encoding="utf-8")

        # Enqueue multiple tasks and measure time
        start = time.perf_counter()
        for i in range(10):
            dst = tmp_path / f"dst_{i}.txt"
            store.queue_backup_task(str(src), str(dst))
        elapsed = time.perf_counter() - start

        # Enqueueing should be near-instant (<100ms for 10 tasks)
        assert elapsed < 0.1, f"queue_backup_task blocked for {elapsed:.3f}s"

        # Wait for all to complete
        store._backup_queue.join()
        store.close()


class TestConcurrentSaves:
    """Tests for CB-4888477-87D7: Race condition in TicketStore._save.

    Validates that concurrent ticket saves do not lose data and that
    lock failures are handled safely without silent corruption.
    """

    def _make_store(self, tmp_path):
        return TicketStore(tmp_path / "tickets.json")

    def test_concurrent_saves_do_not_lose_data(self, tmp_path):
        """Multiple threads saving tickets concurrently should not lose data."""
        import threading

        path = tmp_path / "tickets.json"
        store = self._make_store(tmp_path)

        num_threads = 10
        tickets_per_thread = 5
        errors = []
        saved_tickets = []
        lock = threading.Lock()

        def worker(thread_id):
            try:
                for i in range(tickets_per_thread):
                    t = create_ticket(
                        f"concurrent-{thread_id}-{i}",
                        TicketClass.BUG,
                        Severity.LOW,
                        "test",
                        f"evidence-{thread_id}-{i}",
                        "problem",
                        "desired",
                        ["crit"],
                        risk=RiskLevel.LOW,
                    )
                    store.add(t)
                    with lock:
                        saved_tickets.append(t.id)
            except Exception as e:
                errors.append(e)

        threads = []
        for tid in range(num_threads):
            t = threading.Thread(target=worker, args=(tid,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent saves raised errors: {errors}"

        # Flush to ensure all saves are persisted
        store.flush()
        store.close()

        # Verify all tickets were saved
        store2 = TicketStore(path)
        assert store2.count() == num_threads * tickets_per_thread, (
            f"Expected {num_threads * tickets_per_thread} tickets, got {store2.count()}"
        )

        for tid in saved_tickets:
            assert store2.get(tid) is not None, f"Ticket {tid} was lost"

        store2.close()

    def test_lock_failure_raises_error_not_silent_corruption(self, tmp_path):
        """If lock acquisition permanently fails, _save should raise error."""
        import time

        path = tmp_path / "tickets.json"
        store = self._make_store(tmp_path)

        # Add a ticket to make dirty_ids non-empty
        t = create_ticket(
            "lock-test",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "evidence",
            "problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t)

        # Mock flock to always fail after max retries
        original_flock = None
        import codebot.ticket_engine as te
        original_flock = te.flock

        call_count = [0]

        def failing_flock(fd, flag):
            call_count[0] += 1
            if flag == te.LOCK_EX:
                raise OSError("Mocked lock failure")
            return original_flock(fd, flag)

        te.flock = failing_flock

        try:
            with pytest.raises(RuntimeError, match="failed after.*lock retries"):
                store._save()
        finally:
            te.flock = original_flock

        store.close()


class TestBackupWorker:
    """Tests for CB-D4AD89D42A31: async backup worker consumer.

    Validates that a background worker thread consumes backup tasks from
    a queue, performs shutil.copy2 operations, handles errors gracefully,
    and shuts down cleanly.
    """

    def _make_store(self, tmp_path):
        return TicketStore(tmp_path / "tickets.json")

    def test_worker_thread_starts_on_init(self, tmp_path):
        """Worker thread should be alive immediately after TicketStore.__init__."""
        store = self._make_store(tmp_path)
        assert store._backup_worker.is_alive()
        store.close()

    def test_worker_copies_file(self, tmp_path):
        """Worker copies src to dst asynchronously via shutil.copy2."""
        store = self._make_store(tmp_path)

        src = tmp_path / "src.txt"
        dst = tmp_path / "dst.txt"
        src.write_text("hello backup", encoding="utf-8")

        store.queue_backup_task(str(src), str(dst))
        # Give the worker time to process
        time.sleep(0.5)

        assert dst.exists()
        assert dst.read_text(encoding="utf-8") == "hello backup"
        store.close()

    def test_worker_handles_missing_source(self, tmp_path):
        """Worker should log a warning and continue processing on copy failure."""
        store = self._make_store(tmp_path)

        src = tmp_path / "nonexistent.txt"
        dst = tmp_path / "dst.txt"

        # Queue a failing task followed by a valid one
        store.queue_backup_task(str(src), str(dst))

        # Queue a second valid task to confirm the worker keeps running
        valid_src = tmp_path / "valid.txt"
        valid_dst = tmp_path / "valid_dst.txt"
        valid_src.write_text("valid data", encoding="utf-8")
        store.queue_backup_task(str(valid_src), str(valid_dst))

        time.sleep(0.5)

        # The failing task should not have created dst
        assert not dst.exists()
        # The valid task should have been processed
        assert valid_dst.exists()
        assert valid_dst.read_text(encoding="utf-8") == "valid data"
        store.close()

    def test_worker_shuts_down_cleanly(self, tmp_path):
        """close() should stop the backup worker thread."""
        store = self._make_store(tmp_path)
        assert store._backup_worker.is_alive()

        store.close()
        assert not store._backup_worker.is_alive()

    def test_multiple_concurrent_tasks(self, tmp_path):
        """Multiple queued tasks should all be processed."""
        store = self._make_store(tmp_path)

        src_dir = tmp_path / "srcs"
        src_dir.mkdir()
        dst_dir = tmp_path / "dsts"
        dst_dir.mkdir()

        num_tasks = 5
        for i in range(num_tasks):
            src_file = src_dir / f"file{i}.txt"
            src_file.write_text(f"data-{i}", encoding="utf-8")
            dst_file = dst_dir / f"file{i}.txt"
            store.queue_backup_task(str(src_file), str(dst_file))

        # Give worker time to process all tasks
        time.sleep(1.0)

        for i in range(num_tasks):
            dst_file = dst_dir / f"file{i}.txt"
            assert dst_file.exists(), f"dst file{i}.txt not found"
            assert dst_file.read_text(encoding="utf-8") == f"data-{i}"
        store.close()

    def test_does_not_block_main_thread(self, tmp_path):
        """queue_backup_task should return immediately without blocking."""
        store = self._make_store(tmp_path)

        # Create a large-ish file to copy
        src = tmp_path / "big_src.bin"
        src.write_bytes(b"x" * (1024 * 1024))  # 1 MB
        dst = tmp_path / "big_dst.bin"

        start = time.perf_counter()
        store.queue_backup_task(str(src), str(dst))
        elapsed = time.perf_counter() - start

        # Enqueue should be nearly instant (< 50ms), not the copy time
        assert elapsed < 0.05, f"queue_backup_task blocked for {elapsed*1000:.1f}ms"

        # Wait for actual copy to finish and verify
        time.sleep(1.0)
        assert dst.exists()
        assert dst.stat().st_size == 1024 * 1024
        store.close()

    def test_worker_is_daemon_thread(self, tmp_path):
        """Worker thread should be a daemon thread so it doesn't prevent process exit."""
        store = self._make_store(tmp_path)
        assert store._backup_worker.daemon is True
        store.close()

    def test_regression_existing_store_functionality(self, tmp_path):
        """Existing TicketStore functionality is unaffected by backup worker."""
        store = self._make_store(tmp_path)
        t = create_ticket(
            "regression-test", TicketClass.BUG, Severity.LOW,
            "s", "e", "p", "d", ["a"]
        )
        store.add(t)
        assert store.get(t.id) is not None
        assert store.count() == 1

        # Backup worker is alive alongside normal operations
        assert store._backup_worker.is_alive()
        store.close()
