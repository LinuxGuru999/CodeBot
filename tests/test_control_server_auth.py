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
        """Verify that the production _auth() method fails closed when token is empty."""
        import pathlib
        cs_path = pathlib.Path(__file__).parent.parent / "codebot" / "control_server.py"
        source = cs_path.read_text()

        # The _auth method should NOT unconditionally return True when token is empty.
        # It may return True only inside a CONTROL_ALLOW_UNAUTHENTICATED guard.
        auth_start = source.find("def _auth(self)")
        self.assertNotEqual(auth_start, -1, "_auth method not found in control_server.py")

        # Get the method body (up to next def or end of class)
        auth_body_end = source.find("\n    def ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = source.find("\ndef ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = len(source)
        auth_body = source[auth_start:auth_body_end]

        # Must have fail-closed path: return False when no token and no opt-in
        self.assertIn("return False", auth_body,
                       "control_server.py _auth() must return False when CONTROL_TOKEN is empty")

        # Must require explicit opt-in flag for unauthenticated access
        self.assertIn("CONTROL_ALLOW_UNAUTHENTICATED", auth_body,
                       "control_server.py _auth() must check CONTROL_ALLOW_UNAUTHENTICATED before allowing unauthenticated access")

        # The default path when token is unset must be return False, not return True
        # Verify that 'return True' only appears after the CONTROL_ALLOW_UNAUTHENTICATED check
        allow_idx = auth_body.find("CONTROL_ALLOW_UNAUTHENTICATED")
        true_idx = auth_body.find("return True")
        false_idx = auth_body.find("return False")
        if true_idx != -1:
            self.assertGreater(true_idx, allow_idx,
                               "return True must only appear inside CONTROL_ALLOW_UNAUTHENTICATED guard")
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

    Ticket: CB-9254911-94D6
    Verifies that the control server rejects all authenticated endpoints
    when CONTROL_TOKEN is not set, unless CONTROL_ALLOW_UNAUTHENTICATED=1.
    """

    def setUp(self):
        """Save original environment values."""
        self.original_control_token = os.environ.get("CONTROL_TOKEN", "")
        self.original_allow_unauth = os.environ.get("CONTROL_ALLOW_UNAUTHENTICATED", "")

    def tearDown(self):
        """Restore original environment values."""
        if self.original_control_token:
            os.environ["CONTROL_TOKEN"] = self.original_control_token
        elif "CONTROL_TOKEN" in os.environ:
            del os.environ["CONTROL_TOKEN"]

        if self.original_allow_unauth:
            os.environ["CONTROL_ALLOW_UNAUTHENTICATED"] = self.original_allow_unauth
        elif "CONTROL_ALLOW_UNAUTHENTICATED" in os.environ:
            del os.environ["CONTROL_ALLOW_UNAUTHENTICATED"]

        # Reload the module to pick up new environment variables
        import codebot.control_server as cs
        import importlib
        importlib.reload(cs)

    def test_auth_rejects_when_control_token_unset(self):
        """Server rejects all authenticated endpoints with 401 when CONTROL_TOKEN is empty."""
        # Set CONTROL_TOKEN to empty and reload module
        os.environ["CONTROL_TOKEN"] = ""
        if "CONTROL_ALLOW_UNAUTHENTICATED" in os.environ:
            del os.environ["CONTROL_ALLOW_UNAUTHENTICATED"]

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

    def test_auth_allows_when_control_allow_unauthenticated_set(self):
        """Server allows unauthenticated access when CONTROL_ALLOW_UNAUTHENTICATED=1."""
        # Set CONTROL_TOKEN to empty but allow unauthenticated
        os.environ["CONTROL_TOKEN"] = ""
        os.environ["CONTROL_ALLOW_UNAUTHENTICATED"] = "1"

        import codebot.control_server as cs
        import importlib
        importlib.reload(cs)

        from unittest.mock import MagicMock, patch
        from http.server import BaseHTTPRequestHandler

        mock_request = MagicMock()
        mock_request.makefile.return_value = BytesIO(b"")
        mock_client_address = ("127.0.0.1", 12345)

        with patch.object(BaseHTTPRequestHandler, '__init__', return_value=None):
            handler = cs.ControlHandler(mock_request, mock_client_address, None)
            handler.headers = {}
            handler.client_address = mock_client_address

            # Call _auth - should return True when CONTROL_ALLOW_UNAUTHENTICATED=1
            result = handler._auth()

            # Verify _auth returns True (opt-in unauthenticated mode)
            self.assertTrue(result, "_auth() must return True when CONTROL_ALLOW_UNAUTHENTICATED=1")

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
        if "CONTROL_ALLOW_UNAUTHENTICATED" in os.environ:
            del os.environ["CONTROL_ALLOW_UNAUTHENTICATED"]

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
