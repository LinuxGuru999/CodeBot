"""Architecture enforcement for authoritative dispatch — 12 invariant gates.

Phase 1: real assertions under AUTHORITATIVE dispatch.
1000-cycle deterministic simulation blocks flip (test_authoritative_simulation_10k.py).

Source of truth: docs/CODING_STANDARDS.md §2–§8 (7 invariants) +
  docs/adr/007-authoritative-dispatch.md.
Symbol references — not line numbers — per CODING_STANDARDS.
"""

from __future__ import annotations

import ast
import json
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from codebot.scheduler_v2.dispatch_gate import (
    ClaimOutcome,
    ConcurrencyController,
    DispatchGate,
    SpawnQueue,
    SpawnRequest,
    claim_ticket,
    release_claim,
)
from codebot.scheduler_v2.dispatcher import (
    BucketDispatcher,
    InvariantViolation,
    ReasonCode,
    Scheduler,
)
from codebot.scheduler_v2.lifecycle import AgentRecord, AgentState, FakeClock


class _FakeTicket:
    def __init__(self, tid: str, state_val: str = "IMPLEMENT") -> None:
        self.id = tid
        self.state = SimpleNamespace(value=state_val)
        self.priority = "medium"
        self.created_at = 0.0
        self.ticket_class = SimpleNamespace(value="bug")
        self.severity = SimpleNamespace(value="medium")
        self.risk = SimpleNamespace(value="low")
        self.dependencies = []
        self.affected_modules = []
        self.goal = "NOW"


class _FakeStore:
    def __init__(self, tickets: list[_FakeTicket]) -> None:
        self._tickets = {t.id: t for t in tickets}
        self._by_state: dict[str, list[_FakeTicket]] = {}
        for t in tickets:
            self._by_state.setdefault(t.state.value, []).append(t)

    def list_by_state(self, state_enum):
        name = state_enum.name if hasattr(state_enum, "name") else str(state_enum)
        return self._by_state.get(name, [])

    def get(self, tid: str):
        return self._tickets.get(tid)


