"""Tests for command injection prevention in bot control handlers.

Ticket: CB-8897336-6B72 — Critical: Command injection via pkill in bot control handlers

Constitution §2 mandates:
- Input validation at trust boundaries (bot name format validation)
- Defense-in-depth: regex validation + shlex.quote()
- All subprocess calls with user-supplied data must be protected
"""
from __future__ import annotations

import json
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch


class TestCommandInjectionPrevention(unittest.TestCase):
    """Verify that bot control endpoints reject command injection attempts."""

    def _make_handler(self, method: str, path: str, body: dict | None = None):
        """Create a mock request handler for testing."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.path = path
        handler.command = method
        handler.headers = {"Authorization": "Bearer test-token"}
        if body is not None:
            handler.rfile = BytesIO(json.dumps(body).encode())
            handler.headers["Content-Length"] = str(len(json.dumps(body)))
        else:
            handler.rfile = BytesIO(b"")
            handler.headers["Content-Length"] = "0"
        return handler

    # --- Restart endpoint tests ---

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_semicolon_injection(self):
        """Restart must reject bot names with semicolon injection attempts."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test; rm -rf /restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_pipe_injection(self):
        """Restart must reject bot names with pipe injection attempts."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test|cat /etc/passwd/restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_backtick_injection(self):
        """Restart must reject bot names with backtick injection attempts."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test`whoami`/restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_dollar_sign_injection(self):
        """Restart must reject bot names with $() injection attempts."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test$(rm -rf /)/restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_ampersand_injection(self):
        """Restart must reject bot names with ampersand injection attempts."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test&evil/restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_space_injection(self):
        """Restart must reject bot names with spaces (could break command parsing)."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test evil/restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    # --- Pause endpoint tests ---

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pause_rejects_semicolon_injection(self):
        """Pause must reject bot names with semicolon injection attempts."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test; rm -rf /pause")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pause_rejects_newline_injection(self):
        """Pause must reject bot names with newline injection attempts."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test\nevil/pause")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    # --- Resume endpoint tests ---

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_resume_rejects_semicolon_injection(self):
        """Resume must reject bot names with semicolon injection attempts."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test; rm -rf /resume")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    # --- Stop endpoint tests (batch) ---

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_stop_rejects_injection_in_batch(self):
        """Stop must reject bot names with injection attempts in batch mode."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler(
            "POST", "/bots/stop", body={"bots": ["valid-bot", "evil; rm -rf /"]}
        )
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["valid-bot", "evil; rm -rf /"]}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    # --- Verify shlex.quote is applied ---

    def test_restart_applies_shlex_quote(self):
        """Restart must apply shlex.quote to bot names before subprocess call."""
        from codebot.control_server import ControlHandler
        import shlex

        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.subprocess.run") as mock_run:
                handler = self._make_handler("POST", "/bots/valid-bot/restart")
                responses = []
                handler._json = lambda code, data, r=responses: r.append((code, data))
                handler._auth = lambda: True
                handler._read_json_body = lambda: (None, None, None)

                ControlHandler.do_POST(handler)

                # Verify pkill was called with quoted name
                mock_run.assert_called()
                call_args = mock_run.call_args
                cmd = call_args[0][0]  # First positional arg is the command list
                # The pattern should contain the quoted bot name
                expected_pattern = f"api_runner\\.py {shlex.quote('valid-bot')}"
                self.assertIn(expected_pattern, cmd)

    def test_pause_applies_shlex_quote(self):
        """Pause must apply shlex.quote to bot names before subprocess call."""
        from codebot.control_server import ControlHandler
        import shlex

        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.STATE_DIR") as mock_state:
                with patch("codebot.control_server.subprocess.run") as mock_run:
                    handler = self._make_handler("POST", "/bots/valid-bot/pause")
                    responses = []
                    handler._json = lambda code, data, r=responses: r.append((code, data))
                    handler._auth = lambda: True
                    handler._read_json_body = lambda: (None, None, None)

                    mock_paused_file = MagicMock()
                    mock_state.__truediv__ = MagicMock(return_value=mock_paused_file)

                    ControlHandler.do_POST(handler)

                    mock_run.assert_called()
                    call_args = mock_run.call_args
                    cmd = call_args[0][0]
                    expected_pattern = f"api_runner\\.py {shlex.quote('valid-bot')}"
                    self.assertIn(expected_pattern, cmd)

    def test_resume_applies_shlex_quote(self):
        """Resume must apply shlex.quote to bot names before subprocess call."""
        from codebot.control_server import ControlHandler
        import shlex

        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.STATE_DIR") as mock_state:
                with patch("codebot.control_server.subprocess.Popen") as mock_popen:
                    handler = self._make_handler("POST", "/bots/valid-bot/resume")
                    responses = []
                    handler._json = lambda code, data, r=responses: r.append((code, data))
                    handler._auth = lambda: True
                    handler._read_json_body = lambda: (None, None, None)

                    mock_paused_file = MagicMock()
                    mock_paused_file.exists.return_value = False
                    mock_state.__truediv__ = MagicMock(return_value=mock_paused_file)

                    ControlHandler.do_POST(handler)

                    mock_popen.assert_called()
                    call_args = mock_popen.call_args
                    cmd = call_args[0][0]
                    # The last argument should be the quoted bot name
                    self.assertEqual(cmd[-1], shlex.quote("valid-bot"))

    # --- Validate bot name function directly ---

    def test_validate_bot_name_accepts_valid_names(self):
        """validate_bot_name must accept alphanumeric, hyphen, underscore names."""
        from codebot.control_server import validate_bot_name

        valid_names = [
            "simple",
            "with-hyphen",
            "with_underscore",
            "Bot123",
            "a",
            "test-bot_01",
        ]

        for name in valid_names:
            self.assertTrue(validate_bot_name(name), f"Should accept: {name}")

    def test_validate_bot_name_rejects_injection_attempts(self):
        """validate_bot_name must reject all injection attempt patterns."""
        from codebot.control_server import validate_bot_name

        invalid_names = [
            "test; rm -rf /",
            "test|cat /etc/passwd",
            "test`whoami`",
            "test$(rm -rf /)",
            "test&evil",
            "test evil",
            "test\nevil",
            "../../etc/passwd",
            "test'evil",
            'test"evil',
            "test\\evil",
            "",  # empty string
            "test@evil",
            "test#evil",
            "test$evil",
        ]

        for name in invalid_names:
            self.assertFalse(validate_bot_name(name), f"Should reject: {repr(name)}")


if __name__ == "__main__":
    unittest.main()
