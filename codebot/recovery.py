"""Recovery escalation primitives and artifact retirement for Step 2 context repair.

All escalations are blocking: they suppress both normal dispatch AND repair
dispatch for the affected ticket. Escalation retirement uses correlation fields
to ensure only the owning repair cycle can delete its escalation.
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

ESCALATION_REASONS: frozenset[str] = frozenset({
    "REPAIR_EXHAUSTED",
    "REPAIR_PATCH_REJECTED",
    "REPAIR_EXECUTION_TIMEOUT",
    "CONTEXT_CHANGED_DURING_REPAIR",
    "MULTIPLE_ACTIVE_REPAIR_TARGETS",
    "UNSUPPORTED_INVARIANT_REPAIR",
    "GATE_GENERATION_CHANGED",
    "CRASH_RECONCILIATION_AMBIGUOUS",
    "TICKET_SUPERSEDED_DURING_REPAIR",
})


@dataclass(frozen=True)
class RecoveryEscalation:
    ticket_id: str
    source_state: str
    dest_state: str
    attempt_count: int
    created_at: float
    reason: str
    gate_attempt_number: int | None = None
    repair_execution_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RecoveryEscalation:
        return cls(
            ticket_id=d["ticket_id"],
            source_state=d["source_state"],
            dest_state=d["dest_state"],
            attempt_count=d["attempt_count"],
            created_at=d["created_at"],
            reason=d["reason"],
            gate_attempt_number=d.get("gate_attempt_number"),
            repair_execution_id=d.get("repair_execution_id"),
        )

    def save(self, state_dir: Path) -> None:
        esc_dir = Path(state_dir) / "recovery_escalations"
        esc_dir.mkdir(parents=True, exist_ok=True)
        target = esc_dir / f"{self.ticket_id}.json"
        tmp = target.with_suffix(".tmp")
        content = json.dumps(self.to_dict(), separators=(",", ":"))
        fd = os.open(str(tmp), os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
        try:
            os.write(fd, content.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        tmp.replace(target)

    @classmethod
    def load(cls, state_dir: Path, ticket_id: str) -> RecoveryEscalation | None:
        path = Path(state_dir) / "recovery_escalations" / f"{ticket_id}.json"
        if not path.exists():
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(d)
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    @classmethod
    def delete(cls, state_dir: Path, ticket_id: str) -> None:
        path = Path(state_dir) / "recovery_escalations" / f"{ticket_id}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def has_blocking_escalation(state_dir: Path, ticket_id: str) -> bool:
    path = Path(state_dir) / "recovery_escalations" / f"{ticket_id}.json"
    return path.exists()


def handle_exhausted_ticket(
    store: Any,
    state_dir: Path,
    failure: Any,
) -> None:
    existing = RecoveryEscalation.load(state_dir, failure.ticket_id)
    if existing is not None:
        return
    esc = RecoveryEscalation(
        ticket_id=failure.ticket_id,
        source_state=failure.source_state,
        dest_state=failure.destination_state,
        attempt_count=failure.attempt_count,
        created_at=time.time(),
        reason="REPAIR_EXHAUSTED",
        gate_attempt_number=failure.attempt_count,
        repair_execution_id=None,
    )
    esc.save(state_dir)


def persist_escalation(
    state_dir: Path,
    ticket_id: str,
    source: str,
    dest: str,
    attempt_count: int,
    reason: str,
    gate_attempt_number: int | None = None,
    repair_execution_id: str | None = None,
) -> None:
    if reason not in ESCALATION_REASONS:
        raise ValueError(f"Unknown escalation reason: {reason}")
    esc = RecoveryEscalation(
        ticket_id=ticket_id,
        source_state=source,
        dest_state=dest,
        attempt_count=attempt_count,
        created_at=time.time(),
        reason=reason,
        gate_attempt_number=gate_attempt_number,
        repair_execution_id=repair_execution_id,
    )
    esc.save(state_dir)


def retire_original_failure(
    state_dir: Path,
    ticket_id: str,
    source_state: str,
    dest_state: str,
) -> None:
    from codebot.context_contracts import ContextGateFailure
    ContextGateFailure.delete(state_dir, ticket_id, source_state, dest_state)


def retire_repair_state(
    state_dir: Path,
    ticket_id: str,
    source_state: str,
    dest_state: str,
    gate_attempt: int,
    repair_execution_id: str,
) -> None:
    from codebot.context_contracts import ContextGateFailure
    from codebot.repair_applier import RepairProposalRecord

    ContextGateFailure.delete(state_dir, ticket_id, source_state, dest_state)

    esc = RecoveryEscalation.load(state_dir, ticket_id)
    if esc is not None:
        owns = (
            esc.gate_attempt_number == gate_attempt
            and esc.repair_execution_id == repair_execution_id
        )
        if owns:
            RecoveryEscalation.delete(state_dir, ticket_id)

    RepairProposalRecord.delete(state_dir, repair_execution_id)
