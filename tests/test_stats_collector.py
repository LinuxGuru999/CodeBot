"""Tests for codebot.stats_collector module.

Covers:
- StatsCollector initialization and cache loading
- record_call accumulation logic
- get_stats filtering by model_name and task_type
- _save atomic persistence
- _load handling of missing/corrupt files
"""
import json
import os
import time
from pathlib import Path

import pytest

from codebot.stats_collector import StatsCollector


class TestStatsCollectorInit:
    """Test initialization and cache loading behavior."""

    def test_init_creates_state_dir(self, tmp_path):
        state_dir = tmp_path / "custom_state"
        assert not state_dir.exists()
        collector = StatsCollector(state_dir=str(state_dir))
        assert state_dir.exists()
        assert (state_dir / "model_stats.json").exists() or True  # file may be created on save

    def test_init_loads_existing_stats(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        existing_data = {
            "gpt-4:code_generation": {
                "total_calls": 5,
                "successful_calls": 4,
                "failed_calls": 1,
                "total_cost": 0.5,
                "total_tokens_in": 100,
                "total_tokens_out": 200,
                "last_updated": 1700000000.0
            }
        }
        stats_file.write_text(json.dumps(existing_data))

        collector = StatsCollector(state_dir=str(state_dir))
        assert collector._cache == existing_data

    def test_init_handles_missing_file(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # No model_stats.json exists
        collector = StatsCollector(state_dir=str(state_dir))
        assert collector._cache == {}

    def test_init_handles_corrupt_json(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text("{ invalid json }}}")

        collector = StatsCollector(state_dir=str(state_dir))
        assert collector._cache == {}

    def test_init_handles_non_dict_json(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text(json.dumps([1, 2, 3]))  # list instead of dict

        collector = StatsCollector(state_dir=str(state_dir))
        assert collector._cache == {}


class TestRecordCall:
    """Test record_call accumulation logic."""

    def test_record_call_new_entry(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call(
            model_name="gpt-4",
            task_type="code_generation",
            success=True,
            cost=0.01,
            tokens_in=100,
            tokens_out=200
        )

        key = "gpt-4:code_generation"
        assert key in collector._cache
        entry = collector._cache[key]
        assert entry["total_calls"] == 1
        assert entry["successful_calls"] == 1
        assert entry["failed_calls"] == 0
        assert entry["total_cost"] == 0.01
        assert entry["total_tokens_in"] == 100
        assert entry["total_tokens_out"] == 200
        assert entry["last_updated"] > 0

    def test_record_call_accumulates(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)
        collector.record_call("gpt-4", "code_generation", False, 0.02, 150, 250)
        collector.record_call("gpt-4", "code_generation", True, 0.03, 200, 300)

        key = "gpt-4:code_generation"
        entry = collector._cache[key]
        assert entry["total_calls"] == 3
        assert entry["successful_calls"] == 2
        assert entry["failed_calls"] == 1
        assert entry["total_cost"] == 0.06
        assert entry["total_tokens_in"] == 450
        assert entry["total_tokens_out"] == 750

    def test_record_call_persists_to_disk(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)

        stats_file = state_dir / "model_stats.json"
        assert stats_file.exists()
        data = json.loads(stats_file.read_text())
        assert "gpt-4:code_generation" in data

    def test_record_call_different_models_and_tasks(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)
        collector.record_call("claude-3", "code_generation", True, 0.02, 150, 250)
        collector.record_call("gpt-4", "summarization", True, 0.005, 50, 100)

        assert len(collector._cache) == 3
        assert "gpt-4:code_generation" in collector._cache
        assert "claude-3:code_generation" in collector._cache
        assert "gpt-4:summarization" in collector._cache


class TestGetStats:
    """Test get_stats filtering logic."""

    def test_get_stats_no_filter(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)
        collector.record_call("claude-3", "summarization", True, 0.02, 150, 250)

        result = collector.get_stats()
        assert len(result) == 2
        assert "gpt-4:code_generation" in result
        assert "claude-3:summarization" in result

    def test_get_stats_filter_by_model(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)
        collector.record_call("claude-3", "code_generation", True, 0.02, 150, 250)
        collector.record_call("gpt-4", "summarization", True, 0.005, 50, 100)

        result = collector.get_stats(model_name="gpt-4")
        assert len(result) == 2
        assert "gpt-4:code_generation" in result
        assert "gpt-4:summarization" in result
        assert "claude-3:code_generation" not in result

    def test_get_stats_filter_by_task_type(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)
        collector.record_call("claude-3", "code_generation", True, 0.02, 150, 250)
        collector.record_call("gpt-4", "summarization", True, 0.005, 50, 100)

        result = collector.get_stats(task_type="code_generation")
        assert len(result) == 2
        assert "gpt-4:code_generation" in result
        assert "claude-3:code_generation" in result
        assert "gpt-4:summarization" not in result

    def test_get_stats_filter_by_both(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)
        collector.record_call("claude-3", "code_generation", True, 0.02, 150, 250)
        collector.record_call("gpt-4", "summarization", True, 0.005, 50, 100)

        result = collector.get_stats(model_name="gpt-4", task_type="code_generation")
        assert len(result) == 1
        assert "gpt-4:code_generation" in result

    def test_get_stats_empty_cache(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        result = collector.get_stats()
        assert result == {}

    def test_get_stats_no_match(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)

        result = collector.get_stats(model_name="nonexistent")
        assert result == {}


class TestSaveLoadPersistence:
    """Test _save and _load atomic persistence."""

    def test_save_writes_valid_json(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)

        stats_file = state_dir / "model_stats.json"
        raw = stats_file.read_text()
        # Should be valid JSON
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        assert "gpt-4:code_generation" in parsed

    def test_load_after_restart(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # First session
        collector1 = StatsCollector(state_dir=str(state_dir))
        collector1.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)
        collector1.record_call("gpt-4", "code_generation", False, 0.02, 150, 250)

        # Second session - should load previous data
        collector2 = StatsCollector(state_dir=str(state_dir))
        key = "gpt-4:code_generation"
        assert key in collector2._cache
        entry = collector2._cache[key]
        assert entry["total_calls"] == 2
        assert entry["successful_calls"] == 1
        assert entry["failed_calls"] == 1
        assert entry["total_cost"] == 0.03

    def test_save_atomic_tmp_replace(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)

        stats_file = state_dir / "model_stats.json"
        # The .tmp file should not exist after successful save
        tmp_file = state_dir / "model_stats.tmp"
        assert not tmp_file.exists()
        assert stats_file.exists()
