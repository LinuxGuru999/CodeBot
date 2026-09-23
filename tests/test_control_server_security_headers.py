"""Tests for security headers on all control_server.py HTTP responses.

Ticket: CB-6048526-4930 — Missing security headers on all control_server.py
HTTP responses (Constitution §2 violation).

Verifies that X-Content-Type-Options: nosniff, X-Frame-Options: DENY, and
Referrer-Policy: no-referrer are present on /health, /bots, and error responses.
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
import unittest
from http.client import HTTPConnection
from unittest.mock import patch

# Set CONTROL_TOKEN before importing control_server so auth works in tests
os.environ["CONTROL_TOKEN"] = "test-token-for-security-headers"
os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)

import importlib
import codebot.control_server as _cs_mod
importlib.reload(_cs_mod)
from codebot.control_server import ControlHandler, ThreadingHTTPServer, PORT


def _find_free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestSecurityHeadersOnResponses(unittest.TestCase):
    """Verify required security headers are present on all HTTP responses."""

    @classmethod
    def setUpClass(cls):
        """Start a test HTTP server."""
        from codebot.control_server import _rate_limiter, _health_rate_limiter
        for _lim in (_rate_limiter, _health_rate_limiter):
            try:
                with _lim._lock:
                    _lim._failures.pop("127.0.0.1", None)
                    _lim._blocked_until.pop("127.0.0.1", None)
            except Exception:
                pass
        cls.port = _find_free_port()
        cls.server = ThreadingHTTPServer(("127.0.0.1", cls.port), ControlHandler)
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # Give server a moment to start
        time.sleep(0.1)

    def setUp(self):
        """Reset global rate limiters AND re-sync CONTROL_TOKEN.

        Earlier test modules reload codebot.control_server with different
        CONTROL_TOKEN values (module-global, read at import time). This
        module's server was started with "test-token-for-security-headers",
        so re-assert the env var and module global before each test; also
        clear rate limiter state so tests are order-independent.
        """
        os.environ["CONTROL_TOKEN"] = "test-token-for-security-headers"
        os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)
        import codebot.control_server as _cs_live
        _cs_live.CONTROL_TOKEN = "test-token-for-security-headers"
        from codebot.control_server import _rate_limiter, _health_rate_limiter
        for _lim in (_rate_limiter, _health_rate_limiter):
            try:
                with _lim._lock:
                    _lim._failures.pop("127.0.0.1", None)
                    _lim._blocked_until.pop("127.0.0.1", None)
            except Exception:
                pass

    @classmethod
    def tearDownClass(cls):
        """Shut down the test HTTP server."""
        cls.server.shutdown()
        cls.thread.join(timeout=5)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, dict, str]:
        """Make a GET request and return (status, response_headers_dict, body)."""
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            req_headers = headers or {}
            conn.request("GET", path, headers=req_headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            body = resp.read().decode("utf-8", errors="replace")
            return status, resp_headers, body
        finally:
            conn.close()

    def _post(self, path: str, body: dict | None = None, headers: dict | None = None) -> tuple[int, dict, str]:
        """Make a POST request and return (status, response_headers_dict, body)."""
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            req_headers = headers or {}
            payload = json.dumps(body or {}).encode()
            req_headers["Content-Type"] = "application/json"
            conn.request("POST", path, body=payload, headers=req_headers)
            resp = conn.getresponse()
            status = resp.status
            resp_headers = {k.lower(): v for k, v in resp.getheaders()}
            resp_body = resp.read().decode("utf-8", errors="replace")
            return status, resp_headers, resp_body
        finally:
            conn.close()

    def _assert_security_headers(self, resp_headers: dict, context: str):
        """Assert all three required security headers are present."""
        self.assertEqual(
            resp_headers.get("x-content-type-options"),
            "nosniff",
            f"X-Content-Type-Options: nosniff missing on {context}",
        )
        self.assertEqual(
            resp_headers.get("x-frame-options"),
            "DENY",
            f"X-Frame-Options: DENY missing on {context}",
        )
        self.assertEqual(
            resp_headers.get("referrer-policy"),
            "no-referrer",
            f"Referrer-Policy: no-referrer missing on {context}",
        )

    def test_health_endpoint_has_security_headers(self):
        """GET /health must include all security headers."""
        status, headers, body = self._get("/health")
        self.assertEqual(status, 200)
        self._assert_security_headers(headers, "GET /health (200)")

    def test_bots_endpoint_has_security_headers(self):
        """GET /bots with valid auth must include all security headers."""
        auth_headers = {"Authorization": "Bearer test-token-for-security-headers"}
        status, headers, body = self._get("/bots", headers=auth_headers)
        self.assertEqual(status, 200)
        self._assert_security_headers(headers, "GET /bots (200)")

    def test_unauthorized_response_has_security_headers(self):
        """401 responses must include all security headers."""
        status, headers, body = self._get("/bots")  # No auth header
        self.assertEqual(status, 401)
        self._assert_security_headers(headers, "GET /bots (401 unauthorized)")

    def test_not_found_response_has_security_headers(self):
        """404 responses must include all security headers."""
        auth_headers = {"Authorization": "Bearer test-token-for-security-headers"}
        status, headers, body = self._get("/nonexistent/path", headers=auth_headers)
        self.assertEqual(status, 404)
        self._assert_security_headers(headers, "GET /nonexistent (404)")

    def test_post_unauthorized_has_security_headers(self):
        """POST 401 responses must include all security headers."""
        status, headers, body = self._post("/bots/start", body={})
        self.assertEqual(status, 401)
        self._assert_security_headers(headers, "POST /bots/start (401)")

    def test_wrong_token_has_security_headers(self):
        """Auth failure with wrong token must still include security headers."""
        auth_headers = {"Authorization": "Bearer wrong-token"}
        status, headers, body = self._get("/bots", headers=auth_headers)
        self.assertEqual(status, 401)
        self._assert_security_headers(headers, "GET /bots (401 wrong token)")

    def test_internal_error_response_has_security_headers(self):
        """500 responses must include all security headers.

        Triggers a 500 by mocking bot_status to raise, verifying that even
        unhandled exceptions produce responses with Constitution §2 headers.
        """
        auth_headers = {"Authorization": "Bearer test-token-for-security-headers"}
        with patch("codebot.control_server.bot_status", side_effect=RuntimeError("simulated failure")):
            status, headers, body = self._get("/bots", headers=auth_headers)
        # The handler should catch the exception and return 500 via _json or send_error
        self.assertEqual(status, 500, f"Expected 500 but got {status}: {body}")
        self._assert_security_headers(headers, "GET /bots (500 internal error)")


if __name__ == "__main__":
    unittest.main()
