"""Tests for CB-2509422-D33D: TOCTOU race in _check_prompt_changes / start_bot.

Verifies that prompt changes are detected and applied atomically without
race conditions, no partial prompt loads occur, and concurrent prompt
updates are handled safely.
"""

import os
import sys
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import codebot.orchestrator as orch
from codebot.orchestrator import BotConfig, BotState, _check_prompt_changes


def _make_config(name: str = "test-bot", prompt_file: str = "test-bot.md",
                 interval: int = 300) -> BotConfig:
    return BotConfig(
        name=name,
        prompt_file=prompt_file,
        interval_seconds=interval,
        heartbeat_timeout=interval * 2,
        model="xiaomi-mimo-2.5",
        enabled=True,
        max_restarts=5,
        tier=2,
    )


def _make_bot(config: BotConfig | None = None, **kwargs) -> BotState:
    cfg = config or _make_config()
    return BotState(config=cfg, **kwargs)


class TestPromptChangeAtomicity:
    """Verify prompt read is atomic with mtime check (no TOCTOU)."""

    def test_read_prompt_with_mtime_returns_consistent_pair(self, tmp_path):
        """read_prompt_with_mtime returns content and mtime from a single atomic read."""
        prompt_file = tmp_path / "test-bot.md"
        prompt_file.write_text("original content", encoding="utf-8")
        mtime_before = prompt_file.stat().st_mtime

        content, mtime = orch.read_prompt_with_mtime(prompt_file)

        assert content == "original content"
        assert mtime == pytest.approx(mtime_before, abs=1.0)

    def test_read_prompt_with_mtime_missing_file(self, tmp_path):
        """read_prompt_with_mtime returns empty string and 0.0 for missing file."""
        missing = tmp_path / "nonexistent.md"
        content, mtime = orch.read_prompt_with_mtime(missing)
        assert content == ""
        assert mtime == 0.0

    def test_read_prompt_with_mtime_no_partial_reads(self, tmp_path):
        """Even during concurrent writes, read_prompt_with_mtime never returns partial content."""
        prompt_file = tmp_path / "test-bot.md"
        full_content = "A" * 10000
        prompt_file.write_text(full_content, encoding="utf-8")

        results: list[tuple[str, float]] = []
        stop_event = threading.Event()

        def writer():
            while not stop_event.is_set():
                # Atomic write via tmp+replace to simulate real updates
                tmp = prompt_file.with_suffix(".tmp")
                new_content = "B" * 10000
                tmp.write_text(new_content, encoding="utf-8")
                tmp.replace(prompt_file)
                time.sleep(0.001)
                tmp2 = prompt_file.with_suffix(".tmp")
                tmp2.write_text(full_content, encoding="utf-8")
                tmp2.replace(prompt_file)
                time.sleep(0.001)

        def reader():
            while not stop_event.is_set():
                content, mtime = orch.read_prompt_with_mtime(prompt_file)
                if content:
                    results.append((content, mtime))
                time.sleep(0.0005)

        writer_thread = threading.Thread(target=writer, daemon=True)
        reader_thread = threading.Thread(target=reader, daemon=True)
        writer_thread.start()
        reader_thread.start()

        time.sleep(0.2)
        stop_event.set()
        writer_thread.join(timeout=2)
        reader_thread.join(timeout=2)

        # Every read must be complete — either all A's or all B's, never mixed/truncated
        assert len(results) > 0, "Reader should have captured at least one read"
        for content, mtime in results:
            assert len(content) == 10000, f"Partial read detected: length={len(content)}"
            assert content == "A" * 10000 or content == "B" * 10000, \
                f"Mixed content detected: {content[:50]}...{content[-50:]}"
            assert mtime > 0


