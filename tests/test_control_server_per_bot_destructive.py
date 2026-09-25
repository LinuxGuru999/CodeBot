"""Tests for per-bot destructive endpoint validation (restart, pause) and dry-run support.

Ticket: CB-4437763-EB8F — Destructive control actions lack confirmation and undo affordance

Acceptance Criteria:
- Per-bot destructive endpoints (restart, pause) require force/confirm
- dry-run mode returns preview without executing
- Undo hints are included in success responses
- Unknown bot names are rejected
"""
from __future__ import annotations

import json
import time
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch


class MockBotConfig:
    def __init__(self, name: str):
        self.name = name
        self.model = "test-model"
        self.interval_seconds = 300
        self.heartbeat_timeout = 600


def _make_handler(method: str, path: str, body: dict | None = None):
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
    handler.client_address = ("127.0.0.1", 12345)
    return handler


class TestPerBotRestartDestructiveValidation(unittest.TestCase):
    """Verify that POST /bots/{name}/restart requires force/confirm."""

    def test_restart_without_force_returns_400(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/restart", body={})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]):
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())

    def test_restart_with_force_proceeds(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/restart", body={"force": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]), \
             patch("codebot.control_server.subprocess") as mock_sub, \
             patch("codebot.control_server.time") as mock_time:
            mock_time.sleep = lambda x: None
            mock_sub.run = MagicMock()
            mock_sub.Popen = MagicMock()
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("ok"))

    def test_restart_with_confirm_proceeds(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/restart", body={"confirm": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"confirm": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]), \
             patch("codebot.control_server.subprocess") as mock_sub, \
             patch("codebot.control_server.time") as mock_time:
            mock_time.sleep = lambda x: None
            mock_sub.run = MagicMock()
            mock_sub.Popen = MagicMock()
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("ok"))

    def test_restart_unknown_bot_returns_404(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/unknown-bot/restart", body={"force": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]):
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 404)

    def test_restart_force_false_rejected(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/restart", body={"force": False})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": False}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]):
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)

    def test_restart_includes_undo_hint(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/restart", body={"force": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]), \
             patch("codebot.control_server.subprocess") as mock_sub, \
             patch("codebot.control_server.time") as mock_time:
            mock_time.sleep = lambda x: None
            mock_sub.run = MagicMock()
            mock_sub.Popen = MagicMock()
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertIn("undo", body)
        self.assertIn("respawn", body["undo"].lower())


class TestPerBotPauseDestructiveValidation(unittest.TestCase):
    """Verify that POST /bots/{name}/pause requires force/confirm."""

    def test_pause_without_force_returns_400(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/pause", body={})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]):
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())

    def test_pause_with_force_proceeds(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/pause", body={"force": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]), \
             patch("codebot.control_server.STATE_DIR") as mock_state, \
             patch("codebot.control_server.subprocess") as mock_sub:
            mock_state.__truediv__ = MagicMock(return_value=MagicMock(exists=MagicMock(return_value=False)))
            mock_sub.run = MagicMock()
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("ok"))
        self.assertIn("undo", body)

    def test_pause_with_confirm_proceeds(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/pause", body={"confirm": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"confirm": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]), \
             patch("codebot.control_server.STATE_DIR") as mock_state, \
             patch("codebot.control_server.subprocess") as mock_sub:
            mock_state.__truediv__ = MagicMock(return_value=MagicMock(exists=MagicMock(return_value=False)))
            mock_sub.run = MagicMock()
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)

    def test_pause_includes_undo_hint(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/pause", body={"force": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]), \
             patch("codebot.control_server.STATE_DIR") as mock_state, \
             patch("codebot.control_server.subprocess") as mock_sub:
            mock_state.__truediv__ = MagicMock(return_value=MagicMock(exists=MagicMock(return_value=False)))
            mock_sub.run = MagicMock()
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertIn("undo", body)
        self.assertIn("resume", body["undo"].lower())


class TestPerBotDryRun(unittest.TestCase):
    """Verify that dry-run mode returns preview without executing for per-bot endpoints."""

    def test_restart_dry_run_returns_preview(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/restart", body={"dry_run": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"dry_run": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]):
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("dry_run"))
        self.assertIn("preview", body)
        preview = body["preview"]
        self.assertIn("description", preview)
        self.assertIn("undo", preview)

    def test_pause_dry_run_returns_preview(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/pause", body={"dry_run": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"dry_run": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]):
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("dry_run"))
        self.assertIn("preview", body)

    def test_restart_dry_run_does_not_execute(self):
        """dry-run must not kill any processes or start anything."""
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/restart", body={"dry_run": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"dry_run": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]), \
             patch("codebot.control_server.subprocess") as mock_sub:
            ControlHandler.do_POST(handler)
            mock_sub.run.assert_not_called()
            mock_sub.Popen.assert_not_called()

    def test_pause_dry_run_does_not_write_state(self):
        """dry-run must not write .paused file."""
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/test-bot/pause", body={"dry_run": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"dry_run": True}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]), \
             patch("codebot.control_server.subprocess") as mock_sub, \
             patch("codebot.control_server.STATE_DIR") as mock_state:
            ControlHandler.do_POST(handler)
            mock_sub.run.assert_not_called()
            mock_state.__truediv__.assert_not_called()


class TestDryRunGlobalDestructive(unittest.TestCase):
    """Verify dry-run for global destructive endpoints (stop, drain, update)."""

    def test_stop_dry_run(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/stop", body={"dry_run": True, "bots": ["test-bot"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"dry_run": True, "bots": ["test-bot"]}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", []):
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("dry_run"))
        self.assertIn("preview", body)

    def test_drain_dry_run(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/control/drain", body={"dry_run": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"dry_run": True}, None, None)

        with patch("codebot.control_server.STATE_DIR") as mock_state:
            ControlHandler.do_POST(handler)
            # Must NOT have written .drain file
            mock_state.__truediv__.assert_not_called()

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("dry_run"))

    def test_update_dry_run(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/control/update", body={"dry_run": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"dry_run": True}, None, None)

        with patch("codebot.control_server.SAFE_UPDATE", "/nonexistent"):
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("dry_run"))


class TestUndoHintsInResponses(unittest.TestCase):
    """Verify that destructive endpoint success responses include undo information."""

    def test_stop_undo_hint(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/bots/stop", body={"force": True, "bots": ["test-bot"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True, "bots": ["test-bot"]}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig("test-bot")]), \
             patch("codebot.control_server.subprocess") as mock_sub:
            mock_sub.run = MagicMock()
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertIn("undo", body)

    def test_drain_undo_hint(self):
        from codebot.control_server import ControlHandler
        handler = _make_handler("POST", "/control/drain", body={"confirm": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"confirm": True}, None, None)

        with patch("codebot.control_server.STATE_DIR") as mock_state:
            mock_drain = MagicMock()
            mock_state.__truediv__ = MagicMock(return_value=mock_drain)
            ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertIn("undo", body)
        self.assertIn("clear-drain", body["undo"])


class TestMalformedBotNameInjection(unittest.TestCase):
    """Verify that restart and resume reject malformed/injection-prone bot names with 400 and no Popen.

    Ticket: CB-F6D034E1AC2AD26D8B3A2693268CB7A2
    Acceptance Criteria:
    - Test restart with '--help' payload asserts HTTP 400 and Popen not called
    - Test resume with 'rm -rf /' asserts 400 and Popen not called
    - Test '../etc/passwd' and '.*' payloads assert 400
    - validate_bot_name regex enforcement verified
    """

    def _test_endpoint_rejects_malformed_name(self, endpoint: str, bot_name: str):
        """Helper to test that an endpoint rejects a malformed bot name."""
        from codebot.control_server import ControlHandler
        path = f"/bots/{bot_name}/{endpoint}"
        handler = _make_handler("POST", path, body={"force": True})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True}, None, None)

        with patch("codebot.control_server.subprocess") as mock_sub:
            mock_sub.Popen = MagicMock()
            # Use empty BOT_REGISTRY so we don't need to mock bot lookup
            with patch("codebot.control_server.BOT_REGISTRY", []):
                ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0, f"Expected response for {endpoint} with name {bot_name!r}")
        status_code, body = responses[0]
        self.assertEqual(status_code, 400, f"Expected 400 for {endpoint} with name {bot_name!r}, got {status_code}: {body}")
        self.assertIn("invalid bot name", body.get("error", "").lower(), f"Expected 'invalid bot name' in error for {bot_name!r}")
        # Verify Popen was never called
        mock_sub.Popen.assert_not_called()

    def test_restart_with_help_payload_returns_400_no_popen(self):
        """Test restart with '--help' payload asserts HTTP 400 and Popen not called."""
        self._test_endpoint_rejects_malformed_name("restart", "--help")

    def test_resume_with_rm_rf_payload_returns_400_no_popen(self):
        """Test resume with 'rm -rf /' asserts 400 and Popen not called."""
        self._test_endpoint_rejects_malformed_name("resume", "rm -rf /")

    def test_restart_with_path_traversal_payload_returns_400(self):
        """Test restart with '../etc/passwd' payload asserts 400."""
        self._test_endpoint_rejects_malformed_name("restart", "../etc/passwd")

    def test_resume_with_regex_injection_payload_returns_400(self):
        """Test resume with '.*' payload asserts 400."""
        self._test_endpoint_rejects_malformed_name("resume", ".*")

    def test_restart_with_space_injection_returns_400(self):
        """Test restart with space-containing payload asserts 400."""
        self._test_endpoint_rejects_malformed_name("restart", "bot name with spaces")

    def test_resume_with_semicolon_injection_returns_400(self):
        """Test resume with semicolon injection payload asserts 400."""
        self._test_endpoint_rejects_malformed_name("resume", "bot; rm -rf /")

    def test_restart_with_pipe_injection_returns_400(self):
        """Test restart with pipe injection payload asserts 400."""
        self._test_endpoint_rejects_malformed_name("restart", "bot|cat /etc/passwd")

    def test_resume_with_backtick_injection_returns_400(self):
        """Test resume with backtick injection payload asserts 400."""
        self._test_endpoint_rejects_malformed_name("resume", "bot`whoami`")


