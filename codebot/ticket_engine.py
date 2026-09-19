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
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "2.0"


class TicketState(str, Enum):
    DISCOVERED = "DISCOVERED"
    VALIDATING = "VALIDATING"
    TRIAGED = "TRIAGED"
    READY = "READY"
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
        TicketState.PLANNING,
        TicketState.IMPLEMENTING,
        TicketState.DEFERRED,
    }),
    TicketState.PLANNING: frozenset({
        TicketState.IMPLEMENTING,
        TicketState.READY,
        TicketState.BLOCKED,
    }),
    TicketState.IMPLEMENTING: frozenset({
        TicketState.REVIEWING,
        TicketState.REWORK,
        TicketState.BLOCKED,
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
        TicketState.PLANNING,
        TicketState.REJECTED,
        TicketState.DEFERRED,
    }),
    TicketState.BLOCKED: frozenset({
        TicketState.READY,
        TicketState.PLANNING,
        TicketState.IMPLEMENTING,
        TicketState.DEFERRED,
    }),
    TicketState.DEFERRED: frozenset({
        TicketState.READY,
        TicketState.TRIAGED,
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
        if (self.state == TicketState.READY and
            new_state == TicketState.IMPLEMENTING and
            _RISK_ORDER.get(self.risk.value, 0) >= _RISK_ORDER.get(MIN_RISK_FOR_PLANNING.value, 1)):
            raise ValueError(
                f"ticket {self.id} risk={self.risk.value} requires PLANNING before IMPLEMENTING"
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


_ticket_counter = 0


def generate_ticket_id(prefix: str = "CB") -> str:
    global _ticket_counter
    _ticket_counter += 1
    ts = int(time.time() * 1000) % 10_000_000
    rand = hashlib.sha256(f"{time.time_ns()}-{_ticket_counter}".encode()).hexdigest()[:4].upper()
    return f"{prefix}-{ts}-{rand}"


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

    def __init__(self, path: Path) -> None:
        import threading
        self._path = path
        self._lock = threading.RLock()
        self._tickets: dict[str, Ticket] = {}
        self._evidence_index: dict[str, str] = {}
        # Word inverted index: word -> set of ticket IDs whose title contains it
        self._word_index: dict[str, set[str]] = {}
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            for entry in data.get("tickets", []):
                t = Ticket.from_dict(entry)
                self._tickets[t.id] = t
                self._evidence_index[t.evidence_hash()] = t.id
                self._index_title(t)
        except (json.JSONDecodeError, KeyError, ValueError):
            self._tickets = {}
            self._evidence_index = {}
            self._word_index = {}

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

    def _save(self) -> None:
        payload = {
            "schema_version": SCHEMA_VERSION,
            "updated_at": time.time(),
            "tickets": [t.to_dict() for t in self._tickets.values()],
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)

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
            self._save()
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

    def _has_gate_approval(self, ticket_id: str) -> bool:
        """Check if the latest gate result for ticket_id indicates approval.

        Reads gate_results.jsonl from the state directory and looks for the
        most recent entry for ticket_id.  Returns True only if that entry
        exists *and* ``passed`` is True.
        """
        gate_path = self._path.parent / "gate_results.jsonl"
        if not gate_path.exists():
            return False
        try:
            last_record: dict[str, Any] | None = None
            with open(gate_path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        record = json.loads(line)
                        if record.get("ticket_id") == ticket_id:
                            last_record = record
                    except json.JSONDecodeError:
                        continue
            return last_record is not None and last_record.get("passed") is True
        except OSError:
            return False

    def transition(self, ticket_id: str, new_state: TicketState, reviewer_feedback: list[dict] | None = None) -> Ticket:
        with self._lock:
            ticket = self._tickets.get(ticket_id)
            if ticket is None:
                raise KeyError(f"ticket not found: {ticket_id}")
            # Enforce planning prerequisite for medium+ risk tickets
            if (ticket.state == TicketState.READY and
                new_state == TicketState.IMPLEMENTING and
                ticket.risk.value in (RiskLevel.MEDIUM.value, RiskLevel.HIGH.value, RiskLevel.CRITICAL.value)):
                if not self._has_plan(ticket_id):
                    raise ValueError(
                        f"ticket {ticket_id} has risk={ticket.risk.value} which requires "
                        f"an implementation plan before transitioning to IMPLEMENTING. "
                        f"Move to PLANNING state first or generate a plan."
                    )
            # Enforce gatekeeper approval before VERIFYING -> COMPLETE (GAP-2)
            if (ticket.state == TicketState.VERIFYING and
                new_state == TicketState.COMPLETE):
                if not self._has_gate_approval(ticket_id):
                    raise ValueError(
                        f"ticket {ticket_id} cannot transition to COMPLETE: "
                        f"gatekeeper approval required but not found"
                    )
            updated = ticket.transition(new_state, reviewer_feedback)
            self._tickets[ticket_id] = updated
            self._save()
            return updated

    def list_by_state(self, state: TicketState) -> list[Ticket]:
        with self._lock:
            return [t for t in self._tickets.values() if t.state == state]

    def list_ready(self) -> list[Ticket]:
        with self._lock:
            ready = [t for t in self._tickets.values() if t.state == TicketState.READY]
        severity_order = {
            Severity.CRITICAL: 0,
            Severity.HIGH: 1,
            Severity.MEDIUM: 2,
            Severity.LOW: 3,
        }
        return sorted(ready, key=lambda t: severity_order.get(t.severity, 99))

    def count(self) -> int:
        with self._lock:
            return len(self._tickets)

    def summary(self) -> dict[str, int]:
        with self._lock:
            counts: dict[str, int] = {}
            for t in self._tickets.values():
                counts[t.state.value] = counts.get(t.state.value, 0) + 1
            return counts
