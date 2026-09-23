"""Regression tests for scheduler v2 slot-token lifecycle.

Verifies that gate tokens are properly released through all agent
lifecycle paths: normal exit, failed spawn, drift reconciliation,
and sustained dispatch cycles.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from codebot.scheduler_v2.dispatcher import Scheduler
from codebot.scheduler_v2.lifecycle import AgentRecord, AgentState, FakeClock


class _FakeProcess:
    def __init__(self, pid: int = 12345, alive: bool = True) -> None:
        self.pid = pid
        self._alive = alive
        self.returncode = None if alive else 0

    def poll(self):
        return None if self._alive else self.returncode


class _FakeTicket:
    def __init__(self, tid: str, state_val: str = "IMPLEMENT") -> None:
        self.id = tid
        self.state = SimpleNamespace(value=state_val)
        self.priority = "high"
        self.created_at = time.time()
        self.updated_at = time.time()
        self.ticket_class = SimpleNamespace(value="feature")
        self.risk = SimpleNamespace(value="low")
        self.blocked_by = []
        self.acceptance_criteria = []
        self.affected_modules = []
        self.required_tests = []
        self.reviewer_feedback = []


class _FakeStore:
    def __init__(self, tickets: list[_FakeTicket]) -> None:
        self._tickets = {t.id: t for t in tickets}
        self._by_state: dict[str, list[_FakeTicket]] = {}
        for t in tickets:
            sv = t.state.value
            self._by_state.setdefault(sv, []).append(t)

    def list_by_state(self, state_enum):
        name = state_enum.name if hasattr(state_enum, "name") else str(state_enum)
        return self._by_state.get(name, [])

    def get(self, tid: str):
        return self._tickets.get(tid)

    def summary(self):
        return {sv: len(ts) for sv, ts in self._by_state.items()}


def _make_scheduler(tmp_path: Path, max_slots: int = 2) -> tuple[Scheduler, FakeClock]:
    clock = FakeClock(initial=1000.0)
    sched = Scheduler(
        max_slots=max_slots,
        state_dir=str(tmp_path),
        clock=clock,
        stagger_seconds=0.001,
    )
    return sched, clock


class TestSlotReleaseOnExit:
    def test_spawn_and_exit_frees_slot(self, tmp_path: Path) -> None:
        sched, clock = _make_scheduler(tmp_path, max_slots=1)
        store = _FakeStore([_FakeTicket("T-1")])

        dispatched = sched.tick(store=store)
        assert dispatched == 1
        assert sched.gate.count_active() == 1

        ready = sched.gate.dequeue_ready()
        assert len(ready) == 1
        agent_id = ready[0].agent_id

        record = AgentRecord(
            agent_id=agent_id, ticket_id="T-1", role="implementer",
            model="test-model", pid=999, state=AgentState.STARTING,
            created_at=clock.now(),
        )
        sched.register_agent(record)

        sched.finalize_agent(agent_id, outcome="exit-code-0")
        assert sched.gate.count_active() == 0

        store2 = _FakeStore([_FakeTicket("T-2")])
        dispatched2 = sched.tick(store=store2)
        assert dispatched2 == 1
        assert sched.gate.count_active() == 1


class TestFailedSpawnCancelsDispatch:
    def test_failed_spawn_releases_slot(self, tmp_path: Path) -> None:
        sched, clock = _make_scheduler(tmp_path, max_slots=1)
        store = _FakeStore([_FakeTicket("T-1")])

        dispatched = sched.tick(store=store)
        assert dispatched == 1
        assert sched.gate.count_active() == 1

        ready = sched.gate.dequeue_ready()
        assert len(ready) == 1
        req = ready[0]

        sched.gate.cancel_dispatch(req.agent_id, req.ticket_id)
        assert sched.gate.count_active() == 0

        store2 = _FakeStore([_FakeTicket("T-2")])
        dispatched2 = sched.tick(store=store2)
        assert dispatched2 == 1


class TestDriftReconciliation:
    def test_finalize_absent_agents_reclaims_leaked_tokens(self, tmp_path: Path) -> None:
        sched, clock = _make_scheduler(tmp_path, max_slots=3)

        for i in range(3):
            tid = f"T-{i}"
            result = sched.gate.try_dispatch(
                ticket_id=tid, agent_id=f"agent-{i}", role="implementer", model="m"
            )
            assert result.success
            record = AgentRecord(
                agent_id=f"agent-{i}", ticket_id=tid, role="implementer",
                model="m", pid=100 + i, state=AgentState.STARTING,
                created_at=clock.now(),
            )
            sched.register_agent(record)

        assert sched.gate.count_active() == 3

        live_ids = {"agent-1"}
        finalized = sched.finalize_absent_agents(live_ids)

        assert finalized == 2
        assert sched.gate.count_active() == 1

        store = _FakeStore([_FakeTicket("T-new")])
        dispatched = sched.tick(store=store)
        assert dispatched >= 1

    def test_finalize_absent_ignores_already_dead(self, tmp_path: Path) -> None:
        sched, clock = _make_scheduler(tmp_path, max_slots=2)

        result = sched.gate.try_dispatch(
            ticket_id="T-1", agent_id="agent-1", role="implementer", model="m"
        )
        assert result.success
        record = AgentRecord(
            agent_id="agent-1", ticket_id="T-1", role="implementer",
            model="m", pid=100, state=AgentState.STARTING,
            created_at=clock.now(),
        )
        sched.register_agent(record)

        sched.finalize_agent("agent-1", outcome="already-dead")
        assert sched.gate.count_active() == 0

        finalized = sched.finalize_absent_agents(set())
        assert finalized == 0


class TestSustainedCyclesNoLeak:
    def test_n_cycles_no_token_leak(self, tmp_path: Path) -> None:
        sched, clock = _make_scheduler(tmp_path, max_slots=2)

        for cycle in range(10):
            store = _FakeStore([_FakeTicket(f"T-{cycle}-a"), _FakeTicket(f"T-{cycle}-b")])
            dispatched = sched.tick(store=store)
            assert dispatched <= 2

            clock.advance(1.0)
            ready = sched.gate.dequeue_ready()
            agent_ids = []
            for req in ready:
                record = AgentRecord(
                    agent_id=req.agent_id, ticket_id=req.ticket_id,
                    role=req.role, model=req.model or "m", pid=cycle * 100,
                    state=AgentState.STARTING, created_at=clock.now(),
                )
                sched.register_agent(record)
                agent_ids.append(req.agent_id)

            for aid in agent_ids:
                sched.finalize_agent(aid, outcome="cycle-complete")

            assert sched.gate.count_active() == 0, (
                f"Token leak at cycle {cycle}: {sched.gate.count_active()} active"
            )


class TestHeartbeatRefresh:
    def test_refresh_prevents_stale_detection(self, tmp_path: Path) -> None:
        sched, clock = _make_scheduler(tmp_path, max_slots=1)

        result = sched.gate.try_dispatch(
            ticket_id="T-1", agent_id="agent-1", role="implementer", model="m"
        )
        assert result.success
        record = AgentRecord(
            agent_id="agent-1", ticket_id="T-1", role="implementer",
            model="m", pid=100, state=AgentState.RUNNING,
            created_at=clock.now(), last_heartbeat=clock.now(),
        )
        sched.register_agent(record)

        clock.advance(60.0)
        sched.refresh_heartbeat("agent-1")

        finalized = sched.finalize_absent_agents({"agent-1"})
        assert finalized == 0
        assert sched.gate.count_active() == 1