class TestRestartResumePopenPositivePath(unittest.TestCase):
    """Verify that restart/resume for a registered bot invokes subprocess.Popen
    with the validated bot name as the sole user-supplied argv element, the
    correct cwd, and without shell=True.

    Ticket: CB-69B105CDDF25D49F3E2D510196581C30
    Acceptance Criteria:
    - Test restart with force=True for registered bot asserts Popen called with
      correct argv list ['python3', orchestrator_path, '--start', validated_name]
    - Test resume for registered bot asserts Popen called with correct argv
    - cwd matches BOTS_DIR
    - No shell=True usage verified
    """

    def _invoke_endpoint(self, endpoint: str, body: dict, registry_name: str = "valid-bot"):
        """Helper that dispatches a POST to /bots/{registry_name}/{endpoint}
        and returns (responses, mock_subprocess, bots_dir_value, orch_value).
        """
        from codebot.control_server import ControlHandler, BOTS_DIR, ORCH

        path = f"/bots/{registry_name}/{endpoint}"
        handler = _make_handler("POST", path, body=body)
        responses: list[tuple[int, dict]] = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda b=body: (b, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", [MockBotConfig(registry_name)]), \
             patch("codebot.control_server.subprocess") as mock_sub, \
             patch("codebot.control_server.time") as mock_time, \
             patch("codebot.control_server._safe_kill_bot_process", return_value=(True, [1234])), \
             patch("codebot.control_server.STATE_DIR") as mock_state:
            mock_time.sleep = lambda x: None
            mock_sub.Popen = MagicMock()
            mock_sub.run = MagicMock()
            # For resume path: ensure .paused exists so unlink is called
            mock_paused = MagicMock()
            mock_paused.exists = MagicMock(return_value=True)
            mock_state.__truediv__ = MagicMock(return_value=mock_paused)

            ControlHandler.do_POST(handler)

        return responses, mock_sub, BOTS_DIR, ORCH

    def test_restart_force_calls_popen_with_validated_name(self):
        """Restart with force=True for a registered bot must call Popen with
        ['python3', orchestrator_path, '--start', validated_name]."""
        responses, mock_sub, bots_dir, orch = self._invoke_endpoint(
            "restart", {"force": True}, registry_name="valid-bot"
        )

        self.assertTrue(responses, "Expected a response from restart endpoint")
        status_code, body = responses[0]
        self.assertEqual(status_code, 200, f"Expected 200, got {status_code}: {body}")

        mock_sub.Popen.assert_called_once()
        args, kwargs = mock_sub.Popen.call_args
        argv = args[0]
        self.assertEqual(
            argv,
            ["python3", str(orch), "--start", "valid-bot"],
            f"Popen argv must be ['python3', orchestrator_path, '--start', validated_name]; got {argv!r}",
        )

    def test_restart_confirm_calls_popen_with_validated_name(self):
        """Restart with confirm=True must also pass validated name to Popen."""
        responses, mock_sub, bots_dir, orch = self._invoke_endpoint(
            "restart", {"confirm": True}, registry_name="my-bot-42"
        )

        self.assertTrue(responses)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)

        mock_sub.Popen.assert_called_once()
        args, kwargs = mock_sub.Popen.call_args
        self.assertEqual(
            args[0],
            ["python3", str(orch), "--start", "my-bot-42"],
        )

    def test_resume_calls_popen_with_validated_name(self):
        """Resume for a registered bot must call Popen with
        ['python3', orchestrator_path, '--start', validated_name]."""
        responses, mock_sub, bots_dir, orch = self._invoke_endpoint(
            "resume", {}, registry_name="valid-bot"
        )

        self.assertTrue(responses, "Expected a response from resume endpoint")
        status_code, body = responses[0]
        self.assertEqual(status_code, 200, f"Expected 200, got {status_code}: {body}")

        mock_sub.Popen.assert_called_once()
        args, kwargs = mock_sub.Popen.call_args
        argv = args[0]
        self.assertEqual(
            argv,
            ["python3", str(orch), "--start", "valid-bot"],
            f"Popen argv must be ['python3', orchestrator_path, '--start', validated_name]; got {argv!r}",
        )

    def test_restart_popen_cwd_matches_bots_dir(self):
        """Restart Popen cwd must equal BOTS_DIR (not /, not user-controlled)."""
        responses, mock_sub, bots_dir, orch = self._invoke_endpoint(
            "restart", {"force": True}, registry_name="cwd-check-bot"
        )

        self.assertTrue(responses)
        self.assertEqual(responses[0][0], 200)

        mock_sub.Popen.assert_called_once()
        _, kwargs = mock_sub.Popen.call_args
        self.assertIn("cwd", kwargs, "Popen must be called with explicit cwd kwarg")
        self.assertEqual(kwargs["cwd"], str(bots_dir))

    def test_resume_popen_cwd_matches_bots_dir(self):
        """Resume Popen cwd must equal BOTS_DIR."""
        responses, mock_sub, bots_dir, orch = self._invoke_endpoint(
            "resume", {}, registry_name="cwd-check-bot"
        )

        self.assertTrue(responses)
        self.assertEqual(responses[0][0], 200)

        mock_sub.Popen.assert_called_once()
        _, kwargs = mock_sub.Popen.call_args
        self.assertIn("cwd", kwargs)
        self.assertEqual(kwargs["cwd"], str(bots_dir))

    def test_restart_popen_no_shell_true(self):
        """Restart must NOT pass shell=True (would enable shell injection)."""
        responses, mock_sub, _, _ = self._invoke_endpoint(
            "restart", {"force": True}, registry_name="no-shell-bot"
        )

        self.assertTrue(responses)
        self.assertEqual(responses[0][0], 200)

        mock_sub.Popen.assert_called_once()
        _, kwargs = mock_sub.Popen.call_args
        self.assertNotIn("shell", kwargs, "Popen must not receive shell kwarg")
        # Also fail loudly if any caller ever passes shell=True as positional
        self.assertFalse(kwargs.get("shell", False), "shell=True is forbidden")

    def test_resume_popen_no_shell_true(self):
        """Resume must NOT pass shell=True."""
        responses, mock_sub, _, _ = self._invoke_endpoint(
            "resume", {}, registry_name="no-shell-bot"
        )

        self.assertTrue(responses)
        self.assertEqual(responses[0][0], 200)

        mock_sub.Popen.assert_called_once()
        _, kwargs = mock_sub.Popen.call_args
        self.assertNotIn("shell", kwargs)
        self.assertFalse(kwargs.get("shell", False), "shell=True is forbidden")

    def test_restart_popen_argv_is_list_not_string(self):
        """Popen argv must be a list (not a shell-joined string)."""
        responses, mock_sub, _, _ = self._invoke_endpoint(
            "restart", {"force": True}, registry_name="list-check"
        )

        self.assertEqual(responses[0][0], 200)
        mock_sub.Popen.assert_called_once()
        args, _ = mock_sub.Popen.call_args
        self.assertIsInstance(args[0], list, "argv must be a list, not a string")

    def test_resume_popen_argv_is_list_not_string(self):
        """Popen argv must be a list (not a shell-joined string)."""
        responses, mock_sub, _, _ = self._invoke_endpoint(
            "resume", {}, registry_name="list-check"
        )

        self.assertEqual(responses[0][0], 200)
        mock_sub.Popen.assert_called_once()
        args, _ = mock_sub.Popen.call_args
        self.assertIsInstance(args[0], list, "argv must be a list, not a string")


if __name__ == "__main__":
    unittest.main()
