"""Tests for server-side validation of destructive POST endpoints.

Ticket: CB-4037132-06FF — Add server-side validation for destructive endpoints
Requires explicit 'force' or 'confirm' field in JSON body for POST /bots/stop,
POST /control/drain, POST /control/update, and similar destructive endpoints.
"""
from __future__ import annotations

import json
import unittest
from io import BytesIO
from unittest.mock import MagicMock, patch


class TestDestructiveEndpointValidation(unittest.TestCase):
    """Verify that destructive endpoints require force or confirm field."""

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

    def _call_do_POST(self, handler, body):
        """Call do_POST with mocked _auth and _read_json_body returning the given body."""
        from codebot.control_server import ControlHandler

        responses = []
        handler._json = lambda code, data, r=responses: r.append((code, data))
        handler._auth = lambda: True
        handler._read_json_body = lambda: (body, None, None)

        ControlHandler.do_POST(handler)
        return responses

    # --- /bots/stop ---

    def test_stop_without_force_returns_400(self):
        """POST /bots/stop without force/confirm must return 400."""
        handler = self._make_handler("POST", "/bots/stop", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0, "Handler must produce a response")
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())
        self.assertIn("confirm", body.get("error", "").lower())

    def test_stop_with_force_true_succeeds(self):
        """POST /bots/stop with force:true should proceed past validation."""
        handler = self._make_handler("POST", "/bots/stop", body={"force": True})
        responses = self._call_do_POST(handler, {"force": True})

        self.assertTrue(len(responses) > 0, "Handler must produce a response")
        status_code, _ = responses[0]
        # Should NOT be 400 (passes validation; may get other status from downstream logic)
        self.assertNotEqual(status_code, 400)

    def test_stop_with_confirm_true_succeeds(self):
        """POST /bots/stop with confirm:true should proceed past validation."""
        handler = self._make_handler("POST", "/bots/stop", body={"confirm": True})
        responses = self._call_do_POST(handler, {"confirm": True})

        self.assertTrue(len(responses) > 0, "Handler must produce a response")
        status_code, _ = responses[0]
        self.assertNotEqual(status_code, 400)

    def test_stop_with_force_false_returns_400(self):
        """POST /bots/stop with force:false must return 400."""
        handler = self._make_handler("POST", "/bots/stop", body={"force": False})
        responses = self._call_do_POST(handler, {"force": False})

        self.assertTrue(len(responses) > 0, "Handler must produce a response")
        status_code, _ = responses[0]
        self.assertEqual(status_code, 400)

    def test_stop_with_confirm_false_returns_400(self):
        """POST /bots/stop with confirm:false must return 400."""
        handler = self._make_handler("POST", "/bots/stop", body={"confirm": False})
        responses = self._call_do_POST(handler, {"confirm": False})

        self.assertTrue(len(responses) > 0, "Handler must produce a response")
        status_code, _ = responses[0]
        self.assertEqual(status_code, 400)

    def test_stop_with_empty_body_returns_400(self):
        """POST /bots/stop with empty body (no keys) must return 400."""
        handler = self._make_handler("POST", "/bots/stop", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertEqual(status_code, 400)

    # --- /control/drain ---

    def test_drain_without_confirm_returns_400(self):
        """POST /control/drain without force/confirm must return 400."""
        handler = self._make_handler("POST", "/control/drain", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())

    def test_drain_with_force_succeeds(self):
        """POST /control/drain with force:true should proceed past validation."""
        handler = self._make_handler("POST", "/control/drain", body={"force": True})
        responses = self._call_do_POST(handler, {"force": True})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertNotEqual(status_code, 400)

    def test_drain_with_confirm_succeeds(self):
        """POST /control/drain with confirm:true should proceed past validation."""
        handler = self._make_handler("POST", "/control/drain", body={"confirm": True})
        responses = self._call_do_POST(handler, {"confirm": True})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertNotEqual(status_code, 400)

    # --- /control/update ---

    def test_update_without_force_returns_400(self):
        """POST /control/update without force/confirm must return 400."""
        handler = self._make_handler("POST", "/control/update", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0)
        status_code, body = responses[0]
        self.assertEqual(status_code, 400)
        self.assertIn("force", body.get("error", "").lower())

    def test_update_with_force_succeeds(self):
        """POST /control/update with force:true should proceed past validation."""
        handler = self._make_handler("POST", "/control/update", body={"force": True})
        responses = self._call_do_POST(handler, {"force": True})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertNotEqual(status_code, 400)

    def test_update_with_confirm_succeeds(self):
        """POST /control/update with confirm:true should proceed past validation."""
        handler = self._make_handler("POST", "/control/update", body={"confirm": True})
        responses = self._call_do_POST(handler, {"confirm": True})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertNotEqual(status_code, 400)

    # --- /api/ prefix variants ---

    def test_api_prefix_stop_without_force_returns_400(self):
        """POST /api/bots/stop without force/confirm must return 400."""
        handler = self._make_handler("POST", "/api/bots/stop", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertEqual(status_code, 400)

    def test_api_prefix_drain_without_force_returns_400(self):
        """POST /api/control/drain without force/confirm must return 400."""
        handler = self._make_handler("POST", "/api/control/drain", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertEqual(status_code, 400)

    def test_api_prefix_update_without_force_returns_400(self):
        """POST /api/control/update without force/confirm must return 400."""
        handler = self._make_handler("POST", "/api/control/update", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertEqual(status_code, 400)

    # --- /control/stop variant ---

    def test_control_stop_without_force_returns_400(self):
        """POST /control/stop without force/confirm must return 400."""
        handler = self._make_handler("POST", "/control/stop", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        self.assertEqual(status_code, 400)

    # --- Error message quality ---

    def test_error_message_instructs_user(self):
        """Error message should instruct the user to use --force or confirm field."""
        handler = self._make_handler("POST", "/bots/stop", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0)
        _, body = responses[0]
        error = body.get("error", "")
        self.assertIn("--force", error, "Error message should mention --force flag")
        self.assertIn("confirm", error, "Error message should mention confirm field")

    # --- Non-destructive endpoints not affected ---

    def test_non_destructive_endpoints_not_affected(self):
        """Non-destructive POST endpoints should not require force/confirm."""
        # /bots/start is not in DESTRUCTIVE_PATHS
        handler = self._make_handler("POST", "/bots/test-bot/restart", body={})
        responses = self._call_do_POST(handler, {})

        self.assertTrue(len(responses) > 0)
        status_code, _ = responses[0]
        # Should NOT be 400 for missing force/confirm
        self.assertNotEqual(status_code, 400)


if __name__ == "__main__":
    unittest.main()
