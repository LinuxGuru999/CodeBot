"""Tests for codebot/conflict_detector.py — parallel work safety enforcement."""

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# Ensure project root is on sys.path for direct imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import codebot.conflict_detector as cd


# ---------------------------------------------------------------------------
# ConflictEdge dataclass tests
# ---------------------------------------------------------------------------

class TestConflictEdge:
    def test_frozen_dataclass_is_immutable(self):
        edge = cd.ConflictEdge(
            ticket_a="T1",
            ticket_b="T2",
            reason="shared_module",
            overlap_detail="codebot/foo.py",
        )
        with pytest.raises(AttributeError):
            edge.ticket_a = "T3"  # type: ignore[misc]

    def test_fields_accessible(self):
        edge = cd.ConflictEdge("A", "B", "shared_file", "x.py")
        assert edge.ticket_a == "A"
        assert edge.ticket_b == "B"
        assert edge.reason == "shared_file"
        assert edge.overlap_detail == "x.py"

    def test_default_overlap_detail_is_empty(self):
        edge = cd.ConflictEdge("A", "B", "shared_interface")
        assert edge.overlap_detail == ""

    def test_equality(self):
        e1 = cd.ConflictEdge("A", "B", "shared_module")
        e2 = cd.ConflictEdge("A", "B", "shared_module")
        assert e1 == e2

    def test_hashable_for_use_in_sets(self):
        e1 = cd.ConflictEdge("A", "B", "shared_module")
        e2 = cd.ConflictEdge("A", "B", "shared_module")
        s = {e1, e2}
        assert len(s) == 1


# ---------------------------------------------------------------------------
# ConflictMatrix.conflicts_with tests
# ---------------------------------------------------------------------------

class TestConflictsWith:
    def test_returns_conflicting_tickets_from_both_sides(self):
        edges = (
            cd.ConflictEdge("A", "B", "shared_module"),
            cd.ConflictEdge("C", "A", "shared_file"),
        )
        matrix = cd.ConflictMatrix(edges=edges)
        result = matrix.conflicts_with("A")
        assert result == frozenset({"B", "C"})

    def test_symmetry_property(self):
        """If A conflicts with B, then B conflicts with A."""
        edges = (cd.ConflictEdge("A", "B", "shared_module"),)
        matrix = cd.ConflictMatrix(edges=edges)
        assert "B" in matrix.conflicts_with("A")
        assert "A" in matrix.conflicts_with("B")

    def test_no_conflicts_returns_empty_frozenset(self):
        edges = (cd.ConflictEdge("A", "B", "shared_module"),)
        matrix = cd.ConflictMatrix(edges=edges)
        assert matrix.conflicts_with("Z") == frozenset()

    def test_multi_edge_graph(self):
        edges = (
            cd.ConflictEdge("A", "B", "shared_module"),
            cd.ConflictEdge("A", "C", "shared_file"),
            cd.ConflictEdge("B", "D", "shared_interface"),
        )
        matrix = cd.ConflictMatrix(edges=edges)
        assert matrix.conflicts_with("A") == frozenset({"B", "C"})
        assert matrix.conflicts_with("B") == frozenset({"A", "D"})
        assert matrix.conflicts_with("D") == frozenset({"B"})

    def test_returns_frozenset_type(self):
        matrix = cd.ConflictMatrix(edges=())
        result = matrix.conflicts_with("X")
        assert isinstance(result, frozenset)


# ---------------------------------------------------------------------------
# ConflictMatrix.has_any_conflict tests
# ---------------------------------------------------------------------------

class TestHasAnyConflict:
    def test_returns_true_when_ticket_has_conflict(self):
        edges = (cd.ConflictEdge("A", "B", "shared_module"),)
        matrix = cd.ConflictMatrix(edges=edges)
        assert matrix.has_any_conflict("A") is True
        assert matrix.has_any_conflict("B") is True

    def test_returns_false_when_ticket_has_no_conflict(self):
        edges = (cd.ConflictEdge("A", "B", "shared_module"),)
        matrix = cd.ConflictMatrix(edges=edges)
        assert matrix.has_any_conflict("C") is False

    def test_empty_matrix_returns_false(self):
        matrix = cd.ConflictMatrix(edges=())
        assert matrix.has_any_conflict("A") is False


# ---------------------------------------------------------------------------
# Empty matrix behavior
# ---------------------------------------------------------------------------

