"""Tests for codebot/stats_collector.py StatsCollector class.

Covers: instantiation, persistence, record_call, get_stats,
edge cases (empty data, malformed files, colons in keys, OSError on save),
and isolation between instances.
"""
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
                "last_updated": 1700000000.0,
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

    def test_handles_empty_stats_file(self, tmp_path):
        """Test graceful handling of an empty stats file."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text("")

        # Act
        collector = StatsCollector(state_dir=str(state_dir))

        # Assert - empty string causes JSONDecodeError, cache should be empty
        assert collector._cache == {}

    def test_handles_missing_stats_file(self, tmp_path):
        """Test that missing stats file starts with empty cache."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        # No stats file created

        # Act
        collector = StatsCollector(state_dir=str(state_dir))

        # Assert
        assert collector._cache == {}

    def test_handles_stats_file_is_symlink_to_missing(self, tmp_path):
        """Test handling of dangling symlink as stats file."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.symlink_to(tmp_path / "nonexistent.json")

        # Act
        collector = StatsCollector(state_dir=str(state_dir))

        # Assert - symlink to missing file triggers OSError in read_text
        assert collector._cache == {}

    def test_stats_file_not_created_until_first_record(self, tmp_path):
        """Test that stats file is not created until first record_call."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Act
        collector = StatsCollector(state_dir=str(state_dir))

        # Assert - no file yet
        assert not (state_dir / "model_stats.json").exists()


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
            tokens_out=50,
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
            tokens_out=0,
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
        assert entry["total_cost"] == pytest.approx(0.30)
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

    def test_last_updated_is_set(self, tmp_path):
        """Test that last_updated is set to a reasonable timestamp."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        before = time.time()

        # Act
        collector.record_call("gpt-4", "test", True, 0.01, 10, 5)
        after = time.time()

        # Assert
        entry = collector._cache["gpt-4:test"]
        assert before <= entry["last_updated"] <= after

    def test_last_updated_monotonically_increases(self, tmp_path):
        """Test that last_updated increases across sequential calls."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act
        collector.record_call("gpt-4", "test", True, 0.01, 10, 5)
        first_update = collector._cache["gpt-4:test"]["last_updated"]
        collector.record_call("gpt-4", "test", True, 0.02, 20, 10)
        second_update = collector._cache["gpt-4:test"]["last_updated"]

        # Assert
        assert second_update >= first_update

    def test_no_temp_file_left_after_save(self, tmp_path):
        """Test that no temporary file is left behind after atomic save."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act
        collector.record_call("gpt-4", "test", True, 0.01, 10, 5)

        # Assert
        assert (state_dir / "model_stats.json").exists()
        assert not (state_dir / "model_stats.tmp").exists()


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

    def test_get_stats_returns_matching_entries(self, tmp_path):
        """Test that get_stats returns the actual stat entry data."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)

        # Act
        result = collector.get_stats(model_name="gpt-4")

        # Assert - verify all expected fields are present in result
        entry = result["gpt-4:code_generation"]
        assert entry["total_calls"] == 1
        assert entry["successful_calls"] == 1
        assert entry["failed_calls"] == 0
        assert entry["total_cost"] == 0.10
        assert entry["total_tokens_in"] == 100
        assert entry["total_tokens_out"] == 50
        assert "last_updated" in entry

    def test_filter_by_empty_model_name(self, tmp_path):
        """Test filtering by model_name that matches no entries."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)

        # Act
        result = collector.get_stats(model_name="")

        # Assert
        assert result == {}

    def test_filter_by_empty_task_type(self, tmp_path):
        """Test filtering by task_type that matches no entries."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)

        # Act
        result = collector.get_stats(task_type="")

        # Assert
        assert result == {}


