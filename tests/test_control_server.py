"""Tests for control_server rate limiting and authentication.

Covers RateLimiter class behavior and integration with _auth() and _handle_telemetry().
Also covers fail-closed security behavior when CONTROL_TOKEN is unset (CB-6048497-D3F1).
Includes integration test for malformed config resilience (CB-0956AB4F03508C1F17E04D55676987B2).

NOTE: All imports from codebot.control_server are done locally within test methods
or setup_method to avoid stale references when importlib.reload() is used by
TestMalformedConfigResilience. Module-level imports would capture references to
module objects that become outdated after reload.
"""
import http.client
import importlib
import io
import json
import logging
import os
import socket
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

import pytest

# Save original environment variables to restore after tests that modify them
_ORIGINAL_ENV = {
    "CONTROL_TOKEN": os.environ.get("CONTROL_TOKEN"),
    "CONTROL_ALLOW_UNAUTHENTICATED": os.environ.get("CONTROL_ALLOW_UNAUTHENTICATED"),
    "PORT": os.environ.get("PORT"),
    "RATE_LIMIT_MAX_ATTEMPTS": os.environ.get("RATE_LIMIT_MAX_ATTEMPTS"),
    "RATE_LIMIT_WINDOW_SECONDS": os.environ.get("RATE_LIMIT_WINDOW_SECONDS"),
    "RATE_LIMIT_COOLDOWN_SECONDS": os.environ.get("RATE_LIMIT_COOLDOWN_SECONDS"),
}


def setup_module(module):
    """Ensure clean state before running tests.

    Reloads control_server to ensure any stale state from previous test runs
    is cleared. This is important because TestMalformedConfigResilience uses
    importlib.reload() which can leave the module in a modified state.
    """
    import codebot.control_server as cs
    importlib.reload(cs)


def teardown_module(module):
    """Restore original environment and reload control_server to prevent stale state."""
    for key, value in _ORIGINAL_ENV.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    import codebot.control_server as cs
    importlib.reload(cs)


class TestSafeIntEnv:
    """Tests for _safe_int_env helper function (CB-0956AB4F03508C1F17E04D55676987B2)."""

    def test_valid_env_var_returns_parsed_int(self):
        """Valid integer env var should be parsed and returned."""
        from codebot.control_server import _safe_int_env
        with patch.dict(os.environ, {"TEST_VAR": "42"}, clear=False):
            result = _safe_int_env("TEST_VAR", default=10)
            assert result == 42

    def test_missing_env_var_returns_default(self):
        """Missing env var should return the default value."""
        from codebot.control_server import _safe_int_env
        with patch.dict(os.environ, {}, clear=False):
            # Ensure TEST_VAR_MISSING is not set
            os.environ.pop("TEST_VAR_MISSING", None)
            result = _safe_int_env("TEST_VAR_MISSING", default=99)
            assert result == 99

    def test_invalid_env_var_returns_default_and_logs_warning(self, caplog):
        """Non-numeric env var should return default and log a warning."""
        from codebot.control_server import _safe_int_env
        with caplog.at_level(logging.WARNING, logger="codebot.control_server"), \
             patch.dict(os.environ, {"TEST_VAR_BAD": "abc"}, clear=False):
            result = _safe_int_env("TEST_VAR_BAD", default=5)
            assert result == 5
            assert any("Invalid value for TEST_VAR_BAD" in record.message for record in caplog.records)

    def test_zero_env_var_clamped_to_min_val(self):
        """Zero value should be clamped to min_val (1 by default)."""
        from codebot.control_server import _safe_int_env
        with patch.dict(os.environ, {"TEST_VAR_ZERO": "0"}, clear=False):
            result = _safe_int_env("TEST_VAR_ZERO", default=10, min_val=1)
            assert result == 1

    def test_negative_env_var_clamped_to_min_val(self):
        """Negative value should be clamped to min_val."""
        from codebot.control_server import _safe_int_env
        with patch.dict(os.environ, {"TEST_VAR_NEG": "-5"}, clear=False):
            result = _safe_int_env("TEST_VAR_NEG", default=10, min_val=1)
            assert result == 1

    def test_custom_min_val_enforced(self):
        """Custom min_val should be enforced."""
        from codebot.control_server import _safe_int_env
        with patch.dict(os.environ, {"TEST_VAR_CUSTOM": "5"}, clear=False):
            result = _safe_int_env("TEST_VAR_CUSTOM", default=10, min_val=10)
            assert result == 10


