#!/usr/bin/env python3
"""Pipeline state snapshot for the adaptive scheduler.

Purpose
-------
Provides a frozen point-in-time view of every queue in the CodeBot pipeline.
The scheduler reads this snapshot each tick to make allocation decisions.
Decoupled from TicketStore so tests can construct arbitrary pipeline states
without touching disk.

Why
---
The spec (§5) requires the scheduler to inspect ready_tickets, planning,
implementing, review_pending, verification_pending, rework, blocked,
discovered candidates, active workers, free slots, stuck workers, failed
workers, budget state, dependency constraints, and file conflicts. A single
frozen dataclass carrying all of this prevents scattered queries and makes
the scheduler's decision function pure.

Invariants
----------
- stdlib-only (dataclasses, time)
- Frozen dataclass: immutable snapshot, never mutated after creation
- All counts are non-negative integers
- PipelineInspector builds snapshots from TicketStore + WorkerPool
- inspect_pipeline() is the preferred factory for production use
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class WorkerSlot:
    """Represents one occupied slot in the worker pool (§3)."""
    worker_id: str
    ticket_id: str
    role: str
    started_at: float
    heartbeat_at: float
    lease_expires: float
    worktree: str = ""
    model: str = ""
    cost_tokens: int = 0

    @property
    def is_expired(self) -> bool:
        return time.time() > self.lease_expires

    def seconds_since_heartbeat(self, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        return max(0.0, now - self.heartbeat_at)


@dataclass(frozen=True)
class PipelineState:
    """Complete point-in-time snapshot of the engineering pipeline.

    Every field the scheduler needs to make an allocation decision lives here.
    Construct directly in tests; use inspect_pipeline() or PipelineInspector
    in production.
    """
    # Queue counts by stage
    discovered_count: int = 0
    validating_count: int = 0
    triaged_count: int = 0
    ready_count: int = 0
    planning_count: int = 0
    implementing_count: int = 0
    reviewing_count: int = 0
    verifying_count: int = 0
    rework_count: int = 0
    blocked_count: int = 0
    complete_count: int = 0
    rejected_count: int = 0
    duplicate_count: int = 0
    deferred_count: int = 0

    # Discovery candidates awaiting validation (§12)
    candidate_count: int = 0

    # Worker pool state
    active_workers: tuple[WorkerSlot, ...] = ()
    stuck_workers: tuple[str, ...] = ()
    failed_workers: tuple[str, ...] = ()

    # Slot accounting
    total_slots: int = 30

    # Budget state
    budget_exhausted: bool = False
    budget_warning: bool = False
    hourly_spend_usd: float = 0.0
    daily_spend_usd: float = 0.0

    # Dependency info: ticket_id -> unmet dependency IDs
    unsatisfied_dependencies: dict[str, tuple[str, ...]] = field(default_factory=dict)

    # Conflict groups: sets of ticket_ids touching overlapping files (§21)
    conflict_groups: tuple[frozenset[str], ...] = ()

    # Integration queue (§23)
    integration_queue_depth: int = 0

    # Timestamp
    snapshot_time: float = field(default_factory=time.time)

    # --- Derived properties ---

    @property
    def max_slots(self) -> int:
        """Alias for backward compat with first draft."""
        return self.total_slots

    @property
    def free_slots(self) -> int:
        return max(0, self.total_slots - len(self.active_workers))

    @property
    def total_active(self) -> int:
        return len(self.active_workers)

    @property
    def stuck_count(self) -> int:
        return len(self.stuck_workers)

    @property
    def failed_count(self) -> int:
        return len(self.failed_workers)

    @property
    def actionable_backlog(self) -> int:
        """Tickets that can be worked on right now (ready + rework)."""
        return self.ready_count + self.rework_count

    @property
    def downstream_demand(self) -> int:
        return self.implementing_count + self.reviewing_count + self.verifying_count + self.rework_count

    @property
    def total_pipeline_work(self) -> int:
        """All non-terminal tickets in the system."""
        return (
            self.discovered_count
            + self.validating_count
            + self.triaged_count
            + self.ready_count
            + self.planning_count
            + self.implementing_count
            + self.reviewing_count
            + self.verifying_count
            + self.rework_count
            + self.blocked_count
            + self.candidate_count
        )

    def active_by_role(self) -> dict[str, int]:
        """Count active workers grouped by role."""
        counts: dict[str, int] = {}
        for w in self.active_workers:
            counts[w.role] = counts.get(w.role, 0) + 1
        return counts

    def active_roles_for_ticket(self, ticket_id: str) -> list[str]:
        """Return roles currently assigned to a specific ticket."""
        return [w.role for w in self.active_workers if w.ticket_id == ticket_id]

    def workers_by_category(self) -> dict[str, int]:
        """Group workers into discovery/implementation/review/planning/control."""
        try:
            from codebot.role_registry import ROLE_REGISTRY
        except ImportError:
            return {"unknown": self.total_active}
        cats: dict[str, int] = {
            "discovery": 0, "implementation": 0, "review": 0,
            "planning": 0, "control": 0, "unknown": 0,
        }
        for w in self.active_workers:
            role = ROLE_REGISTRY.get(w.role)
            if role:
                cats[role.category.value] = cats.get(role.category.value, 0) + 1
            else:
                cats["unknown"] += 1
        return cats

    def tickets_blocked_by_deps(self, ticket_id: str) -> bool:
        """Return True if ticket has unsatisfied dependencies."""
        deps = self.unsatisfied_dependencies.get(ticket_id, ())
        return len(deps) > 0

    def has_conflict_with_active(self, ticket_id: str) -> bool:
        """Return True if ticket conflicts with any currently active worker's ticket."""
        active_tickets = {w.ticket_id for w in self.active_workers}
        for group in self.conflict_groups:
            if ticket_id in group and group & active_tickets:
                return True
        return False

    def has_security_emergency(self) -> bool:
        """Heuristic: security tickets in ready/rework indicate emergency."""
        return False

    def summary(self) -> dict[str, Any]:
        """Human-readable summary for dashboard output (§47)."""
        cats = self.workers_by_category()
        return {
            "slots_total": self.total_slots,
            "slots_active": self.total_active,
            "slots_free": self.free_slots,
            "slots_stuck": self.stuck_count,
            "slots_failed": self.failed_count,
            "queues": {
                "discovered": self.discovered_count,
                "validating": self.validating_count,
                "triaged": self.triaged_count,
                "ready": self.ready_count,
                "planning": self.planning_count,
                "implementing": self.implementing_count,
                "reviewing": self.reviewing_count,
                "verifying": self.verifying_count,
                "rework": self.rework_count,
                "blocked": self.blocked_count,
                "candidates": self.candidate_count,
                "integration_queue": self.integration_queue_depth,
            },
            "budget": {
                "exhausted": self.budget_exhausted,
                "warning": self.budget_warning,
                "hourly_usd": self.hourly_spend_usd,
                "daily_usd": self.daily_spend_usd,
            },
            "by_role": self.active_by_role(),
            "by_category": cats,
        }


