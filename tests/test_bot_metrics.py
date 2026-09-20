"""Tests for codebot.bot_metrics — TDD RED phase.

Covers:
- record_bot_metric() creates and updates entries correctly
- alive vs not-alive + exit_code logic for successes/failures counting
- _MAX_RUNS_PER_BOT cap enforced
- _MAX_METRICS_FILE_BYTES pruning triggered
- get_success_rate() returns correct ratio and None for missing data
- get_token_burn_rate() averages correctly
- atomic write via tmp+replace verified
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot import bot_metrics


@pytest.fixture(autouse=True)
def isolated_state(tmp_path: Path) -> Path:
    """Point bot_metrics at a temporary directory for every test."""
    bot_metrics.set_state_dir(tmp_path)
    return tmp_path


class TestRecordBotMetricCreatesEntry:
    def test_creates_new_bot_entry_on_first_record(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0)
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert metrics is not None
        assert "runs" in metrics
        assert "successes" in metrics
        assert "failures" in metrics
        assert "total_tokens" in metrics
        assert "total_duration_s" in metrics

    def test_appends_run_timestamp(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0)
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0)
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert len(metrics["runs"]) == 2


class TestAliveAndExitCodeLogic:
    def test_alive_true_does_not_count_success_or_failure(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=True, exit_code=None)
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert metrics["successes"] == 0
        assert metrics["failures"] == 0

    def test_exit_code_zero_counts_as_success(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0)
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert metrics["successes"] == 1
        assert metrics["failures"] == 0

    def test_nonzero_exit_code_counts_as_failure(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=1)
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert metrics["successes"] == 0
        assert metrics["failures"] == 1

    def test_negative_exit_code_counts_as_failure(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=-9)
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert metrics["failures"] == 1

    def test_not_alive_but_none_exit_code_no_count(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=None)
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert metrics["successes"] == 0
        assert metrics["failures"] == 0


class TestTokenAndDurationTracking:
    def test_accumulates_tokens(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0, tokens_this_run=100)
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0, tokens_this_run=50)
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert metrics["total_tokens"] == 150

    def test_accumulates_duration_from_started_at(self, isolated_state: Path) -> None:
        now = 1700000000.0
        with patch("codebot.bot_metrics.time.time", return_value=now):
            bot_metrics.record_bot_metric(
                "agent-a", alive=False, exit_code=0,
                started_at=now - 10.0,
            )
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert metrics["total_duration_s"] == pytest.approx(10.0, abs=0.1)


class TestMaxRunsPerBotCap:
    def test_runs_capped_at_max_runs_per_bot(self, isolated_state: Path) -> None:
        max_runs = bot_metrics._MAX_RUNS_PER_BOT
        for _ in range(max_runs + 50):
            bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0)
        metrics = bot_metrics.get_bot_metrics("agent-a")
        assert len(metrics["runs"]) <= max_runs


class TestMaxMetricsFileBytesPruning:
    def test_pruning_triggered_when_file_exceeds_budget(self, isolated_state: Path) -> None:
        budget = bot_metrics._MAX_METRICS_FILE_BYTES
        # Record enough runs to exceed the byte budget
        for i in range(600):
            bot_metrics.record_bot_metric(f"bot-{i % 5}", alive=False, exit_code=0, tokens_this_run=100)

        metrics_path = bot_metrics._get_metrics_path()
        file_size = metrics_path.stat().st_size
        assert file_size <= budget, f"File size {file_size} exceeds budget {budget}"


class TestGetSuccessRate:
    def test_returns_none_when_no_data(self, isolated_state: Path) -> None:
        assert bot_metrics.get_success_rate("nonexistent") is None

    def test_returns_none_when_no_completed_runs(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=True, exit_code=None)
        assert bot_metrics.get_success_rate("agent-a") is None

    def test_returns_correct_ratio(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0)
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0)
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=1)
        rate = bot_metrics.get_success_rate("agent-a")
        assert rate == pytest.approx(2.0 / 3.0)

    def test_all_failures_returns_zero(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=1)
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=2)
        assert bot_metrics.get_success_rate("agent-a") == 0.0


class TestGetTokenBurnRate:
    def test_returns_none_when_no_data(self, isolated_state: Path) -> None:
        assert bot_metrics.get_token_burn_rate("nonexistent") is None

    def test_returns_average_tokens_per_run(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0, tokens_this_run=100)
        bot_metrics.record_bot_metric("agent-a", alive=False, exit_code=0, tokens_this_run=200)
        rate = bot_metrics.get_token_burn_rate("agent-a")
        assert rate == pytest.approx(150.0)


class TestAtomicWrite:
    def test_write_json_atomic_uses_tmp_then_replace(self, isolated_state: Path) -> None:
        target = isolated_state / "test.json"
        data = {"key": "value"}
        bot_metrics._write_json_atomic(target, data)

        assert target.exists()
        assert json.loads(target.read_text(encoding="utf-8")) == data
        # No leftover tmp files
        tmp_files = list(isolated_state.glob("*.tmp"))
        assert len(tmp_files) == 0

    def test_no_partial_write_on_crash(self, isolated_state: Path) -> None:
        target = isolated_state / "test.json"
        target.write_text('{"old": true}', encoding="utf-8")

        original_replace = Path.replace

        def failing_replace(self_path: Path, dest: Path) -> None:
            raise OSError("simulated crash")

        with patch.object(Path, "replace", failing_replace):
            with pytest.raises(OSError):
                bot_metrics._write_json_atomic(target, {"new": True})

        # Original content preserved because replace failed atomically
        assert json.loads(target.read_text(encoding="utf-8")) == {"old": True}


class TestGetAllMetrics:
    def test_returns_empty_dict_when_no_file(self, isolated_state: Path) -> None:
        assert bot_metrics.get_all_metrics() == {}

    def test_returns_all_bots(self, isolated_state: Path) -> None:
        bot_metrics.record_bot_metric("a", alive=False, exit_code=0)
        bot_metrics.record_bot_metric("b", alive=False, exit_code=1)
        all_m = bot_metrics.get_all_metrics()
        assert "a" in all_m
        assert "b" in all_m
