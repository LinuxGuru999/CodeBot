#!/usr/bin/env python3
"""Tests for codebot/bot_metrics.py.

Covers: record_bot_metric, get_bot_metrics, get_success_rate, get_token_burn_rate,
atomic writes, run capping, and file size pruning.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

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

    def test_record_atomic_write_via_tmp_replace(self, tmp_state_dir: Path) -> None:
        """Metrics are written atomically using tmp+replace pattern."""
        metrics_path = tmp_state_dir / "bot_metrics.json"

        with patch("codebot.bot_metrics.os.getpid", return_value=12345):
            record_bot_metric("test_bot", alive=False, exit_code=0)

        # Verify no temp files remain
        temp_files = list(tmp_state_dir.glob("*.tmp"))
        assert len(temp_files) == 0
        assert metrics_path.exists()

    def test_record_runs_capped_at_max(self, tmp_state_dir: Path) -> None:
        """Runs list is capped at _MAX_RUNS_PER_BOT."""
        # Record more than max runs
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

        metrics_path = tmp_state_dir / "bot_metrics.json"
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


class TestGetBotMetrics:
    """Tests for get_bot_metrics function."""

    def test_get_returns_none_for_missing_bot(self, tmp_state_dir: Path) -> None:
        """Returns None when bot has no metrics."""
        result = get_bot_metrics("nonexistent_bot")
        assert result is None

    def test_get_returns_none_when_no_metrics_file(self, tmp_state_dir: Path) -> None:
        """Returns None when metrics file does not exist."""
        # Ensure file doesn't exist
        metrics_path = tmp_state_dir / "bot_metrics.json"
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
        metrics_path = tmp_state_dir / "bot_metrics.json"
        data = {"test_bot": {"runs": [], "total_tokens": 100}}
        metrics_path.write_text(json.dumps(data))

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
