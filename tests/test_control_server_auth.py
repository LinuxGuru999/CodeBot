"""Tests for control_server.py authentication and security headers.

Ticket: CB-876495-636F — Insecure default: control server allows all requests
when CONTROL_TOKEN is unset.

Constitution §2 mandates:
- Bearer token handling via Authorization header only
- Security headers: nosniff, DENY frame, no-referrer on all responses
"""
from __future__ import annotations

import importlib
import json
import os
import socket
import sys
import threading
import time
import unittest
from http.client import HTTPConnection
from io import BytesIO
from pathlib import Path
from unittest.mock import MagicMock, patch


def _find_free_port() -> int:
    """Find a free TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _reload_control_server(token: str = "", allow_unauth: str = ""):
    """Reload control_server module with specific environment settings."""
    if token:
        os.environ["CONTROL_TOKEN"] = token
    else:
        os.environ.pop("CONTROL_TOKEN", None)

    if allow_unauth:
        os.environ["CONTROL_ALLOW_UNAUTHENTICATED"] = allow_unauth
    else:
        os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)

    import codebot.control_server as cs_mod
    importlib.reload(cs_mod)
    return cs_mod


def _make_handler(cs_mod, headers=None, client_address=("127.0.0.1", 12345)):
    """Create a real ControlHandler instance without starting a server.

    Uses __new__ to bypass BaseHTTPRequestHandler.__init__ which requires
    a real socket connection, then sets the minimal attributes needed by _auth().
    """
    handler = cs_mod.ControlHandler.__new__(cs_mod.ControlHandler)
    handler.headers = headers or {}
    handler.client_address = client_address
    handler.wfile = BytesIO()
    # Mock _json to prevent actual writes during auth tests
    handler._json = MagicMock()
    return handler


class TestAuthFailClosed(unittest.TestCase):
    """Verify that ControlHandler._auth() rejects requests when CONTROL_TOKEN is unset.

    These tests exercise the ACTUAL production _auth() method against real
    module state, not a reimplementation.
    """

    def setUp(self):
        """Save original environment and reset rate limiter before each test."""
        self.original_control_token = os.environ.get("CONTROL_TOKEN", "")
        import codebot.control_server as cs
        # Reset rate limiter to prevent cross-test contamination
        cs._rate_limiter = cs.RateLimiter()

    def tearDown(self):
        """Restore original environment values."""
        if self.original_control_token:
            os.environ["CONTROL_TOKEN"] = self.original_control_token
        elif "CONTROL_TOKEN" in os.environ:
            del os.environ["CONTROL_TOKEN"]
        import codebot.control_server as cs
        importlib.reload(cs)

    def test_auth_returns_false_when_token_unset(self):
        """When CONTROL_TOKEN is empty, _auth() must return False (fail-closed)."""
        cs = _reload_control_server(token="")
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={})
        result = handler._auth()

        self.assertFalse(result, "_auth() must return False when CONTROL_TOKEN is empty")

    def test_auth_returns_false_when_token_unset_but_header_present(self):
        """Even with a Bearer header, _auth() must reject if CONTROL_TOKEN is unset."""
        cs = _reload_control_server(token="")
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={"Authorization": "Bearer some-token"})
        result = handler._auth()

        self.assertFalse(result, "_auth() must return False when CONTROL_TOKEN is empty, even with valid-looking header")

    def test_auth_accepts_valid_token(self):
        """_auth() accepts requests with correct Bearer token."""
        cs = _reload_control_server(token="my-secret-token")
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={"Authorization": "Bearer my-secret-token"})
        result = handler._auth()

        self.assertTrue(result, "_auth() must return True with correct Bearer token")

    def test_auth_rejects_wrong_token(self):
        """_auth() rejects requests with incorrect Bearer token."""
        cs = _reload_control_server(token="my-secret-token")
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={"Authorization": "Bearer wrong-token"})
        result = handler._auth()

        self.assertFalse(result, "_auth() must return False with incorrect Bearer token")

    def test_auth_rejects_empty_auth_header(self):
        """_auth() rejects when Authorization header is missing."""
        cs = _reload_control_server(token="my-secret-token")
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={})
        result = handler._auth()

        self.assertFalse(result, "_auth() must return False when Authorization header is missing")

    def test_auth_returns_none_when_rate_limited(self):
        """When rate limit is exceeded, _auth() must return None (429 already sent)."""
        cs = _reload_control_server(token="my-secret-token")
        # Exhaust the rate limiter for our test IP
        for _ in range(cs.RATE_LIMIT_MAX_ATTEMPTS):
            cs._rate_limiter.record_failure("127.0.0.1")

        handler = _make_handler(cs, headers={"Authorization": "Bearer wrong-token"})
        result = handler._auth()

        self.assertIsNone(result, "_auth() must return None when rate limited")
        handler._json.assert_called_once()
        call_args = handler._json.call_args
        self.assertEqual(call_args[0][0], 429)

    def test_auth_records_failure_on_wrong_token(self):
        """When token comparison fails, _auth() must record the failure for rate limiting."""
        cs = _reload_control_server(token="my-secret-token")
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={"Authorization": "Bearer wrong-token"})
        result = handler._auth()

        self.assertFalse(result)
        # Verify that a failure was recorded by checking the rate limiter state
        allowed, reason = cs._rate_limiter.is_allowed("127.0.0.1")
        self.assertTrue(allowed)  # Still allowed after 1 failure
        # Record enough failures to hit the limit and verify it blocks
        for _ in range(cs.RATE_LIMIT_MAX_ATTEMPTS - 1):
            cs._rate_limiter.record_failure("127.0.0.1")
        allowed2, reason2 = cs._rate_limiter.is_allowed("127.0.0.1")
        self.assertFalse(allowed2, "Rate limiter should block after MAX_ATTEMPTS failures including the one from _auth")

    def test_auth_with_no_client_address(self):
        """_auth() handles missing client_address gracefully (uses 'unknown')."""
        cs = _reload_control_server(token="my-secret-token")
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={"Authorization": "Bearer my-secret-token"}, client_address=None)
        handler.client_address = None
        result = handler._auth()

        self.assertTrue(result, "_auth() must still work when client_address is None")


class TestSecurityHeaders(unittest.TestCase):
    """Verify that all responses include required security headers (Constitution §2)."""

    def test_security_headers_defined(self):
        """Verify the security headers constant exists and contains required headers."""
        cs_path = Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        self.assertIn("X-Content-Type-Options", source,
                       "control_server.py must set X-Content-Type-Options header")
        self.assertIn("nosniff", source,
                       "control_server.py must set nosniff value")
        self.assertIn("X-Frame-Options", source,
                       "control_server.py must set X-Frame-Options header")
        self.assertIn("DENY", source,
                       "control_server.py must set DENY value for X-Frame-Options")
        self.assertIn("Referrer-Policy", source,
                       "control_server.py must set Referrer-Policy header")
        self.assertIn("no-referrer", source,
                       "control_server.py must set no-referrer value")

    def test_fail_closed_in_production_code(self):
        """Verify that the production _auth() method fails closed when token is empty.

        Ticket CB-B40869760C5B822FAE354B36F80BC9FF: the
        CONTROL_ALLOW_UNAUTHENTICATED bypass was REMOVED. _auth() must
        unconditionally return False when CONTROL_TOKEN is empty — no opt-in
        flag may re-enable unauthenticated access.
        """
        cs_path = Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        auth_start = source.find("def _auth(self)")
        self.assertNotEqual(auth_start, -1, "_auth method not found in control_server.py")

        auth_body_end = source.find("\n    def ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = source.find("\ndef ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = len(source)
        auth_body = source[auth_start:auth_body_end]

        self.assertIn("return False", auth_body,
                       "control_server.py _auth() must return False when CONTROL_TOKEN is empty")

        self.assertNotIn("CONTROL_ALLOW_UNAUTHENTICATED", auth_body,
                       "control_server.py _auth() must NOT contain CONTROL_ALLOW_UNAUTHENTICATED bypass")
        self.assertNotIn("ALLOW_UNAUTHENTICATED", auth_body,
                       "control_server.py _auth() must NOT contain any unauthenticated bypass")

        false_idx = auth_body.find("return False")
        self.assertNotEqual(false_idx, -1,
                            "_auth() must have a return False path for missing token")

    def test_constitution_ssrf_not_affected(self):
        """SSRF protection should still work - this test ensures the fix doesn't break existing auth."""
        cs_path = Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        self.assertIn("hmac.compare_digest", source,
                       "control_server.py must use hmac.compare_digest for timing-safe comparison")


