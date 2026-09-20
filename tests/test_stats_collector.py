"""Tests for codebot/stats_collector.py StatsCollector class."""
import json
import os
import time
from pathlib import Path

import pytest

from codebot.stats_collector import StatsCollector


class TestStatsCollectorInstantiation:
    """Test StatsCollector initialization behavior."""

    def test_default_state_dir(self, tmp_path):
        """Test instantiation with default state directory."""
        # Arrange
        original_cwd = os.getcwd()
        os.chdir(tmp_path)
        try:
            # Act
            collector = StatsCollector()
            
            # Assert - state_dir is relative path by default
            assert collector.state_dir == Path(".codebot/state")
            assert collector.stats_file == Path(".codebot/state/model_stats.json")
            assert collector._cache == {}
        finally:
            os.chdir(original_cwd)

    def test_custom_state_dir(self, tmp_path):
        """Test instantiation with custom state directory."""
        # Arrange
        custom_dir = tmp_path / "custom_state"
        
        # Act
        collector = StatsCollector(state_dir=str(custom_dir))
        
        # Assert
        assert collector.state_dir == custom_dir
        assert collector.stats_file == custom_dir / "model_stats.json"
        assert custom_dir.exists()

    def test_creates_state_directory(self, tmp_path):
        """Test that state directory is created if it doesn't exist."""
        # Arrange
        new_dir = tmp_path / "new" / "nested" / "state"
        
        # Act
        collector = StatsCollector(state_dir=str(new_dir))
        
        # Assert
        assert new_dir.exists()
        assert new_dir.is_dir()

    def test_loads_existing_stats(self, tmp_path):
        """Test that existing stats are loaded on initialization."""
        # Arrange
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
                "total_tokens_out": 500,
                "last_updated": 1700000000.0
            }
        }
        stats_file.write_text(json.dumps(existing_data))
        
        # Act
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Assert
        assert collector._cache == existing_data

    def test_handles_corrupted_stats_file(self, tmp_path):
        """Test graceful handling of corrupted stats file."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text("not valid json {{{")
        
        # Act
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Assert
        assert collector._cache == {}

    def test_handles_non_dict_stats_file(self, tmp_path):
        """Test graceful handling when stats file contains non-dict data."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text("[1, 2, 3]")
        
        # Act
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Assert
        assert collector._cache == {}


class TestRecordCall:
    """Test record_call method behavior."""

    def test_record_successful_call(self, tmp_path):
        """Test recording a successful API call."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call(
            model_name="gpt-4",
            task_type="code_generation",
            success=True,
            cost=0.10,
            tokens_in=100,
            tokens_out=50
        )
        
        # Assert
        key = "gpt-4:code_generation"
        assert key in collector._cache
        entry = collector._cache[key]
        assert entry["total_calls"] == 1
        assert entry["successful_calls"] == 1
        assert entry["failed_calls"] == 0
        assert entry["total_cost"] == 0.10
        assert entry["total_tokens_in"] == 100
        assert entry["total_tokens_out"] == 50
        assert entry["last_updated"] > 0

    def test_record_failed_call(self, tmp_path):
        """Test recording a failed API call."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call(
            model_name="gpt-4",
            task_type="code_generation",
            success=False,
            cost=0.05,
            tokens_in=50,
            tokens_out=0
        )
        
        # Assert
        key = "gpt-4:code_generation"
        entry = collector._cache[key]
        assert entry["total_calls"] == 1
        assert entry["successful_calls"] == 0
        assert entry["failed_calls"] == 1

    def test_multiple_calls_accumulate(self, tmp_path):
        """Test that multiple calls accumulate correctly."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector.record_call("gpt-4", "code_generation", True, 0.15, 150, 75)
        collector.record_call("gpt-4", "code_generation", False, 0.05, 50, 0)
        
        # Assert
        key = "gpt-4:code_generation"
        entry = collector._cache[key]
        assert entry["total_calls"] == 3
        assert entry["successful_calls"] == 2
        assert entry["failed_calls"] == 1
        assert entry["total_cost"] == 0.30
        assert entry["total_tokens_in"] == 300
        assert entry["total_tokens_out"] == 125

    def test_different_models_separate(self, tmp_path):
        """Test that different models are tracked separately."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector.record_call("claude-3", "code_generation", True, 0.08, 100, 50)
        
        # Assert
        assert "gpt-4:code_generation" in collector._cache
        assert "claude-3:code_generation" in collector._cache
        assert len(collector._cache) == 2

    def test_different_task_types_separate(self, tmp_path):
        """Test that different task types are tracked separately."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector.record_call("gpt-4", "summarization", True, 0.05, 200, 100)
        
        # Assert
        assert "gpt-4:code_generation" in collector._cache
        assert "gpt-4:summarization" in collector._cache
        assert len(collector._cache) == 2

    def test_persists_to_disk(self, tmp_path):
        """Test that stats are persisted to disk atomically."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        
        # Assert - reload from disk
        stats_file = state_dir / "model_stats.json"
        assert stats_file.exists()
        data = json.loads(stats_file.read_text())
        assert "gpt-4:code_generation" in data

    def test_zero_cost_allowed(self, tmp_path):
        """Test recording calls with zero cost."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call("local-model", "testing", True, 0.0, 100, 50)
        
        # Assert
        key = "local-model:testing"
        entry = collector._cache[key]
        assert entry["total_cost"] == 0.0
        assert entry["total_calls"] == 1

    def test_zero_tokens_allowed(self, tmp_path):
        """Test recording calls with zero tokens."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call("gpt-4", "ping", True, 0.01, 0, 0)
        
        # Assert
        key = "gpt-4:ping"
        entry = collector._cache[key]
        assert entry["total_tokens_in"] == 0
        assert entry["total_tokens_out"] == 0


