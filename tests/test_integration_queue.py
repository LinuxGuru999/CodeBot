"""Integration tests for integration queue merge ordering and stale branch detection.

Ticket: CB-1273982-939C
Purpose: Verify safe parallel development through:
  1. Merge ordering that respects dependency graph
  2. Stale branch detection (>7 days old or abandoned tickets)
  3. Automatic cleanup after notification period
  4. Conflict prevention under concurrent worker simulation
  5. Edge cases: circular deps, force pushes
"""
import pytest
import time
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.dependency_graph import DependencyGraph, CyclicDependencyError
from codebot.conflict_detector import (
    ConflictMatrix, ConflictEdge, WorktreeRegistry,
    build_conflict_matrix, filter_non_conflicting,
)
from codebot.integration_queue import (
    IntegrationQueue, MergeRequest, MergeOrderViolation,
)
from codebot.stale_branch_detector import (
    BranchRecord, StaleBranchDetector, CleanupAction,
)


# ---------------------------------------------------------------------------
# Acceptance Criterion 1: Merge ordering respects dependencies
# ---------------------------------------------------------------------------
class TestMergeOrderingRespectsDependencies:
    """Integration test: merge ordering must respect dependency graph."""

    def test_linear_dependency_chain_merges_in_order(self):
        """A -> B -> C must merge A first, then B, then C."""
        graph = DependencyGraph()
        graph.add_dependency("CB-2", "CB-1")
        graph.add_dependency("CB-3", "CB-2")

        queue = IntegrationQueue(dependency_graph=graph)
        queue.enqueue(MergeRequest(ticket_id="CB-3", branch="feat/cb-3", commit_sha="aaa"))
        queue.enqueue(MergeRequest(ticket_id="CB-1", branch="feat/cb-1", commit_sha="bbb"))
        queue.enqueue(MergeRequest(ticket_id="CB-2", branch="feat/cb-2", commit_sha="ccc"))

        order = queue.compute_merge_order()
        ids = [mr.ticket_id for mr in order]
        assert ids.index("CB-1") < ids.index("CB-2") < ids.index("CB-3")

    def test_independent_tickets_merge_in_deterministic_order(self):
        """Independent tickets should merge in a deterministic (sorted) order."""
        graph = DependencyGraph()
        queue = IntegrationQueue(dependency_graph=graph)
        queue.enqueue(MergeRequest(ticket_id="CB-5", branch="feat/cb-5", commit_sha="a"))
        queue.enqueue(MergeRequest(ticket_id="CB-2", branch="feat/cb-2", commit_sha="b"))
        queue.enqueue(MergeRequest(ticket_id="CB-9", branch="feat/cb-9", commit_sha="c"))

        order = queue.compute_merge_order()
        ids = [mr.ticket_id for mr in order]
        assert ids == sorted(ids)

    def test_diamond_dependency_merges_correctly(self):
        """Diamond: D depends on B and C, both depend on A. A first, D last."""
        graph = DependencyGraph()
        graph.add_dependency("CB-B", "CB-A")
        graph.add_dependency("CB-C", "CB-A")
        graph.add_dependency("CB-D", "CB-B")
        graph.add_dependency("CB-D", "CB-C")

        queue = IntegrationQueue(dependency_graph=graph)
        for tid in ["CB-D", "CB-C", "CB-B", "CB-A"]:
            queue.enqueue(MergeRequest(ticket_id=tid, branch=f"feat/{tid}", commit_sha=tid))

        order = queue.compute_merge_order()
        ids = [mr.ticket_id for mr in order]
        assert ids.index("CB-A") < ids.index("CB-B")
        assert ids.index("CB-A") < ids.index("CB-C")
        assert ids.index("CB-B") < ids.index("CB-D")
        assert ids.index("CB-C") < ids.index("CB-D")

    def test_merge_out_of_order_raises_violation(self):
        """Attempting to merge a ticket whose deps are not yet merged raises."""
        graph = DependencyGraph()
        graph.add_dependency("CB-2", "CB-1")

        queue = IntegrationQueue(dependency_graph=graph)
        queue.enqueue(MergeRequest(ticket_id="CB-1", branch="feat/cb-1", commit_sha="a"))
        queue.enqueue(MergeRequest(ticket_id="CB-2", branch="feat/cb-2", commit_sha="b"))

        with pytest.raises(MergeOrderViolation):
            queue.execute_merge("CB-2")  # CB-1 not yet merged

    def test_merge_in_correct_order_succeeds(self):
        """Merging in dependency order should succeed and track merged state."""
        graph = DependencyGraph()
        graph.add_dependency("CB-2", "CB-1")

        queue = IntegrationQueue(dependency_graph=graph)
        queue.enqueue(MergeRequest(ticket_id="CB-1", branch="feat/cb-1", commit_sha="a"))
        queue.enqueue(MergeRequest(ticket_id="CB-2", branch="feat/cb-2", commit_sha="b"))

        result1 = queue.execute_merge("CB-1")
        assert result1["status"] == "merged"
        result2 = queue.execute_merge("CB-2")
        assert result2["status"] == "merged"
        assert queue.merged_tickets == ["CB-1", "CB-2"]


