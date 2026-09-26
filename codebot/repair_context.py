"""Repair context building and scanning for Step 2 context repair.

Parses Step 1 ContextGateFailure artifacts, derives allowed repair fields,
builds scoped repair context packages, and scans for actionable failures.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from codebot.context_contracts import (
    ContextGateFailure,
    get_producer_role,
)

INVARIANT_MARKER_PREFIX = "__transition_invariant__:"

INVARIANT_REPAIR_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    ("GOAL", "DECOMP"): frozenset({"goal_disposition"}),
    ("GOAL", "LATER"): frozenset({"goal_disposition"}),
    ("GOAL", "NEVER"): frozenset({"goal_disposition"}),
}


def parse_step1_artifact(
    failure: ContextGateFailure,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    actual_missing: list[str] = []
    failed_invariants: list[str] = []
    for field in failure.missing_fields:
        if field.startswith(INVARIANT_MARKER_PREFIX):
            transition_pair = field[len(INVARIANT_MARKER_PREFIX):]
            failed_invariants.append(transition_pair)
        else:
            actual_missing.append(field)
    return tuple(actual_missing), tuple(failed_invariants)


def derive_allowed_fields(failure: ContextGateFailure) -> frozenset[str] | None:
    actual_missing, failed_invariants = parse_step1_artifact(failure)
    allowed: set[str] = set(actual_missing)
    for inv in failed_invariants:
        parts = inv.split("->")
        if len(parts) == 2:
            pair = (parts[0], parts[1])
        else:
            return None
        if pair not in INVARIANT_REPAIR_FIELDS:
            return None
        allowed |= INVARIANT_REPAIR_FIELDS[pair]
    return frozenset(allowed)


@dataclass(frozen=True)
class RepairContextPackage:
    ticket_id: str
    source_state: str
    dest_state: str
    missing_fields: tuple[str, ...]
    failed_invariants: tuple[str, ...]
    producer_role: str
    attempt_number: int
    repair_instructions: str
    current_ticket_snapshot: dict[str, Any]
    allowed_fields: frozenset[str]
    repair_execution_id: str
    expected_field_values: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["allowed_fields"] = list(self.allowed_fields)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RepairContextPackage:
        d = dict(d)
        d["allowed_fields"] = frozenset(d.get("allowed_fields", []))
        d["missing_fields"] = tuple(d.get("missing_fields", []))
        d["failed_invariants"] = tuple(d.get("failed_invariants", []))
        return cls(**d)


def build_repair_context(
    ticket: Any,
    failure: ContextGateFailure,
    repair_execution_id: str,
    expected_field_values: dict[str, Any],
    allowed_fields: frozenset[str],
) -> RepairContextPackage:
    actual_missing, failed_invariants = parse_step1_artifact(failure)
    producer_role = get_producer_role(failure.source_state, failure.destination_state)

    parts: list[str] = []
    if actual_missing:
        parts.append(f"Add values for: {', '.join(actual_missing)}.")
    if failed_invariants:
        parts.append(
            f"Fix values to match transition requirements: "
            f"{', '.join(failed_invariants)}. "
            f"Field values contradict the requested transition."
        )
    parts.append(f"This is attempt {failure.attempt_count} of 2.")
    repair_instructions = " ".join(parts)

    snapshot: dict[str, Any] = {}
    if hasattr(ticket, "__dataclass_fields__"):
        for field_name in ticket.__dataclass_fields__:
            snapshot[field_name] = getattr(ticket, field_name, None)
    else:
        for field_name in allowed_fields:
            snapshot[field_name] = getattr(ticket, field_name, None)
        snapshot["id"] = getattr(ticket, "id", None)
        snapshot["state"] = getattr(ticket, "state", None)

    return RepairContextPackage(
        ticket_id=failure.ticket_id,
        source_state=failure.source_state,
        dest_state=failure.destination_state,
        missing_fields=actual_missing,
        failed_invariants=failed_invariants,
        producer_role=producer_role,
        attempt_number=failure.attempt_count,
        repair_instructions=repair_instructions,
        current_ticket_snapshot=snapshot,
        allowed_fields=allowed_fields,
        repair_execution_id=repair_execution_id,
        expected_field_values=expected_field_values,
    )


class RepairScanner:
    def scan(self, state_dir: Path) -> list[ContextGateFailure]:
        gate_dir = Path(state_dir) / "gate_failures"
        if not gate_dir.exists():
            return []
        results: list[ContextGateFailure] = []
        for path in gate_dir.glob("*.json"):
            parts = path.stem.split("__")
            if len(parts) != 3:
                continue
            ticket_id, source_state, dest_state = parts
            failure = ContextGateFailure.load(
                state_dir, ticket_id, source_state, dest_state
            )
            if failure is not None:
                results.append(failure)
        return results


def get_actionable_failures(
    state_dir: Path,
) -> tuple[list[ContextGateFailure], list[ContextGateFailure]]:
    scanner = RepairScanner()
    all_failures = scanner.scan(state_dir)
    repairable: list[ContextGateFailure] = []
    exhausted: list[ContextGateFailure] = []
    for f in all_failures:
        if f.repair_exhausted:
            exhausted.append(f)
        elif f.repairable:
            repairable.append(f)
    repairable.sort(key=lambda x: (-int(x.repairable), x.attempt_count))
    return repairable, exhausted


def arbitrate_ticket_repairs(
    failures: list[ContextGateFailure],
) -> ContextGateFailure | None:
    if not failures:
        return None
    if len(failures) == 1:
        return failures[0]
    max_created = max(f.created_at for f in failures)
    candidates = [f for f in failures if f.created_at == max_created]
    if len(candidates) > 1:
        return None
    return candidates[0]
