#!/usr/bin/env python3
"""Ticket-scoped review verdict store.

Purpose
-------
Owns the canonical location and parsing rules for structured reviewer
verdicts. Every verdict is scoped to one ticket under
``<state>/reviews/<ticket_id>/<role>.json``. The legacy shared files
(``<state>/correctness_review.json`` and siblings) are read only as a
deprecated fallback: when such a file names the requested ticket, it is
migrated into the ticket-scoped directory on read.

Why
---
Shared per-role verdict files allow only one ticket's verdict per role to
exist at a time. Parallel reviewers overwrite each other, and the gatekeeper
can load another ticket's verdict or silently find ``review_count == 0``
even after reviews completed. Ticket-scoped files remove collisions and let
the platform require the configured reviewer set before completion.

Invariants
----------
- stdlib-only (json, os, time, pathlib)
- Writers use atomic tmp+replace; readers are fail-open
- Malformed verdicts are quarantined, never returned as decisions
- Role names are restricted to ``[A-Za-z0-9_-]+`` to prevent path escape
- Ticket IDs are sanitized for filesystem use (``/`` and ``\\`` become ``_``)
"""

from __future__ import annotations

import ast
import json
import os
import re
import time
from pathlib import Path
from typing import Any

_ROLE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

MAX_VERDICT_BYTES = 64 * 1024


def _sanitize_ticket_id(ticket_id: str) -> str:
    return ticket_id.replace("/", "_").replace("\\", "_")


def _validate_role(role: str) -> str:
    if not isinstance(role, str) or not _ROLE_RE.match(role):
        raise ValueError(f"invalid reviewer role: {role!r}")
    return role


def reviews_dir(state_dir: Path | str) -> Path:
    directory = Path(state_dir) / "reviews"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def ticket_reviews_dir(state_dir: Path | str, ticket_id: str) -> Path:
    directory = reviews_dir(state_dir) / _sanitize_ticket_id(ticket_id)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def attempt_reviews_dir(
    state_dir: Path | str,
    ticket_id: str,
    implementation_attempt_id: int,
) -> Path:
    """Return the attempt-scoped review directory.

    Structure: state/reviews/<ticket_id>/attempt_<N>/
    Each implementation attempt gets its own directory so old approvals
    never leak into new implementations.
    """
    directory = (
        reviews_dir(state_dir)
        / _sanitize_ticket_id(ticket_id)
        / f"attempt_{implementation_attempt_id}"
    )
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def verdict_path(
    state_dir: Path | str,
    ticket_id: str,
    role: str,
    implementation_attempt_id: int = 0,
) -> Path:
    """Return the path for a verdict file.

    When implementation_attempt_id > 0, uses attempt-scoped storage.
    Otherwise falls back to legacy ticket-scoped storage for backward
    compatibility during migration.
    """
    _validate_role(role)
    if implementation_attempt_id > 0:
        return (
            attempt_reviews_dir(state_dir, ticket_id, implementation_attempt_id)
            / f"{role}.json"
        )
    return ticket_reviews_dir(state_dir, ticket_id) / f"{role}.json"


def _read_json_lenient(path: Path) -> dict[str, Any] | None:
    """Parse verdict JSON, tolerating agents that emit Python dict reprs."""
    try:
        if not path.exists():
            return None
        if path.stat().st_size > MAX_VERDICT_BYTES:
            return None
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        try:
            data = ast.literal_eval(raw)
        except (ValueError, SyntaxError, MemoryError):
            return None
    return data if isinstance(data, dict) else None


def _quarantine(path: Path) -> None:
    try:
        quarantine_dir = path.parent / "quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / f"{path.stem}-{int(time.time())}{path.suffix}"
        path.replace(target)
    except OSError:
        pass


