"""Tests for cost_tracker.py."""
import json
import os
from unittest.mock import patch

import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.cost_tracker import CostTracker, TicketCost


class TestRecordPhaseCost:
    def test_record_creates_entry(self, tmp_path):
        ct = CostTracker(tmp_path)
        entry = ct.record_phase_cost("CB-1", "worker-1", "qwen-3.8-max", 1000, 500, "implementation")
        assert entry.ticket_id == "CB-1"
        assert entry.total_tokens == 1500
        assert entry.phase == "implementation"

    def test_invalid_ticket_id_raises(self, tmp_path):
        ct = CostTracker(tmp_path)
        with pytest.raises(ValueError, match="invalid ticket_id"):
            ct.record_phase_cost("INVALID-1", "w", "m", 0, 0, "p")

    def test_empty_ticket_id_raises(self, tmp_path):
        ct = CostTracker(tmp_path)
        with pytest.raises(ValueError, match="invalid ticket_id"):
            ct.record_phase_cost("", "w", "m", 0, 0, "p")

    def test_negative_tokens_clamped(self, tmp_path):
        ct = CostTracker(tmp_path)
        entry = ct.record_phase_cost("CB-1", "w", "m", -100, -50, "p")
        assert entry.prompt_tokens == 0
        assert entry.completion_tokens == 0

    def test_negative_wall_clock_clamped(self, tmp_path):
        ct = CostTracker(tmp_path)
        entry = ct.record_phase_cost("CB-1", "w", "m", 100, 50, "p", wall_clock_seconds=-5.0)
        assert entry.wall_clock_seconds == 0.0

    def test_attempts_clamped_to_minimum_1(self, tmp_path):
        ct = CostTracker(tmp_path)
        entry = ct.record_phase_cost("CB-1", "w", "m", 100, 50, "p", attempts=0)
        assert entry.attempts == 1

    def test_total_tokens_is_sum_of_clamped(self, tmp_path):
        """total_tokens uses max(0,x) for each component."""
        ct = CostTracker(tmp_path)
        entry = ct.record_phase_cost("CB-1", "w", "m", -10, 20, "p")
        assert entry.prompt_tokens == 0
        assert entry.completion_tokens == 20
        assert entry.total_tokens == 20


class TestAppend:
    def test_append_creates_directory_and_file(self, tmp_path):
        ct = CostTracker(tmp_path / "sub" / "deep")
        entry = TicketCost(
            ticket_id="CB-1", agent="w", model="m",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            phase="p", timestamp=0.0,
        )
        ct._append(entry)
        assert (tmp_path / "sub" / "deep" / "ticket_costs.jsonl").exists()

    def test_append_uses_raw_fd(self, tmp_path):
        """_append uses os.open/os.write (raw fd), verify data written is valid JSONL."""
        ct = CostTracker(tmp_path)
        entry = TicketCost(
            ticket_id="CB-1", agent="w", model="m",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            phase="p", timestamp=0.0,
        )
        ct._append(entry)
        line = ct._costs_path.read_text().strip()
        parsed = json.loads(line)
        assert parsed["ticket_id"] == "CB-1"


