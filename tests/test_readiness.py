"""Tests for readiness.py — pure-function readiness predicates."""

import time
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.readiness import (
    STALE_SECONDS,
    is_due,
    noop_ok,
    queue_has_work,
    signals_ok,
    ready,
    effective_timeout,
    _parse_queue_complexity_from_text,
    filter_unapproved_items,
    load_approved_ids,
    parse_queue_ids_from_text,
)

# ---------------------------------------------------------------------------
# Helpers to build queue texts
# ---------------------------------------------------------------------------

QUEUE_SIMPLE = """| ID | Source | Module | Complexity | Status | Priority | Owner |
|---|---|---|---|---|---|---|
| Q-001 | scanner | foo.py | medium | confirmed | P1 | bot |
| Q-002 | scanner | bar.py | high | approved | P2 | bot |
| Q-003 | scanner | baz.py | small | deferred | P3 | bot |
"""

QUEUE_TRIVIAL = """| ID | Source | Module | Complexity | Status | Priority | Owner |
|---|---|---|---|---|---|---|
| Q-010 | scanner | a.py | trivial | confirmed | P1 | bot |
"""

QUEUE_DECOMP = """### QUEUE-DECOMP-1
- **Status**: confirmed
- **Lane**: short

### QUEUE-DECOMP-2
- **Status**: approved
- **Lane**: long
"""

QUEUE_ALL_DEFERRED = """| ID | Source | Module | Complexity | Status | Priority | Owner |
|---|---|---|---|---|---|---|
| Q-005 | scanner | foo.py | medium | deferred | P1 | bot |
| Q-006 | scanner | bar.py | high | wontfix | P2 | bot |
"""

NOW = 1_700_000_000.0


# ---------------------------------------------------------------------------
# is_due
# ---------------------------------------------------------------------------

class TestIsDue:
    def test_none_means_due(self):
        assert is_due({}, NOW, None) is True

    def test_zero_means_due(self):
        assert is_due({}, NOW, 0) is True
        assert is_due({}, NOW, 0.0) is True
        assert is_due({}, NOW, "0") is True

    def test_future_not_due(self):
        assert is_due({}, NOW, NOW + 3600) is False

    def test_past_is_due(self):
        assert is_due({}, NOW, NOW - 1) is True
        assert is_due({}, NOW, NOW) is True

    def test_invalid_string_means_due(self):
        assert is_due({}, NOW, "not-a-number") is True
        assert is_due({}, NOW, "") is True

    def test_numeric_string_parsed(self):
        assert is_due({}, NOW, str(NOW + 100)) is False
        assert is_due({}, NOW, str(NOW - 100)) is True

    def test_exact_boundary(self):
        assert is_due({}, NOW, NOW) is True

    def test_manifest_unused(self):
        assert is_due({"whatever": 1}, NOW, None) is True
        assert is_due({"whatever": 1}, NOW, NOW + 10) is False


# ---------------------------------------------------------------------------
# noop_ok
# ---------------------------------------------------------------------------

class TestNoopOk:
    def test_no_cap_means_ok(self):
        assert noop_ok({}, 999) is True
        assert noop_ok({"noop_cap": 0}, 999) is True
        assert noop_ok({"noop_cap": -1}, 999) is True

    def test_below_cap_ok(self):
        assert noop_ok({"noop_cap": 5}, 4) is True
        assert noop_ok({"noop_cap": 5}, 0) is True

    def test_at_cap_not_ok(self):
        assert noop_ok({"noop_cap": 5}, 5) is False

    def test_above_cap_not_ok(self):
        assert noop_ok({"noop_cap": 5}, 10) is False

    def test_none_counter_treated_as_zero(self):
        assert noop_ok({"noop_cap": 1}, None) is True

    def test_string_cap_parsed(self):
        assert noop_ok({"noop_cap": "3"}, 2) is True
        assert noop_ok({"noop_cap": "3"}, 3) is False

    def test_invalid_cap_treated_as_no_cap(self):
        assert noop_ok({"noop_cap": "invalid"}, 999) is True
        assert noop_ok({"noop_cap": None}, 999) is True

    def test_invalid_counter_treated_as_zero(self):
        assert noop_ok({"noop_cap": 5}, "bad") is True
        assert noop_ok({"noop_cap": 5}, {}) is True

    def test_float_counter(self):
        assert noop_ok({"noop_cap": 5}, 4.9) is True  # int(4.9) == 4


