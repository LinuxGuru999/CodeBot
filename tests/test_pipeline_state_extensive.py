"""Extensive tests for codebot.pipeline_state — frozen snapshot & merged implementation bucket."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot.pipeline_state import PipelineInspector, PipelineState, WorkerSlot, inspect_pipeline

FIXED_NOW = 1_700_000_000.0


def _slot(
    worker_id: str = "w-1",
    ticket_id: str = "T-001",
    role: str = "general_implementer",
    now: float = FIXED_NOW,
    heartbeat_offset: float = 0.0,
    lease_offset: float = 1000.0,
    worktree: str = "",
    model: str = "",
    cost_tokens: int = 0,
) -> WorkerSlot:
    return WorkerSlot(
        worker_id=worker_id,
        ticket_id=ticket_id,
        role=role,
        started_at=now - 100.0,
        heartbeat_at=now + heartbeat_offset,
        lease_expires=now + lease_offset,
        worktree=worktree,
        model=model,
        cost_tokens=cost_tokens,
    )


class _BadSummaryStore:
    def summary(self) -> dict[str, int]:
        raise RuntimeError("disk failure")

    def list_by_state(self, _state: object) -> list[object]:
        return []


class _FakeSummaryStore:
    def __init__(self, data: dict[str, int]) -> None:
        self._data: dict[str, int] = data

    def summary(self) -> dict[str, int]:
        return dict(self._data)

    def list_by_state(self, _state: object) -> list[object]:
        return []


class TestWorkerSlotExtensive:
    def test_Given_fresh_lease_When_is_expired_Then_false(self) -> None:
        slot = WorkerSlot(
            worker_id="w",
            ticket_id="T",
            role="r",
            started_at=0,
            heartbeat_at=0,
            lease_expires=time.time() + 1000,
        )
        assert slot.is_expired is False

    def test_Given_expired_lease_When_is_expired_Then_true(self) -> None:
        slot = WorkerSlot(
            worker_id="w",
            ticket_id="T",
            role="r",
            started_at=0,
            heartbeat_at=0,
            lease_expires=time.time() - 10,
        )
        assert slot.is_expired is True

    def test_Given_lease_expires_exactly_now_When_is_expired_Then_false_or_true_deterministic(self) -> None:
        now = FIXED_NOW
        slot = WorkerSlot(
            worker_id="w",
            ticket_id="T",
            role="r",
            started_at=now,
            heartbeat_at=now,
            lease_expires=now,
        )
        with patch("codebot.pipeline_state.time.time", return_value=now):
            assert slot.is_expired is False
        with patch("codebot.pipeline_state.time.time", return_value=now + 0.001):
            assert slot.is_expired is True

    def test_Given_heartbeat_50s_ago_When_seconds_since_heartbeat_Then_50(self) -> None:
        now = FIXED_NOW
        slot = WorkerSlot(
            worker_id="w",
            ticket_id="T",
            role="r",
            started_at=now - 100,
            heartbeat_at=now - 50,
            lease_expires=now + 1000,
        )
        assert slot.seconds_since_heartbeat(now) == pytest.approx(50.0)

    def test_Given_heartbeat_in_future_When_seconds_since_heartbeat_Then_clamped_zero(self) -> None:
        now = FIXED_NOW
        slot = WorkerSlot(
            worker_id="w",
            ticket_id="T",
            role="r",
            started_at=now,
            heartbeat_at=now + 100,
            lease_expires=now + 1000,
        )
        assert slot.seconds_since_heartbeat(now) == 0.0

    def test_Given_no_now_arg_When_seconds_since_heartbeat_Then_uses_time_time(self) -> None:
        slot = WorkerSlot(
            worker_id="w",
            ticket_id="T",
            role="r",
            started_at=time.time(),
            heartbeat_at=time.time(),
            lease_expires=time.time() + 1000,
        )
        result = slot.seconds_since_heartbeat()
        assert result >= 0.0
        assert result < 5.0

    def test_Given_frozen_slot_When_mutate_Then_raises(self) -> None:
        slot = _slot()
        with pytest.raises((AttributeError, TypeError)):
            setattr(slot, "worker_id", "new")
        slot2 = _slot(worker_id="w-2")
        with pytest.raises((AttributeError, TypeError)):
            setattr(slot2, "worker_id", "hacked")

    def test_Given_defaults_When_construct_minimal_Then_worktree_model_cost_zero(self) -> None:
        slot = WorkerSlot(worker_id="w", ticket_id="T", role="r", started_at=0, heartbeat_at=0, lease_expires=0)
        assert slot.worktree == ""
        assert slot.model == ""
        assert slot.cost_tokens == 0

    def test_Given_explicit_cost_tokens_When_construct_Then_preserved(self) -> None:
        slot = _slot(cost_tokens=123, worktree="/tmp/wt", model="gpt-4")
        assert slot.cost_tokens == 123
        assert slot.worktree == "/tmp/wt"
        assert slot.model == "gpt-4"


class TestPipelineStateDerived:
    def test_Given_empty_state_When_free_slots_Then_equals_total_slots(self) -> None:
        ps = PipelineState(total_slots=30)
        assert ps.free_slots == 30
        assert ps.total_active == 0

    def test_Given_one_worker_When_free_slots_Then_decremented(self) -> None:
        ps = PipelineState(active_workers=(_slot(),), total_slots=10)
        assert ps.free_slots == 9
        assert ps.total_active == 1

    def test_Given_more_workers_than_slots_When_free_slots_Then_zero(self) -> None:
        workers = tuple(_slot(f"w{i}", ticket_id=f"T-{i}") for i in range(5))
        ps = PipelineState(active_workers=workers, total_slots=3)
        assert ps.free_slots == 0

    def test_Given_frozen_state_When_mutate_Then_raises(self) -> None:
        ps = PipelineState(ready_count=1)
        with pytest.raises((AttributeError, TypeError)):
            setattr(ps, "ready_count", 99)

    def test_Given_max_slots_alias_When_accessed_Then_equals_total_slots(self) -> None:
        ps = PipelineState(total_slots=42)
        assert ps.max_slots == 42
        assert ps.max_slots == ps.total_slots

    def test_Given_stuck_and_failed_tuples_When_counts_Then_lengths(self) -> None:
        ps = PipelineState(stuck_workers=("s1", "s2"), failed_workers=("f1",))
        assert ps.stuck_count == 2
        assert ps.failed_count == 1

    def test_Given_no_stuck_failed_When_counts_Then_zero(self) -> None:
        ps = PipelineState()
        assert ps.stuck_count == 0
        assert ps.failed_count == 0

    def test_Given_actionable_backlog_When_computed_Then_sum_ready_implementation_ready_rework(self) -> None:
        ps = PipelineState(ready_count=5, implementation_ready_count=3, rework_count=2)
        assert ps.actionable_backlog == 10

    def test_Given_actionable_backlog_zero_implementation_ready_When_computed_Then_excludes_implementing(self) -> None:
        ps = PipelineState(ready_count=2, implementation_ready_count=0, implementing_count=99, rework_count=1)
        assert ps.actionable_backlog == 3

    def test_Given_downstream_demand_When_computed_Then_sum_implementing_reviewing_verifying_rework(self) -> None:
        ps = PipelineState(implementing_count=2, reviewing_count=3, verifying_count=1, rework_count=4)
        assert ps.downstream_demand == 10

    def test_Given_downstream_excludes_implementation_ready_When_computed_Then_not_included(self) -> None:
        ps = PipelineState(implementation_ready_count=5, implementing_count=1, reviewing_count=0, verifying_count=0, rework_count=0)
        assert ps.downstream_demand == 1

    def test_Given_full_pipeline_When_total_pipeline_work_Then_sum_all_queues(self) -> None:
        ps = PipelineState(
            discovered_count=1,
            validating_count=2,
            triaged_count=1,
            ready_count=3,
            decompose_count=0,
            planning_count=1,
            implementation_ready_count=2,
            implementing_count=3,
            reviewing_count=1,
            verifying_count=1,
            rework_count=1,
            blocked_count=1,
            candidate_count=5,
        )
        assert ps.total_pipeline_work == 22

    def test_Given_total_pipeline_work_excludes_terminal_states_When_computed_Then_not_included(self) -> None:
        ps = PipelineState(
            complete_count=100,
            rejected_count=50,
            duplicate_count=30,
            deferred_count=20,
            ready_count=1,
        )
        assert ps.total_pipeline_work == 1

    def test_Given_active_by_role_mixed_When_grouped_Then_counts(self) -> None:
        w1 = _slot("w1", ticket_id="T-1", role="general_implementer")
        w2 = _slot("w2", ticket_id="T-2", role="general_implementer")
        w3 = _slot("w3", ticket_id="T-3", role="correctness_reviewer")
        ps = PipelineState(active_workers=(w1, w2, w3))
        counts = ps.active_by_role()
        assert counts["general_implementer"] == 2
        assert counts["correctness_reviewer"] == 1

    def test_Given_no_workers_When_active_by_role_Then_empty_dict(self) -> None:
        ps = PipelineState()
        assert ps.active_by_role() == {}

    def test_Given_roles_for_ticket_When_queried_Then_filtered(self) -> None:
        w1 = _slot("w1", ticket_id="T-001", role="general_implementer")
        w2 = _slot("w2", ticket_id="T-001", role="correctness_reviewer")
        w3 = _slot("w3", ticket_id="T-002", role="general_implementer")
        ps = PipelineState(active_workers=(w1, w2, w3))
        assert set(ps.active_roles_for_ticket("T-001")) == {"general_implementer", "correctness_reviewer"}
        assert ps.active_roles_for_ticket("T-999") == []

    def test_Given_unsatisfied_deps_When_check_blocked_Then_true_if_unmet(self) -> None:
        ps = PipelineState(unsatisfied_dependencies={"T-001": ("dep1", "dep2"), "T-002": ()})
        assert ps.tickets_blocked_by_deps("T-001") is True
        assert ps.tickets_blocked_by_deps("T-002") is False
        assert ps.tickets_blocked_by_deps("T-999") is False

    def test_Given_conflict_overlaps_active_When_has_conflict_Then_true(self) -> None:
        w = _slot(ticket_id="T-001")
        ps = PipelineState(active_workers=(w,), conflict_groups=(frozenset({"T-001", "T-002"}),))
        assert ps.has_conflict_with_active("T-002") is True

    def test_Given_conflict_no_overlap_When_has_conflict_Then_false(self) -> None:
        w = _slot(ticket_id="T-001")
        ps = PipelineState(active_workers=(w,), conflict_groups=(frozenset({"T-003", "T-004"}),))
        assert ps.has_conflict_with_active("T-002") is False

    def test_Given_no_active_workers_When_has_conflict_Then_false(self) -> None:
        ps = PipelineState(active_workers=(), conflict_groups=(frozenset({"T-001", "T-002"}),))
        assert ps.has_conflict_with_active("T-002") is False

    def test_Given_conflict_self_group_When_has_conflict_Then_true(self) -> None:
        w = _slot(ticket_id="T-001")
        ps = PipelineState(active_workers=(w,), conflict_groups=(frozenset({"T-001"}),))
        assert ps.has_conflict_with_active("T-001") is True

    def test_Given_no_conflict_groups_When_has_conflict_Then_false(self) -> None:
        w = _slot(ticket_id="T-001")
        ps = PipelineState(active_workers=(w,), conflict_groups=())
        assert ps.has_conflict_with_active("T-002") is False

    def test_Given_multiple_conflict_groups_When_has_conflict_Then_any_match_true(self) -> None:
        w1 = _slot("w1", ticket_id="T-1")
        w2 = _slot("w2", ticket_id="T-2")
        ps = PipelineState(
            active_workers=(w1, w2),
            conflict_groups=(frozenset({"T-1", "T-3"}), frozenset({"T-5", "T-6"})),
        )
        assert ps.has_conflict_with_active("T-3") is True
        assert ps.has_conflict_with_active("T-5") is False
        assert ps.has_conflict_with_active("T-6") is False

    def test_Given_has_security_emergency_When_called_Then_always_false(self) -> None:
        ps = PipelineState(ready_count=99, rework_count=99)
        assert ps.has_security_emergency() is False

    def test_Given_workers_by_category_known_roles_When_grouped_Then_correct_buckets(self) -> None:
        w_impl = _slot("w1", role="general_implementer")
        w_review = _slot("w2", role="correctness_reviewer")
        w_discovery = _slot("w3", role="bug_hunter")
        w_control = _slot("w4", role="scheduler")
        w_planning = _slot("w5", role="decomposer")
        ps = PipelineState(active_workers=(w_impl, w_review, w_discovery, w_control, w_planning))
        cats = ps.workers_by_category()
        assert cats["implementation"] == 1
        assert cats["review"] == 1
        assert cats["discovery"] == 1
        assert cats["control"] == 1
        assert cats["planning"] == 1

    def test_Given_unknown_role_When_workers_by_category_Then_unknown_bucket(self) -> None:
        w = _slot(role="totally_unknown_role_xyz")
        ps = PipelineState(active_workers=(w,))
        cats = ps.workers_by_category()
        assert cats["unknown"] == 1

    def test_Given_no_workers_When_workers_by_category_Then_all_zero(self) -> None:
        ps = PipelineState()
        cats = ps.workers_by_category()
        assert cats["implementation"] == 0
        assert cats["discovery"] == 0
        assert cats["unknown"] == 0


class TestMergedImplementationBucket:
    def test_Given_both_buckets_populated_When_summary_Then_merged_equals_sum(self) -> None:
        ps = PipelineState(implementation_ready_count=4, implementing_count=7)
        s = ps.summary()
        assert s["queues"]["implementation"] == 11
        assert s["queues"]["implementation_ready"] == 4
        assert s["queues"]["implementing"] == 7

    def test_Given_only_implementation_ready_When_summary_Then_merged_equals_ready(self) -> None:
        ps = PipelineState(implementation_ready_count=3, implementing_count=0)
        s = ps.summary()
        assert s["queues"]["implementation"] == 3
        assert s["queues"]["implementing"] == 0
        assert s["queues"]["implementation_ready"] == 3

    def test_Given_only_implementing_When_summary_Then_merged_equals_implementing(self) -> None:
        ps = PipelineState(implementation_ready_count=0, implementing_count=5)
        s = ps.summary()
        assert s["queues"]["implementation"] == 5
        assert s["queues"]["implementation_ready"] == 0
        assert s["queues"]["implementing"] == 5

    def test_Given_zero_both_When_summary_Then_merged_zero(self) -> None:
        ps = PipelineState(implementation_ready_count=0, implementing_count=0)
        s = ps.summary()
        assert s["queues"]["implementation"] == 0

    def test_Given_split_counts_When_summary_vs_split_fields_Then_consistent(self) -> None:
        ps = PipelineState(implementation_ready_count=10, implementing_count=15, ready_count=2, rework_count=1)
        s = ps.summary()
        assert s["queues"]["implementation"] == ps.implementation_ready_count + ps.implementing_count
        assert ps.actionable_backlog == 2 + 10 + 1
        assert ps.downstream_demand == 15 + ps.reviewing_count + ps.verifying_count + 1

    def test_Given_split_counts_When_total_pipeline_work_Then_both_buckets_counted(self) -> None:
        ps = PipelineState(implementation_ready_count=2, implementing_count=3)
        assert ps.total_pipeline_work == 5
        ps2 = PipelineState(implementation_ready_count=2, implementing_count=0)
        assert ps2.total_pipeline_work == 2
        ps3 = PipelineState(implementation_ready_count=0, implementing_count=3)
        assert ps3.total_pipeline_work == 3


class TestPipelineStateSummary:
    def test_Given_slots_and_queues_When_summary_Then_structure(self) -> None:
        ps = PipelineState(ready_count=5, total_slots=30, budget_exhausted=True, budget_warning=True, hourly_spend_usd=12.5, daily_spend_usd=99.9)
        s = ps.summary()
        assert s["slots_total"] == 30
        assert s["slots_active"] == 0
        assert s["slots_free"] == 30
        assert s["slots_stuck"] == 0
        assert s["slots_failed"] == 0
        assert s["queues"]["ready"] == 5
        assert s["queues"]["implementation"] == 0
        assert s["budget"]["exhausted"] is True
        assert s["budget"]["warning"] is True
        assert s["budget"]["hourly_usd"] == pytest.approx(12.5)
        assert s["budget"]["daily_usd"] == pytest.approx(99.9)
        assert "by_role" in s
        assert "by_category" in s

    def test_Given_workers_stuck_failed_When_summary_Then_slot_counts_correct(self) -> None:
        w = _slot()
        ps = PipelineState(active_workers=(w,), stuck_workers=("s1",), failed_workers=("f1", "f2"), total_slots=5)
        s = ps.summary()
        assert s["slots_active"] == 1
        assert s["slots_free"] == 4
        assert s["slots_stuck"] == 1
        assert s["slots_failed"] == 2

    def test_Given_all_queue_fields_When_summary_Then_all_present(self) -> None:
        ps = PipelineState(
            discovered_count=1,
            validating_count=2,
            triaged_count=3,
            ready_count=4,
            decompose_count=5,
            planning_count=6,
            implementation_ready_count=7,
            implementing_count=8,
            reviewing_count=9,
            verifying_count=10,
            rework_count=11,
            blocked_count=12,
            candidate_count=13,
            integration_queue_depth=14,
        )
        s = ps.summary()
        q = s["queues"]
        assert q["discovered"] == 1
        assert q["validating"] == 2
        assert q["triaged"] == 3
        assert q["ready"] == 4
        assert q["decompose"] == 5
        assert q["planning"] == 6
        assert q["implementation"] == 15
        assert q["implementation_ready"] == 7
        assert q["implementing"] == 8
        assert q["reviewing"] == 9
        assert q["verifying"] == 10
        assert q["rework"] == 11
        assert q["blocked"] == 12
        assert q["candidates"] == 13
        assert q["integration_queue"] == 14


class TestInspectPipelineExtensive:
    def test_Given_store_with_discovered_ticket_When_inspect_Then_discovered_count_one(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel

        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t)
        ps = inspect_pipeline(store, now=FIXED_NOW)
        assert ps.discovered_count == 1
        assert ps.snapshot_time == FIXED_NOW
        store.close()

    def test_Given_none_store_When_inspect_Then_empty_counts(self) -> None:
        ps = inspect_pipeline(None, total_slots=5, now=FIXED_NOW)
        assert ps.total_slots == 5
        assert ps.discovered_count == 0
        assert ps.ready_count == 0
        assert ps.snapshot_time == FIXED_NOW

    def test_Given_store_summary_raises_When_inspect_Then_gracefully_empty(self) -> None:
        bad = _BadSummaryStore()
        ps = inspect_pipeline(bad, now=FIXED_NOW)
        assert ps.discovered_count == 0
        assert ps.total_pipeline_work == 0

    def test_Given_active_workers_param_When_inspect_Then_reflected(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore

        store = TicketStore(tmp_path / "tickets.json")
        w = _slot()
        ps = inspect_pipeline(store, active_workers=[w], total_slots=10, now=FIXED_NOW)
        assert ps.total_active == 1
        assert ps.free_slots == 9
        assert ps.active_workers[0].worker_id == "w-1"
        store.close()

    def test_Given_stuck_and_failed_ids_When_inspect_Then_counts(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore

        store = TicketStore(tmp_path / "tickets.json")
        ps = inspect_pipeline(store, stuck_worker_ids=["w1", "w2"], failed_worker_ids=["w3"], now=FIXED_NOW)
        assert ps.stuck_count == 2
        assert ps.failed_count == 1
        assert ps.stuck_workers == ("w1", "w2")
        assert ps.failed_workers == ("w3",)
        store.close()

    def test_Given_budget_params_When_inspect_Then_snapshot_has_budget(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore

        store = TicketStore(tmp_path / "tickets.json")
        ps = inspect_pipeline(
            store,
            budget_exhausted=True,
            budget_warning=True,
            hourly_spend_usd=42.0,
            daily_spend_usd=100.5,
            now=FIXED_NOW,
        )
        assert ps.budget_exhausted is True
        assert ps.budget_warning is True
        assert ps.hourly_spend_usd == pytest.approx(42.0)
        assert ps.daily_spend_usd == pytest.approx(100.5)
        s = ps.summary()
        assert s["budget"]["exhausted"] is True
        store.close()

    def test_Given_conflict_and_integration_params_When_inspect_Then_stored(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore

        store = TicketStore(tmp_path / "tickets.json")
        groups = [frozenset({"T-1", "T-2"})]
        ps = inspect_pipeline(store, conflict_groups=groups, integration_queue_count=7, candidate_count=9, now=FIXED_NOW)
        assert ps.conflict_groups == (frozenset({"T-1", "T-2"}),)
        assert ps.integration_queue_depth == 7
        assert ps.candidate_count == 9
        assert ps.has_conflict_with_active("T-1") is False
        store.close()

    def test_Given_conflict_with_active_worker_When_inspect_Then_detectable(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore

        store = TicketStore(tmp_path / "tickets.json")
        w = _slot(ticket_id="T-1")
        groups = [frozenset({"T-1", "T-2"})]
        ps = inspect_pipeline(store, active_workers=[w], conflict_groups=groups, now=FIXED_NOW)
        assert ps.has_conflict_with_active("T-2") is True
        store.close()

    def test_Given_dependency_graph_with_unmet_deps_When_inspect_Then_unsatisfied_populated(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel

        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["ac"], risk=RiskLevel.LOW)
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t1)
        store.add(t2)

        class FakeGraph:
            def get_dependencies(self, tid: str) -> set[str]:
                return {t1.id} if tid == t2.id else set()

        ps = inspect_pipeline(store, dependency_graph=FakeGraph(), now=FIXED_NOW)
        assert ps.tickets_blocked_by_deps(t2.id) is True
        assert ps.tickets_blocked_by_deps(t1.id) is False
        store.close()

    def test_Given_dependency_graph_all_deps_completed_When_inspect_Then_not_blocked(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel, TicketState

        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["ac"], risk=RiskLevel.LOW)
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t1)
        store.add(t2)

        class FakeGraph:
            def get_dependencies(self, tid: str) -> set[str]:
                return {t1.id} if tid == t2.id else set()

        def fake_list(state: object) -> list[object]:
            if state == TicketState.COMPLETE:
                return [t1]
            if state == TicketState.DISCOVERED:
                return [t2]
            return []

        with patch.object(store, "list_by_state", side_effect=fake_list):
            ps = inspect_pipeline(store, dependency_graph=FakeGraph(), now=FIXED_NOW)
            assert ps.tickets_blocked_by_deps(t2.id) is False
        store.close()

    def test_Given_dependency_graph_none_When_inspect_Then_no_unsatisfied(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore

        store = TicketStore(tmp_path / "tickets.json")
        ps = inspect_pipeline(store, dependency_graph=None, now=FIXED_NOW)
        assert ps.unsatisfied_dependencies == {}
        store.close()

    def test_Given_dependency_graph_raises_When_inspect_Then_gracefully_empty(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel

        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t)

        class BrokenGraph:
            def get_dependencies(self, _tid: str) -> set[str]:
                raise RuntimeError("graph broken")

        ps = inspect_pipeline(store, dependency_graph=BrokenGraph(), now=FIXED_NOW)
        assert isinstance(ps.unsatisfied_dependencies, dict)
        store.close()

    def test_Given_explicit_now_When_inspect_Then_snapshot_time_matches(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore

        store = TicketStore(tmp_path / "tickets.json")
        ps = inspect_pipeline(store, now=12345.678)
        assert ps.snapshot_time == pytest.approx(12345.678)
        store.close()

    def test_Given_ticket_store_counts_include_implementing_When_inspect_Then_mapped(self) -> None:
        store = _FakeSummaryStore({"IMPLEMENTING": 5, "IMPLEMENTATION_READY": 3, "READY": 2})
        ps = inspect_pipeline(store, now=FIXED_NOW)
        assert ps.implementing_count == 5
        assert ps.implementation_ready_count == 0
        assert ps.ready_count == 2

    def test_Given_store_with_all_states_When_inspect_Then_all_counts_mapped(self) -> None:
        store = _FakeSummaryStore(
            {
                "DISCOVERED": 1,
                "VALIDATING": 2,
                "TRIAGED": 3,
                "READY": 4,
                "DECOMPOSE": 5,
                "PLANNING": 6,
                "IMPLEMENTING": 7,
                "REVIEWING": 8,
                "VERIFYING": 9,
                "REWORK": 10,
                "BLOCKED": 11,
                "COMPLETE": 12,
                "REJECTED": 13,
                "DUPLICATE": 14,
                "DEFERRED": 15,
            }
        )
        ps = inspect_pipeline(store, now=FIXED_NOW)
        assert ps.discovered_count == 1
        assert ps.validating_count == 2
        assert ps.triaged_count == 3
        assert ps.ready_count == 4
        assert ps.decompose_count == 5
        assert ps.planning_count == 6
        assert ps.implementing_count == 7
        assert ps.reviewing_count == 8
        assert ps.verifying_count == 9
        assert ps.rework_count == 10
        assert ps.blocked_count == 11
        assert ps.complete_count == 12
        assert ps.rejected_count == 13
        assert ps.duplicate_count == 14
        assert ps.deferred_count == 15


class TestPipelineInspectorExtensive:
    def test_Given_no_store_When_inspect_Then_empty_snapshot(self) -> None:
        inspector = PipelineInspector(max_slots=10)
        ps = inspector.inspect(now=FIXED_NOW)
        assert ps.total_pipeline_work == 0
        assert ps.total_slots == 10
        assert ps.snapshot_time == FIXED_NOW

    def test_Given_store_none_explicit_When_inspect_Then_slots_preserved(self) -> None:
        inspector = PipelineInspector(ticket_store=None, max_slots=25)
        ps = inspector.inspect(now=FIXED_NOW)
        assert ps.total_slots == 25

    def test_Given_leases_json_active_When_collect_active_workers_Then_returns_slots(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        now = FIXED_NOW
        leases = {
            "leases": {
                "T-001": {"owner": "worker-1", "expires_at": now + 500},
                "T-002": {"owner": "worker-2", "expires_at": now + 500},
            },
            "dead_letters": [],
        }
        (state_dir / "leases.json").write_text(json.dumps(leases), encoding="utf-8")
        inspector = PipelineInspector(state_dir=state_dir, max_slots=10)
        workers = inspector._collect_active_workers(now)
        assert len(workers) == 2
        ids = {w.ticket_id for w in workers}
        assert ids == {"T-001", "T-002"}

    def test_Given_expired_lease_When_collect_active_workers_Then_filtered(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        now = FIXED_NOW
        leases = {
            "leases": {
                "T-001": {"owner": "worker-1", "expires_at": now - 10},
                "T-002": {"owner": "worker-2", "expires_at": now + 500},
            }
        }
        (state_dir / "leases.json").write_text(json.dumps(leases), encoding="utf-8")
        inspector = PipelineInspector(state_dir=state_dir)
        workers = inspector._collect_active_workers(now)
        assert len(workers) == 1
        assert workers[0].ticket_id == "T-002"

    def test_Given_no_leases_file_When_collect_active_workers_Then_empty(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        inspector = PipelineInspector(state_dir=state_dir)
        workers = inspector._collect_active_workers(FIXED_NOW)
        assert workers == []

    def test_Given_no_state_dir_When_collect_active_workers_Then_empty(self) -> None:
        inspector = PipelineInspector(state_dir=None)
        workers = inspector._collect_active_workers(FIXED_NOW)
        assert workers == []

    def test_Given_malformed_leases_json_When_collect_active_workers_Then_empty(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "leases.json").write_text("not json {{{", encoding="utf-8")
        inspector = PipelineInspector(state_dir=state_dir)
        workers = inspector._collect_active_workers(FIXED_NOW)
        assert workers == []

    def test_Given_stale_heartbeat_When_detect_stuck_Then_flagged(self) -> None:
        now = FIXED_NOW
        w_stuck = WorkerSlot(
            worker_id="w-stuck",
            ticket_id="T-1",
            role="general_implementer",
            started_at=now - 1000,
            heartbeat_at=now - 700,
            lease_expires=now + 1000,
        )
        w_fresh = WorkerSlot(
            worker_id="w-fresh",
            ticket_id="T-2",
            role="general_implementer",
            started_at=now - 100,
            heartbeat_at=now - 10,
            lease_expires=now + 1000,
        )
        inspector = PipelineInspector()
        stuck = inspector._detect_stuck_workers([w_stuck, w_fresh], now)
        assert "w-stuck" in stuck
        assert "w-fresh" not in stuck

    def test_Given_fresh_heartbeat_When_detect_stuck_Then_not_flagged(self) -> None:
        now = FIXED_NOW
        w = WorkerSlot(
            worker_id="w-1",
            ticket_id="T-1",
            role="r",
            started_at=now - 100,
            heartbeat_at=now - 5,
            lease_expires=now + 1000,
        )
        inspector = PipelineInspector()
        stuck = inspector._detect_stuck_workers([w], now)
        assert stuck == []

    def test_Given_dead_letters_When_detect_failed_Then_returns_ids(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "leases.json").write_text(
            json.dumps({"leases": {}, "dead_letters": [{"id": "failed-1"}, {"id": "failed-2"}, {"no_id": "x"}]}),
            encoding="utf-8",
        )
        inspector = PipelineInspector(state_dir=state_dir)
        failed = inspector._detect_failed_workers()
        assert failed == ["failed-1", "failed-2"]

    def test_Given_no_state_dir_When_detect_failed_Then_empty(self) -> None:
        inspector = PipelineInspector(state_dir=None)
        assert inspector._detect_failed_workers() == []

    def test_Given_no_leases_file_When_detect_failed_Then_empty(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        inspector = PipelineInspector(state_dir=state_dir)
        assert inspector._detect_failed_workers() == []

    def test_Given_malformed_leases_When_detect_failed_Then_empty(self, tmp_path: Path) -> None:
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "leases.json").write_text("bad json", encoding="utf-8")
        inspector = PipelineInspector(state_dir=state_dir)
        assert inspector._detect_failed_workers() == []

    def test_Given_store_summary_raises_When_count_by_state_Then_empty(self) -> None:
        bad_store = _BadSummaryStore()
        inspector = PipelineInspector(ticket_store=bad_store)
        counts = inspector._count_by_state()
        assert counts == {}

    def test_Given_store_summary_succeeds_When_count_by_state_Then_returns_dict(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel

        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t)
        inspector = PipelineInspector(ticket_store=store)
        counts = inspector._count_by_state()
        assert counts.get("DISCOVERED") == 1
        store.close()

    def test_Given_detect_conflicts_When_called_Then_returns_empty(self) -> None:
        inspector = PipelineInspector()
        w = _slot(ticket_id="T-001")
        result = inspector._detect_conflicts([w])
        assert result == []

    def test_Given_inspector_with_real_store_When_inspect_Then_raises_due_to_conflicting_pairs_bug(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel

        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t)
        inspector = PipelineInspector(ticket_store=store, max_slots=10)
        with pytest.raises(TypeError, match="conflicting_pairs"):
            inspector.inspect(now=FIXED_NOW)
        store.close()

    def test_Given_inspector_with_dependency_graph_When_patched_Then_unsatisfied_via_dep_graph(self, tmp_path: Path) -> None:
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel

        store = TicketStore(tmp_path / "tickets.json")
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["ac"], risk=RiskLevel.LOW)
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["ac"], risk=RiskLevel.LOW)
        store.add(t1)
        store.add(t2)

        class FakeGraph:
            def get_dependencies(self, tid: str) -> set[str]:
                return {t1.id} if tid == t2.id else set()

        inspector = PipelineInspector(ticket_store=store, dep_graph=FakeGraph(), max_slots=10)
        orig_state = PipelineState

        def patched_state(**kwargs: object) -> PipelineState:
            kwargs.pop("conflicting_pairs", None)
            unsatisfied = kwargs.get("unsatisfied_dependencies", {})
            if not isinstance(unsatisfied, dict):
                unsatisfied = {}
            return orig_state(
                unsatisfied_dependencies=unsatisfied,
                total_slots=10,
                snapshot_time=FIXED_NOW,
            )

        with patch("codebot.pipeline_state.PipelineState", side_effect=patched_state):
            ps = inspector.inspect(now=FIXED_NOW)
            assert ps.tickets_blocked_by_deps(t2.id) is True
            assert ps.tickets_blocked_by_deps(t1.id) is False
        store.close()