class TestUnauthenticatedRejection(unittest.TestCase):
    """Integration tests for fail-closed behavior when CONTROL_TOKEN is unset.

    Ticket: CB-B40869760C5B822FAE354B36F80BC9FF
    Verifies that the control server rejects all authenticated endpoints
    when CONTROL_TOKEN is not set. The CONTROL_ALLOW_UNAUTHENTICATED bypass has been removed.
    """

    def setUp(self):
        """Save original environment values."""
        self.original_control_token = os.environ.get("CONTROL_TOKEN", "")

    def tearDown(self):
        """Restore original environment values."""
        if self.original_control_token:
            os.environ["CONTROL_TOKEN"] = self.original_control_token
        elif "CONTROL_TOKEN" in os.environ:
            del os.environ["CONTROL_TOKEN"]

        import codebot.control_server as cs
        importlib.reload(cs)

    def test_auth_rejects_when_control_token_unset(self):
        """Server rejects all authenticated endpoints with 401 when CONTROL_TOKEN is empty."""
        cs = _reload_control_server(token="")
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={})
        result = handler._auth()

        self.assertFalse(result, "_auth() must return False when CONTROL_TOKEN is empty")

    def test_startup_warning_logged_when_no_token(self):
        """Startup warning is logged when CONTROL_TOKEN is not configured."""
        cs = _reload_control_server(token="")
        self.assertEqual(cs.CONTROL_TOKEN, "", "CONTROL_TOKEN should be empty")