# ---------------------------------------------------------------------------
# Acceptance Criterion 2: Stale branch detection
# ---------------------------------------------------------------------------
class TestStaleBranchDetection:
    """Stale branch detector identifies branches >7 days old or with no recent activity."""

    def test_branch_older_than_threshold_is_stale(self):
        """A branch older than 7 days must be flagged as stale."""
        now = 1_700_000_000.0
        seven_days = 7 * 24 * 3600
        detector = StaleBranchDetector(stale_threshold_seconds=seven_days)
        detector.register(BranchRecord(
            branch_name="feat/old-branch",
            ticket_id="CB-100",
            last_activity_ts=now - seven_days - 100,
            last_commit_sha="abc123",
        ))
        stale = detector.detect_stale(now=now)
        assert len(stale) == 1
        assert stale[0].branch_name == "feat/old-branch"

    def test_recent_branch_is_not_stale(self):
        """A branch with recent activity should not be flagged."""
        now = 1_700_000_000.0
        seven_days = 7 * 24 * 3600
        detector = StaleBranchDetector(stale_threshold_seconds=seven_days)
        detector.register(BranchRecord(
            branch_name="feat/fresh-branch",
            ticket_id="CB-101",
            last_activity_ts=now - 3600,  # 1 hour ago
            last_commit_sha="def456",
        ))
        stale = detector.detect_stale(now=now)
        assert len(stale) == 0

    def test_branch_with_abandoned_ticket_is_stale(self):
        """Branch linked to a closed/abandoned ticket should be flagged."""
        now = 1_700_000_000.0
        seven_days = 7 * 24 * 3600
        detector = StaleBranchDetector(stale_threshold_seconds=seven_days)
        detector.register(BranchRecord(
            branch_name="feat/abandoned",
            ticket_id="CB-999",
            last_activity_ts=now - 3600,  # recent activity but ticket abandoned
            last_commit_sha="ghi789",
        ))
        # Mark ticket as abandoned
        stale = detector.detect_stale(now=now, abandoned_tickets={"CB-999"})
        assert len(stale) == 1
        assert stale[0].branch_name == "feat/abandoned"

    def test_exactly_at_threshold_not_stale(self):
        """A branch exactly at the threshold boundary is not yet stale."""
        now = 1_700_000_000.0
        seven_days = 7 * 24 * 3600
        detector = StaleBranchDetector(stale_threshold_seconds=seven_days)
        detector.register(BranchRecord(
            branch_name="feat/boundary",
            ticket_id="CB-102",
            last_activity_ts=now - seven_days,  # exactly at threshold
            last_commit_sha="jkl012",
        ))
        stale = detector.detect_stale(now=now)
        assert len(stale) == 0

    def test_multiple_stale_branches_detected(self):
        """Multiple stale branches should all be detected."""
        now = 1_700_000_000.0
        seven_days = 7 * 24 * 3600
        detector = StaleBranchDetector(stale_threshold_seconds=seven_days)
        for i in range(5):
            detector.register(BranchRecord(
                branch_name=f"feat/stale-{i}",
                ticket_id=f"CB-{200 + i}",
                last_activity_ts=now - seven_days - (i * 3600),
                last_commit_sha=f"sha{i}",
            ))
        stale = detector.detect_stale(now=now)
        assert len(stale) == 5


