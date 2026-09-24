#!/usr/bin/env python3
"""CodeBot Ticket Engine — normalized schema and state machine.

Purpose
-------
Defines the v2 ticket schema per CODEBOT-ROADMAP.md §4 and the formal
state machine governing ticket lifecycle. All CodeBot work flows through
normalized tickets; no agent may discover an issue and immediately modify
code without an authorized work item in TRIAGED or later state.

Why
---
The previous QUEUE.md format was Monitor-specific markdown with no enforced
schema, no state transitions, and no deduplication guarantees. This module
provides a JSON-serializable ticket representation with validated state
transitions that any project adapter can consume.

Invariants
----------
- Uses stdlib plus codebot.file_lock for cross-process file locking
- Tickets are immutable once created; state changes produce new snapshots
- State transitions are validated; invalid transitions raise ValueError
- Evidence hash is SHA-256 of canonical evidence string for deduplication
- All timestamps are Unix epoch floats
- Schema version is embedded for forward compatibility
"""

from __future__ import annotations

import atexit
import hashlib
import json
import logging
import os
import queue
import re
import secrets
import shutil
import threading
import time
import weakref
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any

from codebot.file_lock import flock, LOCK_SH, LOCK_EX, LOCK_UN

logger = logging.getLogger(__name__)

SCHEMA_VERSION = "3.0"

# Strict ticket ID format: PREFIX-HEX (e.g., CB-1A2B3C4D5E6F7890).
# Prevents WAL injection or dictionary key abuse via crafted IDs.
_TICKET_ID_RE = re.compile(r"^[A-Z]{1,10}-[0-9A-F]{1,64}(-[0-9A-F]{1,64})?$")


def _validate_ticket_id(ticket_id: str) -> None:
    """Raise ValueError if ticket_id does not match strict format."""
    if not isinstance(ticket_id, str) or not _TICKET_ID_RE.match(ticket_id):
        raise ValueError(
            f"invalid ticket ID format: {ticket_id!r} "
            f"(must match ^[A-Z]+-[0-9A-F]+(-[0-9A-F]+)?$)"
        )


# Global registry of active TicketStore instances for atexit cleanup.
# Uses weak references to avoid preventing garbage collection.
_active_stores: "list[weakref.ref[TicketStore]]" = []


def _atexit_cleanup() -> None:
    """Call close() on all active TicketStore instances at process exit."""
    for ref in _active_stores:
        store = ref()
        if store is not None:
            try:
                store.close()
            except Exception:
                pass
    _active_stores.clear()


atexit.register(_atexit_cleanup)


class TicketState(str, Enum):
    DISCOVERED = "DISCOVERED"
    TRIAGED = "TRIAGED"
    GOAL = "GOAL"
    DECOMP = "DECOMP"
    DECOMPOSE = DECOMP
    PLANNING = "PLANNING"
    IMPLEMENT = "IMPLEMENT"
    IMPLEMENTING = IMPLEMENT
    REVIEW = "REVIEW"
    REVIEWING = REVIEW
    COMPLETE = "COMPLETE"
    REWORK = "REWORK"
    DEFERRED = "DEFERRED"
    LATER = "LATER"
    NEVER = "NEVER"
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    NOT_ACTIONABLE = "NOT_ACTIONABLE"
    RESOLVED = "RESOLVED"
    SUPERSEDED = "SUPERSEDED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"

    @classmethod
    def _missing_(cls, value: object) -> "TicketState | None":
        aliases = {
            "DECOMPOSE": cls.DECOMP,
            "IMPLEMENTING": cls.IMPLEMENT,
            "REVIEWING": cls.REVIEW,
        }
        if isinstance(value, str) and value in aliases:
            return aliases[value]
        return None


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

_STATE_TO_RL_EVENT: dict[str, str] = {
    "DISCOVERED": "DISCOVERY_CREATED",
    "TRIAGED": "TRIAGE_DECISION",
    "GOAL": "GOAL_DECISION",
    "DECOMP": "DECOMP_STARTED",
    "PLANNING": "PLAN_STARTED",
    "IMPLEMENT": "IMPLEMENT_STARTED",
    "REVIEW": "REVIEW_STARTED",
    "COMPLETE": "TICKET_COMPLETE",
    "REWORK": "REVIEW_REWORK",
    "DEFERRED": "TICKET_DEFERRED",
    "RESOLVED": "TICKET_RESOLVED",
    "SUPERSEDED": "TICKET_SUPERSEDED",
    "CANCELLED": "TICKET_CANCELLED",
    "REJECTED": "REVIEW_REJECTED",
    "DUPLICATE": "TICKET_DUPLICATE",
    "NOT_ACTIONABLE": "TICKET_NOT_ACTIONABLE",
    "LATER": "GOAL_REASSESSMENT",
    "NEVER": "GOAL_REASSESSMENT",
    # BLOCKED: intentionally unmapped — no RL event for blocked state
}


def get_min_risk_for_planning() -> "RiskLevel":
    """Return the current minimum risk level that requires a plan."""
    return MIN_RISK_FOR_PLANNING


def set_min_risk_for_planning(level: "RiskLevel") -> None:
    """Update the minimum risk level that requires a plan before IMPLEMENTING."""
    global MIN_RISK_FOR_PLANNING
    MIN_RISK_FOR_PLANNING = level


# Ordered risk levels for threshold comparison
_RISK_ORDER: dict[str, int] = {
    RiskLevel.LOW.value: 0,
    RiskLevel.MEDIUM.value: 1,
    RiskLevel.HIGH.value: 2,
    RiskLevel.CRITICAL.value: 3,
}


TRANSITIONS: dict[TicketState, frozenset[TicketState]] = {
    TicketState.DISCOVERED: frozenset({
        TicketState.TRIAGED,
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
        TicketState.REJECTED,
        TicketState.DUPLICATE,
    }),
    TicketState.TRIAGED: frozenset({
        TicketState.GOAL,
        TicketState.DUPLICATE,
        TicketState.NOT_ACTIONABLE,
        TicketState.REJECTED,
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
    }),
    TicketState.GOAL: frozenset({
        TicketState.DECOMP,
        TicketState.LATER,
        TicketState.NEVER,
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
    }),
    TicketState.DECOMP: frozenset({
        TicketState.PLANNING,
        TicketState.BLOCKED,
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
    }),
    TicketState.PLANNING: frozenset({
        TicketState.IMPLEMENT,
        TicketState.BLOCKED,
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
    }),
    TicketState.IMPLEMENT: frozenset({
        TicketState.REVIEW,
        TicketState.REWORK,
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
    }),
    TicketState.REVIEW: frozenset({
        TicketState.COMPLETE,
        TicketState.REWORK,
        TicketState.BLOCKED,
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
    }),
    TicketState.REWORK: frozenset({
        TicketState.IMPLEMENT,
        TicketState.PLANNING,
        TicketState.DECOMP,
        TicketState.DEFERRED,
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
    }),
    TicketState.LATER: frozenset({
        TicketState.GOAL,
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
    }),
    TicketState.DEFERRED: frozenset({
        TicketState.RESOLVED,
        TicketState.SUPERSEDED,
        TicketState.CANCELLED,
        TicketState.DECOMP,
        TicketState.IMPLEMENT,
        TicketState.BLOCKED,
    }),
    TicketState.COMPLETE: frozenset(),
    TicketState.REJECTED: frozenset(),
    TicketState.DUPLICATE: frozenset(),
    TicketState.NOT_ACTIONABLE: frozenset(),
    TicketState.RESOLVED: frozenset(),
    TicketState.SUPERSEDED: frozenset(),
    TicketState.CANCELLED: frozenset(),
    TicketState.NEVER: frozenset(),
    TicketState.BLOCKED: frozenset({
        TicketState.DECOMP,
        TicketState.PLANNING,
        TicketState.DEFERRED,
    }),
}