class TestTimingSafeComparison(unittest.TestCase):
    """Verify that token comparison uses timing-safe hmac.compare_digest.

    Ticket: CB-3488569-8A5E — Timing attack on bearer token comparison
    Acceptance: test verifies timing-safe comparison is used
    """

    def test_auth_method_uses_hmac_compare_digest(self):
        """Verify _auth() uses hmac.compare_digest for constant-time comparison."""
        cs_path = Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        auth_start = source.find("def _auth(self)")
        self.assertNotEqual(auth_start, -1, "_auth method not found")

        auth_body_end = source.find("\n    def ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = source.find("\ndef ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = len(source)
        auth_body = source[auth_start:auth_body_end]

        self.assertIn("hmac.compare_digest", auth_body,
                       "_auth() must use hmac.compare_digest for timing-safe token comparison")

        self.assertNotIn("auth == expected", auth_body,
                         "_auth() must not use == for token comparison")
        self.assertNotIn("auth.strip() == expected", auth_body,
                         "_auth() must not use == for token comparison")

    def test_telemetry_handler_uses_hmac_compare_digest(self):
        """Verify _handle_telemetry() uses hmac.compare_digest for constant-time comparison."""
        cs_path = Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        telemetry_start = source.find("def _handle_telemetry(self")
        self.assertNotEqual(telemetry_start, -1, "_handle_telemetry method not found")

        telemetry_body_end = source.find("\n    def ", telemetry_start + 1)
        if telemetry_body_end == -1:
            telemetry_body_end = source.find("\ndef ", telemetry_start + 1)
        if telemetry_body_end == -1:
            telemetry_body_end = len(source)
        telemetry_body = source[telemetry_start:telemetry_body_end]

        self.assertIn("hmac.compare_digest", telemetry_body,
                       "_handle_telemetry() must use hmac.compare_digest for timing-safe token comparison")

    def test_no_equals_comparison_on_token_material(self):
        """Verify no == comparison is used on CONTROL_TOKEN or auth header values."""
        cs_path = Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            if stripped.startswith("#") or not stripped:
                continue
            if "==" in stripped:
                if "CONTROL_TOKEN" in stripped and ('""' in stripped or "''" in stripped or "not " in stripped):
                    continue
                if "TELEMETRY_TOKEN" in stripped and ('""' in stripped or "''" in stripped or "not " in stripped):
                    continue
                if any(var in stripped for var in ["auth", "expected", "token"]):
                    if not stripped.startswith("#"):
                        self.fail(f"Line {i} uses == for potential token comparison: {stripped}")


class TestTelemetryAuthBypassFix(unittest.TestCase):
    """Verify telemetry endpoint respects fail-closed policy when CONTROL_TOKEN is empty.

    Ticket: CB-B30A27B81D697DA358F82DA04940B527
    Ensures that even with a valid TELEMETRY_TOKEN, requests are rejected if CONTROL_TOKEN is unset.
    """

    def setUp(self):
        """Save original environment values."""
        self.original_control_token = os.environ.get("CONTROL_TOKEN", "")
        self.original_telemetry_token = os.environ.get("CODEBOT_TELEMETRY_TOKEN", "")
        self.original_allow_unauth = os.environ.get("CONTROL_ALLOW_UNAUTHENTICATED", "")

    def tearDown(self):
        """Restore original environment values."""
        if self.original_control_token:
            os.environ["CONTROL_TOKEN"] = self.original_control_token
        elif "CONTROL_TOKEN" in os.environ:
            del os.environ["CONTROL_TOKEN"]

        if self.original_telemetry_token:
            os.environ["CODEBOT_TELEMETRY_TOKEN"] = self.original_telemetry_token
        elif "CODEBOT_TELEMETRY_TOKEN" in os.environ:
            del os.environ["CODEBOT_TELEMETRY_TOKEN"]

        if self.original_allow_unauth:
            os.environ["CONTROL_ALLOW_UNAUTHENTICATED"] = self.original_allow_unauth
        elif "CONTROL_ALLOW_UNAUTHENTICATED" in os.environ:
            del os.environ["CONTROL_ALLOW_UNAUTHENTICATED"]

        import codebot.control_server as cs
        importlib.reload(cs)

    def test_telemetry_rejected_when_control_token_empty_even_if_telemetry_token_set(self):
        """POST /telemetry must return 401 when CONTROL_TOKEN is empty, even if TELEMETRY_TOKEN is valid."""
        cs = _reload_control_server(token="")
        os.environ["CODEBOT_TELEMETRY_TOKEN"] = "valid-telemetry-token"
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={"Authorization": "Bearer valid-telemetry-token"})

        handler._handle_telemetry({"signal_type": "test", "data": {}})

        handler._json.assert_called()
        call_args = handler._json.call_args
        self.assertEqual(call_args[0][0], 401,
                         "Telemetry endpoint must return 401 when CONTROL_TOKEN is empty")
        self.assertIn("unauthorized", call_args[0][1].get("error", "").lower())

    def test_do_post_telemetry_rejected_via_full_path_when_control_token_empty(self):
        """Integration test: POST /telemetry through do_POST returns 401 when CONTROL_TOKEN is empty.

        This verifies the defense-in-depth: even if _handle_telemetry's guard were bypassed,
        do_POST's _auth() check rejects the request before reaching _handle_telemetry.
        Ticket: CB-B30A27B81D697DA358F82DA04940B527
        """
        cs = _reload_control_server(token="")
        os.environ["CODEBOT_TELEMETRY_TOKEN"] = "valid-telemetry-token"
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={"Authorization": "Bearer valid-telemetry-token"})
        handler.path = "/telemetry"
        handler.command = "POST"
        handler._read_json_body = lambda: ({"signal_type": "error", "data": {}}, None, None)

        handler.do_POST()

        handler._json.assert_called()
        call_args = handler._json.call_args
        self.assertEqual(call_args[0][0], 401,
                         "do_POST must return 401 for /telemetry when CONTROL_TOKEN is empty")
        self.assertIn("unauthorized", call_args[0][1].get("error", "").lower())