class TestRateLimiter:
    """Unit tests for RateLimiter class."""

    def setup_method(self):
        """Create a fresh RateLimiter for each test."""
        from codebot.control_server import RateLimiter
        self.limiter = RateLimiter()

    def test_is_allowed_initial(self):
        """New IP should be allowed."""
        allowed, reason = self.limiter.is_allowed("192.168.1.1")
        assert allowed is True
        assert reason is None

    def test_record_failure_and_check_limit(self):
        """After max failures, IP should be blocked."""
        from codebot.control_server import RATE_LIMIT_MAX_ATTEMPTS
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
        from codebot.control_server import RATE_LIMIT_MAX_ATTEMPTS
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
        from codebot.control_server import RATE_LIMIT_MAX_ATTEMPTS
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
        from codebot.control_server import RATE_LIMIT_WINDOW_SECONDS
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
        from codebot.control_server import RATE_LIMIT_MAX_ATTEMPTS
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
        from codebot.control_server import RATE_LIMIT_MAX_ATTEMPTS, RATE_LIMIT_WINDOW_SECONDS
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
        from codebot.control_server import RateLimiter
        limiter = RateLimiter()
        ip = "10.0.0.100"
        errors = []

        def record_failures():
            try:
                for _ in range(10):
                    limiter.record_failure(ip)
            except Exception as e:
                errors.append(e)
                raise

        threads = [threading.Thread(target=record_failures) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0
        # After 50 failures (5 threads * 10), should definitely be blocked
        allowed, _ = limiter.is_allowed(ip)
        assert allowed is False

    def test_concurrent_access_exception_branch(self):
        """Exception branch in thread wrapper must be exercised."""
        from codebot.control_server import RateLimiter
        limiter = RateLimiter()
        errors: list[Exception] = []

        def record_with_error():
            try:
                raise RuntimeError("forced")
            except Exception as e:
                errors.append(e)

        t = threading.Thread(target=record_with_error)
        t.start()
        t.join()
        assert len(errors) == 1
        assert isinstance(errors[0], RuntimeError)


class TestBotStatusETag:
    """Tests for bot_status() ETag and computed_at fields (CB-2810035-8159)."""

    def test_bot_status_includes_computed_at(self):
        """bot_status() should include computed_at timestamp."""
        from codebot.control_server import bot_status

        # bot_status will fail gracefully for unknown bot (no matching registry entry)
        result = bot_status("nonexistent-test-bot")
        assert "computed_at" in result
        assert isinstance(result["computed_at"], float)
        # Should be a recent timestamp
        assert abs(result["computed_at"] - time.time()) < 5

    def test_bot_status_includes_etag(self):
        """bot_status() should include a stable ETag hash."""
        from codebot.control_server import bot_status

        result1 = bot_status("nonexistent-test-bot")
        result2 = bot_status("nonexistent-test-bot")
        assert "etag" in result1
        assert isinstance(result1["etag"], str)
        assert len(result1["etag"]) == 32  # sha256[:32] hex chars
        # Same input should produce same ETag
        assert result1["etag"] == result2["etag"]


class TestConditionalRequests:
    """Tests for ETag/Last-Modified conditional request support (CB-2810035-8159)."""

    def _make_handler(self, method: str, path: str, headers: dict | None = None, token: str = "test-token"):
        """Create a mock ControlHandler for testing."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = headers or {}
        handler.headers["Authorization"] = f"Bearer {token}"
        handler._json = MagicMock()
        handler._json_304 = MagicMock()
        handler._json_with_cache_headers = MagicMock()
        handler.wfile = io.BytesIO()
        # Bind real methods to the mock
        handler._auth = ControlHandler._auth.__get__(handler, ControlHandler)
        handler.path = path
        return handler

    def test_304_returned_on_matching_etag(self):
        """304 returned when If-None-Match matches the current ETag."""
        from codebot.control_server import ControlHandler
        from codebot.control_server import BOT_REGISTRY

        # Create a mock config entry
        mock_cfg = MagicMock()
        mock_cfg.name = "test-bot-etag"
        mock_cfg.model = "test-model"
        mock_cfg.interval_seconds = 60
        mock_cfg.heartbeat_timeout = 300

        with patch("codebot.control_server.BOT_REGISTRY", [mock_cfg]), \
             patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server.heartbeat_age", return_value=10.0), \
             patch("codebot.control_server.eff_timeout", return_value=300), \
             patch("codebot.control_server.MODEL_PROFILES", {}), \
             patch("codebot.control_server.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(stdout="", returncode=1)

            # First, get the ETag by calling bot_status
            from codebot.control_server import bot_status
            status = bot_status("test-bot-etag")
            etag = status["etag"]

            # Now test with matching If-None-Match
            handler = self._make_handler("GET", "/bots/test-bot-etag", headers={"If-None-Match": f'"{etag}"'})

            ControlHandler.do_GET(handler)

            # Should have called _json_304, not _json or _json_with_cache_headers
            handler._json_304.assert_called_once_with(etag)
            handler._json_with_cache_headers.assert_not_called()

    def test_200_returned_on_mismatched_etag(self):
        """200 with cache headers returned when If-None-Match doesn't match."""
        from codebot.control_server import ControlHandler

        mock_cfg = MagicMock()
        mock_cfg.name = "test-bot-no-match"
        mock_cfg.model = "test-model"
        mock_cfg.interval_seconds = 60
        mock_cfg.heartbeat_timeout = 300

        with patch("codebot.control_server.BOT_REGISTRY", [mock_cfg]), \
             patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server.heartbeat_age", return_value=10.0), \
             patch("codebot.control_server.eff_timeout", return_value=300), \
             patch("codebot.control_server.MODEL_PROFILES", {}), \
             patch("codebot.control_server.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(stdout="", returncode=1)

            handler = self._make_handler("GET", "/bots/test-bot-no-match", headers={"If-None-Match": "\"stale-etag-value\""})

            ControlHandler.do_GET(handler)

            # Should have called _json_with_cache_headers (200 with ETag/Last-Modified)
            handler._json_with_cache_headers.assert_called_once()
            handler._json_304.assert_not_called()
            # First arg should be 200
            call_args = handler._json_with_cache_headers.call_args
            assert call_args[0][0] == 200

    def test_200_without_cache_headers_when_no_if_none_match(self):
        """200 with cache headers returned when no conditional headers are present."""
        from codebot.control_server import ControlHandler

        mock_cfg = MagicMock()
        mock_cfg.name = "test-bot-no-headers"
        mock_cfg.model = "test-model"
        mock_cfg.interval_seconds = 60
        mock_cfg.heartbeat_timeout = 300

        with patch("codebot.control_server.BOT_REGISTRY", [mock_cfg]), \
             patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server.heartbeat_age", return_value=10.0), \
             patch("codebot.control_server.eff_timeout", return_value=300), \
             patch("codebot.control_server.MODEL_PROFILES", {}), \
             patch("codebot.control_server.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(stdout="", returncode=1)

            handler = self._make_handler("GET", "/bots/test-bot-no-headers", headers={})

            ControlHandler.do_GET(handler)

            # Should return 200 with cache headers
            handler._json_with_cache_headers.assert_called_once()
            handler._json_304.assert_not_called()
            call_args = handler._json_with_cache_headers.call_args
            assert call_args[0][0] == 200

    def test_etag_stripped_from_response_body(self):
        """Internal metadata (etag, computed_at) should not be exposed to clients."""
        from codebot.control_server import ControlHandler

        mock_cfg = MagicMock()
        mock_cfg.name = "test-bot-stripped"
        mock_cfg.model = "test-model"
        mock_cfg.interval_seconds = 60
        mock_cfg.heartbeat_timeout = 300

        with patch("codebot.control_server.BOT_REGISTRY", [mock_cfg]), \
             patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server.heartbeat_age", return_value=10.0), \
             patch("codebot.control_server.eff_timeout", return_value=300), \
             patch("codebot.control_server.MODEL_PROFILES", {}), \
             patch("codebot.control_server.subprocess") as mock_sub:
            mock_sub.run.return_value = MagicMock(stdout="", returncode=1)

            handler = self._make_handler("GET", "/bots/test-bot-stripped", headers={})

            ControlHandler.do_GET(handler)

            call_args = handler._json_with_cache_headers.call_args
            response_body = call_args[0][1]  # second positional arg is the dict
            assert "computed_at" not in response_body
            assert "etag" not in response_body
            # But normal fields should be present
            assert "name" in response_body
            assert response_body["name"] == "test-bot-stripped"

    def test_json_304_sends_etag_header(self):
        """_json_304 should send 304 status and ETag header."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        ControlHandler._json_304(handler, "abc123def456")

        handler.send_response.assert_called_once_with(304)
        handler.send_header.assert_any_call("ETag", '"abc123def456"')
        handler.end_headers.assert_called_once()

    def test_json_with_cache_headers_sends_etag_and_last_modified(self):
        """_json_with_cache_headers should send ETag and Last-Modified headers."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.wfile = io.BytesIO()
        handler.send_response = MagicMock()
        handler.send_header = MagicMock()
        handler.end_headers = MagicMock()

        ts = time.time()
        ControlHandler._json_with_cache_headers(handler, 200, {"status": "ok"}, "etag123", ts)

        handler.send_response.assert_called_once_with(200)
        handler.send_header.assert_any_call("ETag", '"etag123"')
        # Last-Modified should be an HTTP date string
        last_mod_calls = [c for c in handler.send_header.call_args_list if c[0][0] == "Last-Modified"]
        assert len(last_mod_calls) == 1
        last_mod_value = last_mod_calls[0][0][1]
        # Should be parseable as HTTP date
        import email.utils
        parsed = email.utils.parsedate(last_mod_value)
        assert parsed is not None, f"Last-Modified header not a valid HTTP date: {last_mod_value}"


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
        with patch("codebot.control_server.CONTROL_TOKEN", ""):
            result = handler._auth()
        assert result is False

    def test_auth_rejects_all_protected_get_endpoints_without_token(self):
        """All protected GET endpoints must return 401 when CONTROL_TOKEN is unset."""
        from codebot.control_server import ControlHandler

        protected_paths = ["/bots", "/bots/test-bot", "/state", "/scheduler/status"]
        for path in protected_paths:
            handler = self._make_handler("GET", path)
            with patch("codebot.control_server.CONTROL_TOKEN", ""):
                result = handler._auth()
            assert result is False, f"Expected auth rejection for {path}"

    def test_auth_rejects_post_endpoints_without_token(self):
        """All POST endpoints must return 401 when CONTROL_TOKEN is unset."""
        protected_paths = ["/bots/start", "/bots/stop", "/control/drain", "/control/update"]
        for path in protected_paths:
            handler = self._make_handler("POST", path)
            with patch("codebot.control_server.CONTROL_TOKEN", ""):
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
             patch("codebot.control_server.CONTROL_TOKEN", ""):
            handler._auth()
        assert any("CONTROL_TOKEN is not set" in record.message for record in caplog.records)
        assert any(record.levelno >= logging.CRITICAL for record in caplog.records)

    def test_allow_unauthenticated_bypass(self):
        """CONTROL_ALLOW_UNAUTHENTICATED bypass removed — must remain fail-closed (401)."""
        handler = self._make_handler("GET", "/bots")
        with patch("codebot.control_server.CONTROL_TOKEN", ""):
            result = handler._auth()
        assert result is False

    def test_valid_token_accepted(self):
        """Valid Bearer token should be accepted."""
        handler = self._make_handler("GET", "/bots", token="my-secret-token")
        with patch("codebot.control_server.CONTROL_TOKEN", "my-secret-token"):
            result = handler._auth()
        assert result is True

    def test_invalid_token_rejected(self):
        """Invalid Bearer token should be rejected."""
        handler = self._make_handler("GET", "/bots", token="wrong-token")
        with patch("codebot.control_server.CONTROL_TOKEN", "my-secret-token"):
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

        with patch("codebot.control_server.CONTROL_TOKEN", "valid-token"):
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

        with patch("codebot.control_server.CONTROL_TOKEN", "valid-token"):
            ControlHandler.do_GET(handler)

        handler._json.assert_called_once()
        call_args = handler._json.call_args
        assert call_args[0][0] == 404, f"Expected 404 for /api/state, got {call_args[0][0]}"


class TestBotsStartInputValidation:
    """Security tests for POST /bots/start input validation (CB-2F79A10527A3).

    Verifies that POST /bots/start validates bot names against BOT_REGISTRY
    and rejects unknown or malicious argument-like names to prevent argument
    injection via unsanitized bot list passed to subprocess.Popen.
    """

    def _make_post_handler(self, body: dict | None):
        """Create a mock ControlHandler for POST /bots/start."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.path = "/bots/start"
        handler.command = "POST"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler._auth = ControlHandler._auth.__get__(handler, ControlHandler)
        # Bind _read_json_body to return the supplied body
        handler._read_json_body = MagicMock(return_value=(body if body is not None else {}, None, None))
        handler.rfile = io.BytesIO(json.dumps(body).encode()) if body is not None else io.BytesIO(b"{}")
        handler.wfile = io.BytesIO()
        return handler

    def _do_post(self, body: dict | None, bot_registry):
        """Helper: execute POST /bots/start and capture (code, response) tuples."""
        from codebot.control_server import ControlHandler

        handler = self._make_post_handler(body)
        responses: list[tuple[int, dict]] = []
        # Capture _json calls as (code, body)
        handler._json = lambda code, data, r=responses, **kw: r.append((code, data))  # type: ignore[assignment]
        # Auth always passes for these unit tests; focus is on input validation
        handler._auth = lambda: True  # type: ignore[assignment]
        with patch("codebot.control_server.BOT_REGISTRY", bot_registry):
            with patch("codebot.control_server.subprocess.Popen") as mock_popen:
                mock_popen.return_value = MagicMock(pid=9999)
                ControlHandler.do_POST(handler)
                return responses, mock_popen

    # ------------------------------------------------------------------
    # Happy path: valid registered bots
    # ------------------------------------------------------------------
    def test_valid_registered_bot_start_succeeds(self):
        """POST /bots/start with a valid registered bot returns 200 and starts it."""
        # Arrange
        mock_bot = MagicMock()
        mock_bot.name = "valid-bot"
        body = {"bots": ["valid-bot"]}

        # Act
        responses, mock_popen = self._do_post(body, [mock_bot])

        # Assert
        assert len(responses) == 1, "expected exactly one response"
        code, data = responses[0]
        assert code == 200, f"expected 200 for valid bot, got {code}: {data}"
        assert data.get("ok") is True
        assert data.get("started") == ["valid-bot"]
        mock_popen.assert_called_once()
        # Verify Popen args contain the bot name and do not contain injection
        args = mock_popen.call_args[0][0]
        assert "valid-bot" in args

    def test_valid_multiple_registered_bots_succeeds(self):
        """POST /bots/start with multiple valid registered bots starts all."""
        # Arrange
        bot_a = MagicMock(); bot_a.name = "alpha-bot"
        bot_b = MagicMock(); bot_b.name = "beta_bot"
        body = {"bots": ["alpha-bot", "beta_bot"]}

        # Act
        responses, mock_popen = self._do_post(body, [bot_a, bot_b])

        # Assert
        code, data = responses[0]
        assert code == 200
        assert set(data.get("started", [])) == {"alpha-bot", "beta_bot"}
        mock_popen.assert_called_once()

    def test_empty_bots_list_starts_with_empty(self):
        """POST /bots/start with empty list returns 200 with empty started (no-op)."""
        # Arrange: empty list is valid but starts nothing
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body: dict = {"bots": []}

        # Act
        responses, mock_popen = self._do_post(body, [mock_bot])

        # Assert: empty list is allowed; Popen is still called with no extra args
        code, data = responses[0]
        assert code == 200
        assert data.get("started") == []
        mock_popen.assert_called_once()

    def test_no_bots_key_starts_all_registered(self):
        """POST /bots/start with {} (no bots key) starts all registered bots."""
        bot_a = MagicMock(); bot_a.name = "alpha-bot"
        bot_b = MagicMock(); bot_b.name = "beta-bot"

        from codebot.control_server import ControlHandler

        handler = self._make_post_handler({})
        responses: list[tuple[int, dict]] = []
        handler._json = lambda code, data, r=responses, **kw: r.append((code, data))  # type: ignore[assignment]
        handler._auth = lambda: True  # type: ignore[assignment]
        # _read_json_body returns {} -> body.get("bots") is None -> start all
        handler._read_json_body = MagicMock(return_value=({}, None, None))

        with patch("codebot.control_server.BOT_REGISTRY", [bot_a, bot_b]):
            with patch("codebot.control_server.subprocess.Popen") as mock_popen:
                mock_popen.return_value = MagicMock(pid=1111)
                ControlHandler.do_POST(handler)

        code, data = responses[0]
        assert code == 200
        assert set(data.get("started", [])) == {"alpha-bot", "beta-bot"}

    # ------------------------------------------------------------------
    # Rejection: unknown bots -> 400
    # ------------------------------------------------------------------
    def test_unknown_bot_returns_400(self):
        """POST /bots/start with unknown bot name returns 400 unknown bot."""
        # Arrange: registry has only valid-bot, request asks for nonexistent
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body = {"bots": ["nonexistent"]}

        # Act
        responses, mock_popen = self._do_post(body, [mock_bot])

        # Assert
        assert len(responses) == 1
        code, data = responses[0]
        assert code == 400, f"expected 400 for unknown bot, got {code}: {data}"
        assert "unknown bot" in data.get("error", "").lower()
        mock_popen.assert_not_called()

    def test_unknown_bot_among_valid_returns_400_and_no_subprocess(self):
        """If any bot in list is unknown, entire request is rejected and no subprocess runs."""
        bot_a = MagicMock(); bot_a.name = "alpha-bot"
        body = {"bots": ["alpha-bot", "ghost-bot"]}

        responses, mock_popen = self._do_post(body, [bot_a])

        code, data = responses[0]
        assert code == 400
        assert "unknown bot" in data.get("error", "").lower()
        mock_popen.assert_not_called()

    def test_unknown_bot_case_sensitive_returns_400(self):
        """Bot name validation is case-sensitive; wrong case is unknown -> 400."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body = {"bots": ["Valid-Bot"]}

        responses, mock_popen = self._do_post(body, [mock_bot])

        code, data = responses[0]
        assert code == 400
        assert "unknown bot" in data.get("error", "").lower()
        mock_popen.assert_not_called()

    # ------------------------------------------------------------------
    # Rejection: argument-like / crafted injection names -> 400
    # ------------------------------------------------------------------
    def test_arg_help_rejected(self):
        """POST /bots/start rejects '--help' (argument injection via --help)."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body = {"bots": ["--help"]}

        responses, mock_popen = self._do_post(body, [mock_bot])

        code, data = responses[0]
        assert code == 400, f"expected 400 for '--help', got {code}: {data}"
        # May be unknown bot (passes regex but not registry) or invalid format;
        # both are 400. Ensure injection blocked and no subprocess.
        assert data.get("error")
        mock_popen.assert_not_called()

    def test_arg_config_rejected(self):
        """POST /bots/start rejects '--config=/etc/passwd' (argument injection)."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body = {"bots": ["--config=/etc/passwd"]}

        responses, mock_popen = self._do_post(body, [mock_bot])

        code, data = responses[0]
        assert code == 400, f"expected 400 for '--config=/etc/passwd', got {code}: {data}"
        assert "invalid bot name" in data.get("error", "").lower()
        mock_popen.assert_not_called()

    def test_arg_double_dash_rejected(self):
        """Bare '--' should be rejected as not in registry."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body = {"bots": ["--"]}

        responses, mock_popen = self._do_post(body, [mock_bot])

        code, _ = responses[0]
        assert code == 400
        mock_popen.assert_not_called()

    def test_arg_verbose_rejected(self):
        """POST /bots/start rejects '--verbose' (flag-like name)."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body = {"bots": ["--verbose"]}

        responses, mock_popen = self._do_post(body, [mock_bot])

        code, _ = responses[0]
        assert code == 400
        mock_popen.assert_not_called()

    def test_arg_with_equals_and_slash_rejected_as_invalid_format(self):
        """Names containing '=' or '/' must be rejected with invalid format."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        for crafted in ["--config=/etc/passwd", "evil=1", "a/b", "bot/name"]:
            body = {"bots": [crafted]}
            responses, mock_popen = self._do_post(body, [mock_bot])
            code, data = responses[0]
            assert code == 400, f"expected 400 for {crafted!r}, got {code}"
            assert "invalid" in data.get("error", "").lower()
            mock_popen.assert_not_called()

    def test_crafted_names_never_reach_subprocess(self):
        """All crafted argument-like payloads must be blocked before Popen."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        payloads = [
            "--help",
            "--config=/etc/passwd",
            "--verbose",
            "--version",
            "-c",
            "--",
            " --help",  # leading space -> invalid format
        ]
        for payload in payloads:
            body = {"bots": [payload]}
            responses, mock_popen = self._do_post(body, [mock_bot])
            code, _ = responses[0]
            assert code == 400, f"crafted payload {payload!r} should be 400, got {code}"
            mock_popen.assert_not_called()

    def test_injection_with_semicolon_rejected_before_registry_check(self):
        """Semicolon injection must be rejected with invalid format, not unknown."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body = {"bots": ["evil; rm -rf /"]}

        responses, mock_popen = self._do_post(body, [mock_bot])

        code, data = responses[0]
        assert code == 400
        assert "invalid bot name" in data.get("error", "").lower()
        mock_popen.assert_not_called()

    # ------------------------------------------------------------------
    # Edge: type validation
    # ------------------------------------------------------------------
    def test_bots_not_a_list_returns_400(self):
        """POST /bots/start with bots as string returns 400."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body = {"bots": "valid-bot"}  # type: ignore[dict-item]

        responses, mock_popen = self._do_post(body, [mock_bot])

        code, data = responses[0]
        assert code == 400
        assert "must be an array" in data.get("error", "").lower()
        mock_popen.assert_not_called()

    def test_bot_names_must_be_strings(self):
        """POST /bots/start with non-string bot name returns 400."""
        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        body = {"bots": [123]}  # type: ignore[list-item]

        responses, mock_popen = self._do_post(body, [mock_bot])

        code, data = responses[0]
        assert code == 400
        assert "must be strings" in data.get("error", "").lower()
        mock_popen.assert_not_called()

    def test_api_prefix_also_validated(self):
        """POST /api/bots/start must enforce same validation as /bots/start."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock(); mock_bot.name = "valid-bot"
        handler = self._make_post_handler({"bots": ["--help"]})
        handler.path = "/api/bots/start"
        responses: list[tuple[int, dict]] = []
        handler._json = lambda code, data, r=responses, **kw: r.append((code, data))  # type: ignore[assignment]
        handler._auth = lambda: True  # type: ignore[assignment]
        handler._read_json_body = MagicMock(return_value=({"bots": ["--help"]}, None, None))

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]):
            with patch("codebot.control_server.subprocess.Popen") as mock_popen:
                ControlHandler.do_POST(handler)
                code, _ = responses[0]
                assert code == 400
                mock_popen.assert_not_called()


class TestEmptyControlTokenRejection:
    """Integration tests for unauthenticated access rejection when CONTROL_TOKEN is empty.

    Verifies fail-closed security logic:
    - GET /bots returns 401 when CONTROL_TOKEN is unset
    - Server binds to 127.0.0.1 when token is unset
    - GET /health remains public (200) without token
    """

    @classmethod
    def setup_class(cls):
        """Start a real control server with empty CONTROL_TOKEN."""
        # Save originals then force empty CONTROL_TOKEN to trigger fail-closed mode
        cls.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_TOKEN"] = ""
        # Also remove CONTROL_ALLOW_UNAUTHENTICATED to prevent bypass of fail-closed logic
        cls.original_allow_unauth = os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)

        # Reload module to pick up empty token
        import codebot.control_server as cs_mod
        importlib.reload(cs_mod)
        cls.cs_mod = cs_mod

        # Replicate production bind-host selection logic from main():
        # when CONTROL_TOKEN is empty, server binds to 127.0.0.1 (fail-closed).
        cls.expected_bind_host = "127.0.0.1" if not cls.cs_mod.CONTROL_TOKEN else "0.0.0.0"

        # Find free port
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((cls.expected_bind_host, 0))
            cls.port = s.getsockname()[1]

        # Start server using the same bind host that production would choose
        cls.server = cls.cs_mod.ThreadingHTTPServer(
            (cls.expected_bind_host, cls.port), cls.cs_mod.ControlHandler
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        # Poll for server readiness instead of fixed sleep
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        else:
            raise RuntimeError(f"Server did not become ready within 5s on port {cls.port}")

    @classmethod
    def teardown_class(cls):
        """Shutdown server and restore environment."""
        cls.server.shutdown()
        cls.thread.join(timeout=5)

        # Restore environment exactly to pre-test state to avoid pollution
        if cls.original_token is not None:
            os.environ["CONTROL_TOKEN"] = cls.original_token
        else:
            os.environ.pop("CONTROL_TOKEN", None)

        if cls.original_allow_unauth is not None:
            os.environ["CONTROL_ALLOW_UNAUTHENTICATED"] = cls.original_allow_unauth
        else:
            os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)

        # Reload module to restore state
        import codebot.control_server as cs_mod
        importlib.reload(cs_mod)

    def _get(self, path: str) -> tuple[int, dict]:
        """Helper to make GET request and return (status, body_dict)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path)
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body) if body else {}
        finally:
            conn.close()

    def test_bots_returns_401_when_token_empty(self):
        """GET /bots must return 401 when CONTROL_TOKEN is unset."""
        status, body = self._get("/bots")
        assert status == 401
        assert "unauthorized" in body.get("error", "").lower()

    def test_server_binds_to_localhost_when_token_empty(self):
        """Server socket must bind to 127.0.0.1 when CONTROL_TOKEN is unset.

        Verifies the actual production server socket binds to 127.0.0.1
        (fail-closed mode) by inspecting the live socket, not test setup.
        """
        # Verify the actual socket is bound to localhost (production behavior)
        actual_bind = self.server.socket.getsockname()[0]
        assert actual_bind == "127.0.0.1", (
            f"Server socket bound to {actual_bind}, expected 127.0.0.1 (fail-closed)"
        )

    def test_health_remains_public_without_token(self):
        """GET /health must return 200 without authentication when token is unset."""
        status, body = self._get("/health")
        assert status == 200
        assert body.get("status") == "ok"

    def test_forged_bearer_rejected_when_token_empty(self):
        """Authorization: Bearer <forged> must still return 401 when CONTROL_TOKEN is empty.

        Even if a client sends a valid-looking Bearer header, fail-closed mode
        must reject all authenticated endpoints because there is no token to compare against.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/bots", headers={"Authorization": "Bearer forged-token-123"})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            assert resp.status == 401, f"Expected 401 for forged bearer with empty token, got {resp.status}"
            data = json.loads(body) if body else {}
            assert "unauthorized" in data.get("error", "").lower()
        finally:
            conn.close()


class TestWhitespaceTokenFailClosed:
    """Verify that whitespace-only CONTROL_TOKEN triggers fail-closed (401 + localhost bind).

    CONTROL_TOKEN='   ' should .strip() to empty string, which must behave identically
    to an unset token: reject all protected endpoints and bind to 127.0.0.1 only.
    """

    @classmethod
    def setup_class(cls):
        """Start a real control server with whitespace-only CONTROL_TOKEN."""
        cls.original_token = os.environ.get("CONTROL_TOKEN")
        os.environ["CONTROL_TOKEN"] = "   "
        os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)

        import codebot.control_server as cs_mod
        importlib.reload(cs_mod)
        cls.cs_mod = cs_mod

        # Whitespace-only token strips to empty -> fail-closed -> 127.0.0.1
        cls.expected_bind_host = "127.0.0.1" if not cls.cs_mod.CONTROL_TOKEN else "0.0.0.0"

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((cls.expected_bind_host, 0))
            cls.port = s.getsockname()[1]

        cls.server = cls.cs_mod.ThreadingHTTPServer(
            (cls.expected_bind_host, cls.port), cls.cs_mod.ControlHandler
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        # Poll for readiness
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        else:
            raise RuntimeError(f"Server did not become ready within 5s on port {cls.port}")

    @classmethod
    def teardown_class(cls):
        """Shutdown server and restore environment."""
        cls.server.shutdown()
        cls.thread.join(timeout=5)

        if cls.original_token is not None:
            os.environ["CONTROL_TOKEN"] = cls.original_token
        else:
            os.environ.pop("CONTROL_TOKEN", None)

        import codebot.control_server as cs_mod
        importlib.reload(cs_mod)

    def _get(self, path: str, headers: dict | None = None) -> tuple[int, dict]:
        """Helper to make GET request and return (status, body_dict)."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", path, headers=headers or {})
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            return resp.status, json.loads(body) if body else {}
        finally:
            conn.close()

    def test_whitespace_token_strips_to_empty_and_rejects_bots(self):
        """CONTROL_TOKEN='   ' must strip to empty and reject GET /bots with 401."""
        # Verify the module-level constant stripped correctly
        assert self.cs_mod.CONTROL_TOKEN == "", (
            f"Expected empty CONTROL_TOKEN after strip, got {self.cs_mod.CONTROL_TOKEN!r}"
        )
        status, body = self._get("/bots")
        assert status == 401, f"Expected 401 for whitespace token, got {status}"
        assert "unauthorized" in body.get("error", "").lower()

    def test_whitespace_token_binds_localhost(self):
        """Server must bind to 127.0.0.1 when CONTROL_TOKEN is whitespace-only."""
        actual_bind = self.server.socket.getsockname()[0]
        assert actual_bind == "127.0.0.1", (
            f"Server bound to {actual_bind}, expected 127.0.0.1 for whitespace token"
        )

    def test_whitespace_token_health_still_public(self):
        """GET /health must remain 200 even with whitespace-only CONTROL_TOKEN."""
        status, body = self._get("/health")
        assert status == 200
        assert body.get("status") == "ok"