class TestPersistenceRoundtrip:
    """Test persistence: write, destroy, reload, verify."""

    def test_persistence_across_instances(self, tmp_path):
        """Test that data survives creating a new StatsCollector instance."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Act - first instance writes data
        collector1 = StatsCollector(state_dir=str(state_dir))
        collector1.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector1.record_call("gpt-4", "code_generation", False, 0.05, 50, 0)
        del collector1  # Force cleanup

        # Assert - second instance loads data
        collector2 = StatsCollector(state_dir=str(state_dir))
        result = collector2.get_stats()
        assert len(result) == 1
        entry = result["gpt-4:code_generation"]
        assert entry["total_calls"] == 2
        assert entry["successful_calls"] == 1
        assert entry["failed_calls"] == 1
        assert entry["total_cost"] == pytest.approx(0.15)
        assert entry["total_tokens_in"] == 150
        assert entry["total_tokens_out"] == 50

    def test_accumulation_across_instances(self, tmp_path):
        """Test that new instance accumulates on top of loaded data."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Act - first instance writes
        collector1 = StatsCollector(state_dir=str(state_dir))
        collector1.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)

        # Act - second instance adds more
        collector2 = StatsCollector(state_dir=str(state_dir))
        collector2.record_call("gpt-4", "code_generation", True, 0.20, 200, 100)

        # Assert
        entry = collector2._cache["gpt-4:code_generation"]
        assert entry["total_calls"] == 2
        assert entry["total_cost"] == pytest.approx(0.30)
        assert entry["total_tokens_in"] == 300

    def test_multiple_models_persist(self, tmp_path):
        """Test that multiple models persist and load correctly."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Act
        collector1 = StatsCollector(state_dir=str(state_dir))
        collector1.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector1.record_call("claude-3", "summarization", True, 0.08, 200, 100)
        collector1.record_call("local-llama", "testing", True, 0.0, 50, 25)

        # Reload
        collector2 = StatsCollector(state_dir=str(state_dir))
        result = collector2.get_stats()

        # Assert
        assert len(result) == 3
        assert "gpt-4:code_generation" in result
        assert "claude-3:summarization" in result
        assert "local-llama:testing" in result

    def test_json_file_content_is_valid_json(self, tmp_path):
        """Test that the persisted file is valid JSON and reloadable."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)

        # Assert - read file and re-parse
        stats_file = state_dir / "model_stats.json"
        raw = stats_file.read_text()
        parsed = json.loads(raw)
        assert isinstance(parsed, dict)
        assert "gpt-4:code_generation" in parsed


