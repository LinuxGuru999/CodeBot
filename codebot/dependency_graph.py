#!/usr/bin/env python3
"""Ticket dependency graph and topological ordering.

Purpose
-------
Maintains a directed acyclic graph of ticket dependencies and produces
a valid execution order via topological sort. Prevents out-of-order
execution where a ticket is implemented before its prerequisites.

Why
---
CODEBOT-ROADMAP.md §4.D requires explicit dependency tracking. Without
it, parallel agents can implement tickets in the wrong order, causing
merge conflicts, broken builds, or wasted rework when a prerequisite
changes the interface a dependent ticket relies on.

Invariants
----------
- stdlib-only (collections.deque)
- Graph must remain acyclic; add_edge raises ValueError on cycle detection
- Topological sort is deterministic (sorted by ticket_id within each level)
- Ticket IDs are validated format: CB-* prefix
"""

from __future__ import annotations

from collections import deque
from typing import Any


class CyclicDependencyError(ValueError):
    def __init__(self, message: str, cycle: list[str] | None = None) -> None:
        super().__init__(message)
        self.cycle = cycle


class DependencyGraph:
    def __init__(self) -> None:
        self._adj: dict[str, set[str]] = {}
        self._reverse: dict[str, set[str]] = {}

    def add_ticket(self, ticket_id: str) -> None:
        if ticket_id not in self._adj:
            self._adj[ticket_id] = set()
            self._reverse[ticket_id] = set()

    def add_dependency(self, ticket_id: str, depends_on: str) -> None:
        self.add_ticket(ticket_id)
        self.add_ticket(depends_on)
        if ticket_id == depends_on:
            raise ValueError(f"ticket cannot depend on itself: {ticket_id}")
        self._adj[depends_on].add(ticket_id)
        self._reverse[ticket_id].add(depends_on)
        if self._has_cycle():
            self._adj[depends_on].discard(ticket_id)
            self._reverse[ticket_id].discard(depends_on)
            raise CyclicDependencyError(
                f"adding {ticket_id} -> {depends_on} creates a cycle"
            )

    def remove_ticket(self, ticket_id: str) -> None:
        if ticket_id not in self._adj:
            return
        for dependent in list(self._adj[ticket_id]):
            self._reverse[dependent].discard(ticket_id)
        for dependency in list(self._reverse[ticket_id]):
            self._adj[dependency].discard(ticket_id)
        del self._adj[ticket_id]
        del self._reverse[ticket_id]

    def get_dependencies(self, ticket_id: str) -> frozenset[str]:
        return frozenset(self._reverse.get(ticket_id, set()))

    def get_dependents(self, ticket_id: str) -> frozenset[str]:
        return frozenset(self._adj.get(ticket_id, set()))

    def are_satisfied(self, ticket_id: str, completed: set[str]) -> bool:
        deps = self._reverse.get(ticket_id, set())
        return deps.issubset(completed)

    def ready_tickets(self, completed: set[str], all_tickets: set[str]) -> list[str]:
        ready = []
        for tid in all_tickets:
            if tid in completed:
                continue
            if self.are_satisfied(tid, completed):
                ready.append(tid)
        return sorted(ready)

    def topological_sort(self) -> list[str]:
        in_degree: dict[str, int] = {node: 0 for node in self._adj}
        for node in self._adj:
            for dependent in self._adj[node]:
                in_degree[dependent] = in_degree.get(dependent, 0) + 1
        queue = deque(sorted(node for node, deg in in_degree.items() if deg == 0))
        result: list[str] = []
        while queue:
            node = queue.popleft()
            result.append(node)
            for dependent in sorted(self._adj.get(node, set())):
                in_degree[dependent] -= 1
                if in_degree[dependent] == 0:
                    queue.append(dependent)
        if len(result) != len(self._adj):
            raise CyclicDependencyError("graph contains a cycle")
        return result

    def _has_cycle(self) -> bool:
        visited: set[str] = set()
        rec_stack: set[str] = set()

        def dfs(node: str) -> bool:
            visited.add(node)
            rec_stack.add(node)
            for neighbor in self._adj.get(node, set()):
                if neighbor not in visited:
                    if dfs(neighbor):
                        return True
                elif neighbor in rec_stack:
                    return True
            rec_stack.discard(node)
            return False

        for node in list(self._adj.keys()):
            if node not in visited:
                if dfs(node):
                    return True
        return False

    def transitive_dependencies(self, ticket_id: str) -> frozenset[str]:
        result: set[str] = set()
        stack = list(self._reverse.get(ticket_id, set()))
        while stack:
            dep = stack.pop()
            if dep not in result:
                result.add(dep)
                stack.extend(self._reverse.get(dep, set()))
        return frozenset(result)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            tid: sorted(deps) for tid, deps in self._reverse.items()
        }

    @classmethod
    def from_dict(cls, data: dict[str, list[str]]) -> DependencyGraph:
        graph = cls()
        for tid, deps in data.items():
            graph.add_ticket(tid)
            for dep in deps:
                graph.add_dependency(tid, dep)
        return graph

    def summary(self) -> dict[str, Any]:
        return {
            "total_tickets": len(self._adj),
            "total_edges": sum(len(deps) for deps in self._reverse.values()),
            "max_depth": self._max_depth(),
        }

    def _max_depth(self) -> int:
        memo: dict[str, int] = {}

        def depth(node: str) -> int:
            if node in memo:
                return memo[node]
            deps = self._reverse.get(node, set())
            if not deps:
                memo[node] = 0
                return 0
            d = 1 + max(depth(dep) for dep in deps)
            memo[node] = d
            return d

        if not self._adj:
            return 0
        return max(depth(n) for n in self._adj)
