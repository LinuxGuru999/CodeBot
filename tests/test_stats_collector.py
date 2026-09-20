"""Tests for codebot.stats_collector module.

Covers:
- StatsCollector.record_call accumulation
- get_stats filtering by model_name and task_type
- _load/_save atomic persistence
- cache initialization from existing file
- graceful handling of corrupted JSON
"""
import json
import os
import time
from pathlib import Path

import pytest

from codebot.stats_collector import StatsCollector


class TestStatsCollectorInit:
    """Test StatsCollector initialization and cache loading."""

    def test_init_creates_state_dir(self, tmp_path):
        """State directory is created if it doesn't exist."""
        state_dir = tmp_path / "custom_state"
        assert not state_dir.exists()
        collector = StatsCollector(state_dir=str(state_dir))
        assert state_dir.exists()

    def test_init_loads_existing_stats(self, tmp_path):
        """Existing stats file is loaded into cache on init."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        existing_data = {
            "gpt-4:code_generation": {
                "total_calls": 5,
                "successful_calls": 4,
                "failed_calls": 1,
                "total_cost": 0.50,
                "total_tokens_in": 1000,
                "total_tokens_out": 2000,
                "last_updated": 1700000000.0
            }
        }
        stats_file.write_text(json.dumps(existing_data), encoding="utf-8")

        collector = StatsCollector(state_dir=str(state_dir))
        stats = collector.get_stats()
        assert "gpt-4:code_generation" in stats
        assert stats["gpt-4:code_generation"]["total_calls"] == 5

    def test_init_handles_missing_file(self, tmp_path):
        """Missing stats file results in empty cache."""
        state_dir = tmp_path / "state"
        collector = StatsCollector(state_dir=str(state_dir))
        stats = collector.get_stats()
        assert stats == {}

    def test_init_handles_corrupted_json(self, tmp_path):
        """Corrupted JSON file results in empty cache (graceful degradation)."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text("{invalid json content", encoding="utf-8")

        collector = StatsCollector(state_dir=str(state_dir))
        stats = collector.get_stats()
        assert stats == {}

    def test_init_handles_non_dict_json(self, tmp_path):
        """JSON that is not a dict results in empty cache."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text('["list", "not", "dict"]', encoding="utf-8")

        collector = StatsCollector(state_dir=str(state_dir))
        stats = collector.get_stats()
        assert stats == {}


class TestRecordCall:
    """Test StatsCollector.record_call behavior."""

    def test_record_call_creates_new_entry(self, tmp_path):
        """First call for a model:task creates entry with correct initial values."""
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call(
            model_name="gpt-4",
            task_type="code_generation",
            success=True,
            cost=0.05,
            tokens_in=100,
            tokens_out=200
        )
        stats = collector.get_stats()
        key = "gpt-4:code_generation"
        assert key in stats
        entry = stats[key]
        assert entry["total_calls"] == 1
        assert entry["successful_calls"] == 1
        assert entry["failed_calls"] == 0
        assert entry["total_cost"] == 0.05
        assert entry["total_tokens_in"] == 100
        assert entry["total_tokens_out"] == 200
        assert entry["last_updated"] > 0

    def test_record_call_accumulates_successfully(self, tmp_path):
        """Multiple calls accumulate counters correctly."""
        collector = StatsCollector(state_dir=str(tmp_path))
        # First call: success
        collector.record_call(
            model_name="gpt-4", task_type="code_generation",
            success=True, cost=0.05, tokens_in=100, tokens_out=200
        )
        # Second call: success
        collector.record_call(
            model_name="gpt-4", task_type="code_generation",
            success=True, cost=0.03, tokens_in=50, tokens_out=150
        )
        # Third call: failure
        collector.record_call(
            model_name="gpt-4", task_type="code_generation",
            success=False, cost=0.01, tokens_in=10, tokens_out=0
        )

        stats = collector.get_stats()
        entry = stats["gpt-4:code_generation"]
        assert entry["total_calls"] == 3
        assert entry["successful_calls"] == 2
        assert entry["failed_calls"] == 1
        assert entry["total_cost"] == pytest.approx(0.09)
        assert entry["total_tokens_in"] == 160
        assert entry["total_tokens_out"] == 350

    def test_record_call_different_models_separate_entries(self, tmp_path):
        """Different models create separate stat entries."""
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call(
            model_name="gpt-4", task_type="code_generation",
            success=True, cost=0.05, tokens_in=100, tokens_out=200
        )
        collector.record_call(
            model_name="claude-3", task_type="code_generation",
            success=True, cost=0.03, tokens_in=80, tokens_out=180
        )

        stats = collector.get_stats()
        assert "gpt-4:code_generation" in stats
        assert "claude-3:code_generation" in stats
        assert stats["gpt-4:code_generation"]["total_calls"] == 1
        assert stats["claude-3:code_generation"]["total_calls"] == 1

    def test_record_call_different_task_types_separate_entries(self, tmp_path):
        """Same model with different task types creates separate entries."""
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call(
            model_name="gpt-4", task_type="code_generation",
            success=True, cost=0.05, tokens_in=100, tokens_out=200
        )
        collector.record_call(
            model_name="gpt-4", task_type="summarization",
            success=True, cost=0.02, tokens_in=500, tokens_out=100
        )

        stats = collector.get_stats()
        assert "gpt-4:code_generation" in stats
        assert "gpt-4:summarization" in stats

    def test_record_call_persists_to_disk(self, tmp_path):
        """record_call writes valid JSON to disk."""
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call(
            model_name="gpt-4", task_type="code_generation",
            success=True, cost=0.05, tokens_in=100, tokens_out=200
        )

        stats_file = tmp_path / "model_stats.json"
        assert stats_file.exists()
        raw = stats_file.read_text(encoding="utf-8")
        data = json.loads(raw)  # Should not raise
        assert "gpt-4:code_generation" in data


class TestGetStats:
    """Test StatsCollector.get_stats filtering."""

    @pytest.fixture
    def populated_collector(self, tmp_path):
        """Create a collector with multiple entries."""
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call(
            model_name="gpt-4", task_type="code_generation",
            success=True, cost=0.05, tokens_in=100, tokens_out=200
        )
        collector.record_call(
            model_name="gpt-4", task_type="summarization",
            success=True, cost=0.02, tokens_in=500, tokens_out=100
        )
        collector.record_call(
            model_name="claude-3", task_type="code_generation",
            success=True, cost=0.03, tokens_in=80, tokens_out=180
        )
        return collector

    def test_get_stats_no_filter_returns_all(self, populated_collector):
        """No filters returns all entries."""
        stats = populated_collector.get_stats()
        assert len(stats) == 3
        assert "gpt-4:code_generation" in stats
        assert "gpt-4:summarization" in stats
        assert "claude-3:code_generation" in stats

    def test_get_stats_filter_by_model_name(self, populated_collector):
        """Filtering by model_name returns only matching entries."""
        stats = populated_collector.get_stats(model_name="gpt-4")
        assert len(stats) == 2
        assert "gpt-4:code_generation" in stats
        assert "gpt-4:summarization" in stats
        assert "claude-3:code_generation" not in stats

    def test_get_stats_filter_by_task_type(self, populated_collector):
        """Filtering by task_type returns only matching entries."""
        stats = populated_collector.get_stats(task_type="code_generation")
        assert len(stats) == 2
        assert "gpt-4:code_generation" in stats
        assert "claude-3:code_generation" in stats
        assert "gpt-4:summarization" not in stats

    def test_get_stats_filter_by_both(self, populated_collector):
        """Filtering by both model_name and task_type returns single entry."""
        stats = populated_collector.get_stats(
            model_name="gpt-4", task_type="code_generation"
        )
        assert len(stats) == 1
        assert "gpt-4:code_generation" in stats

    def test_get_stats_no_match_returns_empty(self, populated_collector):
        """Non-matching filter returns empty dict."""
        stats = populated_collector.get_stats(model_name="nonexistent")
        assert stats == {}


class TestPersistence:
    """Test _save and _load atomic persistence."""

    def test_save_writes_valid_json(self, tmp_path):
        """_save produces valid JSON that can be re-read."""
        collector = StatsCollector(state_dir=str(tmp_path))
        collector.record_call(
            model_name="gpt-4", task_type="code_generation",
            success=True, cost=0.05, tokens_in=100, tokens_out=200
        )

        stats_file = tmp_path / "model_stats.json"
        raw = stats_file.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert isinstance(data, dict)
        assert "gpt-4:code_generation" in data

    def test_load_after_restart_preserves_data(self, tmp_path):
        """Data persists across collector restarts."""
        # First session
        collector1 = StatsCollector(state_dir=str(tmp_path))
        collector1.record_call(
            model_name="gpt-4", task_type="code_generation",
            success=True, cost=0.05, tokens_in=100, tokens_out=200
        )

        # Second session (new instance)
        collector2 = StatsCollector(state_dir=str(tmp_path))
        stats = collector2.get_stats()
        assert "gpt-4:code_generation" in stats
        assert stats["gpt-4:code_generation"]["total_calls"] == 1

    def test_load_handles_os_error_gracefully(self, tmp_path):
        """If file cannot be read, cache remains empty."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        # Write invalid content
        stats_file.write_text("corrupted", encoding="utf-8")

        collector = StatsCollector(state_dir=str(state_dir))
        stats = collector.get_stats()
        assert stats == {}
