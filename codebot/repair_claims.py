"""Durable repair attempt claim schema for Step 2 context repair.

Provides non-reentrant transaction API for serialized CAS status transitions
on RepairAttempt claims, crash-safe atomic claim creation, and per-ticket
arbitration locks.

All claim mutations are serialized via a stable sidecar .lock file whose
inode never changes, preventing the flock-on-renamed-file race condition.
"""
from __future__ import annotations

import fcntl
import json
import logging
import os
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Iterator

logger = logging.getLogger("repair_claims")


class RepairAttemptStatus(str, Enum):
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    PATCH_APPLIED = "PATCH_APPLIED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


ACTIVE_REPAIR_STATUSES: frozenset[RepairAttemptStatus] = frozenset({
    RepairAttemptStatus.CLAIMED,
    RepairAttemptStatus.RUNNING,
    RepairAttemptStatus.PATCH_APPLIED,
})

LEGAL_STATUS_TRANSITIONS: dict[RepairAttemptStatus, frozenset[RepairAttemptStatus]] = {
    RepairAttemptStatus.CLAIMED: frozenset({RepairAttemptStatus.RUNNING}),
    RepairAttemptStatus.RUNNING: frozenset({
        RepairAttemptStatus.PATCH_APPLIED,
        RepairAttemptStatus.CLAIMED,
        RepairAttemptStatus.FAILED,
    }),
    RepairAttemptStatus.PATCH_APPLIED: frozenset({
        RepairAttemptStatus.COMPLETED,
        RepairAttemptStatus.FAILED,
    }),
    RepairAttemptStatus.COMPLETED: frozenset(),
    RepairAttemptStatus.FAILED: frozenset(),
}


class ClaimStatusError(Exception):
    """Raised when a CAS status transition is illegal or expected status mismatches."""


@dataclass(frozen=True)
class RepairAttempt:
    ticket_id: str
    source_state: str
    dest_state: str
    gate_attempt_number: int
    repair_execution_id: str
    status: RepairAttemptStatus
    created_at: float
    updated_at: float
    expected_field_values: dict[str, Any]
    expected_transition_version: int
    allowed_fields: tuple[str, ...]
    repair_execution_attempt: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "source_state": self.source_state,
            "dest_state": self.dest_state,
            "gate_attempt_number": self.gate_attempt_number,
            "repair_execution_id": self.repair_execution_id,
            "status": self.status.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "expected_field_values": self.expected_field_values,
            "expected_transition_version": self.expected_transition_version,
            "allowed_fields": list(self.allowed_fields),
            "repair_execution_attempt": self.repair_execution_attempt,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RepairAttempt:
        return cls(
            ticket_id=d["ticket_id"],
            source_state=d["source_state"],
            dest_state=d["dest_state"],
            gate_attempt_number=d["gate_attempt_number"],
            repair_execution_id=d["repair_execution_id"],
            status=RepairAttemptStatus(d["status"]),
            created_at=d["created_at"],
            updated_at=d["updated_at"],
            expected_field_values=d.get("expected_field_values", {}),
            expected_transition_version=d.get("expected_transition_version", 0),
            allowed_fields=tuple(d.get("allowed_fields", [])),
            repair_execution_attempt=d.get("repair_execution_attempt", 1),
        )


def _claim_lock_path(claim_path: Path) -> Path:
    return claim_path.with_suffix(".lock")