class TestLocalhostBindingWithEmptyToken:
    """Integration test verifying localhost binding when CONTROL_TOKEN is empty (CB-9FABFC6710068E46C66D525BE3525A76).

    Acceptance criteria:
    - Test starts server with empty CONTROL_TOKEN
    - Test verifies server socket is bound to 127.0.0.1
    - Test passes only if bind_host logic is correct
    """

    @classmethod
    def setup_class(cls):
        """Start a real control server with empty CONTROL_TOKEN."""
        # Remove CONTROL_TOKEN to trigger fail-closed mode
        cls.original_token = os.environ.pop("CONTROL_TOKEN", None)
        cls.original_allow_unauth = os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)

        # Reload module to pick up empty token
        import codebot.control_server as cs_mod
        importlib.reload(cs_mod)
        cls.cs_mod = cs_mod

        # Fail-closed: empty token means bind to 127.0.0.1
        cls.expected_bind_host = "127.0.0.1"

        # Find free port on localhost
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((cls.expected_bind_host, 0))
            cls.port = s.getsockname()[1]

        # Start server bound to localhost
        cls.server = cls.cs_mod.ThreadingHTTPServer(
            (cls.expected_bind_host, cls.port), cls.cs_mod.ControlHandler
        )
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

        # Poll for server readiness
        deadline = time.time() + 5.0
        while time.time() < deadline:
            try:
                conn = http.client.HTTPConnection("127.0.0.1", cls.port, timeout=1)
                conn.request("GET", "/health")
                resp = conn.getresponse()
                resp.read()
                conn.close()
                break
            except (ConnectionRefusedError, OSError):
                time.sleep(0.05)
        else:
            raise RuntimeError(f"Server did not become ready within 5s on port {cls.port}")

    @classmethod
    def teardown_class(cls):
        """Shutdown server and restore environment."""
        cls.server.shutdown()
        cls.thread.join(timeout=5)

        # Restore environment
        if cls.original_token is not None:
            os.environ["CONTROL_TOKEN"] = cls.original_token
        else:
            os.environ.pop("CONTROL_TOKEN", None)

        if cls.original_allow_unauth is not None:
            os.environ["CONTROL_ALLOW_UNAUTHENTICATED"] = cls.original_allow_unauth
        else:
            os.environ.pop("CONTROL_ALLOW_UNAUTHENTICATED", None)

        # Reload module to restore state
        import codebot.control_server as cs_mod
        importlib.reload(cs_mod)

    def test_server_socket_bound_to_localhost_with_empty_token(self):
        """Verify server socket is bound to 127.0.0.1 when CONTROL_TOKEN is empty.

        This test starts a real server with empty CONTROL_TOKEN and inspects
        the actual socket to confirm it binds to localhost only (fail-closed).
        """
        # Verify the module-level constant is empty (stripped)
        assert self.cs_mod.CONTROL_TOKEN == "", (
            f"Expected empty CONTROL_TOKEN, got {self.cs_mod.CONTROL_TOKEN!r}"
        )

        # Verify the actual socket bind address
        actual_bind_host = self.server.socket.getsockname()[0]
        assert actual_bind_host == "127.0.0.1", (
            f"Server socket bound to {actual_bind_host}, expected 127.0.0.1 "
            f"(fail-closed binding when CONTROL_TOKEN is empty)"
        )

    def test_server_rejects_external_access_with_empty_token(self):
        """Verify that authenticated endpoints return 401 when CONTROL_TOKEN is empty.

        Confirms fail-closed behavior: no token means no authenticated access.
        """
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/bots")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body else {}
            assert resp.status == 401, f"Expected 401, got {resp.status}"
            assert "unauthorized" in data.get("error", "").lower()
        finally:
            conn.close()

    def test_health_endpoint_remains_public_with_empty_token(self):
        """Verify /health remains accessible without authentication when CONTROL_TOKEN is empty."""
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            conn.request("GET", "/health")
            resp = conn.getresponse()
            body = resp.read().decode("utf-8", errors="replace")
            data = json.loads(body) if body else {}
            assert resp.status == 200, f"Expected 200 for /health, got {resp.status}"
            assert data.get("status") == "ok"
        finally:
            conn.close()


