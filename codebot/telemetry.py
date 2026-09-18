#!/usr/bin/env python3
"""Production telemetry ingestion endpoint for CodeBot.

Purpose
-------
Provides a lightweight HTTP handler that accepts structured telemetry signals
from deployed software and creates ticket candidates in the TicketStore for
human review. This closes the feedback loop between production and the
autonomous improvement pipeline.

Why
---
Currently there is no way for deployed instances to feed errors or performance
degradation back into CodeBot. This module provides a bounded, authenticated,
stdlib-only ingestion path that respects the human approval gate required by
the constitution.

Invariants
----------
- stdlib-only (http.server, json, time)
- All requests authenticated via Bearer token (Constitution §2)
- Request body bounded to MAX_REQUEST_BYTES (Constitution §4: bounded I/O)
- Tickets created in DISCOVERED state requiring human triage (approval gate)
- Input validated at trust boundary (Constitution §2)
- Security headers on all responses (Constitution §2)
"""

from __future__ import annotations

import json
import logging
import os
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Maximum request body size: 64KB bound per Constitution §4 (bounded I/O)
MAX_REQUEST_BYTES = 65_536
REQUEST_TIMEOUT_SECONDS = 10

# Telemetry token from environment; empty means reject all requests
TELEMETRY_TOKEN = os.environ.get("CODEBOT_TELEMETRY_TOKEN", "").strip()

# Allowed signal types for validation
ALLOWED_SIGNAL_TYPES = frozenset({
    "error",
    "exception",
    "performance_degradation",
    "crash_report",
    "security_event",
    "user_feedback",
})

# Map signal severity strings to ticket_engine.Severity values
SEVERITY_MAP = {
    "critical": "critical",
    "high": "high",
    "medium": "medium",
    "low": "low",
}


def _validate_signal(data: dict[str, Any]) -> tuple[bool, str]:
    """Validate incoming telemetry signal structure.

    Returns (is_valid, error_message). Error message is empty when valid.
    """
    if not isinstance(data, dict):
        return False, "signal must be a JSON object"

    signal_type = data.get("signal_type")
    if not isinstance(signal_type, str) or signal_type not in ALLOWED_SIGNAL_TYPES:
        return False, f"signal_type must be one of {sorted(ALLOWED_SIGNAL_TYPES)}"

    summary = data.get("summary")
    if not isinstance(summary, str) or not summary.strip():
        return False, "summary is required and must be non-empty"
    if len(summary) > 500:
        return False, "summary must be 500 characters or fewer"

    severity = data.get("severity", "medium")
    if not isinstance(severity, str) or severity.lower() not in SEVERITY_MAP:
        return False, f"severity must be one of {list(SEVERITY_MAP.keys())}"

    details = data.get("details")
    if details is not None and not isinstance(details, (str, dict)):
        return False, "details must be a string or object if provided"

    source_id = data.get("source_id")
    if source_id is not None:
        if not isinstance(source_id, str) or len(source_id) > 128:
            return False, "source_id must be a string of 128 characters or fewer"

    return True, ""


def _create_ticket_from_signal(data: dict[str, Any], state_dir: Path) -> dict[str, Any]:
    """Create a ticket candidate from a validated telemetry signal.

    Tickets are created in DISCOVERED state, requiring human triage before
    they enter the implementation pipeline. This enforces the human approval
    gate specified in the acceptance criteria.

    Returns dict with 'success', 'ticket_id', and optional 'error'.
    """
    try:
        from codebot.ticket_engine import (
            create_ticket,
            TicketStore,
            TicketClass,
            Severity,
            RiskLevel,
        )
    except ImportError:
        logger.error("ticket_engine not available for telemetry ingestion")
        return {"success": False, "error": "ticket_engine unavailable"}

    signal_type = data["signal_type"]
    summary = data["summary"].strip()
    severity_str = data.get("severity", "medium").lower()
    details = data.get("details", "")
    source_id = data.get("source_id", "unknown")

    # Map signal type to ticket class
    class_map = {
        "error": TicketClass.BUG,
        "exception": TicketClass.BUG,
        "performance_degradation": TicketClass.PERFORMANCE,
        "crash_report": TicketClass.BUG,
        "security_event": TicketClass.SECURITY,
        "user_feedback": TicketClass.FEATURE,
    }
    ticket_class = class_map.get(signal_type, TicketClass.BUG)

    sev_map = {v: k for k, v in SEVERITY_MAP.items()}
    severity_enum = getattr(Severity, severity_str.upper(), Severity.MEDIUM)

    # Build evidence string from signal data
    if isinstance(details, dict):
        details_str = json.dumps(details, ensure_ascii=False)[:2000]
    else:
        details_str = str(details)[:2000] if details else ""

    evidence = f"telemetry:{signal_type}:{source_id}:{summary}"
    problem_statement = f"Production telemetry signal: {summary}"
    if details_str:
        problem_statement += f"\n\nDetails:\n{details_str}"

    desired_state = f"Investigate and resolve production signal: {summary}"
    acceptance_criteria = [
        f"Root cause of '{summary}' identified",
        "Fix implemented and tested",
        "Telemetry signal no longer recurring",
    ]

    # Risk based on severity
    risk_map = {
        "critical": RiskLevel.HIGH,
        "high": RiskLevel.HIGH,
        "medium": RiskLevel.MEDIUM,
        "low": RiskLevel.LOW,
    }
    risk = risk_map.get(severity_str, RiskLevel.MEDIUM)

    try:
        ticket = create_ticket(
            title=f"[telemetry] {summary[:100]}",
            ticket_class=ticket_class,
            severity=severity_enum,
            source="production_telemetry",
            evidence=evidence,
            problem_statement=problem_statement,
            desired_state=desired_state,
            acceptance_criteria=acceptance_criteria,
            risk=risk,
            affected_modules=["codebot/"],
        )
    except ValueError as ve:
        return {"success": False, "error": str(ve)}

    store_path = state_dir / "tickets.json"
    try:
        store = TicketStore(store_path)
        stored = store.add(ticket)
        logger.info(
            "Telemetry ticket created: %s (state=%s, requires human triage)",
            stored.id,
            stored.state.value,
        )
        return {"success": True, "ticket_id": stored.id}
    except ValueError as ve:
        # Duplicate evidence hash — signal already tracked
        return {"success": True, "ticket_id": str(ve), "duplicate": True}
    except Exception as e:
        logger.error("Failed to store telemetry ticket: %s", e)
        return {"success": False, "error": f"store failed: {e}"}


