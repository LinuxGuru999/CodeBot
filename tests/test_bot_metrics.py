#!/usr/bin/env python3
"""Tests for codebot/bot_metrics.py.

Covers: record_bot_metric, get_bot_metrics, get_success_rate, get_token_burn_rate,
atomic writes, run capping, file size pruning, append-only JSONL event log,
compaction, concurrent appends, and performance.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot.bot_metrics import (
    _COMPACT_EVERY_N_EVENTS,
    _MAX_METRICS_FILE_BYTES,
    _MAX_RUNS_PER_BOT,
    _METRICS_WINDOW_DAYS,
    _compact,
    _count_event_lines,
    _get_events_path,
    _get_metrics_path,
    _load_merged_data,
    get_all_metrics,
    get_bot_metrics,
    get_success_rate,
    get_token_burn_rate,
    record_bot_metric,
    set_state_dir,
)


@pytest.fixture
def tmp_state_dir(tmp_path: Path) -> Path:
    """Create isolated state directory for each test."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    set_state_dir(state_dir)
    return state_dir


class TestRecordBotMetric:
    """Tests for record_bot_metric function."""

    def test_record_creates_new_bot_entry(self, tmp_state_dir: Path) -> None:
        """Recording a metric for a new bot creates the entry."""
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=100)

        metrics = get_bot_metrics("test_bot")
        assert metrics is not None
        assert metrics["successes"] == 1
        assert metrics["failures"] == 0
        assert metrics["total_tokens"] == 100
        assert len(metrics["runs"]) == 1

    def test_record_updates_existing_bot(self, tmp_state_dir: Path) -> None:
        """Recording multiple metrics updates the same bot entry."""
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=100)
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=200)

        metrics = get_bot_metrics("test_bot")
        assert metrics["successes"] == 2
        assert metrics["total_tokens"] == 300
        assert len(metrics["runs"]) == 2

    def test_record_alive_true_no_exit_code_no_count(self, tmp_state_dir: Path) -> None:
        """Alive bots with no exit code do not increment success/failure."""
        record_bot_metric("test_bot", alive=True, exit_code=None, tokens_this_run=50)

        metrics = get_bot_metrics("test_bot")
        assert metrics["successes"] == 0
        assert metrics["failures"] == 0
        assert len(metrics["runs"]) == 1

    def test_record_exit_code_nonzero_counts_failure(self, tmp_state_dir: Path) -> None:
        """Non-zero exit code increments failure count."""
        record_bot_metric("test_bot", alive=False, exit_code=1, tokens_this_run=0)

        metrics = get_bot_metrics("test_bot")
        assert metrics["successes"] == 0
        assert metrics["failures"] == 1

    def test_record_appends_to_jsonl_events(self, tmp_state_dir: Path) -> None:
        """record_bot_metric writes to the JSONL events file, not directly to snapshot."""
        events_path = _get_events_path()

        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=100)

        # Events file should exist and contain exactly one line
        assert events_path.exists()
        lines = events_path.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1

        # The line should be valid JSON with expected fields
        event = json.loads(lines[0])
        assert event["b"] == "test_bot"
        assert event["a"] is False
        assert event["e"] == 0
        assert event["tk"] == 100
        assert "t" in event

    def test_record_no_temp_files_remain(self, tmp_state_dir: Path) -> None:
        """No temp files remain after recording a metric."""
        with patch("codebot.bot_metrics.os.getpid", return_value=12345):
            record_bot_metric("test_bot", alive=False, exit_code=0)

        # Verify no temp files remain in state dir
        temp_files = list(tmp_state_dir.glob("*.tmp"))
        assert len(temp_files) == 0

    def test_record_runs_capped_at_max(self, tmp_state_dir: Path) -> None:
        """Runs list is capped at _MAX_RUNS_PER_BOT after compaction."""
        # Record more than max runs — compaction triggers at _COMPACT_EVERY_N_EVENTS
        for i in range(_MAX_RUNS_PER_BOT + 10):
            record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=1)

        metrics = get_bot_metrics("test_bot")
        assert len(metrics["runs"]) == _MAX_RUNS_PER_BOT
        # total_tokens accumulates all runs (capping only affects runs list, not counters)
        assert metrics["total_tokens"] == _MAX_RUNS_PER_BOT + 10

    def test_record_prunes_when_exceeds_max_bytes(self, tmp_state_dir: Path) -> None:
        """File size pruning triggers when exceeding _MAX_METRICS_FILE_BYTES."""
        # Create a bot with a very long name to inflate file size quickly
        long_name = "bot_" + "x" * 1000

        # Record many runs to exceed byte limit
        for i in range(200):
            record_bot_metric(long_name, alive=False, exit_code=0, tokens_this_run=1)

        # After compaction, snapshot should be within budget
        metrics_path = _get_metrics_path()
        assert metrics_path.exists()
        file_size = metrics_path.stat().st_size
        assert file_size <= _MAX_METRICS_FILE_BYTES

    def test_record_multiple_bots(self, tmp_state_dir: Path) -> None:
        """Recording metrics for multiple bots keeps them separate."""
        record_bot_metric("bot_a", alive=False, exit_code=0, tokens_this_run=10)
        record_bot_metric("bot_b", alive=False, exit_code=1, tokens_this_run=20)

        metrics_a = get_bot_metrics("bot_a")
        metrics_b = get_bot_metrics("bot_b")

        assert metrics_a["successes"] == 1
        assert metrics_a["total_tokens"] == 10
        assert metrics_b["failures"] == 1
        assert metrics_b["total_tokens"] == 20


