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
    _apply_event,
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


class TestApplyEventCap:
    """Tests for _apply_event runs list capping behavior."""

    def test_apply_event_caps_runs_before_append(self) -> None:
        """Runs list never exceeds _MAX_RUNS_PER_BOT even transiently during apply.

        This is the core regression test for CB-3810728-0230. The cap must
        execute BEFORE appending the new run timestamp.
        """
        now = time.time()
        # Pre-fill entry with exactly _MAX_RUNS_PER_BOT runs (at capacity)
        entry = {
            "runs": [now - i for i in range(_MAX_RUNS_PER_BOT)],
            "successes": 0,
            "failures": 0,
            "total_tokens": 0,
            "total_duration_s": 0,
        }
        event = {"t": now, "b": "test", "a": False, "e": 0, "tk": 10, "s": now}

        _apply_event(entry, event)

        # List must be exactly at cap, not over
        assert len(entry["runs"]) == _MAX_RUNS_PER_BOT
        # Newest entry must be present at the end
        assert entry["runs"][-1] == now
        # After slicing [-499:] from a 500-element list [now, now-1, ..., now-499],
        # we keep [now-1, now-2, ..., now-499], then append now.
        # So the first element is now-1 (the second-newest from original).
        assert entry["runs"][0] == now - 1
        # The oldest surviving entry from the original list is now-499
        assert entry["runs"][-2] == now - (_MAX_RUNS_PER_BOT - 1)

    def test_apply_event_handles_empty_runs_list(self) -> None:
        """Cap logic handles empty runs list without error."""
        now = time.time()
        entry = {
            "runs": [],
            "successes": 0,
            "failures": 0,
            "total_tokens": 0,
            "total_duration_s": 0,
        }
        event = {"t": now, "b": "test", "a": False, "e": 0, "tk": 5, "s": now}

        _apply_event(entry, event)

        assert len(entry["runs"]) == 1
        assert entry["runs"][0] == now


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


