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
from pathlib import Path
from typing import Any

from codebot.locks import LOCK_EX, LOCK_UN, flock

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


@dataclass(frozen=True)
class ClaimRecord:
    ticket_id: str
    agent_id: str
    role: str
    claimed_at: float
    revision_token: str


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


def claim_ticket(
    state_dir: Path | str | None,
    ticket_id: str,
    agent_id: str,
    role: str = "",
    clock: Clock | None = None,
) -> ClaimRecord | None:
    if not ticket_id or not agent_id:
        return None
    if state_dir is None:
        return None
    clk = clock or RealClock()
    sdir = Path(state_dir)
    claims = _claims_dir(sdir)
    claim_file = _claim_file_for(sdir, ticket_id, role)
    lock_file = claims / f"{ticket_id}.claim.lock"
    if role in REVIEWER_ROLES_FOR_CLAIM:
        lock_file = claims / f"{ticket_id}.{role}.claim.lock"
    try:
        lock_file.touch(exist_ok=True)
    except OSError:  # pragma: no cover - filesystem unavailable
        return None
    try:
        with open(lock_file, "a+") as lf:
            flock(lf.fileno(), LOCK_EX)
            try:
                if claim_file.exists():
                    try:
                        if claim_file.stat().st_size > 65536:
                            claim_file.unlink(missing_ok=True)
                            existing = {}
                        else:
                            existing = json.loads(claim_file.read_text(encoding="utf-8"))
                    except (json.JSONDecodeError, OSError, ValueError):
                        existing = {}
                    owner = str(existing.get("agent_id", ""))
                    if owner == agent_id:
                        return ClaimRecord(
                            ticket_id=ticket_id,
                            agent_id=agent_id,
                            role=role or str(existing.get("role", role)),
                            claimed_at=float(existing.get("claimed_at", clk.now())),
                            revision_token=str(existing.get("revision_token", str(uuid.uuid4()))),
                        )
                    return None
                record = ClaimRecord(
                    ticket_id=ticket_id,
                    agent_id=agent_id,
                    role=role,
                    claimed_at=clk.now(),
                    revision_token=str(uuid.uuid4()),
                )
                tmp = claim_file.with_suffix(".tmp")
                tmp.write_text(json.dumps({
                    "ticket_id": record.ticket_id,
                    "agent_id": record.agent_id,
                    "role": record.role,
                    "claimed_at": record.claimed_at,
                    "revision_token": record.revision_token,
                }), encoding="utf-8")
                tmp.replace(claim_file)
                return record
            finally:
                flock(lf.fileno(), LOCK_UN)
    except OSError:  # pragma: no cover - lock open failure
        return None


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
    candidates: list[Path] = []
    for role in REVIEWER_ROLES_FOR_CLAIM:
        candidates.append(claims / f"{ticket_id}.{role}.claim.json")
    candidates.append(claims / f"{ticket_id}.claim.json")
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


class SpawnQueue:
    """Global stagger queue. 0.5-second minimum between spawns. Heartbeat-aware."""

    def __init__(
        self,
        clock: Clock,
        stagger_seconds: float = 1.0,
        heartbeat_dir: Path | str | None = None,
    ) -> None:
        if stagger_seconds <= 0:
            raise ValueError("stagger_seconds must be > 0")
        self._clock = clock
        self._stagger = stagger_seconds
        self._queue: list[ScheduledSpawn] = []
        self._lock = threading.Lock()
        self._latest_heartbeat: float = 0.0
        self._heartbeat_dir: Path | None = Path(heartbeat_dir) if heartbeat_dir else None

    def update_latest_heartbeat(self, ts: float) -> None:
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
        with self._lock:
            now = self._clock.now()
            stagger = self._stagger
            if self._queue:
                last_scheduled = self._queue[-1].scheduled_at
                scheduled_at = max(last_scheduled + stagger, now)
            else:
                hb = self._latest_heartbeat
                if hb <= 0 and self._heartbeat_dir is not None:
                    hb = self._read_latest_heartbeat_from_disk()
                if hb > 0:
                    scheduled_at = max(now, hb + stagger)
                else:
                    scheduled_at = now
            scheduled = ScheduledSpawn(request=request, scheduled_at=scheduled_at)
            self._queue.append(scheduled)
            return scheduled

    def dequeue_ready(self) -> list[SpawnRequest]:
        with self._lock:
            now = self._clock.now()
            ready: list[SpawnRequest] = []
            remaining: list[ScheduledSpawn] = []
            for s in self._queue:
                if s.scheduled_at <= now:
                    ready.append(s.request)
                else:
                    remaining.append(s)
            self._queue = remaining
            return ready

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

    def next_spawn_in(self) -> float | None:
        with self._lock:
            if not self._queue:
                return None
            return max(0.0, self._queue[0].scheduled_at - self._clock.now())


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
        stagger_seconds: float = 1.0,
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
            claim = claim_ticket(sdir, ticket_id, agent_id, role=role, clock=self._clock)
            if claim is None:
                self.concurrency.release(token)
                return DispatchResult(success=False, reason="ALREADY_CLAIMED")
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
        return self.spawn_queue.dequeue_ready()

    def update_heartbeat(self, ts: float) -> None:
        self.spawn_queue.update_latest_heartbeat(ts)
