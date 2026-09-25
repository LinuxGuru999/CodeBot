"""Regression tests for CB-0227B security hardening in _reap_expired_claims.

Verifies that claim JSON files exceeding MAX_CLAIM_FILE_SIZE (1KB) are
skipped *before* json.loads() is invoked, so a compromised bot subprocess
cannot exhaust orchestrator memory or smuggle malicious payloads through
an oversized claim file. An oversized file must also be left on disk
(not reaped/deleted), since we never parse it to evaluate its expiry.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot import ticket_dispatcher as td
from codebot.ticket_dispatcher import (
    _reap_expired_claims,
    CLAIM_TTL_SECONDS,
    MAX_CLAIM_FILE_SIZE,
)

BOT = "implementer-test"


def _claims_dir(state_dir: Path) -> Path:
    d = state_dir / "claims"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _valid_claim_payload(now: float | None = None) -> dict:
    """A well-formed, small (<1KB) claim payload."""
    return {"worker": BOT, "at": now if now is not None else time.time()}


def test_max_claim_file_size_is_one_kb() -> None:
    """Guard constant matches the documented 1KB safety limit."""
    assert MAX_CLAIM_FILE_SIZE == 1024


def test_oversized_claim_is_skipped_not_parsed_or_deleted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """AC#1/#2/#3: file >1KB is skipped BEFORE json.loads, warned, and kept."""
    claims = _claims_dir(tmp_path)
    oversized = claims / f"TICKET.{BOT}.json"

    # Build a valid-looking but oversized JSON body (>1KB). Even though the
    # 'at' timestamp is far in the past (would normally be reaped), the size
    # guard must short-circuit before parsing/expiry logic runs.
    padding = "x" * (MAX_CLAIM_FILE_SIZE + 100)
    body = {"worker": BOT, "at": time.time() - (CLAIM_TTL_SECONDS * 10), "blob": padding}
    oversized.write_text(json.dumps(body), encoding="utf-8")

    # Precondition: file genuinely exceeds the limit.
    assert oversized.stat().st_size > MAX_CLAIM_FILE_SIZE

    # Instrument json.loads to prove the size guard fires *before* any parse.
    real_loads = json.loads
    calls: list[str] = []

    def spy_loads(*args, **kwargs):
        calls.append("loads")
        return real_loads(*args, **kwargs)

    with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path), \
         patch("codebot.ticket_dispatcher.json.loads", side_effect=spy_loads), \
         caplog.at_level(logging.WARNING, logger="codebot.ticket_dispatcher"):
        reaped = _reap_expired_claims(BOT)

    # Nothing was reaped — the oversized file was skipped, not deleted.
    assert reaped == 0
    assert oversized.exists(), "Oversized claim file must NOT be unlinked/deleted"

    # Size guard fired before json.loads ever ran on this file.
    assert calls == [], "json.loads must not be called on an oversized claim file"

    # A warning naming the oversized condition was logged.
    msgs = [r.getMessage() for r in caplog.records]
    assert any(
        "oversized" in m.lower() and str(oversized) in m
        for m in msgs
    ), f"Expected oversized-skip warning; got: {msgs}"


def test_small_expired_claim_still_reaped(tmp_path: Path) -> None:
    """Control: a normal-sized expired claim IS reaped (guard doesn't over-block)."""
    claims = _claims_dir(tmp_path)
    small = claims / f"TICKET.{BOT}.json"
    payload = _valid_claim_payload(now=time.time() - (CLAIM_TTL_SECONDS + 60))
    small.write_text(json.dumps(payload), encoding="utf-8")

    assert small.stat().st_size <= MAX_CLAIM_FILE_SIZE

    with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
        reaped = _reap_expired_claims(BOT)

    assert reaped == 1
    assert not small.exists(), "Expired within-limit claim should be unlinked"


def test_small_fresh_claim_not_reaped(tmp_path: Path) -> None:
    """Control: a normal-sized fresh claim is left untouched."""
    claims = _claims_dir(tmp_path)
    fresh = claims / f"TICKET.{BOT}.json"
    payload = _valid_claim_payload(now=time.time())
    fresh.write_text(json.dumps(payload), encoding="utf-8")

    with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
        reaped = _reap_expired_claims(BOT)

    assert reaped == 0
    assert fresh.exists()