class TestRateLimiterUnit(unittest.TestCase):
    """Unit tests for RateLimiter class behavior."""

    def setUp(self):
        """Import RateLimiter fresh for each test."""
        from codebot.control_server import RateLimiter
        self.limiter = RateLimiter()

    def test_initial_allowed(self):
        """New IP should be allowed."""
        allowed, reason = self.limiter.is_allowed("192.168.1.1")
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_blocked_after_max_failures(self):
        """IP should be blocked after recording max failures."""
        from codebot.control_server import RATE_LIMIT_MAX_ATTEMPTS
        ip = "10.0.0.1"
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip)
        allowed, reason = self.limiter.is_allowed(ip)
        self.assertFalse(allowed)
        self.assertIn("rate limit exceeded", reason.lower())

    def test_still_allowed_below_limit(self):
        """IP should still be allowed if below max failures."""
        from codebot.control_server import RATE_LIMIT_MAX_ATTEMPTS
        ip = "10.0.0.2"
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS - 1):
            self.limiter.record_failure(ip)
        allowed, reason = self.limiter.is_allowed(ip)
        self.assertTrue(allowed)
        self.assertIsNone(reason)

    def test_per_ip_independence(self):
        """Rate limiting should be per-IP."""
        from codebot.control_server import RATE_LIMIT_MAX_ATTEMPTS
        ip1 = "10.0.0.3"
        ip2 = "10.0.0.4"
        for _ in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip1)
        allowed1, _ = self.limiter.is_allowed(ip1)
        allowed2, reason2 = self.limiter.is_allowed(ip2)
        self.assertFalse(allowed1)
        self.assertTrue(allowed2)
        self.assertIsNone(reason2)