# ---------------------------------------------------------------------------
# Acceptance Criterion 3: Cleanup mechanism removes stale branches after warning
# ---------------------------------------------------------------------------
class TestStaleBranchCleanup:
    """Cleanup mechanism removes stale branches after warning period."""

    def test_warning_issued_before_cleanup(self):
        """First detection issues a warning; branch is NOT yet removed."""
        now = 1_700_000_000.0
        seven_days = 7 * 24 * 3600
        warning_period = 24 * 3600  # 24-hour warning window
        detector = StaleBranchDetector(
            stale_threshold_seconds=seven_days,
            warning_period_seconds=warning_period,
        )
        detector.register(BranchRecord(
            branch_name="feat/warn-first",
            ticket_id="CB-300",
            last_activity_ts=now - seven_days - 100,
            last_commit_sha="sha-warn",
        ))

        action = detector.schedule_cleanup(branch_name="feat/warn-first", now=now)
        assert action.action == "warn"
        assert action.branch_name == "feat/warn-first"

    def test_cleanup_after_warning_period_expires(self):
        """After warning period expires, cleanup action becomes 'delete'."""
        now = 1_700_000_000.0
        seven_days = 7 * 24 * 3600
        warning_period = 24 * 3600
        detector = StaleBranchDetector(
            stale_threshold_seconds=seven_days,
            warning_period_seconds=warning_period,
        )
        detector.register(BranchRecord(
            branch_name="feat/delete-me",
            ticket_id="CB-301",
            last_activity_ts=now - seven_days - 100,
            last_commit_sha="sha-del",
        ))

        # First: warn
        action1 = detector.schedule_cleanup(branch_name="feat/delete-me", now=now)
        assert action1.action == "warn"

        # After warning period expires: delete
        action2 = detector.schedule_cleanup(
            branch_name="feat/delete-me",
            now=now + warning_period + 1,
        )
        assert action2.action == "delete"

    def test_cleanup_executes_and_removes_branch(self):
        """Executing cleanup actually removes the branch from the registry."""
        now = 1_700_000_000.0
        seven_days = 7 * 24 * 3600
        warning_period = 24 * 3600
        detector = StaleBranchDetector(
            stale_threshold_seconds=seven_days,
            warning_period_seconds=warning_period,
        )
        detector.register(BranchRecord(
            branch_name="feat/gone",
            ticket_id="CB-302",
            last_activity_ts=now - seven_days - 100,
            last_commit_sha="sha-gone",
        ))
        # Warn
        detector.schedule_cleanup(branch_name="feat/gone", now=now)
        # Delete after warning period
        result = detector.execute_cleanup(
            branch_name="feat/gone",
            now=now + warning_period + 1,
        )
        assert result["status"] == "deleted"
        assert detector.get_branch("feat/gone") is None

    def test_refreshed_branch_cancels_cleanup(self):
        """If activity resumes on a warned branch, cleanup is cancelled."""
        now = 1_700_000_000.0
        seven_days = 7 * 24 * 3600
        warning_period = 24 * 3600
        detector = StaleBranchDetector(
            stale_threshold_seconds=seven_days,
            warning_period_seconds=warning_period,
        )
        detector.register(BranchRecord(
            branch_name="feat/revived",
            ticket_id="CB-303",
            last_activity_ts=now - seven_days - 100,
            last_commit_sha="sha-old",
        ))
        # Warn
        detector.schedule_cleanup(branch_name="feat/revived", now=now)
        # Activity resumes (new commit)
        detector.update_activity(
            branch_name="feat/revived",
            new_ts=now + 1000,
            new_sha="sha-new",
        )
        # Cleanup should now be cancelled
        action = detector.schedule_cleanup(branch_name="feat/revived", now=now + 2000)
        assert action.action == "none"


