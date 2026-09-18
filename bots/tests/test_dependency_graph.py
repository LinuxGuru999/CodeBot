"""Tests for dependency_graph.py."""
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from dependency_graph import DependencyGraph, CyclicDependencyError

class TestAddAndQuery:
    def test_add_ticket(self):
        g = DependencyGraph()
        g.add_ticket("CB-1")
        assert g.get_dependencies("CB-1") == frozenset()

    def test_add_dependency(self):
        g = DependencyGraph()
        g.add_dependency("CB-2", "CB-1")
        assert g.get_dependencies("CB-2") == frozenset({"CB-1"})
        assert g.get_dependents("CB-1") == frozenset({"CB-2"})

    def test_self_dependency_raises(self):
        g = DependencyGraph()
        with pytest.raises(ValueError, match="cannot depend on itself"):
            g.add_dependency("CB-1", "CB-1")

    def test_remove_ticket(self):
        g = DependencyGraph()
        g.add_dependency("CB-2", "CB-1")
        g.remove_ticket("CB-1")
        assert g.get_dependencies("CB-2") == frozenset()

class TestCycleDetection:
    def test_direct_cycle_raises(self):
        g = DependencyGraph()
        g.add_dependency("CB-2", "CB-1")
        with pytest.raises(CyclicDependencyError):
            g.add_dependency("CB-1", "CB-2")

    def test_indirect_cycle_raises(self):
        g = DependencyGraph()
        g.add_dependency("CB-2", "CB-1")
        g.add_dependency("CB-3", "CB-2")
        with pytest.raises(CyclicDependencyError):
            g.add_dependency("CB-1", "CB-3")

    def test_cycle_rolled_back(self):
        g = DependencyGraph()
        g.add_dependency("CB-2", "CB-1")
        try:
            g.add_dependency("CB-1", "CB-2")
        except CyclicDependencyError:
            pass
        assert g.get_dependencies("CB-1") == frozenset()

class TestTopologicalSort:
    def test_linear_chain(self):
        g = DependencyGraph()
        g.add_dependency("CB-3", "CB-2")
        g.add_dependency("CB-2", "CB-1")
        order = g.topological_sort()
        assert order.index("CB-1") < order.index("CB-2") < order.index("CB-3")

    def test_empty_graph(self):
        assert DependencyGraph().topological_sort() == []

class TestReadyTickets:
    def test_all_satisfied(self):
        g = DependencyGraph()
        g.add_dependency("CB-2", "CB-1")
        ready = g.ready_tickets(completed={"CB-1"}, all_tickets={"CB-1", "CB-2"})
        assert ready == ["CB-2"]

    def test_none_satisfied(self):
        g = DependencyGraph()
        g.add_dependency("CB-2", "CB-1")
        ready = g.ready_tickets(completed=set(), all_tickets={"CB-1", "CB-2"})
        assert ready == ["CB-1"]

    def test_are_satisfied(self):
        g = DependencyGraph()
        g.add_dependency("CB-3", "CB-1")
        g.add_dependency("CB-3", "CB-2")
        assert not g.are_satisfied("CB-3", {"CB-1"})
        assert g.are_satisfied("CB-3", {"CB-1", "CB-2"})