class TestGetTicketTotal:
    def test_aggregates_multiple_phases(self, tmp_path):
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "planning")
        ct.record_phase_cost("CB-1", "w2", "m2", 200, 100, "implementation")
        total = ct.get_ticket_total("CB-1")
        assert total["total_tokens"] == 450
        assert total["prompt_tokens"] == 300
        assert total["completion_tokens"] == 150
        assert "planning" in total["by_phase"]
        assert "implementation" in total["by_phase"]

    def test_empty_ticket(self, tmp_path):
        ct = CostTracker(tmp_path)
        total = ct.get_ticket_total("CB-missing")
        assert total["total_tokens"] == 0

    def test_ticket_not_found_among_entries(self, tmp_path):
        """Entries exist for other tickets but not the queried one."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-99", "w", "m", 100, 50, "p")
        total = ct.get_ticket_total("CB-1")
        assert total["total_tokens"] == 0

    def test_phase_aggregation(self, tmp_path):
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w", "m", 10, 5, "phase_a")
        ct.record_phase_cost("CB-1", "w", "m", 20, 10, "phase_a")
        ct.record_phase_cost("CB-1", "w", "m", 30, 15, "phase_b")
        total = ct.get_ticket_total("CB-1")
        assert total["by_phase"]["phase_a"] == 45  # (10+5) + (20+10)
        assert total["by_phase"]["phase_b"] == 45  # (30+15)


class TestIterEntries:
    def test_blank_lines_skipped(self, tmp_path):
        """Blank lines in JSONL are silently skipped."""
        ct = CostTracker(tmp_path)
        ct._costs_path.write_text(
            '{"ticket_id":"CB-1","prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"phase":"p"}\n'
            '\n'
            '\n'
            '{"ticket_id":"CB-1","prompt_tokens":20,"completion_tokens":10,"total_tokens":30,"phase":"q"}\n',
            encoding="utf-8",
        )
        total = ct.get_ticket_total("CB-1")
        assert total["total_tokens"] == 45

    def test_malformed_json_line_skipped(self, tmp_path):
        """Invalid JSON lines are silently skipped."""
        ct = CostTracker(tmp_path)
        ct._costs_path.write_text(
            '{"ticket_id":"CB-1","prompt_tokens":10,"completion_tokens":5,"total_tokens":15,"phase":"p"}\n'
            'not valid json\n'
            '{"ticket_id":"CB-1","prompt_tokens":20,"completion_tokens":10,"total_tokens":30,"phase":"q"}\n',
            encoding="utf-8",
        )
        total = ct.get_ticket_total("CB-1")
        assert total["total_tokens"] == 45

    def test_nonexistent_costs_file(self, tmp_path):
        """get_ticket_total when costs file doesn't exist returns zeros."""
        ct = CostTracker(tmp_path)
        assert not ct._costs_path.exists()
        total = ct.get_ticket_total("CB-1")
        assert total["total_tokens"] == 0

    def test_read_recent_lines_os_error(self, tmp_path):
        """_read_recent_lines returns [] on OSError."""
        ct = CostTracker(tmp_path)
        # Create a broken path that can't be read
        with patch.object(Path, "read_text", side_effect=OSError("denied")):
            result = ct._read_recent_lines()
            assert result == []


