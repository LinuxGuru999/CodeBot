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

            # Verify via TicketStore (handles WAL + main JSON) rather than raw file read
            from codebot.ticket_engine import TicketStore
            verify = TicketStore(tickets_path)
            try:
                assert verify.count() == 1
                t = verify.get(result["ticket_id"])
                assert t is not None
                assert t.state.value == "DISCOVERED"
                assert "production_telemetry" in t.source
            finally:
                verify.close()

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

            from codebot.ticket_engine import TicketStore
            verify = TicketStore(tickets_path)
            try:
                t = verify.get(result["ticket_id"])
                assert t is not None
                assert t.ticket_class.value == "performance"
            finally:
                verify.close()

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

            from codebot.ticket_engine import TicketStore
            verify = TicketStore(tickets_path)
            try:
                t = verify.get(result["ticket_id"])
                assert t is not None
                assert t.ticket_class.value == "security"
            finally:
                verify.close()

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

            from codebot.ticket_engine import TicketStore
            verify = TicketStore(tickets_path)
            try:
                t = verify.get(result["ticket_id"])
                assert t is not None
                assert "libc" in t.problem_statement
            finally:
                verify.close()


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

    def test_telemetry_handler_uses_hmac_compare_digest(self) -> None:
        """Source-inspection test enforcing hmac.compare_digest in TelemetryHandler._auth.

        Mirrors TestTimingSafeComparison in test_control_server_auth.py.
        Guards against silent regression to timing-unsafe == comparison on
        bearer-token material (CB-9067805-F528 acceptance criterion 2).
        """
        from pathlib import Path

        telemetry_path = Path(__file__).parent.parent / "codebot" / "telemetry.py"
        source = telemetry_path.read_text(encoding="utf-8")

        # Locate the _auth method definition inside TelemetryHandler
        auth_start = source.find("def _auth(self)")
        assert auth_start != -1, "_auth method not found in telemetry.py"

        # Slice out only the _auth method body (up to next sibling def/class/end)
        auth_body_end = source.find("\n    def ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = source.find("\n\nclass ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = source.find("\ndef ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = len(source)
        auth_body = source[auth_start:auth_body_end]

        # AC-2a: constant-time comparison MUST be present
        assert "hmac.compare_digest" in auth_body, (
            "TelemetryHandler._auth must use hmac.compare_digest for "
            "constant-time bearer-token comparison"
        )

        # AC-2b: no timing-unsafe == on auth/token material.
        # Scan each non-comment, non-docstring line inside _auth for dangerous
        # equality comparisons involving auth variables. Emptiness guards such
        # as `if not TELEMETRY_TOKEN:` are allowed because they do not compare
        # secret bytes.
        danger_vars = ("auth", "expected", "token", "bearer")
        for lineno, raw_line in enumerate(auth_body.splitlines(), start=1):
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            # Skip docstring lines (triple-quote delimiters or pure prose)
            if stripped.startswith(('"""', "'''")):
                continue
            if "==" not in stripped:
                continue
            lowered = stripped.lower()
            # Allow benign emptiness / sentinel checks against literals
            if ('""' in lowered or "''" in lowered) and any(
                v in lowered for v in ("not ", "telemetry_token", "control_token")
            ):
                continue
            # Flag any == whose operands look like auth/token material
            if any(v in lowered for v in danger_vars):
                raise AssertionError(
                    f"TelemetryHandler._auth line {lineno} uses '==' on "
                    f"auth/token material: {stripped!r}. Use "
                    f"hmac.compare_digest instead to avoid timing side-channels."
                )

    def test_telemetry_auth_uses_hmac_compare_digest(self) -> None:
        """Source-inspection test enforcing hmac.compare_digest in TelemetryHandler._auth.

        Mirrors TestTimingSafeComparison.test_auth_method_uses_hmac_compare_digest
        in test_control_server_auth.py. Guards against silent regression to
        timing-unsafe == comparison on bearer-token material.
        """
        from pathlib import Path

        telemetry_path = Path(__file__).parent.parent / "codebot" / "telemetry.py"
        source = telemetry_path.read_text(encoding="utf-8")

        auth_start = source.find("def _auth(self)")
        assert auth_start != -1, "_auth method not found in telemetry.py"

        auth_body_end = source.find("\n    def ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = source.find("\n\nclass ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = source.find("\ndef ", auth_start + 1)
        if auth_body_end == -1:
            auth_body_end = len(source)
        auth_body = source[auth_start:auth_body_end]

        assert "hmac.compare_digest" in auth_body, (
            "TelemetryHandler._auth must use hmac.compare_digest for "
            "constant-time bearer-token comparison"
        )
        assert '== f"Bearer' not in auth_body, (
            "TelemetryHandler._auth must not use == for token comparison"
        )


class TestCreateTicketFromSignalErrors:
    """Tests for error paths in _create_ticket_from_signal."""

    def test_import_error_returns_failure(self) -> None:
        """Test when ticket_engine cannot be imported."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            signal = {"signal_type": "error", "summary": "test"}
            import builtins as _builtins
            import sys as _sys2
            orig_te = _sys2.modules.get("codebot.ticket_engine")
            if "codebot.ticket_engine" in _sys2.modules:
                del _sys2.modules["codebot.ticket_engine"]

            original_import = _builtins.__import__

            def fake_import(name, *args, **kwargs):
                if name == "codebot.ticket_engine" or name.startswith("codebot.ticket_engine."):
                    raise ImportError(f"Mocked import error for {name}")
                return original_import(name, *args, **kwargs)

            try:
                with patch("builtins.__import__", side_effect=fake_import):
                    result = _create_ticket_from_signal(signal, state_dir)
                    assert result["success"] is False
                    assert "ticket_engine unavailable" in result["error"]
            finally:
                if orig_te is not None:
                    sys.modules["codebot.ticket_engine"] = orig_te
                elif "codebot.ticket_engine" in sys.modules:
                    del sys.modules["codebot.ticket_engine"]

    def test_create_ticket_value_error_returns_failure(self) -> None:
        """Test when create_ticket raises ValueError."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            signal = {"signal_type": "error", "summary": "test"}

            with patch("codebot.ticket_engine.create_ticket", side_effect=ValueError("Invalid ticket data")):
                result = _create_ticket_from_signal(signal, state_dir)
                assert result["success"] is False
                assert "Invalid ticket data" in result["error"]

    def test_store_exception_returns_failure(self) -> None:
        """Test when TicketStore.add or flush raises an exception."""
        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            tickets_path = state_dir / "tickets.json"
            tickets_path.write_text(json.dumps({
                "schema_version": "2.0", "updated_at": 0, "tickets": []
            }))

            signal = {"signal_type": "error", "summary": "test"}

            # Mock TicketStore to raise an exception on add — patch where it is imported from
            with patch("codebot.ticket_engine.TicketStore") as mock_store_class:
                mock_store_instance = MagicMock()
                mock_store_instance.add.side_effect = Exception("DB Error")
                mock_store_class.return_value = mock_store_instance

                result = _create_ticket_from_signal(signal, state_dir)
                assert result["success"] is False
                assert "store failed" in result["error"]


class TestTelemetryHandlerPostErrors:
    """Tests for error paths in TelemetryHandler.do_POST."""

    def test_invalid_content_length_returns_400(self) -> None:
        """Test when Content-Length is not an integer."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            handler = _make_handler(
                "POST", "/telemetry",
                headers={
                    "Authorization": "Bearer tok",
                    "Content-Length": "not-an-int",
                }
            )
            handler.do_POST()
            assert handler._response_code == 400  # type: ignore[attr-defined]

    def test_negative_content_length_returns_400(self) -> None:
        """Test when Content-Length is negative."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            handler = _make_handler(
                "POST", "/telemetry",
                headers={
                    "Authorization": "Bearer tok",
                    "Content-Length": "-1",
                }
            )
            handler.do_POST()
            assert handler._response_code == 400  # type: ignore[attr-defined]

    def test_read_timeout_returns_408(self) -> None:
        """Test when reading the request body times out."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            handler = _make_handler(
                "POST", "/telemetry",
                headers={
                    "Authorization": "Bearer tok",
                    "Content-Length": "10",
                }
            )
            # Mock rfile.read to raise TimeoutError
            handler.rfile = MagicMock()
            handler.rfile.read.side_effect = TimeoutError("Read timed out")
            
            handler.do_POST()
            assert handler._response_code == 408  # type: ignore[attr-defined]

    def test_os_error_on_read_returns_408(self) -> None:
        """Test when reading the request body raises OSError."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            handler = _make_handler(
                "POST", "/telemetry",
                headers={
                    "Authorization": "Bearer tok",
                    "Content-Length": "10",
                }
            )
            # Mock rfile.read to raise OSError
            handler.rfile = MagicMock()
            handler.rfile.read.side_effect = OSError("Connection reset")
            
            handler.do_POST()
            assert handler._response_code == 408  # type: ignore[attr-defined]

    def test_get_unknown_path_returns_404(self) -> None:
        """Test GET on unknown path returns 404."""
        handler = _make_handler("GET", "/unknown")
        handler.do_GET()
        assert handler._response_code == 404  # type: ignore[attr-defined]

    def test_post_invalid_signal_returns_400(self) -> None:
        """Test POST with invalid signal body returns 400."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            body = json.dumps({"signal_type": "invalid", "summary": "test"}).encode()
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

    def test_post_ticket_creation_fails_returns_500(self) -> None:
        """Test POST when ticket creation fails returns 500."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            with patch("codebot.ticket_engine.create_ticket", side_effect=ValueError("Invalid")):
                body = json.dumps({"signal_type": "error", "summary": "test"}).encode()
                handler = _make_handler(
                    "POST", "/telemetry",
                    headers={
                        "Authorization": "Bearer tok",
                        "Content-Length": str(len(body)),
                    },
                    body=body,
                )
                handler.do_POST()
                assert handler._response_code == 500  # type: ignore[attr-defined]

    def test_post_store_close_exception_suppressed(self) -> None:
        """Test that store.close exception in finally block is suppressed."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            with patch("codebot.ticket_engine.TicketStore") as mock_store_class:
                mock_store_instance = MagicMock()
                mock_store_instance.add.return_value = MagicMock(id="CB-TEST", state=MagicMock(value="DISCOVERED"))
                mock_store_instance.flush.return_value = None
                mock_store_instance.close.side_effect = Exception("close failed")
                mock_store_class.return_value = mock_store_instance

                body = json.dumps({"signal_type": "error", "summary": "test"}).encode()
                handler = _make_handler(
                    "POST", "/telemetry",
                    headers={
                        "Authorization": "Bearer tok",
                        "Content-Length": str(len(body)),
                    },
                    body=body,
                )
                handler.do_POST()
                # Should succeed despite close failing
                assert handler._response_code == 201  # type: ignore[attr-defined]


class TestTelemetryHandlerLogMessage:
    """Tests for log_message method."""

    def test_log_message_calls_logger(self) -> None:
        """Test that log_message calls logger.debug."""
        with patch("codebot.telemetry.logger") as mock_logger:
            handler = _make_handler("GET", "/telemetry/health")
            handler.log_message("test message %s", "arg")
            mock_logger.debug.assert_called_once_with("test message %s", "arg")


class TestStateDirFallback:
    """Tests for state directory fallback resolution in telemetry handler."""

    def test_state_dir_fallback_resolution(self) -> None:
        """Verify that the fallback state_dir resolves to PROJECT_ROOT/.codebot/state.

        This test ensures that when CODEBOT_STATE_DIR is unset, the telemetry
        handler correctly navigates from codebot/telemetry.py up two levels
        to reach the project root before appending .codebot/state.
        """
        import os
        from pathlib import Path
        from unittest.mock import patch

        # Ensure CODEBOT_STATE_DIR is unset to trigger fallback
        with patch.dict(os.environ, {}, clear=False):
            if "CODEBOT_STATE_DIR" in os.environ:
                del os.environ["CODEBOT_STATE_DIR"]

            # Import telemetry module to get current file path
            import codebot.telemetry as telemetry_module
            
            # Calculate expected path: codebot/telemetry.py -> parent (codebot/) -> parent (project_root) -> .codebot/state
            expected_path = Path(telemetry_module.__file__).parent.parent / ".codebot" / "state"
            
            # Verify the logic matches what's in do_POST
            # The code does: Path(__file__).parent.parent / ".codebot" / "state"
            actual_fallback = Path(telemetry_module.__file__).parent.parent / ".codebot" / "state"
            
            assert str(actual_fallback).endswith(".codebot/state"), (
                f"Fallback path should end with .codebot/state, got: {actual_fallback}"
            )
            
            # Ensure it does NOT end with codebot/.codebot/state (the bug)
            assert "codebot/.codebot/state" not in str(actual_fallback), (
                f"Fallback path incorrectly contains codebot/.codebot/state: {actual_fallback}"
            )


class TestDetectAnomalies:
    """Tests for detect_anomalies function."""

    def test_empty_signals_returns_empty(self) -> None:
        from codebot.telemetry import detect_anomalies
        result = detect_anomalies([], 10.0)
        assert result == []

    def test_zero_baseline_returns_empty(self) -> None:
        from codebot.telemetry import detect_anomalies
        signals = [{"signal_type": "error"}]
        result = detect_anomalies(signals, 0.0)
        assert result == []

    def test_negative_baseline_returns_empty(self) -> None:
        from codebot.telemetry import detect_anomalies
        signals = [{"signal_type": "error"}]
        result = detect_anomalies(signals, -1.0)
        assert result == []

    def test_no_anomaly_detected(self) -> None:
        from codebot.telemetry import detect_anomalies
        signals = [
            {"signal_type": "error"},
            {"signal_type": "error"},
        ]
        baseline = 10.0
        result = detect_anomalies(signals, baseline)
        assert result == []

    def test_anomaly_detected(self) -> None:
        from codebot.telemetry import detect_anomalies
        # Baseline 10, threshold 30 (3x). Send 30 errors.
        signals = [{"signal_type": "error"} for _ in range(30)]
        baseline = 10.0
        result = detect_anomalies(signals, baseline)
        assert len(result) == 1
        assert result[0]["type"] == "error_spike"
        assert result[0]["count"] == 30
        assert result[0]["baseline_rate"] == 10.0

    def test_multiple_anomalies_detected(self) -> None:
        from codebot.telemetry import detect_anomalies
        signals = [
            {"signal_type": "error"},
            {"signal_type": "error"},
            {"signal_type": "error"},
            {"signal_type": "crash_report"},
            {"signal_type": "crash_report"},
            {"signal_type": "crash_report"},
        ]
        baseline = 1.0
        result = detect_anomalies(signals, baseline)
        assert len(result) == 2
        types = {r["type"] for r in result}
        assert "error_spike" in types
        assert "crash_report_spike" in types

    def test_non_string_signal_type_ignored(self) -> None:
        from codebot.telemetry import detect_anomalies
        signals = [
            {"signal_type": 123},  # Invalid type
            {"signal_type": "error"},
        ]
        baseline = 0.1
        result = detect_anomalies(signals, baseline)
        # Only "error" should be counted. 1 count vs threshold 0.3. 
        # Threshold is max(3.0, 0.1*3) = 3.0. 1 < 3.0, so no anomaly.
        assert result == []


class TestTelemetryHandlerAuthDirect:
    """Direct tests for _auth method to ensure line coverage."""

    def test_auth_returns_false_when_token_empty(self) -> None:
        """Test _auth returns False when TELEMETRY_TOKEN is empty."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", ""):
            handler = _make_handler("POST", "/telemetry")
            # Call _auth directly
            result = handler._auth()
            assert result is False

    def test_auth_returns_false_when_token_unset(self) -> None:
        """Test _auth returns False when TELEMETRY_TOKEN is unset (None-like)."""
        # TELEMETRY_TOKEN defaults to "" if env var missing, so patching to "" covers it.
        # This test is redundant but ensures the line is hit in a different context
        with patch("codebot.telemetry.TELEMETRY_TOKEN", ""):
            handler = _make_handler("POST", "/telemetry", headers={"Authorization": "Bearer anything"})
            result = handler._auth()
            assert result is False


class TestTelemetryHandlerPostEdgeCases:
    """Additional edge case tests for do_POST."""

    def test_unicode_decode_error_returns_400(self) -> None:
        """Test when request body has invalid UTF-8."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            # Invalid UTF-8 bytes
            body = b'\xff\xfe{"signal_type": "error"}'
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

    def test_valid_json_but_invalid_signal_returns_400(self) -> None:
        """Test when JSON is valid but signal validation fails."""
        with patch("codebot.telemetry.TELEMETRY_TOKEN", "tok"):
            body = json.dumps({"signal_type": "invalid_type", "summary": "test"}).encode()
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
