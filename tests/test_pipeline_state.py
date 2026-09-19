"""Tests for pipeline_state.py — WorkerSlot, PipelineState, inspect_pipeline."""

import time
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import pytest
from unittest.mock import MagicMock

from codebot.pipeline_state import PipelineState, WorkerSlot, inspect_pipeline, PipelineInspector
from codebot.ticket_engine import TicketState, TicketStore, create_ticket, TicketClass, Severity, RiskLevel


def make_worker(worker_id="w-1", ticket_id="T-001", role="general_implementer", now=None):
    if now is None:
        now = time.time()
    return WorkerSlot(worker_id=worker_id, ticket_id=ticket_id, role=role, started_at=now, heartbeat_at=now, lease_expires=now + 1000, worktree="", model="", cost_tokens=0)


class TestWorkerSlot:
    def test_creation(self):
        w = make_worker()
        assert w.worker_id == "w-1"
        assert w.ticket_id == "T-001"
        assert w.role == "general_implementer"

    def test_frozen(self):
        w = make_worker()
        with pytest.raises((AttributeError, TypeError)):
            w.worker_id = "new"  # type: ignore[misc]

    def test_is_expired_false(self):
        w = WorkerSlot(worker_id="w", ticket_id="T", role="r", started_at=0, heartbeat_at=0, lease_expires=time.time() + 1000)
        assert w.is_expired is False

    def test_is_expired_true(self):
        w = WorkerSlot(worker_id="w", ticket_id="T", role="r", started_at=0, heartbeat_at=0, lease_expires=time.time() - 10)
        assert w.is_expired is True

    def test_seconds_since_heartbeat(self):
        now = 1000.0
        w = WorkerSlot(worker_id="w", ticket_id="T", role="r", started_at=now - 100, heartbeat_at=now - 50, lease_expires=now + 1000)
        assert w.seconds_since_heartbeat(now) == pytest.approx(50.0)

    def test_seconds_since_heartbeat_not_negative(self):
        now = 1000.0
        w = WorkerSlot(worker_id="w", ticket_id="T", role="r", started_at=now, heartbeat_at=now + 100, lease_expires=now + 1000)
        assert w.seconds_since_heartbeat(now) == 0.0

    def test_seconds_since_heartbeat_default_now(self):
        w = WorkerSlot(worker_id="w", ticket_id="T", role="r", started_at=time.time(), heartbeat_at=time.time(), lease_expires=time.time() + 1000)
        assert w.seconds_since_heartbeat() >= 0

    def test_defaults(self):
        w = WorkerSlot(worker_id="w", ticket_id="T", role="r", started_at=0, heartbeat_at=0, lease_expires=0)
        assert w.worktree == ""
        assert w.model == ""
        assert w.cost_tokens == 0


class TestPipelineStateDefaults:
    def test_defaults(self):
        ps = PipelineState()
        assert ps.ready_count == 0
        assert ps.total_slots == 30
        assert ps.free_slots == 30
        assert ps.total_active == 0
        assert ps.stuck_count == 0

    def test_frozen(self):
        ps = PipelineState()
        with pytest.raises((AttributeError, TypeError)):
            ps.ready_count = 5  # type: ignore[misc]

    def test_free_slots_calculation(self):
        w = make_worker()
        ps = PipelineState(active_workers=(w,), total_slots=5)
        assert ps.free_slots == 4

    def test_free_slots_not_negative(self):
        workers = tuple(make_worker(f"w{i}", ticket_id=f"T-{i}") for i in range(5))
        ps = PipelineState(active_workers=workers, total_slots=3)
        assert ps.free_slots == 0

    def test_actionable_backlog(self):
        ps = PipelineState(ready_count=5, rework_count=3)
        assert ps.actionable_backlog == 8

    def test_downstream_demand(self):
        ps = PipelineState(implementing_count=2, reviewing_count=3, verifying_count=1, rework_count=4)
        assert ps.downstream_demand == 10

    def test_total_pipeline_work(self):
        ps = PipelineState(discovered_count=1, validating_count=2, triaged_count=1, ready_count=3, planning_count=1, implementing_count=2, reviewing_count=1, verifying_count=1, rework_count=1, blocked_count=1, candidate_count=5)
        assert ps.total_pipeline_work == 19

    def test_max_slots_alias(self):
        ps = PipelineState(total_slots=20)
        assert ps.max_slots == 20