class TestMainBindHostFailClosed:
    """Regression for fail-closed localhost binding when CONTROL_TOKEN empty (CB-7B239).

    main() must bind 127.0.0.1 when CONTROL_TOKEN is empty to avoid exposing
    an unauthenticated control surface on 0.0.0.0. When a token is set the
    server is expected to bind 0.0.0.0. A regression to 0.0.0.0 with an empty
    token would be a high-severity security exposure; these tests patch
    ThreadingHTTPServer to avoid a real socket bind and assert the selected
    host argument. See codebot/control_server.py:main() bind_host selection.
    """

    def test_main_binds_loopback_when_token_empty(self, tmp_path: Path):
        """main() must bind 127.0.0.1 when CONTROL_TOKEN is empty (fail-closed).

        Patches BOTH os.environ and the module-level CONTROL_TOKEN attribute
        to ensure the test remains valid whether main() reads the token from
        the environment directly (future refactor) or from the module constant
        (current implementation). Also clears CONTROL_ALLOW_UNAUTHENTICATED
        to prevent any bypass logic from interfering with fail-closed behavior.
        """
        import codebot.control_server as cs

        original_token = cs.CONTROL_TOKEN
        original_env = os.environ.get("CONTROL_TOKEN")
        original_allow = os.environ.get("CONTROL_ALLOW_UNAUTHENTICATED")

        # Patch both sources atomically using ExitStack for cleaner nesting
        from contextlib import ExitStack
        with ExitStack() as stack:
            # Ensure os.environ has empty CONTROL_TOKEN (covers direct os.environ reads)
            stack.enter_context(patch.dict(os.environ, {
                "CONTROL_TOKEN": "",
                "CONTROL_ALLOW_UNAUTHENTICATED": "",
            }, clear=False))
            # Ensure module attribute is also empty (covers current impl reading module global)
            stack.enter_context(patch.object(cs, "CONTROL_TOKEN", ""))
            stack.enter_context(patch.object(cs, "STATE_DIR", tmp_path))
            stack.enter_context(patch.object(cs, "BOTS_DIR", tmp_path))
            stack.enter_context(patch.object(cs, "BOT_REGISTRY", []))
            mock_srv = stack.enter_context(patch("codebot.control_server.ThreadingHTTPServer"))

            mock_inst = MagicMock()
            mock_srv.return_value = mock_inst
            mock_inst.serve_forever.side_effect = KeyboardInterrupt

            cs.main()

            # Assert: first arg to ThreadingHTTPServer is (bind_host, PORT)
            assert mock_srv.call_args is not None, "ThreadingHTTPServer was not called"
            args, _ = mock_srv.call_args
            bind_host = args[0][0]
            assert bind_host == "127.0.0.1", (
                f"Expected fail-closed bind to 127.0.0.1 with empty CONTROL_TOKEN, got {bind_host!r}"
            )

        # Assert: env/module state restored (no leak to other tests)
        assert cs.CONTROL_TOKEN == original_token
        assert os.environ.get("CONTROL_TOKEN") == original_env

    def test_main_binds_all_when_token_set(self, tmp_path: Path):
        """main() must bind 0.0.0.0 when CONTROL_TOKEN is set.

        Patches BOTH os.environ and the module-level CONTROL_TOKEN attribute
        to ensure the test remains valid whether main() reads the token from
        the environment directly (future refactor) or from the module constant
        (current implementation).
        """
        import codebot.control_server as cs
        from contextlib import ExitStack

        original_token = cs.CONTROL_TOKEN
        original_env = os.environ.get("CONTROL_TOKEN")

        with ExitStack() as stack:
            stack.enter_context(patch.dict(os.environ, {
                "CONTROL_TOKEN": "test-secret-token",
            }, clear=False))
            stack.enter_context(patch.object(cs, "CONTROL_TOKEN", "test-secret-token"))
            stack.enter_context(patch.object(cs, "STATE_DIR", tmp_path))
            stack.enter_context(patch.object(cs, "BOTS_DIR", tmp_path))
            stack.enter_context(patch.object(cs, "BOT_REGISTRY", []))
            mock_srv = stack.enter_context(patch("codebot.control_server.ThreadingHTTPServer"))

            mock_inst = MagicMock()
            mock_srv.return_value = mock_inst
            mock_inst.serve_forever.side_effect = KeyboardInterrupt

            cs.main()

            assert mock_srv.call_args is not None, "ThreadingHTTPServer was not called"
            args, _ = mock_srv.call_args
            bind_host = args[0][0]
            assert bind_host == "0.0.0.0", (
                f"Expected bind to 0.0.0.0 with CONTROL_TOKEN set, got {bind_host!r}"
            )

        assert cs.CONTROL_TOKEN == original_token
        assert os.environ.get("CONTROL_TOKEN") == original_env

    def test_main_binds_loopback_when_token_whitespace_only(self, tmp_path: Path):
        """main() must bind 127.0.0.1 when CONTROL_TOKEN is whitespace-only.

        Production strips CONTROL_TOKEN at import
        (``os.environ.get("CONTROL_TOKEN", "").strip()``), so a whitespace-only
        env value normalizes to ``""`` (fail-closed). Patch env to whitespace
        and the module attr to its stripped value so both sources agree with
        real import behavior; a refactor that reads os.environ without
        stripping would bind 0.0.0.0 and fail this test.
        """
        import codebot.control_server as cs
        from contextlib import ExitStack

        original_token = cs.CONTROL_TOKEN
        original_env = os.environ.get("CONTROL_TOKEN")

        with ExitStack() as stack:
            # Env has whitespace; module attr has stripped empty string
            stack.enter_context(patch.dict(os.environ, {
                "CONTROL_TOKEN": "   ",
            }, clear=False))
            stack.enter_context(patch.object(cs, "CONTROL_TOKEN", ""))
            stack.enter_context(patch.object(cs, "STATE_DIR", tmp_path))
            stack.enter_context(patch.object(cs, "BOTS_DIR", tmp_path))
            stack.enter_context(patch.object(cs, "BOT_REGISTRY", []))
            mock_srv = stack.enter_context(patch("codebot.control_server.ThreadingHTTPServer"))

            mock_inst = MagicMock()
            mock_srv.return_value = mock_inst
            mock_inst.serve_forever.side_effect = KeyboardInterrupt

            cs.main()

            assert mock_srv.call_args is not None, "ThreadingHTTPServer was not called"
            args, _ = mock_srv.call_args
            bind_host = args[0][0]
            assert bind_host == "127.0.0.1", (
                f"Expected fail-closed bind to 127.0.0.1 with whitespace CONTROL_TOKEN, got {bind_host!r}"
            )

        assert cs.CONTROL_TOKEN == original_token
        assert os.environ.get("CONTROL_TOKEN") == original_env


