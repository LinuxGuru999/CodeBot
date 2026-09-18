"""Store bounded versioned operational events for the CodeBot control plane.

Purpose
-------
Provides append-only JSONL event records and bounded retrieval for operators.

Why
---
Operational recovery needs an auditable signal without exposing raw prompts,
tool output, or unbounded logs through the control plane.

Invariants
----------
- Every event has a version, type, and structured data payload.
- Appends are atomic O_APPEND writes, safe for concurrent writers.
- Readers return at most the requested bounded tail.
"""

import json
import time
from pathlib import Path


MAX_EVENTS = 100
EVENT_VERSION = 1
MAX_DATA_FIELDS = 32
MAX_STRING_CHARS = 256
MAX_LIST_ITEMS = 20

ALLOWED_EVENT_TYPES = frozenset(
    {
        "readiness",
        "packing",
        "execution",
        "usage",
        "lease",
        "retry",
        "dead-letter",
        "dead-letter-retry",
        "state-corruption",
        "rollback",
        "telemetry",
        "discovery-trigger",
    }
)

# Substrings that must never pass through the control plane. Keys containing
# any of these are dropped so raw prompts, tool output, and secrets cannot
# leak via events. Values with secret prefixes are redacted.
_SENSITIVE_SUBSTRINGS = (
    "token",
    "secret",
    "password",
    "passwd",
    "api_key",
    "apikey",
    "authorization",
    "bearer",
    "private_key",
    "prompt",
    "tool_output",
    "tool-output",
    "tool_result",
    "messages",
    "stdout",
    "stderr",
    "transcript",
)

_SECRET_PREFIXES = ("ghp_", "gho_", "sk-", "Bearer ", "xoxb-")


def _looks_secret(value: str) -> bool:
    return value.startswith(_SECRET_PREFIXES)


def _clean_value(value: object, depth: int = 0) -> object:
    if depth > 2:
        return str(value)[:MAX_STRING_CHARS]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        if _looks_secret(value):
            return "[redacted]"
        return value[:MAX_STRING_CHARS]
    if isinstance(value, dict):
        out: dict[str, object] = {}
        for k, v in list(value.items())[:MAX_DATA_FIELDS]:
            if not isinstance(k, str):
                continue
            if any(s in k.lower() for s in _SENSITIVE_SUBSTRINGS):
                continue
            if isinstance(v, str) and _looks_secret(v):
                out[k] = "[redacted]"
            else:
                out[k] = _clean_value(v, depth + 1)
        return out
    if isinstance(value, (list, tuple)):
        return [_clean_value(v, depth + 1) for v in list(value)[:MAX_LIST_ITEMS]]
    return str(value)[:MAX_STRING_CHARS]


def sanitize_data(data: dict) -> dict:
    """Return a bounded copy of data with sensitive keys dropped.

    Why a denylist here: event callers pass free-form dicts, so the only
    safe default is to strip anything resembling secrets, prompts, or
    tool output before the record reaches disk.
    """
    cleaned = _clean_value(data, 0)
    return cleaned if isinstance(cleaned, dict) else {}


def append_event(state_dir: Path, event_type: str, data: dict) -> None:
    import os
    state_dir.mkdir(parents=True, exist_ok=True)
    event_path = state_dir / "events.jsonl"
    safe_data = sanitize_data(data if isinstance(data, dict) else {})
    record = {
        "version": EVENT_VERSION,
        "type": event_type if event_type in ALLOWED_EVENT_TYPES else "unknown",
        "ts": time.time(),
        "data": safe_data,
    }
    line = json.dumps(record, sort_keys=True) + "\n"
    fd = os.open(str(event_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def read_events(state_dir: Path, *, limit: int = MAX_EVENTS) -> list[dict]:
    try:
        limit_int = int(limit)
    except (TypeError, ValueError):
        limit_int = MAX_EVENTS
    bounded_limit = max(1, min(limit_int, MAX_EVENTS))
    event_path = state_dir / "events.jsonl"
    if not event_path.exists():
        return []
    records: list[dict] = []
    for line in event_path.read_text(encoding="utf-8").splitlines()[-bounded_limit:]:
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(record, dict):
            continue
        if record.get("version") != EVENT_VERSION:
            continue
        if not isinstance(record.get("type"), str):
            continue
        if not isinstance(record.get("data"), dict):
            continue
        records.append(
            {
                "version": EVENT_VERSION,
                "type": record["type"],
                "ts": record.get("ts"),
                "data": sanitize_data(record["data"]),
            }
        )
    return records