class TestSaveOSErrorPath:
    """Test _save OSError handling (fail-open behavior)."""

    def test_record_call_does_not_crash_on_save_failure(self, tmp_path):
        """Test that record_call succeeds in memory even when disk save fails."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Pre-populate cache with valid data
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)

        # Make stats file a directory so save will fail (OSError on replace)
        stats_file = state_dir / "model_stats.json"
        stats_file.unlink()
        stats_file.mkdir()

        # Act - should not raise even though save fails
        collector.record_call("gpt-4", "code_generation", True, 0.20, 200, 100)

        # Assert - in-memory cache is updated despite save failure
        entry = collector._cache["gpt-4:code_generation"]
        assert entry["total_calls"] == 2
        assert entry["total_cost"] == pytest.approx(0.30)


class TestEdgeCases:
    """Test edge cases and error conditions."""

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

    def test_missing_fields_default_to_zero(self, tmp_path):
        """Test that missing fields in loaded data default properly."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        # Write data with missing optional fields
        incomplete_data = {"gpt-4:test": {"total_calls": 1}}
        stats_file.write_text(json.dumps(incomplete_data))
        collector = StatsCollector(state_dir=str(state_dir))

        # Act - record another call to trigger .get() defaults
        collector.record_call("gpt-4", "test", True, 0.01, 10, 5)

        # Assert
        entry = collector._cache["gpt-4:test"]
        assert entry["total_calls"] == 2
        assert entry.get("successful_calls", 0) >= 0
        assert entry.get("total_cost", 0.0) >= 0.01

    def test_model_name_with_colon_characters(self, tmp_path):
        """Test model names containing colon (split on ':', 1)."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act - model name with additional colons
        collector.record_call("openai:gpt-4:latest", "code_generation", True, 0.10, 100, 50)

        # Assert - key uses model:task format; split(':', 1) preserves extra colons in task
        # The key is "openai:gpt-4:latest:code_generation"
        # When split on ":", 1 → model="openai", task="gpt-4:latest:code_generation"
        result = collector.get_stats(model_name="openai")
        assert len(result) == 1
        # The actual key stored
        all_keys = list(collector._cache.keys())
        assert len(all_keys) == 1
        key = all_keys[0]
        assert key.startswith("openai:")
        assert "code_generation" in key

    def test_model_name_with_trailing_colon(self, tmp_path):
        """Test model name ending with colon (empty task_type in key)."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act - model name with trailing colon gives key "m:task"
        collector.record_call("m", "task", True, 0.01, 10, 5)

        # Assert
        assert "m:task" in collector._cache

    def test_negative_cost_accumulates(self, tmp_path):
        """Test that negative cost values are recorded (no validation)."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act
        collector.record_call("gpt-4", "refund", True, -0.10, 100, 50)

        # Assert - code doesn't validate, so negative is stored
        entry = collector._cache["gpt-4:refund"]
        assert entry["total_cost"] == -0.10

    def test_negative_tokens_accumulate(self, tmp_path):
        """Test that negative token values are recorded (no validation)."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act
        collector.record_call("gpt-4", "test", True, 0.01, -10, -5)

        # Assert - code doesn't validate, so negative is stored
        entry = collector._cache["gpt-4:test"]
        assert entry["total_tokens_in"] == -10
        assert entry["total_tokens_out"] == -5

    def test_many_distinct_keys(self, tmp_path):
        """Test performance with many distinct model:task combinations."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act - record 100 distinct combinations
        for i in range(100):
            collector.record_call(f"model-{i}", f"task-{i}", True, 0.01, 10, 5)

        # Assert
        assert len(collector._cache) == 100
        result = collector.get_stats()
        assert len(result) == 100

    def test_get_stats_model_filter_exact_match(self, tmp_path):
        """Test that model filter requires exact match (not substring)."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4-turbo", "code_generation", True, 0.10, 100, 50)
        collector.record_call("gpt-4", "code_generation", True, 0.08, 100, 50)

        # Act
        result = collector.get_stats(model_name="gpt-4")

        # Assert - only exact match
        assert len(result) == 1
        assert "gpt-4:code_generation" in result
        assert "gpt-4-turbo:code_generation" not in result

    def test_get_stats_task_filter_exact_match(self, tmp_path):
        """Test that task_type filter requires exact match (not substring)."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector.record_call("gpt-4", "code_review", True, 0.08, 100, 50)

        # Act
        result = collector.get_stats(task_type="code")

        # Assert - no substring match
        assert len(result) == 0

    def test_empty_string_model_name(self, tmp_path):
        """Test recording with empty string model name."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act
        collector.record_call("", "task", True, 0.01, 10, 5)

        # Assert - key is ":task"
        assert ":task" in collector._cache

    def test_empty_string_task_type(self, tmp_path):
        """Test recording with empty string task type."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act
        collector.record_call("model", "", True, 0.01, 10, 5)

        # Assert - key is "model:"
        assert "model:" in collector._cache

    def test_both_empty_strings(self, tmp_path):
        """Test recording with both empty model and task type."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act
        collector.record_call("", "", True, 0.01, 10, 5)

        # Assert - key is ":"
        assert ":" in collector._cache
        result = collector.get_stats()
        assert ":" in result

    def test_cost_accumulates_float_precision(self, tmp_path):
        """Test floating point cost accumulation."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act - accumulate floating point values
        for _ in range(3):
            collector.record_call("gpt-4", "test", True, 0.1, 10, 5)

        # Assert
        entry = collector._cache["gpt-4:test"]
        assert entry["total_cost"] == pytest.approx(0.3)

    def test_interleaved_success_and_failure(self, tmp_path):
        """Test alternating success and failure calls."""
        # Arrange
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Act
        collector.record_call("gpt-4", "code_generation", True, 0.10, 100, 50)
        collector.record_call("gpt-4", "code_generation", False, 0.05, 50, 0)
        collector.record_call("gpt-4", "code_generation", True, 0.12, 120, 60)
        collector.record_call("gpt-4", "code_generation", False, 0.08, 80, 0)

        # Assert
        entry = collector._cache["gpt-4:code_generation"]
        assert entry["total_calls"] == 4
        assert entry["successful_calls"] == 2
        assert entry["failed_calls"] == 2
        assert entry["total_cost"] == pytest.approx(0.35)
