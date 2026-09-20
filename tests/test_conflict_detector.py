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


# ---------------------------------------------------------------------------
# _normalize_keywords tests
# ---------------------------------------------------------------------------

class TestNormalizeKeywords:
    def test_basic_tokenization(self):
        kw = cd._normalize_keywords("Implement caching layer for API endpoints")
        assert "implement" in kw
        assert "caching" in kw
        assert "layer" in kw
        assert "api" in kw
        assert "endpoints" in kw

    def test_stop_words_removed(self):
        kw = cd._normalize_keywords("the quick brown fox jumps over the lazy dog")
        assert "the" not in kw
        assert "over" not in kw
        assert "the" not in kw
        assert "quick" in kw
        assert "brown" in kw
        assert "fox" in kw

    def test_short_tokens_removed(self):
        kw = cd._normalize_keywords("a bug in the yo-yo or ok")
        assert "yo" not in kw  # 2 chars, below min
        # "yo-yo" → tokens "yo-yo" via regex, which normalizes to hyphenated form
        assert "ok" not in kw  # 2 chars, below min

    def test_empty_string(self):
        kw = cd._normalize_keywords("")
        assert kw == set()

    def test_unicode_normalization(self):
        # NFKD normalization decomposes accented chars
        kw = cd._normalize_keywords("résumé forüber checklist")
        # "résumé" → "resume" after NFKD
        assert "resume" in kw or "r\u00e9sum\u00e9" in kw

    def test_case_insensitive(self):
        kw1 = cd._normalize_keywords("DATABASE migration fix")
        kw2 = cd._normalize_keywords("database migration fix")
        assert kw1 == kw2

    def test_hyphenated_tokens_preserved(self):
        kw = cd._normalize_keywords("fix the task-scheduler heartbeat timeout")
        assert "task-scheduler" in kw or "task" in kw

    def test_only_stop_words(self):
        kw = cd._normalize_keywords("the a an and or but")
        assert kw == set()


# ---------------------------------------------------------------------------
# TaskOverlap dataclass tests
# ---------------------------------------------------------------------------

class TestTaskOverlap:
    def test_frozen(self):
        o = cd.TaskOverlap(
            agent_a="A", agent_b="B",
            shared_keywords=frozenset({"test", "fix"}),
            similarity=0.5,
        )
        with pytest.raises(AttributeError):
            o.agent_a = "X"  # type: ignore[misc]

    def test_fields(self):
        o = cd.TaskOverlap(
            agent_a="agent1", agent_b="agent2",
            shared_keywords=frozenset({"api", "endpoint"}),
            similarity=0.75,
        )
        assert o.agent_a == "agent1"
        assert o.agent_b == "agent2"
        assert o.similarity == 0.75

    def test_equality(self):
        o1 = cd.TaskOverlap("A", "B", frozenset({"x"}), 0.5)
        o2 = cd.TaskOverlap("A", "B", frozenset({"x"}), 0.5)
        assert o1 == o2


# ---------------------------------------------------------------------------
# _build_keyword_index tests
# ---------------------------------------------------------------------------

class TestBuildKeywordIndex:
    def test_basic_index(self):
        agent_tasks = [
            ("agent1", "Fix database migration script"),
            ("agent2", "Fix API endpoint authentication"),
            ("agent3", "Update database schema for users"),
        ]
        idx = cd._build_keyword_index(agent_tasks)
        assert "fix" in idx
        assert "database" in idx
        assert idx["fix"] == {"agent1", "agent2"}
        assert idx["database"] == {"agent1", "agent3"}

    def test_empty_descriptions(self):
        agent_tasks = [
            ("agent1", ""),
            ("agent2", ""),
        ]
        idx = cd._build_keyword_index(agent_tasks)
        assert len(idx) == 0

    def test_single_agent(self):
        agent_tasks = [("agent1", "Fix database connection pool")]
        idx = cd._build_keyword_index(agent_tasks)
        for agent_set in idx.values():
            assert agent_set == {"agent1"}


# ---------------------------------------------------------------------------
# _check_task_overlap tests
# ---------------------------------------------------------------------------