UNIVERSAL_EXITS = frozenset({
    TicketState.RESOLVED,
    TicketState.SUPERSEDED,
    TicketState.CANCELLED,
})


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
    commit_sha: str = ""
    committed_at: float = 0.0
    pr_url: str = ""
    confidence: str = ""
    priority: str = ""
    repo_revision: str = ""
    atomicity: str = ""
    discovery_category: str = ""
    finding_id: str = ""
    fingerprint: str = ""
    goal_disposition: str = ""
    goal_reason: str = ""
    goal_relevant_to: str = ""
    goal_revision: int = 0
    goal_aligned_at: float = 0.0
    goal_reconsider_when: str = ""
    superseded_by: str = ""
    cancelled_reason: str = ""
    cancelled_by: str = ""
    resolved_by: str = ""
    implementation_approvals: list[str] = field(default_factory=list)
    transition_version: int = 0
    current_attempt_id: str = ""

    def evidence_hash(self) -> str:
        canonical = f"{self.ticket_class}:{self.problem_statement}:{self.evidence}"
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def transition(self, new_state: TicketState, reviewer_feedback: list[dict] | None = None) -> Ticket:
        _UNIVERSAL_EXITS = frozenset({TicketState.RESOLVED, TicketState.SUPERSEDED, TicketState.CANCELLED})
        if new_state in _UNIVERSAL_EXITS and self.state != TicketState.COMPLETE:
            pass
        else:
            allowed = TRANSITIONS.get(self.state, frozenset())
            if new_state not in allowed:
                raise ValueError(
                    f"invalid transition {self.state.value} -> {new_state.value} "
                    f"(allowed: {sorted(s.value for s in allowed)})"
                )
        updates = {"state": new_state, "updated_at": time.time(), "transition_version": self.transition_version + 1}
        if new_state == TicketState.PLANNING:
            updates["implementation_approvals"] = []
        if new_state == TicketState.REWORK:
            updates["rework_count"] = self.rework_count + 1
            updates["implementation_approvals"] = []
            if reviewer_feedback:
                existing = list(self.reviewer_feedback) if self.reviewer_feedback else []
                existing.extend(reviewer_feedback)
                updates["reviewer_feedback"] = existing
        if new_state == TicketState.IMPLEMENT:
            updates["attempts"] = self.attempts + 1
            import uuid as _uuid
            updates["current_attempt_id"] = f"{self.id}:impl:{_uuid.uuid4().hex[:8]}"
        return Ticket(**{**asdict(self), **updates})

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["ticket_class"] = self.ticket_class.value
        d["severity"] = self.severity.value
        d["state"] = self.state.value
        d["risk"] = self.risk.value
        return d

    def to_json(self, *, pretty: bool = False) -> str:
        """Serialize ticket to JSON string.

        Uses compact JSON by default for performance. Pass pretty=True
        only for debugging or human-readable output.
        """
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict(), separators=(",", ":"))

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
    """Generate a globally unique ticket ID with 128-bit cryptographic randomness.

    Uses secrets.token_hex(16) to guarantee 128 bits of entropy, ensuring
    uniqueness across process restarts, module reimports, and rapid-fire
    creation bursts. No timestamp or counter is used to avoid any predictability
    or collision window. Format: CB-{32-char-hex-upper} provides 128 bits of
    cryptographic randomness per ID, making enumeration attacks computationally
    prohibitive.
    """
    return f"{prefix}-{secrets.token_hex(16).upper()}"


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
    confidence: str = "",
    priority: str = "",
    repo_revision: str = "",
    atomicity: str = "",
    discovery_category: str = "",
    finding_id: str = "",
    fingerprint: str = "",
) -> Ticket:
    if not title or not title.strip():
        raise ValueError("title is required")
    if not acceptance_criteria:
        raise ValueError("at least one acceptance criterion is required")
    if not problem_statement or not problem_statement.strip():
        raise ValueError("problem_statement is required")
    if not evidence or not str(evidence).strip():
        raise ValueError("evidence is required (file:line reference or code snippet proving the issue)")

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
        confidence=confidence,
        priority=priority,
        repo_revision=repo_revision,
        atomicity=atomicity,
        discovery_category=discovery_category,
        finding_id=finding_id,
        fingerprint=fingerprint,
    )


def _normalize_title_words(title: str) -> frozenset[str]:
    """Return a frozenset of normalized lowercase words from a title.

    Used by TicketStore for O(1)-amortized title-similarity dedup via an
    inverted word index.  Stop-words shorter than 3 characters are excluded
    to improve signal-to-noise ratio.
    """
    return frozenset(w for w in title.lower().split() if len(w) >= 3)


_PROBLEM_WORD_RE = re.compile(r"[a-z0-9_]+")


def _normalize_problem_words(text: str) -> frozenset[str]:
    words = _PROBLEM_WORD_RE.findall(text.lower())
    return frozenset(w for w in words if len(w) >= 3)


