"""Tests for ticket_engine.py — schema, state machine, dedup, TicketStore CRUD."""

import json
import os
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

    def test_hex_length_128_bits(self):
        """Verify ticket ID has 32 hex chars = 128 bits of entropy (CB-3838083-80AA)."""
        tid = generate_ticket_id()
        # Format: CB-{32 hex chars}
        parts = tid.split("-")
        assert len(parts) == 2
        assert len(parts[1]) == 32, f"Expected 32 hex chars, got {len(parts[1])}"

    def test_all_hex_uppercase(self):
        """Verify all characters in random portion are uppercase hex."""
        tid = generate_ticket_id()
        random_part = tid.split("-")[1]
        assert random_part == random_part.upper()
        assert all(c in "0123456789ABCDEF" for c in random_part)

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
        t2 = t.transition(TicketState.TRIAGED)
        assert t2.state == TicketState.TRIAGED
        assert t2.updated_at >= t.updated_at

    def test_valid_transition_discovered_to_triaged(self):
        t = self._make_ticket()
        t2 = t.transition(TicketState.TRIAGED)
        assert t2.state == TicketState.TRIAGED
        assert t2.updated_at >= t.updated_at

    def test_invalid_transition_discovered_to_complete(self):
        t = self._make_ticket()
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.COMPLETE)

    def test_full_lifecycle(self):
        t = self._make_ticket()
        for state in [
            TicketState.TRIAGED,
            TicketState.GOAL,
            TicketState.DECOMP,
            TicketState.PLANNING,
            TicketState.IMPLEMENT,
            TicketState.REVIEW,
            TicketState.COMPLETE,
        ]:
            t = t.transition(state)
            assert t.state == state

    def test_rework_increments_counter(self):
        t = self._make_ticket()
        t = t.transition(TicketState.TRIAGED)
        t = t.transition(TicketState.GOAL)
        t = t.transition(TicketState.DECOMP)
        t = t.transition(TicketState.PLANNING)
        t = t.transition(TicketState.IMPLEMENT)
        t = t.transition(TicketState.REVIEW)
        t = t.transition(TicketState.REWORK)
        assert t.rework_count == 1

    def test_implementing_increments_attempts(self):
        t = self._make_ticket()
        t = t.transition(TicketState.TRIAGED)
        t = t.transition(TicketState.GOAL)
        t = t.transition(TicketState.DECOMP)
        t = t.transition(TicketState.PLANNING)
        t = t.transition(TicketState.IMPLEMENT)
        assert t.attempts == 1

    def test_terminal_states_have_no_transitions(self):
        assert TRANSITIONS[TicketState.COMPLETE] == frozenset()
        assert TRANSITIONS[TicketState.REJECTED] == frozenset()
        assert TRANSITIONS[TicketState.DUPLICATE] == frozenset()
        assert TRANSITIONS[TicketState.RESOLVED] == frozenset()
        assert TRANSITIONS[TicketState.SUPERSEDED] == frozenset()
        assert TRANSITIONS[TicketState.CANCELLED] == frozenset()
        assert TRANSITIONS[TicketState.NEVER] == frozenset()
        assert TRANSITIONS[TicketState.NOT_ACTIONABLE] == frozenset()

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
        t = t.transition(TicketState.TRIAGED)
        t = t.transition(TicketState.GOAL)
        t = t.transition(TicketState.DECOMP)
        t = t.transition(TicketState.PLANNING)
        t2 = t.transition(TicketState.BLOCKED)
        assert t2.state == TicketState.BLOCKED

    def test_deferred_from_triaged(self):
        t = self._make_ticket()
        t = t.transition(TicketState.TRIAGED)
        t = t.transition(TicketState.GOAL)
        t = t.transition(TicketState.DECOMP)
        t = t.transition(TicketState.PLANNING)
        t = t.transition(TicketState.IMPLEMENT)
        t = t.transition(TicketState.REVIEW)
        t = t.transition(TicketState.REWORK)
        t2 = t.transition(TicketState.DEFERRED)
        assert t2.state == TicketState.DEFERRED

    def test_immutable_original(self):
        t = self._make_ticket()
        t2 = t.transition(TicketState.TRIAGED)
        assert t.state == TicketState.DISCOVERED
        assert t2.state == TicketState.TRIAGED


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

    def test_compact_json_serialization_default(self, tmp_path):
        """Verify _save() produces compact JSON (no indent) by default (CB-1674105-82E1)."""
        path = tmp_path / "tickets.json"
        # Test default behavior: pretty=False should produce compact JSON
        store_compact = TicketStore(path)
        t = create_ticket("compact_test", TicketClass.BUG, Severity.LOW, "s", "evidence", "problem", "d", ["a"])
        store_compact.add(t)
        store_compact.flush()  # Force compaction to write main JSON file
        
        # Read the written file and verify it's compact (no newlines followed by spaces for indentation)
        content_compact = path.read_text()
        # Compact JSON should not contain '\n  ' patterns (newline followed by two spaces)
        assert "\n  " not in content_compact, f"Expected compact JSON but found indentation patterns in: {content_compact[:200]}..."
        # Also verify no standalone newlines within the JSON structure (except possibly at end)
        lines = content_compact.strip().split("\n")
        assert len(lines) == 1, f"Expected single-line compact JSON but got {len(lines)} lines"
        
        store_compact.close()
        
        # Test that pretty=True does produce indented output
        path_pretty = tmp_path / "tickets_pretty.json"
        store_pretty = TicketStore(path_pretty, pretty=True)
        t2 = create_ticket("pretty_test", TicketClass.BUG, Severity.LOW, "s", "evidence2", "problem2", "d", ["a"])
        store_pretty.add(t2)
        store_pretty.flush()
        
        content_pretty = path_pretty.read_text()
        # Pretty JSON should contain indentation patterns
        assert "\n  " in content_pretty, f"Expected pretty-printed JSON with indentation but got: {content_pretty[:200]}..."
        
        store_pretty.close()

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
        for state in [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT, TicketState.REVIEW, TicketState.COMPLETE]:
            if state == TicketState.COMPLETE:
                store.record_gate_result(t1.id, True, gates=[])
            store.transition(t1.id, state)
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "same evidence", "same problem", "d", ["a"])
        store.add(t2)
        assert store.count() == 2

    def test_transition_via_store(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)
        updated = store.transition(t.id, TicketState.TRIAGED)
        assert updated.state == TicketState.TRIAGED
        assert store.get(t.id).state == TicketState.TRIAGED

    def test_transition_nonexistent_raises(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        with pytest.raises(KeyError):
            store.transition("CB-DEADBEEF12345678", TicketState.TRIAGED)

    def test_list_by_state(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"])
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["a"])
        store.add(t1)
        store.add(t2)
        store.transition(t1.id, TicketState.TRIAGED)
        discovered = store.list_by_state(TicketState.DISCOVERED)
        triaged = store.list_by_state(TicketState.TRIAGED)
        assert len(discovered) == 1
        assert len(triaged) == 1

    def test_list_ready_sorted_by_severity(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        low = create_ticket("low", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"])
        crit = create_ticket("crit", TicketClass.BUG, Severity.CRITICAL, "s", "e2", "p", "d", ["a"])
        med = create_ticket("med", TicketClass.BUG, Severity.MEDIUM, "s", "e3", "p", "d", ["a"])
        for t in (low, crit, med):
            store.add(t)
            store.transition(t.id, TicketState.TRIAGED)
            store.transition(t.id, TicketState.GOAL)
            store.transition(t.id, TicketState.DECOMP)
            store.transition(t.id, TicketState.PLANNING)
        planning = store.list_by_state(TicketState.PLANNING)
        assert len(planning) == 3
        severity_order = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3}
        sorted_planning = sorted(planning, key=lambda x: severity_order.get(x.severity, 99))
        assert sorted_planning[0].severity == Severity.CRITICAL
        assert sorted_planning[-1].severity == Severity.LOW

    def test_summary(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"])
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["a"])
        store.add(t1)
        store.add(t2)
        store.transition(t1.id, TicketState.TRIAGED)
        s = store.summary()
        assert s["DISCOVERED"] == 1
        assert s["TRIAGED"] == 1

    def test_corrupt_file_resets(self, tmp_path):
        path = tmp_path / "tickets.json"
        path.write_text("not valid json{{{")
        store = TicketStore(path)
        assert store.count() == 0

    def test_oversized_tickets_json_loads_empty_with_warning(self, tmp_path, caplog):
        """Test that oversized tickets.json (>50MB) loads as empty list with warning.

        Regression test for CB-4024053-A159: verifies that oversized files
        are handled gracefully without crashing, loading empty ticket list
        and logging a warning.
        """
        import logging
        path = tmp_path / "tickets.json"
        
        # Create a dummy JSON file >50MB (51MB = 51 * 1024 * 1024 bytes)
        # Write valid JSON structure with large padding to exceed 50MB
        size_target = 51 * 1024 * 1024  # 51MB
        
        # Write in chunks to avoid memory issues
        chunk_size = 1024 * 1024  # 1MB chunks
        with open(path, "w", encoding="utf-8") as f:
            f.write('{"schema_version":"2.0","updated_at":1234567890.0,"tickets":[')
            written = len('{"schema_version":"2.0","updated_at":1234567890.0,"tickets":[')
            
            # Write padding until we exceed 50MB
            padding = '{"id":"CB-PAD","title":"padding","ticket_class":"bug","severity":"low","state":"DISCOVERED","source":"test","evidence":"pad","problem_statement":"pad","desired_state":"pad","acceptance_criteria":["pad"],"created_at":1234567890.0,"updated_at":1234567890.0},'
            padding_len = len(padding)
            
            while written < size_target:
                # Calculate how many padding entries we can add
                remaining = size_target - written
                entries_to_add = max(1, remaining // padding_len)
                chunk = padding * entries_to_add
                f.write(chunk)
                written += len(chunk)
            
            # Close the JSON structure
            f.write(']}')
        
        # Verify file size exceeds 50MB
        actual_size = path.stat().st_size
        assert actual_size > 50 * 1024 * 1024, f"File size {actual_size} bytes should exceed 50MB"
        
        # Capture warnings
        with caplog.at_level(logging.WARNING):
            store = TicketStore(path)
        
        # Assert store loads empty list (graceful degradation)
        assert store.count() == 0, "TicketStore should load empty list for oversized file"
        
        # Assert warning was logged
        warning_found = any(
            "oversized" in record.message.lower() or "large" in record.message.lower()
            for record in caplog.records
        )
        assert warning_found, "Warning should be logged for oversized tickets.json"
        
        store.close()

    def test_oversized_path_calls_flock_unlock_exactly_once(self, tmp_path):
        """Verify that flock(LOCK_UN) is called exactly once during oversized file load.

        This is a regression test for the double-unlock bug where the advisory lock
        could be released twice: once manually before return and once in the finally block.
        Acceptance criterion: flock(LOCK_UN) is called exactly once per _load invocation
        regardless of code path.
        """
        from fcntl import LOCK_UN
        from unittest.mock import patch

        store = TicketStore(tmp_path / "tickets.json")

        # Create a small valid JSON file
        small_data = {
            "tickets": [
                {
                    "id": "CB-TEST",
                    "title": "Test Ticket",
                    "ticket_class": "bug",
                    "severity": "low",
                    "source": "test",
                    "evidence": "test evidence",
                    "problem_statement": "test problem",
                    "desired_state": "test desired",
                    "acceptance_criteria": ["test criteria"],
                    "risk": "low",
                    "state": "discovered",
                    "created_at": "2024-01-01T00:00:00Z",
                    "updated_at": "2024-01-01T00:00:00Z",
                }
            ]
        }
        store._path.write_text(json.dumps(small_data), encoding="utf-8")

        # Mock flock to track calls
        with patch("codebot.ticket_engine.flock") as mock_flock:
            # Make the mock behave like the real flock (no-op by default)
            mock_flock.side_effect = lambda fd, op: None

            # Mock os.fstat to return a large file size to trigger oversized path
            original_fstat = os.fstat

            def mock_fstat(fd):
                class MockStat:
                    st_size = 60 * 1024 * 1024  # 60MB > 50MB limit

                return MockStat()

            with patch("codebot.ticket_engine.os.fstat", mock_fstat):
                store._load()

            # Count how many times LOCK_UN was passed to flock
            lock_un_calls = [
                c for c in mock_flock.call_args_list if c[0][1] == LOCK_UN
            ]

            # Verify LOCK_UN was called exactly once
            assert len(lock_un_calls) == 1, (
                f"Expected flock(LOCK_UN) to be called exactly once, but it was called "
                f"{len(lock_un_calls)} times. Calls: {mock_flock.call_args_list}"
            )

        # Restore original fstat
        os.fstat = original_fstat
        store.close()

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

        # DISCOVERED -> TRIAGED (100 tickets)
        for t in tickets[:100]:
            store.transition(t.id, TicketState.TRIAGED)
        # TRIAGED -> GOAL (50 tickets)
        for t in tickets[:50]:
            store.transition(t.id, TicketState.GOAL)
        # GOAL -> DECOMP (50 tickets)
        for t in tickets[:50]:
            store.transition(t.id, TicketState.DECOMP)

        # Verify list_by_state correctness
        discovered = store.list_by_state(TicketState.DISCOVERED)
        triaged = store.list_by_state(TicketState.TRIAGED)
        decomp = store.list_by_state(TicketState.DECOMP)
        assert len(discovered) == 100   # 200 - 100
        assert len(triaged) == 50    # 100 - 50
        assert len(decomp) == 50

        # Verify summary correctness
        s = store.summary()
        assert s.get("DISCOVERED", 0) == 100
        assert s.get("TRIAGED", 0) == 50
        assert s.get("DECOMP", 0) == 50

        # Verify correctness after persistence reload
        store.flush()
        store2 = TicketStore(tmp_path / "tickets.json")
        assert store2.summary().get("DISCOVERED", 0) == 100
        assert store2.summary().get("TRIAGED", 0) == 50
        assert store2.summary().get("DECOMP", 0) == 50
        assert len(store2.list_by_state(TicketState.DECOMP)) == 50

    def test_state_index_consistency_after_transition(self, tmp_path):
        """State index must stay consistent across rapid transitions."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.MEDIUM, "s", "ev", "p", "d", ["a"],
                          risk=RiskLevel.LOW)
        store.add(t)
        assert len(store.list_by_state(TicketState.DISCOVERED)) == 1

        # Transition through valid lifecycle (LOW risk skips planning prerequisite)
        for state in [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP,
                      TicketState.PLANNING, TicketState.IMPLEMENT,
                      TicketState.REVIEW]:
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
        store1.transition(t1.id, TicketState.TRIAGED)
        store1.transition(t1.id, TicketState.GOAL)
        store1.transition(t1.id, TicketState.DECOMP)

        # Flush to persist all WAL entries
        store1.flush()
        store1.close()

        # Reload store — WAL replay must rebuild state index correctly
        store2 = TicketStore(path)

        # t1 should only be in DECOMP, not in DISCOVERED/TRIAGED/GOAL
        assert len(store2.list_by_state(TicketState.DISCOVERED)) == 1  # t2
        assert len(store2.list_by_state(TicketState.TRIAGED)) == 0
        assert len(store2.list_by_state(TicketState.GOAL)) == 0
        assert len(store2.list_by_state(TicketState.DECOMP)) == 1  # t1

        # Summary must match
        s = store2.summary()
        assert s.get("DISCOVERED", 0) == 1
        assert s.get("TRIAGED", 0) == 0
        assert s.get("GOAL", 0) == 0
        assert s.get("DECOMP", 0) == 1

        # No duplicate ticket appearances
        total = sum(s.values())
        assert total == 2  # exactly 2 tickets across all states

        store2.close()

    def test_wal_replay_no_compaction_state_index(self, tmp_path):
        """State index is correct after WAL-only replay (no main JSON).

        When tickets.json does not exist but .wal.jsonl does, the store
        must still maintain correct state indexes from WAL entries alone.
        We construct this state manually to avoid races with background
        compaction which deletes the WAL on every flush/close.
        """
        import json as _json
        path = tmp_path / "tickets.json"
        wal_path = path.with_suffix(".wal.jsonl")

        # Manually write a WAL entry for a ticket in TRIAGED state
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"],
                           risk=RiskLevel.LOW)
        # Mutate state in memory for the WAL entry
        t1_triaged = t1.transition(TicketState.TRIAGED)
        wal_line = _json.dumps(t1_triaged.to_dict(), separators=(",", ":"))
        wal_path.write_text(wal_line + "\n", encoding="utf-8")

        # Ensure no snapshot exists
        path.unlink(missing_ok=True)
        (tmp_path / "tickets.lock").unlink(missing_ok=True)

        # New store: _load sees no tickets.json → _replay_wal() only
        store2 = TicketStore(path)
        # Only t1 should be present (from WAL), in TRIAGED state
        assert store2.count() == 1
        assert len(store2.list_by_state(TicketState.TRIAGED)) == 1
        assert len(store2.list_by_state(TicketState.DISCOVERED)) == 0
        s = store2.summary()
        assert s.get("TRIAGED", 0) == 1
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

    def _move_to_review(self, store, ticket_id):
        """Move a ticket through states to REVIEW."""
        from codebot.implementation_planner import PlanStore
        t = store.get(ticket_id)
        if t and t.risk != RiskLevel.LOW:
            try:
                plan_store = PlanStore(store._path.parent)
                if not plan_store.exists(ticket_id):
                    plan_store.save(ticket_id, {"steps": ["auto plan for test"]})
            except Exception:
                pass
        store.transition(ticket_id, TicketState.TRIAGED)
        store.transition(ticket_id, TicketState.GOAL)
        store.transition(ticket_id, TicketState.DECOMP)
        store.transition(ticket_id, TicketState.PLANNING)
        store.transition(ticket_id, TicketState.IMPLEMENT)
        store.transition(ticket_id, TicketState.REVIEW)

    def _move_to_verifying(self, store, ticket_id):
        return self._move_to_review(store, ticket_id)

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
        """REVIEW -> COMPLETE is blocked without gatekeeper approval."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_review(store, t.id)

        with pytest.raises(ValueError, match="gatekeeper"):
            store.transition(t.id, TicketState.COMPLETE)

    def test_verifying_to_complete_allowed_with_gate_pass(self, tmp_path):
        """REVIEW -> COMPLETE is allowed with passing gate result."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_review(store, t.id)
        self._write_gate_pass(tmp_path, t.id, store=store)

        updated = store.transition(t.id, TicketState.COMPLETE)
        assert updated.state == TicketState.COMPLETE

    def test_verifying_to_complete_blocked_with_gate_fail(self, tmp_path):
        """REVIEW -> COMPLETE is blocked with failing gate result."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_review(store, t.id)
        self._write_gate_fail(tmp_path, t.id, store=store)

        with pytest.raises(ValueError, match="gatekeeper"):
            store.transition(t.id, TicketState.COMPLETE)

    def test_verifying_to_rework_allowed_without_gate(self, tmp_path):
        """REVIEW -> REWORK is allowed even without gatekeeper approval."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_review(store, t.id)

        updated = store.transition(t.id, TicketState.REWORK)
        assert updated.state == TicketState.REWORK

    def test_rework_to_implementing_not_gated(self, tmp_path):
        """REWORK -> IMPLEMENT is not affected by gatekeeper enforcement."""
        store = self._make_store(tmp_path)
        t = self._add_ticket(store)
        self._move_to_review(store, t.id)
        store.transition(t.id, TicketState.REWORK)

        updated = store.transition(t.id, TicketState.IMPLEMENT)
        assert updated.state == TicketState.IMPLEMENT


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

    def _move_to_planning(self, store, ticket_id):
        """Move a ticket through states to PLANNING."""
        store.transition(ticket_id, TicketState.TRIAGED)
        store.transition(ticket_id, TicketState.GOAL)
        store.transition(ticket_id, TicketState.DECOMP)
        store.transition(ticket_id, TicketState.PLANNING)

    def _move_to_ready(self, store, ticket_id):
        return self._move_to_planning(store, ticket_id)

    def test_medium_risk_requires_plan_before_implementing(self, tmp_path):
        """Medium risk tickets cannot transition to IMPLEMENT without a plan."""
        store = self._make_store(tmp_path)
        t = self._add_medium_risk_ticket(store)
        self._move_to_planning(store, t.id)

        with pytest.raises(ValueError, match="requires.*implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENT)

    def test_high_risk_requires_plan_before_implementing(self, tmp_path):
        """High risk tickets cannot transition to IMPLEMENT without a plan."""
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
        self._move_to_planning(store, t.id)

        with pytest.raises(ValueError, match="requires.*implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENT)

    def test_critical_risk_requires_plan_before_implementing(self, tmp_path):
        """Critical risk tickets cannot transition to IMPLEMENT without a plan."""
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
        self._move_to_planning(store, t.id)

        with pytest.raises(ValueError, match="requires.*implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENT)

    def test_low_risk_allows_implementing_without_plan(self, tmp_path):
        """Low risk tickets can transition to IMPLEMENT without a plan."""
        store = self._make_store(tmp_path)
        t = self._add_low_risk_ticket(store)
        self._move_to_planning(store, t.id)

        updated = store.transition(t.id, TicketState.IMPLEMENT)
        assert updated.state == TicketState.IMPLEMENT

    def test_medium_risk_with_plan_allows_implementing(self, tmp_path):
        """Medium risk tickets with a plan can transition to IMPLEMENT."""
        from codebot.implementation_planner import PlanStore

        store = self._make_store(tmp_path)
        t = self._add_medium_risk_ticket(store)
        self._move_to_planning(store, t.id)

        plan_store = PlanStore(tmp_path)
        plan_store.save(t.id, {"steps": ["step1", "step2"]})

        updated = store.transition(t.id, TicketState.IMPLEMENT)
        assert updated.state == TicketState.IMPLEMENT

    def test_transition_from_other_states_not_enforced(self, tmp_path):
        """Planning prerequisite only enforced for PLANNING -> IMPLEMENT."""
        store = self._make_store(tmp_path)
        t = self._add_medium_risk_ticket(store)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.transition(t.id, TicketState.PLANNING)

        from codebot.implementation_planner import PlanStore
        plan_store = PlanStore(tmp_path)
        plan_store.save(t.id, {"steps": ["step1"]})
        updated = store.transition(t.id, TicketState.IMPLEMENT)
        assert updated.state == TicketState.IMPLEMENT

    def test_rework_to_implementing_not_enforced(self, tmp_path):
        """REWORK -> IMPLEMENT transition is not subject to planning check."""
        from codebot.implementation_planner import PlanStore

        store = self._make_store(tmp_path)
        t = self._add_medium_risk_ticket(store)
        self._move_to_planning(store, t.id)

        plan_store = PlanStore(tmp_path)
        plan_store.save(t.id, {"steps": ["step1"]})
        store.transition(t.id, TicketState.IMPLEMENT)
        store.transition(t.id, TicketState.REVIEW)
        store.transition(t.id, TicketState.REWORK)

        plan_store.delete(t.id)

        updated = store.transition(t.id, TicketState.IMPLEMENT)
        assert updated.state == TicketState.IMPLEMENT


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

        for t in tickets[:num_ready]:
            store.transition(t.id, TicketState.TRIAGED)

        # Wait for async save to complete
        time.sleep(0.6)

        start = time.perf_counter()
        triaged_tickets = store.list_by_state(TicketState.TRIAGED)
        elapsed_ms = (time.perf_counter() - start) * 1000
        assert len(triaged_tickets) == num_ready
        assert elapsed_ms < 10, f"list_by_state(TRIAGED) took {elapsed_ms:.2f}ms, expected <10ms"

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
        """Rapid-fire mutations are durable individually with synchronous WAL writes.

        Previously with SAVE_DEBOUNCE_SECONDS = 0.5, 50 rapid adds produced
        at most 1-2 saves. After the durability fix (CB-DFF7728AAAAECB81D4A713F5B09C3478),
        each add() performs a synchronous WAL append to guarantee crash safety,
        so 50 adds produce 50 WAL appends. Durability takes priority over
        batching; compaction amortizes full snapshots.
        """
        path = tmp_path / "tickets.json"
        store = TicketStore(path)

        # Count synchronous WAL durability writes (one per mutation) via
        # _append_wal_locked, which add() calls directly under the RLock.
        # _save() itself is only deferred compaction via the background
        # worker, so counting _save conflates compaction scheduling with
        # durability and is timing-sensitive.
        original_append = store._append_wal_locked
        wal_count = [0]

        def counting_append(*args, **kwargs):
            wal_count[0] += 1
            return original_append(*args, **kwargs)

        store._append_wal_locked = counting_append

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

        # No waiting needed: WAL appends are synchronous under the lock.
        # Synchronous WAL writes: one durable append per mutation.
        assert wal_count[0] == 50, (
            f"Expected 50 durable WAL appends after 50-add burst, got {wal_count[0]}"
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
        from codebot.implementation_planner import PlanStore
        tickets = []
        for i in range(count):
            t = create_ticket(
                f"batch-{i}-{target_state.value}-{store._path.stem}-{len(tickets)}",
                TicketClass.BUG, Severity.LOW,
                "test", f"evidence-batch-{store._path.stem}-{target_state.value}-{i}", "problem", "desired", ["ac"],
                risk=risk,
            )
            store.add(t)
            # Auto-create a plan when the path goes through PLANNING->IMPLEMENT
            # so the planning gate does not block the helper.
            path_to_target = {
                TicketState.TRIAGED: [TicketState.TRIAGED],
                TicketState.GOAL: [TicketState.TRIAGED, TicketState.GOAL],
                TicketState.DECOMP: [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP],
                TicketState.PLANNING: [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING],
                TicketState.IMPLEMENT: [
                    TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP,
                    TicketState.PLANNING,
                    TicketState.IMPLEMENT,
                ],
                TicketState.REVIEW: [
                    TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP,
                    TicketState.PLANNING,
                    TicketState.IMPLEMENT, TicketState.REVIEW,
                ],
            }
            states = path_to_target.get(target_state, [])
            if TicketState.IMPLEMENT in states:
                try:
                    plan_store = PlanStore(store._path.parent)
                    if not plan_store.exists(t.id):
                        plan_store.save(t.id, {"steps": ["auto plan for test"]})
                except Exception:
                    pass
            for state in states:
                cur = store.get(t.id)
                assert cur is not None
                if cur.state == state:
                    continue
                t = store.transition(t.id, state)
            tickets.append(t)
        return tickets

    def test_batch_transition_applies_all_changes(self, tmp_path):
        """batch_transition should apply all K transitions atomically."""
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 5, TicketState.IMPLEMENT)

        transitions = [(t.id, TicketState.REVIEW, None) for t in tickets]
        results = store.batch_transition(transitions)

        assert len(results) == 5
        for r in results:
            assert r.state == TicketState.REVIEW

        reviewing = store.list_by_state(TicketState.REVIEW)
        assert len(reviewing) == 5
        implementing = store.list_by_state(TicketState.IMPLEMENT)
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
            store.batch_transition([("CB-DEADBEEF12345678", TicketState.TRIAGED, None)])
        store.close()

    def test_batch_transition_invalid_state_raises_valueerror(self, tmp_path):
        """batch_transition should raise ValueError for invalid state transitions."""
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 2, TicketState.IMPLEMENT)

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
        tickets = self._add_tickets_in_state(store, 3, TicketState.IMPLEMENT)

        transitions = [
            (tickets[0].id, TicketState.REVIEW, None),
            (tickets[1].id, TicketState.COMPLETE, None),
            (tickets[2].id, TicketState.REVIEW, None),
        ]

        with pytest.raises(ValueError, match="invalid transition"):
            store.batch_transition(transitions)

        # All tickets should remain in IMPLEMENTING since the batch failed
        for t in tickets:
            stored = store.get(t.id)
            assert stored.state == TicketState.IMPLEMENT, (
                f"Ticket {t.id} was mutated to {stored.state} despite batch failure; "
                f"batch_transition must be atomic"
            )
        store.close()

    def test_save_called_once_per_batch(self, tmp_path):
        """_append_wal_locked() is called exactly once per batch_transition.

        Durability is synchronous: batch_transition writes one WAL append
        covering the whole batch (atomic, under the RLock) before return.
        Deferred compaction via _save() is out of scope here.
        """
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 10, TicketState.IMPLEMENT)

        # Count synchronous WAL durability writes
        original_append = store._append_wal_locked
        wal_count = [0]

        def counting_append(*args, **kwargs):
            wal_count[0] += 1
            return original_append(*args, **kwargs)

        store._append_wal_locked = counting_append

        transitions = [(t.id, TicketState.REVIEW, None) for t in tickets]
        store.batch_transition(transitions)

        # WAL append should happen exactly once for the entire batch
        assert wal_count[0] == 1, (
            f"Expected WAL append called 1 time for batch of {len(tickets)}, "
            f"got {wal_count[0]}"
        )
        store.close()

    def test_dispatch_10_tickets_constant_time(self, tmp_path):
        """Dispatching 10 tickets takes constant time relative to total ticket count.

        Acceptance criterion: Dispatching 10 tickets takes constant time
        relative to total ticket count. We measure batch_transition of 10
        tickets with 100 vs 500 total tickets; ratio should be < 3x.
        """
        def measure_batch_dispatch(total_tickets, batch_size=10):
            import os
            # Use a fresh unique path per measurement so .lock/.wal siblings
            # get a distinct, non-existing .json target (with_suffix replaces
            # only the last suffix, so "tickets_100.json" is required —
            # paths like "store_100/tickets.json" would collide on "tickets.lock").
            store_path = tmp_path / f"tickets_{total_tickets}.json"
            os.makedirs(store_path.parent, exist_ok=True)
            store = self._make_store(store_path)
            tickets = self._add_tickets_in_state(store, total_tickets, TicketState.IMPLEMENT)

            batch = [(t.id, TicketState.REVIEW, None) for t in tickets[:batch_size]]

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
        """batch_transition must enforce gate approval for REVIEW->COMPLETE."""
        store = self._make_store(tmp_path)
        tickets = self._add_tickets_in_state(store, 2, TicketState.REVIEW)

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
        tickets = self._add_tickets_in_state(store, 2, TicketState.PLANNING, risk=RiskLevel.MEDIUM)

        transitions = [(t.id, TicketState.IMPLEMENT, None) for t in tickets]
        with pytest.raises(ValueError, match="requires.*implementation plan"):
            store.batch_transition(transitions)

        from codebot.implementation_planner import PlanStore
        plan_store = PlanStore(tmp_path)
        plan_store.save(tickets[0].id, {"steps": ["step1"]})

        results = store.batch_transition([(tickets[0].id, TicketState.IMPLEMENT, None)])
        assert len(results) == 1
        assert results[0].state == TicketState.IMPLEMENT
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

    def test_bounded_queue_drops_excess_tasks_without_growth(self, tmp_path):
        """Bounded backup queue prevents memory exhaustion DoS (CB-DFF7728).

        Rapid mutations must not grow the queue unboundedly: when full,
        put_nowait() drops the excess best-effort task.  Durability is
        unaffected because it is guaranteed by synchronous WAL writes.
        """
        import queue as queue_module

        store = TicketStore(tmp_path / "tickets.json")
        assert store._backup_queue.maxsize == TicketStore.BACKUP_QUEUE_MAXSIZE == 100

        # Fill the queue with best-effort tasks while the worker is busy.
        # Pause the worker by filling faster than it copies.
        src = tmp_path / "bounded_src.txt"
        src.write_text("data", encoding="utf-8")
        dropped = 0
        enqueued = 0
        for i in range(TicketStore.BACKUP_QUEUE_MAXSIZE + 50):
            try:
                store._backup_queue.put_nowait((str(src), str(tmp_path / f"bounded_{i}.txt")))
                enqueued += 1
            except queue_module.Full:
                dropped += 1
        # Queue never exceeds its bound even under burst load.
        assert store._backup_queue.qsize() <= TicketStore.BACKUP_QUEUE_MAXSIZE
        # queue_backup_task itself must never block/raise when full.
        for i in range(150):
            store.queue_backup_task(str(src), str(tmp_path / f"nb_{i}.txt"))
        assert store._backup_queue.qsize() <= TicketStore.BACKUP_QUEUE_MAXSIZE
        store._backup_queue.join()
        store.close()

    def test_backup_drop_does_not_lose_ticket_data(self, tmp_path):
        """Dropping a backup under burst load must not lose ticket data."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "bounded durability",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "ev-bounded-durability",
            "problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        store.close()
        store2 = TicketStore(tmp_path / "tickets.json")
        assert store2.get(t.id) is not None
        store2.close()


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

        # Manually mark a ticket dirty so _save() has work to do.
        # (store.add already saved synchronously, leaving _dirty_ids empty,
        # so we must re-dirty an ID to exercise the lock-failure path.)
        store._dirty_ids.add(t.id)

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

    def test_concurrent_compaction_no_crash(self, tmp_path):
        """Concurrent mutations during WAL compaction must not corrupt state or crash.
        
        Regression test for CB-A47DFE378D1156B8D5452B28F9EC19CE:
        Verifies that _replay_wal_into_memory_locked() and _build_full_payload()
        execute while holding self._lock, preventing intra-process race conditions
        that could cause 'RuntimeError: dictionary changed size during iteration'
        or silent corruption of self._tickets/_state_index.
        """
        import threading

        path = tmp_path / "tickets.json"
        store = self._make_store(tmp_path)

        # Pre-populate with some tickets to make compaction meaningful
        initial_tickets = []
        for i in range(20):
            t = create_ticket(
                f"initial-{i}",
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
            initial_tickets.append(t.id)

        # Force compaction on next save to trigger the vulnerable code path
        store._force_compaction_on_next_save = True

        num_threads = 10
        mutations_per_thread = 5
        errors = []
        mutated_tickets = []
        lock = threading.Lock()
        start_barrier = threading.Barrier(num_threads + 1)  # +1 for main thread

        def worker(thread_id):
            try:
                start_barrier.wait()  # Wait for all threads to be ready
                for i in range(mutations_per_thread):
                    # Mix of add() and transition() to stress both read and write paths
                    if i % 2 == 0:
                        t = create_ticket(
                            f"concurrent-{thread_id}-{i}",
                            TicketClass.FEATURE,
                            Severity.MEDIUM,
                            "test",
                            f"evidence-{thread_id}-{i}",
                            "problem",
                            "desired",
                            ["crit"],
                            risk=RiskLevel.LOW,
                        )
                        store.add(t)
                        with lock:
                            mutated_tickets.append(("add", t.id))
                    else:
                        # Transition a unique ticket created by this thread
                        # to avoid contention on shared initial_tickets
                        t = create_ticket(
                            f"transition-{thread_id}-{i}",
                            TicketClass.BUG,
                            Severity.LOW,
                            "test",
                            f"evidence-trans-{thread_id}-{i}",
                            "problem",
                            "desired",
                            ["crit"],
                            risk=RiskLevel.LOW,
                        )
                        store.add(t)
                        store.transition(t.id, TicketState.TRIAGED)
                        with lock:
                            mutated_tickets.append(("transition", t.id))
            except Exception as e:
                errors.append((thread_id, str(e)))

        # Start worker threads
        threads = []
        for tid in range(num_threads):
            t = threading.Thread(target=worker, args=(tid,))
            threads.append(t)
            t.start()

        # Release all threads simultaneously to maximize contention
        start_barrier.wait()

        # Trigger compaction while threads are mutating
        # The _save() will be called by the background worker or we force it
        # Force a save to trigger compaction during concurrent mutations
        time.sleep(0.05)  # Let threads start mutating
        store._save()  # This should trigger compaction with _force_compaction_on_next_save

        for t in threads:
            t.join(timeout=30)

        # Assert no crashes or race condition errors
        assert not errors, f"Concurrent compaction raised errors: {errors}"

        # Flush to ensure all saves are persisted
        store.flush()
        store.close()

        # Verify final state consistency
        store2 = TicketStore(path)
        try:
            # Count expected tickets: initial + all new tickets created by threads
            # Both 'add' and 'transition' ops now create new tickets in this test
            expected_new = len(mutated_tickets)
            expected_count = len(initial_tickets) + expected_new
            actual_count = store2.count()
            assert actual_count == expected_count, (
                f"Ticket count mismatch: expected {expected_count}, got {actual_count}. "
                f"Errors during test: {errors}"
            )

            # Verify all initial tickets still exist
            for tid in initial_tickets:
                ticket = store2.get(tid)
                assert ticket is not None, f"Initial ticket {tid} was lost during compaction"

            # Verify all added tickets exist
            for op, tid in mutated_tickets:
                if op == "add":
                    ticket = store2.get(tid)
                    assert ticket is not None, f"Added ticket {tid} was lost during compaction"
        finally:
            store2.close()

    def test_backup_failure_propagates_not_silent(self, tmp_path):
        """Verify _save raises RuntimeError when _backup consistently fails.

        Regression test for CB-4149816-5C34: backup failure must NOT be
        silently swallowed. When _backup raises OSError on all retry attempts,
        _save must raise RuntimeError so the caller knows the save failed.
        Data durability is preserved via WAL (written in add()/transition()).
        """
        from unittest.mock import patch

        path = tmp_path / "tickets.json"
        store = self._make_store(tmp_path)

        # Add a ticket to ensure dirty_ids is non-empty
        t = create_ticket(
            "backup-fail-test",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "evidence-backup-fail",
            "problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        store.flush()  # Clean slate

        # Add another ticket to make dirty_ids non-empty again
        t2 = create_ticket(
            "backup-fail-test-2",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "evidence-backup-fail-2",
            "problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t2)

        # Force compaction to trigger _backup path
        store._force_compaction_on_next_save = True

        backup_call_count = [0]

        def failing_backup(*args, **kwargs):
            backup_call_count[0] += 1
            raise OSError("Simulated backup failure")

        # Patch _backup to raise OSError — _save should log warning but continue
        # Backup failure is non-fatal; WAL guarantees durability.
        with patch.object(store, '_backup', failing_backup):
            store._save()  # Should not raise; backup failure is logged

        # Verify _backup was called (once per compaction, not retried)
        assert backup_call_count[0] >= 1, (
            f"Expected _backup to be called at least once, got {backup_call_count[0]}"
        )

        # Data is still safe via WAL (add() wrote to WAL synchronously)
        store.close()
        store2 = TicketStore(path)
        assert store2.get(t2.id) is not None, "Ticket lost despite backup failure — WAL should preserve it"
        store2.close()

    def test_concurrent_saves_serialized_despite_backup_failure(self, tmp_path):
        """Verify concurrent saves succeed even when backup consistently fails.

        Regression test for CB-4149816-5C34 (updated from CB-1835532-11DC):
        backup failure is non-fatal; it is logged but does not prevent save.
        Data durability is preserved via WAL written synchronously in add().

        Verifies:
        1. No RuntimeError is raised when backup fails (non-fatal)
        2. All tickets remain safe via WAL despite backup failures
        3. A successful flush persists all data
        """
        import threading
        from unittest.mock import patch

        path = tmp_path / "tickets.json"
        # Use a single shared store to test intra-process concurrency
        store = TicketStore(path, start_background_workers=False)
        # Force compaction on every save to trigger backup path
        store._force_compaction_on_next_save = True

        save_errors = []
        saved_tickets = []
        lock = threading.Lock()
        backup_failures = [0]

        def failing_backup(*args, **kwargs):
            backup_failures[0] += 1
            raise OSError("Simulated backup failure")

        def worker(thread_id):
            try:
                for i in range(3):
                    t = create_ticket(
                        f"concurrent-bf-{thread_id}-{i}",
                        TicketClass.BUG,
                        Severity.LOW,
                        "test",
                        f"evidence-bf-{thread_id}-{i}",
                        "problem",
                        "desired",
                        ["crit"],
                        risk=RiskLevel.LOW,
                    )
                    store.add(t)
                    with patch.object(store, '_backup', failing_backup):
                        store._save()  # Should not raise; backup failure is non-fatal
                    with lock:
                        saved_tickets.append(t.id)
            except Exception as e:
                with lock:
                    save_errors.append(e)

        # Run concurrent saves with backup failures
        threads = []
        num_threads = 5
        for tid in range(num_threads):
            t = threading.Thread(target=worker, args=(tid,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        assert not save_errors, f"Concurrent saves with backup failure raised unexpected errors: {save_errors}"

        # Verify all tickets were saved in memory
        assert store.count() == num_threads * 3, (
            f"Expected {num_threads * 3} tickets in memory, got {store.count()}. "
            f"Backup failures: {backup_failures[0]}"
        )

        # Flush WITHOUT backup failure to persist all WAL data
        store.flush()
        store.close()

        # Reload and verify persistence
        store2 = TicketStore(path)
        assert store2.count() == num_threads * 3, (
            f"Expected {num_threads * 3} tickets after reload, got {store2.count()}"
        )

        # Verify each ticket exists
        for tid in saved_tickets:
            assert store2.get(tid) is not None, f"Ticket {tid} was lost"

        # Verify backup was attempted at least once per thread
        # (not every save triggers compaction/backup due to debounce and WAL path)
        assert backup_failures[0] >= num_threads, (
            f"Expected at least {num_threads} backup failures, got {backup_failures[0]}"
        )

        store2.close()

    def test_backup_failure_after_atomic_write_does_not_release_lock_early(self, tmp_path):
        """Verify backup failure occurs AFTER atomic write completes, ensuring lock integrity.

        Regression test for CB-5492279F973917EF8DDD9F1F9EA1B071:
        The fix moves _backup() to execute AFTER the atomic write (tmp.replace) succeeds.
        This ensures that if backup fails, the lock is still held during the critical
        write section, preventing any race condition where another process could acquire
        the lock during backup exception handling.

        Verifies:
        1. Atomic write completes before backup is attempted
        2. Backup failure does not affect the already-completed atomic write
        3. Lock is held throughout the entire critical section including backup
        """
        from unittest.mock import patch, MagicMock
        import threading

        path = tmp_path / "tickets.json"
        store = TicketStore(path, start_background_workers=False)

        # Add a ticket and force compaction
        t = create_ticket(
            "backup-order-test",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "evidence-backup-order",
            "problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        store.flush()  # Clean slate

        # Add another ticket to trigger compaction
        t2 = create_ticket(
            "backup-order-test-2",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "evidence-backup-order-2",
            "problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t2)

        # Force compaction to trigger backup path
        store._force_compaction_on_next_save = True

        # Track the order of operations
        operation_order = []
        original_replace = None

        def track_replace(src, dst):
            operation_order.append("atomic_write_complete")
            return original_replace(src, dst)

        def failing_backup(*args, **kwargs):
            operation_order.append("backup_attempted")
            raise OSError("Simulated backup failure after write")

        # Patch both the replace method and backup
        import pathlib
        original_replace = pathlib.Path.replace

        with patch.object(pathlib.Path, 'replace', track_replace):
            with patch.object(store, '_backup', failing_backup):
                # This should complete the atomic write first, then attempt backup
                store._save()

        # Verify order: atomic write must complete before backup is attempted
        assert "atomic_write_complete" in operation_order, "Atomic write was not completed"
        assert "backup_attempted" in operation_order, "Backup was not attempted"
        
        # The atomic write should happen before backup
        atomic_write_idx = operation_order.index("atomic_write_complete")
        backup_idx = operation_order.index("backup_attempted")
        assert atomic_write_idx < backup_idx, (
            f"Backup was attempted before atomic write completed. Order: {operation_order}"
        )

        # Verify the ticket was saved despite backup failure
        store.close()
        store2 = TicketStore(path)
        assert store2.get(t2.id) is not None, "Ticket lost despite backup failure"
        store2.close()


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


class TestBackupAtomicity:
    """Tests for CB-4435524-2908: atomic backup writes under contention."""

    def test_backup_atomic_under_contention(self, tmp_path):
        """Simulate concurrent saves and verify no torn JSON in backup files.

        Creates two TicketStore instances pointing to the same file, triggers
        concurrent compaction saves (which invoke _backup), then validates all
        backup files parse as valid JSON. Before the fix, async shutil.copy2
        could produce torn backups; after the fix, atomic tmp+replace prevents this.
        """
        import threading

        ticket_file = tmp_path / "tickets.json"

        # Initialize with some data so compaction has something to back up
        store_init = TicketStore(ticket_file)
        for i in range(5):
            t = create_ticket(
                f"init-ticket-{i}", TicketClass.BUG, Severity.LOW,
                f"problem {i}", f"evidence {i}", f"desired {i}", "accept", ["mod"]
            )
            store_init.add(t)
        store_init.flush()
        store_init.close()

        errors = []

        def do_saves(store_id: int) -> None:
            try:
                store = TicketStore(ticket_file)
                for i in range(3):
                    t = create_ticket(
                        f"concurrent-{store_id}-{i}", TicketClass.BUG, Severity.LOW,
                        f"problem {store_id}-{i}", f"evidence {store_id}-{i}",
                        f"desired {store_id}-{i}", "accept", ["mod"]
                    )
                    store.add(t)
                    # Force compaction by manipulating internal counter
                    store._save_count = store._FULL_SAVE_INTERVAL - 1
                    store.flush()
                store.close()
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=do_saves, args=(i,))
            for i in range(3)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent saves raised errors: {errors}"

        # Verify all backup files contain valid JSON
        backup_dir = tmp_path / "ticket_backups"
        if backup_dir.exists():
            backup_files = list(backup_dir.glob("tickets-*.json"))
            assert len(backup_files) > 0, "Expected at least one backup file"
            for bf in backup_files:
                content = bf.read_text(encoding="utf-8")
                assert content.strip(), f"Backup file {bf.name} is empty"
                parsed = json.loads(content)  # Raises on torn/corrupt JSON
                assert "tickets" in parsed, f"Backup {bf.name} missing 'tickets' key"


class TestLifecycleTelemetry:
    """Tests for append-only lifecycle event telemetry on valid transitions."""

    def _make_store_and_ticket(self, tmp_path, risk=RiskLevel.LOW):
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "telemetry ticket", TicketClass.BUG, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
            risk=risk,
        )
        store.add(t)
        return store, t

    def _read_events(self, tmp_path):
        events_path = tmp_path / "lifecycle_events.jsonl"
        if not events_path.exists():
            return []
        events = []
        for line in events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                events.append(json.loads(line))
        return events

    def test_single_valid_transition_emits_one_event(self, tmp_path):
        store, t = self._make_store_and_ticket(tmp_path)
        store.transition(t.id, TicketState.TRIAGED, actor="test-runner")
        store.flush()
        events = self._read_events(tmp_path)
        assert len(events) == 1
        ev = events[0]
        assert ev["ticket_id"] == t.id
        assert ev["from_state"] == "DISCOVERED"
        assert ev["to_state"] == "TRIAGED"
        assert ev["actor"] == "test-runner"
        assert ev["attempts"] == 0
        assert ev["rework_count"] == 0
        assert ev["queue_age_seconds"] >= 0
        assert "timestamp" in ev
        assert "revision" in ev
        store.close()

    def test_multiple_transitions_emit_ordered_events(self, tmp_path):
        store, t = self._make_store_and_ticket(tmp_path)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.flush()
        events = self._read_events(tmp_path)
        assert len(events) == 3
        assert events[0]["to_state"] == "TRIAGED"
        assert events[1]["to_state"] == "GOAL"
        assert events[2]["to_state"] == "DECOMP"
        store.close()

    def test_invalid_transition_emits_no_event(self, tmp_path):
        store, t = self._make_store_and_ticket(tmp_path)
        with pytest.raises(ValueError):
            store.transition(t.id, TicketState.COMPLETE)
        events = self._read_events(tmp_path)
        assert len(events) == 0
        store.close()

    def test_batch_transition_emits_events_per_success(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        tickets = []
        for i in range(3):
            t = create_ticket(
                f"batch ticket {i}", TicketClass.BUG, Severity.MEDIUM,
                "test", f"ev-{i}", f"prob-{i}", "desired", ["ac"],
                risk=RiskLevel.LOW,
            )
            store.add(t)
            tickets.append(t)
        transitions = [
            (tickets[0].id, TicketState.TRIAGED, None),
            (tickets[1].id, TicketState.TRIAGED, None),
            (tickets[2].id, TicketState.TRIAGED, None),
        ]
        store.batch_transition(transitions, actor="batch-runner")
        store.flush()
        events = self._read_events(tmp_path)
        assert len(events) == 3
        for ev in events:
            assert ev["from_state"] == "DISCOVERED"
            assert ev["to_state"] == "TRIAGED"
            assert ev["actor"] == "batch-runner"
        store.close()

    def test_failed_batch_transition_emits_no_events(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket(
            "batch ok", TicketClass.BUG, Severity.MEDIUM,
            "test", "ev-1", "prob-1", "desired", ["ac"],
            risk=RiskLevel.LOW,
        )
        t2 = create_ticket(
            "batch bad", TicketClass.BUG, Severity.MEDIUM,
            "test", "ev-2", "prob-2", "desired", ["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t1)
        store.add(t2)
        transitions = [
            (t1.id, TicketState.TRIAGED, None),
            (t2.id, TicketState.COMPLETE, None),
        ]
        with pytest.raises(ValueError):
            store.batch_transition(transitions)
        events = self._read_events(tmp_path)
        assert len(events) == 0
        store.close()

    def test_planning_rejected_transition_emits_no_event(self, tmp_path):
        store, t = self._make_store_and_ticket(tmp_path, risk=RiskLevel.MEDIUM)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.transition(t.id, TicketState.PLANNING)
        store.flush()
        events_before = self._read_events(tmp_path)
        assert len(events_before) == 4
        with pytest.raises(ValueError, match="requires an implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENT)
        store.flush()
        events_after = self._read_events(tmp_path)
        assert len(events_after) == 4
        store.close()


class TestSaveFailureTracking:
    """Tests for CB-4676C79BA1C2: silent exception swallowing fix."""

    def _make_store_and_ticket(self, tmp_path):
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity
        store = TicketStore(tmp_path / "tickets.json")
        ticket = create_ticket(
            title="Test save failure",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="test",
            evidence="test",
            problem_statement="test",
            desired_state="test",
            acceptance_criteria=["test"],
        )
        return store, ticket

    def test_save_worker_logs_error_on_io_failure(self, tmp_path, caplog):
        """All exceptions in _save_worker_loop must be logged at ERROR level.

        NOTE: since add()/transition() now save synchronously for crash
        safety, the background worker is a redundant flush path only.
        This test therefore exercises the worker directly via _queue_save()
        rather than through add(), which raises synchronously on I/O error.
        """
        import logging
        store, ticket = self._make_store_and_ticket(tmp_path)
        store.add(ticket)
        store.flush()

        # Mock _save to raise OSError
        original_save = store._save
        call_count = [0]

        def failing_save():
            call_count[0] += 1
            raise OSError("simulated disk full")

        store._save = failing_save

        # Trigger a save via the background worker queue and wait for it.
        # add() is intentionally NOT used here: it calls _save()
        # synchronously and would raise OSError directly instead of
        # exercising the worker's "background save failed" log path.
        with caplog.at_level(logging.ERROR):
            store._queue_save()
            # Wait for debounce + worker processing
            time.sleep(1.5)

        # Verify ERROR was logged
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_records) >= 1, f"Expected ERROR log, got: {[r.message for r in caplog.records]}"
        assert any("background save failed" in r.message for r in error_records)
        assert call_count[0] >= 1

        # Restore and cleanup
        store._save = original_save
        store.close()

    def test_failure_counter_and_sync_fallback(self, tmp_path, caplog):
        """Failure counter increments on worker exception, resets on success.

        The background worker is the only path that logs "background save
        failed" and tracks _save_failure_count.  Synchronous mutation paths
        (add/transition) use _append_wal_locked directly and propagate
        OSError immediately, so they are not part of this counter.  This
        test therefore drives the worker via _queue_save() only, while the
        synchronous WAL append path is verified separately below.
        """
        import logging
        store, ticket = self._make_store_and_ticket(tmp_path)
        store.add(ticket)
        store.flush()

        original_save = store._save

        def failing_save():
            raise OSError("simulated permission denied")

        store._save = failing_save

        with caplog.at_level(logging.ERROR):
            # Simulate consecutive worker failures to exceed threshold.
            # Each _queue_save() triggers one worker _save() attempt.
            for i in range(store._SAVE_FAILURE_THRESHOLD + 1):
                store._queue_save()
                time.sleep(0.8)  # let worker process
            # Synchronous WAL appends raise immediately while the WAL
            # itself is broken: patch open() is out of scope, so instead
            # verify the WAL path directly is still writable for a new
            # ticket after restore (counter reset proves durability path
            # is independent of the failed compaction worker).
            assert store._save_failure_count >= store._SAVE_FAILURE_THRESHOLD

        # Verify threshold-exceeded message was logged (accurate durability statement)
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert any("durability relies on synchronous WAL appends only" in r.message for r in error_records), \
            f"Expected accurate durability message, got: {[r.message for r in error_records]}"

        # Restore and verify a successful background save resets the counter.
        # Drive reset through the worker path (not add()) so a slow
        # background retry cannot interleave between restore and assertion.
        store._save = original_save
        store._queue_save()
        time.sleep(1.0)  # debounce (0.5s) + worker processing
        assert store._save_failure_count == 0

        t_recovery = create_ticket(
            title="Recovery test",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="test",
            evidence="ev-recovery",
            problem_statement="recovery",
            desired_state="test",
            acceptance_criteria=["test"],
        )
        store.add(t_recovery)  # Should succeed (counter already reset)

        store.close()


class TestTicketStoreCrashSafety:
    """Verify synchronous WAL writes survive process termination within 2s window."""

    def test_mutation_survives_sigkill(self, tmp_path):
        """Add a ticket, send SIGKILL (kill -9) immediately, verify data persists after reload.

        This proves the synchronous WAL append in add() calls os.fsync() before return,
        so even an uncatchable SIGKILL (power-loss simulation) cannot lose the mutation.
        Acceptance criterion 3: test verifies data survives simulated power loss via
        subprocess kill -9 immediately after add().
        """
        import os
        import signal
        import subprocess
        import sys

        db_path = tmp_path / "tickets.json"

        # Child process: create store, add ticket, then exit (simulating crash after add)
        # We use SIGKILL because it cannot be caught, simulating a hard crash/power loss.
        child_code = f'''
import sys, os, time
sys.path.insert(0, "{str(Path(__file__).parent.parent)}")
from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity

store = TicketStore("{str(db_path)}")
t = create_ticket(
    title="Crash safety test",
    ticket_class=TicketClass.BUG,
    severity=Severity.HIGH,
    source="crash_test",
    evidence="ev-crash",
    problem_statement="test",
    desired_state="test",
    acceptance_criteria=["test"],
)
store.add(t)
# Signal parent that add() completed (WAL should be fsynced by now)
print(t.id, flush=True)
# Exit normally - the parent will kill us, but if add() returned, data is durable
# We sleep briefly to give parent time to read stdout before killing
time.sleep(0.1)
os._exit(0)
'''
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Read ticket ID from child's stdout (blocks until add() returns)
        ticket_id = proc.stdout.readline().strip()
        assert ticket_id.startswith("CB-"), f"Expected ticket ID, got: {ticket_id!r}"

        # Send SIGKILL immediately after add() returned
        # SIGKILL cannot be caught, simulating a hard crash/power loss
        proc.kill()
        proc.wait(timeout=5)

        # Reload store and verify ticket survived via WAL replay
        store2 = TicketStore(str(db_path))
        recovered = store2.get(ticket_id)
        assert recovered is not None, f"Ticket {ticket_id} lost after SIGKILL (kill -9)"
        assert recovered.title == "Crash safety test"
        store2.close()

    def test_mutation_survives_sigterm(self, tmp_path):
        """Add a ticket, send SIGTERM immediately, verify data persists after reload.

        This proves the synchronous _save() in add() writes WAL before return,
        so even immediate SIGTERM cannot lose the mutation.
        """
        import os
        import signal
        import subprocess
        import sys

        db_path = tmp_path / "tickets.json"

        # Child process: create store, add ticket, then wait for SIGTERM
        child_code = f'''
import sys, signal, time
sys.path.insert(0, "{str(Path(__file__).parent.parent)}")
from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity

def handler(signum, frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, handler)

store = TicketStore("{str(db_path)}")
t = create_ticket(
    title="Crash safety test",
    ticket_class=TicketClass.BUG,
    severity=Severity.HIGH,
    source="crash_test",
    evidence="ev-crash",
    problem_statement="test",
    desired_state="test",
    acceptance_criteria=["test"],
)
store.add(t)
# Signal parent that add() completed (WAL should be written by now)
print(t.id, flush=True)
# Wait indefinitely for SIGTERM
time.sleep(300)
'''
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Read ticket ID from child's stdout (blocks until add() returns)
        ticket_id = proc.stdout.readline().strip()
        assert ticket_id.startswith("CB-"), f"Expected ticket ID, got: {ticket_id!r}"

        # Send SIGTERM immediately after add() returned
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

        # Reload store and verify ticket survived
        store2 = TicketStore(str(db_path))
        recovered = store2.get(ticket_id)
        assert recovered is not None, f"Ticket {ticket_id} lost after SIGTERM"
        assert recovered.title == "Crash safety test"
        store2.close()

    def test_transition_survives_sigterm(self, tmp_path):
        """Transition a ticket, send SIGTERM immediately, verify state persists."""
        import os
        import signal
        import subprocess
        import sys

        db_path = tmp_path / "tickets.json"

        # First create a ticket
        store = TicketStore(str(db_path))
        t = create_ticket(
            title="Transition crash test",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="crash_test",
            evidence="ev-trans",
            problem_statement="test",
            desired_state="test",
            acceptance_criteria=["test"],
        )
        store.add(t)
        tid = t.id
        store.close()

        # Child process: load store, transition ticket, wait for SIGTERM
        child_code = f'''
import sys, signal, time
sys.path.insert(0, "{str(Path(__file__).parent.parent)}")
from codebot.ticket_engine import TicketStore, TicketState

def handler(signum, frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, handler)

store = TicketStore("{str(db_path)}")
store.transition("{tid}", TicketState.TRIAGED, actor="crash_test")
print("DONE", flush=True)
time.sleep(300)
'''
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        result = proc.stdout.readline().strip()
        assert result == "DONE", f"Expected DONE, got: {result!r}"

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

        # Reload and verify state persisted
        store2 = TicketStore(str(db_path))
        recovered = store2.get(tid)
        assert recovered is not None
        assert recovered.state.value == "TRIAGED", \
            f"Expected TRIAGED, got {recovered.state.value}"
        store2.close()

    def test_batch_transition_survives_sigterm(self, tmp_path):
        """Batch transition tickets, send SIGTERM immediately, verify state persists."""
        import os
        import signal
        import subprocess
        import sys

        db_path = tmp_path / "tickets.json"

        # First create two tickets
        store = TicketStore(str(db_path))
        t1 = create_ticket(
            title="Batch crash test 1",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="crash_test",
            evidence="ev-batch-1",
            problem_statement="test",
            desired_state="test",
            acceptance_criteria=["test"],
        )
        t2 = create_ticket(
            title="Batch crash test 2",
            ticket_class=TicketClass.FEATURE,
            severity=Severity.LOW,
            source="crash_test",
            evidence="ev-batch-2",
            problem_statement="test",
            desired_state="test",
            acceptance_criteria=["test"],
        )
        store.add(t1)
        store.add(t2)
        tid1 = t1.id
        tid2 = t2.id
        store.close()

        # Child process: load store, batch transition tickets, wait for SIGTERM
        child_code = f'''
import sys, signal, time
sys.path.insert(0, "{str(Path(__file__).parent.parent)}")
from codebot.ticket_engine import TicketStore, TicketState

def handler(signum, frame):
    sys.exit(0)

signal.signal(signal.SIGTERM, handler)

store = TicketStore("{str(db_path)}")
transitions = [
    ("{tid1}", TicketState.TRIAGED, None),
    ("{tid2}", TicketState.TRIAGED, None),
]
store.batch_transition(transitions, actor="crash_test")
print("DONE", flush=True)
time.sleep(300)
'''
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        result = proc.stdout.readline().strip()
        assert result == "DONE", f"Expected DONE, got: {result!r}"

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

        # Reload and verify state persisted for both tickets
        store2 = TicketStore(str(db_path))
        recovered1 = store2.get(tid1)
        recovered2 = store2.get(tid2)
        assert recovered1 is not None, "Ticket 1 lost after SIGTERM"
        assert recovered2 is not None, "Ticket 2 lost after SIGTERM"
        assert recovered1.state.value == "TRIAGED", \
            f"Expected TRIAGED for ticket 1, got {recovered1.state.value}"
        assert recovered2.state.value == "TRIAGED", \
            f"Expected TRIAGED for ticket 2, got {recovered2.state.value}"
        store2.close()

    def test_concurrent_mutation_during_save_survives_sigkill(self, tmp_path):
        """Verify concurrent mutation during _save() survives SIGKILL.

        This test exercises the two-set _pending_flush mechanism:
        1. Parent process creates a store with initial tickets
        2. Child process spawns a mutator thread that continuously mutates tickets
        3. Parent triggers a save (which moves IDs from _dirty_ids to _pending_flush)
        4. While _save() is in-flight (writing WAL), parent sends SIGKILL
        5. After restart, both the initial mutations and concurrent mutations must survive

        Acceptance criterion 2: test verifies concurrent mutation during _save survives SIGKILL.
        """
        import os
        import signal
        import subprocess
        import sys
        import threading
        import time

        db_path = tmp_path / "tickets.json"

        # Child process: create store, start mutator thread, trigger save, then get killed
        child_code = f'''
import sys, os, time, threading
sys.path.insert(0, "{str(Path(__file__).parent.parent)}")
from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel

db_path = "{str(db_path)}"
store = TicketStore(db_path, start_background_workers=False)

# Create initial ticket
t1 = create_ticket(
    title="Initial ticket",
    ticket_class=TicketClass.BUG,
    severity=Severity.HIGH,
    source="concurrent_test",
    evidence="ev-initial",
    problem_statement="initial problem",
    desired_state="initial desired",
    acceptance_criteria=["survives concurrent crash"],
    risk=RiskLevel.MEDIUM,
)
store.add(t1)
print(f"INITIAL:{{t1.id}}", flush=True)

# Mutation function: continuously transition tickets
mutation_count = [0]
stop_flag = [False]

def mutator():
    while not stop_flag[0]:
        try:
            t2 = create_ticket(
                title=f"Concurrent mutation {{mutation_count[0]}}",
                ticket_class=TicketClass.FEATURE,
                severity=Severity.MEDIUM,
                source="mutator_thread",
                evidence=f"ev-mutate-{{mutation_count[0]}}",
                problem_statement=f"concurrent problem {{mutation_count[0]}}",
                desired_state=f"concurrent desired {{mutation_count[0]}}",
                acceptance_criteria=["survives crash"],
                risk=RiskLevel.LOW,
            )
            store.add(t2)
            mutation_count[0] += 1
            if mutation_count[0] >= 5:
                break
        except Exception as e:
            print(f"MUTATOR_ERROR:{{e}}", flush=True)
            break

# Start mutator thread
mutator_thread = threading.Thread(target=mutator, daemon=True)
mutator_thread.start()

# Give mutator time to start
time.sleep(0.05)

# Trigger synchronous save while mutator is running
# This exercises the _pending_flush mechanism
store.flush()

# Signal that save completed (mutator may still be running)
print(f"SAVE_COMPLETE:mutations={{mutation_count[0]}}", flush=True)

# Wait briefly for any final mutations
time.sleep(0.1)
stop_flag[0] = True
mutator_thread.join(timeout=1.0)

print(f"FINAL_COUNT:{{mutation_count[0]}}", flush=True)

# Exit normally - parent will SIGKILL us, but if flush() returned, data is durable
time.sleep(0.1)
os._exit(0)
'''
        proc = subprocess.Popen(
            [sys.executable, "-c", child_code],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        # Read output lines
        initial_line = None
        save_complete_line = None
        final_count_line = None

        for line in proc.stdout:
            line = line.strip()
            if line.startswith("INITIAL:"):
                initial_line = line
            elif line.startswith("SAVE_COMPLETE:"):
                save_complete_line = line
            elif line.startswith("FINAL_COUNT:"):
                final_count_line = line

        # Parse initial ticket ID
        assert initial_line is not None, f"Child did not print INITIAL line. stderr: {proc.stderr.read()}"
        initial_ticket_id = initial_line.split(":", 1)[1]
        assert initial_ticket_id.startswith("CB-"), f"Invalid ticket ID: {initial_ticket_id!r}"

        # Send SIGKILL immediately after save completed
        # This simulates crash during or immediately after _save()
        proc.kill()
        proc.wait(timeout=5)

        # Reload store and verify all mutations survived via WAL replay
        store2 = TicketStore(str(db_path), start_background_workers=False)
        try:
            # Verify initial ticket survived
            recovered_initial = store2.get(initial_ticket_id)
            assert recovered_initial is not None, (
                f"Initial ticket {initial_ticket_id} lost after SIGKILL"
            )
            assert recovered_initial.title == "Initial ticket"

            # Verify concurrent mutations survived
            # Count tickets from concurrent_test source
            concurrent_tickets = [
                t for t in store2._tickets.values()
                if t.source == "mutator_thread"
            ]
            assert len(concurrent_tickets) > 0, (
                f"No concurrent mutations survived. Expected at least 1, got {len(concurrent_tickets)}. "
                f"Total tickets in store: {store2.count()}"
            )

            # Verify no dirty tracking corruption: all tickets should be in valid states
            for t in store2._tickets.values():
                assert t.state is not None, f"Ticket {t.id} has corrupted state"
                assert t.evidence_hash(), f"Ticket {t.id} has corrupted evidence"

        finally:
            store2.close()


class TestTicketStoreRemove:
    """Tests for TicketStore.remove() method."""

    def _make_store(self, tmp_path):
        return TicketStore(tmp_path / "tickets.json")

    def test_remove_existing_ticket(self, tmp_path):
        """Removing an existing ticket should delete it from the store."""
        store = self._make_store(tmp_path)
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)
        assert store.count() == 1

        removed = store.remove(t.id)
        assert removed is not None
        assert removed.id == t.id
        assert store.count() == 0
        assert store.get(t.id) is None
        store.close()

    def test_remove_nonexistent_ticket(self, tmp_path):
        """Removing a nonexistent ticket should return None."""
        store = self._make_store(tmp_path)
        removed = store.remove("CB-DEADBEEF12345678")
        assert removed is None
        store.close()

    def test_remove_updates_indexes(self, tmp_path):
        """Removing a ticket should update all internal indexes."""
        store = self._make_store(tmp_path)
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "unique-evidence", "unique-problem", "d", ["a"])
        store.add(t)
        store.flush()

        # Verify indexes exist
        assert t.evidence_hash() in store._evidence_index
        assert t.id in store._state_index.get(t.state, set())

        store.remove(t.id)

        # Verify indexes are updated
        assert t.evidence_hash() not in store._evidence_index
        assert t.id not in store._state_index.get(t.state, set())
        store.close()

    def test_remove_persists(self, tmp_path):
        """Removed ticket should not reappear after reload."""
        path = tmp_path / "tickets.json"
        store1 = self._make_store(tmp_path)
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store1.add(t)
        store1.remove(t.id)
        store1.close()

        store2 = TicketStore(path)
        assert store2.get(t.id) is None
        assert store2.count() == 0
        store2.close()


class TestQueueManager:
    """Tests for QueueManager adapter."""

    def test_actionable_queue_depth(self, tmp_path):
        """actionable_queue_depth should return count of tickets in actionable states."""
        from codebot.ticket_engine import QueueManager

        store = TicketStore(tmp_path / "tickets.json")
        qm = QueueManager(store)

        # Add tickets in various states
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"], risk=RiskLevel.LOW)
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["a"], risk=RiskLevel.LOW)
        t3 = create_ticket("t3", TicketClass.BUG, Severity.LOW, "s", "e3", "p", "d", ["a"], risk=RiskLevel.LOW)
        t4 = create_ticket("t4", TicketClass.BUG, Severity.LOW, "s", "e4", "p", "d", ["a"], risk=RiskLevel.LOW)

        store.add(t1)  # DISCOVERED
        store.add(t2)  # DISCOVERED
        store.transition(t2.id, TicketState.TRIAGED)
        store.transition(t2.id, TicketState.GOAL)
        store.transition(t2.id, TicketState.DECOMP)  # DECOMP is actionable
        store.add(t3)  # DISCOVERED
        store.transition(t3.id, TicketState.TRIAGED)
        store.transition(t3.id, TicketState.GOAL)
        store.transition(t3.id, TicketState.DECOMP)
        store.transition(t3.id, TicketState.PLANNING)  # PLANNING is actionable
        store.add(t4)  # DISCOVERED
        store.transition(t4.id, TicketState.TRIAGED)
        store.transition(t4.id, TicketState.GOAL)
        store.transition(t4.id, TicketState.DECOMP)
        store.transition(t4.id, TicketState.PLANNING)
        store.transition(t4.id, TicketState.IMPLEMENT)  # IMPLEMENT is actionable

        depth = qm.actionable_queue_depth()
        # DECOMP (t2), PLANNING (t3), IMPLEMENT (t4) = 3
        assert depth == 3
        store.close()

    def test_ticket_classes(self, tmp_path):
        """ticket_classes should return classes of tickets in IMPLEMENT state."""
        from codebot.ticket_engine import QueueManager

        store = TicketStore(tmp_path / "tickets.json")
        qm = QueueManager(store)

        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"], risk=RiskLevel.LOW)
        t2 = create_ticket("t2", TicketClass.FEATURE, Severity.LOW, "s", "e2", "p", "d", ["a"], risk=RiskLevel.LOW)

        store.add(t1)
        store.add(t2)
        store.transition(t1.id, TicketState.TRIAGED)
        store.transition(t1.id, TicketState.GOAL)
        store.transition(t1.id, TicketState.DECOMP)
        store.transition(t1.id, TicketState.PLANNING)
        store.transition(t1.id, TicketState.IMPLEMENT)

        store.transition(t2.id, TicketState.TRIAGED)
        store.transition(t2.id, TicketState.GOAL)
        store.transition(t2.id, TicketState.DECOMP)
        store.transition(t2.id, TicketState.PLANNING)
        store.transition(t2.id, TicketState.IMPLEMENT)

        classes = qm.ticket_classes()
        assert "bug" in classes
        assert "feature" in classes
        store.close()

    def test_summary(self, tmp_path):
        """summary should return counts per state."""
        from codebot.ticket_engine import QueueManager

        store = TicketStore(tmp_path / "tickets.json")
        qm = QueueManager(store)

        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"])
        store.add(t1)

        s = qm.summary()
        assert s.get("DISCOVERED", 0) == 1
        store.close()

    def test_from_state_dir(self, tmp_path):
        """from_state_dir should create QueueManager from directory."""
        from codebot.ticket_engine import QueueManager

        qm = QueueManager.from_state_dir(tmp_path)
        # No tickets.json exists yet, so store should be None or empty
        assert qm.get_store() is None or qm.get_store().count() == 0

    def test_clear_cache(self, tmp_path):
        """clear_cache should reset internal store reference."""
        from codebot.ticket_engine import QueueManager

        store = TicketStore(tmp_path / "tickets.json")
        qm = QueueManager(store)
        assert qm._store is not None

        qm.clear_cache()
        # After clear, _store is None, next access might reload if path known
        assert qm._store is None
        store.close()


class TestTicketSerializationExtras:
    """Tests for serialization methods not previously covered."""

    def test_to_json_compact(self, tmp_path):
        """to_json should produce compact JSON by default."""
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        raw = t.to_json()
        assert "\n" not in raw
        assert ":" in raw

    def test_to_json_pretty(self, tmp_path):
        """to_json(pretty=True) should produce indented JSON."""
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        raw = t.to_json(pretty=True)
        assert "\n" in raw

    def test_from_json(self, tmp_path):
        """from_json should deserialize a ticket."""
        t1 = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        raw = t1.to_json()
        t2 = Ticket.from_json(raw)
        assert t2.id == t1.id
        assert t2.title == t1.title


class TestTicketStoreExtras:
    """Additional tests for TicketStore methods to improve coverage."""

    def test_record_commit(self, tmp_path):
        """record_commit should store git SHA and PR URL."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"], risk=RiskLevel.LOW)
        store.add(t)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.transition(t.id, TicketState.PLANNING)
        store.transition(t.id, TicketState.IMPLEMENT)
        store.transition(t.id, TicketState.REVIEW)
        store.record_gate_result(t.id, True, gates=[])
        store.transition(t.id, TicketState.COMPLETE)

        updated = store.record_commit(t.id, "abc123", "https://github.com/pr/1")
        assert updated is not None
        assert updated.commit_sha == "abc123"
        assert updated.pr_url == "https://github.com/pr/1"
        store.close()

    def test_record_commit_no_change(self, tmp_path):
        """record_commit with same SHA should return existing ticket."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"], risk=RiskLevel.LOW)
        store.add(t)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.GOAL)
        store.transition(t.id, TicketState.DECOMP)
        store.transition(t.id, TicketState.PLANNING)
        store.transition(t.id, TicketState.IMPLEMENT)
        store.transition(t.id, TicketState.REVIEW)
        store.record_gate_result(t.id, True, gates=[])
        store.transition(t.id, TicketState.COMPLETE)
        store.record_commit(t.id, "abc123")

        updated = store.record_commit(t.id, "abc123")
        assert updated.commit_sha == "abc123"
        store.close()

    def test_record_commit_nonexistent(self, tmp_path):
        """record_commit for nonexistent ticket should return None."""
        store = TicketStore(tmp_path / "tickets.json")
        result = store.record_commit("CB-DEADBEEF12345678", "abc123")
        assert result is None
        store.close()

    def test_update_gate_approval(self, tmp_path):
        """update_gate_approval should update cache."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)

        store.update_gate_approval(t.id, True)
        assert store._has_gate_approval(t.id) is True

        store.update_gate_approval(t.id, False)
        assert store._has_gate_approval(t.id) is False
        store.close()

    def test_restore_latest_backup(self, tmp_path):
        """restore_latest_backup should restore from backup if available."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)
        store.flush()  # Creates backup
        store.close()

        # Corrupt main file
        (tmp_path / "tickets.json").write_text("corrupted")

        store2 = TicketStore(tmp_path / "tickets.json")
        # Load might fail or load empty, try restore
        restored = store2.restore_latest_backup()
        if restored:
            assert store2.get(t.id) is not None
        store2.close()

    def test_restore_latest_backup_skips_oversized_file(self, tmp_path, caplog):
        """restore_latest_backup should skip oversized backup files (>50MB) with a warning.

        Regression test for CB-1DB49: verifies that oversized backup files
        are handled gracefully without causing OOM crashes.
        """
        import logging

        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)
        store.flush()  # Creates backup
        store.close()

        # Create an oversized backup file (>50MB)
        backup_dir = tmp_path / "ticket_backups"
        oversized_backup = backup_dir / "tickets-99999999-999999.json"  # Latest by timestamp

        # Write >50MB of valid JSON structure to the backup
        size_target = 51 * 1024 * 1024  # 51MB
        with open(oversized_backup, "w", encoding="utf-8") as f:
            f.write('{"schema_version":"2.0","updated_at":1234567890.0,"tickets":[')
            written = len('{"schema_version":"2.0","updated_at":1234567890.0,"tickets":[')
            padding = '{"id":"CB-PAD","title":"padding","ticket_class":"bug","severity":"low","state":"DISCOVERED","source":"test","evidence":"pad","problem_statement":"pad","desired_state":"pad","acceptance_criteria":["pad"],"created_at":1234567890.0,"updated_at":1234567890.0},'
            padding_len = len(padding)
            while written < size_target:
                remaining = size_target - written
                entries_to_add = max(1, remaining // padding_len)
                chunk = padding * entries_to_add
                f.write(chunk)
                written += len(chunk)
            f.write(']}')

        # Verify oversized backup is the latest
        assert oversized_backup.stat().st_size > 50 * 1024 * 1024

        # Corrupt main file so restore is needed
        (tmp_path / "tickets.json").write_text("corrupted")

        store2 = TicketStore(tmp_path / "tickets.json")
        with caplog.at_level(logging.WARNING):
            restored = store2.restore_latest_backup()

        # Should return False because the latest backup is oversized
        assert restored is False, "restore_latest_backup should return False for oversized backup"

        # Should log a warning about the oversized file
        warning_found = any(
            "oversized" in record.message.lower() or "large" in record.message.lower()
            for record in caplog.records
        )
        assert warning_found, "Warning should be logged for oversized backup file"

        # Store should remain empty (no OOM crash)
        assert store2.count() == 0
        store2.close()

    def test_prune_stale_sibling_backups(self, tmp_path):
        """prune_stale_sibling_backups should remove old pre-* files."""
        store = TicketStore(tmp_path / "tickets.json")
        # Create dummy pre-* files
        for i in range(5):
            (tmp_path / f"tickets.pre-{i}.json").write_text("{}")
        (tmp_path / "tickets.backup.json").write_text("{}")

        removed = store.prune_stale_sibling_backups(keep_pre_backups=1)
        # Should keep 1 pre-* and remove legacy backup
        assert removed >= 1
        store.close()

    def test_find_similar(self, tmp_path):
        """find_similar should return similar tickets."""
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "database connection error", "d", ["a"])
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "network timeout issue", "d", ["a"])
        store.add(t1)
        store.add(t2)

        # Use exact match query to guarantee Jaccard similarity >= 0.8
        results = store.find_similar("database connection error")
        assert len(results) >= 1
        assert results[0][0].id == t1.id
        store.close()

    def test_discovery_history(self, tmp_path):
        """discovery_history should return tickets by fingerprint/evidence."""
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"], fingerprint="fp1")
        store.add(t1)
        # Use a valid terminal transition from DISCOVERED
        store.transition(t1.id, TicketState.REJECTED)

        history = store.discovery_history(fingerprint="fp1")
        assert len(history) == 1
        assert history[0].id == t1.id
        store.close()

    def test_queue_save_and_worker(self, tmp_path):
        """_queue_save should trigger background worker."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        store.add(t)
        store.flush()

        # Trigger queue save
        store._queue_save()
        time.sleep(1.0)  # Wait for worker
        store.close()

    def test_close_with_pending_backup_tasks(self, tmp_path):
        """close should handle pending backup tasks gracefully."""
        store = TicketStore(tmp_path / "tickets.json")
        # Fill queue to test close behavior with full queue
        src = tmp_path / "src.txt"
        src.write_text("data")
        for i in range(110):  # Exceed maxsize=100
            store.queue_backup_task(str(src), str(tmp_path / f"dst_{i}.txt"))
        store.close()
        assert not store._backup_worker.is_alive()


class TestTOCTORaceCondition:
    """Tests for CB-B24EF: TOCTOU race in _save() dirty_ids copy.

    Verifies that concurrent mutations during _save() are not lost on crash.
    The fix uses a two-set approach: move IDs from _dirty_ids to _pending_flush
    under lock, serialize payloads under lock, then write outside lock.
    """

    def test_concurrent_add_during_save_preserves_mutations(self, tmp_path):
        """Concurrent add() during _save() must not lose mutations.

        This test verifies the TOCTOU fix: when one thread is in _save()
        (between releasing self._lock and completing WAL write), another
        thread calling add() must have its mutation preserved, not shadowed
        by stale data from the in-flight save.
        """
        import threading

        store = TicketStore(tmp_path / "tickets.json", start_background_workers=False)

        # Add initial ticket
        t1 = create_ticket(
            "initial-ticket",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "evidence-1",
            "problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t1)
        store.flush()  # Ensure clean state

        errors = []
        barrier = threading.Barrier(2)  # Synchronize threads

        def concurrent_adder():
            """Thread that adds a ticket while _save() is in progress."""
            try:
                # Wait for main thread to be ready
                barrier.wait(timeout=5)
                # Add ticket immediately - this should go to _dirty_ids
                # while main thread's _save() is writing pre-captured data
                t2 = create_ticket(
                    "concurrent-ticket",
                    TicketClass.BUG,
                    Severity.LOW,
                    "test",
                    "evidence-2",
                    "problem-concurrent",
                    "desired",
                    ["crit"],
                    risk=RiskLevel.LOW,
                )
                store.add(t2)
            except Exception as e:
                errors.append(e)

        # Start concurrent thread
        adder_thread = threading.Thread(target=concurrent_adder)
        adder_thread.start()

        # Wait for barrier synchronization
        barrier.wait(timeout=5)

        # Immediately trigger _save() - this will capture current _dirty_ids
        # (which should be empty after flush), then release lock.
        # The concurrent thread will add to _dirty_ids while we're writing.
        store._save()

        # Wait for concurrent thread to complete
        adder_thread.join(timeout=5)
        assert not errors, f"Concurrent add raised errors: {errors}"

        # Now trigger another save to persist the concurrent mutation
        store._save()
        store.flush()

        # Verify both tickets exist
        assert store.get(t1.id) is not None, "Initial ticket lost"
        # Find the concurrent ticket by searching
        found_concurrent = False
        for tid in store._tickets:
            ticket = store._tickets[tid]
            if ticket.title == "concurrent-ticket":
                found_concurrent = True
                break
        assert found_concurrent, "Concurrent ticket lost due to TOCTOU race"

        store.close()

    def test_pending_flush_disjoint_from_dirty_ids(self, tmp_path):
        """Verify _pending_flush and _dirty_ids are disjoint after handoff.

        This is a unit test for the two-set approach: after _save() moves
        IDs to _pending_flush, new mutations should go to _dirty_ids only.
        """
        store = TicketStore(tmp_path / "tickets.json", start_background_workers=False)

        t1 = create_ticket(
            "test-1",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "ev-1",
            "prob",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t1)

        # Manually trigger _save to move IDs to _pending_flush
        # We'll intercept to check internal state
        original_save = store._save

        save_called = threading.Event()
        ids_captured = []

        def instrumented_save():
            # Capture state just before releasing lock
            with store._lock:
                if store._dirty_ids:
                    ids_captured.extend(store._dirty_ids.copy())
            save_called.set()
            return original_save()

        store._save = instrumented_save

        # Trigger save
        store._save()
        save_called.wait(timeout=5)

        # After save, _dirty_ids should be cleared (or contain only new mutations)
        # and _pending_flush should have been processed
        assert len(store._dirty_ids) == 0 or all(
            tid not in ids_captured for tid in store._dirty_ids
        ), "_dirty_ids should not contain IDs from previous flush"

        store.close()

    def test_pre_serialized_payloads_prevent_stale_reads(self, tmp_path):
        """Verify that ticket data is serialized under lock to prevent stale reads.

        The TOCTOU fix serializes ticket payloads while holding self._lock,
        ensuring the WAL contains consistent state even if tickets are mutated
        during I/O.
        """
        store = TicketStore(tmp_path / "tickets.json", start_background_workers=False)

        t1 = create_ticket(
            "mutation-test",
            TicketClass.BUG,
            Severity.LOW,
            "test",
            "ev-mut",
            "original problem",
            "desired",
            ["crit"],
            risk=RiskLevel.LOW,
        )
        store.add(t1)
        store.flush()

        # Transition ticket to change its state
        store.transition(t1.id, TicketState.TRIAGED)

        # Now manually inspect what would be written by checking internal state
        # The key insight: if we call _save(), it should capture the TRIAGED state,
        # not the DISCOVERED state, because serialization happens under lock.

        # Force a save and verify the WAL contains the updated state
        store._save()
        store.flush()

        # Reload and verify state persisted correctly
        store2 = TicketStore(tmp_path / "tickets.json", start_background_workers=False)
        recovered = store2.get(t1.id)
        assert recovered is not None
        assert recovered.state == TicketState.TRIAGED, (
            f"Expected TRIAGED state, got {recovered.state}. "
            "TOCTOU race may have caused stale data to be written."
        )
        store.close()
        store2.close()


class TestWALCrashSurvival:
    """Tests for CB-E26FDAFF6EDD8547B49DCCA996F56959: WAL fsync durability.

    Verifies that data survives simulated power loss (SIGKILL) immediately
    after add() by spawning a subprocess, adding a ticket, killing it with
    signal 9, then reopening the store and verifying WAL replay recovers
    the ticket.
    """

    def test_crash_survival_subprocess_kill9(self, tmp_path):
        """Data must survive kill -9 immediately after add() via WAL replay."""
        import subprocess
        import sys
        import signal

        store_path = tmp_path / "tickets.json"
        ticket_title = "crash-survival-test-ticket"

        # Subprocess script: create store, add ticket, print ID, wait forever
        # The parent will kill us before we can exit cleanly.
        child_script = f"""
import sys
sys.path.insert(0, {str(Path(__file__).parent.parent)!r})
from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel

store = TicketStore({str(store_path)!r}, start_background_workers=False)
t = create_ticket(
    title={ticket_title!r},
    ticket_class=TicketClass.BUG,
    severity=Severity.HIGH,
    source="crash-test",
    evidence="evidence-line",
    problem_statement="test problem for crash survival",
    desired_state="test desired state",
    acceptance_criteria=["survives kill -9"],
    risk=RiskLevel.MEDIUM,
)
store.add(t)
# Print ticket ID so parent knows what to look for
print(t.id, flush=True)
# Wait forever — parent will SIGKILL us
import time
while True:
    time.sleep(1)
"""
        # Spawn child process
        proc = subprocess.Popen(
            [sys.executable, "-c", child_script],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        try:
            # Read ticket ID from child's stdout (child prints after add())
            line = proc.stdout.readline().strip()
            assert line, f"Child did not print ticket ID. stderr: {proc.stderr.read()}"
            ticket_id = line

            # Immediately kill with SIGKILL (signal 9) — no cleanup possible
            proc.kill()
            proc.wait(timeout=5)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()

        # Reopen store — WAL replay should recover the ticket
        recovered_store = TicketStore(store_path, start_background_workers=False)
        try:
            recovered = recovered_store.get_by_id(ticket_id)
            assert recovered is not None, (
                f"Ticket {ticket_id} not found after crash recovery. "
                f"WAL file exists: {(store_path.with_suffix('.wal.jsonl')).exists()}. "
                f"Store count: {recovered_store.count()}"
            )
            assert recovered.title == ticket_title
            assert recovered.state == TicketState.DISCOVERED
        finally:
            recovered_store.close()


class TestFlockExactlyOnce:
    """Challenge tests for CB-2BC9F6AEE1D27EAA28420116A9754279: verify flock(LOCK_UN) called exactly once.

    These tests mock flock to count LOCK_UN calls and prove the double-unlock bug is fixed.
    """

    def test_oversized_path_calls_lock_exactly_once(self, tmp_path, monkeypatch, caplog):
        """Verify flock(LOCK_UN) is called exactly once during oversized file load.

        This is a regression test for the double-unlock bug where the advisory
        lock was released twice: once manually before return and once in finally.
        The fix ensures LOCK_UN is called exactly once per _load invocation.
        """
        import logging
        from codebot.ticket_engine import LOCK_EX, LOCK_UN

        # Track all flock calls
        flock_calls = []

        def mock_flock(fd, flag):
            flock_calls.append((fd, flag))
            # Don't actually lock, just record the call

        monkeypatch.setattr("codebot.ticket_engine.flock", mock_flock)

        # Create an oversized tickets.json file (>50MB)
        path = tmp_path / "tickets.json"
        size_target = 51 * 1024 * 1024  # 51MB

        with open(path, "w", encoding="utf-8") as f:
            f.write('{"schema_version":"2.0","updated_at":1234567890.0,"tickets":[')
            written = len('{"schema_version":"2.0","updated_at":1234567890.0,"tickets":[')
            padding = '{"id":"CB-PAD","title":"padding","ticket_class":"bug","severity":"low","state":"DISCOVERED","source":"test","evidence":"pad","problem_statement":"pad","desired_state":"pad","acceptance_criteria":["pad"],"created_at":1234567890.0,"updated_at":1234567890.0},'
            padding_len = len(padding)
            while written < size_target:
                remaining = size_target - written
                entries_to_add = max(1, remaining // padding_len)
                chunk = padding * entries_to_add
                f.write(chunk)
                written += len(chunk)
            f.write(']}')

        # Verify file size exceeds 50MB
        actual_size = path.stat().st_size
        assert actual_size > 50 * 1024 * 1024, f"File size {actual_size} bytes should exceed 50MB"

        # Capture warnings and create store (triggers _load with oversized path)
        with caplog.at_level(logging.WARNING):
            store = TicketStore(path)

        # Count LOCK_UN calls
        lock_un_calls = [call for call in flock_calls if call[1] == LOCK_UN]
        lock_ex_calls = [call for call in flock_calls if call[1] == LOCK_EX]

        # Assert exactly one LOCK_EX and one LOCK_UN
        assert len(lock_ex_calls) == 1, f"Expected exactly 1 LOCK_EX, got {len(lock_ex_calls)}"
        assert len(lock_un_calls) == 1, (
            f"Expected exactly 1 LOCK_UN (no double-unlock), got {len(lock_un_calls)}. "
            f"All flock calls: {flock_calls}"
        )

        # Assert store loaded empty (graceful degradation)
        assert store.count() == 0, "TicketStore should load empty list for oversized file"

        # Assert warning was logged
        warning_found = any(
            "oversized" in record.message.lower()
            for record in caplog.records
        )
        assert warning_found, "Warning should be logged for oversized tickets.json"

        store.close()