class TestEmptyMatrix:
    def test_empty_edges_defaults_to_tuple(self):
        matrix = cd.ConflictMatrix()
        assert matrix.edges == ()

    def test_conflicts_with_on_empty(self):
        matrix = cd.ConflictMatrix()
        assert matrix.conflicts_with("anything") == frozenset()

    def test_has_any_conflict_on_empty(self):
        matrix = cd.ConflictMatrix()
        assert matrix.has_any_conflict("anything") is False

    def test_conflict_groups_on_empty(self):
        matrix = cd.ConflictMatrix()
        assert matrix.conflict_groups() == []

    def test_summary_on_empty(self):
        matrix = cd.ConflictMatrix()
        summary = matrix.summary()
        assert summary["total_edges"] == 0
        assert summary["conflict_groups"] == 0
        assert summary["tickets_involved"] == 0


# ---------------------------------------------------------------------------
# conflict_groups tests
# ---------------------------------------------------------------------------

class TestConflictGroups:
    def test_single_connected_component(self):
        edges = (
            cd.ConflictEdge("A", "B", "shared_module"),
            cd.ConflictEdge("B", "C", "shared_file"),
        )
        matrix = cd.ConflictMatrix(edges=edges)
        groups = matrix.conflict_groups()
        assert len(groups) == 1
        assert groups[0] == frozenset({"A", "B", "C"})

    def test_multiple_disconnected_components(self):
        edges = (
            cd.ConflictEdge("A", "B", "shared_module"),
            cd.ConflictEdge("C", "D", "shared_file"),
        )
        matrix = cd.ConflictMatrix(edges=edges)
        groups = matrix.conflict_groups()
        assert len(groups) == 2
        group_sets = [set(g) for g in groups]
        assert {"A", "B"} in group_sets
        assert {"C", "D"} in group_sets

    def test_isolated_nodes_not_in_groups(self):
        """Groups only contain components with >1 node."""
        edges = (cd.ConflictEdge("A", "B", "shared_module"),)
        matrix = cd.ConflictMatrix(edges=edges)
        groups = matrix.conflict_groups()
        all_nodes = set()
        for g in groups:
            all_nodes.update(g)
        # Only A and B should appear; no isolated nodes
        assert all_nodes == {"A", "B"}


# ---------------------------------------------------------------------------
# summary tests
# ---------------------------------------------------------------------------

class TestSummary:
    def test_summary_counts(self):
        edges = (
            cd.ConflictEdge("A", "B", "shared_module"),
            cd.ConflictEdge("B", "C", "shared_file"),
        )
        matrix = cd.ConflictMatrix(edges=edges)
        summary = matrix.summary()
        assert summary["total_edges"] == 2
        assert summary["tickets_involved"] == 3
        assert summary["conflict_groups"] == 1


# ---------------------------------------------------------------------------
# detect_module_conflicts tests
# ---------------------------------------------------------------------------

class TestDetectModuleConflicts:
    def test_overlapping_modules_detected(self):
        tickets = [
            {"id": "T1", "affected_modules": ["codebot/foo.py", "codebot/bar.py"]},
            {"id": "T2", "affected_modules": ["codebot/bar.py", "codebot/baz.py"]},
        ]
        edges = cd.detect_module_conflicts(tickets)
        assert len(edges) >= 1
        reasons = [e.reason for e in edges]
        assert "shared_module" in reasons

    def test_no_overlap_produces_no_edges(self):
        tickets = [
            {"id": "T1", "affected_modules": ["codebot/foo.py"]},
            {"id": "T2", "affected_modules": ["codebot/bar.py"]},
        ]
        edges = cd.detect_module_conflicts(tickets)
        assert edges == []

    def test_handles_object_tickets_with_attributes(self):
        t1 = MagicMock()
        t1.id = "T1"
        t1.affected_modules = ["mod.py"]
        t2 = MagicMock()
        t2.id = "T2"
        t2.affected_modules = ["mod.py"]
        edges = cd.detect_module_conflicts([t1, t2])
        assert len(edges) == 1
        assert edges[0].reason == "shared_module"

    def test_deduplicates_pairs(self):
        tickets = [
            {"id": "T1", "affected_modules": ["a.py", "b.py"]},
            {"id": "T2", "affected_modules": ["a.py", "b.py"]},
        ]
        edges = cd.detect_module_conflicts(tickets)
        pairs = [(e.ticket_a, e.ticket_b) for e in edges]
        # Should only have one edge between T1 and T2 despite two shared modules
        assert len(pairs) == 1

    def test_skips_tickets_without_id(self):
        tickets = [
            {"affected_modules": ["a.py"]},
            {"id": "T1", "affected_modules": ["a.py"]},
        ]
        edges = cd.detect_module_conflicts(tickets)
        assert edges == []

    def test_empty_modules_list(self):
        tickets = [
            {"id": "T1", "affected_modules": []},
            {"id": "T2", "affected_modules": []},
        ]
        edges = cd.detect_module_conflicts(tickets)
        assert edges == []

    def test_reason_values(self):
        tickets = [
            {"id": "T1", "affected_modules": ["shared.py"]},
            {"id": "T2", "affected_modules": ["shared.py"]},
        ]
        edges = cd.detect_module_conflicts(tickets)
        assert edges[0].reason == "shared_module"
        assert edges[0].overlap_detail == "shared.py"