class TicketStore:
    SIMILARITY_THRESHOLD = 0.8  # Jaccard threshold for "similar" titles
    SAVE_DEBOUNCE_SECONDS = 0.5  # Debounce window for batching saves
    MAX_TICKETS_FILE_SIZE = 50 * 1024 * 1024  # 50MB max file size
    _SAVE_FAILURE_THRESHOLD = 3  # Consecutive failures before sync fallback
    # Backup frequency: perform a full backup every N compactions (not every one)
    # to amortize O(n) I/O cost of backup + prune across more saves.
    BACKUP_EVERY_N_COMPACT = 5
    # Prune frequency: prune old backups every N backups created
    PRUNE_EVERY_N_BACKUPS = 3
    # Bound on async backup queue: backups are best-effort; excess tasks
    # are dropped rather than growing memory unboundedly (DoS prevention).
    # Durability is guaranteed by synchronous WAL writes, not by backups.
    BACKUP_QUEUE_MAXSIZE = 100

    def __init__(
        self,
        path: Path | str,
        *,
        pretty: bool = False,
        start_background_workers: bool = True,
    ) -> None:
        # Coerce str -> Path so callers (including subprocess crash tests and
        # external integrations passing string paths) get a working store
        # instead of AttributeError on .exists()/.with_suffix().
        self._path = path if isinstance(path, Path) else Path(path)
        self._pretty = pretty
        self._lock = threading.RLock()
        self._tickets: dict[str, Ticket] = {}
        self._evidence_index: dict[str, str] = {}
        # Word inverted index: word -> set of ticket IDs whose title contains it
        self._word_index: dict[str, set[str]] = {}
        self._problem_index: dict[str, set[str]] = {}
        self._fingerprint_index: dict[str, str] = {}
        # Per-state index: state -> set of ticket IDs for O(k) list_by_state lookups
        self._state_index: dict[TicketState, set[str]] = {}
        # Cached per-state counts for O(1) summary() lookups
        self._state_counts: dict[str, int] = {}
        # In-memory cache for gate approvals: ticket_id -> bool (latest passed status)
        self._approval_cache: dict[str, bool] = {}
        # Dirty ticket tracking: IDs modified since last save (for incremental saves)
        self._dirty_ids: set[str] = set()
        # Pending flush set: IDs currently being written to WAL outside the lock.
        # This is disjoint from _dirty_ids to prevent TOCTOU race (Feedback #18/#23).
        self._pending_flush: set[str] = set()
        # Full-save counter: trigger full save every _FULL_SAVE_INTERVAL saves
        self._save_count: int = 0
        self._FULL_SAVE_INTERVAL: int = 50
        # Debounced save mechanism
        self._save_lock = threading.Lock()
        self._shutdown = False
        self._load()
        # Build initial state counts cache from loaded state index
        with self._lock:
            for state, ticket_ids in self._state_index.items():
                self._state_counts[state.value] = len(ticket_ids)
        self._save_queue: list[dict] = []
        self._save_condition = threading.Condition(self._save_lock)
        self._save_worker: threading.Thread | None = None
        # Backup frequency tracking: avoid O(n) backup on every compaction
        self._compact_count: int = 0  # compactions since last backup
        self._backup_count: int = 0  # backups since last prune
        # Buffered lifecycle events — flushed in batches to reduce syscalls
        self._lifecycle_event_buffer: list[dict[str, Any]] = []
        self._LIFECYCLE_FLUSH_THRESHOLD = 50
        # Save failure tracking for sync fallback
        self._save_failure_count: int = 0
        # When True, the next _save() compacts unconditionally (used by
        # flush()/close() so the snapshot reflects all WAL entries).
        self._force_compaction_on_next_save: bool = False
        # Backup worker: async file-copy and prune consumer.  Queue is bounded
        # (BACKUP_QUEUE_MAXSIZE) so rapid mutations cannot exhaust memory;
        # excess best-effort backup tasks are dropped via put_nowait.
        self._backup_queue: queue.Queue[tuple[str, str] | tuple[str, str] | None] = queue.Queue(maxsize=self.BACKUP_QUEUE_MAXSIZE)
        self._backup_shutdown = False
        self._backup_worker: threading.Thread | None = None
        if start_background_workers:
            # Daemon threads: allow pytest/process exit without hanging.
            # Durability relies on synchronous WAL appends; compaction is
            # best-effort during normal operation and forced only in close().
            self._save_worker = threading.Thread(target=self._save_worker_loop, daemon=True)
            self._save_worker.start()
            self._backup_worker = threading.Thread(target=self._backup_worker_loop, daemon=True)
            self._backup_worker.start()
        if start_background_workers:
            _active_stores.append(weakref.ref(self))

    def _save_worker_loop(self) -> None:
        """Background worker that debounces saves.

        Waits for SAVE_DEBOUNCE_SECONDS after the last mutation before
        flushing to disk, coalescing rapid-fire add()/transition() calls
        into a single I/O operation.

        On transient I/O errors the worker logs the failure and continues
        waiting for the next signal rather than dying, which previously
        caused silent data loss for all subsequent mutations.
        """
        while not self._shutdown:
            with self._save_condition:
                while not self._save_queue and not self._shutdown:
                    self._save_condition.wait(timeout=1.0)
                if self._shutdown:
                    break
                first_ts = self._save_queue[0]["ts"]
                elapsed = time.time() - first_ts
                remaining = self.SAVE_DEBOUNCE_SECONDS - elapsed
                if remaining > 0:
                    self._save_condition.wait(timeout=remaining)
                if self._shutdown:
                    break
                self._save_queue.clear()
            # Perform save outside the condition lock to avoid holding it during I/O
            if not self._shutdown:
                try:
                    self._save()
                    self._save_failure_count = 0
                except Exception as exc:
                    self._save_failure_count += 1
                    logger.error("background save failed: %s", exc)
                    if self._save_failure_count >= self._SAVE_FAILURE_THRESHOLD:
                        # Log once at threshold crossing; do NOT claim synchronous
                        # fallback exists (no such mechanism is implemented).
                        # Durability for new mutations relies on synchronous WAL
                        # appends in add()/transition(); compaction is deferred.
                        if self._save_failure_count == self._SAVE_FAILURE_THRESHOLD:
                            logger.error(
                                "consecutive save failures %d exceeds threshold %d - "
                                "background compaction disabled until recovery; "
                                "durability relies on synchronous WAL appends only",
                                self._save_failure_count,
                                self._SAVE_FAILURE_THRESHOLD,
                            )

    # ---- Backup worker ----

    def _backup_worker_loop(self) -> None:
        """Background worker that performs queued file copies (shutil.copy2).

        Consumes tasks from _backup_queue.  Supported task types:

        - ``(src, dst)`` tuples → ``shutil.copy2`` (file backup)
        - ``("__PRUNE__", backup_dir_str)`` → prune old backups
        - ``None`` sentinel → exit

        Errors are logged but never propagate so the worker keeps
        processing subsequent tasks.
        """
        while True:
            try:
                item = self._backup_queue.get(timeout=1.0)
            except queue.Empty:
                if self._backup_shutdown:
                    break
                continue
            if item is None:
                break
            try:
                if (
                    isinstance(item, tuple)
                    and len(item) == 2
                    and item[0] == "__PRUNE__"
                ):
                    backup_dir = Path(item[1])
                    self._do_async_prune(backup_dir)
                else:
                    src, dst = item
                    shutil.copy2(src, dst)
            except Exception as exc:
                logger.warning("backup worker task failed: %r: %s", item, exc)
            finally:
                self._backup_queue.task_done()

    def queue_backup_task(self, src: str, dst: str) -> None:
        """Enqueue an asynchronous file-copy backup task.

        The actual ``shutil.copy2`` runs in the background worker thread
        so the caller is never blocked.

        The queue is bounded (BACKUP_QUEUE_MAXSIZE); when full the task is
        dropped and a debug message logged.  Backups are best-effort \u2014
        durability is guaranteed by synchronous WAL writes, so dropping an
        occasional backup under burst load is safe and prevents memory
        exhaustion DoS via rapid mutations.
        """
        try:
            self._backup_queue.put_nowait((src, dst))
        except queue.Full:
            logger.debug(
                "backup queue full (%d); dropping best-effort backup task %s -> %s",
                self.BACKUP_QUEUE_MAXSIZE,
                src,
                dst,
            )

    def queue_prune_task(self, backup_dir: Path) -> None:
        """Enqueue an asynchronous backup-prune task.

        The actual directory listing, sorting, and deletion runs in the
        background worker thread so the caller is never blocked.

        Like backups, prune tasks are best-effort and dropped when the
        bounded queue is full.
        """
        try:
            self._backup_queue.put_nowait(("__PRUNE__", str(backup_dir)))
        except queue.Full:
            logger.debug(
                "backup queue full (%d); dropping best-effort prune task for %s",
                self.BACKUP_QUEUE_MAXSIZE,
                backup_dir,
            )

    def _do_async_prune(self, backup_dir: Path) -> None:
        """Prune old backup files. Called by the backup worker thread.

        Lists all backup files, sorts by mtime, and removes all but the
        newest ``keep`` files.  This is the O(m log m) operation that
        was previously on the critical save path.
        """
        try:
            backups = sorted(
                backup_dir.glob("tickets-*.json"),
                key=lambda p: p.stat().st_mtime,
            )
            keep = 20
            for old in backups[:-keep]:
                try:
                    old.unlink()
                except OSError:
                    pass
        except OSError:
            pass

    def flush(self) -> None:
        """Force an immediate save of all dirty tickets.

        Blocks until pending changes are persisted to disk.
        Use this when durability is required (e.g., before shutdown or
        in tests that verify persistence).

        Compaction-aware: if uncompacted WAL entries exist, they are
        compacted into the main snapshot so readers of tickets.json see
        every mutation, not just the first one.
        """
        self._flush_lifecycle_events()
        # Drain any pending debounce signals so the worker doesn't also
        # attempt a redundant save of the same dirty IDs.
        with self._save_condition:
            self._save_queue.clear()
        # Force a full compaction so the snapshot reflects all WAL data.
        # _save() compacts only periodically; flush() must make the file
        # itself complete for readers that inspect tickets.json directly.
        self._force_compaction_on_next_save = True
        try:
            self._save()
        finally:
            self._force_compaction_on_next_save = False

    def close(self) -> None:
        """Shut down the background save and backup workers, then flush.

        After calling close(), no further mutations or backup tasks should
        be queued.  This ensures all dirty tickets are persisted before the
        store is discarded (e.g., at process exit or test teardown).
        """
        # Shut down the backup worker first (it only does file I/O, fast).
        # Use put_nowait: the queue is bounded and may be full of
        # best-effort tasks, so a blocking put could stall close().
        # If the sentinel cannot be enqueued, the worker still exits
        # promptly via the _backup_shutdown flag on its get() timeout.
        self._backup_shutdown = True
        try:
            self._backup_queue.put_nowait(None)
        except queue.Full:
            # Discard one best-effort task to make room for the sentinel.
            try:
                self._backup_queue.get_nowait()
                self._backup_queue.task_done()
            except queue.Empty:
                pass
            try:
                self._backup_queue.put_nowait(None)
            except queue.Full:
                pass
        if self._backup_worker is not None and self._backup_worker.is_alive():
            self._backup_worker.join(timeout=5.0)

        self._shutdown = True
        with self._save_condition:
            self._save_condition.notify_all()
        if self._save_worker is not None and self._save_worker.is_alive():
            self._save_worker.join(timeout=5.0)
        # Final flush of any remaining dirty tickets and lifecycle events.
        # Force compaction so the snapshot on disk is complete even if the
        # last mutations only produced WAL entries.
        self._flush_lifecycle_events()
        self._force_compaction_on_next_save = True
        try:
            self._save()
        except Exception as exc:
            logger.critical("close() failed to persist data: %s", exc)
            raise
        finally:
            self._force_compaction_on_next_save = False

    def _load(self) -> None:
        if not self._path.exists():
            self._replay_wal()
            self._build_approval_cache()
            return
        try:
            lock_path = self._path.with_suffix(".lock")
            lock_path.touch(exist_ok=True)
            with open(lock_path, "a+") as lock_fd:
                flock(lock_fd, LOCK_EX)
                try:
                    # Check file size and read atomically using a single file descriptor
                    # to eliminate TOCTOU race between stat() and read_text()
                    with open(self._path, "r", encoding="utf-8") as fd:
                        file_size = os.fstat(fd.fileno()).st_size
                        if file_size > self.MAX_TICKETS_FILE_SIZE:
                            logger.warning(
                                "tickets.json is oversized (%.1f MB > %d MB limit); "
                                "loading empty ticket list to prevent memory exhaustion",
                                file_size / (1024 * 1024),
                                self.MAX_TICKETS_FILE_SIZE // (1024 * 1024),
                            )
                            self._tickets = {}
                            self._evidence_index = {}
                            self._word_index = {}
                            self._problem_index = {}
                            self._fingerprint_index = {}
                            self._state_index = {}
                            self._state_counts = {}
                            self._replay_wal()
                            self._build_approval_cache()
                            return
                        data = json.load(fd)
                finally:
                    flock(lock_fd, LOCK_UN)
            for entry in data.get("tickets", []):
                t = Ticket.from_dict(entry)
                self._tickets[t.id] = t
                self._evidence_index[t.evidence_hash()] = t.id
                self._index_title(t)
                self._index_problem(t)
                self._index_fingerprint(t)
                self._state_index.setdefault(t.state, set()).add(t.id)
        except (json.JSONDecodeError, KeyError, ValueError, OSError):
            self._tickets = {}
            self._evidence_index = {}
            self._word_index = {}
            self._problem_index = {}
            self._fingerprint_index = {}
            self._state_index = {}
            self._state_counts = {}
        # Replay WAL entries written after last compaction
        self._replay_wal()
        self._build_approval_cache()

    def _apply_wal_tombstone(self, ticket_id: str) -> None:
        """Apply a WAL tombstone: remove ticket and all index entries."""
        ticket = self._tickets.pop(ticket_id, None)
        if ticket is None:
            return
        self._evidence_index.pop(ticket.evidence_hash(), None)
        self._unindex_title(ticket)
        for word in _normalize_problem_words(ticket.problem_statement):
            bucket = self._problem_index.get(word)
            if bucket:
                bucket.discard(ticket_id)
                if not bucket:
                    del self._problem_index[word]
        if ticket.fingerprint:
            self._fingerprint_index.pop(ticket.fingerprint, None)
        old_set = self._state_index.get(ticket.state)
        if old_set is not None:
            old_set.discard(ticket_id)
            if not old_set:
                del self._state_index[ticket.state]

    def _replay_wal(self) -> None:
        """Replay append-only WAL to restore mutations made after last compaction."""
        wal_path = self._path.with_suffix(".wal.jsonl")
        if not wal_path.exists():
            return
        try:
            with open(wal_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        if isinstance(entry, dict) and entry.get("__deleted__"):
                            tid = entry.get("id")
                            if tid:
                                self._apply_wal_tombstone(tid)
                            continue
                        t = Ticket.from_dict(entry)
                        # Remove from old state index before updating to avoid
                        # duplicates when the same ticket appears multiple times
                        # in the WAL with different states (CB-4418574-630E).
                        old_ticket = self._tickets.get(t.id)
                        if old_ticket is not None and old_ticket.state != t.state:
                            old_set = self._state_index.get(old_ticket.state)
                            if old_set is not None:
                                old_set.discard(t.id)
                                if not old_set:
                                    del self._state_index[old_ticket.state]
                        self._tickets[t.id] = t
                        self._evidence_index[t.evidence_hash()] = t.id
                        self._index_title(t)
                        self._index_problem(t)
                        self._index_fingerprint(t)
                        self._state_index.setdefault(t.state, set()).add(t.id)
                    except (json.JSONDecodeError, KeyError, ValueError):
                        continue
        except OSError:
            pass

    def _replay_wal_into_memory_locked(self) -> None:
        """Replay WAL into in-memory state. Thread-safe via self._lock.

        Merges entries appended by other processes since this store was
        loaded. Tickets already present locally with a NEWER updated_at
        are never overwritten by stale WAL entries.

        Acquires self._lock internally to prevent concurrent mutations
        from corrupting shared state during replay (Feedback #35).
        """
        wal_path = self._path.with_suffix(".wal.jsonl")
        if not wal_path.exists():
            return
        with self._lock:
            try:
                with open(wal_path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = json.loads(line)
                            if isinstance(entry, dict) and entry.get("__deleted__"):
                                tid = entry.get("id")
                                if tid:
                                    existing = self._tickets.get(tid)
                                    if existing is not None:
                                        old_set = self._state_index.get(existing.state)
                                        if old_set is not None:
                                            old_set.discard(tid)
                                            if not old_set:
                                                del self._state_index[existing.state]
                                        self._evidence_index.pop(existing.evidence_hash(), None)
                                        self._unindex_title(existing)
                                        for word in _normalize_problem_words(existing.problem_statement):
                                            bucket = self._problem_index.get(word)
                                            if bucket:
                                                bucket.discard(tid)
                                                if not bucket:
                                                    del self._problem_index[word]
                                        if existing.fingerprint:
                                            self._fingerprint_index.pop(existing.fingerprint, None)
                                        del self._tickets[tid]
                                continue
                            t = Ticket.from_dict(entry)
                        except (json.JSONDecodeError, KeyError, ValueError):
                            continue
                        existing = self._tickets.get(t.id)
                        if existing is not None and existing.updated_at > t.updated_at:
                            continue
                        if existing is not None and existing.state != t.state:
                            old_set = self._state_index.get(existing.state)
                            if old_set is not None:
                                old_set.discard(t.id)
                                if not old_set:
                                    del self._state_index[existing.state]
                        self._tickets[t.id] = t
                        self._evidence_index[t.evidence_hash()] = t.id
                        self._index_title(t)
                        self._index_problem(t)
                        self._index_fingerprint(t)
                        self._state_index.setdefault(t.state, set()).add(t.id)
            except OSError:
                pass
            self._state_counts.clear()
            for state, ticket_ids in self._state_index.items():
                self._state_counts[state.value] = len(ticket_ids)

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

    def _index_problem(self, ticket: Ticket) -> None:
        for word in _normalize_problem_words(ticket.problem_statement):
            self._problem_index.setdefault(word, set()).add(ticket.id)

    def _index_fingerprint(self, ticket: Ticket) -> None:
        if ticket.fingerprint:
            self._fingerprint_index[ticket.fingerprint] = ticket.id

    def _jaccard_similarity(self, s1: frozenset[str], s2: frozenset[str]) -> float:
        """Compute Jaccard index between two word sets.  O(min(|s1|, |s2|))."""
        if not s1 and not s2:
            return 1.0
        intersection = len(s1 & s2)
        union = len(s1 | s2)
        return intersection / union if union else 0.0

    def _backup(self) -> None:
        """Create an atomic backup of the ticket store file.

        Performs a synchronous atomic write (read + tmp.write + tmp.replace)
        while still inside the _save() lock context. This prevents torn backup
        files that could occur when the async shutil.copy2 ran after the lock
        was released and another process was replacing the main file.

        The backup write itself uses tmp+replace to ensure no partial/corrupt
        backup files are visible to readers even under concurrent saves from
        multiple TicketStore instances.

        Periodically queues a backup-prune task to clean up old backups
        (pruning remains async as it does not affect data integrity).

        Logs OSError if backup fails rather than swallowing it silently.
        Caller handles the exception appropriately.
        """
        backup_dir = self._path.parent / "ticket_backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        ts = time.strftime("%Y%m%d-%H%M%S")
        backup_path = backup_dir / f"tickets-{ts}.json"
        # Atomic backup: read current content, write to temp, then rename
        # This runs while holding the flock in _save(), so self._path is
        # stable and will not be mid-replace by another process.
        if self._path.exists():
            content = self._path.read_text(encoding="utf-8")
            tmp = backup_path.with_suffix(".tmp")
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(backup_path)
        # Periodically prune old backups (every N backups)
        self._backup_count += 1
        if self._backup_count >= self.PRUNE_EVERY_N_BACKUPS:
            self._backup_count = 0
            self.queue_prune_task(backup_dir)

    def prune_stale_sibling_backups(self, keep_pre_backups: int = 1) -> int:
        """Delete one-off `tickets*.pre-*` / `tickets.backup.json` siblings.

        Called opportunistically before compaction; returns files removed.
        Keeps the newest `keep_pre_backups` pre-* files as a safety margin.
        """
        removed = 0
        try:
            candidates = sorted(
                (p for p in self._path.parent.glob("tickets*.pre-*") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
            )
            for old in candidates[: max(0, len(candidates) - keep_pre_backups)]:
                try:
                    old.unlink()
                    removed += 1
                except OSError:
                    pass
            legacy = self._path.parent / "tickets.backup.json"
            if legacy.exists():
                try:
                    legacy.unlink()
                    removed += 1
                except OSError:
                    pass
        except OSError:
            pass
        return removed

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
            # Check file size before reading to prevent OOM from oversized or corrupted backups
            file_size = latest.stat().st_size
            if file_size > self.MAX_TICKETS_FILE_SIZE:
                logger.warning(
                    "backup file %s is oversized (%.1f MB > %d MB limit); skipping to prevent memory exhaustion",
                    latest.name,
                    file_size / (1024 * 1024),
                    self.MAX_TICKETS_FILE_SIZE // (1024 * 1024),
                )
                return False
            data = json.loads(latest.read_text(encoding="utf-8"))
            if "tickets" not in data:
                return False
            self._tickets = {}
            self._evidence_index = {}
            self._word_index = {}
            self._problem_index = {}
            self._fingerprint_index = {}
            self._state_index = {}
            self._state_counts = {}
            for entry in data["tickets"]:
                t = Ticket.from_dict(entry)
                self._tickets[t.id] = t
                self._evidence_index[t.evidence_hash()] = t.id
                self._index_title(t)
                self._index_problem(t)
                self._index_fingerprint(t)
                self._state_index.setdefault(t.state, set()).add(t.id)
            for state, ticket_ids in self._state_index.items():
                self._state_counts[state.value] = len(ticket_ids)
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

    def _append_wal_locked(self, dirty_ids: set[str], deleted_ids: set[str] | None = None) -> None:
        """Append dirty tickets (and optional tombstones) to WAL with fsync.

        Caller MUST hold self._lock.

        This is the fast-path durability guarantee: O(K) append + fsync,
        no compaction, no backup, no file-lock acquisition. Safe to call
        inside the RLock without causing contention DoS.

        Args:
            dirty_ids: Ticket IDs that were added or modified (ticket data appended).
            deleted_ids: Optional set of ticket IDs that were removed (tombstones appended).

        Raises OSError on I/O failure (caller decides whether to retry or raise).
        """
        if not dirty_ids and not deleted_ids:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        wal_path = self._path.with_suffix(".wal.jsonl")
        lines = []
        for tid in dirty_ids:
            ticket = self._tickets.get(tid)
            if ticket is not None:
                lines.append(json.dumps(ticket.to_dict(), separators=(",", ":")))
        if deleted_ids:
            for tid in deleted_ids:
                tombstone = {"__deleted__": True, "id": tid}
                lines.append(json.dumps(tombstone, separators=(",", ":")))
        if lines:
            with open(wal_path, "a", encoding="utf-8") as wf:
                wf.write("\n".join(lines) + "\n")
                wf.flush()
                # fsync MUST propagate: silent failure violates crash durability
                # guarantee. Callers handle exceptions appropriately.
                os.fsync(wf.fileno())
            # Directory fsync: ensures the WAL file entry itself is durable.
            # Critical after compaction which deletes+recreates the WAL file;
            # without this, power loss can lose the entire WAL even if its
            # contents were fsynced (Feedback #2).
            # Directory fsync failures MUST propagate: silent failure violates
            # crash durability guarantee (CB-9607C).
            dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)

    def _save(self) -> None:
        """Atomically save ticket store using append-only WAL + periodic compaction.

        Instead of serializing the entire store on every mutation (O(N)), this
        method appends only dirty tickets to a Write-Ahead Log (O(K) where K is
        the number of changed tickets). A full JSON snapshot is written only
        during periodic compaction, keeping single-mutation I/O constant.

        Retries lock acquisition up to 3 times with exponential backoff.
        
        CRITICAL: Uses two-set approach to prevent TOCTOU race (Feedback #18/#23).
        IDs are moved from _dirty_ids to _pending_flush under lock, then written
        outside the lock. New mutations go to _dirty_ids which is disjoint from
        _pending_flush. On write failure, _pending_flush is merged back.
        
        CRITICAL: Backup is performed INSIDE the lock context to prevent race
        conditions where backup failure could cause lock release before write
        completes (CB-1835532-11DC). The lock is held throughout the entire
        critical section including backup, ensuring atomicity.
        """
        # Snapshot payloads under lock to prevent stale data read off-lock.
        # This closes the TOCTOU window where concurrent mutations during I/O
        # could be shadowed by stale ticket snapshots.
        # All shared-state reads for compaction decision are snapshotted
        # inside this lock to satisfy AC#1 (no shared reads outside lock).
        with self._lock:
            if not self._dirty_ids:
                return
            # Atomically move dirty IDs to pending flush set.
            # _pending_flush is disjoint from _dirty_ids after this point.
            self._pending_flush = self._dirty_ids.copy()
            self._dirty_ids.clear()
            # Snapshot serialized payloads while still holding the lock.
            # This ensures we write the exact state at the moment of handoff,
            # preventing newer mutations (which go to _dirty_ids) from being
            # overwritten by stale data during off-lock I/O.
            pending_payloads: list[str] = []
            for tid in self._pending_flush:
                if tid.startswith("__deleted__:"):
                    tombstone = {"__deleted__": True, "id": tid.split(":", 1)[1]}
                    pending_payloads.append(json.dumps(tombstone, separators=(",", ":")))
                else:
                    ticket = self._tickets.get(tid)
                    if ticket is not None:
                        pending_payloads.append(json.dumps(ticket.to_dict(), separators=(",", ":")))
            pending_ids_snapshot = self._pending_flush.copy()
            # Snapshot shared-state decision variables atomically with payloads.
            total_snapshot = len(self._tickets)
            self._save_count += 1
            save_count_snapshot = self._save_count
            force_compaction_snapshot = self._force_compaction_on_next_save

        if not pending_ids_snapshot:
            return

        # Emergency compaction trigger: if WAL exceeds 1MB, force compaction
        # to prevent unbounded growth if background worker is lagging (Feedback #17).
        wal_path_check = self._path.with_suffix(".wal.jsonl")
        wal_size_exceeded = False
        try:
            if wal_path_check.exists():
                wal_size_exceeded = wal_path_check.stat().st_size > 1_000_000  # 1MB
        except OSError:
            pass

        needs_compaction = (
            force_compaction_snapshot
            or not self._path.exists()
            or len(pending_ids_snapshot) >= max(total_snapshot * 0.3, 50)
            or save_count_snapshot % self._FULL_SAVE_INTERVAL == 0
            or wal_size_exceeded
        )

        lock_path = self._path.with_suffix(".lock")
        max_retries = 3
        base_delay = 0.1

        for attempt in range(max_retries):
            try:
                # Ensure parent directory exists so lock/tmp/WAL sibling files
                # can be created even when the store path was constructed under
                # a not-yet-created directory (prevents ENOENT on first save).
                try:
                    self._path.parent.mkdir(parents=True, exist_ok=True)
                except OSError:
                    pass
                with open(lock_path, "a+") as lock_fd:
                    flock(lock_fd, LOCK_EX)
                    try:
                        if needs_compaction:
                            # Hold RLock during replay + payload build to ensure
                            # a consistent snapshot. New mutations arriving after
                            # release will go to WAL and survive crash.
                            with self._lock:
                                # Merge WAL entries written by OTHER processes since
                                # our in-memory snapshot was loaded: replay the WAL
                                # into memory BEFORE building the full payload so a
                                # concurrent compaction cannot silently drop their
                                # mutations (cross-process lost-update fix).
                                self._replay_wal_into_memory_locked()
                                payload = self._build_full_payload()
                            tmp = self._path.with_suffix(".tmp")
                            content = (
                                json.dumps(payload, separators=(",", ":"))
                                if not self._pretty
                                else json.dumps(payload, indent=2)
                            )
                            with open(tmp, "w", encoding="utf-8") as tf:
                                tf.write(content)
                                tf.flush()
                                # fsync MUST propagate: silent failure violates
                                # crash durability guarantee during compaction.
                                os.fsync(tf.fileno())
                            # Verify atomic replace succeeded by checking file exists and is valid JSON
                            try:
                                tmp.replace(self._path)
                                # Post-replace verification: ensure the file is readable
                                # This catches rare cases where replace succeeds but file is corrupted
                                with open(self._path, "r", encoding="utf-8") as verify_fd:
                                    json.load(verify_fd)  # Raises on corrupt JSON
                            except (OSError, json.JSONDecodeError) as exc:
                                logger.error("atomic replace or verification failed: %s", exc)
                                raise RuntimeError(f"atomic replace failed: {exc}") from exc
                            # fsync the directory so the rename itself is
                            # durable before _save() returns to the caller.
                            # Directory fsync failures MUST propagate: silent failure
                            # violates crash durability guarantee (CB-9607C).
                            dir_fd = os.open(str(self._path.parent), os.O_RDONLY)
                            try:
                                os.fsync(dir_fd)
                            finally:
                                os.close(dir_fd)
                            # Clear WAL after successful compaction, but ONLY if
                            # no new mutations accumulated during the write.
                            # If _dirty_ids has entries, they were added after
                            # _build_full_payload() and their WAL entries must
                            # survive for the next save cycle.
                            with self._lock:
                                if not self._dirty_ids:
                                    wal_path = self._path.with_suffix(".wal.jsonl")
                                    try:
                                        wal_path.unlink(missing_ok=True)
                                    except OSError:
                                        pass
                                # Discard flushed IDs from _pending_flush.
                                self._pending_flush -= pending_ids_snapshot
                            # CRITICAL: Backup is performed AFTER the atomic write completes
                            # and the lock is still held. This ensures that backup failure
                            # cannot cause lock release before the write completes,
                            # preventing the race condition where backup failure could
                            # allow another process to acquire the lock during exception
                            # handling (CB-5492279F973917EF8DDD9F1F9EA1B071).
                            try:
                                self._backup()
                                self.prune_stale_sibling_backups()
                            except OSError as exc:
                                logger.warning("backup failed after compaction: %s", exc)
                                # Continue even if backup fails.
                                # Backup is best-effort; durability is guaranteed by WAL.
                                # The atomic write already completed successfully.
                        else:
                            # Append-only WAL write: O(K) serialization.
                            # fsync ensures durability before return so a crash
                            # within the debounce window cannot lose mutations.
                            # Tombstones (``__deleted__:ID``) persist removes;
                            # live dirty IDs serialize current ticket state.
                            # Use pre-snapshotted payloads to avoid off-lock reads.
                            wal_path = self._path.with_suffix(".wal.jsonl")
                            if pending_payloads:
                                with open(wal_path, "a", encoding="utf-8") as wf:
                                    wf.write("\n".join(pending_payloads) + "\n")
                                    wf.flush()
                                    os.fsync(wf.fileno())
                            # WAL append succeeded: pending_ids_snapshot are now durable.
                            # Discard them from _pending_flush; new mutations in
                            # _dirty_ids will be flushed in the next cycle.
                            with self._lock:
                                self._pending_flush -= pending_ids_snapshot
                        return
                    except OSError as e:
                        # Write failed: merge pending_ids_snapshot back into _dirty_ids so
                        # the mutations are not lost and will be retried.
                        with self._lock:
                            self._dirty_ids |= pending_ids_snapshot
                            self._pending_flush -= pending_ids_snapshot
                        if attempt < max_retries - 1:
                            delay = base_delay * (2 ** attempt)
                            time.sleep(delay)
                        else:
                            raise RuntimeError(
                                f"TicketStore._save failed after {max_retries} lock retries: {e}. "
                                f"Data may be at risk if concurrent writes occurred."
                            ) from e
                    finally:
                        flock(lock_fd, LOCK_UN)
            except OSError as e:
                if attempt < max_retries - 1:
                    time.sleep(base_delay * (2 ** attempt))
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

    def add(self, ticket: Ticket) -> Ticket:
        _validate_ticket_id(ticket.id)
        eh = ticket.evidence_hash()
        with self._lock:
            if eh in self._evidence_index:
                existing_id = self._evidence_index[eh]
                existing = self._tickets.get(existing_id)
                if existing and existing.state not in (
                    TicketState.COMPLETE,
                    TicketState.REJECTED,
                    TicketState.DUPLICATE,
                    TicketState.LATER,
                    TicketState.NEVER,
                    TicketState.NOT_ACTIONABLE,
                    TicketState.RESOLVED,
                    TicketState.SUPERSEDED,
                    TicketState.CANCELLED,
                    TicketState.DEFERRED,
                ):
                    raise ValueError(
                        f"duplicate ticket: evidence matches {existing_id}"
                    )
            if ticket.fingerprint:
                fp_id = self._fingerprint_index.get(ticket.fingerprint)
                if fp_id and fp_id != ticket.id:
                    fp_ticket = self._tickets.get(fp_id)
                    if fp_ticket and fp_ticket.state not in (
                        TicketState.COMPLETE,
                        TicketState.REJECTED,
                        TicketState.DUPLICATE,
                        TicketState.LATER,
                        TicketState.NEVER,
                        TicketState.NOT_ACTIONABLE,
                        TicketState.RESOLVED,
                        TicketState.SUPERSEDED,
                        TicketState.CANCELLED,
                        TicketState.DEFERRED,
                    ):
                        raise ValueError(
                            f"duplicate ticket: fingerprint matches {fp_id}"
                        )
            self._tickets[ticket.id] = ticket
            self._evidence_index[eh] = ticket.id
            self._index_title(ticket)
            self._index_problem(ticket)
            self._index_fingerprint(ticket)
            # Maintain per-state index and counts cache
            self._state_index.setdefault(ticket.state, set()).add(ticket.id)
            self._state_counts[ticket.state.value] = len(self._state_index[ticket.state])
            # Track dirty for incremental save
            self._dirty_ids.add(ticket.id)
            # Hybrid durability: fast WAL append under lock guarantees crash
            # safety without holding the lock during expensive compaction I/O.
            try:
                self._append_wal_locked({ticket.id})
            except Exception as exc:
                logger.error("WAL append failed in add(): %s", exc)
                raise
        # Signal background worker for deferred compaction (outside lock).
        self._queue_save()
        return ticket

    def record_commit(self, ticket_id: str, sha: str, pr_url: str = "") -> Ticket | None:
        """Record the git SHA (and PR URL) for a COMPLETE ticket."""
        _validate_ticket_id(ticket_id)
        import time as _time
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                return None
            if ticket.commit_sha == sha and sha and (not pr_url or ticket.pr_url == pr_url):
                return ticket
            updated = Ticket(**{**asdict(ticket), "commit_sha": sha,
                                "committed_at": _time.time(),
                                "pr_url": pr_url or ticket.pr_url})
            self._tickets[ticket_id] = updated
            self._dirty_ids.add(ticket_id)
            # Hybrid durability: fast WAL append under lock guarantees crash
            # safety without holding the lock during expensive compaction I/O.
            try:
                self._append_wal_locked({ticket_id})
            except Exception as exc:
                logger.error("WAL append failed in record_commit(): %s", exc)
                raise
        # Signal background worker for deferred compaction (outside lock).
        self._queue_save()
        return updated

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

    def update_gate_approval(self, ticket_id: str, passed: bool) -> None:
        with self._lock:
            self._approval_cache[ticket_id] = passed

    def _has_gate_approval(self, ticket_id: str) -> bool:
        """Check if the latest gate result for ticket_id indicates approval.

        Uses the in-memory _approval_cache for O(1) lookup instead of
        scanning gate_results.jsonl on every call. The cache is populated
        at load time via _build_approval_cache and updated on each
        record_gate_result call.
        """
        with self._lock:
            return self._approval_cache.get(ticket_id, False)

    def _build_lifecycle_event(
        self,
        ticket_id: str,
        old_state: TicketState,
        prev_updated_at: float,
        updated: Ticket,
        actor: str,
    ) -> dict[str, Any]:
        """Build an additive lifecycle event record for one successful transition."""
        return {
            "ticket_id": ticket_id,
            "from_state": old_state.value,
            "to_state": updated.state.value,
            "timestamp": updated.updated_at,
            "attempts": updated.attempts,
            "rework_count": updated.rework_count,
            "queue_age_seconds": round(max(0.0, updated.updated_at - prev_updated_at), 6),
            "actor": actor,
            "revision": updated.updated_at,
        }

    def _emit_lifecycle_event(self, event: dict[str, Any]) -> None:
        """Buffer a lifecycle event; flushed in batches to reduce file I/O syscalls."""
        self._lifecycle_event_buffer.append(event)
        if len(self._lifecycle_event_buffer) >= self._LIFECYCLE_FLUSH_THRESHOLD:
            self._flush_lifecycle_events()

    def _flush_lifecycle_events(self) -> None:
        """Write all buffered lifecycle events to disk in a single append."""
        if not self._lifecycle_event_buffer:
            return
        try:
            events_path = self._path.parent / "lifecycle_events.jsonl"
            lines = "\n".join(json.dumps(e) for e in self._lifecycle_event_buffer) + "\n"
            with open(events_path, "a", encoding="utf-8") as f:
                f.write(lines)
        except OSError:
            pass
        self._lifecycle_event_buffer.clear()

    def _emit_rl_event(
        self,
        ticket_id: str,
        old_state: TicketState,
        new_state: TicketState,
        ticket: Ticket,
        actor: str = "",
        reviewer_feedback: list[dict] | None = None,
    ) -> None:
        try:
            from codebot.rl_event_log import append_event
            event_type = _STATE_TO_RL_EVENT.get(new_state.value, "")
            if not event_type:
                return
            tc = getattr(ticket, "ticket_class", None)
            tc_val = tc.value if hasattr(tc, "value") else str(tc) if tc else ""
            risk = getattr(ticket, "risk", None)
            risk_val = risk.value if hasattr(risk, "value") else str(risk) if risk else ""
            sev = getattr(getattr(ticket, "severity", None), "value", "")
            context: dict[str, Any] = {}
            if new_state == TicketState.REWORK and reviewer_feedback:
                try:
                    from codebot.rl_failure_taxonomy import extract_attribution_from_reviewer_feedback
                    context["attribution"] = extract_attribution_from_reviewer_feedback(reviewer_feedback)
                except Exception:
                    pass
            tv = getattr(ticket, "transition_version", 0)
            transition_id = f"{ticket_id}:v{tv}"
            attempt_id = getattr(ticket, "current_attempt_id", "") or ""
            append_event(
                event_type,
                state_dir=self._path.parent,
                ticket_id=ticket_id,
                stage=new_state.value,
                actor_type="agent" if actor else "system",
                actor_role=actor,
                model=getattr(ticket, "assigned_model", "") or "",
                attempt_id=attempt_id,
                transition_id=transition_id,
                input_features={
                    "ticket_class": tc_val,
                    "severity": sev,
                    "risk": risk_val,
                    "affected_modules": list(getattr(ticket, "affected_modules", []) or []),
                    "rework_count": getattr(ticket, "rework_count", 0),
                    "from_state": old_state.value,
                },
                decision={"to_state": new_state.value},
                context=context,
            )
            terminal_states = {"COMPLETE", "RESOLVED", "REJECTED", "DEFERRED", "CANCELLED"}
            ns = new_state.value
            if ns in terminal_states:
                failure_origin = context.get("attribution", {}).get("failure_origin", "UNKNOWN")
                failure_type = context.get("attribution", {}).get("failure_type", "UNKNOWN")
                append_event(
                    "TICKET_TERMINAL",
                    state_dir=self._path.parent,
                    ticket_id=ticket_id,
                    attempt_id=attempt_id,
                    transition_id=transition_id,
                    actor_role=actor,
                    model=getattr(ticket, "assigned_model", "") or "",
                    decision={
                        "terminal_state": ns,
                        "failure_origin": failure_origin,
                        "failure_type": failure_type,
                    },
                    input_features={
                        "ticket_class": tc_val,
                        "severity": sev,
                        "risk": risk_val,
                    },
                )
        except Exception as e:
            from codebot.rl_diagnostics import record_rl_error
            record_rl_error(str(self._path.parent), "emit_rl_event", e)

    def record_implementation_approval(self, ticket_id: str, role: str, actor: str = "") -> Ticket | None:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                return None
            approvals = list(ticket.implementation_approvals or [])
            if role and role not in approvals:
                approvals.append(role)
            updated = Ticket(**{**asdict(ticket), "implementation_approvals": approvals, "updated_at": time.time()})
            self._tickets[ticket_id] = updated
            self._dirty_ids.add(ticket_id)
            try:
                from codebot.lifecycle_packet import LifecyclePacketStore
                LifecyclePacketStore(self._path.parent).record_implementation_approval(
                    ticket_id, role, updated.updated_at
                )
                LifecyclePacketStore(self._path.parent).record_transition(updated, ticket.state.value, actor or role)
            except OSError:
                pass
            # Hybrid durability: fast WAL append under lock guarantees crash
            # safety without holding the lock during expensive compaction I/O.
            try:
                self._append_wal_locked({ticket_id})
            except Exception as exc:
                logger.error("WAL append failed in record_implementation_approval(): %s", exc)
                raise
        # Signal background worker for deferred compaction (outside lock).
        self._queue_save()
        return updated

    def transition(self, ticket_id: str, new_state: TicketState, reviewer_feedback: list[dict] | None = None, actor: str = "") -> Ticket:
        _validate_ticket_id(ticket_id)
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise KeyError(f"ticket not found: {ticket_id}")
            UNIVERSAL_EXITS = frozenset({TicketState.RESOLVED, TicketState.SUPERSEDED, TicketState.CANCELLED})
            if new_state in UNIVERSAL_EXITS and ticket.state != TicketState.COMPLETE:
                pass
            else:
                if (ticket.state == TicketState.REVIEW and
                    new_state == TicketState.COMPLETE):
                    if not self._has_gate_approval(ticket_id):
                        raise ValueError(
                            f"ticket {ticket_id} cannot transition to COMPLETE: "
                            f"gatekeeper approval required but not found"
                        )
                if (ticket.state == TicketState.PLANNING and
                    new_state == TicketState.IMPLEMENT):
                    ticket_risk_order = _RISK_ORDER.get(ticket.risk.value, 0)
                    threshold_order = _RISK_ORDER.get(MIN_RISK_FOR_PLANNING.value, 1)
                    if ticket_risk_order >= threshold_order:
                        if not self._has_plan(ticket_id):
                            raise ValueError(
                                f"ticket {ticket_id} risk={ticket.risk.value} "
                                f"requires an implementation plan before IMPLEMENT; "
                                f"create plan in PLANNING state"
                            )
            old_state = ticket.state
            prev_updated_at = ticket.updated_at
            updated = ticket.transition(new_state, reviewer_feedback)
            self._tickets[ticket_id] = updated
            # Maintain per-state index: remove from old state, add to new
            if old_state in self._state_index:
                self._state_index[old_state].discard(ticket_id)
                if not self._state_index[old_state]:
                    del self._state_index[old_state]
                    self._state_counts.pop(old_state.value, None)
                else:
                    self._state_counts[old_state.value] = len(self._state_index[old_state])
            self._state_index.setdefault(new_state, set()).add(ticket_id)
            self._state_counts[new_state.value] = len(self._state_index[new_state])
            # Track dirty for incremental save
            self._dirty_ids.add(ticket_id)
            self._emit_lifecycle_event(
                self._build_lifecycle_event(ticket_id, old_state, prev_updated_at, updated, actor)
            )
            try:
                from codebot.lifecycle_packet import LifecyclePacketStore
                LifecyclePacketStore(self._path.parent).record_transition(updated, old_state.value, actor)
            except OSError:
                pass
            # Hybrid durability: fast WAL append under lock guarantees crash
            # safety without holding the lock during expensive compaction I/O.
            try:
                self._append_wal_locked({ticket_id})
            except Exception as exc:
                logger.error("WAL append failed in transition(): %s", exc)
                raise
        # Signal background worker for deferred compaction (outside lock).
        self._queue_save()
        self._emit_rl_event(ticket_id, old_state, new_state, updated, actor, reviewer_feedback)
        return updated

    def batch_transition(
        self,
        transitions: list[tuple[str, TicketState, list[dict] | None]],
        actor: str = "",
    ) -> list[Ticket]:
        """Apply multiple ticket transitions in memory, then save once.

        This method avoids the O(K*N) cost of calling transition() K times
        by collecting all state changes in memory under a single lock hold,
        updating indices, and queuing a single save operation.

        Args:
            transitions: List of (ticket_id, new_state, reviewer_feedback) tuples.

        Returns:
            List of updated Ticket objects in the same order as input.

        Raises:
            KeyError: If any ticket_id is not found.
            ValueError: If any transition is invalid or prerequisites not met.
        """
        if not transitions:
            return []

        results: list[Ticket] = []
        pending_events: list[dict[str, Any]] = []
        with self._lock:
            # Snapshot state for rollback on failure to guarantee atomicity.
            # We capture only the tickets that will be mutated so the cost is
            # O(K) rather than O(N).
            original_tickets: dict[str, Ticket] = {}
            original_state_index_entries: list[tuple[str, TicketState]] = []
            # Snapshot dirty_ids BEFORE batch to restore correctly on rollback.
            # Feedback #20/#24: subtracting batch keys corrupts pre-existing
            # dirty tracking for tickets modified before the batch.
            dirty_ids_snapshot = self._dirty_ids.copy()

            try:
                for ticket_id, new_state, reviewer_feedback in transitions:
                    _validate_ticket_id(ticket_id)
                    ticket = self._tickets.get(ticket_id)
                    if ticket is None:
                        raise KeyError(f"ticket not found: {ticket_id}")

                    # Save original state for potential rollback
                    if ticket_id not in original_tickets:
                        original_tickets[ticket_id] = ticket
                        original_state_index_entries.append((ticket_id, ticket.state))

                    UNIVERSAL_EXITS_BT = frozenset({TicketState.RESOLVED, TicketState.SUPERSEDED, TicketState.CANCELLED})
                    if new_state in UNIVERSAL_EXITS_BT and ticket.state != TicketState.COMPLETE:
                        pass
                    else:
                        if (ticket.state == TicketState.REVIEW and
                            new_state == TicketState.COMPLETE):
                            if not self._has_gate_approval(ticket_id):
                                raise ValueError(
                                    f"ticket {ticket_id} cannot transition to COMPLETE: "
                                    f"gatekeeper approval required but not found"
                                )

                        if (ticket.state == TicketState.PLANNING and
                            new_state == TicketState.IMPLEMENT):
                            ticket_risk_order = _RISK_ORDER.get(ticket.risk.value, 0)
                            threshold_order = _RISK_ORDER.get(MIN_RISK_FOR_PLANNING.value, 1)
                            if ticket_risk_order >= threshold_order:
                                if not self._has_plan(ticket_id):
                                    raise ValueError(
                                        f"ticket {ticket_id} risk={ticket.risk.value} "
                                        f"requires an implementation plan before IMPLEMENT; "
                                        f"create plan in PLANNING state"
                                    )

                    old_state = ticket.state
                    prev_updated_at = ticket.updated_at
                    updated = ticket.transition(new_state, reviewer_feedback)
                    self._tickets[ticket_id] = updated

                    # Maintain per-state index: remove from old state, add to new
                    if old_state in self._state_index:
                        self._state_index[old_state].discard(ticket_id)
                        if not self._state_index[old_state]:
                            del self._state_index[old_state]
                            self._state_counts.pop(old_state.value, None)
                        else:
                            self._state_counts[old_state.value] = len(self._state_index[old_state])
                    self._state_index.setdefault(new_state, set()).add(ticket_id)
                    self._state_counts[new_state.value] = len(self._state_index[new_state])

                    # Track dirty for incremental save
                    self._dirty_ids.add(ticket_id)
                    pending_events.append(
                        self._build_lifecycle_event(ticket_id, old_state, prev_updated_at, updated, actor)
                    )
                    results.append(updated)
            except (ValueError, KeyError):
                # Rollback all mutations applied so far in this batch
                for tid, orig_ticket in original_tickets.items():
                    self._tickets[tid] = orig_ticket
                # Restore state index entries and counts cache
                for tid, orig_state in original_state_index_entries:
                    current_ticket = self._tickets.get(tid)
                    if current_ticket is not None:
                        # Remove from whatever state it was moved to
                        for state, state_set in list(self._state_index.items()):
                            if tid in state_set:
                                state_set.discard(tid)
                                if not state_set:
                                    del self._state_index[state]
                                    self._state_counts.pop(state.value, None)
                                else:
                                    self._state_counts[state.value] = len(state_set)
                        # Re-add to original state
                        self._state_index.setdefault(orig_state, set()).add(tid)
                        self._state_counts[orig_state.value] = len(self._state_index[orig_state])
                # Restore dirty_ids to pre-batch snapshot. This preserves
                # pre-existing dirty flags for tickets modified before the batch.
                # Feedback #20/#24: previous -= approach corrupted state.
                self._dirty_ids = dirty_ids_snapshot
                raise

            # Atomic persistence: Write WAL synchronously under the lock to ensure
            # all batch mutations are durable before any other thread can observe
            # or modify the store. This prevents the window where concurrent
            # mutations could interleave and cause atomicity violations.
            # CRITICAL: Only WAL-write IDs modified in THIS batch to preserve
            # atomicity and prevent cross-transaction state leakage on crash.
            # NOTE: We do NOT call _save() (compaction) here to avoid holding the
            # lock during expensive I/O (Feedback #6/#9/#12). Durability is guaranteed
            # by the WAL append; compaction is deferred to the background worker.
            batch_specific_ids = {t.id for t in results}
            try:
                self._append_wal_locked(batch_specific_ids)
            except Exception as exc:
                logger.error("WAL append failed in batch_transition(): %s", exc)
                raise
            # Defer compaction to background worker to release lock quickly.
            # The WAL entries above ensure crash safety.
            self._queue_save()

        for event in pending_events:
            self._emit_lifecycle_event(event)
            try:
                from codebot.lifecycle_packet import LifecyclePacketStore
                updated = self._tickets.get(event["ticket_id"])
                if updated is not None:
                    LifecyclePacketStore(self._path.parent).record_transition(
                        updated, event["from_state"], actor,
                    )
            except OSError:
                pass
            try:
                old_st = TicketState(event["from_state"])
                new_st = TicketState(event["to_state"])
                tkt = self._tickets.get(event["ticket_id"])
                if tkt is not None:
                    fb = None
                    for tid_in, new_state_in, fb_in in transitions:
                        if tid_in == event["ticket_id"]:
                            fb = fb_in
                            break
                    self._emit_rl_event(event["ticket_id"], old_st, new_st, tkt, actor, fb)
            except Exception:
                pass
        return results

    def remove(self, ticket_id: str) -> Ticket | None:
        """Remove a ticket from the store atomically.

        Removes the ticket from _tickets and all indexes (_evidence_index,
        _word_index, _problem_index, _fingerprint_index, _state_index,
        _state_counts), marks dirty and queues save. Returns the removed
        ticket or None if not found.

        Args:
            ticket_id: The ID of the ticket to remove.

        Returns:
            The removed Ticket object, or None if ticket_id was not found.
        """
        _validate_ticket_id(ticket_id)
        with self._lock:
            ticket = self._tickets.pop(ticket_id, None)
            if ticket is None:
                return None

            # Remove from evidence index
            eh = ticket.evidence_hash()
            self._evidence_index.pop(eh, None)

            # Remove from word index via unindex helper
            self._unindex_title(ticket)

            # Remove from problem index
            for word in _normalize_problem_words(ticket.problem_statement):
                bucket = self._problem_index.get(word)
                if bucket:
                    bucket.discard(ticket_id)
                    if not bucket:
                        del self._problem_index[word]

            # Remove from fingerprint index
            if ticket.fingerprint:
                self._fingerprint_index.pop(ticket.fingerprint, None)

            # Remove from state index and update counts
            old_state = ticket.state
            if old_state in self._state_index:
                self._state_index[old_state].discard(ticket_id)
                if not self._state_index[old_state]:
                    del self._state_index[old_state]
                    self._state_counts.pop(old_state.value, None)
                else:
                    self._state_counts[old_state.value] = len(self._state_index[old_state])

            # Track dirty tombstone for incremental save: record removed id
            # separately so WAL compaction (snapshot-based) can drop it, and
            # WAL-only replay can apply the tombstone via _apply_wal_tombstone.
            self._dirty_ids.discard(ticket_id)
            self._dirty_ids.add(f"__deleted__:{ticket_id}")
            # Hybrid durability: fast WAL append under lock guarantees crash
            # safety without holding the lock during expensive compaction I/O.
            try:
                self._append_wal_locked(set(), {ticket_id})
            except Exception as exc:
                logger.error("WAL append failed in remove(): %s", exc)
                raise
        # Signal background worker for deferred compaction (outside lock).
        self._queue_save()
        return ticket

    def list_by_state(self, state: TicketState, include_workers: bool = True) -> list[Ticket]:
        with self._lock:
            ticket_ids = self._state_index.get(state, set())
            tickets = [self._tickets[tid] for tid in ticket_ids if tid in self._tickets]
            if not include_workers:
                tickets = [t for t in tickets if not getattr(t, "assigned_agent", "")]
            return tickets

    def list_tickets(self, include_workers: bool = True) -> list[Ticket]:
        with self._lock:
            tickets = list(self._tickets.values())
            if not include_workers:
                tickets = [t for t in tickets if not getattr(t, "assigned_agent", "")]
            return tickets

    def list_all(self, include_workers: bool = True) -> list[Ticket]:
        return self.list_tickets(include_workers=include_workers)

    def count(self) -> int:
        with self._lock:
            return len(self._tickets)

    def summary(self) -> dict[str, int]:
        """Return count of tickets per state in O(1) using cached counts.

        The _state_counts cache is maintained atomically alongside _state_index
        during add/transition/remove operations, avoiding any iteration over
        states on each call.
        """
        with self._lock:
            return dict(self._state_counts)

    def find_similar(
        self,
        problem_statement: str,
        affected_modules: list[str] | None = None,
        threshold: float | None = None,
        exclude_states: frozenset[TicketState] | None = None,
        limit: int = 10,
    ) -> list[tuple[Ticket, float]]:
        if threshold is None:
            threshold = self.SIMILARITY_THRESHOLD
        query_words = _normalize_problem_words(problem_statement)
        if affected_modules:
            for module in affected_modules:
                query_words = query_words | _normalize_problem_words(module)
        if not query_words:
            return []
        with self._lock:
            candidate_ids: set[str] = set()
            for word in query_words:
                bucket = self._problem_index.get(word)
                if bucket:
                    candidate_ids |= bucket
            results: list[tuple[Ticket, float]] = []
            for tid in candidate_ids:
                ticket = self._tickets.get(tid)
                if ticket is None:
                    continue
                if exclude_states and ticket.state in exclude_states:
                    continue
                ticket_words = _normalize_problem_words(ticket.problem_statement)
                for module in ticket.affected_modules:
                    ticket_words = ticket_words | _normalize_problem_words(module)
                similarity = self._jaccard_similarity(query_words, ticket_words)
                if similarity >= threshold:
                    results.append((ticket, similarity))
        results.sort(key=lambda pair: pair[1], reverse=True)
        return results[:limit]

    def discovery_history(
        self,
        fingerprint: str = "",
        evidence_hash: str = "",
        discovery_category: str = "",
    ) -> list[Ticket]:
        matches: dict[str, Ticket] = {}
        with self._lock:
            if fingerprint and fingerprint in self._fingerprint_index:
                tid = self._fingerprint_index[fingerprint]
                ticket = self._tickets.get(tid)
                if ticket is not None:
                    matches[tid] = ticket
            if evidence_hash and evidence_hash in self._evidence_index:
                tid = self._evidence_index[evidence_hash]
                ticket = self._tickets.get(tid)
                if ticket is not None:
                    matches[tid] = ticket
            if discovery_category:
                terminal = (
                    TicketState.COMPLETE,
                    TicketState.REJECTED,
                    TicketState.DUPLICATE,
                )
                for tid, ticket in self._tickets.items():
                    if (
                        ticket.discovery_category == discovery_category
                        and ticket.state in terminal
                    ):
                        matches[tid] = ticket
        return sorted(matches.values(), key=lambda t: t.updated_at, reverse=True)


class ReadOnlyTicketView:
    def __init__(self, store: TicketStore):
        object.__setattr__(self, "_store", store)

    def get(self, ticket_id: str):
        return self._store.get(ticket_id)

    def get_by_id(self, ticket_id: str):
        return self._store.get_by_id(ticket_id)

    def list_by_state(self, state, include_workers: bool = False):
        return self._store.list_by_state(state, include_workers=include_workers)

    def list_tickets(self, include_workers: bool = False):
        return self._store.list_tickets(include_workers=include_workers)

    def list_all(self, include_workers: bool = False):
        return self._store.list_all(include_workers=include_workers)

    def summary(self):
        return self._store.summary()

    def count(self):
        return self._store.count()

    def find_similar(self, *a, **kw):
        return self._store.find_similar(*a, **kw)

    def discovery_history(self, *a, **kw):
        return self._store.discovery_history(*a, **kw)


# ---------------------------------------------------------------------------
# QueueManager — Adapter interface for queue depth and ticket class queries
# ---------------------------------------------------------------------------

class QueueManager:
    def __init__(self, store: TicketStore | None = None, *, state_dir: Path | None = None):
        if store is not None:
            self._store: TicketStore | None = store
            try:
                self._state_dir = Path(getattr(store, "_path", None)).parent if getattr(store, "_path", None) else state_dir
            except Exception:
                self._state_dir = state_dir
            self._store_path: Path | None = getattr(store, "_path", None)
        else:
            self._store = None
            self._state_dir = state_dir
            self._store_path = (state_dir / "tickets.json") if state_dir is not None else None
            if self._store_path is None:
                try:
                    from codebot.state_manager import get_paths
                    self._state_dir = get_paths().state_dir
                    self._store_path = self._state_dir / "tickets.json"
                except Exception:
                    self._store_path = None

    def _ensure_store(self) -> TicketStore | None:
        if self._store is not None:
            return self._store
        if self._state_dir is not None:
            try:
                from codebot.ticket_dispatcher import get_ticket_store
                s = get_ticket_store(self._state_dir)
                if s is not None:
                    self._store = s
                    self._store_path = getattr(s, "_path", None)
                    return s
            except Exception:
                pass
        if self._store_path is not None:
            try:
                from codebot.ticket_dispatcher import get_ticket_store
                parent = self._store_path.parent
                s = get_ticket_store(parent)
                if s is not None:
                    self._store = s
                    self._state_dir = parent
                    return s
            except Exception:
                pass
        return None

    def clear_cache(self) -> None:
        sdir = getattr(self, "_state_dir", None)
        if sdir is not None:
            try:
                from codebot.ticket_dispatcher import _canonical_state_dir, _ticket_store_cache, _ticket_store_cache_lock, _ticket_store_fingerprints
            except Exception:
                self._store = None
                return
            try:
                canonical = _canonical_state_dir(sdir)
            except Exception:
                canonical = None
            if canonical is not None:
                with _ticket_store_cache_lock:
                    cached = _ticket_store_cache.get(canonical)
                    if cached is not None:
                        try:
                            store_path = canonical / "tickets.json"
                            if not store_path.exists():
                                _ticket_store_cache.pop(canonical, None)
                                _ticket_store_fingerprints.pop(canonical, None)
                                try:
                                    cached.close()
                                except Exception:
                                    pass
                            else:
                                st = store_path.stat()
                                fp = (float(st.st_mtime), int(st.st_size))
                                cur = _ticket_store_fingerprints.get(canonical)
                                if cur is not None and cur != fp:
                                    _ticket_store_cache.pop(canonical, None)
                                    _ticket_store_fingerprints.pop(canonical, None)
                                    try:
                                        cached.close()
                                    except Exception:
                                        pass
                        except Exception:
                            pass
        else:
            try:
                from codebot.ticket_dispatcher import clear_ticket_store_cache
                clear_ticket_store_cache()
            except Exception:
                pass
        self._store = None

    def actionable_queue_depth(self) -> int:
        store = self._ensure_store()
        if store is None:
            return 0
        try:
            with store._lock:
                decomp_count = len(store._state_index.get(TicketState.DECOMP, set()))
                planning_count = len(store._state_index.get(TicketState.PLANNING, set()))
                implement_count = len(store._state_index.get(TicketState.IMPLEMENT, set()))
                rework_count = len(store._state_index.get(TicketState.REWORK, set()))
                return decomp_count + planning_count + implement_count + rework_count
        except Exception:
            return 0

    def ticket_classes(self) -> list[str]:
        store = self._ensure_store()
        if store is None:
            return []
        try:
            with store._lock:
                impl_ids = store._state_index.get(TicketState.IMPLEMENT, set())
                tickets = [store._tickets[tid] for tid in impl_ids if tid in store._tickets]
            classes = []
            for t in tickets:
                tc = getattr(t, "ticket_class", None)
                if tc is not None:
                    classes.append(tc.value if hasattr(tc, "value") else str(tc))
                else:
                    classes.append("feature")
            return classes
        except Exception:
            return []

    def summary(self) -> dict[str, int]:
        store = self._ensure_store()
        if store is None:
            return {}
        try:
            return store.summary()
        except Exception:
            return {}

    def get_store(self) -> TicketStore | None:
        return self._ensure_store()

    def as_readonly(self) -> ReadOnlyTicketView | None:
        s = self._ensure_store()
        if s is None:
            return None
        return ReadOnlyTicketView(s)

    @classmethod
    def from_state_dir(cls, state_dir: Path) -> "QueueManager":
        qm = cls(None, state_dir=Path(state_dir))
        qm._store_path = Path(state_dir) / "tickets.json"
        return qm