def _atomic_write(claim_path: Path, attempt: RepairAttempt) -> None:
    content = json.dumps(attempt.to_dict(), separators=(",", ":"))
    tmp_path = claim_path.with_suffix(".tmp")
    fd = os.open(str(tmp_path), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    try:
        os.write(fd, content.encode("utf-8"))
        os.fsync(fd)
    finally:
        os.close(fd)
    tmp_path.replace(claim_path)
    dir_fd = os.open(str(claim_path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _read_claim_locked(claim_path: Path) -> RepairAttempt | None:
    if not claim_path.exists():
        return None
    try:
        size = claim_path.stat().st_size
        if size == 0:
            corrupt = claim_path.with_suffix(".corrupt")
            claim_path.rename(corrupt)
            logger.warning("quarantined zero-length claim: %s", claim_path)
            return None
        text = claim_path.read_text(encoding="utf-8")
        d = json.loads(text)
        required_keys = {"ticket_id", "source_state", "dest_state", "status"}
        if not required_keys.issubset(d.keys()):
            corrupt = claim_path.with_suffix(".corrupt")
            claim_path.rename(corrupt)
            logger.warning("quarantined incomplete claim: %s", claim_path)
            return None
        return RepairAttempt.from_dict(d)
    except (json.JSONDecodeError, KeyError, ValueError, OSError):
        try:
            corrupt = claim_path.with_suffix(".corrupt")
            claim_path.rename(corrupt)
            logger.warning("quarantined corrupt claim: %s", claim_path)
        except OSError:
            pass
        return None


def _update_claim_status_locked(
    claim_path: Path,
    expected_status: RepairAttemptStatus,
    new_status: RepairAttemptStatus,
    **field_updates: Any,
) -> RepairAttempt:
    current = _read_claim_locked(claim_path)
    if current is None:
        raise ClaimStatusError(f"claim not found or corrupt: {claim_path}")
    if current.status != expected_status:
        raise ClaimStatusError(
            f"CAS mismatch: expected {expected_status.value}, "
            f"got {current.status.value} for {claim_path}"
        )
    legal = LEGAL_STATUS_TRANSITIONS.get(expected_status, frozenset())
    if new_status not in legal:
        raise ClaimStatusError(
            f"illegal transition: {expected_status.value} -> {new_status.value}"
        )
    base = asdict(current)
    base["status"] = new_status.value
    base["updated_at"] = time.time()
    for key, value in field_updates.items():
        if key in base:
            base[key] = value
    updated = RepairAttempt.from_dict(base)
    _atomic_write(claim_path, updated)
    return updated


@contextmanager
def claim_lock(claim_path: Path) -> Iterator[int]:
    lock_path = _claim_lock_path(claim_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield lock_fd
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def claim_repair(
    state_dir: Path,
    ticket_id: str,
    source: str,
    dest: str,
    gate_attempt: int,
    envelope: dict[str, Any],
) -> RepairAttempt | None:
    claim_dir = Path(state_dir) / "repair_attempts"
    claim_dir.mkdir(parents=True, exist_ok=True)
    canonical_name = f"{ticket_id}__{source}__{dest}__{gate_attempt}.json"
    canonical_path = claim_dir / canonical_name

    with claim_lock(canonical_path):
        existing = _read_claim_locked(canonical_path)
        if existing is not None:
            return None

        now = time.time()
        attempt = RepairAttempt(
            ticket_id=ticket_id,
            source_state=source,
            dest_state=dest,
            gate_attempt_number=gate_attempt,
            repair_execution_id=str(uuid.uuid4()),
            status=RepairAttemptStatus.CLAIMED,
            created_at=now,
            updated_at=now,
            expected_field_values=envelope.get("expected_field_values", {}),
            expected_transition_version=envelope.get("expected_transition_version", 0),
            allowed_fields=tuple(envelope.get("allowed_fields", ())),
            repair_execution_attempt=envelope.get("repair_execution_attempt", 1),
        )

        tmp_path = claim_dir / f".tmp_{uuid.uuid4().hex[:12]}.json"
        content = json.dumps(attempt.to_dict(), separators=(",", ":"))
        fd = os.open(str(tmp_path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)

        try:
            os.link(str(tmp_path), str(canonical_path))
        except FileExistsError:
            tmp_path.unlink(missing_ok=True)
            return None

        dir_fd = os.open(str(claim_dir), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

        tmp_path.unlink(missing_ok=True)
        return attempt


def get_active_claim(claim_path: Path) -> RepairAttempt | None:
    with claim_lock(claim_path):
        return _read_claim_locked(claim_path)


def update_claim_status(
    claim_path: Path,
    expected_status: RepairAttemptStatus,
    new_status: RepairAttemptStatus,
    **field_updates: Any,
) -> RepairAttempt:
    with claim_lock(claim_path):
        return _update_claim_status_locked(
            claim_path, expected_status, new_status, **field_updates
        )


def find_stale_claims(
    state_dir: Path,
    max_age_seconds: float = 300.0,
) -> list[tuple[Path, RepairAttempt]]:
    claim_dir = Path(state_dir) / "repair_attempts"
    if not claim_dir.exists():
        return []
    stale: list[tuple[Path, RepairAttempt]] = []
    now = time.time()
    for path in claim_dir.glob("*.json"):
        if path.name.startswith(".tmp_"):
            continue
        with claim_lock(path):
            claim = _read_claim_locked(path)
        if claim is None:
            continue
        if claim.status == RepairAttemptStatus.RUNNING:
            if (now - claim.updated_at) > max_age_seconds:
                stale.append((path, claim))
    return stale


def get_all_claims_for_ticket(
    state_dir: Path,
    ticket_id: str,
) -> list[RepairAttempt]:
    claim_dir = Path(state_dir) / "repair_attempts"
    if not claim_dir.exists():
        return []
    results: list[RepairAttempt] = []
    prefix = f"{ticket_id}__"
    for path in claim_dir.glob(f"{prefix}*.json"):
        if path.name.startswith(".tmp_"):
            continue
        with claim_lock(path):
            claim = _read_claim_locked(path)
        if claim is not None:
            results.append(claim)
    return results


def has_active_claim_for_ticket(state_dir: Path, ticket_id: str) -> bool:
    claims = get_all_claims_for_ticket(state_dir, ticket_id)
    return any(c.status in ACTIVE_REPAIR_STATUSES for c in claims)


def acquire_ticket_lock(state_dir: Path, ticket_id: str) -> int:
    lock_dir = Path(state_dir) / "repair_ticket_locks"
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / f"{ticket_id}.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_WRONLY, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def release_ticket_lock(fd: int) -> None:
    fcntl.flock(fd, fcntl.LOCK_UN)
    os.close(fd)