class TelemetryHandler(BaseHTTPRequestHandler):
    """HTTP handler for production telemetry ingestion.

    Mount this handler on an existing server or run standalone.
    Only accepts POST /telemetry with Bearer token authentication.
    """

    def _auth(self) -> bool:
        """Check Bearer token. Rejects all if TELEMETRY_TOKEN is unset."""
        if not TELEMETRY_TOKEN:
            return False
        auth = self.headers.get("Authorization", "")
        return auth.strip() == f"Bearer {TELEMETRY_TOKEN}"

    def _send_security_headers(self) -> None:
        """Apply security headers per Constitution §2."""
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")

    def _json_response(self, code: int, obj: dict[str, Any]) -> None:
        body = json.dumps(obj).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self._send_security_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        """Health check endpoint — no auth required, no data exposed."""
        if self.path in ("/telemetry/health", "/api/telemetry/health"):
            self._json_response(200, {"status": "ok", "time": time.time()})
            return
        self._json_response(404, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        """Accept structured telemetry signals."""
        if self.path not in ("/telemetry", "/api/telemetry"):
            self._json_response(404, {"error": "not found"})
            return

        if not self._auth():
            self._json_response(401, {"error": "unauthorized"})
            return

        # Bounded read per Constitution §4
        raw_length = self.headers.get("Content-Length")
        if not raw_length:
            self._json_response(411, {"error": "Content-Length required"})
            return

        try:
            length = int(raw_length)
        except ValueError:
            self._json_response(400, {"error": "Content-Length must be an integer"})
            return

        if length < 0:
            self._json_response(400, {"error": "Content-Length must not be negative"})
            return

        if length > MAX_REQUEST_BYTES:
            self._json_response(413, {"error": "request body too large"})
            return

        try:
            self.connection.settimeout(REQUEST_TIMEOUT_SECONDS)
            raw = self.rfile.read(length)
        except (OSError, TimeoutError):
            self._json_response(408, {"error": "request read timed out"})
            return

        try:
            data = json.loads(raw.decode("utf-8")) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._json_response(400, {"error": "request body must be valid JSON"})
            return

        # Validate input at trust boundary per Constitution §2
        is_valid, error_msg = _validate_signal(data)
        if not is_valid:
            self._json_response(400, {"error": error_msg})
            return

        # Determine state directory
        state_dir = Path(os.environ.get(
            "CODEBOT_STATE_DIR",
            str(Path(__file__).parent / ".codebot" / "state"),
        ))
        state_dir.mkdir(parents=True, exist_ok=True)

        result = _create_ticket_from_signal(data, state_dir)

        if result.get("success"):
            response = {
                "ok": True,
                "ticket_id": result.get("ticket_id"),
                "message": "signal accepted, ticket candidate created (requires human triage)",
            }
            if result.get("duplicate"):
                response["message"] = "signal already tracked (duplicate)"
            self._json_response(201, response)
        else:
            self._json_response(500, {"error": result.get("error", "internal error")})

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        """Suppress default stderr logging; use module logger instead."""
        logger.debug(format, *args)


def detect_anomalies(
    signals: list[dict[str, Any]], baseline_rate: float
) -> list[dict[str, Any]]:
    """Detect anomalous signal patterns that warrant discovery triggers.

    Compares recent signal counts against a baseline rate per window.
    Returns a list of anomaly descriptors (empty if no anomalies detected).

    Why: production telemetry is only useful if it drives action. Anomaly
    detection converts raw signal volume into actionable discovery events
    so the orchestrator can schedule investigation agents automatically.
    """
    if not signals or baseline_rate <= 0:
        return []

    anomalies: list[dict[str, Any]] = []

    # Group signals by type
    counts_by_type: dict[str, int] = {}
    for sig in signals:
        sig_type = sig.get("signal_type", "unknown")
        if isinstance(sig_type, str):
            counts_by_type[sig_type] = counts_by_type.get(sig_type, 0) + 1

    # Threshold: more than 3x baseline rate constitutes an anomaly spike
    threshold = max(3.0, baseline_rate * 3.0)

    for sig_type, count in counts_by_type.items():
        if count >= threshold:
            anomalies.append({
                "type": f"{sig_type}_spike",
                "signal_type": sig_type,
                "count": count,
                "baseline_rate": baseline_rate,
                "threshold": threshold,
            })

    return anomalies
