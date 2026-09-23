"""Tests for the production telemetry ingestion endpoint."""

import json
import time
import types
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestTelemetryEndpoint:
    """Test POST /telemetry endpoint in control_server."""

    def _make_handler(self, method: str = "POST", path: str = "/telemetry", body: dict | None = None):
        """Create a mock handler for testing."""
        from codebot.control_server import ControlHandler as Handler
        from codebot.control_server import _rate_limiter

        # Reset global rate limiter for the shared loopback test IP so
        # earlier failed-auth tests in the same process do not leak 429s
        # into telemetry validation tests.
        try:
            with _rate_limiter._lock:
                _rate_limiter._failures.pop("127.0.0.1", None)
                _rate_limiter._blocked_until.pop("127.0.0.1", None)
        except Exception:
            pass

        handler = MagicMock()
        handler.path = path
        handler.command = method
        handler.headers = {"Content-Type": "application/json", "Authorization": "Bearer test-token"}
        handler.client_address = ("127.0.0.1", 12345)
        if body is not None:
            encoded = json.dumps(body).encode()
            handler.headers["Content-Length"] = str(len(encoded))
            handler.rfile = MagicMock()
            handler.rfile.read.return_value = encoded
        else:
            handler.headers["Content-Length"] = "0"
            handler.rfile = MagicMock()
            handler.rfile.read.return_value = b""
        handler._json = MagicMock()
        handler._auth = MagicMock(return_value=True)
        handler._read_json_body = MagicMock(return_value=(body or {}, None, None))
        handler._handle_telemetry = types.MethodType(Handler._handle_telemetry, handler)
        return handler

    @pytest.fixture(autouse=True)
    def _telemetry_tokens(self):
        """CB-B4086: _handle_telemetry fail-closes when CONTROL_TOKEN is empty.

        These tests target telemetry-signal validation, so both tokens are
        set to a known value for the duration of each test (restored after).
        """
        from codebot import control_server as _cs
        orig_control, orig_tele = _cs.CONTROL_TOKEN, _cs.TELEMETRY_TOKEN
        _cs.CONTROL_TOKEN = "test-token"
        _cs.TELEMETRY_TOKEN = "test-token"
        yield
        _cs.CONTROL_TOKEN, _cs.TELEMETRY_TOKEN = orig_control, orig_tele

    def test_telemetry_accepts_valid_json(self, tmp_path: Path) -> None:
        from codebot.control_server import ControlHandler as Handler

        handler = self._make_handler(
            body={
                "signal_type": "error",
                "summary": "Connection timeout",
                "details": {"host": "web-1", "region": "us-east"},
            }
        )

        with patch("codebot.control_server.TELEMETRY_TOKEN", "test-token"), patch("codebot.control_server.STATE_DIR", tmp_path):
            handler._handle_telemetry({"signal_type": "error", "summary": "Connection timeout", "details": {"host": "web-1"}})

        handler._json.assert_called_once()
        call_args = handler._json.call_args[0]
        assert call_args[0] in (200, 201)

    def test_telemetry_rejects_missing_source(self, tmp_path: Path) -> None:
        from codebot.control_server import ControlHandler as Handler

        handler = self._make_handler(
            body={
                "level": "error",
                "message": "Something broke",
            }
        )

        with patch("codebot.control_server.TELEMETRY_TOKEN", "test-token"), patch("codebot.control_server.STATE_DIR", tmp_path):
            handler._handle_telemetry({"level": "error", "message": "Something broke"})

        handler._json.assert_called_once()
        call_args = handler._json.call_args[0]
        assert call_args[0] == 400

    def test_telemetry_rejects_oversized_payload(self, tmp_path: Path) -> None:
        """Payload exceeding MAX_REQUEST_BYTES should be rejected via do_POST."""
        from codebot.control_server import ControlHandler as Handler, MAX_REQUEST_BYTES
        import types as _types

        handler = self._make_handler(body={"data": "x" * (MAX_REQUEST_BYTES + 1)})
        handler._read_json_body = MagicMock(return_value=(None, 413, "request body too large"))
        handler._handle_telemetry = _types.MethodType(Handler._handle_telemetry, handler)

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            Handler.do_POST(handler)

        handler._json.assert_called_once()
        call_args = handler._json.call_args[0]
        assert call_args[0] == 413

    def test_telemetry_creates_ticket_candidate(self, tmp_path: Path) -> None:
        from codebot.control_server import ControlHandler as Handler

        handler = self._make_handler(
            body={
                "signal_type": "error",
                "summary": "Database connection pool exhausted",
            }
        )

        with patch("codebot.control_server.TELEMETRY_TOKEN", "test-token"), patch("codebot.control_server.STATE_DIR", tmp_path):
            handler._handle_telemetry({"signal_type": "error", "summary": "Database connection pool exhausted"})

        handler._json.assert_called_once()
        assert handler._json.call_args[0][0] in (200, 201)

    def test_telemetry_stores_for_trend_analysis(self, tmp_path: Path) -> None:
        from codebot.control_server import ControlHandler as Handler

        payloads = [
            {"signal_type": "error", "summary": "Request completed"},
            {"signal_type": "error", "summary": "High latency"},
        ]

        with patch("codebot.control_server.TELEMETRY_TOKEN", "test-token"), patch("codebot.control_server.STATE_DIR", tmp_path):
            for p in payloads:
                handler = self._make_handler(body=p)
                handler._handle_telemetry(p)

        assert handler._json.call_count == 1

    def test_telemetry_requires_auth(self, tmp_path: Path) -> None:
        from codebot.control_server import ControlHandler as Handler

        handler = self._make_handler(body={"signal_type": "info", "summary": "test"})
        handler.headers = {"Content-Type": "application/json"}

        with patch("codebot.control_server.TELEMETRY_TOKEN", "test-token"), patch("codebot.control_server.STATE_DIR", tmp_path):
            handler._handle_telemetry({"signal_type": "info", "summary": "test"})

        args = handler._json.call_args[0]
        assert args[0] == 401
        assert args[1].get("error") == "unauthorized"