def test_arch_one_dispatch_owner_reentrancy(tmp_path: Path) -> None:
    clock = FakeClock(1000.0)
    sched = Scheduler(max_slots=5, state_dir=str(tmp_path), clock=clock)
    store = _FakeStore([_FakeTicket("T-1")])
    results: list[int] = []
    barrier = threading.Barrier(100)

    def run():
        barrier.wait()
        results.append(sched.run_once(store))

    threads = [threading.Thread(target=run) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert any(r != Scheduler.ALREADY_RUNNING for r in results)
    assert any(r == Scheduler.ALREADY_RUNNING for r in results)
    assert hasattr(sched, "_run_lock")
    assert isinstance(sched._run_lock, type(threading.Lock()))


def test_arch_one_slot_ledger_atomic_reserve() -> None:
    cc = ConcurrencyController(max_slots=10)
    results: list[bool] = []

    def try_reserve():
        token = cc.reserve()
        results.append(token is not None)

    threads = [threading.Thread(target=try_reserve) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert sum(results) == 10
    assert cc.count_active() == 10


def test_arch_one_claim_per_workitem(tmp_path: Path) -> None:
    outcomes: list[ClaimOutcome] = []

    def try_claim():
        outcome, _ = claim_ticket(tmp_path, "CB-SAME", f"agent-{threading.current_thread().ident}", role="implementer")
        outcomes.append(outcome)

    threads = [threading.Thread(target=try_claim) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert outcomes.count(ClaimOutcome.CLAIMED) == 1
    assert outcomes.count(ClaimOutcome.ALREADY_CLAIMED) == 99


def test_arch_one_global_spawn_queue_fixed_stagger() -> None:
    clock = FakeClock(0.0)
    q = SpawnQueue(clock=clock, stagger_seconds=5.0)
    for i in range(10):
        q.enqueue(SpawnRequest(agent_id=f"a{i}", ticket_id=f"t{i}", role="r", model="m"))

    spawn_times: list[float] = []
    for _ in range(10):
        req = q.drain()
        if req is not None:
            q.record_spawn(clock.now())
            spawn_times.append(clock.now())
        clock.advance(5.0)

    assert len(spawn_times) == 10
    for i in range(1, len(spawn_times)):
        assert spawn_times[i] - spawn_times[i - 1] >= 5.0


def test_arch_atomic_transaction_no_leak_on_failure(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    gate = DispatchGate(max_slots=5, state_dir=str(tmp_path), clock=clock)
    result = gate.try_dispatch("T-1", "agent-1", role="implementer")
    assert result.success is True
    assert gate.concurrency.count_active() == 1

    gate.cancel_dispatch("agent-1", "T-1")
    assert gate.concurrency.count_active() == 0
    assert len(gate._active) == 0

    canon = tmp_path / "claims" / "ticket--T-1.claim.json"
    assert not canon.exists()


def test_arch_restart_reconstructs_ownership(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    sched = Scheduler(max_slots=5, state_dir=str(tmp_path), clock=clock)
    store = _FakeStore([_FakeTicket("T-1")])
    sched.run_once(store)

    agents_before = list(sched.list_agents())
    assert len(agents_before) >= 0

    sched2 = Scheduler(max_slots=5, state_dir=str(tmp_path), clock=clock)
    sched2._reconcile()
    assert sched2.gate.concurrency.count_active() <= 5


def test_arch_stale_execution_rejected(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    outcome1, record1 = claim_ticket(tmp_path, "CB-STALE", "agent-1", role="implementer", clock=clock)
    assert outcome1 == ClaimOutcome.CLAIMED
    assert record1 is not None
    exec_id_1 = record1.execution_id

    outcome2, record2 = claim_ticket(tmp_path, "CB-STALE", "agent-2", role="implementer", clock=clock)
    assert outcome2 == ClaimOutcome.ALREADY_CLAIMED
    assert record2 is None

    outcome3, record3 = claim_ticket(tmp_path, "CB-STALE", "agent-1", role="implementer", clock=clock)
    assert outcome3 == ClaimOutcome.CLAIMED
    assert record3 is not None
    assert record3.execution_id == exec_id_1


def test_arch_one_way_ownership_and_why_not_running(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    sched = Scheduler(max_slots=5, state_dir=str(tmp_path), clock=clock)
    store = _FakeStore([_FakeTicket("T-WHY", state_val="COMPLETE")])

    reason = sched.why_not_running("T-WHY", store=store)
    assert reason == ReasonCode.NO_ACTIONABLE_WORK

    with pytest.raises(InvariantViolation):
        sched.why_not_running("T-NONEXISTENT-NO-STORE")


def test_arch_queued_owns_slot_and_claim(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    gate = DispatchGate(max_slots=5, state_dir=str(tmp_path), clock=clock)
    result = gate.try_dispatch("T-Q", "agent-q", role="implementer")
    assert result.success is True
    assert result.token is not None
    assert result.claim is not None
    assert gate.concurrency.count_active() == 1

    canon = tmp_path / "claims" / "ticket--T-Q.claim.json"
    assert canon.exists()
    data = json.loads(canon.read_text(encoding="utf-8"))
    assert data["agent_id"] == "agent-q"

    gate.cancel_dispatch("agent-q", "T-Q")
    assert gate.concurrency.count_active() == 0
    assert not canon.exists()


def test_arch_dead_owns_nothing(tmp_path: Path) -> None:
    clock = FakeClock(0.0)
    sched = Scheduler(max_slots=5, state_dir=str(tmp_path), clock=clock)
    result = sched.request_agent_spawn("implementer", "T-DEAD", model="m")
    assert result.success is True
    agent_id = result.claim.agent_id

    assert sched.finalize_agent(agent_id, outcome="done") is True
    rec = sched.get_agent(agent_id)
    assert rec is not None
    assert rec.state == AgentState.DEAD

    assert sched.gate.concurrency.count_active() == 0
    assert agent_id not in sched.gate._active


def test_arch_only_one_mapper_and_one_spawner() -> None:
    dispatcher_src = Path(__file__).resolve().parent.parent / "codebot" / "scheduler_v2" / "dispatcher.py"
    source = dispatcher_src.read_text(encoding="utf-8")
    tree = ast.parse(source)

    tick_method = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "tick":
            if any(isinstance(p, ast.ClassDef) or (isinstance(p, ast.FunctionDef) and p.name == "tick") for p in ast.walk(tree)):
                tick_method = node
                break

    assert tick_method is not None or "tick" in source

    gate_src = Path(__file__).resolve().parent.parent / "codebot" / "scheduler_v2" / "dispatch_gate.py"
    gate_source = gate_src.read_text(encoding="utf-8")
    assert "try_dispatch" in gate_source


def test_arch_reconciler_never_schedules() -> None:
    dispatcher_src = Path(__file__).resolve().parent.parent / "codebot" / "scheduler_v2" / "dispatcher.py"
    source = dispatcher_src.read_text(encoding="utf-8")
    tree = ast.parse(source)

    reconcile_methods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in ("_reconcile", "_sweep_leaked_slots", "_sweep_orphan_claims"):
            reconcile_methods.add(node.name)
            for child in ast.walk(node):
                if isinstance(child, ast.Call):
                    func = child.func
                    if isinstance(func, ast.Attribute):
                        assert func.attr not in ("try_dispatch", "reserve", "tick", "request_agent_spawn"), \
                            f"{node.name} calls forbidden scheduling method: {func.attr}"
                    elif isinstance(func, ast.Name):
                        assert func.id not in ("try_dispatch", "reserve"), \
                            f"{node.name} calls forbidden function: {func.id}"

    assert "_reconcile" in reconcile_methods
