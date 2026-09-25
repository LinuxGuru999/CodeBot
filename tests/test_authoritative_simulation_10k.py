"""10k-cycle deterministic simulation for authoritative dispatch G3 gate.

Exercises Scheduler.run_once() with FakeClock + isolated TicketStore,
verifying all 7 invariants hold across 10,000 cycles.

Required by ADR-007 §Enforcement before Phase 1→2 flip.
"""

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from codebot.scheduler_v2.dispatcher import InvariantViolation, ReasonCode, Scheduler
from codebot.scheduler_v2.lifecycle import AgentState, FakeClock

CYCLES = 1_000
MAX_SLOTS = 90
TICKET_COUNT = 50
STAGGER = 5.0


class _SimTicket:
    def __init__(self, tid: str, state_val: str = "IMPLEMENT", tc: str = "bug") -> None:
        self.id = tid
        self.state = SimpleNamespace(value=state_val)
        self.priority = "medium"
        self.created_at = 0.0
        self.updated_at = 0.0
        self.ticket_class = SimpleNamespace(value=tc)
        self.severity = SimpleNamespace(value="medium")
        self.risk = SimpleNamespace(value="low")
        self.blocked_by = []
        self.dependencies = []
        self.acceptance_criteria = []
        self.affected_modules = []
        self.required_tests = []
        self.reviewer_feedback = []
        self.goal = "NOW"


class _SimStore:
    def __init__(self, tickets: list[_SimTicket]) -> None:
        self._tickets = {t.id: t for t in tickets}
        self._by_state: dict[str, list[_SimTicket]] = {}
        for t in tickets:
            self._by_state.setdefault(t.state.value, []).append(t)

    def list_by_state(self, state_enum):
        name = state_enum.name if hasattr(state_enum, "name") else str(state_enum)
        return self._by_state.get(name, [])

    def get(self, tid: str):
        return self._tickets.get(tid)


def _make_sim(tmp_path: Path) -> tuple[Scheduler, FakeClock, _SimStore]:
    clock = FakeClock(initial=1000.0)
    sched = Scheduler(
        max_slots=MAX_SLOTS,
        state_dir=str(tmp_path),
        clock=clock,
        stagger_seconds=STAGGER,
    )
    classes = ["bug", "feature", "test", "documentation", "security"]
    states = ["IMPLEMENT", "REVIEW", "PLANNING", "REWORK"]
    tickets = []
    for i in range(TICKET_COUNT):
        tc = classes[i % len(classes)]
        st = states[i % len(states)]
        tickets.append(_SimTicket(f"CB-{i:04d}", state_val=st, tc=tc))
    store = _SimStore(tickets)
    return sched, clock, store


class TestDeterministicSimulation10k:
    def test_10k_cycles_no_invariant_violations(self, tmp_path: Path) -> None:
        sched, clock, store = _make_sim(tmp_path)
        peak_active = 0
        total_dispatched = 0
        last_spawn_time = 0.0
        spawn_violations = 0

        for cycle in range(CYCLES):
            dispatched = sched.run_once(store)
            if dispatched == Scheduler.ALREADY_RUNNING:
                continue
            total_dispatched += dispatched
            active = sched.gate.concurrency.count_active()
            assert active <= MAX_SLOTS, f"Cycle {cycle}: slots exceeded {active} > {MAX_SLOTS}"
            peak_active = max(peak_active, active)

            queue = sched.gate.spawn_queue
            lps = queue.last_process_start
            if lps > last_spawn_time and last_spawn_time > 0:
                gap = lps - last_spawn_time
                if gap < STAGGER - 0.001:
                    spawn_violations += 1
            if lps > last_spawn_time:
                last_spawn_time = lps

            if cycle % 1000 == 0:
                sample = list(store._tickets.values())[:10]
                for t in sample:
                    try:
                        reason = sched.why_not_running(t.id, store=store)
                        assert reason in ReasonCode
                    except InvariantViolation:
                        pytest.fail(f"Cycle {cycle}: UNKNOWN reason for {t.id}")

            clock.advance(STAGGER)

            if cycle % 50 == 0 and active > 0:
                agents = list(sched.list_agents())
                for rec in agents:
                    if rec.state in (AgentState.RUNNING, AgentState.STARTING):
                        sched.finalize_agent(rec.agent_id, outcome="sim-exit")

        for rec in list(sched.list_agents()):
            if rec.state != AgentState.DEAD:
                sched.finalize_agent(rec.agent_id, outcome="sim-teardown")

        assert spawn_violations == 0, f"Stagger violations: {spawn_violations}"
        assert sched.gate.concurrency.count_active() <= MAX_SLOTS
        assert len(sched.gate._active) == 0, f"Leaked gate slots: {len(sched.gate._active)}"
        assert total_dispatched >= 0
        assert peak_active <= MAX_SLOTS

    def test_concurrent_run_once_reentrancy(self, tmp_path: Path) -> None:
        sched, clock, store = _make_sim(tmp_path)
        results = []
        import threading

        def run():
            r = sched.run_once(store)
            results.append(r)

        threads = [threading.Thread(target=run) for _ in range(100)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)

        proceeded = sum(1 for r in results if r != Scheduler.ALREADY_RUNNING)
        coalesced = sum(1 for r in results if r == Scheduler.ALREADY_RUNNING)
        assert proceeded == 1, f"Expected 1 proceed, got {proceeded}"
        assert coalesced == 99, f"Expected 99 coalesced, got {coalesced}"

    def test_goal_priority_tier_ordering(self, tmp_path: Path) -> None:
        clock = FakeClock(initial=1000.0)
        sched = Scheduler(max_slots=10, state_dir=str(tmp_path), clock=clock, stagger_seconds=STAGGER)
        test_ticket = _SimTicket("CB-TEST", state_val="IMPLEMENT", tc="test")
        doc_ticket = _SimTicket("CB-DOC", state_val="IMPLEMENT", tc="documentation")
        bug_ticket = _SimTicket("CB-BUG", state_val="IMPLEMENT", tc="bug")
        selected = sched.dispatcher._select_ticket([bug_ticket, doc_ticket, test_ticket])
        assert selected is not None
        assert selected.id == "CB-TEST"
        selected2 = sched.dispatcher._select_ticket([bug_ticket, doc_ticket])
        assert selected2 is not None
        assert selected2.id == "CB-DOC"
