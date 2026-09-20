"""Tests for control_server rate limiting and authentication.

Covers RateLimiter class behavior and integration with _auth() and _handle_telemetry().
Also covers fail-closed security behavior when CONTROL_TOKEN is unset (CB-6048497-D3F1).
"""
import io
import json
import logging
import time
import threading
from http.server import BaseHTTPRequestHandler
from unittest.mock import patch, MagicMock

import pytest

# Import the RateLimiter class directly for unit testing
from codebot.control_server import RateLimiter, RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_WINDOW_SECONDS, RATE_LIMIT_COOLDOWN_SECONDS


class TestRateLimiter:
    """Unit tests for RateLimiter class."""

    def setup_method(self):
        """Create a fresh RateLimiter for each test."""
        self.limiter = RateLimiter()

    def test_is_allowed_initial(self):
        """New IP should be allowed."""
        allowed, reason = self.limiter.is_allowed("192.168.1.1")
        assert allowed is True
        assert reason is None

    def test_record_failure_and_check_limit(self):
        """After max failures, IP should be blocked."""
        ip = "10.0.0.1"
        # Record failures up to the limit
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip)

        # Next check should be blocked
        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is False
        assert "rate limit exceeded" in reason

    def test_record_failure_below_limit(self):
        """Below max failures, IP should still be allowed."""
        ip = "10.0.0.2"
        # Record one less than max
        for i in range(RATE_LIMIT_MAX_ATTEMPTS - 1):
            self.limiter.record_failure(ip)

        # Should still be allowed
        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is True
        assert reason is None

    def test_cooldown_expiration(self):
        """After cooldown period, IP should be unblocked."""
        ip = "10.0.0.3"
        # Fill up failures
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip)

        # Verify blocked
        allowed, _ = self.limiter.is_allowed(ip)
        assert allowed is False

        # Manually expire the cooldown by setting blocked_until to past
        with self.limiter._lock:
            self.limiter._blocked_until[ip] = time.time() - 100

        # Should be allowed now
        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is True
        # Failures should be reset after cooldown
        assert len(self.limiter._failures[ip]) == 0

    def test_window_cleanup(self):
        """Old failures outside the window should be cleaned up."""
        ip = "10.0.0.4"
        # Add old failures
        old_time = time.time() - RATE_LIMIT_WINDOW_SECONDS - 100
        with self.limiter._lock:
            self.limiter._failures[ip] = [old_time, old_time + 1]

        # Check should clean up old failures and allow
        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is True
        with self.limiter._lock:
            assert len(self.limiter._failures[ip]) == 0

    def test_different_ips_independent(self):
        """Rate limiting should be per-IP, not global."""
        ip1 = "10.0.0.5"
        ip2 = "10.0.0.6"

        # Block ip1
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip1)

        # ip1 should be blocked
        allowed1, _ = self.limiter.is_allowed(ip1)
        assert allowed1 is False

        # ip2 should still be allowed
        allowed2, reason2 = self.limiter.is_allowed(ip2)
        assert allowed2 is True
        assert reason2 is None

    def test_rate_limit_reason_message(self):
        """Blocked response should include helpful message."""
        ip = "10.0.0.7"
        for i in range(RATE_LIMIT_MAX_ATTEMPTS):
            self.limiter.record_failure(ip)

        allowed, reason = self.limiter.is_allowed(ip)
        assert allowed is False
        assert "rate limit exceeded" in reason
        assert str(RATE_LIMIT_MAX_ATTEMPTS) in reason
        assert str(RATE_LIMIT_WINDOW_SECONDS) in reason


class TestRateLimiterThreadSafety:
    """Test thread safety of RateLimiter."""

    def test_concurrent_access(self):
        """Multiple threads accessing RateLimiter should not cause race conditions."""
        limiter = RateLimiter()
        ip = "10.0.0.100"
        errors = []

        def record_failures():
            try:
                for _ in range(10):
                    limiter.record_failure(ip)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=record_failures) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # After 50 failures (5 threads * 10), should definitely be blocked
        allowed, _ = limiter.is_allowed(ip)
        assert allowed is False