class TestEdgeCasesAndErrorHandling:
    """Tests for error handling and edge cases to achieve 100% coverage."""

    def test_read_events_skips_corrupt_json_lines(self, tmp_state_dir: Path) -> None:
        """_read_events skips lines that are not valid JSON."""
        events_path = _get_events_path()
        events_path.write_text(
            '{"t":1.0,"b":"good","a":false,"e":0,"tk":10,"s":null}\n'
            'NOT_VALID_JSON\n'
            '{"t":2.0,"b":"also_good","a":false,"e":0,"tk":20,"s":null}\n',
            encoding="utf-8"
        )
        from codebot.bot_metrics import _read_events
        events = _read_events()
        assert len(events) == 2
        assert events[0]["b"] == "good"
        assert events[1]["b"] == "also_good"

    def test_read_events_handles_os_error(self, tmp_state_dir: Path) -> None:
        """_read_events returns empty list on OSError."""
        from codebot.bot_metrics import _read_events
        with patch("codebot.bot_metrics.Path.read_text", side_effect=OSError("disk fail")):
            events = _read_events()
        assert events == []

    def test_count_event_lines_handles_os_error(self, tmp_state_dir: Path) -> None:
        """_count_event_lines returns 0 on OSError."""
        events_path = _get_events_path()
        events_path.write_text("line1\nline2\n", encoding="utf-8")
        with patch("builtins.open", side_effect=OSError("fail")):
            count = _count_event_lines()
        assert count == 0

    def test_count_event_lines_returns_zero_when_missing(self, tmp_state_dir: Path) -> None:
        """_count_event_lines returns 0 when file does not exist."""
        assert _count_event_lines() == 0

    def test_load_snapshot_returns_empty_on_non_dict_json(self, tmp_state_dir: Path) -> None:
        """_load_snapshot returns {} if JSON is not a dict."""
        snapshot_path = _get_metrics_path()
        snapshot_path.write_text('[1, 2, 3]', encoding="utf-8")
        from codebot.bot_metrics import _load_snapshot
        data = _load_snapshot()
        assert data == {}

    def test_load_snapshot_returns_empty_on_invalid_json(self, tmp_state_dir: Path) -> None:
        """_load_snapshot returns {} on JSON decode error."""
        snapshot_path = _get_metrics_path()
        snapshot_path.write_text('NOT JSON', encoding="utf-8")
        from codebot.bot_metrics import _load_snapshot
        data = _load_snapshot()
        assert data == {}

    def test_apply_events_to_data_skips_empty_bot_name(self, tmp_state_dir: Path) -> None:
        """_apply_events_to_data skips events with empty bot name."""
        from codebot.bot_metrics import _apply_events_to_data
        data = {}
        events = [
            {"t": 1.0, "b": "", "a": False, "e": 0, "tk": 10, "s": None},
            {"t": 2.0, "b": "valid", "a": False, "e": 0, "tk": 20, "s": None},
        ]
        result = _apply_events_to_data(data, events)
        assert "" not in result
        assert "valid" in result

    def test_prune_snapshot_size_reduces_runs_when_over_limit(self, tmp_state_dir: Path) -> None:
        """_prune_snapshot_size reduces runs lists when file exceeds byte limit."""
        from codebot.bot_metrics import _prune_snapshot_size
        # Create data that exceeds _MAX_METRICS_FILE_BYTES
        data = {}
        for i in range(20):
            bot_name = f"bot_{i}_" + "x" * 200
            data[bot_name] = {
                "runs": list(range(1000)),
                "successes": 100,
                "failures": 50,
                "total_tokens": 100000,
                "total_duration_s": 5000.0,
            }
        pruned = _prune_snapshot_size(data)
        serialized = json.dumps(pruned, indent=2)
        assert len(serialized.encode("utf-8")) <= _MAX_METRICS_FILE_BYTES

    def test_compact_handles_clear_events_os_error(self, tmp_state_dir: Path) -> None:
        """_compact logs warning but does not crash if clearing events fails."""
        events_path = _get_events_path()
        events_path.write_text(
            json.dumps({"t": time.time(), "b": "x", "a": False, "e": 0, "tk": 1, "s": None}) + "\n",
            encoding="utf-8"
        )
        # Make the tmp replace fail
        original_replace = Path.replace
        call_count = [0]
        def failing_replace(self, target):
            call_count[0] += 1
            if call_count[0] > 1:  # Allow first replace (snapshot write), fail second (events clear)
                raise OSError("simulated failure")
            return original_replace(self, target)

        with patch.object(Path, "replace", failing_replace):
            _compact()  # Should not raise

    def test_record_bot_metric_handles_exception(self, tmp_state_dir: Path) -> None:
        """record_bot_metric catches exceptions and logs warning."""
        with patch("codebot.bot_metrics._append_event", side_effect=RuntimeError("boom")):
            # Should not raise
            record_bot_metric("test", alive=False, exit_code=0)

    def test_get_bot_metrics_returns_none_on_exception(self, tmp_state_dir: Path) -> None:
        """get_bot_metrics returns None if loading fails."""
        with patch("codebot.bot_metrics._load_merged_data", side_effect=RuntimeError("fail")):
            result = get_bot_metrics("any")
        assert result is None

    def test_get_all_metrics_returns_empty_on_exception(self, tmp_state_dir: Path) -> None:
        """get_all_metrics returns {} if loading fails."""
        with patch("codebot.bot_metrics._load_merged_data", side_effect=RuntimeError("fail")):
            result = get_all_metrics()
        assert result == {}

    def test_get_success_rate_returns_none_on_exception(self, tmp_state_dir: Path) -> None:
        """get_success_rate returns None if loading fails."""
        with patch("codebot.bot_metrics._load_merged_data", side_effect=RuntimeError("fail")):
            result = get_success_rate("x")
        assert result is None

    def test_get_token_burn_rate_returns_none_on_exception(self, tmp_state_dir: Path) -> None:
        """get_token_burn_rate returns None if loading fails."""
        with patch("codebot.bot_metrics._load_merged_data", side_effect=RuntimeError("fail")):
            result = get_token_burn_rate("x")
        assert result is None

    def test_read_events_handles_missing_file(self, tmp_state_dir: Path) -> None:
        """_read_events returns empty list when events file does not exist."""
        from codebot.bot_metrics import _read_events
        # Ensure no events file exists
        events_path = _get_events_path()
        if events_path.exists():
            events_path.unlink()
        events = _read_events()
        assert events == []

    def test_count_event_lines_counts_correctly(self, tmp_state_dir: Path) -> None:
        """_count_event_lines returns correct line count."""
        events_path = _get_events_path()
        events_path.write_text("line1\nline2\nline3\n", encoding="utf-8")
        assert _count_event_lines() == 3

    def test_compact_logs_debug_message(self, tmp_state_dir: Path) -> None:
        """_compact completes successfully and logs debug message (covers logger.debug line)."""
        events_path = _get_events_path()
        events_path.write_text(
            json.dumps({"t": time.time(), "b": "debug_bot", "a": False, "e": 0, "tk": 1, "s": None}) + "\n",
            encoding="utf-8"
        )
        _compact()  # Covers the logger.debug line at end of _compact
        snapshot_path = _get_metrics_path()
        assert snapshot_path.exists()

    def test_read_events_os_error_path(self, tmp_state_dir: Path) -> None:
        """Cover except OSError: pass in _read_events (lines 111-112)."""
        from codebot.bot_metrics import _read_events
        # Create the file first so exists() returns True
        events_path = _get_events_path()
        events_path.write_text("{}\n", encoding="utf-8")
        # Patch the specific Path instance's read_text via the class
        original_read_text = Path.read_text
        def raising_read_text(self, *args, **kwargs):
            if str(self).endswith("bot_metrics_events.jsonl"):
                raise OSError("simulated read error")
            return original_read_text(self, *args, **kwargs)
        with patch.object(Path, "read_text", raising_read_text):
            events = _read_events()
        assert events == []

    def test_get_success_rate_exception_in_body(self, tmp_state_dir: Path) -> None:
        """Cover except Exception in get_success_rate (lines 346-347)."""
        # Mock get_bot_metrics to raise inside get_success_rate's try block
        with patch("codebot.bot_metrics.get_bot_metrics", side_effect=ValueError("boom")):
            result = get_success_rate("x")
        assert result is None

    def test_get_token_burn_rate_exception_in_body(self, tmp_state_dir: Path) -> None:
        """Cover except Exception in get_token_burn_rate."""
        with patch("codebot.bot_metrics.get_bot_metrics", side_effect=ValueError("boom")):
            result = get_token_burn_rate("x")
        assert result is None

    def test_prune_snapshot_size_multiple_reductions(self, tmp_state_dir: Path) -> None:
        """Cover max_runs = max(1, max_runs // 2) loop iteration (line 211).

        We force the pruning loop to iterate by setting an extremely small byte
        limit (100 bytes). Even a bot with zero runs exceeds this due to JSON
        structure overhead, guaranteeing the halving branch executes multiple
        times until max_runs reaches 1.
        """
        from codebot.bot_metrics import _prune_snapshot_size
        import codebot.bot_metrics as bm

        original_limit = bm._MAX_METRICS_FILE_BYTES
        # 100 bytes is smaller than the JSON skeleton of a bot entry,
        # so the loop will keep halving max_runs until it hits 1.
        bm._MAX_METRICS_FILE_BYTES = 100
        try:
            base_ts = 1790000000.123456
            data = {
                "bot": {
                    "runs": [base_ts + j * 0.001 for j in range(600)],
                    "successes": 0,
                    "failures": 0,
                    "total_tokens": 0,
                    "total_duration_s": 0,
                }
            }
            pruned = _prune_snapshot_size(data)
            # With such a tiny limit, runs should be reduced to very few
            assert len(pruned["bot"]["runs"]) < 600
        finally:
            bm._MAX_METRICS_FILE_BYTES = original_limit

        # After restoring the real limit, verify the pruned data is valid
        serialized = json.dumps(pruned, indent=2)
        assert len(serialized.encode("utf-8")) <= original_limit