# ---------------------------------------------------------------------------
# Acceptance Criterion 4: Conflict prevention under concurrent workers
# ---------------------------------------------------------------------------
class TestConflictPreventionConcurrent:
    """Conflict prevention verified under concurrent worker simulation."""

    def test_concurrent_workers_blocked_on_shared_module(self):
        """Two workers cannot operate on tickets that share a module."""
        from dataclasses import dataclass

        @dataclass
        class FakeTicket:
            id: str
            affected_modules: list

        t1 = FakeTicket(id="CB-400", affected_modules=["codebot/orchestrator.py"])
        t2 = FakeTicket(id="CB-401", affected_modules=["codebot/orchestrator.py"])

        matrix = build_conflict_matrix([t1, t2])
        assert matrix.has_any_conflict("CB-400")
        assert matrix.has_any_conflict("CB-401")

        # Simulate: CB-400 is active, CB-401 should be filtered out
        safe = filter_non_conflicting(
            ticket_ids=["CB-401"],
            active_ticket_ids={"CB-400"},
            matrix=matrix,
        )
        assert safe == []

    def test_non_conflicting_tickets_can_run_concurrently(self):
        """Tickets modifying different modules can run in parallel."""
        from dataclasses import dataclass

        @dataclass
        class FakeTicket:
            id: str
            affected_modules: list

        t1 = FakeTicket(id="CB-410", affected_modules=["codebot/orchestrator.py"])
        t2 = FakeTicket(id="CB-411", affected_modules=["codebot/dependency_graph.py"])

        matrix = build_conflict_matrix([t1, t2])
        safe = filter_non_conflicting(
            ticket_ids=["CB-411"],
            active_ticket_ids={"CB-410"},
            matrix=matrix,
        )
        assert safe == ["CB-411"]

    def test_worktree_isolation_prevents_file_conflicts(self):
        """Each concurrent worker should get an isolated worktree."""
        registry = WorktreeRegistry()
        registry.assign("CB-420", "/worktrees/cb-420")
        registry.assign("CB-421", "/worktrees/cb-421")

        assert registry.is_isolated("CB-420")
        assert registry.is_isolated("CB-421")
        assert registry.get_worktree("CB-420") != registry.get_worktree("CB-421")

    def test_integration_queue_blocks_conflicting_merges(self):
        """Integration queue should not allow merging conflicting branches concurrently."""
        from dataclasses import dataclass

        @dataclass
        class FakeTicket:
            id: str
            affected_modules: list

        graph = DependencyGraph()
        t1 = FakeTicket(id="CB-430", affected_modules=["codebot/shared.py"])
        t2 = FakeTicket(id="CB-431", affected_modules=["codebot/shared.py"])
        matrix = build_conflict_matrix([t1, t2])

        queue = IntegrationQueue(dependency_graph=graph, conflict_matrix=matrix)
        queue.enqueue(MergeRequest(ticket_id="CB-430", branch="feat/cb-430", commit_sha="a"))
        queue.enqueue(MergeRequest(ticket_id="CB-431", branch="feat/cb-431", commit_sha="b"))

        # Start merging CB-430
        queue.execute_merge("CB-430")
        # Attempting to merge conflicting CB-431 should fail while CB-430 is "in-flight"
        # Since CB-430 is already merged, CB-431 can proceed.
        # But if we mark CB-430 as in-flight (not yet fully committed), CB-431 must wait.
        queue2 = IntegrationQueue(dependency_graph=graph, conflict_matrix=matrix)
        queue2.enqueue(MergeRequest(ticket_id="CB-430", branch="feat/cb-430", commit_sha="a"))
        queue2.enqueue(MergeRequest(ticket_id="CB-431", branch="feat/cb-431", commit_sha="b"))
        queue2.mark_in_flight("CB-430")

        with pytest.raises(Exception):  # ConflictError or similar
            queue2.execute_merge("CB-431")


