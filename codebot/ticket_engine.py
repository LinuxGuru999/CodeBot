#!/usr/bin/env python3
"""CodeBot Ticket Engine — normalized schema and state machine.

Purpose
-------
Defines the v2 ticket schema per CODEBOT-ROADMAP.md §4 and the formal
state machine governing ticket lifecycle. All CodeBot work flows through
normalized tickets; no agent may discover an issue and immediately modify
code without an authorized work item in READY or later state.

Why
---
The previous QUEUE.md format was Monitor-specific markdown with no enforced
schema, no state transitions, and no deduplication guarantees. This module
provides a stdlib-only, JSON-serializable ticket representation with
validated state transitions that any project adapter can consume.

Invariants
----------
- stdlib-only (json, enum, dataclasses, hashlib, time, re)
- Tickets are immutable once created; state changes produce new snapshots
- State transitions are validated; invalid transitions raise ValueError
- Evidence hash is SHA-256 of canonical evidence string for deduplication
- All timestamps are Unix epoch floats
- Schema version is embedded for forward compatibility
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any

from codebot.file_lock import flock, LOCK_SH, LOCK_EX, LOCK_UN


SCHEMA_VERSION = "2.0"


class TicketState(str, Enum):
    DISCOVERED = "DISCOVERED"
    VALIDATING = "VALIDATING"
    TRIAGED = "TRIAGED"
    READY = "READY"
    DECOMPOSE = "DECOMPOSE"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    REVIEWING = "REVIEWING"
    VERIFYING = "VERIFYING"
    COMPLETE = "COMPLETE"
    BLOCKED = "BLOCKED"
    REWORK = "REWORK"
    REJECTED = "REJECTED"
    DUPLICATE = "DUPLICATE"
    DEFERRED = "DEFERRED"


class TicketClass(str, Enum):
    BUG = "bug"
    FEATURE = "feature"
    SECURITY = "security"
    PERFORMANCE = "performance"
    DOCUMENTATION = "documentation"
    TEST = "test"
    REFACTOR = "refactor"
    DEPENDENCY = "dependency"
    ARCHITECTURE = "architecture"
    INFRASTRUCTURE = "infrastructure"


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class RiskLevel(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


# Minimum risk level that requires an implementation plan before IMPLEMENTING.
# Configurable: raise to HIGH to exempt medium-risk tickets from planning.
MIN_RISK_FOR_PLANNING = RiskLevel.MEDIUM

# Ordered risk levels for threshold comparison
_RISK_ORDER: dict[str, int] = {
    RiskLevel.LOW.value: 0,
    RiskLevel.MEDIUM.value: 1,
    RiskLevel.HIGH.value: 2,
    RiskLevel.CRITICAL.value: 3,
}


# Valid state transitions: from_state -> set of allowed to_states
TRANSITIONS: dict[TicketState, frozenset[TicketState]] = {
    TicketState.DISCOVERED: frozenset({
        TicketState.VALIDATING,
        TicketState.REJECTED,
        TicketState.DUPLICATE,
    }),
    TicketState.VALIDATING: frozenset({
        TicketState.TRIAGED,
        TicketState.REJECTED,
        TicketState.DUPLICATE,
    }),
    TicketState.TRIAGED: frozenset({
        TicketState.READY,
        TicketState.DEFERRED,
        TicketState.REJECTED,
    }),
    TicketState.READY: frozenset({
        TicketState.DECOMPOSE,
        TicketState.PLANNING,
        TicketState.IMPLEMENTING,
        TicketState.DEFERRED,
    }),
    TicketState.DECOMPOSE: frozenset({
        TicketState.PLANNING,
        TicketState.READY,
        TicketState.BLOCKED,
    }),
    TicketState.PLANNING: frozenset({
        TicketState.IMPLEMENTING,
        TicketState.DECOMPOSE,
        TicketState.BLOCKED,
    }),
    TicketState.IMPLEMENTING: frozenset({
        TicketState.REVIEWING,
        TicketState.BLOCKED,
        TicketState.REWORK,
    }),
    TicketState.REVIEWING: frozenset({
        TicketState.VERIFYING,
        TicketState.REWORK,
    }),
    TicketState.VERIFYING: frozenset({
        TicketState.COMPLETE,
        TicketState.REWORK,
    }),
    TicketState.REWORK: frozenset({
        TicketState.IMPLEMENTING,
        TicketState.DECOMPOSE,
        TicketState.PLANNING,
        TicketState.REJECTED,
        TicketState.DEFERRED,
    }),
    TicketState.BLOCKED: frozenset({
        TicketState.READY,
        TicketState.DECOMPOSE,
        TicketState.PLANNING,
        TicketState.IMPLEMENTING,
        TicketState.DEFERRED,
    }),
    TicketState.DEFERRED: frozenset({
        TicketState.READY,
        TicketState.TRIAGED,
        TicketState.DECOMPOSE,
    }),
    # Terminal states
    TicketState.COMPLETE: frozenset(),
    TicketState.REJECTED: frozenset(),
    TicketState.DUPLICATE: frozenset(),
}


@dataclass(frozen=True)
class Ticket:
    id: str
    title: str
    ticket_class: TicketClass
    severity: Severity
    state: TicketState
    source: str
    evidence: str
    problem_statement: str
    desired_state: str
    acceptance_criteria: list[str]
    affected_modules: list[str]
    dependencies: list[str]
    risk: RiskLevel
    blast_radius: str
    security_impact: str
    migration_impact: str
    required_reviewers: list[str]
    required_tests: list[str]
    documentation_requirements: list[str]
    rollback_strategy: str
    estimated_cost_tokens: int
    created_at: float
    updated_at: float
    schema_version: str = SCHEMA_VERSION
    outcome: str = ""
    final_cost_tokens: int = 0
    attempts: int = 0
    rework_count: int = 0
    assigned_agent: str = ""
    assigned_model: str = ""
    reviewer_feedback: list[dict] = field(default_factory=list)
    last_gate_result: dict[str, Any] = field(default_factory=dict)
    gate_history: list[dict[str, Any]] = field(default_factory=list)

    def evidence_hash(self) -> str:
        canonical = f"{self.ticket_class}:{self.problem_statement}:{self.evidence}"
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def transition(self, new_state: TicketState, reviewer_feedback: list[dict] | None = None) -> Ticket:
        allowed = TRANSITIONS.get(self.state, frozenset())
        if new_state not in allowed:
            raise ValueError(
                f"invalid transition {self.state.value} -> {new_state.value} "
                f"(allowed: {sorted(s.value for s in allowed)})"
            )
        updates = {"state": new_state, "updated_at": time.time()}
        if new_state == TicketState.REWORK:
            updates["rework_count"] = self.rework_count + 1
            if reviewer_feedback:
                existing = list(self.reviewer_feedback) if self.reviewer_feedback else []
                existing.extend(reviewer_feedback)
                updates["reviewer_feedback"] = existing
        if new_state == TicketState.IMPLEMENTING:
            updates["attempts"] = self.attempts + 1
        return Ticket(**{**asdict(self), **updates})

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ticket_class"] = self.ticket_class.value
        d["severity"] = self.severity.value
        d["state"] = self.state.value
        d["risk"] = self.risk.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Ticket:
        data = dict(data)
        data["ticket_class"] = TicketClass(data["ticket_class"])
        data["severity"] = Severity(data["severity"])
        data["state"] = TicketState(data["state"])
        data["risk"] = RiskLevel(data["risk"])
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)

    @classmethod
    def from_json(cls, raw: str) -> Ticket:
        return cls.from_dict(json.loads(raw))


def generate_ticket_id(prefix: str = "CB") -> str:
    """Generate a globally unique ticket ID using uuid4.

    Uses uuid4 to guarantee uniqueness across process restarts, module reimports,
    and rapid-fire creation bursts. No timestamp modulo is used to avoid any
    collision window. Format: CB-{12-char-uuid-hex-upper} provides 48 bits of
    cryptographic randomness per ID.
    """
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def create_ticket(
    title: str,
    ticket_class: TicketClass,
    severity: Severity,
    source: str,
    evidence: str,
    problem_statement: str,
    desired_state: str,
    acceptance_criteria: list[str],
    risk: RiskLevel = RiskLevel.MEDIUM,
    affected_modules: list[str] | None = None,
    dependencies: list[str] | None = None,
    blast_radius: str = "",
    security_impact: str = "none",
    migration_impact: str = "none",
    required_reviewers: list[str] | None = None,
    required_tests: list[str] | None = None,
    documentation_requirements: list[str] | None = None,
    rollback_strategy: str = "revert commit",
    estimated_cost_tokens: int = 0,
) -> Ticket:
    if not title or not title.strip():
        raise ValueError("title is required")
    if not acceptance_criteria:
        raise ValueError("at least one acceptance criterion is required")
    if not problem_statement or not problem_statement.strip():
        raise ValueError("problem_statement is required")

    now = time.time()
    return Ticket(
        id=generate_ticket_id(),
        title=title.strip(),
        ticket_class=ticket_class,
        severity=severity,
        state=TicketState.DISCOVERED,
        source=source,
        evidence=evidence,
        problem_statement=problem_statement.strip(),
        desired_state=desired_state.strip(),
        acceptance_criteria=acceptance_criteria,
        affected_modules=affected_modules or [],
        dependencies=dependencies or [],
        risk=risk,
        blast_radius=blast_radius,
        security_impact=security_impact,
        migration_impact=migration_impact,
        required_reviewers=required_reviewers or [],
        required_tests=required_tests or [],
        documentation_requirements=documentation_requirements or [],
        rollback_strategy=rollback_strategy,
        estimated_cost_tokens=estimated_cost_tokens,
        created_at=now,
        updated_at=now,
    )


def _normalize_title_words(title: str) -> frozenset[str]:
    """Return a frozenset of normalized lowercase words from a title.

    Used by TicketStore for O(1)-amortized title-similarity dedup via an
    inverted word index.  Stop-words shorter than 3 characters are excluded
    to improve signal-to-noise ratio.
    """
    return frozenset(w for w in title.lower().split() if len(w) >= 3)


class TicketStore:
    SIMILARITY_THRESHOLD = 0.8  # Jaccard threshold for "similar" titles
    SAVE_DEBOUNCE_SECONDS = 0.5  # Debounce window for batching saves

    def __init__(self, path: Path, *, pretty: bool = False) -> None:
        self._path = path
        self._pretty = pretty
        self._lock = threading.RLock()
        self._tickets: dict[str, Ticket] = {}
        self._evidence_index: dict[str, str] = {}
        # Word inverted index: word -> set of ticket IDs whose title contains it
        self._word_index: dict[str, set[str]] = {}
        # Per-state index: state -> set of ticket IDs for O(k) list_by_state lookups
        self._state_index: dict[TicketState, set[str]] = {}
        # In-memory cache for gate approvals: ticket_id -> bool (latest passed status)
        self._approval_cache: dict[str, bool] = {}
        # Dirty ticket tracking: IDs modified since last save (for incremental saves)
        self._dirty_ids: set[str] = set()
        # Full-save counter: trigger full save every _FULL_SAVE_INTERVAL saves
        self._save_count: int = 0
        self._FULL_SAVE_INTERVAL: int = 50
        # Debounced save mechanism
        self._save_pending = False
        self._save_lock = threading.Lock()
        self._save_timer: threading.Timer | None = None
        self._shutdown = False
        self._load()
        self._save_queue: list[dict] = []
        self._save_condition = threading.Condition(self._save_lock)
        self._save_worker = threading.Thread(target=self._save_worker_loop, daemon=True)
        self._save_worker.start()

    def _save_worker_loop(self) -> None:
        """Background worker that debounces saves.

        Waits for SAVE_DEBOUNCE_SECONDS after the last mutation before
        flushing to disk, coalescing rapid-fire add()/transition() calls
        into a single I/O operation.
        """
        while not self._shutdown:
            with self._save_condition:
                while not self._save_queue and not self._shutdown:
                    self._save_condition.wait(timeout=1.0)
                if self._shutdown:
                    break
                # Record when the first mutation in this batch arrived
                first_ts = self._save_queue[0]["ts"]
                elapsed = time.time() - first_ts
                remaining = self.SAVE_DEBOUNCE_SECONDS - elapsed
                if remaining > 0:
                    self._save_condition.wait(timeout=remaining)
                if self._shutdown:
                    break
                self._save_queue.clear()
            try:
                self._save()
            except Exception:
                pass

    def flush(self) -> None:
        """Force an immediate save of all dirty tickets.

        Blocks until the background worker has persisted pending changes.
        Use this when durability is required (e.g., before shutdown or
        in tests that verify persistence).
        """
        with self._save_condition:
            if self._save_queue:
                self._save_queue.clear()
                self._save_condition.notify()
        self._save()

    def _load(self) -> None:
        if not self._path.exists():
            self._build_approval_cache()
            return
        try:
            lock_path = self._path.with_suffix(".lock")
            lock_path.touch(exist_ok=True)
            with open(lock_path, "a+") as lock_fd:
                flock(lock_fd, LOCK_EX)
                try:
                    data = json.loads(self._path.read_text(encoding="utf-8"))
                finally:
                    flock(lock_fd, LOCK_UN)
            for entry in data.get("tickets", []):
                t = Ticket.from_dict(entry)
                self._tickets[t.id] = t
                self._evidence_index[t.evidence_hash()] = t.id
                self._index_title(t)
                # Maintain per-state index for O(k) list_by_state lookups
                self._state_index.setdefault(t.state, set()).add(t.id)
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            self._tickets = {}
            self._evidence_index = {}
            self._word_index = {}
            self._state_index = {}
        self._build_approval_cache()

    def _index_title(self, ticket: Ticket) -> None:
        """Add ticket title words to the inverted index."""
        for word in _normalize_title_words(ticket.title):
            self._word_index.setdefault(word, set()).add(ticket.id)

    def _unindex_title(self, ticket: Ticket) -> None:
        """Remove ticket title words from the inverted index."""
        for word in _normalize_title_words(ticket.title):
            bucket = self._word_index.get(word)
            if bucket:
                bucket.discard(ticket.id)
                if not bucket:
                    del self._word_index[word]

    def _jaccard_similarity(self, s1: frozenset[str], s2: frozenset[str]) -> float:
        """Compute Jaccard index between two word sets.  O(min(|s1|, |s2|))."""
        if not s1 and not s2:
            return 1.0
        intersection = len(s1 & s2)
        union = len(s1 | s2)
        return intersection / union if union else 0.0

    def _backup(self) -> None:
        backup_dir = self._path.parent / "ticket_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"tickets-{ts}.json"
        try:
            backup_path.write_text(self._path.read_text(encoding="utf-8"), encoding="utf-8")
            self._prune_backups(backup_dir)
        except OSError:
            pass

    def _prune_backups(self, backup_dir: Path, keep: int = 20) -> None:
        backups = sorted(backup_dir.glob("tickets-*.json"), key=lambda p: p.stat().st_mtime)
        for old in backups[:-keep]:
            try:
                old.unlink()
            except OSError:
                pass

    def restore_latest_backup(self) -> bool:
        backup_dir = self._path.parent / "ticket_backups"
        if not backup_dir.exists():
            return False
        backups = sorted(backup_dir.glob("tickets-*.json"), key=lambda p: p.stat().st_mtime)
        if not backups:
            return False
        latest = backups[-1]
        try:
            data = json.loads(latest.read_text(encoding="utf-8"))
            if "tickets" not in data:
                return False
            self._tickets = {}
            self._evidence_index = {}
            self._word_index = {}
            self._state_index = {}
            for entry in data["tickets"]:
                t = Ticket.from_dict(entry)
                self._tickets[t.id] = t
                self._evidence_index[t.evidence_hash()] = t.id
                self._index_title(t)
                self._state_index.setdefault(t.state, set()).add(t.id)
            self._dirty_ids.clear()
            self._save()
            return True
        except Exception:
            return False

    def _queue_save(self) -> None:
        """Debounce saves so rapid mutations trigger only one I/O operation.

        Appends a signal to the save queue and notifies the background worker.
        The actual serialization happens asynchronously, ensuring that
        add()/transition() return in O(1) time without blocking on disk I/O.
        For immediate durability needs, call flush() explicitly.
        """
        with self._save_condition:
            self._save_queue.append({"ts": time.time()})
            self._save_condition.notify()

    def _save(self) -> None:
        """Atomically save ticket store, preferring incremental saves.

        When a small subset of tickets has changed (dirty tracking), loads the
        existing file and patches only those entries — avoiding O(N) full-store
        serialization. Falls back to full serialization periodically or when
        the dirty set is large relative to the total store size.

        Retries lock acquisition up to 3 times with exponential backoff.
        """
        # Snapshot dirty IDs and clear them atomically
        with self._lock:
            dirty_ids = self._dirty_ids.copy()
            self._dirty_ids.clear()

        # Decide: incremental vs full save
        total = len(self._tickets)
        self._save_count += 1
        use_incremental = (
            dirty_ids
            and self._path.exists()
            and len(dirty_ids) < max(total * 0.3, 50)
            and self._save_count % self._FULL_SAVE_INTERVAL != 0
        )

        if use_incremental:
            payload = self._build_incremental_payload(dirty_ids)
        else:
            payload = self._build_full_payload()

        lock_path = self._path.with_suffix(".lock")
        max_retries = 3
        base_delay = 0.1  # seconds

        for attempt in range(max_retries):
            try:
                with open(lock_path, "a+") as lock_fd:
                    flock(lock_fd, LOCK_EX)
                    try:
                        self._backup()
                        tmp = self._path.with_suffix(".tmp")
                        tmp.write_text(
                            json.dumps(payload, indent=2 if self._pretty else None),
                            encoding="utf-8",
                        )
                        tmp.replace(self._path)
                    finally:
                        flock(lock_fd, LOCK_UN)
                return  # Success
            except OSError as e:
                if attempt < max_retries - 1:
                    delay = base_delay * (2 ** attempt)
                    time.sleep(delay)
                else:
                    raise RuntimeError(
                        f"TicketStore._save failed after {max_retries} lock retries: {e}. "
                        f"Data may be at risk if concurrent writes occurred."
                    ) from e

    def _build_full_payload(self) -> dict:
        """Build save payload by serializing all tickets — O(N)."""
        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": time.time(),
            "tickets": [t.to_dict() for t in self._tickets.values()],
        }

    def _build_incremental_payload(self, dirty_ids: set[str]) -> dict:
        """Build save payload by patching only dirty tickets in existing file.

        Loads the existing JSON, updates only the entries for dirty ticket IDs,
        and rebuilds the payload. This avoids calling to_dict() on unchanged
        tickets, reducing CPU cost from O(N) to O(K) for serialization where
        K = len(dirty_ids). The JSON parse/write is still O(N) but avoids the
        expensive dataclass->dict conversion for most tickets.
        """
        try:
            existing_data = json.loads(self._path.read_text(encoding="utf-8"))
            existing_map: dict[str, dict] = {
                t["id"]: t for t in existing_data.get("tickets", [])
            }
        except (json.JSONDecodeError, KeyError, OSError):
            # Corrupted or missing file — fall back to full save
            return self._build_full_payload()

        # Patch only dirty entries
        for tid in dirty_ids:
            ticket = self._tickets.get(tid)
            if ticket is not None:
                existing_map[tid] = ticket.to_dict()

        return {
            "schema_version": SCHEMA_VERSION,
            "updated_at": time.time(),
            "tickets": list(existing_map.values()),
        }

    def add(self, ticket: Ticket) -> Ticket:
        eh = ticket.evidence_hash()
        with self._lock:
            if eh in self._evidence_index:
                existing_id = self._evidence_index[eh]
                existing = self._tickets.get(existing_id)
                if existing and existing.state not in (
                    TicketState.COMPLETE,
                    TicketState.REJECTED,
                ):
                    raise ValueError(
                        f"duplicate ticket: evidence matches {existing_id}"
                    )
            self._tickets[ticket.id] = ticket
            self._evidence_index[eh] = ticket.id
            # Maintain per-state index
            self._state_index.setdefault(ticket.state, set()).add(ticket.id)
            # Track dirty for incremental save
            self._dirty_ids.add(ticket.id)
            self._queue_save()
        return ticket

    def get(self, ticket_id: str) -> Ticket | None:
        """Alias for get_by_id for backward compatibility."""
        return self.get_by_id(ticket_id)

    def get_by_id(self, ticket_id: str) -> Ticket | None:
        """Return the ticket with the given ID, or None if not found."""
        with self._lock:
            return self._tickets.get(ticket_id)

    def _has_plan(self, ticket_id: str) -> bool:
        """Check if an implementation plan exists for the given ticket."""
        try:
            from codebot.implementation_planner import PlanStore
            plans_dir = self._path.parent / "plans"
            if not plans_dir.exists():
                return False
            plan_store = PlanStore(self._path.parent)
            return plan_store.exists(ticket_id)
        except Exception:
            return False

    def _build_approval_cache(self) -> None:
        """Build in-memory cache of latest gate approvals from gate_results.jsonl.

        Scans the file once at startup to populate _approval_cache.
        Subsequent lookups use the cache for O(1) access.
        Must be called while holding self._lock or during __init__ before
        other threads can access the store.
        """
        gate_path = self._path.parent / "gate_results.jsonl"
        self._approval_cache.clear()
        if not gate_path.exists():
            return
        try:
            with open(gate_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        tid = record.get("ticket_id")
                        if tid:
                            self._approval_cache[tid] = record.get("passed") is True
                    except json.JSONDecodeError:
                        continue
        except OSError:
            pass

    def record_gate_result(self, ticket_id: str, passed: bool, **extra: Any) -> None:
        """Record a gate result and update the approval cache atomically.

        Appends to gate_results.jsonl under exclusive lock and updates
        _approval_cache so subsequent _has_gate_approval calls are O(1).
        """
        gate_path = self._path.parent / "gate_results.jsonl"
        record: dict[str, Any] = {
            "ticket_id": ticket_id,
            "passed": passed,
            "timestamp": time.time(),
        }
        record.update(extra)
        with self._lock:
            try:
                gate_path.parent.mkdir(parents=True, exist_ok=True)
                with open(gate_path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(record) + "\n")
                self._approval_cache[ticket_id] = passed
            except OSError:
                pass

    def _has_gate_approval(self, ticket_id: str) -> bool:
        """Check if the latest gate result for ticket_id indicates approval.

        Uses the in-memory _approval_cache for O(1) lookup instead of
        scanning gate_results.jsonl on every call. The cache is populated
        at load time via _build_approval_cache and updated on each
        record_gate_result call.
        """
        with self._lock:
            return self._approval_cache.get(ticket_id, False)

    def transition(self, ticket_id: str, new_state: TicketState, reviewer_feedback: list[dict] | None = None) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise KeyError(f"ticket not found: {ticket_id}")
            # Enforce gatekeeper approval before VERIFYING -> COMPLETE (GAP-2)
            if (ticket.state == TicketState.VERIFYING and
                new_state == TicketState.COMPLETE):
                if not self._has_gate_approval(ticket_id):
                    raise ValueError(
                        f"ticket {ticket_id} cannot transition to COMPLETE: "
                        f"gatekeeper approval required but not found"
                    )
            # Enforce planning prerequisite before READY -> IMPLEMENTING
            if (ticket.state == TicketState.READY and
                new_state == TicketState.IMPLEMENTING):
                ticket_risk_order = _RISK_ORDER.get(ticket.risk.value, 0)
                threshold_order = _RISK_ORDER.get(MIN_RISK_FOR_PLANNING.value, 1)
                if ticket_risk_order >= threshold_order:
                    if not self._has_plan(ticket_id):
                        raise ValueError(
                            f"ticket {ticket_id} risk={ticket.risk.value} "
                            f"requires an implementation plan before IMPLEMENTING; "
                            f"create plan in PLANNING state"
                        )
            updated = ticket.transition(new_state, reviewer_feedback)
            self._tickets[ticket_id] = updated
            # Maintain per-state index: remove from old state, add to new
            old_state = ticket.state
            if old_state in self._state_index:
                self._state_index[old_state].discard(ticket_id)
                if not self._state_index[old_state]:
                    del self._state_index[old_state]
            self._state_index.setdefault(new_state, set()).add(ticket_id)
            # Track dirty for incremental save
            self._dirty_ids.add(ticket_id)
            self._queue_save()
            return updated

    def list_by_state(self, state: TicketState) -> list[Ticket]:
        with self._lock:
            ticket_ids = self._state_index.get(state, set())
            return [self._tickets[tid] for tid in ticket_ids if tid in self._tickets]

    def list_ready_raw(self) -> list[Ticket]:
        """Return READY tickets without sorting (O(n)).

        Use this when callers only need to inspect ticket classes or count
        ready tickets and do not require severity ordering.
        """
        with self._lock:
            ready_ids = self._state_index.get(TicketState.READY, set())
            return [self._tickets[tid] for tid in ready_ids if tid in self._tickets]

    def list_ready(self) -> list[Ticket]:
        severity_order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }
        return sorted(self.list_ready_raw(), key=lambda t: severity_order.get(t.severity, 99))

    def count(self) -> int:
        with self._lock:
            return len(self._tickets)

    def summary(self) -> dict[str, int]:
        """Return count of tickets per state using state index for O(S) lookup.

        S is the number of distinct states (constant ~14), making this
        significantly faster than O(N) iteration for large stores.
        """
        with self._lock:
            counts: dict[str, int] = {}
            for state, ticket_ids in self._state_index.items():
                counts[state.value] = len(ticket_ids)
            return counts