def write_verdict(
    state_dir: Path | str,
    ticket_id: str,
    role: str,
    verdict: dict[str, Any],
    implementation_attempt_id: int = 0,
) -> Path:
    """Atomically persist one reviewer verdict.

    When implementation_attempt_id > 0, writes to attempt-scoped directory.
    Otherwise writes to legacy ticket-scoped directory.
    """
    _validate_role(role)
    if not ticket_id or not isinstance(ticket_id, str):
        raise ValueError("ticket_id is required")
    if not isinstance(verdict, dict):
        raise ValueError("verdict must be a mapping")
    payload = dict(verdict)
    payload.setdefault("ticket_id", ticket_id)
    payload.setdefault("reviewer", role)
    payload.setdefault("completed_at", time.time())
    if implementation_attempt_id > 0:
        payload["implementation_attempt_id"] = implementation_attempt_id
    if not payload.get("implementation_revision"):
        payload["is_legacy"] = True
    path = verdict_path(state_dir, ticket_id, role, implementation_attempt_id)
    encoded = json.dumps(payload, indent=2)
    if len(encoded.encode("utf-8")) > MAX_VERDICT_BYTES:
        raise ValueError("verdict exceeds size limit")
    temporary = path.with_suffix(".tmp")
    temporary.write_text(encoded, encoding="utf-8")
    os.replace(temporary, path)
    return path


def load_ticket_verdicts(
    state_dir: Path | str,
    ticket_id: str,
) -> list[dict[str, Any]]:
    """Load all ticket-scoped verdict dicts for one ticket.

    Malformed files are quarantined.
    """
    resolved = Path(state_dir)
    verdicts: list[dict[str, Any]] = []
    directory = resolved / "reviews" / _sanitize_ticket_id(ticket_id)
    if directory.exists():
        for path in sorted(directory.glob("*.json")):
            if path.parent.name == "quarantine":
                continue
            data = _read_json_lenient(path)
            if data is None:
                _quarantine(path)
                continue
            if data.get("ticket_id", ticket_id) != ticket_id:
                _quarantine(path)
                continue
            verdicts.append(data)
    return verdicts


def load_attempt_verdicts(
    state_dir: Path | str,
    ticket_id: str,
    implementation_attempt_id: int,
) -> list[dict[str, Any]]:
    """Load verdicts from a specific implementation attempt directory.

    Only reads from attempt-scoped storage. Does NOT fall back to legacy
    shared files or ticket-level files. Verdicts without implementation_revision
    are marked is_legacy=True and are non-authoritative.
    """
    resolved = Path(state_dir)
    verdicts: list[dict[str, Any]] = []
    directory = (
        resolved
        / "reviews"
        / _sanitize_ticket_id(ticket_id)
        / f"attempt_{implementation_attempt_id}"
    )
    if not directory.exists():
        return verdicts
    for path in sorted(directory.glob("*.json")):
        if path.parent.name == "quarantine":
            continue
        data = _read_json_lenient(path)
        if data is None:
            _quarantine(path)
            continue
        if data.get("ticket_id", ticket_id) != ticket_id:
            _quarantine(path)
            continue
        # Mark unversioned reviews as legacy (non-authoritative)
        if not data.get("implementation_revision"):
            data["is_legacy"] = True
        verdicts.append(data)
    return verdicts


def get_current_attempt_verdicts(
    state_dir: Path | str,
    ticket_id: str,
    implementation_attempt_id: int,
    implementation_revision: str,
) -> list[dict[str, Any]]:
    """Load verdicts that match the current implementation artifact.

    Filters out stale and legacy verdicts. Only returns verdicts where
    both implementation_attempt_id and implementation_revision match.
    """
    all_verdicts = load_attempt_verdicts(state_dir, ticket_id, implementation_attempt_id)
    current = []
    for v in all_verdicts:
        if v.get("is_legacy"):
            continue
        if v.get("implementation_attempt_id") != implementation_attempt_id:
            continue
        if v.get("implementation_revision") != implementation_revision:
            continue
        current.append(v)
    return current


def reviewer_names_for_ticket(state_dir: Path | str, ticket_id: str) -> list[str]:
    """Return sorted reviewer role names with verdicts for one ticket."""
    names = {
        str(v.get("reviewer", ""))
        for v in load_ticket_verdicts(state_dir, ticket_id)
        if v.get("reviewer")
    }
    return sorted(names)


def has_required_reviewers(
    state_dir: Path | str,
    ticket_id: str,
    required_roles: list[str] | tuple[str, ...],
) -> bool:
    return False
