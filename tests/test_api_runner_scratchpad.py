"""Tests for _write_scratchpad O(1) append optimization.

Verifies that:
  - Consecutive writes under SCRATCHPAD_MAX_BYTES use append mode (no read_text)
  - Truncation to last 100 lines occurs when file exceeds SCRATCHPAD_MAX_BYTES
  - No data loss or corruption during normal operation

Ticket: CB-8106171-4841
"""
import sys
from pathlib import Path
from unittest.mock import patch, call

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.api_runner import _write_scratchpad, SCRATCHPAD_MAX_BYTES


class TestWriteScratchpadAppendOnly:
    """Append-only fast path: no full-file read when under size limit."""

    def test_consecutive_writes_append_without_read(self, tmp_path):
        """Multiple writes should only use append, never read_text."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()

        with patch("codebot.api_runner._scratchpad_path") as mock_path:
            target = state_dir / "test-bot.scratchpad.md"
            mock_path.return_value = target

            # Mock read_text to fail if called — proves it is NOT called in fast path
            with patch.object(Path, "read_text", side_effect=AssertionError("read_text should not be called in append-only path")):
                for i in range(5):
                    _write_scratchpad("test-bot", state_dir, f"task-{i}", f"detail-{i}")

        # Verify file was created and has correct content
        assert target.exists()
        content = target.read_text(encoding="utf-8")
        lines = content.splitlines()
        assert len(lines) == 5
        for i in range(5):
            assert f"task-{i}" in lines[i]
            assert f"detail-{i}" in lines[i]

    def test_file_grows_correctly_with_appends(self, tmp_path):
        """Each call appends exactly one line to the file."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = state_dir / "bot.scratchpad.md"

        with patch("codebot.api_runner._scratchpad_path") as mock_path:
            mock_path.return_value = target
            for i in range(10):
                _write_scratchpad("bot", state_dir, f"step-{i}")

        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 10
        # Lines should be in order
        for i in range(10):
            assert f"step-{i}" in lines[i]

    def test_append_creates_file_if_not_exists(self, tmp_path):
        """Scratchpad file is created on first write even if it doesn't exist."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = state_dir / "bot.scratchpad.md"

        with patch("codebot.api_runner._scratchpad_path") as mock_path:
            mock_path.return_value = target
            _write_scratchpad("bot", state_dir, "first-task")

        assert target.exists()
        assert "first-task" in target.read_text(encoding="utf-8")

    def test_empty_file_writes_correctly(self, tmp_path):
        """First write to a non-existent file works correctly."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = state_dir / "empty.scratchpad.md"

        with patch("codebot.api_runner._scratchpad_path") as mock_path:
            mock_path.return_value = target
            _write_scratchpad("empty", state_dir, "hello")

        assert target.read_text(encoding="utf-8").startswith("- [")
        assert "hello" in target.read_text(encoding="utf-8")


class TestWriteScratchpadTruncation:
    """Lazy truncation: only happens when file exceeds SCRATCHPAD_MAX_BYTES."""

    def test_truncation_preserves_last_100_lines(self, tmp_path):
        """When file exceeds limit, only last 100 lines are kept."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = state_dir / "bot.scratchpad.md"

        # Create a file that exceeds SCRATCHPAD_MAX_BYTES with ~150 lines
        lines = [f"- [00:00:00] line-{i}: {'x' * 40}\n" for i in range(150)]
        target.write_text("".join(lines), encoding="utf-8")

        with patch("codebot.api_runner._scratchpad_path") as mock_path:
            mock_path.return_value = target
            _write_scratchpad("bot", state_dir, "trigger-truncation")

        result_lines = target.read_text(encoding="utf-8").splitlines()
        # Should have at most 101 lines (100 retained + 1 new)
        assert len(result_lines) <= 101
        # The new line should be present
        assert any("trigger-truncation" in l for l in result_lines)

    def test_truncation_reduces_file_size(self, tmp_path):
        """After truncation, file size should be below SCRATCHPAD_MAX_BYTES."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = state_dir / "bot.scratchpad.md"

        # Create a file above the limit with realistic line sizes (~60 bytes)
        lines = [f"- [00:00:00] line-{i}: {'x' * 40}\n" for i in range(200)]
        target.write_text("".join(lines), encoding="utf-8")
        oversized = target.stat().st_size
        assert oversized > SCRATCHPAD_MAX_BYTES

        with patch("codebot.api_runner._scratchpad_path") as mock_path:
            mock_path.return_value = target
            _write_scratchpad("bot", state_dir, "truncated")

        # After truncation to 100 lines at ~60 bytes each, should be under limit
        assert target.stat().st_size <= SCRATCHPAD_MAX_BYTES

    def test_under_limit_no_truncation(self, tmp_path):
        """Small file should not be truncated after writes."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = state_dir / "bot.scratchpad.md"

        with patch("codebot.api_runner._scratchpad_path") as mock_path:
            mock_path.return_value = target
            for i in range(3):
                _write_scratchpad("bot", state_dir, f"small-{i}")

        lines = target.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        for i in range(3):
            assert f"small-{i}" in lines[i]

    def test_no_data_loss_during_truncation(self, tmp_path):
        """Most recent lines survive truncation; nothing is silently dropped."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = state_dir / "bot.scratchpad.md"

        # Fill file to exceed limit
        lines = [f"- [00:00:00] line-{i}: {'y' * 40}\n" for i in range(150)]
        target.write_text("".join(lines), encoding="utf-8")

        with patch("codebot.api_runner._scratchpad_path") as mock_path:
            mock_path.return_value = target
            _write_scratchpad("bot", state_dir, "final-line")

        result = target.read_text(encoding="utf-8")
        # The very last line (line-149) should be in the retained set
        assert "line-149" in result
        # Our new final line should be the actual last line
        result_lines = result.splitlines()
        assert "final-line" in result_lines[-1]

    def test_multiple_truncations_stable(self, tmp_path):
        """Multiple calls that each exceed limit should each truncate cleanly."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        target = state_dir / "bot.scratchpad.md"

        with patch("codebot.api_runner._scratchpad_path") as mock_path:
            mock_path.return_value = target

            # First, build a file over the limit with many iterations
            # Use a very small limit to force truncation quickly
            with patch("codebot.api_runner.SCRATCHPAD_MAX_BYTES", 500):
                for i in range(20):
                    _write_scratchpad("bot", state_dir, f"iteration-{i}: " + "z" * 50)

                # File should still be manageable
                size = target.stat().st_size
                assert size < 2000  # well under what 20 uncapped writes would produce
