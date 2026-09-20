"""Tests for control_client.py --force/--dry-run/confirmation for destructive commands.

Ticket: CB-4437763-EB8F — Destructive control actions lack confirmation and undo affordance

Acceptance Criteria:
- CLI --force flag skips interactive confirmation
- CLI --dry-run flag sends dry_run in request body
- Without --force or --dry-run, interactive confirmation is prompted
- Undo info is displayed in response output
- Bot name validation prevents injection
"""
from __future__ import annotations

import json
import unittest
from io import StringIO
from unittest.mock import MagicMock, patch

from codebot.control_client import validate_bot_name_simple, _print_result


class TestBotNameValidation(unittest.TestCase):
    """Verify that bot name validation matches server-side rules."""

    def test_valid_names(self):
        assert validate_bot_name_simple("test-bot")
        assert validate_bot_name_simple("bug_fix")
        assert validate_bot_name_simple("general_implementer")
        assert validate_bot_name_simple("bot1")

    def test_invalid_names(self):
        assert not validate_bot_name_simple("test bot")  # space
        assert not validate_bot_name_simple("test/bot")  # slash
        assert not validate_bot_name_simple("test;bot")  # semicolon
        assert not validate_bot_name_simple("test`bot")  # backtick
        assert not validate_bot_name_simple("$(cmd)")  # command injection
        assert not validate_bot_name_simple("")  # empty
        assert not validate_bot_name_simple("bot..etc/passwd")  # path traversal


class TestPrintResult(unittest.TestCase):
    """Verify _print_result displays undo info."""

    @patch("builtins.print")
    def test_undo_hint_displayed(self, mock_print):
        j = {"ok": True, "undo": "POST /bots/test/resume to unpause"}
        _print_result(j)
        calls = [str(c) for c in mock_print.call_args_list]
        # Should print recovery hint
        assert any("Recovery" in c for c in calls)

    @patch("builtins.print")
    def test_dry_run_preview_displayed(self, mock_print):
        j = {"ok": True, "dry_run": True, "preview": {
            "description": "Would restart bot 'test'",
            "undo": "Bot will auto-respawn"
        }}
        _print_result(j)
        calls = [str(c) for c in mock_print.call_args_list]
        assert any("Preview" in c or "Recovery" in c for c in calls)

    @patch("builtins.print")
    def test_string_response_printed_as_is(self, mock_print):
        _print_result("raw string")
        mock_print.assert_called_with("raw string")


