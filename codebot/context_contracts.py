"""Context contracts for ticket lifecycle transitions.

Every legal state transition has an explicit context contract specifying
which fields must be present on the ticket before the transition may proceed.
Contracts are keyed by (source_state, destination_state) pairs.

Core invariant: NO CONTEXT, NO ADVANCE. NO CONTRACT, NO ADVANCE.

This module provides transition completeness guarantees — that required fields
exist on the ticket before advancement. It does NOT yet guarantee provenance
(that the producing stage created them during its execution). Provenance
requires lifecycle_context namespaces and is deferred to a future step.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------

class MissingTransitionContract(ValueError):
    """Raised when a legal state transition has no defined context contract.

    Subclasses ValueError so batch_transition's existing
    except (ValueError, KeyError) rollback catches it without modification.
    """


class ContextContractViolation(ValueError):
    """Raised when a transition's context contract is not satisfied.

    Subclasses ValueError so existing except (ValueError, KeyError) handlers
    in callers (including batch_transition rollback) catch it without modification.
    """

    def __init__(
        self,
        ticket_id: str,
        source_state: str,
        dest_state: str,
        missing_fields: list[str],
        failed_invariants: list[str],
    ) -> None:
        self.ticket_id = ticket_id
        self.source_state = source_state
        self.dest_state = dest_state
        self.missing_fields = missing_fields
        self.failed_invariants = failed_invariants
        parts: list[str] = []
        if missing_fields:
            parts.append(f"missing {missing_fields}")
        if failed_invariants:
            parts.append(f"invariant violations {failed_invariants}")
        detail = "; ".join(parts) if parts else "unknown"
        super().__init__(
            f"Context gate blocked {source_state}->{dest_state} "
            f"for {ticket_id}: {detail}"
        )


# ---------------------------------------------------------------------------
# Authoritative Contract Registry
# ---------------------------------------------------------------------------
# Built from the actual TRANSITIONS dict at ticket_engine.py:200-301.
# Every non-exit transition has an explicit entry. Universal exits
# (RESOLVED, SUPERSEDED, CANCELLED) are covered by wildcard entries.
#
# Actual TRANSITIONS topology (verified by reading source):
#   DISCOVERED → TRIAGED, REJECTED, DUPLICATE + exits
#   REQUESTED → COMPLETE, REJECTED, DEFERRED
#   TRIAGED → DECOMP, LATER, NEVER + exits
#   GOAL → DECOMP, LATER, NEVER + exits
#   DECOMP → PLANNING, BLOCKED + exits
#   PLANNING → IMPLEMENT, BLOCKED + exits
#   IMPLEMENT → REVIEW, REWORK + exits
#   REVIEW → VERIFY, REWORK, BLOCKED + exits
#   VERIFY → COMPLETE, REWORK, BLOCKED + exits
#   REWORK → IMPLEMENT + exits
#   LATER → GOAL + exits
#   DEFERRED → DECOMP, IMPLEMENT, BLOCKED, REQUESTED + exits
#   BLOCKED → DECOMP, PLANNING, DEFERRED
#   Terminal sinks (no outbound): COMPLETE, REJECTED, DUPLICATE,
#       NOT_ACTIONABLE, RESOLVED, SUPERSEDED, CANCELLED, NEVER

CONTRACTS: dict[tuple[str, str], tuple[str, ...]] = {
    # === WILDCARD EXITS (cover all source states × 3 terminal destinations) ===
    ("*", "RESOLVED"): (),
    ("*", "SUPERSEDED"): ("superseded_by",),
    ("*", "CANCELLED"): (),

    # === DISCOVERY PHASE ===
    ("DISCOVERED", "TRIAGED"): (
        "evidence",
        "problem_statement",
    ),
    ("DISCOVERED", "REJECTED"): (
        "evidence",
    ),
    ("DISCOVERED", "DUPLICATE"): (
        "evidence",
        "fingerprint",
    ),

    # === REQUEST LIFECYCLE ===
    # TEMPORARY STRUCTURAL CONTRACTS: origin_id alone does not distinguish
    # the quality of outcome. Placeholders until ticket-context-carrier plan.
    ("REQUESTED", "COMPLETE"): (
        "origin_id",
    ),
    ("REQUESTED", "REJECTED"): (
        "origin_id",
    ),
    ("REQUESTED", "DEFERRED"): (
        "origin_id",
    ),

    # === TRIAGE PHASE ===
    # Actual code: TRIAGED → DECOMP, LATER, NEVER (not GOAL as originally planned)
    ("TRIAGED", "DECOMP"): (
        "problem_statement",
        "ticket_class",
        "severity",
    ),
    ("TRIAGED", "LATER"): (
        "problem_statement",
        "goal_reason",
    ),
    ("TRIAGED", "NEVER"): (
        "problem_statement",
        "goal_reason",
    ),

    # === GOAL ALIGNMENT ===
    ("GOAL", "DECOMP"): (
        "goal_disposition",
        "problem_statement",
    ),
    ("GOAL", "LATER"): (
        "goal_disposition",
        "goal_reason",
    ),
    ("GOAL", "NEVER"): (
        "goal_disposition",
        "goal_reason",
    ),

    # === DECOMPOSITION ===
    ("DECOMP", "PLANNING"): (
        "affected_modules",
        "problem_statement",
        "acceptance_criteria",
    ),
    ("DECOMP", "BLOCKED"): (
        "problem_statement",
    ),

    # === PLANNING ===
    ("PLANNING", "IMPLEMENT"): (
        "desired_state",
        "acceptance_criteria",
        "affected_modules",
    ),
    ("PLANNING", "BLOCKED"): (
        "problem_statement",
    ),

    # === IMPLEMENTATION ===
    # TEMPORARY STRUCTURAL CONTRACT: attempts > 0 proves an implementation
    # attempt was made but does not prove reviewable implementation evidence.
    # Will be upgraded when lifecycle_context provides provenance-tracked fields.
    ("IMPLEMENT", "REVIEW"): (
        "attempts",
    ),
    ("IMPLEMENT", "REWORK"): (
        "reviewer_feedback",
    ),

    # === REVIEW ===
    # Actual code: REVIEW → VERIFY (not COMPLETE directly)
    ("REVIEW", "VERIFY"): (
        "reviewer_feedback",
    ),
    ("REVIEW", "REWORK"): (
        "reviewer_feedback",
    ),
    ("REVIEW", "BLOCKED"): (
        "reviewer_feedback",
    ),

    # === VERIFY ===
    # Actual code: VERIFY state exists with COMPLETE, REWORK, BLOCKED
    ("VERIFY", "COMPLETE"): (
        "commit_sha",
    ),
    ("VERIFY", "REWORK"): (
        "reviewer_feedback",
    ),
    ("VERIFY", "BLOCKED"): (
        "problem_statement",
    ),

    # === REWORK ===
    # Actual code: REWORK → IMPLEMENT only (not PLANNING, DECOMP, DEFERRED)
    ("REWORK", "IMPLEMENT"): (
        "reviewer_feedback",
        "rework_count",
    ),

    # === LATER ===
    ("LATER", "GOAL"): (
        "goal_disposition",
    ),

    # === DEFERRED ===
    ("DEFERRED", "REQUESTED"): (
        "origin_id",
    ),
    ("DEFERRED", "DECOMP"): (
        "problem_statement",
    ),
    ("DEFERRED", "IMPLEMENT"): (
        "desired_state",
        "acceptance_criteria",
    ),
    ("DEFERRED", "BLOCKED"): (
        "problem_statement",
    ),

    # === BLOCKED ===
    ("BLOCKED", "DECOMP"): (
        "problem_statement",
    ),
    ("BLOCKED", "PLANNING"): (
        "desired_state",
        "acceptance_criteria",
    ),
    ("BLOCKED", "DEFERRED"): (
        "origin_id",
    ),
}


# ---------------------------------------------------------------------------
# Contract Resolution (Fail-Closed)
# ---------------------------------------------------------------------------

def resolve_contract(source_state: str, dest_state: str) -> tuple[str, ...]:
    """Resolve the context contract for a transition. Fail-closed.

    Lookup order:
    1. Exact match: (source_state, dest_state)
    2. Wildcard match: ("*", dest_state)
    3. Raise MissingTransitionContract

    This ensures adding a new lifecycle transition forces the developer
    to consciously declare its context contract. No silent pass-through.
    """
    exact = CONTRACTS.get((source_state, dest_state))
    if exact is not None:
        return exact

    wildcard = CONTRACTS.get(("*", dest_state))
    if wildcard is not None:
        return wildcard

    raise MissingTransitionContract(
        f"No context contract defined for {source_state}->{dest_state}. "
        f"Add an entry to CONTRACTS in codebot/context_contracts.py."
    )


# ---------------------------------------------------------------------------
# Field Presence Checking
# ---------------------------------------------------------------------------

def field_present(name: str, value: Any) -> bool:
    """Deterministic field presence check.

    Rules:
    - None → always missing
    - str → non-empty after strip
    - list/tuple/dict/set → len > 0
    - int/float → != 0
    - other types → True (present if exists)
    """
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, tuple, dict, set)):
        return bool(value)
    if isinstance(value, (int, float)):
        return value != 0
    return True


# Fields requiring semantic validation beyond structural presence.
# Extend this dict as contracts mature. Keys are field names,
# values are callables returning bool.
SPECIAL_VALIDATORS: dict[str, Callable[[Any], bool]] = {
    "rework_count": lambda x: isinstance(x, int) and x > 0,
    "attempts": lambda x: isinstance(x, int) and x > 0,
}


# Transition-specific value validators. These enforce deterministic
# invariants where mere field presence is insufficient. For example,
# GOAL→NEVER must not pass if goal_disposition="NOW".
# Keys are (source_state, dest_state) tuples, values are callables
# taking the ticket and returning bool (True = invariant satisfied).
TRANSITION_VALIDATORS: dict[tuple[str, str], Callable[[Any], bool]] = {
    ("GOAL", "DECOMP"): lambda t: getattr(t, "goal_disposition", "") == "NOW",
    ("GOAL", "LATER"): lambda t: getattr(t, "goal_disposition", "") == "LATER",
    ("GOAL", "NEVER"): lambda t: getattr(t, "goal_disposition", "") == "NEVER",
    ("TRIAGED", "LATER"): lambda t: getattr(t, "goal_disposition", "") in ("LATER", ""),
    ("TRIAGED", "NEVER"): lambda t: getattr(t, "goal_disposition", "") in ("NEVER", ""),
}


def check_transition_contract(
    ticket: Any,
    source_state: str,
    dest_state: str,
) -> tuple[bool, list[str], list[str]]:
    """Check if a ticket satisfies the context contract for a transition.

    Returns:
        (ok, missing_fields, failed_invariants)
        - missing_fields: fields that are absent or empty
        - failed_invariants: transition-specific value checks that failed
    """
    required = resolve_contract(source_state, dest_state)
    missing: list[str] = []
    for field_name in required:
        value = getattr(ticket, field_name, None)
        if field_name in SPECIAL_VALIDATORS:
            if not SPECIAL_VALIDATORS[field_name](value):
                missing.append(field_name)
        elif not field_present(field_name, value):
            missing.append(field_name)

    failed_invariants: list[str] = []
    validator = TRANSITION_VALIDATORS.get((source_state, dest_state))
    if validator is not None and not validator(ticket):
        failed_invariants.append(f"{source_state}->{dest_state}")

    ok = len(missing) == 0 and len(failed_invariants) == 0
    return ok, missing, failed_invariants


# ---------------------------------------------------------------------------
# Producer Role Resolution (Total)
# ---------------------------------------------------------------------------

SOURCE_PRODUCERS: dict[str, str] = {
    "DISCOVERED": "discovery",
    "REQUESTED": "user_agent",
    "TRIAGED": "triage",
    "GOAL": "goal_aligner",
    "DECOMP": "decomposer",
    "PLANNING": "planner",
    "IMPLEMENT": "implementer",
    "REVIEW": "reviewer",
    "VERIFY": "platform",
    "REWORK": "rework",
    "LATER": "platform",
    "DEFERRED": "platform",
    "BLOCKED": "platform",
}


def get_producer_role(source_state: str, dest_state: str) -> str:
    """Return the role responsible for producing context for this transition.

    Source-state based. Falls back to 'platform' for unknown source states
    rather than raising, ensuring the error-reporting path never breaks.
    """
    return SOURCE_PRODUCERS.get(source_state, "platform")


# ---------------------------------------------------------------------------
# Failure Artifact Schema
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ContextGateFailure:
    """Durable artifact recording a context gate failure.

    Keyed by (ticket_id, source_state, destination_state) to prevent
    attempt counts from bleeding across different transition attempts
    on the same ticket.
    """

    ticket_id: str
    source_state: str
    destination_state: str
    missing_fields: tuple[str, ...]
    failed_invariants: tuple[str, ...]
    attempt_count: int
    created_at: float
    producer_role: str
    repair_instructions: str
    repairable: bool
    repair_exhausted: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticket_id": self.ticket_id,
            "source_state": self.source_state,
            "destination_state": self.destination_state,
            "missing_fields": list(self.missing_fields),
            "failed_invariants": list(self.failed_invariants),
            "attempt_count": self.attempt_count,
            "created_at": self.created_at,
            "producer_role": self.producer_role,
            "repair_instructions": self.repair_instructions,
            "repairable": self.repairable,
            "repair_exhausted": self.repair_exhausted,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextGateFailure:
        return cls(
            ticket_id=d["ticket_id"],
            source_state=d["source_state"],
            destination_state=d["destination_state"],
            missing_fields=tuple(d.get("missing_fields", [])),
            failed_invariants=tuple(d.get("failed_invariants", [])),
            attempt_count=d["attempt_count"],
            created_at=d["created_at"],
            producer_role=d["producer_role"],
            repair_instructions=d["repair_instructions"],
            repairable=d["repairable"],
            repair_exhausted=d["repair_exhausted"],
        )

    def _artifact_path(self, state_dir: Any) -> Any:
        """Return the Path for this failure's artifact file."""
        from pathlib import Path

        gate_dir = Path(state_dir) / "gate_failures"
        filename = f"{self.ticket_id}__{self.source_state}__{self.destination_state}.json"
        return gate_dir / filename

    def save(self, state_dir: Any) -> None:
        """Persist artifact atomically (write tmp then rename)."""
        import json
        import os
        from pathlib import Path

        target = self._artifact_path(state_dir)
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_suffix(".tmp")
        content = json.dumps(self.to_dict(), separators=(",", ":"))
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        tmp.replace(target)

    @classmethod
    def load(
        cls,
        state_dir: Any,
        ticket_id: str,
        source_state: str,
        dest_state: str,
    ) -> ContextGateFailure | None:
        """Load an existing failure artifact, or None if not found."""
        import json
        from pathlib import Path

        gate_dir = Path(state_dir) / "gate_failures"
        filename = f"{ticket_id}__{source_state}__{dest_state}.json"
        path = gate_dir / filename
        if not path.exists():
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    @classmethod
    def delete(
        cls,
        state_dir: Any,
        ticket_id: str,
        source_state: str,
        dest_state: str,
    ) -> None:
        """Delete a specific transition's failure artifact."""
        from pathlib import Path

        gate_dir = Path(state_dir) / "gate_failures"
        filename = f"{ticket_id}__{source_state}__{dest_state}.json"
        path = gate_dir / filename
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass

    @classmethod
    def delete_all_for_ticket(cls, state_dir: Any, ticket_id: str) -> None:
        """Delete all failure artifacts for a given ticket."""
        from pathlib import Path

        gate_dir = Path(state_dir) / "gate_failures"
        if not gate_dir.exists():
            return
        prefix = f"{ticket_id}__"
        for path in gate_dir.iterdir():
            if path.name.startswith(prefix) and path.suffix == ".json":
                try:
                    path.unlink()
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Gate Failure Handling
# ---------------------------------------------------------------------------

