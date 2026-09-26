"""Repair patch schema, proposal records, and platform-owned applier.

The agent returns a ContextRepairPatch with ONLY proposed changes.
The platform validates it against the frozen authorization envelope
stored in the RepairAttempt claim before applying via TicketStore.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from enum import Enum
from pathlib import Path
from typing import Any


class RepairApplyResult(str, Enum):
    APPLIED = "APPLIED"
    INVALID_PATCH = "INVALID_PATCH"
    STALE_CONTEXT = "STALE_CONTEXT"


@dataclass(frozen=True)
class ContextRepairPatch:
    ticket_id: str
    repair_execution_id: str
    fields_to_update: dict[str, Any]
    evidence: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ContextRepairPatch:
        return cls(
            ticket_id=d["ticket_id"],
            repair_execution_id=d["repair_execution_id"],
            fields_to_update=d.get("fields_to_update", {}),
            evidence=d.get("evidence", ""),
        )


@dataclass(frozen=True)
class RepairProposalRecord:
    repair_execution_id: str
    ticket_id: str
    fields_to_update: dict[str, Any]
    created_at: float

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> RepairProposalRecord:
        return cls(**d)

    def save(self, state_dir: Path) -> None:
        proposals_dir = Path(state_dir) / "repair_proposals"
        proposals_dir.mkdir(parents=True, exist_ok=True)
        target = proposals_dir / f"{self.repair_execution_id}.json"
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
    def load(cls, state_dir: Path, execution_id: str) -> RepairProposalRecord | None:
        path = Path(state_dir) / "repair_proposals" / f"{execution_id}.json"
        if not path.exists():
            return None
        try:
            d = json.loads(path.read_text(encoding="utf-8"))
            return cls.from_dict(d)
        except (json.JSONDecodeError, KeyError, OSError):
            return None

    @classmethod
    def delete(cls, state_dir: Path, execution_id: str) -> None:
        path = Path(state_dir) / "repair_proposals" / f"{execution_id}.json"
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def apply_context_repair(
    store: Any,
    patch: ContextRepairPatch,
    claim: Any,
) -> RepairApplyResult:
    if not patch.fields_to_update:
        return RepairApplyResult.INVALID_PATCH

    allowed = set(claim.allowed_fields)
    if not set(patch.fields_to_update.keys()) <= allowed:
        return RepairApplyResult.INVALID_PATCH

    baseline_keys = set(claim.expected_field_values.keys())
    if not set(patch.fields_to_update.keys()) <= baseline_keys:
        return RepairApplyResult.INVALID_PATCH

    is_noop = all(
        patch.fields_to_update[k] == claim.expected_field_values.get(k)
        for k in patch.fields_to_update
    )
    if is_noop:
        return RepairApplyResult.INVALID_PATCH

    result = store.update_context_fields(
        ticket_id=patch.ticket_id,
        expected_state=claim.source_state,
        expected_transition_version=claim.expected_transition_version,
        expected_field_values=claim.expected_field_values,
        fields_to_update=patch.fields_to_update,
    )
    if result:
        return RepairApplyResult.APPLIED
    current = store.get(patch.ticket_id)
    if current is None:
        return RepairApplyResult.INVALID_PATCH
    version_match = current.transition_version == claim.expected_transition_version
    fields_match = all(
        getattr(current, k, None) == v
        for k, v in claim.expected_field_values.items()
    )
    if not version_match or not fields_match:
        return RepairApplyResult.STALE_CONTEXT
    return RepairApplyResult.INVALID_PATCH