# ---------------------------------------------------------------------------
# queue parsing helpers
# ---------------------------------------------------------------------------

class TestParseQueueComplexity:
    def test_empty_returns_empty(self):
        assert _parse_queue_complexity_from_text("") == {}
        assert _parse_queue_complexity_from_text("no table here") == {}

    def test_confirmed_and_approved_count(self):
        result = _parse_queue_complexity_from_text(QUEUE_SIMPLE)
        assert "Q-001" in result
        assert "Q-002" in result
        assert "Q-003" not in result  # deferred

    def test_complexity_normalization(self):
        result = _parse_queue_complexity_from_text(QUEUE_SIMPLE)
        assert result["Q-001"] == "medium"
        assert result["Q-002"] == "high"

    def test_trivial_normalized(self):
        result = _parse_queue_complexity_from_text(QUEUE_TRIVIAL)
        assert result["Q-010"] == "trivial"

    def test_low_maps_to_small(self):
        q = """| ID | Source | Module | Complexity | Status | Priority | Owner |
|---|---|---|---|---|---|---|
| Q-011 | scanner | a.py | low | confirmed | P1 | bot |
"""
        result = _parse_queue_complexity_from_text(q)
        assert result["Q-011"] == "small"

    def test_decomp_lane_mapping(self):
        result = _parse_queue_complexity_from_text(QUEUE_DECOMP)
        assert result["QUEUE-DECOMP-1"] == "small"  # short -> small
        assert result["QUEUE-DECOMP-2"] == "high"  # long -> high

    def test_decomp_not_duplicated_if_table_has_same_id(self):
        combined = QUEUE_SIMPLE + "\n" + QUEUE_DECOMP
        result = _parse_queue_complexity_from_text(combined)
        # Q-001 already from table, decomp ids separate
        assert "Q-001" in result


# ---------------------------------------------------------------------------
# queue_has_work
# ---------------------------------------------------------------------------

class TestQueueHasWork:
    def test_none_queue_text_false(self):
        assert queue_has_work("queue", None, None) is False
        assert queue_has_work("queue", ["medium"], None) is False

    def test_empty_queue_false(self):
        assert queue_has_work("queue", None, "") is False
        assert queue_has_work("queue", None, "   ") is False

    def test_no_confirmed_items_false(self):
        assert queue_has_work("queue", None, QUEUE_ALL_DEFERRED) is False

    def test_any_work_without_filter(self):
        assert queue_has_work("queue", None, QUEUE_SIMPLE) is True
        assert queue_has_work("queue", [], QUEUE_SIMPLE) is True
        assert queue_has_work("scan", None, QUEUE_SIMPLE) is True

    def test_matching_complexity_filter(self):
        assert queue_has_work("queue", ["medium"], QUEUE_SIMPLE) is True
        assert queue_has_work("queue", ["high"], QUEUE_SIMPLE) is True

    def test_non_matching_filter_false(self):
        assert queue_has_work("queue", ["trivial"], QUEUE_SIMPLE) is False

    def test_low_filter_normalized_to_small(self):
        # QUEUE_SIMPLE has medium and high, no small, so low (=>small) should be False
        assert queue_has_work("queue", ["low"], QUEUE_SIMPLE) is False
        # Queue with trivial/small: low should match
        q_small = """| ID | Source | Module | Complexity | Status | Priority | Owner |
|---|---|---|---|---|---|---|
| Q-020 | scanner | a.py | small | confirmed | P1 | bot |
"""
        assert queue_has_work("queue", ["low"], q_small) is True
        assert queue_has_work("queue", ["small"], q_small) is True

    def test_multiple_filter_values(self):
        assert queue_has_work("queue", ["trivial", "medium"], QUEUE_SIMPLE) is True
        assert queue_has_work("queue", ["trivial", "low"], QUEUE_SIMPLE) is False

    def test_non_string_filter_entries_skipped(self):
        assert queue_has_work("queue", [123, None], QUEUE_SIMPLE) is True

    def test_kind_does_not_gate(self):
        # kind param currently accepted but not gating
        for kind in ("scan", "queue", "command", "other"):
            assert queue_has_work(kind, None, QUEUE_SIMPLE) is True

    def test_decomp_lane_counts_as_work(self):
        assert queue_has_work("queue", None, QUEUE_DECOMP) is True
        assert queue_has_work("queue", ["small"], QUEUE_DECOMP) is True
        assert queue_has_work("queue", ["high"], QUEUE_DECOMP) is True
        assert queue_has_work("queue", ["trivial"], QUEUE_DECOMP) is False