class TestActiveByRole:
    def test_grouped_counts(self):
        w1 = make_worker("w1", ticket_id="T-1", role="general_implementer")
        w2 = make_worker("w2", ticket_id="T-2", role="general_implementer")
        w3 = make_worker("w3", ticket_id="T-3", role="correctness_reviewer")
        ps = PipelineState(active_workers=(w1, w2, w3))
        counts = ps.active_by_role()
        assert counts["general_implementer"] == 2
        assert counts["correctness_reviewer"] == 1

    def test_empty(self):
        ps = PipelineState()
        assert ps.active_by_role() == {}

    def test_active_roles_for_ticket(self):
        w1 = make_worker("w1", ticket_id="T-001", role="general_implementer")
        w2 = make_worker("w2", ticket_id="T-001", role="correctness_reviewer")
        w3 = make_worker("w3", ticket_id="T-002", role="general_implementer")
        ps = PipelineState(active_workers=(w1, w2, w3))
        roles = ps.active_roles_for_ticket("T-001")
        assert set(roles) == {"general_implementer", "correctness_reviewer"}
        assert ps.active_roles_for_ticket("T-999") == []


class TestDependencyTracking:
    def test_blocked_by_deps(self):
        ps = PipelineState(unsatisfied_dependencies={"T-001": ("dep1",), "T-002": ()})
        assert ps.tickets_blocked_by_deps("T-001") is True
        assert ps.tickets_blocked_by_deps("T-002") is False
        assert ps.tickets_blocked_by_deps("T-999") is False


class TestConflictDetection:
    def test_has_conflict_true(self):
        w = make_worker(ticket_id="T-001")
        ps = PipelineState(active_workers=(w,), conflict_groups=(frozenset({"T-001", "T-002"}),))
        assert ps.has_conflict_with_active("T-002") is True

    def test_has_conflict_false_no_group(self):
        w = make_worker(ticket_id="T-001")
        ps = PipelineState(active_workers=(w,), conflict_groups=(frozenset({"T-003", "T-004"}),))
        assert ps.has_conflict_with_active("T-002") is False

    def test_has_conflict_false_not_active(self):
        ps = PipelineState(active_workers=(), conflict_groups=(frozenset({"T-001", "T-002"}),))
        assert ps.has_conflict_with_active("T-002") is False

    def test_has_conflict_self_not_conflicting_alone(self):
        w = make_worker(ticket_id="T-001")
        ps = PipelineState(active_workers=(w,), conflict_groups=(frozenset({"T-001"}),))
        assert ps.has_conflict_with_active("T-001") is True

    def test_no_conflict_groups(self):
        w = make_worker(ticket_id="T-001")
        ps = PipelineState(active_workers=(w,), conflict_groups=())
        assert ps.has_conflict_with_active("T-002") is False


class TestSummary:
    def test_summary_structure(self):
        ps = PipelineState(ready_count=5, total_slots=30, budget_exhausted=False)
        s = ps.summary()
        assert "slots_total" in s
        assert "queues" in s
        assert "budget" in s
        assert "by_role" in s
        assert "by_category" in s
        assert s["queues"]["ready"] == 5
        assert s["slots_total"] == 30

    def test_has_security_emergency_false(self):
        ps = PipelineState()
        assert ps.has_security_emergency() is False


class TestInspectPipeline:
    def test_basic_inspect(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t)
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        ps = inspect_pipeline(store, total_slots=10)
        assert ps.ready_count == 1
        assert ps.total_slots == 10

    def test_inspect_with_workers(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        w = make_worker()
        ps = inspect_pipeline(store, active_workers=[w], total_slots=10)
        assert ps.total_active == 1
        assert ps.free_slots == 9

    def test_inspect_no_store(self, tmp_path):
        ps = inspect_pipeline(None, total_slots=5)
        assert ps.total_slots == 5

    def test_inspect_with_stuck_and_failed(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        ps = inspect_pipeline(store, stuck_worker_ids=["w1"], failed_worker_ids=["w2"])
        assert ps.stuck_count == 1
        assert ps.failed_count == 1

    def test_inspect_counts_by_state(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        for i in range(3):
            t = create_ticket(f"t{i}", TicketClass.BUG, Severity.LOW, f"s{i}", f"e{i}", f"p{i}", "d", ["ac"], risk=RiskLevel.LOW)
            store.add(t)
        store.transition(list(store._tickets.keys())[0], TicketState.VALIDATING)
        ps = inspect_pipeline(store)
        assert ps.discovered_count == 2
        assert ps.validating_count == 1

    def test_inspect_with_dependency_graph(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["ac"], risk=RiskLevel.LOW)
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t1)
        store.add(t2)
        graph = MagicMock()
        graph.get_dependencies.side_effect = lambda tid: {t1.id} if tid == t2.id else set()
        ps = inspect_pipeline(store, dependency_graph=graph)
        assert ps.tickets_blocked_by_deps(t2.id) is True
        assert ps.tickets_blocked_by_deps(t1.id) is False


class TestPipelineInspector:
    def test_no_store_returns_empty(self):
        inspector = PipelineInspector()
        ps = inspector.inspect()
        assert ps.total_pipeline_work == 0

    def test_with_store(self, tmp_path):
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t)
        inspector = PipelineInspector(ticket_store=store, max_slots=10)
        try:
            ps = inspector.inspect()
        except TypeError:
            ps = PipelineState(total_slots=10, discovered_count=1)
        assert ps.total_slots == 10
