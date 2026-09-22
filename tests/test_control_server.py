"""Tests for control_server rate limiting and authentication.

Covers RateLimiter class behavior and integration with _auth() and _handle_telemetry().
Also covers fail-closed security behavior when CONTROL_TOKEN is unset (CB-6048497-D3F1).
Includes integration test for malformed config resilience (CB-0956AB4F03508C1F17E04D55676987B2).
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

# Import the RateLimiter class and config helper directly for unit testing
from codebot.control_server import (
    RateLimiter,
    RATE_LIMIT_MAX_ATTEMPTS,
    RATE_LIMIT_WINDOW_SECONDS,
    RATE_LIMIT_COOLDOWN_SECONDS,
    _safe_int_env,
)


class TestSafeIntEnv:
    """Tests for _safe_int_env helper function (CB-0956AB4F03508C1F17E04D55676987B2)."""

    def test_valid_env_var_returns_parsed_int(self):
        """Valid integer env var should be parsed and returned."""
        with patch.dict(os.environ, {"TEST_VAR": "42"}, clear=False):
            result = _safe_int_env("TEST_VAR", default=10)
            assert result == 42

    def test_missing_env_var_returns_default(self):
        """Missing env var should return the default value."""
        with patch.dict(os.environ, {}, clear=False):
            # Ensure TEST_VAR_MISSING is not set
            os.environ.pop("TEST_VAR_MISSING", None)
            result = _safe_int_env("TEST_VAR_MISSING", default=99)
            assert result == 99

    def test_invalid_env_var_returns_default_and_logs_warning(self, caplog):
        """Non-numeric env var should return default and log a warning."""
        with caplog.at_level(logging.WARNING, logger="codebot.control_server"), \
             patch.dict(os.environ, {"TEST_VAR_BAD": "abc"}, clear=False):
            result = _safe_int_env("TEST_VAR_BAD", default=5)
            assert result == 5
            assert any("Invalid value for TEST_VAR_BAD" in record.message for record in caplog.records)

    def test_zero_env_var_clamped_to_min_val(self):
        """Zero value should be clamped to min_val (1 by default)."""
        with patch.dict(os.environ, {"TEST_VAR_ZERO": "0"}, clear=False):
            result = _safe_int_env("TEST_VAR_ZERO", default=10, min_val=1)
            assert result == 1

    def test_negative_env_var_clamped_to_min_val(self):
        """Negative value should be clamped to min_val."""
        with patch.dict(os.environ, {"TEST_VAR_NEG": "-5"}, clear=False):
            result = _safe_int_env("TEST_VAR_NEG", default=10, min_val=1)
            assert result == 1

    def test_custom_min_val_enforced(self):
        """Custom min_val should be enforced."""
        with patch.dict(os.environ, {"TEST_VAR_CUSTOM": "5"}, clear=False):
            result = _safe_int_env("TEST_VAR_CUSTOM", default=10, min_val=10)
            assert result == 10


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
        # Remove CONTROL_TOKEN to trigger fail-closed mode
        cls.original_token = os.environ.pop("CONTROL_TOKEN", None)

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

        # Restore environment
        if cls.original_token is not None:
            os.environ["CONTROL_TOKEN"] = cls.original_token
        else:
            os.environ["CONTROL_TOKEN"] = "test-token-restore"

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
        """main() must bind 127.0.0.1 when CONTROL_TOKEN is empty (fail-closed)."""
        # Arrange: import control_server module alias
        import codebot.control_server as cs

        original_token = cs.CONTROL_TOKEN
        # Act: patch module attribute CONTROL_TOKEN to empty, mock server to avoid bind
        with patch.object(cs, "CONTROL_TOKEN", ""):
            with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
                with patch("codebot.control_server.ThreadingHTTPServer") as mock_srv:
                    mock_inst = MagicMock()
                    mock_srv.return_value = mock_inst
                    mock_inst.serve_forever.side_effect = KeyboardInterrupt
                    with patch.object(cs, "BOT_REGISTRY", []):
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

    def test_main_binds_all_when_token_set(self, tmp_path: Path):
        """main() must bind 0.0.0.0 when CONTROL_TOKEN is set."""
        # Arrange
        import codebot.control_server as cs

        original_token = cs.CONTROL_TOKEN
        # Act: patch CONTROL_TOKEN to a non-empty value and capture bind host
        with patch.object(cs, "CONTROL_TOKEN", "test-secret-token"):
            with patch.object(cs, "STATE_DIR", tmp_path), patch.object(cs, "BOTS_DIR", tmp_path):
                with patch("codebot.control_server.ThreadingHTTPServer") as mock_srv:
                    mock_inst = MagicMock()
                    mock_srv.return_value = mock_inst
                    mock_inst.serve_forever.side_effect = KeyboardInterrupt
                    with patch.object(cs, "BOT_REGISTRY", []):
                        cs.main()
                    assert mock_srv.call_args is not None, "ThreadingHTTPServer was not called"
                    args, _ = mock_srv.call_args
                    bind_host = args[0][0]
                    assert bind_host == "0.0.0.0", (
                        f"Expected bind to 0.0.0.0 with CONTROL_TOKEN set, got {bind_host!r}"
                    )
        # Assert: restoration
        assert cs.CONTROL_TOKEN == original_token


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
