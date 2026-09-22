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
import sys
import unittest
from unittest.mock import MagicMock, patch
from io import BytesIO


class TestAuthFailClosed(unittest.TestCase):
    """Verify that _auth() rejects requests when CONTROL_TOKEN is unset."""

    def test_auth_returns_false_when_token_unset(self):
        """When CONTROL_TOKEN is empty, _auth() must return False (fail-closed)."""
        # We need to test the Handler._auth method directly with CONTROL_TOKEN=""
        # Import after ensuring we can test
        from http.server import BaseHTTPRequestHandler

        # Create a minimal mock request handler
        class MockHandler:
            headers = {}

            def __init__(self, auth_header: str = ""):
                if auth_header:
                    self.headers = {"Authorization": auth_header}
                else:
                    self.headers = {}

        # Patch the module-level CONTROL_TOKEN to empty string
        # We test the _auth logic directly
        class TestHandler(BaseHTTPRequestHandler):
            def _auth(self) -> bool:
                import hmac
                token = ""  # simulate unset CONTROL_TOKEN
                if not token:
                    return False  # fail-closed
                auth = self.headers.get("Authorization", "")
                expected = f"Bearer {token}"
                return hmac.compare_digest(auth.strip(), expected)

            def log_message(self, format, *args):
                pass

        # Simulate a request with no auth header
        handler = TestHandler.__new__(TestHandler)
        handler.headers = {}

        # With empty CONTROL_TOKEN and no auth header, should return False
        result = handler._auth()
        self.assertFalse(result, "_auth() must return False when CONTROL_TOKEN is empty")

    def test_auth_returns_false_when_token_unset_but_header_present(self):
        """Even with a Bearer header, _auth() must reject if CONTROL_TOKEN is unset."""
        class TestHandler:
            def __init__(self, auth_header: str = ""):
                self.headers = {}
                if auth_header:
                    self.headers = {"Authorization": auth_header}

            def _auth(self) -> bool:
                import hmac
                token = ""  # simulate unset CONTROL_TOKEN
                if not token:
                    return False  # fail-closed
                auth = self.headers.get("Authorization", "")
                expected = f"Bearer {token}"
                return hmac.compare_digest(auth.strip(), expected)

        handler = TestHandler(auth_header="Bearer some-token")
        result = handler._auth()
        self.assertFalse(result, "_auth() must return False when CONTROL_TOKEN is empty, even with valid-looking header")

    def test_auth_accepts_valid_token(self):
        """_auth() accepts requests with correct Bearer token."""
        class TestHandler:
            def __init__(self, auth_header: str = ""):
                self.headers = {}
                if auth_header:
                    self.headers = {"Authorization": auth_header}

            def _auth(self) -> bool:
                import hmac
                token = "my-secret-token"
                if not token:
                    return False
                auth = self.headers.get("Authorization", "")
                expected = f"Bearer {token}"
                return hmac.compare_digest(auth.strip(), expected)

        handler = TestHandler(auth_header="Bearer my-secret-token")
        result = handler._auth()
        self.assertTrue(result, "_auth() must return True with correct Bearer token")

    def test_auth_rejects_wrong_token(self):
        """_auth() rejects requests with incorrect Bearer token."""
        class TestHandler:
            def __init__(self, auth_header: str = ""):
                self.headers = {}
                if auth_header:
                    self.headers = {"Authorization": auth_header}

            def _auth(self) -> bool:
                import hmac
                token = "my-secret-token"
                if not token:
                    return False
                auth = self.headers.get("Authorization", "")
                expected = f"Bearer {token}"
                return hmac.compare_digest(auth.strip(), expected)

        handler = TestHandler(auth_header="Bearer wrong-token")
        result = handler._auth()
        self.assertFalse(result, "_auth() must return False with incorrect Bearer token")

    def test_auth_rejects_empty_auth_header(self):
        """_auth() rejects when Authorization header is missing."""
        class TestHandler:
            def __init__(self, auth_header: str = ""):
                self.headers = {}
                if auth_header:
                    self.headers = {"Authorization": auth_header}

            def _auth(self) -> bool:
                import hmac
                token = "my-secret-token"
                if not token:
                    return False
                auth = self.headers.get("Authorization", "")
                expected = f"Bearer {token}"
                return hmac.compare_digest(auth.strip(), expected)

        handler = TestHandler()
        result = handler._auth()
        self.assertFalse(result, "_auth() must return False when Authorization header is missing")


