"""Tests for codebot/bot_metrics.py — metric recording, retrieval, aggregation."""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import codebot.bot_metrics as bm


def _fixed_write(path: Path, data):
    if isinstance(data, str):
        tmp = path.with_name(f"{path.name}.{__import__('os').getpid()}.tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(path)
    else:
        bm._write_json_atomic_orig(path, data) if hasattr(bm, "_write_json_atomic_orig") else None
        tmp = path.with_name(f"{path.name}.{__import__('os').getpid()}.tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)

@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    d = tmp_path / "state"
    d.mkdir()
    bm.set_state_dir(d)
    orig = bm._write_json_atomic
    bm._write_json_atomic_orig = orig  # type: ignore

    def patched(path, data):
        if isinstance(data, str):
            tmp = path.with_name(f"{path.name}.{__import__('os').getpid()}.tmp")
            tmp.write_text(data, encoding="utf-8")
            tmp.replace(path)
        else:
            tmp = path.with_name(f"{path.name}.{__import__('os').getpid()}.tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(path)

    monkeypatch.setattr(bm, "_write_json_atomic", patched)
    yield d
    bm._STATE_DIR = Path(".codebot/state")


class TestSetStateDir:
    def test_creates_dir(self, tmp_path):
        d = tmp_path / "new_state"
        bm.set_state_dir(d)
        assert d.exists()
        bm._STATE_DIR = Path(".codebot/state")

    def test_sets_global(self, tmp_path):
        d = tmp_path / "sd"
        bm.set_state_dir(d)
        assert bm._STATE_DIR == d
        bm._STATE_DIR = Path(".codebot/state")


class TestRecordAndGet:
    def test_record_creates_file(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0, tokens_this_run=100)
        assert (state_dir / "bot_metrics.json").exists()

    def test_record_success_increments_successes(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        data = bm.get_bot_metrics("bot-a")
        assert data["successes"] == 1
        assert data["failures"] == 0

    def test_record_failure_increments_failures(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=1)
        data = bm.get_bot_metrics("bot-a")
        assert data["failures"] == 1

    def test_record_alive_no_success_or_failure(self, state_dir):
        bm.record_bot_metric("bot-a", alive=True, exit_code=None)
        data = bm.get_bot_metrics("bot-a")
        assert data["successes"] == 0
        assert data["failures"] == 0

    def test_record_none_exit_code_no_count(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=None)
        data = bm.get_bot_metrics("bot-a")
        assert data["successes"] == 0
        assert data["failures"] == 0

    def test_record_tokens_accumulated(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0, tokens_this_run=50)
        bm.record_bot_metric("bot-a", alive=False, exit_code=0, tokens_this_run=30)
        data = bm.get_bot_metrics("bot-a")
        assert data["total_tokens"] == 80

    def test_record_total_duration(self, state_dir):
        now = time.time()
        with patch("codebot.bot_metrics.time.time", return_value=now):
            bm.record_bot_metric("bot-a", alive=False, exit_code=0, started_at=now - 10)
        data = bm.get_bot_metrics("bot-a")
        assert data["total_duration_s"] == pytest.approx(10, abs=0.1)

    def test_record_runs_appended(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        data = bm.get_bot_metrics("bot-a")
        assert len(data["runs"]) == 2

    def test_record_multiple_bots(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        bm.record_bot_metric("bot-b", alive=False, exit_code=1)
        assert bm.get_bot_metrics("bot-a")["successes"] == 1
        assert bm.get_bot_metrics("bot-b")["failures"] == 1

    def test_record_prunes_old_runs(self, state_dir):
        old = time.time() - 8 * 86400
        recent = time.time()
        with patch("codebot.bot_metrics.time.time", return_value=recent):
            path = state_dir / "bot_metrics.json"
            path.write_text(json.dumps({"bot-a": {"runs": [old], "successes": 0, "failures": 0, "total_tokens": 0, "total_duration_s": 0}}))
            bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        data = bm.get_bot_metrics("bot-a")
        assert old not in data["runs"]

    def test_record_caps_runs_at_500(self, state_dir):
        now = time.time()
        with patch("codebot.bot_metrics.time.time", return_value=now):
            bm._MAX_RUNS_PER_BOT = 5
            for _ in range(10):
                bm.record_bot_metric("bot-a", alive=False, exit_code=0)
            data = bm.get_bot_metrics("bot-a")
            assert len(data["runs"]) <= 5
            bm._MAX_RUNS_PER_BOT = 500

    def test_record_never_raises_on_corrupt_file(self, state_dir):
        (state_dir / "bot_metrics.json").write_text("not json")
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        # Should not raise; file may be overwritten or warning logged

    def test_get_missing_bot_returns_none(self, state_dir):
        assert bm.get_bot_metrics("nonexistent") is None

    def test_get_no_file_returns_none(self, state_dir):
        assert bm.get_bot_metrics("any") is None

    def test_get_all_empty(self, state_dir):
        assert bm.get_all_metrics() == {}

    def test_get_all_returns_all(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        bm.record_bot_metric("bot-b", alive=False, exit_code=0)
        all_m = bm.get_all_metrics()
        assert "bot-a" in all_m
        assert "bot-b" in all_m

    def test_get_all_corrupt_returns_empty(self, state_dir):
        (state_dir / "bot_metrics.json").write_text("bad json")
        assert bm.get_all_metrics() == {}

    def test_get_corrupt_returns_none(self, state_dir):
        (state_dir / "bot_metrics.json").write_text("bad json")
        assert bm.get_bot_metrics("any") is None


class TestSuccessRate:
    def test_no_data_returns_none(self, state_dir):
        assert bm.get_success_rate("nope") is None

    def test_no_runs_returns_none(self, state_dir):
        (state_dir / "bot_metrics.json").write_text(json.dumps({"bot-a": {"runs": [], "successes": 0, "failures": 0, "total_tokens": 0, "total_duration_s": 0}}))
        assert bm.get_success_rate("bot-a") is None

    def test_all_success(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        assert bm.get_success_rate("bot-a") == pytest.approx(1.0)

    def test_half_success(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        bm.record_bot_metric("bot-a", alive=False, exit_code=1)
        assert bm.get_success_rate("bot-a") == pytest.approx(0.5)

    def test_all_failure(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=1)
        bm.record_bot_metric("bot-a", alive=False, exit_code=2)
        assert bm.get_success_rate("bot-a") == pytest.approx(0.0)

    def test_boundary_one_success_one_failure(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        bm.record_bot_metric("bot-a", alive=False, exit_code=99)
        assert 0 < bm.get_success_rate("bot-a") < 1


class TestTokenBurnRate:
    def test_no_data_returns_none(self, state_dir):
        assert bm.get_token_burn_rate("nope") is None

    def test_no_runs_returns_none(self, state_dir):
        (state_dir / "bot_metrics.json").write_text(json.dumps({"bot-a": {"runs": [], "successes": 0, "failures": 0, "total_tokens": 100, "total_duration_s": 0}}))
        assert bm.get_token_burn_rate("bot-a") is None

    def test_average(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0, tokens_this_run=100)
        bm.record_bot_metric("bot-a", alive=False, exit_code=0, tokens_this_run=200)
        assert bm.get_token_burn_rate("bot-a") == pytest.approx(150.0)

    def test_single_run(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0, tokens_this_run=42)
        assert bm.get_token_burn_rate("bot-a") == pytest.approx(42.0)

    def test_zero_tokens(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0, tokens_this_run=0)
        assert bm.get_token_burn_rate("bot-a") == pytest.approx(0.0)


class TestAtomicWrite:
    def test_write_is_atomic(self, state_dir):
        bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        raw = (state_dir / "bot_metrics.json").read_text()
        data = json.loads(raw)
        assert "bot-a" in data

    def test_large_file_pruned(self, state_dir, monkeypatch):
        monkeypatch.setattr(bm, "_MAX_METRICS_FILE_BYTES", 200)
        for i in range(5):
            bm.record_bot_metric(f"bot-{i}", alive=False, exit_code=0, tokens_this_run=9999)
            for _ in range(20):
                bm.record_bot_metric(f"bot-{i}", alive=False, exit_code=0)
        raw = (state_dir / "bot_metrics.json").read_text()
        assert json.loads(raw) is not None

    def test_time_window_filters_runs(self, state_dir):
        now = 1_000_000.0
        with patch("codebot.bot_metrics.time.time", return_value=now):
            bm.record_bot_metric("bot-a", alive=False, exit_code=0)
        later = now + 8 * 86400
        with patch("codebot.bot_metrics.time.time", return_value=later):
            bm.record_bot_metric("bot-a", alive=False, exit_code=0)
            data = bm.get_bot_metrics("bot-a")
            assert len(data["runs"]) == 1