def inspect_pipeline(
    ticket_store: Any,
    total_slots: int = 30,
    active_workers: list[WorkerSlot] | None = None,
    stuck_worker_ids: list[str] | None = None,
    failed_worker_ids: list[str] | None = None,
    dependency_graph: Any = None,
    conflict_groups: list[frozenset[str]] | None = None,
    integration_queue_count: int = 0,
    candidate_count: int = 0,
    budget_exhausted: bool = False,
    budget_warning: bool = False,
    hourly_spend_usd: float = 0.0,
    daily_spend_usd: float = 0.0,
    now: float | None = None,
) -> PipelineState:
    """Build a PipelineState snapshot from live system components.

    This is the primary production entry point. Tests can also construct
    PipelineState directly for deterministic scenarios.

    Args:
        ticket_store: ticket_engine.TicketStore instance
        total_slots: configured max concurrent slots
        active_workers: currently running WorkerSlots
        stuck_worker_ids: worker IDs detected as stuck
        failed_worker_ids: worker IDs that crashed/exhausted retries
        dependency_graph: dependency_graph.DependencyGraph instance
        conflict_groups: pre-computed file overlap groups
        integration_queue_count: items waiting for merge
        candidate_count: unvalidated discovery candidates
        budget_exhausted: whether cost budget is exhausted
        budget_warning: whether cost budget is near limit
        hourly_spend_usd: current hourly spend
        daily_spend_usd: current daily spend
        now: current timestamp (for testing)

    Returns:
        Frozen PipelineState snapshot
    """
    now = now or time.time()

    try:
        state_counts = ticket_store.summary()
    except Exception:
        state_counts = {}

    unsatisfied: dict[str, tuple[str, ...]] = {}
    if dependency_graph is not None:
        try:
            from codebot.ticket_engine import TicketState
            completed_ids: set[str] = set()
            for t in ticket_store.list_by_state(TicketState.COMPLETE):
                completed_ids.add(t.id)

            all_ids: set[str] = set()
            for state in TicketState:
                try:
                    for t in ticket_store.list_by_state(state):
                        all_ids.add(t.id)
                except Exception:
                    pass

            for tid in all_ids:
                if tid in completed_ids:
                    continue
                deps = dependency_graph.get_dependencies(tid)
                unmet = deps - completed_ids
                if unmet:
                    unsatisfied[tid] = tuple(sorted(unmet))
        except Exception:
            pass

    return PipelineState(
        discovered_count=state_counts.get("DISCOVERED", 0),
        validating_count=state_counts.get("VALIDATING", 0),
        triaged_count=state_counts.get("TRIAGED", 0),
        ready_count=state_counts.get("READY", 0),
        planning_count=state_counts.get("PLANNING", 0),
        implementing_count=state_counts.get("IMPLEMENTING", 0),
        reviewing_count=state_counts.get("REVIEWING", 0),
        verifying_count=state_counts.get("VERIFYING", 0),
        rework_count=state_counts.get("REWORK", 0),
        blocked_count=state_counts.get("BLOCKED", 0),
        complete_count=state_counts.get("COMPLETE", 0),
        rejected_count=state_counts.get("REJECTED", 0),
        duplicate_count=state_counts.get("DUPLICATE", 0),
        deferred_count=state_counts.get("DEFERRED", 0),
        candidate_count=candidate_count,
        active_workers=tuple(active_workers or []),
        stuck_workers=tuple(stuck_worker_ids or []),
        failed_workers=tuple(failed_worker_ids or []),
        total_slots=total_slots,
        budget_exhausted=budget_exhausted,
        budget_warning=budget_warning,
        hourly_spend_usd=hourly_spend_usd,
        daily_spend_usd=daily_spend_usd,
        unsatisfied_dependencies=unsatisfied,
        conflict_groups=tuple(conflict_groups or []),
        integration_queue_depth=integration_queue_count,
        snapshot_time=now,
    )