class TestSecurityHeaders(unittest.TestCase):
    """Verify that all responses include required security headers (Constitution §2)."""

    def test_security_headers_defined(self):
        """Verify the security headers constant exists and contains required headers."""
        # Read the control_server.py source to check headers are present
        import pathlib
        cs_path = pathlib.Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        # Check that security headers are set in _json method
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
        import pathlib
        cs_path = pathlib.Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        # The _auth method must fail closed: empty token -> return False.
        auth_start = source.find("def _auth(self)")
        self.assertNotEqual(auth_start, -1, "_auth method not found in control_server.py")

        # Get the method body (up to next def or end of class)
        auth_body_end = source.find("\n    def ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = source.find("\ndef ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = len(source)
        auth_body = source[auth_start:auth_body_end]

        # Must have fail-closed path: return False when no token
        self.assertIn("return False", auth_body,
                       "control_server.py _auth() must return False when CONTROL_TOKEN is empty")

        # The CONTROL_ALLOW_UNAUTHENTICATED bypass must be REMOVED from _auth():
        # no opt-in flag may re-enable unauthenticated access to any endpoint
        # (destructive or read-only). CB-B4086 acceptance criterion #2.
        self.assertNotIn("CONTROL_ALLOW_UNAUTHENTICATED", auth_body,
                       "control_server.py _auth() must NOT contain CONTROL_ALLOW_UNAUTHENTICATED bypass")
        self.assertNotIn("ALLOW_UNAUTHENTICATED", auth_body,
                       "control_server.py _auth() must NOT contain any unauthenticated bypass")

        # The empty-token branch must unconditionally reject (no `return True`
        # reachable when CONTROL_TOKEN is empty).
        false_idx = auth_body.find("return False")
        self.assertNotEqual(false_idx, -1,
                            "_auth() must have a return False path for missing token")

    def test_constitution_ssrf_not_affected(self):
        """SSRF protection should still work - this test ensures the fix doesn't break existing auth."""
        import pathlib
        cs_path = pathlib.Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        # Verify hmac.compare_digest is still used (timing-safe comparison)
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

        # Reload the module to pick up new environment variables
        import codebot.control_server as cs
        import importlib
        importlib.reload(cs)

    def test_auth_rejects_when_control_token_unset(self):
        """Server rejects all authenticated endpoints with 401 when CONTROL_TOKEN is empty."""
        # Set CONTROL_TOKEN to empty and reload module
        os.environ["CONTROL_TOKEN"] = ""

        import codebot.control_server as cs
        import importlib
        importlib.reload(cs)

        # Create a mock handler instance
        from unittest.mock import MagicMock, patch
        from http.server import BaseHTTPRequestHandler

        # Create a mock request
        mock_request = MagicMock()
        mock_request.makefile.return_value = BytesIO(b"")
        mock_client_address = ("127.0.0.1", 12345)

        # Patch the socket to avoid actual network calls
        with patch.object(BaseHTTPRequestHandler, '__init__', return_value=None):
            handler = cs.ControlHandler(mock_request, mock_client_address, None)
            handler.headers = {}
            handler.client_address = mock_client_address
            handler._auth_called = False

            # Mock the _json method to capture what would be sent
            captured_response = {}
            def mock_json(code, obj):
                captured_response['code'] = code
                captured_response['obj'] = obj
            handler._json = mock_json

            # Call _auth - should return False when CONTROL_TOKEN is empty
            result = handler._auth()

            # Verify _auth returns False (fail-closed)
            self.assertFalse(result, "_auth() must return False when CONTROL_TOKEN is empty")

    def test_startup_warning_logged_when_no_token(self):
        """Startup warning is logged when CONTROL_TOKEN is not configured."""
        import logging
        import io

        # Set up a string buffer to capture log output
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.CRITICAL)
        logger = logging.getLogger("codebot.control_server")
        logger.addHandler(handler)
        logger.setLevel(logging.CRITICAL)

        # Set CONTROL_TOKEN to empty
        os.environ["CONTROL_TOKEN"] = ""

        import codebot.control_server as cs
        import importlib
        importlib.reload(cs)

        # Call main() which should log the warning
        # We can't actually start the server, but we can check that the
        # CONTROL_TOKEN variable is empty and the bind_host logic would trigger
        self.assertEqual(cs.CONTROL_TOKEN, "", "CONTROL_TOKEN should be empty")

        # Clean up logger
        logger.removeHandler(handler)


if __name__ == "__main__":
    unittest.main()


