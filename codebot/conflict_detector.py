#!/usr/bin/env python3
"""Conflict detector and worktree isolation tracker for the adaptive scheduler.

Purpose
-------
Detects when two tickets would modify overlapping files or modules, preventing
the scheduler from assigning them to concurrent workers. Tracks worktree
isolation so each implementation runs in its own branch (§22).

Why
---
Spec §21 requires conflict detection before starting parallel implementation.
Two workers editing the same file simultaneously produces merge conflicts,
lost changes, or broken builds. The scheduler must serialize conflicting
work or isolate it into separate worktrees.

Invariants
----------
- stdlib-only (dataclasses)
- Pure functions: no I/O, no subprocess, no git operations
- Conflict is symmetric: if A conflicts with B, B conflicts with A
- Module-level overlap is a heuristic; exact file overlap is authoritative
- Worktree assignments are tracked but not created here
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class ConflictEdge:
    """A detected conflict between two tickets."""
    ticket_a: str
    ticket_b: str
    reason: str  # "shared_module", "shared_file", "shared_interface"
    overlap_detail: str = ""


@dataclass(frozen=True)
class ConflictMatrix:
    """Complete conflict graph for a set of tickets."""
    edges: tuple[ConflictEdge, ...] = ()

    def conflicts_with(self, ticket_id: str) -> frozenset[str]:
        """Return all ticket IDs that conflict with the given ticket."""
        result: set[str] = set()
        for edge in self.edges:
            if edge.ticket_a == ticket_id:
                result.add(edge.ticket_b)
            elif edge.ticket_b == ticket_id:
                result.add(edge.ticket_a)
        return frozenset(result)

    def has_any_conflict(self, ticket_id: str) -> bool:
        return any(
            e.ticket_a == ticket_id or e.ticket_b == ticket_id
            for e in self.edges
        )

    def conflict_groups(self) -> list[frozenset[str]]:
        """Compute connected components of the conflict graph.

        Tickets in the same group cannot run concurrently.
        Uses iterative BFS to avoid recursion limits.
        """
        adjacency: dict[str, set[str]] = {}
        for edge in self.edges:
            adjacency.setdefault(edge.ticket_a, set()).add(edge.ticket_b)
            adjacency.setdefault(edge.ticket_b, set()).add(edge.ticket_a)

        visited: set[str] = set()
        groups: list[frozenset[str]] = []

        for node in adjacency:
            if node in visited:
                continue
            component: set[str] = set()
            queue = [node]
            while queue:
                current = queue.pop(0)
                if current in visited:
                    continue
                visited.add(current)
                component.add(current)
                for neighbor in adjacency.get(current, ()):
                    if neighbor not in visited:
                        queue.append(neighbor)
            if len(component) > 1:
                groups.append(frozenset(component))

        return groups

    def summary(self) -> dict[str, Any]:
        return {
            "total_edges": len(self.edges),
            "conflict_groups": len(self.conflict_groups()),
            "tickets_involved": len({
                t for e in self.edges for t in (e.ticket_a, e.ticket_b)
            }),
        }


@dataclass
class WorktreeRegistry:
    """Tracks worktree assignments for ticket isolation (§22).

    Each implementation ticket should run in its own git branch/worktree
    to permit safe parallel implementation without file-level conflicts.
    """
    assignments: dict[str, str] = field(default_factory=dict)  # ticket_id -> worktree_path

    def assign(self, ticket_id: str, worktree_path: str) -> None:
        self.assignments[ticket_id] = worktree_path

    def release(self, ticket_id: str) -> None:
        self.assignments.pop(ticket_id, None)

    def get_worktree(self, ticket_id: str) -> str | None:
        return self.assignments.get(ticket_id)

    def active_worktrees(self) -> dict[str, str]:
        return dict(self.assignments)

    def is_isolated(self, ticket_id: str) -> bool:
        return ticket_id in self.assignments


def detect_module_conflicts(
    tickets: list[Any],
) -> list[ConflictEdge]:
    """Detect conflicts based on overlapping affected_modules (§21).

    Two tickets that both modify the same module are potentially conflicting.
    This is a heuristic — exact file-level detection is more precise but
    requires inspecting implementation plans.

    Args:
        tickets: list of Ticket instances or dicts with 'id' and 'affected_modules'

    Returns:
        List of ConflictEdge instances for overlapping pairs
    """
    edges: list[ConflictEdge] = []
    module_to_tickets: dict[str, list[str]] = {}

    for t in tickets:
        tid = getattr(t, "id", None) or (t.get("id") if isinstance(t, dict) else None)
        modules = getattr(t, "affected_modules", None) or (
            t.get("affected_modules", []) if isinstance(t, dict) else []
        )
        if not tid:
            continue
        for mod in modules:
            mod_str = str(mod).strip()
            if mod_str:
                module_to_tickets.setdefault(mod_str, []).append(tid)

    seen_pairs: set[tuple[str, str]] = set()
    for mod, tids in module_to_tickets.items():
        unique_tids = list(dict.fromkeys(tids))  # deduplicate preserving order
        for i in range(len(unique_tids)):
            for j in range(i + 1, len(unique_tids)):
                a, b = unique_tids[i], unique_tids[j]
                pair = (min(a, b), max(a, b))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    edges.append(ConflictEdge(
                        ticket_a=pair[0],
                        ticket_b=pair[1],
                        reason="shared_module",
                        overlap_detail=mod,
                    ))

    return edges


def detect_file_conflicts(
    tickets: list[Any],
    plan_store: Any = None,
) -> list[ConflictEdge]:
    """Detect conflicts based on overlapping expected files from plans.

    More precise than module-level detection. Requires ImplementationPlan
    data that lists expected_artifacts or interfaces_changed.

    Args:
        tickets: list of Ticket instances
        plan_store: optional PlanStore to look up implementation plans

    Returns:
        List of ConflictEdge instances for file-level overlaps
    """
    if plan_store is None:
        return []

    edges: list[ConflictEdge] = []
    file_to_tickets: dict[str, list[str]] = {}

    for t in tickets:
        tid = getattr(t, "id", None) or (t.get("id") if isinstance(t, dict) else None)
        if not tid:
            continue
        try:
            plan = plan_store.load(tid)
            if plan is None:
                continue
            # Extract file references from plan artifacts
            files: list[str] = []
            for artifact in getattr(plan, "expected_artifacts", []):
                if "/" in str(artifact) or str(artifact).endswith(".py"):
                    files.append(str(artifact))
            for iface in getattr(plan, "interfaces_changed", []):
                files.append(str(iface))
            for f in files:
                file_to_tickets.setdefault(f, []).append(tid)
        except Exception:
            continue

    seen_pairs: set[tuple[str, str]] = set()
    for filepath, tids in file_to_tickets.items():
        unique_tids = list(dict.fromkeys(tids))
        for i in range(len(unique_tids)):
            for j in range(i + 1, len(unique_tids)):
                a, b = unique_tids[i], unique_tids[j]
                pair = (min(a, b), max(a, b))
                if pair not in seen_pairs:
                    seen_pairs.add(pair)
                    edges.append(ConflictEdge(
                        ticket_a=pair[0],
                        ticket_b=pair[1],
                        reason="shared_file",
                        overlap_detail=filepath,
                    ))

    return edges


def build_conflict_matrix(
    tickets: list[Any],
    plan_store: Any = None,
) -> ConflictMatrix:
    """Build a complete conflict matrix from module and file analysis.

    Combines both heuristics into a single frozen matrix the scheduler
    can query efficiently.

    Args:
        tickets: list of Ticket instances or dicts
        plan_store: optional PlanStore for file-level precision

    Returns:
        Frozen ConflictMatrix
    """
    edges = detect_module_conflicts(tickets)
    file_edges = detect_file_conflicts(tickets, plan_store)

    # Deduplicate: prefer file-level edges over module-level for same pair
    seen: set[tuple[str, str]] = set()
    merged: list[ConflictEdge] = []

    for e in file_edges:
        pair = (min(e.ticket_a, e.ticket_b), max(e.ticket_a, e.ticket_b))
        if pair not in seen:
            seen.add(pair)
            merged.append(e)

    for e in edges:
        pair = (min(e.ticket_a, e.ticket_b), max(e.ticket_a, e.ticket_b))
        if pair not in seen:
            seen.add(pair)
            merged.append(e)

    return ConflictMatrix(edges=tuple(merged))


def filter_non_conflicting(
    ticket_ids: list[str],
    active_ticket_ids: set[str],
    matrix: ConflictMatrix,
) -> list[str]:
    """Filter a list of candidate tickets to only those safe to start now.

    Removes any ticket that conflicts with currently active work.

    Args:
        ticket_ids: candidates to evaluate
        active_ticket_ids: tickets currently being worked on
        matrix: pre-computed conflict matrix

    Returns:
        Filtered list of ticket IDs safe to schedule concurrently
    """
    safe: list[str] = []
    for tid in ticket_ids:
        conflicts = matrix.conflicts_with(tid)
        if not conflicts.intersection(active_ticket_ids):
            safe.append(tid)
    return safe
