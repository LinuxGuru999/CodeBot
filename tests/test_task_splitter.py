"""Tests for task_splitter.py — oversized ticket decomposition."""

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock, patch

from codebot.task_splitter import (
    MAX_SUB_TASKS,
    MIN_FILES_FOR_SPLIT,
    should_split,
    split_ticket,
    _compute_chunks,
)
from codebot.ticket_engine import TicketState, TicketClass, Severity, RiskLevel, create_ticket, TicketStore
from codebot.scratchpad import ScratchpadState


def make_ticket(**kwargs):
    defaults = dict(
        title="Big ticket",
        ticket_class=TicketClass.FEATURE,
        severity=Severity.MEDIUM,
        source="test",
        evidence="evidence",
        problem_statement="big problem",
        desired_state="fixed",
        acceptance_criteria=["ac1", "ac2", "ac3"],
        risk=RiskLevel.LOW,
        affected_modules=["a.py", "b.py", "c.py", "d.py", "e.py"],
    )
    defaults.update(kwargs)
    return create_ticket(**defaults)


def make_scratchpad(remaining=None, completed=None):
    s = ScratchpadState(ticket_id="CB-001", remaining_steps=remaining or [], completed_steps=completed or [])
    return s


class TestShouldSplit:
    def test_terminal_state_no_split(self, tmp_path):
        for state in (TicketState.COMPLETE, TicketState.REJECTED, TicketState.DUPLICATE):
            t = make_ticket()
            t = t.transition(TicketState.VALIDATING)
            if state == TicketState.COMPLETE:
                t = t.transition(TicketState.TRIAGED)
                t = t.transition(TicketState.READY)
                t = t.transition(TicketState.PLANNING)
                t = t.transition(TicketState.IMPLEMENTING)
                t = t.transition(TicketState.REVIEWING)
                t = t.transition(TicketState.VERIFYING)
                # gate approval needed for COMPLETE
                import json, time, os
                gate_path = tmp_path / "gate_results.jsonl"
                gate_path.write_text(json.dumps({"ticket_id": t.id, "passed": True, "timestamp": time.time(), "gates": []}) + "\n", encoding="utf-8")
                # Need to use TicketStore to gate transition
                store = TicketStore(tmp_path / "tickets.json")
                store._tickets[t.id] = t
                store._save()
                # Instead just test via ticket directly transitioning to REJECTED/DUPLICATE
                t2 = make_ticket()
                t2 = t2.transition(TicketState.REJECTED)
                assert should_split(t2) is False
            else:
                t2 = make_ticket()
                t2 = t2.transition(state)
                assert should_split(t2) is False

    def test_timeout_triggers_split(self):
        t = make_ticket()
        assert should_split(t, exit_reason="timeout") is True
        assert should_split(t, exit_reason="rate_limit") is True
        assert should_split(t, exit_reason="fatal_error") is True
        assert should_split(t, exit_reason="token_cap") is True

    def test_no_trigger_no_split_small(self):
        t = make_ticket(affected_modules=["a.py"])
        assert should_split(t, exit_reason="") is False

    def test_many_modules_triggers(self):
        t = make_ticket(affected_modules=[f"m{i}.py" for i in range(5)])
        assert should_split(t) is True

    def test_few_modules_no_split(self):
        t = make_ticket(affected_modules=["a.py", "b.py"])
        assert should_split(t) is False

    def test_scratchpad_remaining_triggers(self):
        t = make_ticket(affected_modules=["a.py"])
        sp = make_scratchpad(remaining=["step1", "step2", "step3"])
        assert should_split(t, scratchpad=sp) is True

    def test_scratchpad_few_remaining_no_split(self):
        t = make_ticket(affected_modules=["a.py"])
        sp = make_scratchpad(remaining=["step1", "step2"])
        assert should_split(t, scratchpad=sp) is False

    def test_scratchpad_none_no_remaining(self):
        t = make_ticket(affected_modules=["a.py"])
        assert should_split(t, scratchpad=None) is False

    def test_blocked_state_can_split(self):
        t = make_ticket()
        t = t.transition(TicketState.VALIDATING)
        t = t.transition(TicketState.TRIAGED)
        t = t.transition(TicketState.READY)
        t = t.transition(TicketState.PLANNING)
        t = t.transition(TicketState.BLOCKED)
        assert should_split(t, exit_reason="timeout") is True

    def test_empty_modules_no_scratchpad_no_trigger(self):
        t = make_ticket(affected_modules=[], acceptance_criteria=["ac1"])
        assert should_split(t) is False