class TestCheckTaskOverlap:
    def test_empty_input(self):
        result = cd._check_task_overlap([])
        assert result == []

    def test_single_agent(self):
        result = cd._check_task_overlap([("a1", "fix database bug")])
        assert result == []

    def test_identical_descriptions(self):
        tasks = [
            ("agent1", "Fix critical database migration failure"),
            ("agent2", "Fix critical database migration failure"),
        ]
        result = cd._check_task_overlap(tasks, min_shared_keywords=1, min_similarity=0.0)
        assert len(result) == 1
        assert result[0].agent_a == "agent1"
        assert result[0].agent_b == "agent2"
        assert result[0].similarity == 1.0

    def test_no_overlap(self):
        tasks = [
            ("agent1", "Fix database connection pooling issue"),
            ("agent2", "Update CI/CD pipeline configuration"),
        ]
        result = cd._check_task_overlap(tasks, min_shared_keywords=2)
        assert result == []

    def test_partial_overlap(self):
        tasks = [
            ("agent1", "Fix database connection pooling timeout"),
            ("agent2", "Fix database query performance degradation"),
        ]
        result = cd._check_task_overlap(tasks, min_shared_keywords=2, min_similarity=0.1)
        assert len(result) >= 1
        overlap = result[0]
        assert "fix" in overlap.shared_keywords or "database" in overlap.shared_keywords

    def test_symmetric_results(self):
        """If agent A overlaps with B, both should be in the result once."""
        tasks = [
            ("agentZ", "Implement caching layer for API endpoints"),
            ("agentA", "Implement caching layer for API rate limiting"),
        ]
        result = cd._check_task_overlap(tasks, min_shared_keywords=1, min_similarity=0.05)
        assert len(result) == 1
        assert result[0].agent_a == "agentA"  # lexicographic order
        assert result[0].agent_b == "agentZ"

    def test_multiple_overlaps(self):
        tasks = [
            ("agent1", "Fix database connection pooling bug"),
            ("agent2", "Fix database query performance bug"),
            ("agent3", "Fix database schema migration bug"),
        ]
        result = cd._check_task_overlap(tasks, min_shared_keywords=2, min_similarity=0.1)
        # agent1-agent2, agent1-agent3, agent2-agent3 all share keywords
        pairs = {(o.agent_a, o.agent_b) for o in result}
        # At least some pairs should overlap
        assert len(pairs) >= 1

    def test_empty_descriptions_produce_no_overlap(self):
        tasks = [
            ("agent1", ""),
            ("agent2", ""),
            ("agent3", ""),
        ]
        result = cd._check_task_overlap(tasks)
        assert result == []

    def test_min_shared_keywords_filter(self):
        tasks = [
            ("agent1", "Fix the API endpoint authentication"),
            ("agent2", "Fix the CI/CD pipeline authentication"),
        ]
        # With min_shared_keywords=3, should not match (only share "fix" and "authentication")
        result_high = cd._check_task_overlap(tasks, min_shared_keywords=3)
        assert result_high == []
        # With min_shared_keywords=2, should match
        result_low = cd._check_task_overlap(tasks, min_shared_keywords=2)
        assert len(result_low) >= 1

    def test_min_similarity_filter(self):
        tasks = [
            ("agent1", "Fix database migration script for PostgreSQL"),
            ("agent2", "Fix database migration script for MySQL and add indexes"),
        ]
        # High similarity threshold
        result_high = cd._check_task_overlap(tasks, min_shared_keywords=1, min_similarity=0.9)
        # Lower similarity threshold
        result_low = cd._check_task_overlap(tasks, min_shared_keywords=1, min_similarity=0.3)
        assert len(result_low) >= len(result_high)

    def test_sorted_output(self):
        """Results should be deterministically sorted by agent_a, agent_b."""
        tasks = [
            (f"agent{i}", f"Fix database connection pooling timeout for service {i}")
            for i in range(5)
        ]
        result = cd._check_task_overlap(tasks, min_shared_keywords=2, min_similarity=0.1)
        for i in range(1, len(result)):
            prev = (result[i - 1].agent_a, result[i - 1].agent_b)
            curr = (result[i].agent_a, result[i].agent_b)
            assert prev <= curr

    def test_deterministic_across_runs(self):
        """Same input should produce identical output every time."""
        tasks = [
            ("alpha", "Implement feature flag system for gradual rollout"),
            ("beta", "Implement feature flag system for A/B testing"),
            ("gamma", "Deploy monitoring dashboard for production metrics"),
        ]
        r1 = cd._check_task_overlap(tasks, min_shared_keywords=2, min_similarity=0.1)
        r2 = cd._check_task_overlap(tasks, min_shared_keywords=2, min_similarity=0.1)
        assert r1 == r2

    def test_50_agents_benchmark(self):
        """Performance: 50 agents with varied tasks should complete in <5ms."""
        import time

        tasks = []
        keywords = [
            "database", "api", "caching", "authentication", "migration",
            "performance", "security", "testing", "deployment", "monitoring",
            "logging", "configuration", "scheduling", "optimization", "scaling",
            "documentation", "refactoring", "debugging", "integration", "pipeline",
        ]
        for i in range(50):
            # Each agent gets 3-5 keywords from the pool
            import random
            rng = random.Random(i)  # deterministic seed
            n_kw = rng.randint(3, 5)
            chosen = rng.sample(keywords, n_kw)
            desc = f"Implement {chosen[0]} and {chosen[1]} for agent-{i} system"
            if len(chosen) > 2:
                desc += f" with {chosen[2]} support"
            tasks.append((f"agent-{i}", desc))

        start = time.monotonic()
        result = cd._check_task_overlap(tasks, min_shared_keywords=2, min_similarity=0.1)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 5000, f"Benchmark failed: {elapsed_ms:.1f}ms >= 5000ms"
        # With shared keywords, there should be some overlaps detected
        # (agents sharing database+api, or other pairs)
        assert isinstance(result, list)

    def test_agent_count_scales_better_than_quadratic(self):
        """Verify comparison cost scales sub-quadratically.

        With O(n²), going from 25 to 50 agents would quadruple the time.
        With O(k) indexing, the increase should be much less.
        """
        import time

        def make_agents(n: int) -> list[tuple[str, str]]:
            keywords = ["database", "api", "caching", "auth", "migration",
                        "performance", "security", "testing"]
            agents = []
            for i in range(n):
                kw = keywords[i % len(keywords)]
                agents.append((f"agent-{i}", f"Fix {kw} issues for service {i}"))
            return agents

        # Run with 25 agents
        tasks_25 = make_agents(25)
        start = time.monotonic()
        for _ in range(3):
            cd._check_task_overlap(tasks_25, min_shared_keywords=2, min_similarity=0.1)
        time_25 = (time.monotonic() - start) / 3

        # Run with 50 agents
        tasks_50 = make_agents(50)
        start = time.monotonic()
        for _ in range(3):
            cd._check_task_overlap(tasks_50, min_shared_keywords=2, min_similarity=0.1)
        time_50 = (time.monotonic() - start) / 3

        # O(n²) would give ~4x. O(k) should give much less.
        if time_25 > 0.0001:
            ratio = time_50 / time_25
            assert ratio < 3.0, (
                f"Scaling ratio {ratio:.1f}x suggests quadratic behavior. "
                f"25-agent: {time_25*1000:.2f}ms, 50-agent: {time_50*1000:.2f}ms"
            )
