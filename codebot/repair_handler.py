"""Repair result handling, completion, crash reconciliation, and scheduler integration.

Orchestrates the repair lifecycle after a worker returns a ContextRepairPatch:
correlates execution_id to claim, validates against frozen envelope, applies
via platform applier, and retries the original transition through Step 1's gate.

Lock discipline: _locked helpers assume caller holds the sidecar lock.
Public methods acquire their own lock. Never nest claim_lock() calls.
"""
from __future__ import annotations

import logging
import time
import uuid
from pathlib import Path
from typing import Any

from codebot.context_contracts import ContextGateFailure
from codebot.repair_applier import (
    ContextRepairPatch,
    RepairApplyResult,
    RepairProposalRecord,
    apply_context_repair,
)
from codebot.repair_claims import (
    ACTIVE_REPAIR_STATUSES,
    ClaimStatusError,
    RepairAttempt,
    RepairAttemptStatus,
    _read_claim_locked,
    _update_claim_status_locked,
    claim_lock,
    find_stale_claims,
)
from codebot.recovery import (
    RecoveryEscalation,
    persist_escalation,
    retire_repair_state,
)

logger = logging.getLogger("repair_handler")

MAX_REPAIR_EXECUTIONS = 2


def _find_claim_by_execution_id(
    state_dir: Path,
    execution_id: str,
) -> tuple[Path, RepairAttempt] | None:
    claim_dir = Path(state_dir) / "repair_attempts"
    if not claim_dir.exists():
        return None
    for path in claim_dir.glob("*.json"):
        if path.name.startswith(".tmp_"):
            continue
        with claim_lock(path):
            claim = _read_claim_locked(path)
        if claim is not None and claim.repair_execution_id == execution_id:
            return path, claim
    return None


def _handle_completion_locked(
    store: Any,
    state_dir: Path,
    claim_path: Path,
    claim: RepairAttempt,
) -> bool:
    ticket = store.get(claim.ticket_id)
    if ticket is None:
        return False
    if ticket.state.value != claim.source_state:
        if ticket.state.value == claim.dest_state:
            _update_claim_status_locked(
                claim_path,
                RepairAttemptStatus.PATCH_APPLIED,
                RepairAttemptStatus.COMPLETED,
            )
            retire_repair_state(
                state_dir,
                claim.ticket_id,
                claim.source_state,
                claim.dest_state,
                claim.gate_attempt_number,
                claim.repair_execution_id,
            )
            return True
        persist_escalation(
            state_dir,
            claim.ticket_id,
            claim.source_state,
            claim.dest_state,
            claim.gate_attempt_number,
            "TICKET_SUPERSEDED_DURING_REPAIR",
            gate_attempt_number=claim.gate_attempt_number,
            repair_execution_id=claim.repair_execution_id,
        )
        return False
    try:
        store.transition(claim.ticket_id, _resolve_dest_state(claim.dest_state))
        _update_claim_status_locked(
            claim_path,
            RepairAttemptStatus.PATCH_APPLIED,
            RepairAttemptStatus.COMPLETED,
        )
        retire_repair_state(
            state_dir,
            claim.ticket_id,
            claim.source_state,
            claim.dest_state,
            claim.gate_attempt_number,
            claim.repair_execution_id,
        )
        return True
    except Exception:
        _update_claim_status_locked(
            claim_path,
            RepairAttemptStatus.PATCH_APPLIED,
            RepairAttemptStatus.FAILED,
        )
        RepairProposalRecord.delete(state_dir, claim.repair_execution_id)
        return False


def _resolve_dest_state(dest_state_str: str) -> Any:
    from codebot.ticket_engine import TicketState
    return TicketState(dest_state_str)


class RepairCompletionHandler:
    def handle_completion(
        self,
        store: Any,
        state_dir: Path,
        claim_path: Path,
        claim: RepairAttempt,
    ) -> bool:
        with claim_lock(claim_path):
            current = _read_claim_locked(claim_path)
            if current is None or current.status != RepairAttemptStatus.PATCH_APPLIED:
                return False
            return _handle_completion_locked(store, state_dir, claim_path, current)


