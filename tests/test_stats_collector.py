"""Tests for codebot.stats_collector module."""
import json
import time
from pathlib import Path

import pytest

from codebot.stats_collector import StatsCollector


class TestStatsCollectorInit:
    """Initialization and cache loading tests."""

    def test_creates_state_dir_if_missing(self, tmp_path):
        state_dir = tmp_path / "nested" / "state"
        StatsCollector(state_dir=str(state_dir))
        assert state_dir.exists()

    def test_empty_cache_on_missing_file(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        assert sc._cache == {}

    def test_loads_existing_valid_file(self, tmp_path):
        stats_file = tmp_path / "model_stats.json"
        data = {"gpt-4:code": {"total_calls": 5}}
        stats_file.write_text(json.dumps(data), encoding="utf-8")
        sc = StatsCollector(state_dir=str(tmp_path))
        assert sc._cache == data

    def test_handles_corrupted_json_gracefully(self, tmp_path):
        stats_file = tmp_path / "model_stats.json"
        stats_file.write_text("{invalid json!!!", encoding="utf-8")
        sc = StatsCollector(state_dir=str(tmp_path))
        assert sc._cache == {}

    def test_handles_non_dict_json_gracefully(self, tmp_path):
        stats_file = tmp_path / "model_stats.json"
        stats_file.write_text('["not", "a", "dict"]', encoding="utf-8")
        sc = StatsCollector(state_dir=str(tmp_path))
        assert sc._cache == {}


class TestRecordCall:
    """record_call accumulation tests."""

    def test_increments_counters_on_success(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        sc.record_call("gpt-4", "code", True, 0.05, 100, 50)
        key = "gpt-4:code"
        assert sc._cache[key]["total_calls"] == 1
        assert sc._cache[key]["successful_calls"] == 1
        assert sc._cache[key]["failed_calls"] == 0
        assert sc._cache[key]["total_cost"] == pytest.approx(0.05)
        assert sc._cache[key]["total_tokens_in"] == 100
        assert sc._cache[key]["total_tokens_out"] == 50

    def test_increments_failed_counter(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        sc.record_call("gpt-4", "code", False, 0.01, 10, 0)
        key = "gpt-4:code"
        assert sc._cache[key]["total_calls"] == 1
        assert sc._cache[key]["successful_calls"] == 0
        assert sc._cache[key]["failed_calls"] == 1

    def test_accumulates_multiple_calls(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        sc.record_call("gpt-4", "code", True, 0.05, 100, 50)
        sc.record_call("gpt-4", "code", False, 0.02, 80, 0)
        sc.record_call("gpt-4", "code", True, 0.03, 60, 30)
        key = "gpt-4:code"
        assert sc._cache[key]["total_calls"] == 3
        assert sc._cache[key]["successful_calls"] == 2
        assert sc._cache[key]["failed_calls"] == 1
        assert sc._cache[key]["total_cost"] == pytest.approx(0.10)
        assert sc._cache[key]["total_tokens_in"] == 240
        assert sc._cache[key]["total_tokens_out"] == 80

    def test_updates_last_updated_timestamp(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        before = time.time()
        sc.record_call("gpt-4", "code", True, 0.01, 10, 5)
        after = time.time()
        ts = sc._cache["gpt-4:code"]["last_updated"]
        assert before <= ts <= after

    def test_separate_keys_for_different_models(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        sc.record_call("gpt-4", "code", True, 0.05, 100, 50)
        sc.record_call("claude-3", "code", True, 0.03, 80, 40)
        assert len(sc._cache) == 2
        assert sc._cache["gpt-4:code"]["total_calls"] == 1
        assert sc._cache["claude-3:code"]["total_calls"] == 1

    def test_persists_after_record(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        sc.record_call("gpt-4", "code", True, 0.05, 100, 50)
        raw = (tmp_path / "model_stats.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        assert "gpt-4:code" in data
        assert data["gpt-4:code"]["total_calls"] == 1


class TestGetStats:
    """get_stats filtering tests."""

    @pytest.fixture()
    def populated_collector(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        sc.record_call("gpt-4", "code", True, 0.05, 100, 50)
        sc.record_call("gpt-4", "chat", True, 0.02, 40, 20)
        sc.record_call("claude-3", "code", False, 0.03, 80, 0)
        return sc

    def test_no_filter_returns_all(self, populated_collector):
        result = populated_collector.get_stats()
        assert len(result) == 3

    def test_filter_by_model_name(self, populated_collector):
        result = populated_collector.get_stats(model_name="gpt-4")
        assert len(result) == 2
        assert all(k.startswith("gpt-4:") for k in result)

    def test_filter_by_task_type(self, populated_collector):
        result = populated_collector.get_stats(task_type="code")
        assert len(result) == 2
        assert all(k.endswith(":code") for k in result)

    def test_filter_by_both(self, populated_collector):
        result = populated_collector.get_stats(model_name="gpt-4", task_type="code")
        assert len(result) == 1
        assert "gpt-4:code" in result

    def test_filter_no_match(self, populated_collector):
        result = populated_collector.get_stats(model_name="nonexistent")
        assert result == {}

    def test_skips_malformed_keys(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        sc._cache["no-colon-key"] = {"total_calls": 1}
        sc.record_call("gpt-4", "code", True, 0.01, 10, 5)
        result = sc.get_stats()
        assert "no-colon-key" not in result
        assert "gpt-4:code" in result


class TestSaveLoad:
    """Atomic persistence tests."""

    def test_save_writes_valid_json(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        sc._cache["test:key"] = {"total_calls": 1}
        sc._save()
        raw = (tmp_path / "model_stats.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        assert data["test:key"]["total_calls"] == 1

    def test_load_restores_cache(self, tmp_path):
        sc1 = StatsCollector(state_dir=str(tmp_path))
        sc1.record_call("gpt-4", "code", True, 0.05, 100, 50)
        sc2 = StatsCollector(state_dir=str(tmp_path))
        assert sc2._cache == sc1._cache

    def test_no_tmp_file_left_after_save(self, tmp_path):
        sc = StatsCollector(state_dir=str(tmp_path))
        sc.record_call("gpt-4", "code", True, 0.01, 10, 5)
        tmp_file = tmp_path / "model_stats.tmp"
        assert not tmp_file.exists()
