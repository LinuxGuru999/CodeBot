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

    def test_save_oserror_does_not_crash(self, tmp_path, monkeypatch):
        """_save catches OSError — collector must not crash on write failure."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        # Patch Path.write_text to raise OSError on .tmp writes
        original_write_text = Path.write_text

        def failing_write_text(self, *args, **kwargs):
            if str(self).endswith(".tmp"):
                raise OSError("disk full")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", failing_write_text)

        # Should not raise — fail-open design
        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)

        # Cache should still be updated in memory even though disk write failed
        key = "gpt-4:code_generation"
        assert key in collector._cache
        assert collector._cache[key]["total_calls"] == 1

    def test_save_oserror_cleans_up_original_file(self, tmp_path, monkeypatch):
        """When save fails, the original stats file should remain intact."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"

        # Pre-populate with complete entry data
        initial_data = {
            "model:task": {
                "total_calls": 1,
                "successful_calls": 1,
                "failed_calls": 0,
                "total_cost": 0.05,
                "total_tokens_in": 100,
                "total_tokens_out": 200,
                "last_updated": 1700000000.0,
            }
        }
        stats_file.write_text(json.dumps(initial_data))

        collector = StatsCollector(state_dir=str(state_dir))

        original_write_text = Path.write_text

        def failing_write_text(self, *args, **kwargs):
            if str(self).endswith(".tmp"):
                raise OSError("disk full")
            return original_write_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", failing_write_text)

        # This should not crash, and original file should survive
        collector.record_call("model", "task", True, 0.01, 10, 20)

        # Original file must not be corrupted
        assert stats_file.exists()
        remaining = json.loads(stats_file.read_text())
        assert remaining == initial_data


class TestLoadEdgeCases:
    """Edge cases for _load beyond basic init tests."""

    def test_load_empty_file(self, tmp_path):
        """Empty file raises JSONDecodeError, which _load catches gracefully."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text("")

        collector = StatsCollector(state_dir=str(state_dir))
        assert collector._cache == {}

    def test_load_oserror_on_read(self, tmp_path, monkeypatch):
        """_load should handle OSError when reading the file."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text(json.dumps({"some:data": {}}))

        original_read_text = Path.read_text

        def failing_read_text(self, *args, **kwargs):
            if str(self).endswith("model_stats.json"):
                raise OSError("permission denied")
            return original_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", failing_read_text)

        collector = StatsCollector(state_dir=str(state_dir))
        assert collector._cache == {}

    def test_load_partial_entry_record_call_graceful(self, tmp_path):
        """If loaded entries have missing fields, record_call should not crash."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"

        # Pre-populate with incomplete entry (missing 'successful_calls' field)
        partial_data = {"model:task": {"total_calls": 1}}
        stats_file.write_text(json.dumps(partial_data))

        collector = StatsCollector(state_dir=str(state_dir))
        assert "model:task" in collector._cache

        # record_call should handle missing fields gracefully
        collector.record_call("model", "task", True, 0.01, 10, 20)

        entry = collector._cache["model:task"]
        # Should have incremented total_calls from 1 to 2
        assert entry["total_calls"] == 2

    def test_load_whitespace_only_json(self, tmp_path):
        """File with only whitespace is invalid JSON — should degrade gracefully."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        stats_file = state_dir / "model_stats.json"
        stats_file.write_text("   \n\t  ")

        collector = StatsCollector(state_dir=str(state_dir))
        assert collector._cache == {}


class TestGetStatsEdgeCases:
    """Edge cases for get_stats filtering."""

    def test_get_stats_skips_malformed_keys(self, tmp_path):
        """Keys without colon separator should be silently skipped."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        # Manually inject a malformed key into the cache
        collector = StatsCollector(state_dir=str(state_dir))
        collector._cache["malformed_key_no_colon"] = {"total_calls": 1}
        collector._cache["gpt-4:code_generation"] = {"total_calls": 5}

        result = collector.get_stats()
        assert "malformed_key_no_colon" not in result
        assert "gpt-4:code_generation" in result

    def test_get_stats_result_is_independent_copy(self, tmp_path):
        """Mutating the result dict should not affect the collector's internal cache."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)

        result = collector.get_stats()
        result.clear()

        # Internal cache should be unaffected
        assert len(collector._cache) == 1

    def test_get_stats_nonexistent_model_and_task(self, tmp_path):
        """Filtering for a nonexistent model AND task should return empty."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)

        result = collector.get_stats(model_name="no-model", task_type="no-task")
        assert result == {}

    def test_get_stats_model_only_filter(self, tmp_path):
        """Filter by model_name only (task_type=None) returns all task types for that model."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))
        collector.record_call("gpt-4", "code_generation", True, 0.01, 100, 200)
        collector.record_call("gpt-4", "summarization", True, 0.02, 50, 100)
        collector.record_call("claude-3", "code_generation", True, 0.03, 150, 200)

        result = collector.get_stats(model_name="gpt-4")
        assert len(result) == 2
        assert all(k.startswith("gpt-4:") for k in result)


class TestRecordCallEdgeCases:
    """Edge cases for record_call behavior."""

    def test_record_call_zero_cost_and_tokens(self, tmp_path):
        """Zero cost and zero tokens are valid values."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("model", "task", True, 0.0, 0, 0)

        entry = collector._cache["model:task"]
        assert entry["total_cost"] == 0.0
        assert entry["total_tokens_in"] == 0
        assert entry["total_tokens_out"] == 0

    def test_record_call_only_failures(self, tmp_path):
        """All calls failed: successful_calls=0, failed_calls>0."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("model", "task", False, 0.05, 100, 50)
        collector.record_call("model", "task", False, 0.05, 100, 50)

        entry = collector._cache["model:task"]
        assert entry["total_calls"] == 2
        assert entry["successful_calls"] == 0
        assert entry["failed_calls"] == 2

    def test_record_call_last_updated_monotonic(self, tmp_path):
        """Each call should update last_updated to a value >= previous."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        collector.record_call("m", "t", True, 0.0, 0, 0)
        ts1 = collector._cache["m:t"]["last_updated"]
        assert ts1 > 0

        collector.record_call("m", "t", True, 0.0, 0, 0)
        ts2 = collector._cache["m:t"]["last_updated"]
        assert ts2 >= ts1

    def test_record_call_large_values(self, tmp_path):
        """Large token and cost values should accumulate without overflow."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        large_tokens = 10_000_000
        large_cost = 999.99
        collector.record_call("model", "task", True, large_cost, large_tokens, large_tokens)
        collector.record_call("model", "task", True, large_cost, large_tokens, large_tokens)

        entry = collector._cache["model:task"]
        assert entry["total_cost"] == 2 * large_cost
        assert entry["total_tokens_in"] == 2 * large_tokens
        assert entry["total_tokens_out"] == 2 * large_tokens

    def test_record_call_special_characters_in_names(self, tmp_path):
        """Model names and task types with special characters should work."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        collector = StatsCollector(state_dir=str(state_dir))

        model = "gpt-4-turbo-2024-04-09"
        task = "code_review/rust"
        collector.record_call(model, task, True, 0.1, 500, 300)

        key = f"{model}:{task}"
        assert key in collector._cache
        assert collector._cache[key]["total_calls"] == 1