def handle_gate_failure(
    ticket_id: str,
    source_state: str,
    dest_state: str,
    missing_fields: list[str],
    failed_invariants: list[str],
    state_dir: Any,
    producer_role: str,
) -> None:
    """Record a gate failure artifact and raise ContextContractViolation.

    Attempt count saturates at 2 via min(prev + 1, 2).
    After 2 failures on the same transition, repair_exhausted=True.
    This function does NOT force REWORK or change ticket state.
    Enforcement and recovery are separate authorities.
    """
    existing = ContextGateFailure.load(state_dir, ticket_id, source_state, dest_state)

    if existing is not None:
        attempt_count = min(existing.attempt_count + 1, 2)
    else:
        attempt_count = 1

    repairable = attempt_count == 1
    repair_exhausted = attempt_count >= 2

    # Build repair instructions from both missing fields and failed invariants
    parts: list[str] = []
    if missing_fields:
        parts.append(f"Missing required fields: {', '.join(missing_fields)}")
    if failed_invariants:
        parts.append(
            f"Transition invariant violations: {', '.join(failed_invariants)}. "
            f"Field values contradict the requested transition."
        )
    parts.append(f"Producer role: {producer_role}")
    if repair_exhausted:
        parts.append("Repair attempts exhausted. Awaiting recovery intervention.")
    else:
        parts.append("Repair this context and retry the transition.")
    repair_instructions = " | ".join(parts)

    failure = ContextGateFailure(
        ticket_id=ticket_id,
        source_state=source_state,
        destination_state=dest_state,
        missing_fields=tuple(missing_fields),
        failed_invariants=tuple(failed_invariants),
        attempt_count=attempt_count,
        created_at=time.time(),
        producer_role=producer_role,
        repair_instructions=repair_instructions,
        repairable=repairable,
        repair_exhausted=repair_exhausted,
    )
    failure.save(state_dir)

    raise ContextContractViolation(
        ticket_id=ticket_id,
        source_state=source_state,
        dest_state=dest_state,
        missing_fields=missing_fields,
        failed_invariants=failed_invariants,
    )


def cleanup_gate_failure(
    state_dir: Any,
    ticket_id: str,
    source_state: str,
    dest_state: str,
) -> None:
    """Delete the failure artifact for a specific transition after successful commit."""
    ContextGateFailure.delete(state_dir, ticket_id, source_state, dest_state)