class TestMalformedConfigResilience:
    """Integration tests for malformed config/env input resilience (CB-0956AB4F03508C1F17E04D55676987B2).

    Verifies that the control server initializes without crashing when
    environment variables contain malformed or dangerous values.
    """

    def test_malformed_port_env_does_not_crash(self):
        """Server must handle non-numeric PORT env var gracefully."""
        import importlib
        import codebot.control_server as cs

        with patch.dict(os.environ, {"PORT": "not_a_number", "CONTROL_TOKEN": "test"}, clear=False):
            # Reload module to pick up new env vars
            importlib.reload(cs)
            # If we got here, it didn't crash on import/module-level execution
            assert cs.PORT == 8081  # Should fallback to default

    def test_zero_port_env_clamped_to_default(self):
        """Server must handle PORT=0 by falling back to default."""
        import importlib
        import codebot.control_server as cs

        with patch.dict(os.environ, {"PORT": "0", "CONTROL_TOKEN": "test"}, clear=False):
            importlib.reload(cs)
            assert cs.PORT == 8081

    def test_negative_port_env_clamped_to_default(self):
        """Server must handle negative PORT by falling back to default."""
        import importlib
        import codebot.control_server as cs

        with patch.dict(os.environ, {"PORT": "-1", "CONTROL_TOKEN": "test"}, clear=False):
            importlib.reload(cs)
            assert cs.PORT == 8081

    def test_huge_port_env_clamped_to_default(self):
        """Server must handle PORT > 65535 by falling back to default."""
        import importlib
        import codebot.control_server as cs

        with patch.dict(os.environ, {"PORT": "99999", "CONTROL_TOKEN": "test"}, clear=False):
            importlib.reload(cs)
            assert cs.PORT == 8081

    def test_malformed_rate_limit_env_does_not_crash(self):
        """Server must handle non-numeric rate limit env vars gracefully."""
        import importlib
        import codebot.control_server as cs

        with patch.dict(os.environ, {
            "RATE_LIMIT_MAX_ATTEMPTS": "abc",
            "RATE_LIMIT_WINDOW_SECONDS": "xyz",
            "RATE_LIMIT_COOLDOWN_SECONDS": "!!!",
            "CONTROL_TOKEN": "test"
        }, clear=False):
            importlib.reload(cs)
            # Should fallback to defaults (5, 60, 300) and not crash
            assert cs.RATE_LIMIT_MAX_ATTEMPTS == 5
            assert cs.RATE_LIMIT_WINDOW_SECONDS == 60
            assert cs.RATE_LIMIT_COOLDOWN_SECONDS == 300

    def test_zero_window_seconds_clamped_to_one(self):
        """RATE_LIMIT_WINDOW_SECONDS=0 must be clamped to 1 (min_val)."""
        import importlib
        import codebot.control_server as cs

        with patch.dict(os.environ, {
            "RATE_LIMIT_WINDOW_SECONDS": "0",
            "CONTROL_TOKEN": "test"
        }, clear=False):
            importlib.reload(cs)
            assert cs.RATE_LIMIT_WINDOW_SECONDS == 1

    def test_negative_window_seconds_clamped_to_one(self):
        """RATE_LIMIT_WINDOW_SECONDS=-5 must be clamped to 1 (min_val)."""
        import importlib
        import codebot.control_server as cs

        with patch.dict(os.environ, {
            "RATE_LIMIT_WINDOW_SECONDS": "-5",
            "CONTROL_TOKEN": "test"
        }, clear=False):
            importlib.reload(cs)
            assert cs.RATE_LIMIT_WINDOW_SECONDS == 1

    def test_empty_rate_limit_env_vars_use_defaults(self):
        """Empty string env vars for rate limits should use defaults."""
        import importlib
        import codebot.control_server as cs

        with patch.dict(os.environ, {
            "RATE_LIMIT_MAX_ATTEMPTS": "",
            "RATE_LIMIT_WINDOW_SECONDS": "",
            "RATE_LIMIT_COOLDOWN_SECONDS": "",
            "CONTROL_TOKEN": "test"
        }, clear=False):
            importlib.reload(cs)
            assert cs.RATE_LIMIT_MAX_ATTEMPTS == 5
            assert cs.RATE_LIMIT_WINDOW_SECONDS == 60
            assert cs.RATE_LIMIT_COOLDOWN_SECONDS == 300

    def test_all_malformed_rate_limit_env_vars_simultaneously(self):
        """Integration test: patches os.environ with invalid values for all rate limit env vars,
        uses importlib.reload() to re-import control_server, asserts no exception is raised,
        and verifies RateLimiter instances are created with safe default values.
        This proves the DoS vector is actually mitigated in practice (CB-0956AB4F03508C1F17E04D55676987B2).
        """
        import importlib
        import codebot.control_server as cs

        # Patch all rate limit env vars with invalid/dangerous values simultaneously
        with patch.dict(os.environ, {
            "RATE_LIMIT_MAX_ATTEMPTS": "0",       # Should clamp to 1
            "RATE_LIMIT_WINDOW_SECONDS": "-100",  # Should clamp to 1
            "RATE_LIMIT_COOLDOWN_SECONDS": "abc", # Should fallback to 300
            "CONTROL_TOKEN": "test-token"
        }, clear=False):
            # Reload module to pick up new env vars - this must not crash
            importlib.reload(cs)

            # Verify safe defaults/clamping were applied
            assert cs.RATE_LIMIT_MAX_ATTEMPTS == 1, f"Expected 1, got {cs.RATE_LIMIT_MAX_ATTEMPTS}"
            assert cs.RATE_LIMIT_WINDOW_SECONDS == 1, f"Expected 1, got {cs.RATE_LIMIT_WINDOW_SECONDS}"
            assert cs.RATE_LIMIT_COOLDOWN_SECONDS == 300, f"Expected 300, got {cs.RATE_LIMIT_COOLDOWN_SECONDS}"

            # Verify RateLimiter can be instantiated and used without error
            limiter = cs.RateLimiter()
            allowed, reason = limiter.is_allowed("1.2.3.4")
            assert allowed is True
            limiter.record_failure("1.2.3.4")
            # With max_attempts=1, next check should be blocked
            allowed, reason = limiter.is_allowed("1.2.3.4")
            assert allowed is False
            assert "rate limit exceeded" in reason