class TestRotate:
    def test_rotate_caps_lines(self, tmp_path):
        """rotate() truncates ticket_costs.jsonl to exactly max_lines keeping most recent."""
        ct = CostTracker(tmp_path)
        max_lines = 10
        total_entries = max_lines + 50
        for i in range(total_entries):
            ct.record_phase_cost(f"CB-{i}", "w", "m", 100, 50, "p")

        ct.rotate(max_lines=max_lines)

        lines = (tmp_path / "ticket_costs.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == max_lines

        # Verify JSONL validity and that most recent entries are kept
        parsed = [json.loads(line) for line in lines]
        assert all(isinstance(entry, dict) for entry in parsed)
        # Last line should be the most recently written entry
        assert parsed[-1]["ticket_id"] == f"CB-{total_entries - 1}"
        # First remaining line should be from the tail of original writes
        assert parsed[0]["ticket_id"] == f"CB-{total_entries - max_lines}"

    def test_rotate_no_file(self, tmp_path):
        """rotate() is a no-op when the costs file doesn't exist."""
        ct = CostTracker(tmp_path)
        assert not ct._costs_path.exists()
        ct.rotate()  # Should not raise

    def test_rotate_default_max_lines_exceeds(self, tmp_path):
        """rotate() with default max_lines is called from record_phase_cost.
        When the file has fewer lines than MAX_COST_LINES, no truncation happens."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w", "m", 100, 50, "p")
        lines = (tmp_path / "ticket_costs.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1

    def test_rotate_os_error(self, tmp_path):
        """rotate() swallows OSError gracefully."""
        ct = CostTracker(tmp_path)
        # Write a file so exists() returns True
        ct.record_phase_cost("CB-1", "w", "m", 100, 50, "p")
        with patch.object(Path, "read_text", side_effect=OSError("denied")):
            ct.rotate()  # Should not raise


class TestBuildSummary:
    def test_summary_structure(self, tmp_path):
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "planning")
        ct.record_phase_cost("CB-2", "w2", "m2", 200, 100, "implementation")
        summary = ct.build_summary()
        assert "tickets" in summary
        assert "fleet_totals" in summary
        assert summary["fleet_totals"]["ticket_count"] == 2
        assert summary["fleet_totals"]["total_tokens"] == 450

    def test_summary_persisted(self, tmp_path):
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "p")
        ct.build_summary()
        assert (tmp_path / "ticket_cost_summary.json").exists()

    def test_empty_summary(self, tmp_path):
        ct = CostTracker(tmp_path)
        summary = ct.build_summary()
        assert summary["tickets"] == {}

    def test_build_summary_caching_performance(self, tmp_path):
        """Test that subsequent build_summary calls return cached result in <1ms."""
        import time
        ct = CostTracker(tmp_path)
        # Add some data
        for i in range(100):
            ct.record_phase_cost(f"CB-{i}", "w1", "m1", 100, 50, "planning")

        # First call (cache miss)
        start = time.perf_counter()
        summary1 = ct.build_summary()
        duration1 = time.perf_counter() - start

        # Second call (cache hit)
        start = time.perf_counter()
        summary2 = ct.build_summary()
        duration2 = time.perf_counter() - start

        assert duration2 < 0.001, f"Cached call took {duration2:.4f}s, expected <0.001s"
        assert summary1 == summary2

    def test_build_summary_cache_invalidation(self, tmp_path):
        """Test that build_summary detects file change and updates cache."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "planning")

        # Build initial summary
        summary1 = ct.build_summary()
        assert summary1["fleet_totals"]["ticket_count"] == 1

        # Add new entry
        ct.record_phase_cost("CB-2", "w2", "m2", 200, 100, "implementation")

        # Build summary again - should detect change
        summary2 = ct.build_summary()
        assert summary2["fleet_totals"]["ticket_count"] == 2
        assert summary2["fleet_totals"]["total_tokens"] == 450

    def test_build_summary_with_blank_lines(self, tmp_path):
        """build_summary handles blank lines in JSONL gracefully."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "planning")
        # Inject blank lines into the file
        original = ct._costs_path.read_text()
        ct._costs_path.write_text(
            original.replace("\n", "\n\n\n"),
            encoding="utf-8",
        )
        summary = ct.build_summary()
        assert summary["fleet_totals"]["ticket_count"] == 1
        assert summary["fleet_totals"]["total_tokens"] == 150

    def test_build_summary_with_malformed_json(self, tmp_path):
        """build_summary skips malformed JSON lines gracefully."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "planning")
        # Append a malformed line
        with open(str(ct._costs_path), "a") as f:
            f.write("NOT_VALID_JSON\n")
        summary = ct.build_summary()
        assert summary["fleet_totals"]["ticket_count"] == 1
        assert summary["fleet_totals"]["total_tokens"] == 150

    def test_build_summary_os_error(self, tmp_path):
        """build_summary returns last cached result when read fails with OSError."""
        ct = CostTracker(tmp_path)
        # Build initial summary (empty, no file)
        summary1 = ct.build_summary()
        assert summary1["fleet_totals"] == {}

        # Now simulate OSError on read
        with patch.object(Path, "read_text", side_effect=OSError("denied")):
            summary2 = ct.build_summary()
            assert summary2["fleet_totals"] == {}

    def test_build_summary_os_error_after_data(self, tmp_path):
        """build_summary when OSError occurs during iteration after having had data."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "p")
        # Build once successfully to populate cache
        summary_ok = ct.build_summary()
        assert summary_ok["fleet_totals"]["ticket_count"] == 1

        # Now corrupt the file with garbage that triggers OSError on read
        with patch.object(Path, "read_text", side_effect=OSError("denied")):
            # OSError is caught, stale cache returned
            summary_err = ct.build_summary()
            assert summary_err["fleet_totals"]["ticket_count"] == 1


class TestIsCacheValid:
    def test_cache_valid_empty_state_after_file_deleted(self, tmp_path):
        """If costs file is deleted after building summary of empty state, cache is still valid."""
        ct = CostTracker(tmp_path)
        # Build empty summary → sets mtime=0, size=0 in cache metadata
        summary1 = ct.build_summary()
        assert summary1["tickets"] == {}
        # Manually remove costs file if it exists (it shouldn't)
        if ct._costs_path.exists():
            ct._costs_path.unlink()
        # Cache should still be valid (was for empty state)
        assert ct._is_cache_valid() is True

    def test_cache_invalid_file_deleted_after_data(self, tmp_path):
        """If costs file is deleted after building summary with data, cache is invalid."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w", "m", 100, 50, "p")
        ct.build_summary()
        # Remove the file
        ct._costs_path.unlink()
        assert ct._is_cache_valid() is False

    def test_cache_valid_with_same_mtime(self, tmp_path):
        """Cache is valid when file hasn't changed."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w", "m", 100, 50, "p")
        ct.build_summary()
        # Without modifying the file, cache should be valid
        assert ct._is_cache_valid() is True

    def test_cache_invalid_after_file_change(self, tmp_path):
        """Cache is invalid after file changes."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w", "m", 100, 50, "p")
        ct.build_summary()
        # Write another entry → changes mtime/size
        ct.record_phase_cost("CB-2", "w", "m", 100, 50, "p")
        assert ct._is_cache_valid() is False

    def test_cache_invalid_no_cache_yet(self, tmp_path):
        """No cache → invalid."""
        ct = CostTracker(tmp_path)
        assert ct._is_cache_valid() is False

    def test_is_cache_valid_os_error(self, tmp_path):
        """_is_cache_valid returns False when stat() raises OSError."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w", "m", 100, 50, "p")
        ct.build_summary()
        # Force stat to raise OSError
        with patch.object(Path, "stat", side_effect=OSError("denied")):
            assert ct._is_cache_valid() is False


class TestUpdateCacheMetadata:
    def test_update_cache_metadata_os_error(self, tmp_path):
        """_update_cache_metadata handles OSError from stat gracefully."""
        ct = CostTracker(tmp_path)
        # Create the file so exists() returns True
        ct._costs_path.write_text("dummy\n", encoding="utf-8")
        with patch.object(Path, "stat", side_effect=OSError("denied")):
            ct._update_cache_metadata()
            assert ct._cache_file_mtime is None
            assert ct._cache_file_size is None

    def test_update_cache_metadata_no_file(self, tmp_path):
        """When costs file doesn't exist, mtime=0, size=0."""
        ct = CostTracker(tmp_path)
        ct._update_cache_metadata()
        assert ct._cache_file_mtime == 0
        assert ct._cache_file_size == 0


