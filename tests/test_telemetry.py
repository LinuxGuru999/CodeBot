"""Tests for telemetry.py — validation, ticket creation, and HTTP handler."""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import BytesIO

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.telemetry import (
    _validate_signal,
    _create_ticket_from_signal,
    TelemetryHandler,
    ALLOWED_SIGNAL_TYPES,
    MAX_REQUEST_BYTES,
)


class TestValidateSignal:
    """Tests for _validate_signal input validation at trust boundary."""

    def test_valid_minimal_signal(self) -> None:
        data = {"signal_type": "error", "summary": "Something broke"}
        is_valid, error_msg = _validate_signal(data)
        assert is_valid is True
        assert error_msg == ""

    def test_valid_full_signal(self) -> None:
        data = {
            "signal_type": "performance_degradation",
            "summary": "Latency spike",
            "severity": "high",
            "details": {"p99_ms": 5000},
            "source_id": "prod-node-1",
        }
        is_valid, error_msg = _validate_signal(data)
        assert is_valid is True
        assert error_msg == ""

    def test_all_signal_types_accepted(self) -> None:
        for sig_type in ALLOWED_SIGNAL_TYPES:
            data = {"signal_type": sig_type, "summary": "test"}
            is_valid, _ = _validate_signal(data)
            assert is_valid is True, f"signal_type {sig_type} should be valid"

    def test_not_a_dict_rejected(self) -> None:
        is_valid, error_msg = _validate_signal("not a dict")  # type: ignore[arg-type]
        assert is_valid is False
        assert "JSON object" in error_msg

    def test_missing_signal_type_rejected(self) -> None:
        is_valid, error_msg = _validate_signal({"summary": "test"})
        assert is_valid is False
        assert "signal_type" in error_msg

    def test_invalid_signal_type_rejected(self) -> None:
        is_valid, error_msg = _validate_signal({"signal_type": "unknown_type", "summary": "x"})
        assert is_valid is False
        assert "signal_type must be one of" in error_msg

    def test_missing_summary_rejected(self) -> None:
        is_valid, error_msg = _validate_signal({"signal_type": "error"})
        assert is_valid is False
        assert "summary is required" in error_msg

    def test_empty_summary_rejected(self) -> None:
        is_valid, error_msg = _validate_signal({"signal_type": "error", "summary": "   "})
        assert is_valid is False
        assert "summary is required" in error_msg

    def test_summary_too_long_rejected(self) -> None:
        is_valid, error_msg = _validate_signal({"signal_type": "error", "summary": "a" * 501})
        assert is_valid is False
        assert "500 characters" in error_msg

    def test_summary_at_max_length_accepted(self) -> None:
        is_valid, _ = _validate_signal({"signal_type": "error", "summary": "a" * 500})
        assert is_valid is True

    def test_invalid_severity_rejected(self) -> None:
        is_valid, error_msg = _validate_signal({
            "signal_type": "error", "summary": "x", "severity": "extreme"
        })
        assert is_valid is False
        assert "severity must be one of" in error_msg

    def test_details_as_list_rejected(self) -> None:
        is_valid, error_msg = _validate_signal({
            "signal_type": "error", "summary": "x", "details": [1, 2]  # type: ignore[dict-item]
        })
        assert is_valid is False
        assert "details must be a string or object" in error_msg

    def test_details_as_string_accepted(self) -> None:
        is_valid, _ = _validate_signal({
            "signal_type": "error", "summary": "x", "details": "traceback here"
        })
        assert is_valid is True

    def test_source_id_too_long_rejected(self) -> None:
        is_valid, error_msg = _validate_signal({
            "signal_type": "error", "summary": "x", "source_id": "a" * 129
        })
        assert is_valid is False
        assert "128 characters" in error_msg

    def test_source_id_at_max_length_accepted(self) -> None:
        is_valid, _ = _validate_signal({
            "signal_type": "error", "summary": "x", "source_id": "a" * 128
        })
        assert is_valid is True