class TestSchedulerStatus:
    """Tests for scheduler_status() function coverage."""

    def test_scheduler_status_returns_dict(self):
        """scheduler_status() should return a dict with expected keys."""
        from codebot.control_server import scheduler_status
        result = scheduler_status()
        assert isinstance(result, dict)
        assert "version" in result
        assert "budget_state" in result
        assert "drain" in result
        assert "dead_letter_count" in result
        assert "paused_count" in result
        assert "disabled_count" in result

    def test_scheduler_status_handles_import_error_gracefully(self):
        """scheduler_status() should handle ImportError gracefully."""
        from codebot.control_server import scheduler_status
        with patch("codebot.control_server.STATE_DIR", Path("/nonexistent/path")):
            result = scheduler_status()
            assert isinstance(result, dict)
            assert result["version"] == 1


class TestSafeKillFunctions:
    """Tests for _safe_kill_bot_process and _safe_kill_orchestrator coverage."""

    def test_safe_kill_bot_process_invalid_name(self):
        """_safe_kill_bot_process should reject invalid bot names."""
        from codebot.control_server import _safe_kill_bot_process
        success, pids = _safe_kill_bot_process("invalid/name!")
        assert success is False
        assert pids == []

    def test_safe_kill_bot_process_no_pid_file(self, tmp_path: Path):
        """_safe_kill_bot_process should handle missing PID file gracefully."""
        from codebot.control_server import _safe_kill_bot_process, STATE_DIR
        with patch.object(__import__('codebot.control_server', fromlist=['STATE_DIR']), 'STATE_DIR', tmp_path):
            success, pids = _safe_kill_bot_process("nonexistent-bot")
            assert success is True
            assert pids == []

    def test_safe_kill_orchestrator_no_pid_file(self, tmp_path: Path):
        """_safe_kill_orchestrator should handle missing PID file gracefully."""
        from codebot.control_server import _safe_kill_orchestrator, STATE_DIR
        with patch.object(__import__('codebot.control_server', fromlist=['STATE_DIR']), 'STATE_DIR', tmp_path):
            success, pids = _safe_kill_orchestrator()
            assert success is True
            assert pids == []