class TestClientRestartPauseConfirmation(unittest.TestCase):
    """Verify that restart/pause commands require confirmation."""

    @patch("codebot.control_client.req")
    def test_restart_force_skips_confirmation(self, mock_req):
        """--force should send directly without prompting."""
        mock_req.return_value = (200, {"ok": True, "action": "restart", "bot": "test-bot", "undo": "hint"})
        import sys
        with patch("sys.argv", ["control_client.py", "restart", "test-bot", "--force"]):
            import codebot.control_client as cc
            cc.main()
        # Verify force was passed in request body
        call_args = mock_req.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertEqual(call_args[0][1], "/bots/test-bot/restart")
        self.assertTrue(call_args[0][2].get("force"))

    @patch("codebot.control_client.req")
    def test_pause_force_skips_confirmation(self, mock_req):
        """--force should send directly without prompting."""
        mock_req.return_value = (200, {"ok": True, "paused": "test-bot", "undo": "hint"})
        import sys
        with patch("sys.argv", ["control_client.py", "pause", "test-bot", "--force"]):
            import codebot.control_client as cc
            cc.main()
        call_args = mock_req.call_args
        self.assertEqual(call_args[0][0], "POST")
        self.assertEqual(call_args[0][1], "/bots/test-bot/pause")
        self.assertTrue(call_args[0][2].get("force"))

    @patch("codebot.control_client.req")
    def test_restart_dry_run_sends_dry_run(self, mock_req):
        """--dry-run should send dry_run in payload."""
        mock_req.return_value = (200, {"ok": True, "dry_run": True, "preview": {}})
        import sys
        with patch("sys.argv", ["control_client.py", "restart", "test-bot", "--dry-run"]):
            import codebot.control_client as cc
            cc.main()
        call_args = mock_req.call_args
        self.assertTrue(call_args[0][2].get("dry_run"))

    @patch("codebot.control_client.req")
    def test_pause_dry_run_sends_dry_run(self, mock_req):
        """--dry-run should send dry_run in payload."""
        mock_req.return_value = (200, {"ok": True, "dry_run": True, "preview": {}})
        import sys
        with patch("sys.argv", ["control_client.py", "pause", "test-bot", "--dry-run"]):
            import codebot.control_client as cc
            cc.main()
        call_args = mock_req.call_args
        self.assertTrue(call_args[0][2].get("dry_run"))

    @patch("codebot.control_client.req")
    @patch("builtins.input", return_value="yes")
    def test_restart_confirms_with_yes(self, mock_input, mock_req):
        """Interactive confirmation should be accepted with 'yes'."""
        mock_req.return_value = (200, {"ok": True, "action": "restart", "bot": "test-bot"})
        import sys
        with patch("sys.argv", ["control_client.py", "restart", "test-bot"]):
            import codebot.control_client as cc
            cc.main()
        mock_input.assert_called_once()
        call_args = mock_req.call_args
        self.assertTrue(call_args[0][2].get("force"))

    @patch("codebot.control_client.req")
    @patch("builtins.input", return_value="no")
    def test_restart_aborts_without_confirmation(self, mock_input, mock_req):
        """Interactive confirmation should abort on 'no'."""
        import sys
        with patch("sys.argv", ["control_client.py", "restart", "test-bot"]), \
             self.assertRaises(SystemExit) as cm:
            import codebot.control_client as cc
            cc.main()
        mock_req.assert_not_called()
        self.assertEqual(cm.exception.code, 0)

    @patch("codebot.control_client.req")
    @patch("builtins.input", return_value="yes")
    def test_pause_confirms_with_yes(self, mock_input, mock_req):
        """Interactive confirmation should be accepted with 'yes'."""
        mock_req.return_value = (200, {"ok": True, "paused": "test-bot"})
        import sys
        with patch("sys.argv", ["control_client.py", "pause", "test-bot"]):
            import codebot.control_client as cc
            cc.main()
        mock_input.assert_called_once()

    def test_restart_invalid_bot_name_rejected(self):
        """Invalid bot names should be rejected before any HTTP call."""
        import sys
        with patch("sys.argv", ["control_client.py", "restart", "test bot; rm -rf /"]), \
             self.assertRaises(SystemExit) as cm:
            import codebot.control_client as cc
            cc.main()
        self.assertEqual(cm.exception.code, 1)

    def test_pause_invalid_bot_name_rejected(self):
        """Invalid bot names should be rejected before any HTTP call."""
        import sys
        with patch("sys.argv", ["control_client.py", "pause", "$(evil)"]), \
             self.assertRaises(SystemExit) as cm:
            import codebot.control_client as cc
            cc.main()
        self.assertEqual(cm.exception.code, 1)


class TestClientStopDrainUpdateConfirmation(unittest.TestCase):
    """Verify that stop/drain/update commands also have confirmation."""

    @patch("codebot.control_client.req")
    def test_stop_force(self, mock_req):
        mock_req.return_value = (200, {"ok": True, "stopped": ["test-bot"]})
        import sys
        with patch("sys.argv", ["control_client.py", "stop", "test-bot", "--force"]):
            import codebot.control_client as cc
            cc.main()
        call_args = mock_req.call_args
        self.assertTrue(call_args[0][2].get("force"))

    @patch("codebot.control_client.req")
    def test_drain_force(self, mock_req):
        mock_req.return_value = (200, {"ok": True, "drain": True})
        import sys
        with patch("sys.argv", ["control_client.py", "drain", "--force"]):
            import codebot.control_client as cc
            cc.main()
        call_args = mock_req.call_args
        self.assertTrue(call_args[0][2].get("force"))

    @patch("codebot.control_client.req")
    def test_update_force(self, mock_req):
        mock_req.return_value = (200, {"ok": True})
        import sys
        with patch("sys.argv", ["control_client.py", "update", "--force"]):
            import codebot.control_client as cc
            cc.main()
        call_args = mock_req.call_args
        self.assertTrue(call_args[0][2].get("force"))

    @patch("codebot.control_client.req")
    def test_drain_dry_run(self, mock_req):
        mock_req.return_value = (200, {"dry_run": True, "preview": {}})
        import sys
        with patch("sys.argv", ["control_client.py", "drain", "--dry-run"]):
            import codebot.control_client as cc
            cc.main()
        call_args = mock_req.call_args
        self.assertTrue(call_args[0][2].get("dry_run"))


if __name__ == "__main__":
    unittest.main()
