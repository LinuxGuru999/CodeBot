"""Test that bot names are properly escaped in pkill/pgrep patterns.

This ensures regex metacharacters in bot names don't cause unintended
process matching (security vulnerability CB-E1C9D8BE82AE).
"""
import re
import unittest
from unittest.mock import MagicMock, patch


class TestPkillRegexEscape(unittest.TestCase):
    """Test regex escaping in bot process management."""

    def test_re_escape_dots_in_bot_name(self):
        """Verify re.escape() converts dots to literal matches.

        A bot name like 'test.bot' should be escaped to 'test\.bot'
        so it doesn't match 'testXbot' (where . would match any char).
        """
        bot_name = "test.bot"
        escaped = re.escape(bot_name)
        # re.escape should convert '.' to '\.'
        self.assertEqual(escaped, "test\\.bot")

        # The escaped pattern should NOT match 'testXbot'
        pattern = f"api_runner\\.py {escaped}$"
        self.assertFalse(bool(re.search(pattern, "api_runner.py testXbot")))

        # The escaped pattern SHOULD match 'test.bot'
        self.assertTrue(bool(re.search(pattern, "api_runner.py test.bot")))

    def test_re_escape_other_metacharacters(self):
        """Verify other regex metacharacters are also escaped."""
        test_cases = [
            ("test*bot", "test\\*bot"),
            ("test+bot", "test\\+bot"),
            ("test?bot", "test\\?bot"),
            ("test[bot]", "test\\[bot\\]"),
            ("test(bot)", "test\\(bot\\)"),
        ]
        for original, expected_escaped in test_cases:
            escaped = re.escape(original)
            self.assertEqual(escaped, expected_escaped,
                             f"Failed for {original}")

    def test_valid_bot_names_still_work(self):
        """Ensure normal bot names without special chars still match correctly."""
        valid_names = ["my-bot", "my_bot", "bot123", "BotName"]
        for name in valid_names:
            escaped = re.escape(name)
            pattern = f"api_runner\\.py {escaped}$"
            # Should match exact name
            self.assertTrue(
                bool(re.search(pattern, f"api_runner.py {name}")),
                f"Pattern should match '{name}'"
            )
            # Should not match similar but different names
            self.assertFalse(
                bool(re.search(pattern, f"api_runner.py {name}-extra")),
                f"Pattern should not match '{name}-extra'"
            )

    @patch("codebot.control_server.subprocess.run")
    @patch("codebot.control_server.validate_bot_name")
    def test_safe_kill_uses_re_escape(self, mock_validate, mock_run):
        """Kill path targets only the PID-file process, never a regex sweep.

        Regression guard for CB-3FA9AA16E8A8E10D8586383A316E62C8: the
        implementation must NOT invoke pgrep/pkill -f with a bot-name
        pattern (even re.escape()d), because a non-api_runner process
        with a matching cmdline suffix would be collected and killed.
        Exact PID-file targeting + /proc argv verification + pidfd
        signaling is the only acceptable discovery mechanism.

        The two surviving re.escape() unit tests (dots/metachars) still
        verify the escaping primitive itself for the read-only
        bot_status() pgrep probe; the kill path itself must not scan.
        """
        from codebot.control_server import _safe_kill_bot_process

        mock_validate.return_value = True
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_result.stderr = ""
        mock_run.return_value = mock_result

        # Call with a bot name containing regex metacharacter and no PID file.
        _safe_kill_bot_process("test.bot", timeout=5)

        # Kill path must NOT call subprocess.run at all (no pgrep/pkill).
        mock_run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