# ---------------------------------------------------------------------------
# WorktreeRegistry tests
# ---------------------------------------------------------------------------

class TestWorktreeRegistry:
    def test_assign_and_get(self):
        reg = cd.WorktreeRegistry()
        reg.assign("T1", "/tmp/wt1")
        assert reg.get_worktree("T1") == "/tmp/wt1"

    def test_release(self):
        reg = cd.WorktreeRegistry()
        reg.assign("T1", "/tmp/wt1")
        reg.release("T1")
        assert reg.get_worktree("T1") is None

    def test_release_nonexistent_does_not_raise(self):
        reg = cd.WorktreeRegistry()
        reg.release("nonexistent")  # should not raise

    def test_is_isolated(self):
        reg = cd.WorktreeRegistry()
        assert reg.is_isolated("T1") is False
        reg.assign("T1", "/tmp/wt1")
        assert reg.is_isolated("T1") is True

    def test_active_worktrees(self):
        reg = cd.WorktreeRegistry()
        reg.assign("T1", "/tmp/wt1")
        reg.assign("T2", "/tmp/wt2")
        active = reg.active_worktrees()
        assert active == {"T1": "/tmp/wt1", "T2": "/tmp/wt2"}


# ---------------------------------------------------------------------------
# filter_non_conflicting tests
# ---------------------------------------------------------------------------

class TestFilterNonConflicting:
    def test_filters_out_conflicting_candidates(self):
        edges = (cd.ConflictEdge("T1", "T2", "shared_module"),)
        matrix = cd.ConflictMatrix(edges=edges)
        candidates = ["T2", "T3"]
        active = {"T1"}
        safe = cd.filter_non_conflicting(candidates, active, matrix)
        assert "T2" not in safe
        assert "T3" in safe

    def test_all_safe_when_no_active_conflicts(self):
        edges = (cd.ConflictEdge("T1", "T2", "shared_module"),)
        matrix = cd.ConflictMatrix(edges=edges)
        candidates = ["T3", "T4"]
        active = {"T1"}
        safe = cd.filter_non_conflicting(candidates, active, matrix)
        assert safe == ["T3", "T4"]

    def test_empty_candidates(self):
        matrix = cd.ConflictMatrix(edges=())
        assert cd.filter_non_conflicting([], {"T1"}, matrix) == []

    def test_empty_active(self):
        edges = (cd.ConflictEdge("T1", "T2", "shared_module"),)
        matrix = cd.ConflictMatrix(edges=edges)
        safe = cd.filter_non_conflicting(["T1", "T2"], set(), matrix)
        assert safe == ["T1", "T2"]


# ---------------------------------------------------------------------------
# build_conflict_matrix integration test
# ---------------------------------------------------------------------------

class TestBuildConflictMatrix:
    def test_builds_matrix_from_module_conflicts(self):
        tickets = [
            {"id": "T1", "affected_modules": ["shared.py"]},
            {"id": "T2", "affected_modules": ["shared.py"]},
        ]
        matrix = cd.build_conflict_matrix(tickets)
        assert isinstance(matrix, cd.ConflictMatrix)
        assert matrix.has_any_conflict("T1") is True
        assert matrix.has_any_conflict("T2") is True

    def test_no_plan_store_returns_module_only(self):
        tickets = [
            {"id": "T1", "affected_modules": ["a.py"]},
            {"id": "T2", "affected_modules": ["b.py"]},
        ]
        matrix = cd.build_conflict_matrix(tickets, plan_store=None)
        assert matrix.edges == ()