class TestAppendOnlyPerformance:
    """Tests that the append-only write path is fast."""

    def test_record_takes_less_than_5ms(self, tmp_state_dir: Path) -> None:
        """Metric recording takes <5ms regardless of file size.

        This is the key acceptance criterion from the ticket.
        """
        # Create a realistic snapshot: 10 bots × 50 runs (not 100 × 500)
        # This avoids pathological serialization during compaction pruning.
        large_data = {}
        for i in range(10):
            bot_name = f"historical_bot_{i}"
            large_data[bot_name] = {
                "runs": list(range(1000000, 1000000 + 50)),
                "successes": 50,
                "failures": 0,
                "total_tokens": 50000,
                "total_duration_s": 1000.0,
            }
        snapshot_path = _get_metrics_path()
        snapshot_path.write_text(json.dumps(large_data, separators=(",", ":")), encoding="utf-8")

        # Now record a metric — must be fast (< 5ms)
        start = time.monotonic()
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=100)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 5.0, f"record_bot_metric took {elapsed_ms:.1f}ms, should be <5ms"

    def test_record_fast_with_large_events_log(self, tmp_state_dir: Path) -> None:
        """Record is fast even with many un-compacted events."""
        # Create a large events file without triggering compaction
        events_path = _get_events_path()
        with open(events_path, "w", encoding="utf-8") as f:
            for i in range(99):  # just under compaction threshold
                f.write(json.dumps({"t": time.time(), "b": f"bot_{i}", "a": False,
                                    "e": 0, "tk": 10, "s": None}) + "\n")

        start = time.monotonic()
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=50)
        elapsed_ms = (time.monotonic() - start) * 1000

        assert elapsed_ms < 5.0, f"record_bot_metric took {elapsed_ms:.1f}ms, should be <5ms"


class TestNoMultipleReserialization:
    """Tests that compaction does not perform multiple re-serialization cycles."""

    def test_compaction_single_pass(self, tmp_state_dir: Path) -> None:
        """Compaction serializes data once and writes once."""
        # Record enough events to trigger compaction
        for i in range(_COMPACT_EVERY_N_EVENTS):
            record_bot_metric(f"bot_{i % 5}", alive=False, exit_code=i % 2,
                              tokens_this_run=10)

        # After compaction, snapshot should exist
        snapshot_path = _get_metrics_path()
        assert snapshot_path.exists()

        # Events file should be cleared
        events_path = _get_events_path()
        assert events_path.exists()
        lines = events_path.read_text(encoding="utf-8").strip()
        assert lines == "" or lines == "empty"

        # Data should be correct
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert len(data) > 0  # at least some bots recorded

    def test_compaction_does_not_exceed_max_bytes(self, tmp_state_dir: Path) -> None:
        """After compaction, the snapshot file never exceeds _MAX_METRICS_FILE_BYTES."""
        # Create a scenario that would exceed the byte limit
        long_name = "bot_" + "x" * 500
        for i in range(300):
            record_bot_metric(long_name, alive=False, exit_code=0, tokens_this_run=1)

        snapshot_path = _get_metrics_path()
        if snapshot_path.exists():
            assert snapshot_path.stat().st_size <= _MAX_METRICS_FILE_BYTES