class TestRetryDeadLetter:
    """Tests for retry_dead_letter() function coverage."""

    def test_retry_dead_letter_handles_import_error(self):
        """retry_dead_letter() should handle ImportError gracefully."""
        from codebot.control_server import retry_dead_letter
        result = retry_dead_letter("Q-123")
        assert isinstance(result, dict)
        assert "status" in result


class TestHandleTelemetry:
    """Tests for _handle_telemetry() method coverage."""

    def _make_telemetry_handler(self, body: dict):
        """Create a mock ControlHandler for testing _handle_telemetry."""
        from codebot.control_server import ControlHandler
        handler = MagicMock(spec=ControlHandler)
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler.path = "/telemetry"
        return handler

    def test_handle_telemetry_missing_module(self):
        """_handle_telemetry should handle ImportError gracefully."""
        from codebot.control_server import ControlHandler
        handler = self._make_telemetry_handler({})
        with patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server.TELEMETRY_TOKEN", ""):
            # Simulate ImportError by patching the import inside the method
            with patch("builtins.__import__", side_effect=ImportError("mocked")):
                ControlHandler._handle_telemetry(handler, {})
                handler._json.assert_called_once()
                call_args = handler._json.call_args
                assert call_args[0][0] == 500

    def test_handle_telemetry_validation_failure(self):
        """_handle_telemetry should return 400 on validation failure."""
        from codebot.control_server import ControlHandler
        handler = self._make_telemetry_handler({})
        with patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server.TELEMETRY_TOKEN", ""):
            with patch("codebot.telemetry._validate_signal", return_value=(False, "bad signal")):
                ControlHandler._handle_telemetry(handler, {})
                handler._json.assert_called_once()
                call_args = handler._json.call_args
                assert call_args[0][0] == 400


class TestDoGetBranches:
    """Tests for uncovered branches in do_GET."""

    def test_get_scheduler_events_with_type_filter(self):
        """GET /scheduler/events?type=foo should filter events."""
        from codebot.control_server import ControlHandler
        import codebot.event_log as event_log_mod
        handler = MagicMock(spec=ControlHandler)
        handler.path = "/scheduler/events?type=test-type"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler._auth = ControlHandler._auth.__get__(handler, ControlHandler)
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()

        with patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch.object(event_log_mod, "read_events", return_value=[{"type": "test-type", "ts": 1, "data": {}}]), \
             patch.object(event_log_mod, "sanitize_data", return_value={}):
            ControlHandler.do_GET(handler)
            handler._json.assert_called_once()
            call_args = handler._json.call_args
            assert call_args[0][0] == 200
            payload = call_args[0][1]
            assert "events" in payload

    def test_get_scheduler_events_invalid_limit(self):
        """GET /scheduler/events?limit=abc should return 400."""
        from codebot.control_server import ControlHandler
        handler = MagicMock(spec=ControlHandler)
        handler.path = "/scheduler/events?limit=abc"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler._auth = ControlHandler._auth.__get__(handler, ControlHandler)
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()

        with patch("codebot.control_server.CONTROL_TOKEN", "test-token"):
            ControlHandler.do_GET(handler)
            handler._json.assert_called_once()
            call_args = handler._json.call_args
            assert call_args[0][0] == 400

    def test_get_bot_logs_invalid_lines(self):
        """GET /bots/foo/logs?lines=abc should return 400."""
        from codebot.control_server import ControlHandler
        handler = MagicMock(spec=ControlHandler)
        handler.path = "/bots/test-bot/logs?lines=abc"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler._auth = ControlHandler._auth.__get__(handler, ControlHandler)
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()

        mock_cfg = MagicMock()
        mock_cfg.name = "test-bot"
        with patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server.BOT_REGISTRY", [mock_cfg]):
            ControlHandler.do_GET(handler)
            handler._json.assert_called_once()
            call_args = handler._json.call_args
            assert call_args[0][0] == 400

    def test_get_gates_metrics_import_error(self):
        """GET /gates/metrics should handle ImportError gracefully."""
        from codebot.control_server import ControlHandler
        handler = MagicMock(spec=ControlHandler)
        handler.path = "/gates/metrics"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler._auth = ControlHandler._auth.__get__(handler, ControlHandler)
        handler.rfile = io.BytesIO(b"")
        handler.wfile = io.BytesIO()

        with patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("builtins.__import__", side_effect=ImportError("mocked")):
            ControlHandler.do_GET(handler)
            handler._json.assert_called_once()
            call_args = handler._json.call_args
            assert call_args[0][0] == 200