class RepairResultHandler:
    def handle_result(
        self,
        state_dir: Path,
        store: Any,
        worker_result: dict[str, Any],
    ) -> bool:
        execution_id = worker_result.get("repair_execution_id")
        if not execution_id:
            return False

        found = _find_claim_by_execution_id(state_dir, execution_id)
        if found is None:
            return False
        claim_path, claim = found

        with claim_lock(claim_path):
            current = _read_claim_locked(claim_path)
            if current is None:
                return False

            if current.status in (
                RepairAttemptStatus.COMPLETED,
                RepairAttemptStatus.FAILED,
            ):
                return False

            if current.status != RepairAttemptStatus.RUNNING:
                return False

            if current.repair_execution_id != execution_id:
                return False

            current_failure = ContextGateFailure.load(
                state_dir,
                current.ticket_id,
                current.source_state,
                current.dest_state,
            )
            if (
                current_failure is None
                or current_failure.attempt_count != current.gate_attempt_number
            ):
                _update_claim_status_locked(
                    claim_path,
                    RepairAttemptStatus.RUNNING,
                    RepairAttemptStatus.FAILED,
                )
                persist_escalation(
                    state_dir,
                    current.ticket_id,
                    current.source_state,
                    current.dest_state,
                    current.gate_attempt_number,
                    "GATE_GENERATION_CHANGED",
                    gate_attempt_number=current.gate_attempt_number,
                    repair_execution_id=current.repair_execution_id,
                )
                return False

            try:
                patch = ContextRepairPatch.from_dict(worker_result)
            except (KeyError, TypeError):
                return self._handle_rejection(
                    claim_path, current, state_dir
                )

            result = apply_context_repair(store, patch, current)

            if result == RepairApplyResult.APPLIED:
                _update_claim_status_locked(
                    claim_path,
                    RepairAttemptStatus.RUNNING,
                    RepairAttemptStatus.PATCH_APPLIED,
                )
                # Release lock before calling completion handler
                # to prevent nested flock deadlock.

            elif result == RepairApplyResult.STALE_CONTEXT:
                _update_claim_status_locked(
                    claim_path,
                    RepairAttemptStatus.RUNNING,
                    RepairAttemptStatus.FAILED,
                )
                persist_escalation(
                    state_dir,
                    current.ticket_id,
                    current.source_state,
                    current.dest_state,
                    current.gate_attempt_number,
                    "CONTEXT_CHANGED_DURING_REPAIR",
                    gate_attempt_number=current.gate_attempt_number,
                    repair_execution_id=current.repair_execution_id,
                )
                RepairProposalRecord.delete(state_dir, execution_id)
                return False

            else:
                return self._handle_rejection(
                    claim_path, current, state_dir
                )

        # Lock released. Now invoke completion handler which acquires its own.
        if result == RepairApplyResult.APPLIED:
            handler = RepairCompletionHandler()
            return handler.handle_completion(store, state_dir, claim_path, current)
        return False

    def _handle_rejection(
        self,
        claim_path: Path,
        claim: RepairAttempt,
        state_dir: Path,
    ) -> bool:
        new_attempt = claim.repair_execution_attempt + 1
        if new_attempt < MAX_REPAIR_EXECUTIONS:
            new_id = str(uuid.uuid4())
            _update_claim_status_locked(
                claim_path,
                RepairAttemptStatus.RUNNING,
                RepairAttemptStatus.CLAIMED,
                repair_execution_attempt=new_attempt,
                repair_execution_id=new_id,
            )
        else:
            _update_claim_status_locked(
                claim_path,
                RepairAttemptStatus.RUNNING,
                RepairAttemptStatus.FAILED,
            )
            persist_escalation(
                state_dir,
                claim.ticket_id,
                claim.source_state,
                claim.dest_state,
                claim.gate_attempt_number,
                "REPAIR_PATCH_REJECTED",
                gate_attempt_number=claim.gate_attempt_number,
                repair_execution_id=claim.repair_execution_id,
            )
            RepairProposalRecord.delete(state_dir, claim.repair_execution_id)
        return False


