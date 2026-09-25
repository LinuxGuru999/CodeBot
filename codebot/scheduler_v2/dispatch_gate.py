"""Atomic dispatch gate: concurrency + claims + stagger as one operation.

Lock ordering (E15):
  1. ConcurrencyController lock (in-memory threading.Lock, held briefly)
  2. Claim file lock (flock on state_dir/claims/{ticket_id}.lock, held briefly)
  3. SpawnQueue lock (in-memory threading.Lock, held briefly)

  Never nest TicketStore save lock inside claim lock. Acquire claim lock
  first, release it, then interact with TicketStore. This prevents deadlock
  between claim consumers and TicketStore's own flock on tickets.json.lock.

MAX_CONCURRENCY is the sole capacity limit (E14). No token budgets are
checked, reserved, or enforced anywhere in this module.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from codebot.locks import LOCK_EX, LOCK_UN, flock
from codebot.ticket_dispatcher import _validate_claim_schema

from .lifecycle import Clock, RealClock


@dataclass(frozen=True)
class SlotToken:
    id: str
    created_at: float


class ConcurrencyController:
    """Atomic global concurrency controller. MAX_CONCURRENCY is the sole limit."""

    def __init__(self, max_slots: int, clock: Clock | None = None) -> None:
        if max_slots < 1:
            raise ValueError("max_slots must be >= 1")
        self.max_slots = max_slots
        self._clock = clock or RealClock()
        self._tokens: dict[str, SlotToken] = {}
        self._lock = threading.Lock()

    def reserve(self) -> SlotToken | None:
        with self._lock:
            if len(self._tokens) >= self.max_slots:
                return None
            token = SlotToken(id=str(uuid.uuid4()), created_at=self._clock.now())
            self._tokens[token.id] = token
            return token

    def release(self, token: SlotToken | str) -> bool:
        tid = token.id if isinstance(token, SlotToken) else str(token)
        with self._lock:
            if tid in self._tokens:
                del self._tokens[tid]
                return True
            return False

    def allows_discovery(self, discovery_max: int, current_discovery_active: int) -> bool:
        """Admission cap for discovery workers.

        Discovery shares the same MAX_CONCURRENCY ledger as ticket work.
        This is a cap, not a reservation: when no discovery is actionable,
        all slots remain available to ticket work. Call this BEFORE reserve().

        Args:
            discovery_max: Maximum concurrent discovery workers allowed (e.g. 3).
            current_discovery_active: Currently active discovery worker count.

        Returns:
            True if a new discovery worker may proceed to reserve().
        """
        return current_discovery_active < discovery_max

    def count_active(self) -> int:
        with self._lock:
            return len(self._tokens)

    @property
    def active_count(self) -> int:
        return self.count_active()

    @property
    def free_slots(self) -> int:
        with self._lock:
            return self.max_slots - len(self._tokens)

    @property
    def queued_count(self) -> int:
        with self._lock:
            return len(self._tokens)

    @property
    def used_count(self) -> int:
        return self.count_active()


class ClaimOutcome(str, Enum):
    CLAIMED = "CLAIMED"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    STORAGE_ERROR = "STORAGE_ERROR"
    LOCK_UNAVAILABLE = "LOCK_UNAVAILABLE"
    INVALID_TICKET = "INVALID_TICKET"


@dataclass(frozen=True)
class ClaimRecord:
    ticket_id: str
    agent_id: str
    role: str
    claimed_at: float
    revision_token: str
    work_item_id: str = ""
    work_item_kind: str = "ticket"
    execution_id: str = ""
    generation: int = 0
    ticket_revision: int | None = None
    implementation_attempt_id: str | None = None
    expires_at: float | None = None

    def __post_init__(self) -> None:
        if not self.work_item_id:
            wid = self.ticket_id
            object.__setattr__(self, "work_item_id", wid)
        if not self.execution_id:
            object.__setattr__(self, "execution_id", self.revision_token)
        if self.generation == 0:
            object.__setattr__(self, "generation", 1)


REVIEWER_ROLES_FOR_CLAIM = frozenset({
    "reviewer", "security_reviewer", "architecture_reviewer",
    "performance_reviewer", "concurrency_reviewer",
    "data_integrity_reviewer", "ux_reviewer",
})


def _claims_dir(state_dir: Path) -> Path:
    d = Path(state_dir) / "claims"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _claim_file_for(state_dir: Path, ticket_id: str, role: str = "") -> Path:
    claims = _claims_dir(state_dir)
    if role in REVIEWER_ROLES_FOR_CLAIM:
        return claims / f"{ticket_id}.{role}.claim.json"
    return claims / f"{ticket_id}.claim.json"


def _canonical_work_item_id(raw: str) -> str:
    if raw.startswith("ticket--") or raw.startswith("discovery--"):
        return raw
    return f"ticket--{raw}"


def _raw_ticket_id(work_item_id: str) -> str:
    if work_item_id.startswith("ticket--"):
        return work_item_id[len("ticket--"):]
    if work_item_id.startswith("discovery--"):
        return work_item_id[len("discovery--"):]
    return work_item_id


def _canonical_claim_file(state_dir: Path, work_item_id: str) -> Path:
    canon = _canonical_work_item_id(work_item_id)
    return _claims_dir(state_dir) / f"{canon}.claim.json"


def _canonical_lock_file(state_dir: Path, work_item_id: str) -> Path:
    canon = _canonical_work_item_id(work_item_id)
    return _claims_dir(state_dir) / f"{canon}.claim.lock"


def _legacy_claim_candidates(state_dir: Path, ticket_id: str) -> list[Path]:
    claims = _claims_dir(state_dir)
    raw = _raw_ticket_id(ticket_id)
    cands: list[Path] = []
    cands.append(claims / f"{raw}.claim.json")
    for role in REVIEWER_ROLES_FOR_CLAIM:
        cands.append(claims / f"{raw}.{role}.claim.json")
    return cands


def claim_ticket(
    state_dir: Path | str | None,
    ticket_id: str,
    agent_id: str,
    role: str = "",
    clock: Clock | None = None,
    *,
    generation: int = 1,
    execution_id: str | None = None,
    ticket_revision: int | None = None,
    implementation_attempt_id: str | None = None,
) -> tuple[ClaimOutcome, ClaimRecord | None]:
    if not ticket_id or not agent_id:
        return ClaimOutcome.INVALID_TICKET, None
    if state_dir is None:
        return ClaimOutcome.INVALID_TICKET, None
    clk = clock or RealClock()
    sdir = Path(state_dir)
    canon_wid = _canonical_work_item_id(ticket_id)
    kind = "discovery" if canon_wid.startswith("discovery--") else "ticket"
    raw = _raw_ticket_id(canon_wid)
    claims = _claims_dir(sdir)
    claim_file = _canonical_claim_file(sdir, canon_wid)
    lock_file = _canonical_lock_file(sdir, canon_wid)
    try:
        lock_file.touch(exist_ok=True)
    except OSError:
        return ClaimOutcome.STORAGE_ERROR, None
    try:
        with open(lock_file, "a+") as lf:
            try:
                flock(lf.fileno(), LOCK_EX)
            except OSError:
                return ClaimOutcome.LOCK_UNAVAILABLE, None
            try:
                if claim_file.exists():
                    try:
                        if claim_file.stat().st_size > 65536:
                            claim_file.unlink(missing_ok=True)
                            existing: dict[str, Any] = {}
                        else:
                            existing = json.loads(claim_file.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError, ValueError):
                        existing = {}
                    # Validate claim schema to prevent type confusion attacks
                    if existing and not _validate_claim_schema(existing, claim_file):
                        existing = {}
                    owner = str(existing.get("agent_id", ""))
                    if owner == agent_id:
                        return ClaimOutcome.CLAIMED, ClaimRecord(
                            ticket_id=raw,
                            agent_id=agent_id,
                            role=role or str(existing.get("role", role)),
                            claimed_at=float(existing.get("claimed_at", clk.now())),
                            revision_token=str(existing.get("revision_token", existing.get("execution_id", str(uuid.uuid4())))),
                            work_item_id=canon_wid,
                            work_item_kind=existing.get("work_item_kind", kind),
                            execution_id=str(existing.get("execution_id", existing.get("revision_token", ""))),
                            generation=int(existing.get("generation", generation)),
                            ticket_revision=existing.get("ticket_revision", ticket_revision),
                            implementation_attempt_id=existing.get("implementation_attempt_id", implementation_attempt_id),
                            expires_at=existing.get("expires_at"),
                        )
                    return ClaimOutcome.ALREADY_CLAIMED, None
                for leg in _legacy_claim_candidates(sdir, ticket_id):
                    if leg.exists():
                        try:
                            if leg.stat().st_size > 65536:
                                existing = {}
                            else:
                                existing = json.loads(leg.read_text(encoding="utf-8"))
                        except (json.JSONDecodeError, OSError, ValueError):
                            existing = {}
                        # Validate claim schema to prevent type confusion attacks
                        if existing and not _validate_claim_schema(existing, leg):
                            existing = {}
                        owner = str(existing.get("agent_id", existing.get("worker", existing.get("bot", ""))))
                        if owner:
                            if owner == agent_id:
                                return ClaimOutcome.CLAIMED, ClaimRecord(
                                    ticket_id=raw,
                                    agent_id=agent_id,
                                    role=role or str(existing.get("role", role)),
                                    claimed_at=float(existing.get("claimed_at", existing.get("at", clk.now()))),
                                    revision_token=str(existing.get("revision_token", existing.get("execution_id", str(uuid.uuid4())))),
                                    work_item_id=canon_wid,
                                    work_item_kind=kind,
                                    execution_id=str(existing.get("execution_id", existing.get("revision_token", ""))),
                                    generation=int(existing.get("generation", generation)),
                                    ticket_revision=existing.get("ticket_revision", ticket_revision),
                                    implementation_attempt_id=existing.get("implementation_attempt_id", implementation_attempt_id),
                                    expires_at=existing.get("expires_at"),
                                )
                            return ClaimOutcome.ALREADY_CLAIMED, None
                        return ClaimOutcome.ALREADY_CLAIMED, None
                exec_id = execution_id or str(uuid.uuid4())
                now = clk.now()
                record = ClaimRecord(
                    ticket_id=raw,
                    agent_id=agent_id,
                    role=role,
                    claimed_at=now,
                    revision_token=exec_id,
                    work_item_id=canon_wid,
                    work_item_kind=kind,
                    execution_id=exec_id,
                    generation=generation,
                    ticket_revision=ticket_revision,
                    implementation_attempt_id=implementation_attempt_id,
                    expires_at=None,
                )
                tmp = claim_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({
                    "work_item_kind": record.work_item_kind,
                    "work_item_id": record.work_item_id,
                    "ticket_id": record.ticket_id,
                    "agent_id": record.agent_id,
                    "role": record.role,
                    "execution_id": record.execution_id,
                    "generation": record.generation,
                    "ticket_revision": record.ticket_revision,
                    "implementation_attempt_id": record.implementation_attempt_id,
                    "claimed_at": record.claimed_at,
                    "expires_at": record.expires_at,
                    "revision_token": record.revision_token,
                }), encoding="utf-8")
                tmp.replace(claim_file)
                return ClaimOutcome.CLAIMED, record
            finally:
                flock(lf.fileno(), LOCK_UN)
    except OSError:
        return ClaimOutcome.STORAGE_ERROR, None


def release_claim(
    state_dir: Path | str | None,
    ticket_id: str,
    agent_id: str,
) -> bool:
    if not ticket_id or not agent_id:
        return False
    if state_dir is None:
        return False
    sdir = Path(state_dir)
    claims = _claims_dir(sdir)
    canon_wid = _canonical_work_item_id(ticket_id)
    canon_file = _canonical_claim_file(sdir, canon_wid)
    canon_lock = _canonical_lock_file(sdir, canon_wid)
    if canon_file.exists():
        try:
            existing = json.loads(canon_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):
            try:
                canon_file.unlink(missing_ok=True)
            except OSError:
                pass
            return True
        owner = str(existing.get("agent_id", ""))
        if owner != agent_id:
            return False
        try:
            canon_lock.touch(exist_ok=True)
        except OSError:
            return False
        try:
            with open(canon_lock, "a+") as lf:
                flock(lf.fileno(), LOCK_EX)
                try:
                    if canon_file.exists():
                        canon_file.unlink(missing_ok=True)
                    return True
                finally:
                    flock(lf.fileno(), LOCK_UN)
        except OSError:
            return False
    candidates: list[Path] = []
    for role in REVIEWER_ROLES_FOR_CLAIM:
        candidates.append(claims / f"{ticket_id}.{role}.claim.json")
    candidates.append(claims / f"{ticket_id}.claim.json")
    raw = _raw_ticket_id(canon_wid)
    if raw != ticket_id:
        candidates.append(claims / f"{raw}.claim.json")
        for role in REVIEWER_ROLES_FOR_CLAIM:
            candidates.append(claims / f"{raw}.{role}.claim.json")
    found = False
    any_exists = False
    for claim_file in candidates:
        if not claim_file.exists():
            continue
        any_exists = True
        try:
            existing = json.loads(claim_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, ValueError):  # pragma: no cover
            try:
                claim_file.unlink(missing_ok=True)
            except OSError:  # pragma: no cover
                pass
            found = True
            continue
        owner = str(existing.get("agent_id", ""))
        if owner != agent_id:
            continue
        lock_file = claims / f"{ticket_id}.claim.lock"
        file_role = str(existing.get("role", ""))
        if file_role in REVIEWER_ROLES_FOR_CLAIM:
            lock_file = claims / f"{ticket_id}.{file_role}.claim.lock"
        try:
            lock_file.touch(exist_ok=True)
        except OSError:  # pragma: no cover
            return False
        try:
            with open(lock_file, "a+") as lf:
                flock(lf.fileno(), LOCK_EX)
                try:
                    if claim_file.exists():
                        claim_file.unlink(missing_ok=True)
                    found = True
                finally:
                    flock(lf.fileno(), LOCK_UN)
        except OSError:  # pragma: no cover
            return False
    if found:
        return True
    if any_exists:
        return False
    for legacy in sdir.joinpath("claims").glob(f"{ticket_id}.*.json"):
        if legacy.name.endswith(".claim.json") or legacy.name.endswith(".lock"):
            continue
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            owner = str(data.get("agent_id", data.get("worker", data.get("bot", ""))))
            if owner == agent_id:
                try:
                    legacy.unlink(missing_ok=True)
                except OSError:  # pragma: no cover
                    pass
                return True
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    for legacy in sdir.joinpath("claims").glob(f"{raw}.*.json"):
        if legacy.name.endswith(".claim.json") or legacy.name.endswith(".lock"):
            continue
        try:
            data = json.loads(legacy.read_text(encoding="utf-8"))
            owner = str(data.get("agent_id", data.get("worker", data.get("bot", ""))))
            if owner == agent_id:
                try:
                    legacy.unlink(missing_ok=True)
                except OSError:  # pragma: no cover
                    pass
                return True
        except (json.JSONDecodeError, OSError, ValueError):
            continue
    return True



@dataclass
class SpawnRequest:
    agent_id: str
    ticket_id: str
    role: str
    model: str
    cmd: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None


@dataclass
class ScheduledSpawn:
    request: SpawnRequest
    scheduled_at: float


SPAWN_STAGGER_SECONDS: float = 5.0


class SpawnQueue:
    """Global stagger queue. Fixed 5.0s FIFO authoritative gate.

    Invariant 5 (ADR-007 §6): ONE global spawn queue. FIFO, one clock, one interval.
    No cursor, no heartbeat sliding, no adaptive staggering.
    Derived gate: next_allowed_at = last_process_start + SPAWN_STAGGER_SECONDS.
    Launch failure consumes head; agent ZOMBIE→DEAD, new execution_id on redispatch.
    last_process_start updated only on successful drain().
    """

    def __init__(
        self,
        clock: Clock,
        stagger_seconds: float = SPAWN_STAGGER_SECONDS,
        heartbeat_dir: Path | str | None = None,
    ) -> None:
        if stagger_seconds <= 0:
            raise ValueError("stagger_seconds must be > 0")
        self._clock = clock
        self._stagger = stagger_seconds
        self._queue: list[ScheduledSpawn] = []
        self._lock = threading.Lock()
        self._last_process_start: dict[str, float] = {}
        self._latest_heartbeat: float = 0.0
        self._heartbeat_dir: Path | None = Path(heartbeat_dir) if heartbeat_dir else None

    @property
    def last_process_start(self) -> float:
        with self._lock:
            vals = list(self._last_process_start.values())
            return min(vals) if vals else float("-inf")

    def record_spawn(self, ts: float | None = None, role: str = "") -> None:
        with self._lock:
            key = role if role else "__global__"
            self._last_process_start[key] = ts if ts is not None else self._clock.now()

    def update_latest_heartbeat(self, ts: float) -> None:
        """Legacy: heartbeat tracking for LEGACY mode dequeue_ready()."""
        with self._lock:
            if ts > self._latest_heartbeat:
                self._latest_heartbeat = ts

    def _read_latest_heartbeat_from_disk(self) -> float:
        if self._heartbeat_dir is None:
            return 0.0
        if not self._heartbeat_dir.exists():
            return 0.0
        latest = 0.0
        try:
            iterator = sorted(self._heartbeat_dir.glob("*.heartbeat"))
        except OSError:  # pragma: no cover
            return 0.0
        for hb_file in iterator:
            try:
                val = float(hb_file.read_text(encoding="utf-8").strip())
                if val > latest:
                    latest = val
            except (OSError, ValueError):
                continue
        return latest

    def enqueue(self, request: SpawnRequest) -> ScheduledSpawn:
        """FIFO enqueue. scheduled_at is diagnostic only, never gates dispatch.

        Authoritative gate is drain()'s check of last_process_start + stagger.
        No heartbeat sliding per ADR-007 §6.
        """
        with self._lock:
            now = self._clock.now()
            scheduled_at = now  # diagnostic only; drain() is the gate
            scheduled = ScheduledSpawn(request=request, scheduled_at=scheduled_at)
            self._queue.append(scheduled)
            return scheduled

    def drain(self, role: str = "") -> SpawnRequest | None:
        with self._lock:
            if not self._queue:
                return None
            now = self._clock.now()
            key = role if role else "__global__"
            lps = self._last_process_start.get(key, float("-inf"))
            if now < lps + self._stagger:
                return None
            for i, scheduled in enumerate(self._queue):
                if not role or scheduled.request.role == role:
                    self._queue.pop(i)
                    self._last_process_start[key] = now
                    return scheduled.request
            return None

    def dequeue_ready(self, role: str = "") -> list[SpawnRequest]:
        with self._lock:
            if not self._queue:
                return []
            now = self._clock.now()
            key = role if role else "__global__"
            lps = self._last_process_start.get(key, float("-inf"))
            if now < lps + self._stagger:
                return []
            for i, scheduled in enumerate(self._queue):
                if not role or scheduled.request.role == role:
                    req = self._queue.pop(i)
                    self._last_process_start[key] = now
                    return [req.request]
            return []

    def peek_next(self) -> ScheduledSpawn | None:
        with self._lock:
            if self._queue:
                return self._queue[0]
            return None

    def cancel(self, agent_id: str) -> bool:
        with self._lock:
            for i, s in enumerate(self._queue):
                if s.request.agent_id == agent_id:
                    del self._queue[i]
                    return True
            return False

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)

    @property
    def queue_depth(self) -> int:
        return len(self)

    def next_spawn_in(self, role: str = "") -> float | None:
        with self._lock:
            if not self._queue:
                return None
            key = role if role else "__global__"
            lps = self._last_process_start.get(key, float("-inf"))
            next_allowed = lps + self._stagger
            return max(0.0, next_allowed - self._clock.now())


@dataclass
class DispatchResult:
    success: bool
    reason: str
    token: SlotToken | None = None
    claim: ClaimRecord | None = None
    scheduled_at: float | None = None
    error: str = ""


class DispatchGate:
    """Atomic dispatch gate combining concurrency, claims, and stagger."""

    def __init__(
        self,
        max_slots: int = 40,
        state_dir: Path | str | None = None,
        clock: Clock | None = None,
        heartbeat_dir: Path | str | None = None,
        stagger_seconds: float = SPAWN_STAGGER_SECONDS,
    ) -> None:
        if max_slots < 1:
            raise ValueError("max_slots must be > 1")
        if stagger_seconds <= 0:
            raise ValueError("stagger_seconds must be > 0")
        self._clock = clock or RealClock()
        self._state_dir = Path(state_dir) if state_dir else None
        hb_dir = Path(heartbeat_dir) if heartbeat_dir else self._state_dir
        self.concurrency = ConcurrencyController(max_slots=max_slots, clock=self._clock)
        self.spawn_queue = SpawnQueue(clock=self._clock, stagger_seconds=stagger_seconds, heartbeat_dir=hb_dir)
        self._active: dict[str, tuple[SlotToken, ClaimRecord, ScheduledSpawn]] = {}
        self._gate_lock = threading.Lock()

    def count_active(self) -> int:
        return self.concurrency.count_active()

    def try_dispatch(
        self,
        ticket_id: str,
        agent_id: str,
        role: str = "",
        model: str = "",
        cmd: list[str] | None = None,
        env: dict[str, str] | None = None,
    ) -> DispatchResult:
        if not ticket_id or not agent_id:
            return DispatchResult(success=False, reason="INVALID_ARGS", error="ticket_id and agent_id required")

        token = self.concurrency.reserve()
        if token is None:
            return DispatchResult(success=False, reason="NO_CAPACITY")

        # Claim ticket
        sdir = self._state_dir
        claim: ClaimRecord | None = None
        if sdir is not None:
            outcome, claim = claim_ticket(sdir, ticket_id, agent_id, role=role, clock=self._clock)
            if outcome != ClaimOutcome.CLAIMED or claim is None:
                self.concurrency.release(token)
                reason = outcome.value if isinstance(outcome, ClaimOutcome) else "ALREADY_CLAIMED"
                return DispatchResult(success=False, reason=reason, error=reason)
        else:  # pragma: no cover - only hit when state_dir is None
            claim = ClaimRecord(
                ticket_id=ticket_id, agent_id=agent_id, role=role,
                claimed_at=self._clock.now(), revision_token=str(uuid.uuid4()),
            )

        try:  # pragma: no branch - SpawnQueue.enqueue cannot raise under normal conditions
            request = SpawnRequest(
                agent_id=agent_id, ticket_id=ticket_id, role=role, model=model,
                cmd=cmd or [], env=env,
            )
            scheduled = self.spawn_queue.enqueue(request)
        except Exception as e:  # pragma: no cover - defensive rollback path
            if sdir is not None:
                release_claim(sdir, ticket_id, agent_id)
            self.concurrency.release(token)
            return DispatchResult(success=False, reason="SPAWN_FAILED", error=str(e))

        # Record active dispatch
        with self._gate_lock:
            self._active[agent_id] = (token, claim, scheduled)

        return DispatchResult(
            success=True, reason="DISPATCHED", token=token, claim=claim, scheduled_at=scheduled.scheduled_at,
        )

    def cancel_dispatch(self, agent_id: str, ticket_id: str) -> bool:
        found_queue = self.spawn_queue.cancel(agent_id)
        with self._gate_lock:
            entry = self._active.pop(agent_id, None)
        if entry is not None:
            token, _, _ = entry
            if self._state_dir is not None:
                release_claim(self._state_dir, ticket_id, agent_id)
            self.concurrency.release(token)
            self.spawn_queue.cancel(agent_id)
            return True
        return found_queue

    def complete_dispatch(self, agent_id: str) -> bool:
        with self._gate_lock:
            entry = self._active.pop(agent_id, None)
        if entry is None:
            return False
        token, claim, _ = entry
        if self._state_dir is not None:
            release_claim(self._state_dir, claim.ticket_id, agent_id)
        self.concurrency.release(token)
        self.spawn_queue.cancel(agent_id)
        return True

    def dequeue_ready(self) -> list[SpawnRequest]:
        ready: list[SpawnRequest] = []
        roles_in_queue = set(s.request.role for s in self.spawn_queue._queue)
        if not roles_in_queue:
            return ready
        for role in roles_in_queue:
            reqs = self.spawn_queue.dequeue_ready(role=role)
            ready.extend(reqs)
        return ready

    def update_heartbeat(self, ts: float) -> None:
        self.spawn_queue.update_latest_heartbeat(ts)