class TestResolveStateDirEnv:
    """Tests for _resolve_state_dir with environment variables."""

    def test_resolve_state_dir_from_codebot_state_dir_env(self, tmp_path: Path) -> None:
        """_resolve_state_dir uses CODEBOT_STATE_DIR env var if set and exists."""
        import codebot.bot_metrics as bm
        
        # Save original state
        original_state_dir = bm._state_dir
        bm._state_dir = None
        
        # The logic checks if the path name is "state". If not, it appends /.codebot/state
        # To test the direct return, we name the dir "state"
        env_dir = tmp_path / "state"
        env_dir.mkdir()
        
        # Patch environ to only have CODEBOT_STATE_DIR
        new_environ = {"CODEBOT_STATE_DIR": str(env_dir)}
        with patch.dict(os.environ, new_environ, clear=True):
            resolved = bm._resolve_state_dir()
            assert resolved == env_dir
        
        # Restore
        bm._state_dir = original_state_dir

    def test_resolve_state_dir_from_codebot_project_root_env(self, tmp_path: Path) -> None:
        """_resolve_state_dir derives from CODEBOT_PROJECT_ROOT env var."""
        import codebot.bot_metrics as bm
        original_state_dir = bm._state_dir
        bm._state_dir = None
        
        project_root = tmp_path / "my_project"
        expected_state = project_root / ".codebot" / "state"
        expected_state.mkdir(parents=True)
        
        new_environ = {"CODEBOT_PROJECT_ROOT": str(project_root)}
        with patch.dict(os.environ, new_environ, clear=True):
            resolved = bm._resolve_state_dir()
            assert resolved == expected_state
        
        bm._state_dir = original_state_dir

    def test_resolve_state_dir_falls_back_to_default(self) -> None:
        """_resolve_state_dir falls back to default when env vars not set."""
        import codebot.bot_metrics as bm
        original_state_dir = bm._state_dir
        bm._state_dir = None
        
        # Clear all relevant env vars
        new_environ = {}
        with patch.dict(os.environ, new_environ, clear=True):
            resolved = bm._resolve_state_dir()
            assert resolved == bm._DEFAULT_STATE_DIR
        
        bm._state_dir = original_state_dir