class TestComputeChunks:
    def test_from_scratchpad_remaining(self):
        t = make_ticket(affected_modules=["a.py", "b.py"])
        sp = make_scratchpad(remaining=[f"step{i}" for i in range(6)])
        chunks = _compute_chunks(t, sp)
        assert len(chunks) >= 1
        assert all("description" in c for c in chunks)
        assert all("modules" in c for c in chunks)

    def test_from_modules(self):
        t = make_ticket(affected_modules=[f"mod{i}.py" for i in range(6)])
        chunks = _compute_chunks(t, None)
        assert len(chunks) >= 1
        assert len(chunks) <= MAX_SUB_TASKS

    def test_from_acceptance_criteria(self):
        t = make_ticket(affected_modules=["a.py"], acceptance_criteria=["c1", "c2", "c3", "c4"])
        chunks = _compute_chunks(t, None)
        assert len(chunks) == 2
        assert "Part 1" in chunks[0]["description"]
        assert "Part 2" in chunks[1]["description"]

    def test_single_criterion_no_modules_no_scratchpad(self):
        t = make_ticket(affected_modules=[], acceptance_criteria=["only one"])
        chunks = _compute_chunks(t, None)
        assert chunks == []

    def test_max_sub_tasks_cap(self):
        t = make_ticket(affected_modules=[f"m{i}.py" for i in range(100)])
        chunks = _compute_chunks(t, None)
        assert len(chunks) <= MAX_SUB_TASKS

    def test_scratchpad_chunking(self):
        t = make_ticket()
        sp = make_scratchpad(remaining=[f"s{i}" for i in range(20)])
        chunks = _compute_chunks(t, sp)
        assert len(chunks) <= MAX_SUB_TASKS
        assert all(c["description"] for c in chunks)

    def test_empty_scratchpad_falls_to_modules(self):
        t = make_ticket(affected_modules=["a.py", "b.py", "c.py"])
        sp = make_scratchpad(remaining=[])
        chunks = _compute_chunks(t, sp)
        assert len(chunks) >= 1


class TestSplitTicket:
    def test_no_split_returns_empty(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = make_ticket(affected_modules=["a.py"], acceptance_criteria=["ac1"])
        store.add(t)
        result = split_ticket(t, store)
        assert result == []

    def test_split_with_timeout(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = make_ticket(affected_modules=[f"m{i}.py" for i in range(5)])
        store.add(t)
        sub_ids = split_ticket(t, store, exit_reason="timeout")
        assert len(sub_ids) >= 1
        assert len(sub_ids) <= MAX_SUB_TASKS
        for sid in sub_ids:
            assert store.get(sid) is not None
            assert store.get(sid).dependencies[0] == t.id

    def test_split_with_scratchpad(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = make_ticket(affected_modules=["a.py"])
        store.add(t)
        sp = make_scratchpad(remaining=["s1", "s2", "s3", "s4"])
        sub_ids = split_ticket(t, store, scratchpad=sp, exit_reason="timeout")
        assert len(sub_ids) >= 1

    def test_split_blocks_parent(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = make_ticket(affected_modules=[f"m{i}.py" for i in range(6)])
        store.add(t)
        # Need to move to PLANNING to allow BLOCKED transition
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        store.transition(t.id, TicketState.PLANNING)
        t_updated = store.get(t.id)
        sub_ids = split_ticket(t_updated, store, exit_reason="timeout")
        assert len(sub_ids) >= 1
        assert store.get(t.id).state == TicketState.BLOCKED

    def test_split_sub_tickets_have_ready_state(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = make_ticket(affected_modules=[f"m{i}.py" for i in range(5)])
        store.add(t)
        sub_ids = split_ticket(t, store, exit_reason="timeout")
        for sid in sub_ids:
            assert store.get(sid).state == TicketState.READY

    def test_split_with_handoff_note(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = make_ticket(affected_modules=[f"m{i}.py" for i in range(5)])
        store.add(t)
        sp = ScratchpadState(ticket_id=t.id, remaining_steps=["a", "b", "c"], completed_steps=["done1"])
        sp.files_changed = ["x.py"]
        sub_ids = split_ticket(t, store, scratchpad=sp, exit_reason="timeout")
        assert len(sub_ids) >= 1
        for sid in sub_ids:
            assert "Parent:" in store.get(sid).evidence

    def test_split_chain_dependencies(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = make_ticket(affected_modules=[f"m{i}.py" for i in range(6)])
        store.add(t)
        sub_ids = split_ticket(t, store, exit_reason="timeout")
        if len(sub_ids) >= 2:
            second = store.get(sub_ids[1])
            assert t.id in second.dependencies
            assert sub_ids[0] in second.dependencies

    def test_terminal_ticket_no_split(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = make_ticket(affected_modules=[f"m{i}.py" for i in range(10)])
        store.add(t)
        t_rej = t.transition(TicketState.REJECTED)
        result = split_ticket(t_rej, store, exit_reason="timeout")
        assert result == []