class TestBuildSummaryCacheHit:
    def test_cache_hit_returns_cached(self, tmp_path):
        """Second build_summary call without changes hits cache (line 197)."""
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "planning")
        s1 = ct.build_summary()
        # Force no file change
        s2 = ct.build_summary()
        assert s1 is s2  # Same object returned from cache

    def test_build_summary_os_error_during_iteration(self, tmp_path):
        """build_summary catches OSError from _read_recent_lines during iteration (lines 236-237)."""
        ct = CostTracker(tmp_path)
        # Write valid data so file exists
        ct.record_phase_cost("CB-1", "w", "m", 100, 50, "p")

        # Invalidate cache so build_summary re-reads
        ct._summary_cache = None

        # Patch _read_recent_lines to raise OSError directly
        with patch.object(ct, "_read_recent_lines", side_effect=OSError("read failed")):
            summary = ct.build_summary()
            # OSError caught, summary is empty (no entries processed)
            assert summary["fleet_totals"]["ticket_count"] == 0
            assert summary["fleet_totals"]["total_tokens"] == 0


class TestTicketCost:
    def test_to_dict(self):
        tc = TicketCost(
            ticket_id="CB-1", agent="w", model="m",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            phase="p", timestamp=0.0,
        )
        d = tc.to_dict()
        assert d["ticket_id"] == "CB-1"
        assert d["prompt_tokens"] == 10
        assert d["wall_clock_seconds"] == 0.0
        assert d["attempts"] == 1

    def test_frozen(self):
        tc = TicketCost(
            ticket_id="CB-1", agent="w", model="m",
            prompt_tokens=10, completion_tokens=5, total_tokens=15,
            phase="p", timestamp=0.0,
        )
        with pytest.raises(AttributeError):
            tc.ticket_id = "CB-2"