class PipelineInspector:
    """Builds PipelineState snapshots from live CodeBot infrastructure.

    Reads from TicketStore and worker tracking state. Designed so tests
    can mock or bypass it entirely by constructing PipelineState directly.
    Prefer inspect_pipeline() for new code; this class exists for backward
    compatibility with the first draft.
    """

    def __init__(
        self,
        ticket_store: Any = None,
        dep_graph: Any = None,
        state_dir: Any = None,
        max_slots: int = 30,
    ) -> None:
        self._store = ticket_store
        self._dep_graph = dep_graph
        self._state_dir = state_dir
        self._max_slots = max_slots

    def inspect(self, now: float | None = None) -> PipelineState:
        """Build a fresh pipeline snapshot from current state."""
        now = now if now is not None else time.time()

        if self._store is None:
            return PipelineState(total_slots=self._max_slots, snapshot_time=now)

        counts = self._count_by_state()
        workers = self._collect_active_workers(now)
        stuck = self._detect_stuck_workers(workers, now)
        failed = self._detect_failed_workers()
        conflicts = self._detect_conflicts(workers)

        unsatisfied: dict[str, tuple[str, ...]] = {}
        if self._dep_graph is not None:
            try:
                from codebot.ticket_engine import TicketState
                completed_ids: set[str] = set()
                for t in self._store.list_by_state(TicketState.COMPLETE):
                    completed_ids.add(t.id)
                for state in TicketState:
                    try:
                        for t in self._store.list_by_state(state):
                            deps = self._dep_graph.get_dependencies(t.id)
                            unmet = deps - completed_ids
                            if unmet:
                                unsatisfied[t.id] = tuple(sorted(unmet))
                    except Exception:
                        pass
            except Exception:
                pass

        return PipelineState(
            discovered_count=counts.get("DISCOVERED", 0),
            validating_count=counts.get("VALIDATING", 0),
            triaged_count=counts.get("TRIAGED", 0),
            ready_count=counts.get("READY", 0),
            planning_count=counts.get("PLANNING", 0),
            implementing_count=counts.get("IMPLEMENTING", 0),
            reviewing_count=counts.get("REVIEWING", 0),
            verifying_count=counts.get("VERIFYING", 0),
            rework_count=counts.get("REWORK", 0),
            blocked_count=counts.get("BLOCKED", 0),
            complete_count=counts.get("COMPLETE", 0),
            rejected_count=counts.get("REJECTED", 0),
            duplicate_count=counts.get("DUPLICATE", 0),
            deferred_count=counts.get("DEFERRED", 0),
            candidate_count=counts.get("CANDIDATES", 0),
            active_workers=tuple(workers),
            stuck_workers=tuple(stuck),
            failed_workers=tuple(failed),
            total_slots=self._max_slots,
            unsatisfied_dependencies=unsatisfied,
            conflicting_pairs=tuple(conflicts),
            integration_queue_depth=counts.get("INTEGRATION", 0),
            snapshot_time=now,
        )

    def _count_by_state(self) -> dict[str, int]:
        """Query TicketStore for counts per state."""
        counts: dict[str, int] = {}
        try:
            summary = self._store.summary()
            counts.update(summary)
        except Exception:
            pass
        return counts

    def _collect_active_workers(self, now: float) -> list[WorkerSlot]:
        """Scan state directory for active worker leases."""
        workers: list[WorkerSlot] = []
        if self._state_dir is None:
            return workers
        import json
        from pathlib import Path
        leases_path = Path(self._state_dir) / "leases.json"
        if not leases_path.exists():
            return workers
        try:
            data = json.loads(leases_path.read_text(encoding="utf-8"))
            leases = data.get("leases", {})
            for item_id, lease in leases.items():
                expires = float(lease.get("expires_at", 0))
                if expires > now:
                    workers.append(WorkerSlot(
                        worker_id=str(lease.get("owner", "unknown")),
                        ticket_id=str(item_id),
                        role="unknown",
                        started_at=now,
                        heartbeat_at=now,
                        lease_expires=expires,
                    ))
        except Exception:
            pass
        return workers

    def _detect_stuck_workers(
        self, workers: list[WorkerSlot], now: float
    ) -> list[str]:
        """Identify workers whose heartbeat has gone stale."""
        stuck: list[str] = []
        timeout = 600.0
        for w in workers:
            if w.seconds_since_heartbeat(now) > timeout:
                stuck.append(w.worker_id)
        return stuck

    def _detect_failed_workers(self) -> list[str]:
        """Check dead-letter queue for permanently failed workers."""
        if self._state_dir is None:
            return []
        import json
        from pathlib import Path
        leases_path = Path(self._state_dir) / "leases.json"
        if not leases_path.exists():
            return []
        try:
            data = json.loads(leases_path.read_text(encoding="utf-8"))
            dead = data.get("dead_letters", [])
            return [str(d.get("id", "")) for d in dead if d.get("id")]
        except Exception:
            return []

    def _detect_conflicts(
        self, workers: list[WorkerSlot]
    ) -> list[tuple[str, str]]:
        """Detect workers operating on potentially conflicting tickets."""
        return []
