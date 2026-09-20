"""Tests for codebot.stats_collector module.

Covers StatsCollector instantiation, record_call, get_stats,
persistence, edge cases, and error handling per TDD standards.
"""
import json
import time
from pathlib import Path

import pytest

from codebot.stats_collector import StatsCollector


class TestStatsCollectorInstantiation:
    """Test StatsCollector initialization and directory creation."""

    def test_creates_state_dir_if_missing(self, tmp_path):
        state_dir = tmp_path / "nested" / "state"
        collector = StatsCollector(state_dir=str(state_dir))
        assert state_dir.exists()
        assert collector.state_dir == state_dir

    def test_uses_existing_state_dir(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        assert collector.state_dir == state_dir

    def test_default_state_dir(self):
        # Just verify it doesn't raise; cleanup is caller's responsibility
        collector = StatsCollector()
        assert collector.state_dir == Path(".codebot/state")

    def test_stats_file_path(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        assert collector.stats_file == tmp_path / "model_stats.json"


class TestRecordCall:
    """Test recording API call outcomes."""

    def test_record_successful_call(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call(
            model_name="gpt-4",
            task_type="code_gen",
            success=True,
            cost=0.01,
            tokens_in=100,
            tokens_out=50
        )
        stats = collector.get_stats()
        key = "gpt-4:code_gen"
        assert key in stats
        assert stats[key]["total_calls"] == 1
        assert stats[key]["successful_calls"] == 1
        assert stats[key]["failed_calls"] == 0
        assert stats[key]["total_cost"] == pytest.approx(0.01)
        assert stats[key]["total_tokens_in"] == 100
        assert stats[key]["total_tokens_out"] == 50

    def test_record_failed_call(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call(
            model_name="claude-3",
            task_type="review",
            success=False,
            cost=0.005,
            tokens_in=200,
            tokens_out=0
        )
        stats = collector.get_stats()
        key = "claude-3:review"
        assert stats[key]["total_calls"] == 1
        assert stats[key]["successful_calls"] == 0
        assert stats[key]["failed_calls"] == 1

    def test_accumulates_multiple_calls(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        for i in range(3):
            collector.record_call(
                model_name="gemini",
                task_type="chat",
                success=(i % 2 == 0),
                cost=0.002,
                tokens_in=10,
                tokens_out=5
            )
        stats = collector.get_stats()
        key = "gemini:chat"
        assert stats[key]["total_calls"] == 3
        assert stats[key]["successful_calls"] == 2
        assert stats[key]["failed_calls"] == 1
        assert stats[key]["total_cost"] == pytest.approx(0.006)
        assert stats[key]["total_tokens_in"] == 30
        assert stats[key]["total_tokens_out"] == 15

    def test_last_updated_timestamp(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        before = time.time()
        collector.record_call("m", "t", True, 0.0, 0, 0)
        after = time.time()
        stats = collector.get_stats()
        ts = stats["m:t"]["last_updated"]
        assert before <= ts <= after

    def test_zero_cost_and_tokens(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call("m", "t", True, 0.0, 0, 0)
        stats = collector.get_stats()
        assert stats["m:t"]["total_cost"] == 0.0
        assert stats["m:t"]["total_tokens_in"] == 0
        assert stats["m:t"]["total_tokens_out"] == 0


class TestGetStats:
    """Test querying statistics with filters."""

    def _populate(self, collector):
        collector.record_call("gpt-4", "code", True, 0.01, 100, 50)
        collector.record_call("gpt-4", "review", False, 0.005, 80, 20)
        collector.record_call("claude", "code", True, 0.02, 120, 60)

    def test_get_all_stats(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        self._populate(collector)
        stats = collector.get_stats()
        assert len(stats) == 3

    def test_filter_by_model_name(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        self._populate(collector)
        stats = collector.get_stats(model_name="gpt-4")
        assert len(stats) == 2
        assert all(k.startswith("gpt-4:") for k in stats)

    def test_filter_by_task_type(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        self._populate(collector)
        stats = collector.get_stats(task_type="code")
        assert len(stats) == 2
        assert all(k.endswith(":code") for k in stats)

    def test_filter_by_both(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        self._populate(collector)
        stats = collector.get_stats(model_name="claude", task_type="code")
        assert len(stats) == 1
        assert "claude:code" in stats

    def test_filter_no_match(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        self._populate(collector)
        stats = collector.get_stats(model_name="nonexistent")
        assert stats == {}

    def test_empty_cache_returns_empty(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        assert collector.get_stats() == {}


class TestPersistence:
    """Test data persistence across restarts."""

    def test_data_persists_after_reload(self, tmp_path):
        c1 = StatsCollector(state_dir=str(tmp_path))
        c1.record_call("m1", "t1", True, 0.1, 500, 200)
        
        c2 = StatsCollector(state_dir=str(tmp_path))
        stats = c2.get_stats()
        assert "m1:t1" in stats
        assert stats["m1:t1"]["total_calls"] == 1
        assert stats["m1:t1"]["total_cost"] == pytest.approx(0.1)

    def test_corrupt_json_file_handled_gracefully(self, tmp_path):
        stats_file = tmp_path / "model_stats.json"
        stats_file.write_text("{invalid json", encoding="utf-8")
        collector = StatsCollector(state_dir=str(tmp_path))
        assert collector.get_stats() == {}
        # Should still be able to record new data
        collector.record_call("m", "t", True, 0.0, 0, 0)
        assert "m:t" in collector.get_stats()

    def test_non_dict_json_handled_gracefully(self, tmp_path):
        stats_file = tmp_path / "model_stats.json"
        stats_file.write_text('[1, 2, 3]', encoding="utf-8")
        collector = StatsCollector(state_dir=str(tmp_path))
        assert collector.get_stats() == {}

    def test_missing_file_starts_empty(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        assert collector.get_stats() == {}

    def test_atomic_write_creates_valid_json(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call("m", "t", True, 0.05, 10, 5)
        raw = (tmp_path / "model_stats.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        assert isinstance(data, dict)
        assert "m:t" in data


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_model_name_with_colon(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call("org:model:v2", "task", True, 0.0, 0, 0)
        stats = collector.get_stats(model_name="org:model:v2")
        assert len(stats) == 1

    def test_task_type_with_colon(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call("model", "sub:task:type", True, 0.0, 0, 0)
        stats = collector.get_stats(task_type="sub:task:type")
        assert len(stats) == 1

    def test_large_token_counts(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call("m", "t", True, 1.0, 1_000_000, 500_000)
        stats = collector.get_stats()
        assert stats["m:t"]["total_tokens_in"] == 1_000_000
        assert stats["m:t"]["total_tokens_out"] == 500_000

    def test_negative_cost_accepted(self, tmp_path):
        # Edge case: refund or credit scenario
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call("m", "t", True, -0.01, 0, 0)
        stats = collector.get_stats()
        assert stats["m:t"]["total_cost"] == pytest.approx(-0.01)

    def test_malformed_cache_key_skipped_in_get_stats(self, tmp_path):
        collector = StatsCollector(state_dir=str(tmp_path))
        # Inject malformed key directly into cache
        collector._cache["no_colon_key"] = {"total_calls": 1}
        collector.record_call("valid", "key", True, 0.0, 0, 0)
        stats = collector.get_stats()
        # Only valid key should appear
        assert "valid:key" in stats
        assert "no_colon_key" not in stats
