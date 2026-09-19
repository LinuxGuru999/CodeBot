#!/usr/bin/env python3
"""Integration queue with dependency-aware merge ordering.

Purpose
-------
Manages a queue of merge requests and ensures they are merged in an order
that respects the ticket dependency graph. Prevents out-of-order merges
that could break builds or introduce regressions.

Why
---
ROADMAP.md §11 requires integration queue merges changes in correct order.
Without dependency-aware merge sequencing, parallel development branches
can be merged in the wrong order, causing dependent code to break when
prerequisites haven't been integrated yet.

Invariants
----------
- stdlib-only (dataclasses)
- Merge order must respect dependency graph topological ordering
- Conflicting tickets cannot be merged concurrently
- Force pushes invalidate pending merge requests
- Circular dependencies are rejected at graph level
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from codebot.dependency_graph import DependencyGraph, CyclicDependencyError
from codebot.conflict_detector import ConflictMatrix


class MergeOrderViolation(Exception):
    """Raised when attempting to merge a ticket whose dependencies are not yet merged."""
    pass


class ConflictError(Exception):
    """Raised when attempting to merge a ticket that conflicts with in-flight work."""
    pass


class InvalidatedMerge(Exception):
    """Raised when attempting to merge a ticket that has been invalidated (e.g., force push)."""
    pass


@dataclass(frozen=True)
class MergeRequest:
    """A request to merge a ticket's branch into the main branch."""
    ticket_id: str
    branch: str
    commit_sha: str
    enqueued_at: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)


class IntegrationQueue:
    """Manages merge requests with dependency-aware ordering.

    Usage:
        graph = DependencyGraph()
        graph.add_dependency("CB-2", "CB-1")
        queue = IntegrationQueue(dependency_graph=graph)
        queue.enqueue(MergeRequest("CB-1", "feat/cb-1", "sha1"))
        queue.enqueue(MergeRequest("CB-2", "feat/cb-2", "sha2"))
        order = queue.compute_merge_order()  # [CB-1, CB-2]
        queue.execute_merge("CB-1")  # succeeds
        queue.execute_merge("CB-2")  # succeeds
    """

    def __init__(
        self,
        dependency_graph: DependencyGraph | None = None,
        conflict_matrix: ConflictMatrix | None = None,
    ) -> None:
        self._graph = dependency_graph or DependencyGraph()
        self._conflict_matrix = conflict_matrix or ConflictMatrix()
        self._pending: dict[str, MergeRequest] = {}  # ticket_id -> MergeRequest
        self._merged: list[str] = []  # ordered list of merged ticket IDs
        self._in_flight: set[str] = set()  # tickets currently being merged
        self._invalidated: set[str] = set()  # tickets invalidated by force push

    @property
    def merged_tickets(self) -> list[str]:
        """Return list of ticket IDs that have been successfully merged."""
        return list(self._merged)

    @property
    def pending_count(self) -> int:
        """Return number of pending merge requests."""
        return len(self._pending)

    def enqueue(self, request: MergeRequest) -> None:
        """Add a merge request to the queue.

        If a request for the same ticket_id already exists, it is replaced
        (useful after force push re-enqueue).
        """
        self._pending[request.ticket_id] = request
        self._invalidated.discard(request.ticket_id)
        # Ensure ticket is in the dependency graph
        self._graph.add_ticket(request.ticket_id)

    def compute_merge_order(self) -> list[MergeRequest]:
        """Compute the order in which pending merges should be executed.

        Returns merge requests sorted by dependency order (topological sort).
        Independent tickets are sorted by ticket_id for determinism.
        """
        if not self._pending:
            return []

        # Get topological order from dependency graph
        all_tickets = set(self._pending.keys()) | set(self._merged)
        try:
            topo_order = self._graph.topological_sort()
        except CyclicDependencyError:
            # If cycle exists, fall back to sorted order
            topo_order = sorted(self._pending.keys())

        # Filter to only pending tickets, preserving topo order
        ordered_ids = [tid for tid in topo_order if tid in self._pending]

        # Add any pending tickets not in topo order (no deps)
        remaining = sorted(set(self._pending.keys()) - set(ordered_ids))
        ordered_ids.extend(remaining)

        return [self._pending[tid] for tid in ordered_ids]

    def execute_merge(self, ticket_id: str) -> dict[str, Any]:
        """Execute a merge for the given ticket.

        Raises:
            MergeOrderViolation: if dependencies are not yet merged
            ConflictError: if ticket conflicts with in-flight work
            InvalidatedMerge: if ticket was invalidated (e.g., force push)
            KeyError: if ticket is not in the queue
        """
        if ticket_id not in self._pending:
            raise KeyError(f"ticket {ticket_id} not in queue")

        if ticket_id in self._invalidated:
            raise InvalidatedMerge(
                f"ticket {ticket_id} was invalidated (force push?)"
            )

        # Check dependencies are satisfied
        deps = self._graph.get_dependencies(ticket_id)
        unmet = deps - set(self._merged)
        if unmet:
            raise MergeOrderViolation(
                f"cannot merge {ticket_id}: dependencies not yet merged: {unmet}"
            )

        # Check for conflicts with in-flight work
        conflicts = self._conflict_matrix.conflicts_with(ticket_id)
        in_flight_conflicts = conflicts & self._in_flight
        if in_flight_conflicts:
            raise ConflictError(
                f"cannot merge {ticket_id}: conflicts with in-flight: {in_flight_conflicts}"
            )

        # Execute the merge
        request = self._pending.pop(ticket_id)
        self._merged.append(ticket_id)
        self._in_flight.discard(ticket_id)

        return {
            "status": "merged",
            "ticket_id": ticket_id,
            "branch": request.branch,
            "commit_sha": request.commit_sha,
        }

    def mark_in_flight(self, ticket_id: str) -> None:
        """Mark a ticket as currently being merged (for conflict detection)."""
        if ticket_id in self._pending:
            self._in_flight.add(ticket_id)

    def handle_force_push(
        self,
        branch_name: str,
        old_sha: str,
        new_sha: str,
    ) -> dict[str, Any]:
        """Handle a force push that invalidates pending merge requests.

        Any pending merge request for the given branch with the old SHA
        is invalidated and must be re-enqueued.
        """
        invalidated = []
        for ticket_id, request in list(self._pending.items()):
            if request.branch == branch_name and request.commit_sha == old_sha:
                self._invalidated.add(ticket_id)
                invalidated.append(ticket_id)

        return {
            "status": "invalidated",
            "branch": branch_name,
            "old_sha": old_sha,
            "new_sha": new_sha,
            "invalidated_tickets": invalidated,
        }

    def get_pending(self, ticket_id: str) -> MergeRequest | None:
        """Get a pending merge request by ticket ID."""
        return self._pending.get(ticket_id)

    def is_merged(self, ticket_id: str) -> bool:
        """Check if a ticket has been merged."""
        return ticket_id in self._merged