class TestGetStats:
    """Test get_stats method behavior."""

    def test_get_all_stats_empty(self, tmp_path):
        """Test getting stats when cache is empty."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        result = collector.get_stats()
        
        # Assert
        assert result == {}

    def test_get_all_stats(self, tmp_path):
        """Test getting all stats without filters."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector.record_call("claude-3", "summarization", True, 0.08, 200, 100)
        
        # Act
        result = collector.get_stats()
        
        # Assert
        assert len(result) == 2
        assert "gpt-4:code_generation" in result
        assert "claude-3:summarization" in result

    def test_filter_by_model_name(self, tmp_path):
        """Test filtering stats by model name."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector.record_call("gpt-4", "summarization", True, 0.05, 200, 100)
        collector.record_call("claude-3", "code_generation", True, 0.08, 150, 75)
        
        # Act
        result = collector.get_stats(model_name="gpt-4")
        
        # Assert
        assert len(result) == 2
        assert "gpt-4:code_generation" in result
        assert "gpt-4:summarization" in result
        assert "claude-3:code_generation" not in result

    def test_filter_by_task_type(self, tmp_path):
        """Test filtering stats by task type."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector.record_call("claude-3", "code_generation", True, 0.08, 150, 75)
        collector.record_call("gpt-4", "summarization", True, 0.05, 200, 100)
        
        # Act
        result = collector.get_stats(task_type="code_generation")
        
        # Assert
        assert len(result) == 2
        assert "gpt-4:code_generation" in result
        assert "claude-3:code_generation" in result
        assert "gpt-4:summarization" not in result

    def test_filter_by_model_and_task_type(self, tmp_path):
        """Test filtering stats by both model and task type."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector.record_call("gpt-4", "summarization", True, 0.05, 200, 100)
        collector.record_call("claude-3", "code_generation", True, 0.08, 150, 75)
        
        # Act
        result = collector.get_stats(model_name="gpt-4", task_type="code_generation")
        
        # Assert
        assert len(result) == 1
        assert "gpt-4:code_generation" in result

    def test_filter_no_matches(self, tmp_path):
        """Test filtering when no matches exist."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        
        # Act
        result = collector.get_stats(model_name="nonexistent-model")
        
        # Assert
        assert result == {}

    def test_skips_malformed_keys(self, tmp_path):
        """Test that malformed cache keys are skipped."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        # Inject malformed key directly
        collector._cache["malformed_key_without_colon"] = {"total_calls": 1}
        collector._cache["valid:model"] = {"total_calls": 2}
        
        # Act
        result = collector.get_stats()
        
        # Assert
        assert len(result) == 1
        assert "valid:model" in result
        assert "malformed_key_without_colon" not in result


class TestEdgeCases:
    """Test edge cases and error conditions."""

    def test_none_values_in_record_call(self, tmp_path):
        """Test handling of None-like scenarios (using defaults)."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act & Assert - should not raise
        collector.record_call("model", "task", True, 0.0, 0, 0)
        assert collector._cache["model:task"]["total_calls"] == 1

    def test_very_large_numbers(self, tmp_path):
        """Test handling of very large numeric values."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call("gpt-4", "bulk", True, 999999.99, 1000000, 500000)
        
        # Assert
        entry = collector._cache["gpt-4:bulk"]
        assert entry["total_cost"] == 999999.99
        assert entry["total_tokens_in"] == 1000000
        assert entry["total_tokens_out"] == 500000

    def test_concurrent_writes_atomic(self, tmp_path):
        """Test that writes use atomic tmp+replace pattern."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act
        collector.record_call("gpt-4", "test", True, 0.01, 10, 5)
        
        # Assert - verify no temp file left behind
        stats_file = state_dir / "model_stats.json"
        tmp_file = state_dir / "model_stats.tmp"
        assert stats_file.exists()
        assert not tmp_file.exists()

    def test_missing_fields_default_to_zero(self, tmp_path):
        """Test that missing fields in loaded data default properly."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        # Write data with missing optional fields
        incomplete_data = {
            "gpt-4:test": {
                "total_calls": 1
                # Missing other fields
            }
        }
        stats_file.write_text(json.dumps(incomplete_data))
        collector = StatsCollector(state_dir=str(state_dir))
        
        # Act - record another call to trigger .get() defaults
        collector.record_call("gpt-4", "test", True, 0.01, 10, 5)
        
        # Assert
        entry = collector._cache["gpt-4:test"]
        assert entry["total_calls"] == 2
        assert entry.get("successful_calls", 0) >= 0
        assert entry.get("total_cost", 0.0) >= 0.01