def reconcile_stale_claims(
    state_dir: Path,
    store: Any,
    max_age_seconds: float = 300.0,
) -> None:
    stale = find_stale_claims(state_dir, max_age_seconds)

    for claim_path, claim in stale:
        with claim_lock(claim_path):
            current = _read_claim_locked(claim_path)
            if current is None:
                continue

            if current.status == RepairAttemptStatus.RUNNING:
                proposal = RepairProposalRecord.load(
                    state_dir, current.repair_execution_id
                )
                if proposal is not None:
                    ticket = store.get(current.ticket_id)
                    if ticket is None:
                        _escalate_ambiguous(state_dir, current)
                        continue
                    fields_match = all(
                        getattr(ticket, k, None) == v
                        for k, v in proposal.fields_to_update.items()
                    )
                    version_advanced = (
                        ticket.transition_version
                        == current.expected_transition_version + 1
                    )
                    if fields_match and version_advanced:
                        _update_claim_status_locked(
                            claim_path,
                            RepairAttemptStatus.RUNNING,
                            RepairAttemptStatus.PATCH_APPLIED,
                        )
                    else:
                        _try_redispatch_or_fail(
                            claim_path, current, state_dir
                        )
                else:
                    ticket = store.get(current.ticket_id)
                    if ticket is None:
                        _escalate_ambiguous(state_dir, current)
                        continue
                    baseline_match = all(
                        getattr(ticket, k, None) == v
                        for k, v in current.expected_field_values.items()
                    )
                    if baseline_match:
                        _try_redispatch_or_fail(
                            claim_path, current, state_dir
                        )
                    else:
                        _escalate_ambiguous(state_dir, current)

            elif current.status == RepairAttemptStatus.PATCH_APPLIED:
                ticket = store.get(current.ticket_id)
                if ticket is None:
                    continue
                if ticket.state.value == current.dest_state:
                    _update_claim_status_locked(
                        claim_path,
                        RepairAttemptStatus.PATCH_APPLIED,
                        RepairAttemptStatus.COMPLETED,
                    )
                    retire_repair_state(
                        state_dir,
                        current.ticket_id,
                        current.source_state,
                        current.dest_state,
                        current.gate_attempt_number,
                        current.repair_execution_id,
                    )
                elif ticket.state.value == current.source_state:
                    _handle_completion_locked(
                        store, state_dir, claim_path, current
                    )
                else:
                    persist_escalation(
                        state_dir,
                        current.ticket_id,
                        current.source_state,
                        current.dest_state,
                        current.gate_attempt_number,
                        "TICKET_SUPERSEDED_DURING_REPAIR",
                        gate_attempt_number=current.gate_attempt_number,
                        repair_execution_id=current.repair_execution_id,
                    )


def _try_redispatch_or_fail(
    claim_path: Path,
    claim: RepairAttempt,
    state_dir: Path,
) -> None:
    new_attempt = claim.repair_execution_attempt + 1
    if new_attempt < MAX_REPAIR_EXECUTIONS:
        new_id = str(uuid.uuid4())
        _update_claim_status_locked(
            claim_path,
            RepairAttemptStatus.RUNNING,
            RepairAttemptStatus.CLAIMED,
            repair_execution_attempt=new_attempt,
            repair_execution_id=new_id,
        )
    else:
        _update_claim_status_locked(
            claim_path,
            RepairAttemptStatus.RUNNING,
            RepairAttemptStatus.FAILED,
        )
        persist_escalation(
            state_dir,
            claim.ticket_id,
            claim.source_state,
            claim.dest_state,
            claim.gate_attempt_number,
            "REPAIR_EXECUTION_TIMEOUT",
            gate_attempt_number=claim.gate_attempt_number,
            repair_execution_id=claim.repair_execution_id,
        )


def _escalate_ambiguous(state_dir: Path, claim: RepairAttempt) -> None:
    persist_escalation(
        state_dir,
        claim.ticket_id,
        claim.source_state,
        claim.dest_state,
        claim.gate_attempt_number,
        "CRASH_RECONCILIATION_AMBIGUOUS",
        gate_attempt_number=claim.gate_attempt_number,
        repair_execution_id=claim.repair_execution_id,
    )