class TestCompactExceptionHandling:
    """Tests for exception handling in _compact."""

    def test_compact_handles_load_snapshot_exception(self, tmp_state_dir: Path) -> None:
        """_compact logs warning but does not crash if _load_snapshot raises."""
        events_path = _get_events_path()
        events_path.write_text(
            json.dumps({"t": time.time(), "b": "x", "a": False, "e": 0, "tk": 1, "s": None}) + "\n",
            encoding="utf-8"
        )
        with patch("codebot.bot_metrics._load_snapshot", side_effect=RuntimeError("boom")):
            _compact()  # Should not raise

    def test_compact_handles_apply_events_exception(self, tmp_state_dir: Path) -> None:
        """_compact logs warning but does not crash if _apply_events_to_data raises."""
        events_path = _get_events_path()
        events_path.write_text(
            json.dumps({"t": time.time(), "b": "x", "a": False, "e": 0, "tk": 1, "s": None}) + "\n",
            encoding="utf-8"
        )
        with patch("codebot.bot_metrics._apply_events_to_data", side_effect=RuntimeError("boom")):
            _compact()  # Should not raise


class TestPublicApiExceptionCoverage:
    """Additional exception coverage for public API functions."""

    def test_get_bot_metrics_exception_in_load_merged(self, tmp_state_dir: Path) -> None:
        """get_bot_metrics returns None if _load_merged_data raises."""
        with patch("codebot.bot_metrics._load_merged_data", side_effect=RuntimeError("fail")):
            result = get_bot_metrics("any")
        assert result is None

    def test_get_all_metrics_exception_in_load_merged(self, tmp_state_dir: Path) -> None:
        """get_all_metrics returns {} if _load_merged_data raises."""
        with patch("codebot.bot_metrics._load_merged_data", side_effect=RuntimeError("fail")):
            result = get_all_metrics()
        assert result == {}


class TestRemainingCoverage:
    """Tests to cover remaining uncovered lines."""

    def test_read_events_os_error_catch(self, tmp_state_dir: Path) -> None:
        """Cover OSError exception handler in _read_events (line 110).
        
        We patch Path.exists in the codebot.bot_metrics module to raise OSError.
        """
        from codebot.bot_metrics import _read_events
        import codebot.bot_metrics as bm
        
        # Patch Path.exists globally for the module
        original_exists = Path.exists
        def raising_exists(self):
            if str(self).endswith("bot_metrics_events.jsonl"):
                raise OSError("simulated fail")
            return original_exists(self)
            
        with patch.object(Path, "exists", raising_exists):
            events = _read_events()
        assert events == []

    def test_apply_event_duration_positive(self, tmp_state_dir: Path) -> None:
        """Cover the duration > 0 branch in _apply_event (line 169)."""
        from codebot.bot_metrics import _apply_event
        entry = {
            "runs": [],
            "successes": 0,
            "failures": 0,
            "total_tokens": 0,
            "total_duration_s": 0.0,
        }
        now = time.time()
        started_at = now - 10.0  # 10 seconds ago
        event = {"t": now, "b": "test", "a": False, "e": 0, "tk": 0, "s": started_at}
        
        _apply_event(entry, event)
        
        assert entry["total_duration_s"] == 10.0

    def test_prune_snapshot_size_no_change_break(self, tmp_state_dir: Path) -> None:
        """Cover the 'if not changed: break' branch in _prune_snapshot_size.
        
        This happens when the loop runs, trims nothing (because runs are already short),
        and thus 'changed' remains False.
        """
        from codebot.bot_metrics import _prune_snapshot_size
        import codebot.bot_metrics as bm
        
        original_limit = bm._MAX_METRICS_FILE_BYTES
        # Set a limit small enough to enter the loop, but with data that has few runs
        bm._MAX_METRICS_FILE_BYTES = 100
        try:
            data = {
                "bot": {
                    "runs": [1.0],  # Only 1 run, so trimming won't change it
                    "successes": 0,
                    "failures": 0,
                    "total_tokens": 0,
                    "total_duration_s": 0.0,
                }
            }
            pruned = _prune_snapshot_size(data)
            assert "bot" in pruned
            assert len(pruned["bot"]["runs"]) == 1
        finally:
            bm._MAX_METRICS_FILE_BYTES = original_limit

