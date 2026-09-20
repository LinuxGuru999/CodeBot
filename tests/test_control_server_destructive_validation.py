"""Tests for destructive endpoint validation requiring force/confirm field.

Ticket: CB-4748843-2678 — Add server-side validation for destructive endpoints
Ticket: CB-4437763-EB8F — Destructive control actions lack confirmation and undo affordance

Acceptance Criteria:
- control_server.py rejects destructive POSTs without force/confirm field
- returns helpful error message
- tests verify rejection
- dry-run returns preview without executing
- preview includes recovery/undo hints
"""
from __future__ import annotations

import json
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch


class TestDestructiveEndpointValidation(unittest.TestCase):
    """Verify that destructive endpoints require explicit force or confirm field."""

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

    def test_stop_without_force_returns_400(self):
        """POST /bots/stop without force/confirm must return 400 with helpful message."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={"bots": ["test-bot"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["test-bot"]}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())
        self.assertIn("confirm", body.get("error", "").lower())

    def test_drain_without_confirm_returns_400(self):
        """POST /control/drain without force/confirm must return 400."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/control/drain", body={})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())
        self.assertIn("confirm", body.get("error", "").lower())

    def test_update_without_force_returns_400(self):
        """POST /control/update without force/confirm must return 400."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/control/update", body={"version": "latest"})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"version": "latest"}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())
        self.assertIn("confirm", body.get("error", "").lower())

    def test_stop_with_force_true_succeeds(self):
        """POST /bots/stop with force:true proceeds to bot validation."""
        from codebot.control_server import ControlHandler

        # Bot not in registry, so should get 404 after passing force check
        with patch("codebot.control_server.BOT_REGISTRY", []):
            handler = self._make_handler("POST", "/bots/stop", body={"bots": ["test-bot"], "force": True})
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: ({"bots": ["test-bot"], "force": True}, None, None)

            ControlHandler.do_POST(handler)

            self.assertTrue(len(responses) > 0)
            status_code, body = responses[0]
            # Should pass force check and reach bot validation (404 for unknown bot)
            self.assertEqual(status_code, 404)
            self.assertIn("unknown bot", body.get("error", "").lower())

    def test_drain_with_confirm_true_succeeds(self):
        """POST /control/drain with confirm:true proceeds past validation."""
        from codebot.control_server import ControlHandler

        with patch("codebot.control_server.STATE_DIR") as mock_state_dir:
            mock_drain_file = MagicMock()
            mock_state_dir.__truediv__.return_value = mock_drain_file

            handler = self._make_handler("POST", "/control/drain", body={"confirm": True})
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: ({"confirm": True}, None, None)

            ControlHandler.do_POST(handler)

            self.assertTrue(len(responses) > 0)
            status_code, body = responses[0]
            # Should pass force check and succeed (200)
            self.assertEqual(status_code, 200)
            self.assertTrue(body.get("ok"))

    def test_stop_with_confirm_true_succeeds(self):
        """POST /bots/stop with confirm:true proceeds to bot validation."""
        from codebot.control_server import ControlHandler

        with patch("codebot.control_server.BOT_REGISTRY", []):
            handler = self._make_handler("POST", "/bots/stop", body={"bots": ["test-bot"], "confirm": True})
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: ({"bots": ["test-bot"], "confirm": True}, None, None)

            ControlHandler.do_POST(handler)

            self.assertTrue(len(responses) > 0)
            status_code, body = responses[0]
            self.assertEqual(status_code, 404)
            self.assertIn("unknown bot", body.get("error", "").lower())

    def test_empty_body_rejected(self):
        """Empty JSON body {} must be rejected (no force/confirm key)."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())

    def test_force_false_rejected(self):
        """force:false must be rejected (falsy value)."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={"force": False})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": False}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())

    def test_confirm_false_rejected(self):
        """confirm:false must be rejected (falsy value)."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/control/drain", body={"confirm": False})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"confirm": False}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())

    def test_api_prefix_paths_also_validated(self):
        """Path variants with /api/ prefix must also be covered."""
        from codebot.control_server import ControlHandler

        for path in ["/api/bots/stop", "/api/control/drain", "/api/control/update"]:
            handler = self._make_handler("POST", path, body={})
            responses = []
            handler._json = lambda code, data, r=responses: r.append((code, data))
            handler._auth = lambda: True
            handler._read_json_body = lambda: ({}, None, None)

            ControlHandler.do_POST(handler)

            self.assertTrue(len(responses) > 0, f"No response for {path}")
            status_code, body = responses[0]
            self.assertEqual(status_code, 400, f"Expected 400 for {path}")
            self.assertIn("force", body.get("error", "").lower())

    def test_non_destructive_endpoints_not_affected(self):
        """Non-destructive endpoints should not require force/confirm."""
        from codebot.control_server import ControlHandler

        # /health is GET, but let's test a POST that's not destructive
        # /bots/start is not in DESTRUCTIVE_PATHS
        handler = self._make_handler("POST", "/bots/start", body={"bots": ["test"]})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["test"]}, None, None)

        with patch("codebot.control_server.BOT_REGISTRY", []):
            ControlHandler.do_POST(handler)

        # Should not be blocked by destructive endpoint guard
        # (may fail for other reasons like empty registry, but not 400 for force/confirm)
        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        # Should not be the destructive endpoint error
        error_msg = body.get("error", "").lower()
        self.assertNotIn("force", error_msg)
        self.assertNotIn("confirm", error_msg)

    def test_force_string_rejected(self):
        """force:'yes' (string, not bool) must be rejected."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={"force": "yes"})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": "yes"}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())

    def test_force_int_rejected(self):
        """force:1 (int, not bool) must be rejected to prevent bypass via JSON number."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={"force": 1})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": 1}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())

    def test_force_none_rejected(self):
        """force:null must be rejected."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={"force": None})
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": None}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)


class TestDryRunPreview(unittest.TestCase):
    """Verify that dry_run mode returns a preview without executing the action."""

    def _make_handler(self, method: str, path: str, body: dict | None = None):
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

    def test_stop_dry_run_returns_preview(self):
        """POST /bots/stop with dry_run:true returns preview without executing."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={
            "bots": ["issues", "features"],
            "force": True,
            "dry_run": True,
        })
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"bots": ["issues", "features"], "force": True, "dry_run": True}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("ok"))
        self.assertTrue(body.get("dry_run"))
        preview = body.get("preview", {})
        self.assertIn("description", preview)
        self.assertIn("affected_bots", preview)

    def test_drain_dry_run_returns_preview_with_recovery(self):
        """POST /control/drain with dry_run:true includes recovery info."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/control/drain", body={
            "force": True,
            "dry_run": True,
        })
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True, "dry_run": True}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("dry_run"))
        preview = body.get("preview", {})
        self.assertIn("recovery", preview)
        self.assertIn("undo_command", preview)

    def test_update_dry_run_returns_preview_with_warning(self):
        """POST /control/update with dry_run:true includes warning."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/control/update", body={
            "force": True,
            "dry_run": True,
        })
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True, "dry_run": True}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        self.assertTrue(body.get("dry_run"))
        preview = body.get("preview", {})
        self.assertIn("warning", preview)
        self.assertIn("recovery", preview)

    def test_stop_all_dry_run_warning(self):
        """POST /bots/stop with no bots and dry_run warns about fleet halt."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={
            "force": True,
            "dry_run": True,
        })
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"force": True, "dry_run": True}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 200)
        preview = body.get("preview", {})
        self.assertEqual(preview.get("affected_bots"), "all")
        self.assertIn("warning", preview)

    def test_dry_run_without_force_still_rejected(self):
        """dry_run:true without force/confirm should still be rejected (must confirm intent first)."""
        from codebot.control_server import ControlHandler

        handler = self._make_handler("POST", "/bots/stop", body={
            "dry_run": True,
        })
        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: ({"dry_run": True}, None, None)

        ControlHandler.do_POST(handler)

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)

    def test_get_destructive_preview_stop_with_bots(self):
        """_get_destructive_preview returns correct preview for stop with specific bots."""
        from codebot.control_server import ControlHandler

        preview = ControlHandler._get_destructive_preview(
            None, "/bots/stop", {"bots": ["bug_fix", "issues"]}
        )
        self.assertIn("description", preview)
        self.assertIn("affected_bots", preview)
        self.assertEqual(preview["affected_bots"], ["bug_fix", "issues"])
        self.assertTrue(preview["would_execute"])

    def test_get_destructive_preview_drain(self):
        """_get_destructive_preview returns recovery info for drain."""
        from codebot.control_server import ControlHandler

        preview = ControlHandler._get_destructive_preview(
            None, "/control/drain", {}
        )
        self.assertIn("recovery", preview)
        self.assertIn("undo_command", preview)
        self.assertIn("clear-drain", preview["undo_command"])

    def test_get_destructive_preview_update(self):
        """_get_destructive_preview returns warning for update."""
        from codebot.control_server import ControlHandler

        preview = ControlHandler._get_destructive_preview(
            None, "/control/update", {}
        )
        self.assertIn("warning", preview)
        self.assertIn("recovery", preview)


if __name__ == "__main__":
    unittest.main()


if __name__ == "__main__":
    unittest.main()