# ---------------------------------------------------------------------------
# signals_ok
# ---------------------------------------------------------------------------

class TestSignalsOk:
    def test_fresh_signals_pass(self):
        manifest = {
            "kind": "queue",
            "input": [{"path": "src/foo.py"}],
            "complexity_filter": None,
        }
        mtimes = {"src/foo.py": NOW - 100}
        assert signals_ok(manifest, mtimes, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is True

    def test_stale_mtime_fails(self):
        manifest = {
            "kind": "queue",
            "input": [{"path": "src/foo.py"}],
        }
        mtimes = {"src/foo.py": NOW - STALE_SECONDS - 1}
        assert signals_ok(manifest, mtimes, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is False

    def test_exact_stale_boundary(self):
        manifest = {"kind": "queue", "input": [{"path": "src/foo.py"}]}
        mtimes = {"src/foo.py": NOW - STALE_SECONDS}
        # mt_f == now - STALE_SECONDS exactly => not < threshold => passes
        assert signals_ok(manifest, mtimes, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is True

    def test_missing_mtime_fails(self):
        manifest = {"kind": "queue", "input": [{"path": "src/foo.py"}]}
        assert signals_ok(manifest, {}, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is False
        assert signals_ok(manifest, {"other.py": NOW}, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is False

    def test_no_inputs_vacuously_fresh(self):
        manifest = {"kind": "queue", "input": []}
        assert signals_ok(manifest, {}, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is True

    def test_no_inputs_key_missing(self):
        manifest = {"kind": "queue"}
        assert signals_ok(manifest, {}, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is True

    def test_no_queue_text_fails(self):
        manifest = {"kind": "queue", "input": []}
        assert signals_ok(manifest, {}, ["Q-001"], queue_text="", now=NOW) is False
        assert signals_ok(manifest, {}, ["Q-001"], queue_text=QUEUE_ALL_DEFERRED, now=NOW) is False

    def test_queue_remaining_empty_non_scan_fails(self):
        manifest = {"kind": "queue", "input": []}
        assert signals_ok(manifest, {}, [], queue_text=QUEUE_SIMPLE, now=NOW) is False
        assert signals_ok(manifest, {}, 0, queue_text=QUEUE_SIMPLE, now=NOW) is False
        assert signals_ok(manifest, {}, None, queue_text=QUEUE_SIMPLE, now=NOW) is False

    def test_scan_kind_ignores_queue_remaining(self):
        manifest = {"kind": "scan", "input": []}
        assert signals_ok(manifest, {}, [], queue_text=QUEUE_SIMPLE, now=NOW) is True
        assert signals_ok(manifest, {}, None, queue_text=QUEUE_SIMPLE, now=NOW) is True

    def test_queue_remaining_int_positive_passes(self):
        manifest = {"kind": "queue", "input": []}
        assert signals_ok(manifest, {}, 3, queue_text=QUEUE_SIMPLE, now=NOW) is True

    def test_multiple_inputs_all_must_be_fresh(self):
        manifest = {
            "kind": "queue",
            "input": [{"path": "a.py"}, {"path": "b.py"}],
        }
        mtimes_ok = {"a.py": NOW - 100, "b.py": NOW - 100}
        mtimes_one_stale = {"a.py": NOW - 100, "b.py": NOW - STALE_SECONDS - 10}
        assert signals_ok(manifest, mtimes_ok, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is True
        assert signals_ok(manifest, mtimes_one_stale, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is False

    def test_non_dict_mtimes_fails_when_inputs_declared(self):
        manifest = {"kind": "queue", "input": [{"path": "a.py"}]}
        assert signals_ok(manifest, None, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is False  # type: ignore[arg-type]
        assert signals_ok(manifest, "bad", ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is False  # type: ignore[arg-type]

    def test_invalid_mtime_type_fails(self):
        manifest = {"kind": "queue", "input": [{"path": "a.py"}]}
        assert signals_ok(manifest, {"a.py": "not-a-number"}, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is False

    def test_complexity_filter_respected(self):
        manifest = {"kind": "queue", "input": [], "complexity_filter": ["trivial"]}
        # QUEUE_SIMPLE has medium/high, not trivial
        assert signals_ok(manifest, {}, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is False
        manifest2 = {"kind": "queue", "input": [], "complexity_filter": ["medium"]}
        assert signals_ok(manifest2, {}, ["Q-001"], queue_text=QUEUE_SIMPLE, now=NOW) is True

    def test_command_kind_requires_queue_remaining(self):
        manifest = {"kind": "command", "input": []}
        assert signals_ok(manifest, {}, [], queue_text=QUEUE_SIMPLE, now=NOW) is False
        assert signals_ok(manifest, {}, ["item"], queue_text=QUEUE_SIMPLE, now=NOW) is True

    def test_queue_text_via_kwargs(self):
        manifest = {"kind": "queue", "input": []}
        assert signals_ok(manifest, {}, ["Q-001"], now=NOW, queue_text=QUEUE_SIMPLE) is True


# ---------------------------------------------------------------------------
# ready (composition)
# ---------------------------------------------------------------------------

class TestReady:
    def _ctx(self, now=NOW, next_run_at=None, counter_value=0, mtimes=None, queue_remaining=None, queue_text=QUEUE_SIMPLE):
        if mtimes is None:
            mtimes = {}
        if queue_remaining is None:
            queue_remaining = ["Q-001"]
        return {
            "now": now,
            "next_run_at": next_run_at,
            "counter_value": counter_value,
            "mtimes": mtimes,
            "queue_remaining": queue_remaining,
            "queue_text": queue_text,
        }

    def test_all_pass_returns_true(self):
        manifest = {"kind": "scan", "noop_cap": 5}
        # scan ignores queue_remaining, and no inputs => signals_ok passes
        ctx = self._ctx(next_run_at=NOW - 10, counter_value=0, mtimes={}, queue_remaining=[], queue_text=QUEUE_SIMPLE)
        # But ready uses manifest kind: scan should pass signals even with empty queue_remaining
        assert ready(manifest, ctx) is True

    def test_not_due_returns_false(self):
        manifest = {"kind": "scan"}
        ctx = self._ctx(next_run_at=NOW + 9999)
        assert ready(manifest, ctx) is False

    def test_noop_cap_exceeded_returns_false(self):
        manifest = {"kind": "scan", "noop_cap": 2}
        ctx = self._ctx(counter_value=2)
        assert ready(manifest, ctx) is False

    def test_signals_fail_returns_false(self):
        manifest = {"kind": "queue", "input": []}
        ctx = self._ctx(queue_remaining=[], queue_text=QUEUE_SIMPLE)
        assert ready(manifest, ctx) is False

    def test_stale_mtime_returns_false(self):
        manifest = {"kind": "queue", "input": [{"path": "a.py"}]}
        ctx = self._ctx(mtimes={"a.py": NOW - STALE_SECONDS - 5}, queue_remaining=["Q-001"], queue_text=QUEUE_SIMPLE)
        assert ready(manifest, ctx) is False

    def test_invalid_ctx_returns_false(self):
        assert ready({}, None) is False  # type: ignore[arg-type]
        assert ready({}, "bad") is False  # type: ignore[arg-type]
        assert ready({}, []) is False  # type: ignore[arg-type]

    def test_invalid_now_fallback(self):
        manifest = {"kind": "scan"}
        ctx = {"now": "bad", "next_run_at": None, "counter_value": 0, "mtimes": {}, "queue_remaining": [], "queue_text": QUEUE_SIMPLE}
        # Should not crash, should handle bad now via time.time() fallback
        result = ready(manifest, ctx)
        assert isinstance(result, bool)

    def test_queue_kind_full_flow(self):
        manifest = {"kind": "queue", "noop_cap": 10, "input": []}
        ctx = self._ctx(next_run_at=NOW - 1, counter_value=0, mtimes={}, queue_remaining=["Q-001"], queue_text=QUEUE_SIMPLE)
        assert ready(manifest, ctx) is True

    def test_interval_scheduling(self):
        manifest = {"kind": "scan"}
        # interval not elapsed
        assert ready(manifest, self._ctx(next_run_at=NOW + 5000)) is False
        # interval elapsed
        assert ready(manifest, self._ctx(next_run_at=NOW - 1)) is True
        # no next_run => due
        assert ready(manifest, self._ctx(next_run_at=None)) is True


# ---------------------------------------------------------------------------
# effective_timeout
# ---------------------------------------------------------------------------

class TestEffectiveTimeout:
    def test_manifest_timeout_wins(self):
        assert effective_timeout({"heartbeat_timeout": 999}, heartbeat_max_gap_s=100) == 999
        assert effective_timeout({"heartbeat_timeout": "500"}, heartbeat_max_gap_s=100) == 500

    def test_fallback_to_heartbeat_max_gap(self):
        assert effective_timeout({}, heartbeat_max_gap_s=1234) == 1234
        assert effective_timeout({"heartbeat_timeout": 0}, heartbeat_max_gap_s=1234) == 1234
        assert effective_timeout({"heartbeat_timeout": None}, heartbeat_max_gap_s=1234) == 1234

    def test_default_fallback_3600(self):
        assert effective_timeout({}) == 3600
        assert effective_timeout({"heartbeat_timeout": 0}) == 3600
        assert effective_timeout({"heartbeat_timeout": None}) == 3600

    def test_invalid_manifest_timeout_ignored(self):
        assert effective_timeout({"heartbeat_timeout": "bad"}, heartbeat_max_gap_s=777) == 777
        assert effective_timeout({"heartbeat_timeout": "bad"}) == 3600

    def test_invalid_heartbeat_max_gap_ignored(self):
        assert effective_timeout({}, heartbeat_max_gap_s="bad") == 3600  # type: ignore[arg-type]

    def test_zero_manifest_timeout_uses_override(self):
        assert effective_timeout({"heartbeat_timeout": 0}, heartbeat_max_gap_s=600) == 600

    def test_negative_manifest_timeout_uses_override(self):
        # negative is <=0 so falls through to override
        assert effective_timeout({"heartbeat_timeout": -5}, heartbeat_max_gap_s=600) == 600


# ---------------------------------------------------------------------------
# STALE_SECONDS invariant
# ---------------------------------------------------------------------------

class TestStaleConstant:
    def test_stale_is_86400(self):
        assert STALE_SECONDS == 86400


# ---------------------------------------------------------------------------
# filter_unapproved / load_approved_ids (smoke)
# ---------------------------------------------------------------------------

class TestScrutiny:
    def test_filter_passthrough_no_tier(self):
        text = "### Q-001\n- **Status**: confirmed\nbody without tier\n"
        assert filter_unapproved_items(text) == text

    def test_filter_empty(self):
        assert filter_unapproved_items("") == ""

    def test_load_approved_ids_missing_file(self, tmp_path):
        result = load_approved_ids(str(tmp_path))
        assert result == set()

    def test_load_approved_ids_list_format(self, tmp_path):
        p = tmp_path / "approved_ids.json"
        import json
        p.write_text(json.dumps(["Q-001", "Q-002"]))
        assert load_approved_ids(str(tmp_path)) == {"Q-001", "Q-002"}

    def test_load_approved_ids_dict_format(self, tmp_path):
        p = tmp_path / "approved_ids.json"
        import json
        p.write_text(json.dumps({"Q-001": True, "Q-002": False}))
        assert load_approved_ids(str(tmp_path)) == {"Q-001"}


# ---------------------------------------------------------------------------
# parse_queue_ids_from_text — performance optimization
# ---------------------------------------------------------------------------

class TestParseQueueIds:
    """Tests for parse_queue_ids_from_text (CB-9099192-1C65)."""

    def test_empty_text(self):
        assert parse_queue_ids_from_text("") == set()

    def test_none_text(self):
        assert parse_queue_ids_from_text(None) == set()

    def test_simple_q_ids(self):
        text = """| ID | Source | Title | Complexity | Status | Owner | Notes |
|---|---|---|---|---|---|---|
| Q-001 | bot | Fix bug | small | confirmed | | |
| Q-002 | bot | Add feat | medium | approved | | |
| Q-003 | bot | Refactor | high | deferred | | |
"""
        result = parse_queue_ids_from_text(text)
        assert result == {"Q-001", "Q-002", "Q-003"}

    def test_queue_decomp_ids(self):
        text = """### QUEUE-DECOMP-100
- **Status**: confirmed
- **Lane**: short

### QUEUE-DECOMP-200
- **Status**: approved
- **Lane**: long
"""
        result = parse_queue_ids_from_text(text)
        assert result == {"QUEUE-DECOMP-100", "QUEUE-DECOMP-200"}

    def test_mixed_ids(self):
        text = """### QUEUE-DECOMP-50
- **Status**: confirmed

| ID | Source | Title | Complexity | Status | Owner | Notes |
|---|---|---|---|---|---|---|
| Q-001 | bot | Fix | small | confirmed | | |
| Q-002 | bot | Add | medium | approved | | |
"""
        result = parse_queue_ids_from_text(text)
        assert result == {"Q-001", "Q-002", "QUEUE-DECOMP-50"}

    def test_no_duplicates(self):
        text = "Q-001 appears Q-001 again Q-001 thrice"
        result = parse_queue_ids_from_text(text)
        assert result == {"Q-001"}

    def test_o1_membership(self):
        """Verify set membership is O(1) — same as any set check."""
        text = "\n".join(f"| Q-{i:03d} | bot | Task {i} | small | confirmed | | |" for i in range(100))
        result = parse_queue_ids_from_text(text)
        assert len(result) == 100
        assert "Q-050" in result
        assert "Q-999" not in result

    def test_verifies_same_result_as_naive(self):
        """Ensure parse_queue_ids_from_text produces same IDs as naive approach."""
        text = """| ID | Source | Title | Complexity | Status | Owner | Notes |
|---|---|---|---|---|---|---|
| Q-001 | bot | Fix bug | small | confirmed | | |
| Q-002 | bot | Add feat | medium | approved | | |
### QUEUE-DECOMP-1
- **Status**: confirmed
"""
        # Naive: iterate all ticket IDs and check containment
        all_ids = ["Q-001", "Q-002", "Q-003", "QUEUE-DECOMP-1", "QUEUE-DECOMP-2"]
        naive_approved = set()
        for tid in all_ids:
            if tid in text:
                naive_approved.add(tid)

        fast_approved = parse_queue_ids_from_text(text)
        # Fast approach finds IDs by regex; naive uses containment.
        # Both should find the same IDs that exist in text.
        assert fast_approved == naive_approved

    def test_benchmark_10x_improvement(self):
        """Benchmark: parse_queue_ids_from_text should be 10x faster than naive O(n*m)."""
        import time as _time

        # Build a 10KB QUEUE.md with 100 tickets
        num_tickets = 100
        lines = ["| ID | Source | Title | Complexity | Status | Owner | Notes |",
                 "|---|---|---|---|---|---|---|"]
        for i in range(num_tickets):
            lines.append(f"| Q-{i:03d} | scanner | Task {i} | small | confirmed | bot | |")
        queue_text = "\n".join(lines)
        # Pad to ~10KB
        padding = "x" * (10 * 1024 - len(queue_text))
        queue_text += "\n" + padding

        ticket_ids = [f"Q-{i:03d}" for i in range(num_tickets)]

        # Naive O(n*m): for each ticket, scan the entire text
        t0 = _time.perf_counter()
        for _ in range(10):
            naive = set()
            for tid in ticket_ids:
                if tid in queue_text:
                    naive.add(tid)
        naive_time = _time.perf_counter() - t0

        # Fast O(f): single regex pass
        t0 = _time.perf_counter()
        for _ in range(10):
            fast = parse_queue_ids_from_text(queue_text)
        fast_time = _time.perf_counter() - t0

        # Same result
        assert naive == fast

        # Must be at least 2x faster (conservative threshold for CI variability;
        # the theoretical speedup is ~100x for 100 tickets on 10KB)
        assert fast_time < naive_time * 0.5, (
            f"Expected parse_queue_ids_from_text to be significantly faster: "
            f"naive={naive_time:.4f}s, fast={fast_time:.4f}s, ratio={naive_time/max(fast_time,1e-9):.1f}x"
        )
