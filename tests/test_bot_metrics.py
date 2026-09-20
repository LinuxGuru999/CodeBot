#!/usr/bin/env python3
"""Tests for codebot/bot_metrics.py.

Covers: record_bot_metric, get_bot_metrics, get_success_rate, get_token_burn_rate
Tests are deterministic, isolated, fast, and follow AAA pattern.
"""

import json
import os
import time
from pathlib import Path
from unittest import mock

import pytest

from codebot.bot_metrics import (
    _MAX_METRICS_FILE_BYTES,
    _MAX_RUNS_PER_BOT,
    _METRICS_WINDOW_DAYS,
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

    def test_record_bot_metric_creates_new_entry(self, tmp_state_dir: Path) -> None:
        """Happy path: recording metric for a new bot creates entry."""
        started_at = time.time() - 10
        record_bot_metric("test_bot", alive=False, exit_code=0, tokens_this_run=100, started_at=started_at)

        metrics_path = tmp_state_dir / "bot_metrics.json"
        assert metrics_path.exists()
        data = json.loads(metrics_path.read_text())
        assert "test_bot" in data
        assert data["test_bot"]["successes"] == 1
        assert data["test_bot"]["failures"] == 0
        assert data["test_bot"]["total_tokens"] == 100
        assert len(data["test_bot"]["runs"]) == 1

    def test_record_bot_metric_alive_true_no_count(self, tmp_state_dir: Path) -> None:
        """Edge case: alive=True should not increment success/failure counts."""
        record_bot_metric("running_bot", alive=True, exit_code=None, tokens_this_run=50)

        metrics_path = tmp_state_dir / "bot_metrics.json"
        data = json.loads(metrics_path.read_text())
        assert data["running_bot"]["successes"] == 0
        assert data["running_bot"]["failures"] == 0
        assert len(data["running_bot"]["runs"]) == 1

    def test_record_bot_metric_exit_code_nonzero_failure(self, tmp_state_dir: Path) -> None:
        """Error path: exit_code != 0 increments failures."""
        record_bot_metric("failing_bot", alive=False, exit_code=1, tokens_this_run=0)

        metrics_path = tmp_state_dir / "bot_metrics.json"
        data = json.loads(metrics_path.read_text())
        assert data["failing_bot"]["successes"] == 0
        assert data["failing_bot"]["failures"] == 1

    def test_record_bot_metric_accumulates_multiple_runs(self, tmp_state_dir: Path) -> None:
        """Verify multiple runs accumulate correctly."""
        record_bot_metric("multi_bot", alive=False, exit_code=0, tokens_this_run=10)
        record_bot_metric("multi_bot", alive=False, exit_code=0, tokens_this_run=20)
        record_bot_metric("multi_bot", alive=False, exit_code=1, tokens_this_run=30)

        metrics_path = tmp_state_dir / "bot_metrics.json"
        data = json.loads(metrics_path.read_text())
        assert data["multi_bot"]["successes"] == 2
        assert data["multi_bot"]["failures"] == 1
        assert data["multi_bot"]["total_tokens"] == 60
        assert len(data["multi_bot"]["runs"]) == 3

    def test_record_bot_metric_max_runs_cap_enforced(self, tmp_state_dir: Path) -> None:
        """Verify _MAX_RUNS_PER_BOT cap is enforced."""
        # Record more than _MAX_RUNS_PER_BOT runs
        for i in range(_MAX_RUNS_PER_BOT + 50):
            record_bot_metric("cap_bot", alive=False, exit_code=0, tokens_this_run=1)

        metrics_path = tmp_state_dir / "bot_metrics.json"
        data = json.loads(metrics_path.read_text())
        assert len(data["cap_bot"]["runs"]) == _MAX_RUNS_PER_BOT

    def test_record_bot_metric_file_size_pruning(self, tmp_state_dir: Path) -> None:
        """Verify file size pruning when exceeding _MAX_METRICS_FILE_BYTES."""
        # Create a bot with many runs to exceed file size limit
        large_data = {
            "prune_bot": {
                "runs": [time.time() - i for i in range(10000)],
                "successes": 5000,
                "failures": 5000,
                "total_tokens": 100000,
                "total_duration_s": 10000.0,
            }
        }
        metrics_path = tmp_state_dir / "bot_metrics.json"
        metrics_path.write_text(json.dumps(large_data))

        # Record another metric to trigger pruning
        record_bot_metric("prune_bot", alive=False, exit_code=0, tokens_this_run=1)

        # File should be under the limit now
        final_size = metrics_path.stat().st_size
        assert final_size <= _MAX_METRICS_FILE_BYTES

        # Verify data still exists and is valid
        data = json.loads(metrics_path.read_text())
        assert "prune_bot" in data
        assert len(data["prune_bot"]["runs"]) <= 10  # Should be pruned to max_runs

    def test_record_bot_metric_atomic_write(self, tmp_state_dir: Path) -> None:
        """Verify atomic write via tmp+replace pattern."""
        # Mock replace to verify it's called
        with mock.patch.object(Path, "replace") as mock_replace:
            record_bot_metric("atomic_bot", alive=False, exit_code=0)
            mock_replace.assert_called_once()

    def test_record_bot_metric_handles_missing_started_at(self, tmp_state_dir: Path) -> None:
        """Verify duration calculation when started_at is None."""
        before = time.time()
        record_bot_metric("no_start_bot", alive=False, exit_code=0, started_at=None)
        after = time.time()

        metrics_path = tmp_state_dir / "bot_metrics.json"
        data = json.loads(metrics_path.read_text())
        duration = data["no_start_bot"]["total_duration_s"]
        assert 0 <= duration <= (after - before) + 1


class TestGetBotMetrics:
    """Tests for get_bot_metrics function."""

    def test_get_bot_metrics_returns_none_for_missing(self, tmp_state_dir: Path) -> None:
        """Verify None returned when bot doesn't exist."""
        result = get_bot_metrics("nonexistent_bot")
        assert result is None

    def test_get_bot_metrics_returns_none_when_file_missing(self, tmp_state_dir: Path) -> None:
        """Verify None returned when metrics file doesn't exist."""
        # Don't create any metrics
        result = get_bot_metrics("any_bot")
        assert result is None

    def test_get_bot_metrics_returns_correct_data(self, tmp_state_dir: Path) -> None:
        """Verify correct metrics returned for existing bot."""
        record_bot_metric("fetch_bot", alive=False, exit_code=0, tokens_this_run=42)
        
        result = get_bot_metrics("fetch_bot")
        assert result is not None
        assert result["successes"] == 1
        assert result["total_tokens"] == 42


class TestGetSuccessRate:
    """Tests for get_success_rate function."""

    def test_get_success_rate_none_when_no_runs(self, tmp_state_dir: Path) -> None:
        """Verify None returned when no runs exist for bot."""
        result = get_success_rate("new_bot")
        assert result is None

    def test_get_success_rate_none_when_file_missing(self, tmp_state_dir: Path) -> None:
        """Verify None returned when metrics file doesn't exist."""
        result = get_success_rate("any_bot")
        assert result is None

    def test_get_success_rate_all_successes(self, tmp_state_dir: Path) -> None:
        """Verify 1.0 returned when all runs succeeded."""
        for _ in range(5):
            record_bot_metric("perfect_bot", alive=False, exit_code=0)
        
        rate = get_success_rate("perfect_bot")
        assert rate == 1.0

    def test_get_success_rate_all_failures(self, tmp_state_dir: Path) -> None:
        """Verify 0.0 returned when all runs failed."""
        for _ in range(5):
            record_bot_metric("failing_bot", alive=False, exit_code=1)
        
        rate = get_success_rate("failing_bot")
        assert rate == 0.0

    def test_get_success_rate_mixed_results(self, tmp_state_dir: Path) -> None:
        """Verify correct ratio for mixed success/failure."""
        for _ in range(3):
            record_bot_metric("mixed_bot", alive=False, exit_code=0)
        for _ in range(7):
            record_bot_metric("mixed_bot", alive=False, exit_code=1)
        
        rate = get_success_rate("mixed_bot")
        assert rate == 0.3

    def test_get_success_rate_ignores_alive_runs(self, tmp_state_dir: Path) -> None:
        """Verify alive=True runs don't affect success rate calculation."""
        record_bot_metric("partial_bot", alive=True)  # Should not count
        record_bot_metric("partial_bot", alive=False, exit_code=0)
        record_bot_metric("partial_bot", alive=False, exit_code=1)
        
        rate = get_success_rate("partial_bot")
        assert rate == 0.5


class TestGetTokenBurnRate:
    """Tests for get_token_burn_rate function."""

    def test_get_token_burn_rate_none_when_no_runs(self, tmp_state_dir: Path) -> None:
        """Verify None returned when no runs exist."""
        result = get_token_burn_rate("new_bot")
        assert result is None

    def test_get_token_burn_rate_none_when_file_missing(self, tmp_state_dir: Path) -> None:
        """Verify None returned when metrics file doesn't exist."""
        result = get_token_burn_rate("any_bot")
        assert result is None

    def test_get_token_burn_rate_single_run(self, tmp_state_dir: Path) -> None:
        """Verify correct average for single run."""
        record_bot_metric("single_bot", alive=False, exit_code=0, tokens_this_run=100)
        
        rate = get_token_burn_rate("single_bot")
        assert rate == 100.0

    def test_get_token_burn_rate_multiple_runs(self, tmp_state_dir: Path) -> None:
        """Verify correct average across multiple runs."""
        record_bot_metric("avg_bot", alive=False, exit_code=0, tokens_this_run=10)
        record_bot_metric("avg_bot", alive=False, exit_code=0, tokens_this_run=20)
        record_bot_metric("avg_bot", alive=False, exit_code=0, tokens_this_run=30)
        
        rate = get_token_burn_rate("avg_bot")
        assert rate == 20.0


class TestGetAllMetrics:
    """Tests for get_all_metrics function."""

    def test_get_all_metrics_empty_when_file_missing(self, tmp_state_dir: Path) -> None:
        """Verify empty dict when metrics file doesn't exist."""
        result = get_all_metrics()
        assert result == {}

    def test_get_all_metrics_returns_all_bots(self, tmp_state_dir: Path) -> None:
        """Verify all bots returned."""
        record_bot_metric("bot_a", alive=False, exit_code=0)
        record_bot_metric("bot_b", alive=False, exit_code=1)
        
        result = get_all_metrics()
        assert "bot_a" in result
        assert "bot_b" in result
        assert len(result) == 2


class TestRunWindowCapping:
    """Tests for run window time-based capping."""

    def test_old_runs_pruned_by_time_window(self, tmp_state_dir: Path) -> None:
        """Verify runs older than _METRICS_WINDOW_DAYS are pruned."""
        old_time = time.time() - (_METRICS_WINDOW_DAYS * 86400) - 1000
        
        # Manually create metrics with old runs
        old_data = {
            "old_bot": {
                "runs": [old_time, time.time()],
                "successes": 0,
                "failures": 0,
                "total_tokens": 0,
                "total_duration_s": 0,
            }
        }
        metrics_path = tmp_state_dir / "bot_metrics.json"
        metrics_path.write_text(json.dumps(old_data))
        
        # Record new metric to trigger pruning
        record_bot_metric("old_bot", alive=False, exit_code=0)
        
        data = json.loads(metrics_path.read_text())
        # Old run should be pruned, only recent runs remain
        assert all(run > (time.time() - _METRICS_WINDOW_DAYS * 86400) for run in data["old_bot"]["runs"])
