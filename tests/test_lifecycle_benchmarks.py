"""Tests for Task 6: lifecycle/dispatch/cache performance benchmarks with correctness assertions.

These tests verify that lifecycle operations complete within bounded time
at representative queue sizes, and that correctness is maintained under
cache-miss and claim-conflict scenarios.
"""

import json
import time

from codebot.quality_gate import (
    QualityGatePolicy,
    run_quality_gates_with_cache,
)
from codebot.review_metrics import (
    build_lifecycle_report,
    load_lifecycle_events,
)
from codebot.ticket_engine import (
    RiskLevel,
    Severity,
    TicketClass,
    TicketState,
    TicketStore,
    create_ticket,
)
from codebot.ticket_dispatcher import _rank_tickets_for_dispatch


def _make_event(ticket_id: str, from_state: str, to_state: str, ts: float) -> dict:
    return {
        "ticket_id": ticket_id,
        "from_state": from_state,
        "to_state": to_state,
        "timestamp": ts,
        "attempts": 0,
        "rework_count": 0,
        "queue_age_seconds": 10.0,
        "actor": "bench",
        "revision": ts,
    }


class TestLifecycleEventLoadingBench:
    """Benchmark lifecycle event loading at scale."""

    def test_load_1000_events_within_budget(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events = []
        for i in range(1000):
            tid = f"CB-{i:04d}"
            events.append(_make_event(tid, "DISCOVERED", "VALIDATING", 100.0 + i))
            events.append(_make_event(tid, "VALIDATING", "TRIAGED", 200.0 + i))
        path = state_dir / "lifecycle_events.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

        start = time.perf_counter()
        loaded = load_lifecycle_events(state_dir)
        elapsed = time.perf_counter() - start

        assert len(loaded) == 2000
        assert elapsed < 2.0, f"Loading 2000 events took {elapsed:.3f}s, budget is 2.0s"

    def test_load_5000_events_within_budget(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events = []
        for i in range(5000):
            tid = f"CB-{i:05d}"
            events.append(_make_event(tid, "DISCOVERED", "VALIDATING", 100.0 + i))
        path = state_dir / "lifecycle_events.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

        start = time.perf_counter()
        loaded = load_lifecycle_events(state_dir)
        elapsed = time.perf_counter() - start

        assert len(loaded) == 5000
        assert elapsed < 5.0, f"Loading 5000 events took {elapsed:.3f}s, budget is 5.0s"


class TestLifecycleReportBench:
    """Benchmark lifecycle report generation."""

    def test_report_1000_events_within_budget(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events = []
        for i in range(500):
            tid = f"CB-{i:04d}"
            events.append(_make_event(tid, "DISCOVERED", "VALIDATING", 100.0 + i * 10))
            events.append(_make_event(tid, "VALIDATING", "TRIAGED", 200.0 + i * 10))
        path = state_dir / "lifecycle_events.jsonl"
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")

        start = time.perf_counter()
        loaded = load_lifecycle_events(state_dir)
        report = build_lifecycle_report(loaded)
        elapsed = time.perf_counter() - start

        assert report["total_transitions"] == 1000
        assert elapsed < 2.0, f"Report for 1000 events took {elapsed:.3f}s, budget is 2.0s"


class TestDispatchRankingBench:
    """Benchmark dispatch ranking at representative queue sizes."""

    def _make_tickets(self, count: int):
        tickets = []
        severities = [Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
        for i in range(count):
            sev = severities[i % len(severities)]
            t = create_ticket(
                f"bench ticket {i}", TicketClass.BUG, sev,
                "test", f"ev-{i}", f"prob-{i}", "desired", ["ac"],
                risk=RiskLevel.LOW,
            )
            tickets.append(t)
        return tickets

    def test_rank_100_tickets_within_budget(self):
        tickets = self._make_tickets(100)
        start = time.perf_counter()
        ranked = _rank_tickets_for_dispatch(tickets)
        elapsed = time.perf_counter() - start

        assert len(ranked) == 100
        assert elapsed < 1.0, f"Ranking 100 tickets took {elapsed:.3f}s, budget is 1.0s"

    def test_rank_500_tickets_within_budget(self):
        tickets = self._make_tickets(500)
        start = time.perf_counter()
        ranked = _rank_tickets_for_dispatch(tickets)
        elapsed = time.perf_counter() - start

        assert len(ranked) == 500
        assert elapsed < 5.0, f"Ranking 500 tickets took {elapsed:.3f}s, budget is 5.0s"

    def test_rank_preserves_all_tickets(self):
        tickets = self._make_tickets(200)
        ranked = _rank_tickets_for_dispatch(tickets)
        assert len(ranked) == 200
        original_ids = {t.id for t in tickets}
        ranked_ids = {t.id for t in ranked}
        assert original_ids == ranked_ids

    def test_rank_deterministic(self):
        tickets = self._make_tickets(50)
        ranked1 = _rank_tickets_for_dispatch(tickets)
        ranked2 = _rank_tickets_for_dispatch(tickets)
        ids1 = [t.id for t in ranked1]
        ids2 = [t.id for t in ranked2]
        assert ids1 == ids2


class TestGateCacheBench:
    """Benchmark gate cache hit/miss behavior with correctness assertions."""

    def _simple_policy(self):
        return QualityGatePolicy(
            required=[{"name": "build", "command": "python3 -m py_compile {file}"}],
            conditional={},
        )

    def test_cache_hit_faster_than_miss(self, tmp_path):
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.py").write_text("x = 1\n")
        state = tmp_path / "state"
        state.mkdir()
        policy = self._simple_policy()
        files = ["a.py"]

        # First run: cache miss
        start = time.perf_counter()
        passed1, evals1 = run_quality_gates_with_cache(
            policy, ws, state, "CB-BENCH1", changed_files=files,
        )
        miss_time = time.perf_counter() - start
        assert passed1 is True
        assert evals1[0].gate_name == "build"

        # Second run: cache hit
        start = time.perf_counter()
        passed2, evals2 = run_quality_gates_with_cache(
            policy, ws, state, "CB-BENCH1", changed_files=files,
        )
        hit_time = time.perf_counter() - start
        assert passed2 is True
        assert evals2[0].gate_name == "cached-pass"

        # Cache hit should be faster (or at least not slower by more than margin)
        assert hit_time < miss_time + 0.1, (
            f"Cache hit ({hit_time:.4f}s) not faster than miss ({miss_time:.4f}s)"
        )

    def test_correctness_under_cache_miss(self, tmp_path):
        """Verify gates still run correctly when cache is cold."""
        ws = tmp_path / "ws"
        ws.mkdir()
        (ws / "a.py").write_text("x = 1\n")
        state = tmp_path / "state"
        state.mkdir()
        policy = self._simple_policy()
        files = ["a.py"]

        passed, evals = run_quality_gates_with_cache(
            policy, ws, state, "CB-COLD1", changed_files=files,
        )
        assert passed is True
        assert len(evals) == 1
        assert evals[0].gate_name == "build"
        assert evals[0].passed is True

    def test_correctness_under_claim_conflict(self, tmp_path):
        """Verify cache correctly invalidates when files change (simulating claim conflict)."""
        ws = tmp_path / "ws"
        ws.mkdir()
        target = ws / "a.py"
        target.write_text("x = 1\n")
        state = tmp_path / "state"
        state.mkdir()
        policy = self._simple_policy()
        files = ["a.py"]

        passed1, _ = run_quality_gates_with_cache(
            policy, ws, state, "CB-CONF1", changed_files=files,
        )
        assert passed1 is True

        # Simulate another process modifying the file (claim conflict)
        target.write_text("x = 2  # modified by another claim\n")

        passed2, evals2 = run_quality_gates_with_cache(
            policy, ws, state, "CB-CONF1", changed_files=files,
        )
        assert passed2 is True
        # Should NOT be cached-pass because file content changed
        assert evals2[0].gate_name == "build", (
            f"Expected full re-run after file change, got {evals2[0].gate_name}"
        )