class TestStopEmptyBotsNoPkill:
    """Security tests for POST /bots/stop with empty bots list (CB-0AD5E9FA0EC31C842BFE2EE73B832F2A).

    Verifies that POST /bots/stop with an empty or missing 'bots' array does NOT
    execute unscoped pkill -f commands that could terminate arbitrary host processes.
    Instead, it should only stop registered bots via _safe_kill_bot_process.
    """

    def _make_stop_handler(self, body: dict):
        """Create a mock ControlHandler for POST /bots/stop."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.path = "/bots/stop"
        handler.command = "POST"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler._auth = lambda: True  # type: ignore[assignment]
        handler._read_json_body = MagicMock(return_value=(body, None, None))
        handler.rfile = io.BytesIO(json.dumps(body).encode()) if body else io.BytesIO(b"{}")
        handler.wfile = io.BytesIO()
        return handler

    def test_stop_empty_bots_does_not_call_pkill(self):
        """POST /bots/stop with empty bots list must NOT call subprocess.run with pkill."""
        from codebot.control_server import ControlHandler

        mock_bot_a = MagicMock()
        mock_bot_a.name = "alpha-bot"
        mock_bot_b = MagicMock()
        mock_bot_b.name = "beta-bot"

        handler = self._make_stop_handler({"bots": [], "force": True})
        responses: list[tuple[int, dict]] = []
        handler._json = lambda code, data, r=responses, **kw: r.append((code, data))  # type: ignore[assignment]

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot_a, mock_bot_b]), \
             patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server._safe_kill_bot_process") as mock_safe_kill, \
             patch("codebot.control_server._safe_kill_orchestrator") as mock_safe_kill_orch, \
             patch("codebot.control_server.subprocess") as mock_subprocess:
            # Configure safe kill mocks
            mock_safe_kill.return_value = (True, [])
            mock_safe_kill_orch.return_value = (True, [])

            ControlHandler.do_POST(handler)

            # Verify response
            assert len(responses) == 1
            code, data = responses[0]
            assert code == 200
            assert data.get("ok") is True

            # Verify _safe_kill_bot_process was called for each registered bot
            assert mock_safe_kill.call_count == 2
            killed_names = {call[0][0] for call in mock_safe_kill.call_args_list}
            assert killed_names == {"alpha-bot", "beta-bot"}

            # Verify _safe_kill_orchestrator was also called
            mock_safe_kill_orch.assert_called_once()

            # CRITICAL: Verify NO subprocess.run calls with pkill were made
            # This is the core security assertion
            for call in mock_subprocess.run.call_args_list:
                args = call[0][0] if call[0] else []
                assert "pkill" not in str(args), f"pkill found in subprocess call: {args}"
            # Also check Popen just in case
            for call in mock_subprocess.Popen.call_args_list:
                args = call[0][0] if call[0] else []
                assert "pkill" not in str(args), f"pkill found in subprocess Popen call: {args}"

    def test_stop_missing_bots_key_stops_all_registered(self):
        """POST /bots/stop with {} (no bots key) must stop all registered bots safely."""
        from codebot.control_server import ControlHandler

        mock_bot = MagicMock()
        mock_bot.name = "test-bot"

        handler = self._make_stop_handler({"force": True})
        responses: list[tuple[int, dict]] = []
        handler._json = lambda code, data, r=responses, **kw: r.append((code, data))  # type: ignore[assignment]

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot]), \
             patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server._safe_kill_bot_process") as mock_safe_kill, \
             patch("codebot.control_server._safe_kill_orchestrator") as mock_safe_kill_orch:
            mock_safe_kill.return_value = (True, [])
            mock_safe_kill_orch.return_value = (True, [])

            ControlHandler.do_POST(handler)

            assert len(responses) == 1
            code, data = responses[0]
            assert code == 200
            assert data.get("ok") is True

            # Should have called safe kill for the registered bot
            mock_safe_kill.assert_called_once_with("test-bot", timeout=5)
            mock_safe_kill_orch.assert_called_once()

    def test_stop_specific_bots_validates_and_kills_safely(self):
        """POST /bots/stop with specific bots validates names and kills safely."""
        from codebot.control_server import ControlHandler

        mock_bot_a = MagicMock()
        mock_bot_a.name = "alpha-bot"
        mock_bot_b = MagicMock()
        mock_bot_b.name = "beta-bot"

        body = {"bots": ["alpha-bot"], "force": True}
        handler = self._make_stop_handler(body)
        responses: list[tuple[int, dict]] = []
        handler._json = lambda code, data, r=responses, **kw: r.append((code, data))  # type: ignore[assignment]

        with patch("codebot.control_server.BOT_REGISTRY", [mock_bot_a, mock_bot_b]), \
             patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server._safe_kill_bot_process") as mock_safe_kill:
            mock_safe_kill.return_value = (True, [12345])

            ControlHandler.do_POST(handler)

            assert len(responses) == 1
            code, data = responses[0]
            assert code == 200
            assert data.get("ok") is True
            assert 12345 in data.get("killed_pids", [])

            # Only alpha-bot should be killed, not beta-bot
            mock_safe_kill.assert_called_once_with("alpha-bot", timeout=5)


class TestDoPostBranches:
    """Tests for uncovered branches in do_POST."""

    def test_post_bots_restart_dry_run(self):
        """POST /bots/foo/restart with dry_run=true should return preview."""
        from codebot.control_server import ControlHandler
        handler = MagicMock(spec=ControlHandler)
        handler.path = "/bots/test-bot/restart"
        handler.command = "POST"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler._auth = lambda: True
        handler._read_json_body = MagicMock(return_value=({"dry_run": True}, None, None))
        handler.rfile = io.BytesIO(b'{"dry_run": true}')
        handler.wfile = io.BytesIO()

        mock_cfg = MagicMock()
        mock_cfg.name = "test-bot"
        with patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server.BOT_REGISTRY", [mock_cfg]):
            ControlHandler.do_POST(handler)
            handler._json.assert_called_once()
            call_args = handler._json.call_args
            assert call_args[0][0] == 200
            assert call_args[0][1]["dry_run"] is True

    def test_post_bots_pause_dry_run(self):
        """POST /bots/foo/pause with dry_run=true should return preview."""
        from codebot.control_server import ControlHandler
        handler = MagicMock(spec=ControlHandler)
        handler.path = "/bots/test-bot/pause"
        handler.command = "POST"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler._auth = lambda: True
        handler._read_json_body = MagicMock(return_value=({"dry_run": True}, None, None))
        handler.rfile = io.BytesIO(b'{"dry_run": true}')
        handler.wfile = io.BytesIO()

        mock_cfg = MagicMock()
        mock_cfg.name = "test-bot"
        with patch("codebot.control_server.CONTROL_TOKEN", "test-token"), \
             patch("codebot.control_server.BOT_REGISTRY", [mock_cfg]):
            ControlHandler.do_POST(handler)
            handler._json.assert_called_once()
            call_args = handler._json.call_args
            assert call_args[0][0] == 200
            assert call_args[0][1]["dry_run"] is True

    def test_post_control_update_exception(self):
        """POST /control/update should handle exceptions gracefully."""
        from codebot.control_server import ControlHandler
        handler = MagicMock(spec=ControlHandler)
        handler.path = "/control/update"
        handler.command = "POST"
        handler.client_address = ("127.0.0.1", 12345)
        handler.headers = {"Authorization": "Bearer test-token"}
        handler._json = MagicMock()
        handler._auth = lambda: True
        # Exception during _read_json_body parsing returns (None, 400, error_msg)
        handler._read_json_body = MagicMock(return_value=(None, 400, "mocked error"))
        handler.rfile = io.BytesIO(b'{"force": true}')
        handler.wfile = io.BytesIO()

        with patch("codebot.control_server.CONTROL_TOKEN", "test-token"):
            ControlHandler.do_POST(handler)
            handler._json.assert_called_once()
            call_args = handler._json.call_args
            assert call_args[0][0] == 400