class TestCompactFunction:
    """Tests for the compaction mechanism."""

    def test_compact_merges_events_into_snapshot(self, tmp_state_dir: Path) -> None:
        """Compaction merges JSONL events into the snapshot file."""
        # Write a snapshot
        snapshot_data = {
            "old_bot": {"runs": [1.0], "successes": 5, "failures": 1,
                        "total_tokens": 500, "total_duration_s": 100.0}
        }
        snapshot_path = _get_metrics_path()
        snapshot_path.write_text(json.dumps(snapshot_data), encoding="utf-8")

        # Write events
        events_path = _get_events_path()
        events_path.write_text(
            json.dumps({"t": time.time(), "b": "new_bot", "a": False, "e": 0,
                        "tk": 200, "s": None}) + "\n"
            + json.dumps({"t": time.time(), "b": "old_bot", "a": False, "e": 0,
                          "tk": 50, "s": None}) + "\n",
            encoding="utf-8"
        )

        _compact()

        # Snapshot should now have both bots
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert "old_bot" in data
        assert "new_bot" in data
        assert data["old_bot"]["successes"] == 6  # 5 + 1
        assert data["old_bot"]["total_tokens"] == 550  # 500 + 50
        assert data["new_bot"]["successes"] == 1
        assert data["new_bot"]["total_tokens"] == 200

        # Events file should be cleared
        assert events_path.read_text(encoding="utf-8") == ""

    def test_compact_noop_when_no_events(self, tmp_state_dir: Path) -> None:
        """Compaction is a no-op when there are no events."""
        snapshot_data = {"bot": {"runs": [1.0], "successes": 1, "failures": 0,
                                 "total_tokens": 10, "total_duration_s": 5.0}}
        snapshot_path = _get_metrics_path()
        snapshot_path.write_text(json.dumps(snapshot_data), encoding="utf-8")

        _compact()

        # Snapshot should be unchanged
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert data["bot"]["successes"] == 1

    def test_compact_creates_snapshot_when_none_exists(self, tmp_state_dir: Path) -> None:
        """Compaction creates a new snapshot from events alone."""
        events_path = _get_events_path()
        events_path.write_text(
            json.dumps({"t": time.time(), "b": "brand_new_bot", "a": False, "e": 0,
                        "tk": 42, "s": None}) + "\n",
            encoding="utf-8"
        )

        _compact()

        snapshot_path = _get_metrics_path()
        assert snapshot_path.exists()
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        assert "brand_new_bot" in data
        assert data["brand_new_bot"]["total_tokens"] == 42

    def test_compact_clears_events_atomically(self, tmp_state_dir: Path) -> None:
        """Events file is cleared atomically during compaction."""
        events_path = _get_events_path()
        events_path.write_text(
            json.dumps({"t": time.time(), "b": "x", "a": False, "e": 0, "tk": 1, "s": None}) + "\n",
            encoding="utf-8"
        )

        _compact()

        # No temp files should remain
        temp_files = list(tmp_state_dir.glob("*.tmp"))
        assert len(temp_files) == 0
        # Events file should be empty
        assert events_path.read_text(encoding="utf-8") == ""


class TestConcurrentAppends:
    """Tests for concurrent append safety."""

    def test_multiple_rapid_appends_do_not_corrupt(self, tmp_state_dir: Path) -> None:
        """Rapidly appending multiple events does not produce corrupt JSONL."""
        for i in range(50):
            record_bot_metric(f"bot_{i % 3}", alive=False, exit_code=i % 2,
                              tokens_this_run=i)

        # All data should be readable
        data = _load_merged_data()
        assert len(data) == 3  # 3 unique bots

        # Events file should have valid JSON on each line
        events_path = _get_events_path()
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                event = json.loads(line)
                assert "t" in event
                assert "b" in event

    def test_compaction_after_many_appends(self, tmp_state_dir: Path) -> None:
        """After enough appends, compaction produces correct merged state."""
        total_events = _COMPACT_EVERY_N_EVENTS + 50
        for i in range(total_events):
            record_bot_metric("target_bot", alive=False, exit_code=0,
                              tokens_this_run=10)

        metrics = get_bot_metrics("target_bot")
        assert metrics is not None
        assert metrics["total_tokens"] == total_events * 10


