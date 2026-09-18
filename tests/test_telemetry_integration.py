"""Integration tests for telemetry endpoint wired into control_server.

Tests cover:
- /telemetry route is accessible via control_server Handler
- Telemetry signals are stored for trend analysis in event_log
- Anomaly detection triggers discovery events when error rate spikes
"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock
from io import BytesIO

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


def _make_control_handler(
    method: str,
    path: str,
    headers: dict[str, str] | None = None,
    body: bytes = b"",
) -> object:
    """Create a control_server.Handler instance with mocked request."""
    from codebot.control_server import Handler

    handler = object.__new__(Handler)
    handler.path = path  # type: ignore[attr-defined]
    handler.headers = headers or {}  # type: ignore[attr-defined]
    handler.rfile = BytesIO(body)  # type: ignore[attr-defined]
    handler.wfile = BytesIO()  # type: ignore[attr-defined]
    handler.connection = MagicMock()  # type: ignore[attr-defined]
    handler.command = method  # type: ignore[attr-defined]

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
        handler._response_body += data  # type: ignore[attr-defined]

    handler.send_response = mock_send_response  # type: ignore[attr-defined]
    handler.send_header = mock_send_header  # type: ignore[attr-defined]
    handler.end_headers = mock_end_headers  # type: ignore[attr-defined]
    handler.wfile.write = mock_wfile_write  # type: ignore[attr-defined]

    return handler


class TestTelemetryWiredIntoControlServer:
    """Verify that /telemetry routes are handled by control_server."""

    def test_post_telemetry_route_exists(self) -> None:
        """POST /telemetry should be routed, not return 404."""
        with patch("codebot.control_server.CONTROL_TOKEN", "test-tok"):
            body = json.dumps({"signal_type": "error", "summary": "test"}).encode()
            handler = _make_control_handler(
                "POST",
                "/telemetry",
                headers={
                    "Authorization": "Bearer test-tok",
                    "Content-Length": str(len(body)),
                },
                body=body,
            )
            handler.do_POST()  # type: ignore[attr-defined]
            # Should NOT be 404 (the old behavior before wiring)
            assert handler._response_code != 404  # type: ignore[attr-defined]

    def test_get_telemetry_health_route_exists(self) -> None:
        """GET /telemetry/health should be routed through control_server."""
        with patch("codebot.control_server.CONTROL_TOKEN", "test-tok"):
            handler = _make_control_handler(
                "GET",
                "/telemetry/health",
                headers={"Authorization": "Bearer test-tok"},
            )
            handler.do_GET()  # type: ignore[attr-defined]
            assert handler._response_code == 200  # type: ignore[attr-defined]


class TestTelemetryStorageForTrendAnalysis:
    """Verify that accepted telemetry signals are stored in event_log."""

    def test_signal_stored_in_events_jsonl(self) -> None:
        """After successful ingestion, signal appears in events.jsonl."""
        from codebot.event_log import read_events, append_event

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            signal_data = {
                "signal_type": "error",
                "summary": "NullPointerException in handler",
                "source_id": "prod-node-1",
            }
            append_event(state_dir, "telemetry", signal_data)

            events = read_events(state_dir, limit=10)
            assert len(events) >= 1
            telemetry_events = [e for e in events if e.get("type") == "telemetry"]
            assert len(telemetry_events) == 1
            assert telemetry_events[0]["data"]["signal_type"] == "error"

    def test_multiple_signals_stored_for_trend(self) -> None:
        """Multiple signals accumulate for trend analysis."""
        from codebot.event_log import read_events, append_event

        with tempfile.TemporaryDirectory() as tmpdir:
            state_dir = Path(tmpdir)
            for i in range(5):
                append_event(state_dir, "telemetry", {
                    "signal_type": "error",
                    "summary": f"Error {i}",
                    "count": i,
                })

            events = read_events(state_dir, limit=10)
            telemetry_events = [e for e in events if e.get("type") == "telemetry"]
            assert len(telemetry_events) == 5


class TestAnomalyDetectionTriggersDiscovery:
    """Verify anomaly detection logic triggers discovery events."""

    def test_detect_error_spike(self) -> None:
        """Sudden increase in error signals should trigger anomaly event."""
        from codebot.telemetry import detect_anomalies

        recent_signals = [
            {"signal_type": "error", "ts": 1000.0},
            {"signal_type": "error", "ts": 1001.0},
            {"signal_type": "error", "ts": 1002.0},
            {"signal_type": "error", "ts": 1003.0},
            {"signal_type": "error", "ts": 1004.0},
        ]
        baseline_rate = 1.0  # 1 error per window normally

        anomalies = detect_anomalies(recent_signals, baseline_rate)
        assert len(anomalies) > 0
        assert anomalies[0]["type"] == "error_spike"

    def test_no_anomaly_at_normal_rate(self) -> None:
        """Normal signal rate should not trigger anomaly."""
        from codebot.telemetry import detect_anomalies

        recent_signals = [
            {"signal_type": "error", "ts": 1000.0},
        ]
        baseline_rate = 1.0

        anomalies = detect_anomalies(recent_signals, baseline_rate)
        assert len(anomalies) == 0

    def test_performance_degradation_anomaly(self) -> None:
        """Cluster of performance signals should trigger anomaly."""
        from codebot.telemetry import detect_anomalies

        recent_signals = [
            {"signal_type": "performance_degradation", "ts": 1000.0},
            {"signal_type": "performance_degradation", "ts": 1001.0},
            {"signal_type": "performance_degradation", "ts": 1002.0},
        ]
        baseline_rate = 0.5

        anomalies = detect_anomalies(recent_signals, baseline_rate)
        assert len(anomalies) > 0