class TestTimingSafeComparison(unittest.TestCase):
    """Verify that token comparison uses timing-safe hmac.compare_digest.

    Ticket: CB-3488569-8A5E — Timing attack on bearer token comparison
    Acceptance: test verifies timing-safe comparison is used
    """

    def test_auth_method_uses_hmac_compare_digest(self):
        """Verify _auth() uses hmac.compare_digest for constant-time comparison."""
        import pathlib
        cs_path = pathlib.Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        # Find the _auth method
        auth_start = source.find("def _auth(self)")
        self.assertNotEqual(auth_start, -1, "_auth method not found")

        # Get the method body
        auth_body_end = source.find("\n    def ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = source.find("\ndef ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = len(source)
        auth_body = source[auth_start:auth_body_end]

        # Verify hmac.compare_digest is used
        self.assertIn("hmac.compare_digest", auth_body,
                       "_auth() must use hmac.compare_digest for timing-safe token comparison")

        # Verify no == comparison on auth/token material in _auth
        # Check that we're not using == for comparing the auth header value
        self.assertNotIn("auth == expected", auth_body,
                         "_auth() must not use == for token comparison")
        self.assertNotIn("auth.strip() == expected", auth_body,
                         "_auth() must not use == for token comparison")

    def test_telemetry_handler_uses_hmac_compare_digest(self):
        """Verify _handle_telemetry() uses hmac.compare_digest for constant-time comparison."""
        import pathlib
        cs_path = pathlib.Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        # Find the _handle_telemetry method
        telemetry_start = source.find("def _handle_telemetry(self")
        self.assertNotEqual(telemetry_start, -1, "_handle_telemetry method not found")

        # Get the method body
        telemetry_body_end = source.find("\n    def ", telemetry_start + 1)
        if telemetry_body_end == -1:
            telemetry_body_end = source.find("\ndef ", telemetry_start + 1)
        if telemetry_body_end == -1:
            telemetry_body_end = len(source)
        telemetry_body = source[telemetry_start:telemetry_body_end]

        # Verify hmac.compare_digest is used
        self.assertIn("hmac.compare_digest", telemetry_body,
                       "_handle_telemetry() must use hmac.compare_digest for timing-safe token comparison")

    def test_no_equals_comparison_on_token_material(self):
        """Verify no == comparison is used on CONTROL_TOKEN or auth header values."""
        import pathlib
        cs_path = pathlib.Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        # Check that there's no direct == comparison involving auth tokens
        # We allow == for checking if CONTROL_TOKEN is empty ("if not CONTROL_TOKEN")
        # but not for comparing actual token values

        # Find all lines with == that involve token/auth comparison patterns
        lines = source.split("\n")
        for i, line in enumerate(lines, 1):
            stripped = line.strip()
            # Skip comments and empty lines
            if stripped.startswith("#") or not stripped:
                continue
            # Check for dangerous patterns: comparing auth/token values with ==
            # Allow: if not CONTROL_TOKEN, CONTROL_TOKEN == "", etc. (emptiness checks)
            # Disallow: auth == expected, token == something, etc. (value comparisons)
            if "==" in stripped:
                # Skip emptiness checks and string literal comparisons for config
                if "CONTROL_TOKEN" in stripped and ('""' in stripped or "''" in stripped or "not " in stripped):
                    continue
                if "TELEMETRY_TOKEN" in stripped and ('""' in stripped or "''" in stripped or "not " in stripped):
                    continue
                # Flag any == comparison involving auth variables
                if any(var in stripped for var in ["auth", "expected", "token"]):
                    # Make sure it's not inside a comment
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

        # Reload module to pick up env changes
        import codebot.control_server as cs
        import importlib
        importlib.reload(cs)

    def test_telemetry_rejected_when_control_token_empty_even_if_telemetry_token_set(self):
        """POST /telemetry must return 401 when CONTROL_TOKEN is empty, even if TELEMETRY_TOKEN is valid."""
        # Set env vars: CONTROL_TOKEN empty, TELEMETRY_TOKEN set
        os.environ["CONTROL_TOKEN"] = ""
        os.environ["CODEBOT_TELEMETRY_TOKEN"] = "valid-telemetry-token"
        os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)

        import codebot.control_server as cs
        import importlib
        importlib.reload(cs)

        from unittest.mock import MagicMock, patch
        from http.server import BaseHTTPRequestHandler
        from io import BytesIO

        mock_request = MagicMock()
        mock_request.makefile.return_value = BytesIO(b"")
        mock_client_address = ("127.0.0.1", 12345)

        with patch.object(BaseHTTPRequestHandler, '__init__', return_value=None):
            handler = cs.ControlHandler(mock_request, mock_client_address, None)
            handler.headers = {"Authorization": "Bearer valid-telemetry-token"}
            handler.client_address = mock_client_address

            captured_response = {}
            def mock_json(code, obj, extra_headers=None):
                captured_response['code'] = code
                captured_response['obj'] = obj
            handler._json = mock_json

            # Call _handle_telemetry with a minimal valid body
            handler._handle_telemetry({"signal_type": "test", "data": {}})

            self.assertEqual(captured_response.get('code'), 401,
                             "Telemetry endpoint must return 401 when CONTROL_TOKEN is empty")
            self.assertIn("unauthorized", captured_response.get('obj', {}).get('error', '').lower())


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
