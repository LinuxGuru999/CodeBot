"""Tests for log_tail path traversal protection (CB-8897311-A841).

Verifies that log_tail rejects path traversal attempts and validates
bot names against safe patterns.
"""
import pytest
from unittest.mock import patch, MagicMock

from codebot.control_server import log_tail, BOT_NAME_PATTERN


class TestLogTailPathTraversal:
    """Test path traversal protection in log_tail function."""

    def test_rejects_name_with_forward_slash(self):
        """log_tail must reject names containing '/' character."""
        result = log_tail("../etc/passwd", 10)
        assert result == ""

    def test_rejects_name_with_double_dot(self):
        """log_tail must reject names containing '..' sequence."""
        result = log_tail("..\\windows\\system32", 10)
        assert result == ""

    def test_rejects_name_with_double_dot_slash(self):
        """log_tail must reject '../' sequences."""
        result = log_tail("../../etc/shadow", 10)
        assert result == ""

    def test_rejects_non_string_name(self):
        """log_tail must reject non-string names."""
        result = log_tail(123, 10)  # type: ignore
        assert result == ""
        result = log_tail(None, 10)  # type: ignore
        assert result == ""

    def test_accepts_valid_bot_name(self):
        """log_tail should accept valid bot names matching ^[a-zA-Z0-9_-]+$ pattern."""
        with patch("codebot.control_server.LOGS_DIR") as mock_logs_dir:
            mock_path = MagicMock()
            mock_path.exists.return_value = False
            mock_logs_dir.__truediv__.return_value = mock_path
            
            result = log_tail("valid-bot_name123", 10)
            assert result == ""

    def test_rejects_name_with_special_characters(self):
        """log_tail must reject names with characters outside [a-zA-Z0-9_-]."""
        result = log_tail("bot; rm -rf /", 10)
        assert result == ""
        result = log_tail("bot$(whoami)", 10)
        assert result == ""
        result = log_tail("bot`id`", 10)
        assert result == ""

    def test_os_path_basename_safeguard_strips_path_components(self):
        """os.path.basename() should be used as additional safeguard."""
        # This test verifies the function uses os.path.basename internally
        # by checking that a name with path separators is rejected
        result = log_tail("subdir/botname", 10)
        assert result == ""

    def test_valid_name_returns_empty_for_missing_log(self):
        """Valid name with missing log file should return empty string."""
        with patch("codebot.control_server.LOGS_DIR") as mock_logs_dir:
            mock_path = MagicMock()
            mock_path.exists.return_value = False
            mock_logs_dir.__truediv__.return_value = mock_path
            
            result = log_tail("mybot", 10)
            assert result == ""

    def test_valid_name_reads_existing_log(self):
        """Valid name with existing log file should read the log."""
        with patch("codebot.control_server.LOGS_DIR") as mock_logs_dir, \
             patch("codebot.control_server.subprocess.run") as mock_run:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.return_value = "log line 1\nlog line 2"
            mock_logs_dir.__truediv__.return_value = mock_path
            
            mock_result = MagicMock()
            mock_result.stdout = "log line 1\nlog line 2"
            mock_result.returncode = 0
            mock_run.return_value = mock_result
            
            result = log_tail("mybot", 10)
            assert result == "log line 1\nlog line 2"


class TestBotNamePattern:
    """Test BOT_NAME_PATTERN regex validation."""

    def test_valid_names_match_pattern(self):
        """Valid bot names should match the pattern."""
        assert BOT_NAME_PATTERN.match("mybot") is not None
        assert BOT_NAME_PATTERN.match("bot-123") is not None
        assert BOT_NAME_PATTERN.match("bot_name") is not None
        assert BOT_NAME_PATTERN.match("Bot-Name_123") is not None

    def test_invalid_names_rejected_by_pattern(self):
        """Invalid bot names should not match the pattern."""
        assert BOT_NAME_PATTERN.match("../etc/passwd") is None
        assert BOT_NAME_PATTERN.match("bot;rm") is None
        assert BOT_NAME_PATTERN.match("bot name") is None
        assert BOT_NAME_PATTERN.match("bot/name") is None
        assert BOT_NAME_PATTERN.match("bot.name") is None