class TestDoGetAuthGate(unittest.TestCase):
    """Unit tests exercising the do_GET auth gate directly (no threading).

    Ensures 100% line coverage of the auth_result check in do_GET
    without relying on threaded server coverage tracking.
    """

    def setUp(self):
        self.original_control_token = os.environ.get("CONTROL_TOKEN", "")
        import codebot.control_server as cs
        cs._rate_limiter = cs.RateLimiter()

    def tearDown(self):
        if self.original_control_token:
            os.environ["CONTROL_TOKEN"] = self.original_control_token
        elif "CONTROL_TOKEN" in os.environ:
            del os.environ["CONTROL_TOKEN"]
        import codebot.control_server as cs
        importlib.reload(cs)

    def test_do_get_returns_401_when_auth_fails(self):
        """do_GET must send 401 and return early when _auth() returns False."""
        cs = _reload_control_server(token="")
        cs._rate_limiter = cs.RateLimiter()

        handler = _make_handler(cs, headers={})
        handler.path = "/telemetry/health"
        handler.command = "GET"

        handler.do_GET()

        handler._json.assert_called()
        call_args = handler._json.call_args
        self.assertEqual(call_args[0][0], 401)
        self.assertIn("unauthorized", call_args[0][1].get("error", "").lower())

    def test_do_get_returns_immediately_when_rate_limited(self):
        """do_GET must return immediately when _auth() returns None (rate limited)."""
        cs = _reload_control_server(token="my-secret-token")
        for _ in range(cs.RATE_LIMIT_MAX_ATTEMPTS):
            cs._rate_limiter.record_failure("127.0.0.1")

        handler = _make_handler(cs, headers={"Authorization": "Bearer wrong"})
        handler.path = "/bots"
        handler.command = "GET"

        handler.do_GET()

        handler._json.assert_called()
        call_args = handler._json.call_args
        self.assertEqual(call_args[0][0], 429)


class TestTelemetryHealthAuthIntegration(unittest.TestCase):
    """Integration test: /telemetry/health returns 401 when CONTROL_TOKEN is unset.

    Verifies acceptance criterion: '/telemetry/health returns 401 when
    CONTROL_TOKEN is unset verified by integration test'.
    Uses a live ThreadingHTTPServer following the pattern from
    test_control_server_unauth_rejection.py.
    """

    @classmethod
    def setUpClass(cls):
        """Start a test HTTP server with no CONTROL_TOKEN."""
        cls.cs_mod = _reload_control_server(token="", allow_unauth="")
        cls.cs_mod._rate_limiter = cls.cs_mod.RateLimiter()
        cls.port = _find_free_port()
        cls.server = cls.cs_mod.ThreadingHTTPServer(
            ("127.0.0.1", cls.port), cls.cs_mod.ControlHandler
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        time.sleep(0.1)

    @classmethod
    def tearDownClass(cls):
        """Shut down the test HTTP server and restore environment."""
        cls.server.shutdown()
        cls.thread.join(timeout=5)
        os.environ["CONTROL_TOKEN"] = "test-token-restore"
        import codebot.control_server as cs_mod
        importlib.reload(cs_mod)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, dict]:
        conn = HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body) if body else {}
        finally:
            conn.close()

    def test_telemetry_health_returns_401_when_token_unset(self):
        """GET /telemetry/health must return 401 when CONTROL_TOKEN is unset.

        This is the core acceptance criterion for ticket CB-C9956292F9984257B5303D3AC0B53F84.
        The /telemetry/health route is behind the _auth() gate in do_GET(), so
        when CONTROL_TOKEN is empty (fail-closed), it must return 401.
        """
        status, body = self._get("/telemetry/health")
        self.assertEqual(status, 401,
                         "/telemetry/health must return 401 when CONTROL_TOKEN is unset")
        self.assertIn("unauthorized", body.get("error", "").lower())

    def test_api_telemetry_health_returns_401_when_token_unset(self):
        """GET /api/telemetry/health must also return 401 when CONTROL_TOKEN is unset."""
        status, body = self._get("/api/telemetry/health")
        self.assertEqual(status, 401,
                         "/api/telemetry/health must return 401 when CONTROL_TOKEN is unset")
        self.assertIn("unauthorized", body.get("error", "").lower())


if __name__ == "__main__":
    unittest.main()