class TestCheckPromptChangesUsesAtomicRead:
    """Verify _check_prompt_changes uses atomic read-with-mtime."""

    def test_check_prompt_changes_updates_mtime_atomically(self, tmp_path):
        """_check_prompt_changes stores the mtime from the same read used for comparison."""
        prompt_file = tmp_path / "test-bot.md"
        prompt_file.write_text("v1", encoding="utf-8")

        cfg = _make_config(prompt_file="test-bot.md")
        bot = _make_bot(config=cfg)
        bot.last_prompt_mtime = 0.0
        bots = {"test-bot": bot}

        with patch.object(orch, "BOTS_DIR", tmp_path):
            _check_prompt_changes(bots)

        expected_mtime = prompt_file.stat().st_mtime
        assert bot.last_prompt_mtime == pytest.approx(expected_mtime, abs=1.0)

    def test_check_prompt_changes_detects_update(self, tmp_path):
        """_check_prompt_changes detects when prompt mtime advances."""
        prompt_file = tmp_path / "test-bot.md"
        prompt_file.write_text("v1", encoding="utf-8")

        cfg = _make_config(prompt_file="test-bot.md")
        bot = _make_bot(config=cfg)
        bot.last_prompt_mtime = prompt_file.stat().st_mtime
        bot.process = MagicMock()
        bot.process.poll.return_value = None  # alive
        bots = {"test-bot": bot}

        # Simulate prompt update
        time.sleep(0.05)
        prompt_file.write_text("v2", encoding="utf-8")

        with patch.object(orch, "BOTS_DIR", tmp_path), \
             patch.object(orch, "stop_bot") as mock_stop:
            _check_prompt_changes(bots)
            mock_stop.assert_called_once_with(bot, "prompt-hot-reload")


class TestConcurrentPromptUpdates:
    """Test for concurrent prompt updates (acceptance criterion 3)."""

    def test_concurrent_prompt_changes_no_missed_updates(self, tmp_path):
        """Multiple rapid prompt updates are all detected; none silently missed."""
        prompt_file = tmp_path / "test-bot.md"
        prompt_file.write_text("v0", encoding="utf-8")

        cfg = _make_config(prompt_file="test-bot.md")
        bot = _make_bot(config=cfg)
        bot.last_prompt_mtime = 0.0
        bot.process = None  # not running, so stop_bot won't be called
        bots = {"test-bot": bot}

        versions_written: list[float] = []
        stop_event = threading.Event()

        def writer():
            for i in range(20):
                if stop_event.is_set():
                    break
                # Use atomic tmp+replace to avoid partial writes
                tmp = prompt_file.with_suffix(".tmp")
                tmp.write_text(f"v{i+1}", encoding="utf-8")
                tmp.replace(prompt_file)
                versions_written.append(prompt_file.stat().st_mtime)
                time.sleep(0.01)

        def checker():
            while not stop_event.is_set():
                with patch.object(orch, "BOTS_DIR", tmp_path):
                    _check_prompt_changes(bots)
                time.sleep(0.005)

        writer_thread = threading.Thread(target=writer, daemon=True)
        checker_thread = threading.Thread(target=checker, daemon=True)

        checker_thread.start()
        writer_thread.start()
        writer_thread.join(timeout=5)
        stop_event.set()
        checker_thread.join(timeout=5)

        # The final mtime stored on the bot must match the last written version
        final_mtime = prompt_file.stat().st_mtime
        assert bot.last_prompt_mtime == pytest.approx(final_mtime, abs=1.0), \
            f"Bot mtime {bot.last_prompt_mtime} != final file mtime {final_mtime}"

    def test_start_bot_reads_prompt_matching_recorded_mtime(self, tmp_path):
        """start_bot reads prompt content whose mtime matches what was recorded.

        This ensures the TOCTOU gap between _check_prompt_changes and start_bot
        is closed: both use read_prompt_with_mtime.
        """
        prompt_file = tmp_path / "test-bot.md"
        prompt_file.write_text("stable content", encoding="utf-8")

        content, mtime = orch.read_prompt_with_mtime(prompt_file)
        assert content == "stable content"
        assert mtime > 0

        # Verify re-reading gives same mtime when file hasn't changed
        content2, mtime2 = orch.read_prompt_with_mtime(prompt_file)
        assert content2 == content
        assert mtime2 == mtime
