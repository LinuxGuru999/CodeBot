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

        malicious_names = [".*", "$(rm -rf /)", "; ls", "| cat /etc/passwd"]

        for name in malicious_names:
            handler = self._make_handler("POST", f"/bots/{name}/pause")
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: (None, None, None)

            with patch("subprocess.run") as mock_run:
                ControlHandler.do_POST(handler)
                mock_run.assert_not_called()

            self.assertTrue(len(responses) > 0, f"Handler must respond for malicious name: {name}")
            status_code, body = responses[0]
            self.assertEqual(status_code, 404,
                             f"Expected 404 for unvalidated bot name '{name}', got {status_code}")
            self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_pkill_not_called_with_unvalidated_input_resume(self):
        """pkill/subprocess must never be called when bot name is not in BOT_REGISTRY (resume)."""
        from codebot.control_server import ControlHandler

        malicious_names = [".*", "$(rm -rf /)", "; ls", "| cat /etc/passwd"]

        for name in malicious_names:
            handler = self._make_handler("POST", f"/bots/{name}/resume")
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: (None, None, None)

            with patch("subprocess.Popen") as mock_popen, patch("subprocess.run") as mock_run:
                ControlHandler.do_POST(handler)
                mock_popen.assert_not_called()
                mock_run.assert_not_called()

            self.assertTrue(len(responses) > 0, f"Handler must respond for malicious name: {name}")
            status_code, body = responses[0]
            self.assertEqual(status_code, 404,
                             f"Expected 404 for unvalidated bot name '{name}', got {status_code}")
            self.assertIn("unknown bot", body.get("error", "").lower())


if __name__ == "__main__":
    unittest.main()