class TestFailClosedSecurity:
    """Tests for fail-closed behavior when CONTROL_TOKEN is unset (CB-6048497-D3F1)."""

    def _make_handler(self, method: str, path: str, token: str | None = None):
        """Create a mock ControlHandler for testing."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {}
        if token is not None:
            handler.headers["Authorization"] = f"Bearer {token}"
        handler._json = MagicMock()
        # Bind real methods to the mock
        handler._auth = ControlHandler._auth.__get__(handler, ControlHandler)
        return handler

    def test_auth_rejects_when_no_token_set(self):
        """When CONTROL_TOKEN is empty and no bypass, _auth must return False."""
        handler = self._make_handler("GET", "/bots")
        with patch("codebot.control_server.CONTROL_TOKEN", ""), \
             patch("codebot.control_server.CONTROL_ALLOW_UNAUTHENTICATED", False):
            result = handler._auth()
        assert result is False

    def test_auth_rejects_all_protected_get_endpoints_without_token(self):
        """All protected GET endpoints must return 401 when CONTROL_TOKEN is unset."""
        from codebot.control_server import ControlHandler

        protected_paths = ["/bots", "/bots/test-bot", "/state", "/scheduler/status"]
        for path in protected_paths:
            handler = self._make_handler("GET", path)
            with patch("codebot.control_server.CONTROL_TOKEN", ""), \
                 patch("codebot.control_server.CONTROL_ALLOW_UNAUTHENTICATED", False):
                result = handler._auth()
            assert result is False, f"Expected auth rejection for {path}"

    def test_auth_rejects_post_endpoints_without_token(self):
        """All POST endpoints must return 401 when CONTROL_TOKEN is unset."""
        protected_paths = ["/bots/start", "/bots/stop", "/control/drain", "/control/update"]
        for path in protected_paths:
            handler = self._make_handler("POST", path)
            with patch("codebot.control_server.CONTROL_TOKEN", ""), \
                 patch("codebot.control_server.CONTROL_ALLOW_UNAUTHENTICATED", False):
                result = handler._auth()
            assert result is False, f"Expected auth rejection for POST {path}"

    def test_health_endpoint_public_without_token(self):
        """/health must remain accessible without authentication."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.path = "/health"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {}
        handler._json = MagicMock()
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.requestline = "GET /health HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"

        # do_GET handles /health before calling _auth
        with patch("codebot.control_server.CONTROL_TOKEN", ""), \
             patch("codebot.control_server.time") as mock_time:
            mock_time.time.return_value = 1000.0
            ControlHandler.do_GET(handler)

        # Should have sent 200 response, not 401
        handler._json.assert_called_once()
        call_args = handler._json.call_args
        assert call_args[0][0] == 200, f"Expected 200 for /health, got {call_args[0][0]}"
        assert call_args[0][1]["status"] == "ok"

    def test_critical_log_emitted_on_missing_token(self, caplog):
        """CRITICAL log must be emitted when CONTROL_TOKEN is missing."""
        handler = self._make_handler("GET", "/bots")
        with caplog.at_level(logging.CRITICAL), \
             patch("codebot.control_server.CONTROL_TOKEN", ""), \
             patch("codebot.control_server.CONTROL_ALLOW_UNAUTHENTICATED", False):
            handler._auth()
        assert any("CONTROL_TOKEN is not set" in record.message for record in caplog.records)
        assert any(record.levelno >= logging.CRITICAL for record in caplog.records)

    def test_allow_unauthenticated_bypass(self):
        """CONTROL_ALLOW_UNAUTHENTICATED=1 should permit access when token is unset."""
        handler = self._make_handler("GET", "/bots")
        with patch("codebot.control_server.CONTROL_TOKEN", ""), \
             patch("codebot.control_server.CONTROL_ALLOW_UNAUTHENTICATED", True):
            result = handler._auth()
        assert result is True

    def test_valid_token_accepted(self):
        """Valid Bearer token should be accepted."""
        handler = self._make_handler("GET", "/bots", token="my-secret-token")
        with patch("codebot.control_server.CONTROL_TOKEN", "my-secret-token"), \
             patch("codebot.control_server.CONTROL_ALLOW_UNAUTHENTICATED", False):
            result = handler._auth()
        assert result is True

    def test_invalid_token_rejected(self):
        """Invalid Bearer token should be rejected."""
        handler = self._make_handler("GET", "/bots", token="wrong-token")
        with patch("codebot.control_server.CONTROL_TOKEN", "my-secret-token"), \
             patch("codebot.control_server.CONTROL_ALLOW_UNAUTHENTICATED", False):
            result = handler._auth()
        assert result is False

    def test_state_endpoint_returns_404(self):
        """GET /state must return 404 - endpoint removed for security (CB-8659441-F271)."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.path = "/state"
        handler.client_address = ("192.168.1.100", 54321)
        handler.headers = {"Authorization": "Bearer valid-token"}
        handler._json = MagicMock()
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.requestline = "GET /state HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"

        with patch("codebot.control_server.CONTROL_TOKEN", "valid-token"), \
             patch("codebot.control_server.CONTROL_ALLOW_UNAUTHENTICATED", False):
            ControlHandler.do_GET(handler)

        handler._json.assert_called_once()
        call_args = handler._json.call_args
        assert call_args[0][0] == 404, f"Expected 404 for /state, got {call_args[0][0]}"

    def test_api_state_endpoint_returns_404(self):
        """GET /api/state must return 404 - endpoint removed for security (CB-8659441-F271)."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.path = "/api/state"
        handler.client_address = ("192.168.1.100", 54321)
        handler.headers = {"Authorization": "Bearer valid-token"}
        handler._json = MagicMock()
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()
        handler.requestline = "GET /api/state HTTP/1.1"
        handler.request_version = "HTTP/1.1"
        handler.command = "GET"

        with patch("codebot.control_server.CONTROL_TOKEN", "valid-token"), \
             patch("codebot.control_server.CONTROL_ALLOW_UNAUTHENTICATED", False):
            ControlHandler.do_GET(handler)

        handler._json.assert_called_once()
        call_args = handler._json.call_args
        assert call_args[0][0] == 404, f"Expected 404 for /api/state, got {call_args[0][0]}"