class TestCreateTicketFromSignal:
    """Tests for _create_ticket_from_signal with real TicketStore in temp dir."""

    def test_creates_ticket_in_discovered_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            tickets_path = state_dir / "tickets.json"
            tickets_path.write_text(json.dumps({
                "schema_version": "2.0",
                "updated_at": 0,
                "tickets": []
            }))

            signal = {
                "signal_type": "error",
                "summary": "Null pointer in handler",
                "severity": "high",
                "details": "stack trace here",
                "source_id": "prod-1",
            }
            result = _create_ticket_from_signal(signal, state_dir)
            assert result["success"] is True
            assert "ticket_id" in result
            assert result["ticket_id"].startswith("CB-")

            stored = json.loads(tickets_path.read_text())
            assert len(stored["tickets"]) == 1
            assert stored["tickets"][0]["state"] == "DISCOVERED"
            assert "production_telemetry" in stored["tickets"][0]["source"]

    def test_performance_signal_maps_to_performance_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            tickets_path = state_dir / "tickets.json"
            tickets_path.write_text(json.dumps({
                "schema_version": "2.0", "updated_at": 0, "tickets": []
            }))

            signal = {"signal_type": "performance_degradation", "summary": "slow query"}
            result = _create_ticket_from_signal(signal, state_dir)
            assert result["success"] is True

            stored = json.loads(tickets_path.read_text())
            assert stored["tickets"][0]["ticket_class"] == "performance"

    def test_security_event_maps_to_security_class(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            tickets_path = state_dir / "tickets.json"
            tickets_path.write_text(json.dumps({
                "schema_version": "2.0", "updated_at": 0, "tickets": []
            }))

            signal = {"signal_type": "security_event", "summary": "brute force detected"}
            result = _create_ticket_from_signal(signal, state_dir)
            assert result["success"] is True

            stored = json.loads(tickets_path.read_text())
            assert stored["tickets"][0]["ticket_class"] == "security"

    def test_duplicate_signal_returns_duplicate_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            tickets_path = state_dir / "tickets.json"
            tickets_path.write_text(json.dumps({
                "schema_version": "2.0", "updated_at": 0, "tickets": []
            }))

            signal = {"signal_type": "error", "summary": "same error", "source_id": "s1"}
            result1 = _create_ticket_from_signal(signal, state_dir)
            assert result1["success"] is True

            result2 = _create_ticket_from_signal(signal, state_dir)
            assert result2["success"] is True
            assert result2.get("duplicate") is True

    def test_dict_details_serialized(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            tickets_path = state_dir / "tickets.json"
            tickets_path.write_text(json.dumps({
                "schema_version": "2.0", "updated_at": 0, "tickets": []
            }))

            signal = {
                "signal_type": "crash_report",
                "summary": "segfault",
                "details": {"module": "libc", "offset": 4096},
            }
            result = _create_ticket_from_signal(signal, state_dir)
            assert result["success"] is True

            stored = json.loads(tickets_path.read_text())
            assert "libc" in stored["tickets"][0]["problem_statement"]


def _make_handler(
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> TelemetryHandler:
    """Create a handler instance with mocked request for testing.

    We bypass BaseHTTPRequestHandler.__init__ which tries to read from the
    socket immediately, and instead wire up only the attributes our methods use.
    """
    handler = object.__new__(TelemetryHandler)
    handler.path = path  # type: ignore[attr-defined]
    handler.headers = headers or {}  # type: ignore[attr-defined]
    handler.rfile = BytesIO(body)  # type: ignore[attr-defined]
    handler.wfile = BytesIO()  # type: ignore[attr-defined]
    handler.connection = MagicMock()  # type: ignore[attr-defined]

    # Track responses for assertions
    handler._response_code = None  # type: ignore[attr-defined]
    handler._response_headers: list[tuple[str, str]] = []  # type: ignore[attr-defined]
    handler._response_body = b""  # type: ignore[attr-defined]

    def mock_send_response(code: int) -> None:
        handler._response_code = code  # type: ignore[attr-defined]

    def mock_send_header(key: str, value: str) -> None:
        handler._response_headers.append((key, value))  # type: ignore[attr-defined]

    def mock_end_headers() -> None:
        pass

    def mock_wfile_write(data: bytes) -> None:
        handler._response_body = data  # type: ignore[attr-defined]

    handler.send_response = mock_send_response  # type: ignore[attr-defined]
    handler.send_header = mock_send_header  # type: ignore[attr-defined]
    handler.end_headers = mock_end_headers  # type: ignore[attr-defined]
    handler.wfile.write = mock_wfile_write  # type: ignore[attr-defined]

    return handler


class TestTelemetryHandlerAuth:
    """Tests for Bearer token authentication on the HTTP handler."""

    def test_health_endpoint_no_auth_required(self) -> None:
        handler = _make_handler("GET", "/telemetry/health")
        handler.do_GET()
        assert handler._response_code == 200  # type: ignore[attr-defined]

    def test_post_without_token_rejected(self) -> None:
        with patch("codebot.telemetry.TELEMETRY_TOKEN", ""):
            handler = _make_handler("POST", "/telemetry")
            handler.do_POST()
            assert handler._response_code == 401  # type: ignore[attr-defined]

    def test_post_with_wrong_token_rejected(self) -> None:
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "secret-token"):
            handler = _make_handler(
                "POST", "/telemetry",
                headers={"Authorization": "Bearer wrong-token"}
            )
            handler.do_POST()
            assert handler._response_code == 401  # type: ignore[attr-defined]

    def test_post_with_correct_token_passes_auth(self) -> None:
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "secret-token"):
            body = json.dumps({"signal_type": "error", "summary": "test"}).encode()
            handler = _make_handler(
                "POST", "/telemetry",
                headers={
                    "Authorization": "Bearer secret-token",
                    "Content-Length": str(len(body)),
                },
                body=body,
            )
            handler.do_POST()
            # Should NOT be 401 (may be 500 due to no state dir, but auth passed)
            assert handler._response_code != 401  # type: ignore[attr-defined]

    def test_wrong_path_returns_404(self) -> None:
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            handler = _make_handler(
                "POST", "/wrong-path",
                headers={"Authorization": "Bearer tok"}
            )
            handler.do_POST()
            assert handler._response_code == 404  # type: ignore[attr-defined]

    def test_missing_content_length_returns_411(self) -> None:
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            handler = _make_handler(
                "POST", "/telemetry",
                headers={"Authorization": "Bearer tok"}
            )
            handler.do_POST()
            assert handler._response_code == 411  # type: ignore[attr-defined]

    def test_body_too_large_returns_413(self) -> None:
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            handler = _make_handler(
                "POST", "/telemetry",
                headers={
                    "Authorization": "Bearer tok",
                    "Content-Length": str(MAX_REQUEST_BYTES + 1),
                }
            )
            handler.do_POST()
            assert handler._response_code == 413  # type: ignore[attr-defined]

    def test_invalid_json_returns_400(self) -> None:
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            body = b"not json{{"
            handler = _make_handler(
                "POST", "/telemetry",
                headers={
                    "Authorization": "Bearer tok",
                    "Content-Length": str(len(body)),
                },
                body=body,
            )
            handler.do_POST()
            assert handler._response_code == 400  # type: ignore[attr-defined]

    def test_security_headers_present_on_response(self) -> None:
        handler = _make_handler("GET", "/telemetry/health")
        handler.do_GET()
        header_dict = {k: v for k, v in handler._response_headers}  # type: ignore[attr-defined]
        assert header_dict.get("X-Content-Type-Options") == "nosniff"
        assert header_dict.get("X-Frame-Options") == "DENY"
        assert header_dict.get("Referrer-Policy") == "no-referrer"
"}}]}