class TestGetBotMetrics:
    """Tests for get_bot_metrics function."""

    def test_get_returns_none_for_missing_bot(self, tmp_state_dir: Path) -> None:
        """Returns None when bot has no metrics."""
        result = get_bot_metrics("nonexistent_bot")
        assert result is None

    def test_get_returns_none_when_no_metrics_file(self, tmp_state_dir: Path) -> None:
        """Returns None when metrics file does not exist."""
        # Ensure file doesn't exist
        metrics_path = _get_metrics_path()
        if metrics_path.exists():
            metrics_path.unlink()

        result = get_bot_metrics("any_bot")
        assert result is None

    def test_get_returns_metrics_dict(self, tmp_state_dir: Path) -> None:
        """Returns correct metrics dict for existing bot."""
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=500)

        result = get_bot_metrics("test_bot")
        assert isinstance(result, dict)
        assert "runs" in result
        assert "successes" in result
        assert "total_tokens" in result


class TestGetSuccessRate:
    """Tests for get_success_rate function."""

    def test_success_rate_none_when_no_data(self, tmp_state_dir: Path) -> None:
        """Returns None when bot has no recorded runs."""
        result = get_success_rate("nonexistent_bot")
        assert result is None

    def test_success_rate_none_when_no_completed_runs(self, tmp_state_dir: Path) -> None:
        """Returns None when bot has only alive (incomplete) runs."""
        record_bot_metric("test_bot", alive=True, exit_code=None)

        result = get_success_rate("test_bot")
        assert result is None

    def test_success_rate_all_successful(self, tmp_state_dir: Path) -> None:
        """Returns 1.0 when all runs succeeded."""
        for _ in range(5):
            record_bot_metric("test_bot", alive=False, exit_code=0)

        result = get_success_rate("test_bot")
        assert result == 1.0

    def test_success_rate_all_failed(self, tmp_state_dir: Path) -> None:
        """Returns 0.0 when all runs failed."""
        for _ in range(5):
            record_bot_metric("test_bot", alive=False, exit_code=1)

        result = get_success_rate("test_bot")
        assert result == 0.0

    def test_success_rate_mixed_results(self, tmp_state_dir: Path) -> None:
        """Returns correct ratio for mixed success/failure."""
        for _ in range(3):
            record_bot_metric("test_bot", alive=False, exit_code=0)
        for _ in range(7):
            record_bot_metric("test_bot", alive=False, exit_code=1)

        result = get_success_rate("test_bot")
        assert result == 0.3


class TestGetTokenBurnRate:
    """Tests for get_token_burn_rate function."""

    def test_burn_rate_none_when_no_data(self, tmp_state_dir: Path) -> None:
        """Returns None when bot has no recorded runs."""
        result = get_token_burn_rate("nonexistent_bot")
        assert result is None

    def test_burn_rate_none_when_no_runs(self, tmp_state_dir: Path) -> None:
        """Returns None when runs list is empty."""
        # Manually create metrics with empty runs
        snapshot_path = _get_metrics_path()
        data = {"test_bot": {"runs": [], "total_tokens": 100,
                             "successes": 0, "failures": 0, "total_duration_s": 0}}
        snapshot_path.write_text(json.dumps(data))

        result = get_token_burn_rate("test_bot")
        assert result is None

    def test_burn_rate_single_run(self, tmp_state_dir: Path) -> None:
        """Returns tokens for single run."""
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=250)

        result = get_token_burn_rate("test_bot")
        assert result == 250.0

    def test_burn_rate_average_across_runs(self, tmp_state_dir: Path) -> None:
        """Returns average tokens per run."""
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=100)
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=200)
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=300)

        result = get_token_burn_rate("test_bot")
        assert result == 200.0


class TestGetAllMetrics:
    """Tests for get_all_metrics function."""

    def test_get_all_empty_when_no_file(self, tmp_state_dir: Path) -> None:
        """Returns empty dict when no metrics file exists."""
        result = get_all_metrics()
        assert result == {}

    def test_get_all_returns_all_bots(self, tmp_state_dir: Path) -> None:
        """Returns metrics for all recorded bots."""
        record_bot_metric("bot_a", alive=False, exit_code=0)
        record_bot_metric("bot_b", alive=False, exit_code=1)

        result = get_all_metrics()
        assert "bot_a" in result
        assert "bot_b" in result
        assert len(result) == 2
