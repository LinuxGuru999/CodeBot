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
        self.assertIn("restart", body["undo"].lower())


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


if __name__ == "__main__":
    unittest.main()