# ---------------------------------------------------------------------------
# Acceptance Criterion 5: Edge cases (circular deps, force pushes)
# ---------------------------------------------------------------------------
class TestEdgeCases:
    """Tests cover edge cases: circular deps, force pushes."""

    def test_circular_dependency_raises_on_add(self):
        """Circular dependencies must be rejected by the dependency graph."""
        graph = DependencyGraph()
        graph.add_dependency("CB-B", "CB-A")
        graph.add_dependency("CB-C", "CB-B")
        with pytest.raises(CyclicDependencyError):
            graph.add_dependency("CB-A", "CB-C")

    def test_queue_rejects_ticket_with_circular_deps(self):
        """Integration queue must not accept tickets that form a cycle."""
        graph = DependencyGraph()
        queue = IntegrationQueue(dependency_graph=graph)
        queue.enqueue(MergeRequest(ticket_id="CB-X", branch="feat/x", commit_sha="a"))
        queue.enqueue(MergeRequest(ticket_id="CB-Y", branch="feat/y", commit_sha="b"))
        # Manually try to add a cycle — the graph should reject
        graph.add_dependency("CB-Y", "CB-X")
        with pytest.raises(CyclicDependencyError):
            graph.add_dependency("CB-X", "CB-Y")

    def test_force_push_invalidates_pending_merge(self):
        """If a branch is force-pushed, pending merge requests are invalidated."""
        graph = DependencyGraph()
        queue = IntegrationQueue(dependency_graph=graph)
        queue.enqueue(MergeRequest(
            ticket_id="CB-500", branch="feat/force", commit_sha="original-sha"
        ))
        # Simulate a force push — branch now points to a different commit
        result = queue.handle_force_push(
            branch_name="feat/force",
            old_sha="original-sha",
            new_sha="new-sha-after-force",
        )
        assert result["status"] == "invalidated"
        # The old merge request should no longer be executable
        with pytest.raises(Exception):
            queue.execute_merge("CB-500")

    def test_force_push_requires_re_enqueue(self):
        """After force push, ticket must be re-enqueued with new SHA."""
        graph = DependencyGraph()
        queue = IntegrationQueue(dependency_graph=graph)
        queue.enqueue(MergeRequest(
            ticket_id="CB-501", branch="feat/re-enqueue", commit_sha="old-sha"
        ))
        queue.handle_force_push(
            branch_name="feat/re-enqueue",
            old_sha="old-sha",
            new_sha="new-sha",
        )
        # Re-enqueue
        queue.enqueue(MergeRequest(
            ticket_id="CB-501", branch="feat/re-enqueue", commit_sha="new-sha"
        ))
        result = queue.execute_merge("CB-501")
        assert result["status"] == "merged"
        assert result["commit_sha"] == "new-sha"

    def test_empty_queue_produces_empty_order(self):
        """An empty queue should produce an empty merge order."""
        graph = DependencyGraph()
        queue = IntegrationQueue(dependency_graph=graph)
        assert queue.compute_merge_order() == []

    def test_single_ticket_merges_immediately(self):
        """A single ticket with no deps should merge without issue."""
        graph = DependencyGraph()
        queue = IntegrationQueue(dependency_graph=graph)
        queue.enqueue(MergeRequest(ticket_id="CB-600", branch="feat/solo", commit_sha="s"))
        result = queue.execute_merge("CB-600")
        assert result["status"] == "merged"
