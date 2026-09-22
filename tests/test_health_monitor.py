"""Tests for codebot.health_monitor — focus on log_mtime caching."""

from __future__ import annotations

import time
from unittest.mock import patch, MagicMock

import codebot.health_monitor as hm


class TestLogMtimeCaching:
    """Verify that log_mtime caches results to reduce stat syscalls."""

    def setup_method(self):
        """Clear cache before each test."""
        hm._clear_log_mtime_cache()

    def test_two_calls_within_ttl_only_one_stat(self):
        """Two calls for same bot within 30s should trigger only one stat."""
        fake_mtime = 1700000000.0
        stat_mock = MagicMock(return_value=MagicMock(st_mtime=fake_mtime))

        with patch("codebot.health_monitor.log_path") as lp_mock:
            lp_mock.return_value.stat = stat_mock
            result1 = hm.log_mtime("test_bot")
            result2 = hm.log_mtime("test_bot")

        assert result1 == fake_mtime
        assert result2 == fake_mtime
        assert stat_mock.call_count == 1, (
            f"Expected 1 stat call, got {stat_mock.call_count}"
        )

    def test_call_after_ttl_refreshes_cache(self):
        """A call after TTL expires should trigger another stat."""
        fake_mtime = 1700000000.0
        stat_mock = MagicMock(return_value=MagicMock(st_mtime=fake_mtime))

        with patch("codebot.health_monitor.log_path") as lp_mock:
            lp_mock.return_value.stat = stat_mock
            hm.log_mtime("test_bot")

            # Simulate TTL expiry by backdating the cache entry
            hm._log_mtime_cache["test_bot"] = (
                fake_mtime,
                time.time() - hm._LOG_MTIME_TTL - 1,
            )

            hm.log_mtime("test_bot")

        assert stat_mock.call_count == 2, (
            f"Expected 2 stat calls after TTL, got {stat_mock.call_count}"
        )

    def test_different_bots_get_separate_cache_entries(self):
        """Different bot names should each trigger their own stat call."""
        stat_mock = MagicMock(side_effect=[
            MagicMock(st_mtime=1700000000.0),
            MagicMock(st_mtime=1700000060.0),
        ])

        with patch("codebot.health_monitor.log_path") as lp_mock:
            lp_mock.return_value.stat = stat_mock
            r1 = hm.log_mtime("bot_a")
            r2 = hm.log_mtime("bot_b")

        assert r1 == 1700000000.0
        assert r2 == 1700000060.0
        assert stat_mock.call_count == 2

    def test_missing_log_file_returns_zero_and_caches(self):
        """If stat raises OSError, 0.0 is returned and cached."""
        stat_mock = MagicMock(side_effect=OSError("no such file"))

        with patch("codebot.health_monitor.log_path") as lp_mock:
            lp_mock.return_value.stat = stat_mock
            r1 = hm.log_mtime("no_log_bot")
            r2 = hm.log_mtime("no_log_bot")

        assert r1 == 0.0
        assert r2 == 0.0
        assert stat_mock.call_count == 1, (
            "Even 0.0 results should be cached to avoid repeated stat"
        )

    def test_clear_cache_single_bot(self):
        """_clear_log_mtime_cache(name) clears only that bot's entry."""
        stat_mock = MagicMock(return_value=MagicMock(st_mtime=1700000000.0))

        with patch("codebot.health_monitor.log_path") as lp_mock:
            lp_mock.return_value.stat = stat_mock
            hm.log_mtime("bot_a")
            hm.log_mtime("bot_b")
            assert stat_mock.call_count == 2

            hm._clear_log_mtime_cache("bot_a")

            hm.log_mtime("bot_a")  # should re-stat
            hm.log_mtime("bot_b")  # should use cache

        assert stat_mock.call_count == 3

    def test_clear_cache_all(self):
        """_clear_log_mtime_cache(None) clears everything."""
        stat_mock = MagicMock(return_value=MagicMock(st_mtime=1700000000.0))

        with patch("codebot.health_monitor.log_path") as lp_mock:
            lp_mock.return_value.stat = stat_mock
            hm.log_mtime("bot_a")
            hm.log_mtime("bot_b")
            assert stat_mock.call_count == 2

            hm._clear_log_mtime_cache()

            hm.log_mtime("bot_a")
            hm.log_mtime("bot_b")

        assert stat_mock.call_count == 4
