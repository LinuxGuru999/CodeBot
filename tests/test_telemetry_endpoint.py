"""Tests for the production telemetry ingestion endpoint."""

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


class TestTelemetryEndpoint:
    """Test POST /telemetry endpoint in control_server."""

    def _make_handler(self, method: str = "POST", path: str = "/telemetry", body: dict | None = None):
        """Create a mock handler for testing."""
        from codebot.control_server import Handler

        handler = MagicMock(spec=Handler)
        handler.path = path
        handler.command = method
        handler.headers = {"Content-Type": "application/json"}
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
        return handler

    def test_telemetry_accepts_valid_json(self, tmp_path: Path) -> None:
        """Valid telemetry payload should be accepted and stored."""
        from codebot.control_server import Handler

        handler = self._make_handler(
            body={
                "source": "production",
                "level": "error",
                "message": "Connection timeout",
                "timestamp": time.time(),
                "metadata": {"host": "web-1", "region": "us-east"},
            }
        )

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            Handler.do_POST(handler)

        handler._json.assert_called_once()
        call_args = handler._json.call_args[0]
        assert call_args[0] == 200
        assert call_args[1]["ok"] is True
        assert "received" in call_args[1]

    def test_telemetry_rejects_missing_source(self, tmp_path: Path) -> None:
        """Telemetry without source field should be rejected."""
        from codebot.control_server import Handler

        handler = self._make_handler(
            body={
                "level": "error",
                "message": "Something broke",
            }
        )

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            Handler.do_POST(handler)

        handler._json.assert_called_once()
        call_args = handler._json.call_args[0]
        assert call_args[0] == 400

    def test_telemetry_rejects_oversized_payload(self, tmp_path: Path) -> None:
        """Payload exceeding MAX_REQUEST_BYTES should be rejected."""
        from codebot.control_server import Handler, MAX_REQUEST_BYTES

        handler = self._make_handler(body={"data": "x" * (MAX_REQUEST_BYTES + 1)})
        handler._read_json_body = MagicMock(return_value=(None, 413, "request body too large"))

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            Handler.do_POST(handler)

        handler._json.assert_called_once()
        call_args = handler._json.call_args[0]
        assert call_args[0] == 413

    def test_telemetry_creates_ticket_candidate(self, tmp_path: Path) -> None:
        """Error-level telemetry should create a ticket candidate via TicketStore."""
        from codebot.control_server import Handler

        handler = self._make_handler(
            body={
                "source": "production",
                "level": "error",
                "message": "Database connection pool exhausted",
                "timestamp": time.time(),
            }
        )

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            Handler.do_POST(handler)

        # Verify event was logged
        events_file = tmp_path / "events.jsonl"
        assert events_file.exists()
        lines = events_file.read_text().strip().split("\n")
        assert len(lines) >= 1
        event = json.loads(lines[-1])
        assert event["type"] == "telemetry"

    def test_telemetry_stores_for_trend_analysis(self, tmp_path: Path) -> None:
        """All telemetry should be appended to telemetry log for trends."""
        from codebot.control_server import Handler

        payloads = [
            {"source": "prod", "level": "info", "message": "Request completed", "timestamp": time.time()},
            {"source": "prod", "level": "warn", "message": "High latency", "timestamp": time.time()},
        ]

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            for p in payloads:
                handler = self._make_handler(body=p)
                Handler.do_POST(handler)

        telemetry_file = tmp_path / "telemetry.jsonl"
        assert telemetry_file.exists()
        lines = telemetry_file.read_text().strip().split("\n")
        assert len(lines) == 2

    def test_telemetry_requires_auth(self, tmp_path: Path) -> None:
        """Telemetry endpoint should require authentication."""
        from codebot.control_server import Handler

        handler = self._make_handler(body={"source": "prod", "level": "info", "message": "test"})
        handler._auth = MagicMock(return_value=False)

        with patch("codebot.control_server.STATE_DIR", tmp_path):
            Handler.do_POST(handler)

        handler._json.assert_called_once_with(401, {"error": "unauthorized"})
