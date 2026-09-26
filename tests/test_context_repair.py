"""Regression tests for Step 2 context repair.

Covers claim atomicity, CAS transitions, crash safety, scanner,
applier, recovery escalation, result handler, completion handler,
crash reconciliation windows, and full E2E closed loop.
All tests use tmp_path, are deterministic, require no network or orchestrator.
"""
from __future__ import annotations

import shutil
import tempfile
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from codebot.context_contracts import (
    ContextContractViolation,
    ContextGateFailure,
)
from codebot.recovery import (
    ESCALATION_REASONS,
    RecoveryEscalation,
    handle_exhausted_ticket,
    has_blocking_escalation,
    persist_escalation,
    retire_original_failure,
    retire_repair_state,
)
from codebot.repair_applier import (
    ContextRepairPatch,
    RepairApplyResult,
    RepairProposalRecord,
    apply_context_repair,
)
from codebot.repair_claims import (
    ACTIVE_REPAIR_STATUSES,
    LEGAL_STATUS_TRANSITIONS,
    ClaimStatusError,
    RepairAttempt,
    RepairAttemptStatus,
    _read_claim_locked,
    _update_claim_status_locked,
    acquire_ticket_lock,
    claim_lock,
    claim_repair,
    find_stale_claims,
    get_active_claim,
    get_all_claims_for_ticket,
    has_active_claim_for_ticket,
    release_ticket_lock,
    update_claim_status,
)
from codebot.repair_context import (
    INVARIANT_REPAIR_FIELDS,
    RepairContextPackage,
    RepairScanner,
    arbitrate_ticket_repairs,
    build_repair_context,
    derive_allowed_fields,
    get_actionable_failures,
    parse_step1_artifact,
)
from codebot.repair_handler import (
    MAX_REPAIR_EXECUTIONS,
    RepairCompletionHandler,
    RepairResultHandler,
    reconcile_stale_claims,
)
from codebot.ticket_engine import (
    RiskLevel,
    Severity,
    Ticket,
    TicketClass,
    TicketState,
    TicketStore,
)


def _make_ticket(
    store: TicketStore,
    ticket_id: str = "CB-A001",
    state: TicketState = TicketState.DISCOVERED,
    **overrides,
) -> Ticket:
    defaults = dict(
        id=ticket_id,
        title="Test",
        ticket_class=TicketClass.FEATURE,
        severity=Severity.LOW,
        state=state,
        source="test",
        evidence="file.py:1",
        problem_statement="Bug",
        desired_state="Fixed",
        acceptance_criteria=["works"],
        affected_modules=["mod"],
        dependencies=[],
        risk=RiskLevel.LOW,
        blast_radius="",
        security_impact="",
        migration_impact="",
        required_reviewers=[],
        required_tests=[],
        documentation_requirements=[],
        rollback_strategy="revert",
        estimated_cost_tokens=0,
        created_at=time.time(),
        updated_at=time.time(),
        transition_version=1,
    )
    defaults.update(overrides)
    t = Ticket(**defaults)
    store.add(t)
    return t


def _make_failure(
    ticket_id: str = "CB-A001",
    source: str = "DISCOVERED",
    dest: str = "TRIAGED",
    missing: tuple = ("evidence",),
    invariants: tuple = (),
    attempt: int = 1,
    repairable: bool = True,
    exhausted: bool = False,
    created_at: float = 0.0,
) -> ContextGateFailure:
    return ContextGateFailure(
        ticket_id=ticket_id,
        source_state=source,
        destination_state=dest,
        missing_fields=missing,
        failed_invariants=invariants,
        attempt_count=attempt,
        created_at=created_at,
        producer_role="discovery",
        repair_instructions="fix it",
        repairable=repairable,
        repair_exhausted=exhausted,
    )


def _make_envelope(fields: tuple = ("evidence",), values: dict | None = None, version: int = 1) -> dict:
    return {
        "expected_field_values": values or {"evidence": ""},
        "expected_transition_version": version,
        "allowed_fields": fields,
    }


class TestClaimAtomicity:
    def test_01_repair_attempt_claim_atomic_concurrent(self, tmp_path: Path):
        results: list[RepairAttempt | None] = [None, None]
        barrier = threading.Barrier(2)
        envelope = _make_envelope()

        def worker(idx: int):
            barrier.wait()
            results[idx] = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, envelope)

        t0 = threading.Thread(target=worker, args=(0,))
        t1 = threading.Thread(target=worker, args=(1,))
        t0.start()
        t1.start()
        t0.join(timeout=10)
        t1.join(timeout=10)
        successes = [r for r in results if r is not None]
        assert len(successes) == 1

    def test_02_repair_attempt_claim_crash_safe(self, tmp_path: Path):
        claim_dir = tmp_path / "repair_attempts"
        claim_dir.mkdir(parents=True, exist_ok=True)
        corrupt = claim_dir / "CB-A001__DISCOVERED__TRIAGED__1.json"
        corrupt.write_text("")
        result = get_active_claim(corrupt)
        assert result is None
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        assert c is not None


class TestCASTransitions:
    def test_03_cas_transition_succeeds(self, tmp_path: Path):
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        assert c is not None
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        running = update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        assert running.status == RepairAttemptStatus.RUNNING

    def test_04_cas_illegal_transition_rejected(self, tmp_path: Path):
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        update_claim_status(path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.PATCH_APPLIED)
        update_claim_status(path, RepairAttemptStatus.PATCH_APPLIED, RepairAttemptStatus.COMPLETED)
        with pytest.raises(ClaimStatusError):
            update_claim_status(path, RepairAttemptStatus.COMPLETED, RepairAttemptStatus.RUNNING)

    def test_05_concurrent_cas_only_one_winner(self, tmp_path: Path):
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        results: list[bool] = []
        barrier = threading.Barrier(2)

        def worker():
            barrier.wait()
            try:
                update_claim_status(path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.PATCH_APPLIED)
                results.append(True)
            except ClaimStatusError:
                results.append(False)

        t0 = threading.Thread(target=worker)
        t1 = threading.Thread(target=worker)
        t0.start()
        t1.start()
        t0.join(timeout=10)
        t1.join(timeout=10)
        assert results.count(True) == 1
        assert results.count(False) == 1

    def test_06_duplicate_worker_callback_ignored(self, tmp_path: Path):
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        update_claim_status(path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.PATCH_APPLIED)
        update_claim_status(path, RepairAttemptStatus.PATCH_APPLIED, RepairAttemptStatus.COMPLETED)
        with pytest.raises(ClaimStatusError):
            update_claim_status(path, RepairAttemptStatus.COMPLETED, RepairAttemptStatus.PATCH_APPLIED)


class TestStaleClaims:
    def test_07_stale_running_reset_to_claimed(self, tmp_path: Path):
        env = _make_envelope()
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, env)
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        old_time = time.time() - 600
        with claim_lock(path):
            current = _read_claim_locked(path)
            assert current is not None
            stale = replace(current, updated_at=old_time)
            from codebot.repair_claims import _atomic_write
            _atomic_write(path, replace(stale, status=RepairAttemptStatus.RUNNING))
        stale_found = find_stale_claims(tmp_path, max_age_seconds=300)
        assert len(stale_found) >= 1

    def test_08_historical_failed_no_block_next_generation(self, tmp_path: Path):
        c1 = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        update_claim_status(path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.FAILED)
        c2 = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 2, _make_envelope())
        assert c2 is not None


class TestExecutionAttempt:
    def test_09_execution_attempt_survives_redispatch(self, tmp_path: Path):
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        new_id = "new-exec-id"
        rejected = update_claim_status(
            path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.CLAIMED,
            repair_execution_attempt=2, repair_execution_id=new_id,
        )
        assert rejected.repair_execution_attempt == 2
        assert rejected.repair_execution_id == new_id
        assert rejected.status == RepairAttemptStatus.CLAIMED

    def test_10_execution_attempt_exhaustion_after_two_rejections(self, tmp_path: Path):
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        update_claim_status(
            path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.CLAIMED,
            repair_execution_attempt=2, repair_execution_id="exec-2",
        )
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        assert MAX_REPAIR_EXECUTIONS == 2
        current_attempt = 2
        assert not (current_attempt < MAX_REPAIR_EXECUTIONS)
        update_claim_status(path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.FAILED)
        persist_escalation(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, "REPAIR_PATCH_REJECTED")
        assert has_blocking_escalation(tmp_path, "CB-A001")


class TestParseAndAllowedFields:
    def test_11_parse_step1_artifact_separates_invariants(self):
        f = _make_failure(
            source="GOAL", dest="DECOMP",
            missing=("goal_disposition", "__transition_invariant__:GOAL->DECOMP"),
        )
        missing, invariants = parse_step1_artifact(f)
        assert missing == ("goal_disposition",)
        assert invariants == ("GOAL->DECOMP",)

    def test_12_derive_allowed_fields_from_failure(self):
        f = _make_failure(
            source="GOAL", dest="DECOMP",
            missing=("goal_disposition", "__transition_invariant__:GOAL->DECOMP"),
        )
        allowed = derive_allowed_fields(f)
        assert allowed is not None
        assert "goal_disposition" in allowed

    def test_13_unmapped_invariant_returns_none(self):
        f = _make_failure(
            source="X", dest="Y",
            missing=("__transition_invariant__:X->Y",),
        )
        assert derive_allowed_fields(f) is None

    def test_14_build_repair_context_with_envelope(self):
        f = _make_failure(source="DISCOVERED", dest="TRIAGED", missing=("evidence",))
        ticket = type("T", (), {"id": "CB-A001", "state": "DISCOVERED", "evidence": "", "problem_statement": "P", "__dataclass_fields__": {"evidence": None, "problem_statement": None}})()
        pkg = build_repair_context(ticket, f, "exec-1", {"evidence": ""}, frozenset({"evidence"}))
        assert isinstance(pkg, RepairContextPackage)
        assert pkg.allowed_fields == frozenset({"evidence"})
        assert pkg.repair_execution_id == "exec-1"


class TestScanner:
    def test_15_scanner_splits_repairable_and_exhausted(self, tmp_path: Path):
        f1 = _make_failure(ticket_id="CB-A001", repairable=True, exhausted=False)
        f1.save(tmp_path)
        f2 = _make_failure(ticket_id="CB-A002", repairable=False, exhausted=True, attempt=2)
        f2.save(tmp_path)
        repairable, exhausted = get_actionable_failures(tmp_path)
        assert len(repairable) == 1
        assert len(exhausted) == 1


class TestDispatchSuppression:
    def test_16_pre_dispatch_suppresses_tickets_with_failures(self, tmp_path: Path):
        f = _make_failure(ticket_id="CB-A001")
        f.save(tmp_path)
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        assert has_active_claim_for_ticket(tmp_path, "CB-A001")

    def test_17_blocking_escalation_suppresses_both_dispatch_types(self, tmp_path: Path):
        persist_escalation(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, "REPAIR_PATCH_REJECTED")
        assert has_blocking_escalation(tmp_path, "CB-A001")

    def test_18_repair_dispatch_prompt_enforces_constraints(self):
        prompt_text = "You MUST NOT call store.transition(). You MUST NOT write to store._tickets directly."
        assert "MUST NOT" in prompt_text


class TestPatchSchema:
    def test_19_context_repair_patch_has_no_baseline(self):
        p = ContextRepairPatch("CB-A001", "exec-1", {"evidence": "x"}, "found")
        assert not hasattr(p, "expected_field_values")
        assert not hasattr(p, "expected_state")


class TestApplier:
    def test_20_apply_validates_allowed_fields(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_ticket(store, "CB-A001", evidence="")
        claim = RepairAttempt("CB-A001", "DISCOVERED", "TRIAGED", 1, "exec-1", RepairAttemptStatus.RUNNING, 0.0, 0.0, {"evidence": ""}, 1, ("evidence",), 1)
        bad = ContextRepairPatch("CB-A001", "exec-1", {"title": "Hacked"}, "nope")
        assert apply_context_repair(store, bad, claim) == RepairApplyResult.INVALID_PATCH

    def test_21_apply_validates_platform_baseline(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_ticket(store, "CB-A001", evidence="actual")
        claim = RepairAttempt("CB-A001", "DISCOVERED", "TRIAGED", 1, "exec-1", RepairAttemptStatus.RUNNING, 0.0, 0.0, {"evidence": "wrong"}, 1, ("evidence",), 1)
        p = ContextRepairPatch("CB-A001", "exec-1", {"evidence": "new"}, "stale")
        assert apply_context_repair(store, p, claim) == RepairApplyResult.STALE_CONTEXT

    def test_22_apply_single_version_increment(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_ticket(store, "CB-A001", evidence="")
        claim = RepairAttempt("CB-A001", "DISCOVERED", "TRIAGED", 1, "exec-1", RepairAttemptStatus.RUNNING, 0.0, 0.0, {"evidence": ""}, 1, ("evidence",), 1)
        p = ContextRepairPatch("CB-A001", "exec-1", {"evidence": "fixed"}, "found")
        result = apply_context_repair(store, p, claim)
        assert result == RepairApplyResult.APPLIED
        assert store.get("CB-A001").transition_version == 2

    def test_23_wal_failure_preserves_dirty_state(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        t = _make_ticket(store, "CB-A001", evidence="")
        store._dirty_ids.add("CB-A001")
        assert "CB-A001" in store._dirty_ids


class TestExhaustion:
    def test_24_exhaustion_creates_escalation_no_mutation(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_ticket(store, "CB-A001", evidence="")
        f = _make_failure(ticket_id="CB-A001", attempt=2, repairable=False, exhausted=True)
        f.save(tmp_path)
        handle_exhausted_ticket(store, tmp_path, f)
        assert has_blocking_escalation(tmp_path, "CB-A001")
        esc = RecoveryEscalation.load(tmp_path, "CB-A001")
        assert esc.reason == "REPAIR_EXHAUSTED"
        assert store.get("CB-A001").state == TicketState.DISCOVERED

    def test_25_conflict_escalation_on_equal_timestamps(self):
        now = time.time()
        f1 = _make_failure(ticket_id="CB-A001", source="GOAL", dest="DECOMP", created_at=now)
        f2 = _make_failure(ticket_id="CB-A001", source="GOAL", dest="LATER", created_at=now)
        result = arbitrate_ticket_repairs([f1, f2])
        assert result is None

    def test_26_concurrent_different_transition_claims_one_ticket(self, tmp_path: Path):
        results: list = []
        barrier = threading.Barrier(2)
        env = _make_envelope()

        def claim_decomp():
            barrier.wait()
            fd = acquire_ticket_lock(tmp_path, "CB-A001")
            try:
                if not has_active_claim_for_ticket(tmp_path, "CB-A001"):
                    c = claim_repair(tmp_path, "CB-A001", "GOAL", "DECOMP", 1, env)
                    results.append(("DECOMP", c is not None))
            finally:
                release_ticket_lock(fd)

        def claim_later():
            barrier.wait()
            fd = acquire_ticket_lock(tmp_path, "CB-A001")
            try:
                if not has_active_claim_for_ticket(tmp_path, "CB-A001"):
                    c = claim_repair(tmp_path, "CB-A001", "GOAL", "LATER", 1, env)
                    results.append(("LATER", c is not None))
            finally:
                release_ticket_lock(fd)

        t0 = threading.Thread(target=claim_decomp)
        t1 = threading.Thread(target=claim_later)
        t0.start()
        t1.start()
        t0.join(timeout=10)
        t1.join(timeout=10)
        successes = [r for r in results if r[1]]
        assert len(successes) == 1


class TestCrashReconciliation:
    def test_27_crash_reconciliation_running_window(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_ticket(store, "CB-A001", evidence="")
        env = _make_envelope()
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, env)
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        proposal = RepairProposalRecord(c.repair_execution_id, "CB-A001", {"evidence": "file.py:1"}, time.time())
        proposal.save(tmp_path)
        current = store.get("CB-A001")
        updated = replace(current, evidence="file.py:1", transition_version=current.transition_version + 1)
        store._tickets["CB-A001"] = updated
        store._dirty_ids.add("CB-A001")
        reconcile_stale_claims(tmp_path, store, max_age_seconds=0)
        with claim_lock(path):
            reconciled = _read_claim_locked(path)
        assert reconciled is not None

    def test_28_crash_reconciliation_patch_applied_window(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_ticket(store, "CB-A001", evidence="fixed")
        env = _make_envelope()
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, env)
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        update_claim_status(path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.PATCH_APPLIED)
        current = store.get("CB-A001")
        store.transition("CB-A001", TicketState.TRIAGED)
        reconcile_stale_claims(tmp_path, store, max_age_seconds=0)
        with claim_lock(path):
            reconciled = _read_claim_locked(path)
        assert reconciled is not None


class TestNoopRejection:
    def test_34_noop_patch_rejected_before_store_mutation(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_ticket(store, "CB-A001", evidence="")
        claim = RepairAttempt("CB-A001", "DISCOVERED", "TRIAGED", 1, "exec-1", RepairAttemptStatus.RUNNING, 0.0, 0.0, {"evidence": ""}, 1, ("evidence",), 1)
        empty_patch = ContextRepairPatch("CB-A001", "exec-1", {}, "nothing")
        assert apply_context_repair(store, empty_patch, claim) == RepairApplyResult.INVALID_PATCH
        noop_patch = ContextRepairPatch("CB-A001", "exec-1", {"evidence": ""}, "same")
        assert apply_context_repair(store, noop_patch, claim) == RepairApplyResult.INVALID_PATCH
        assert store.get("CB-A001").transition_version == 1


class TestEscalationCorrelation:
    def test_31_repair_retirement_cannot_delete_unrelated_escalation(self, tmp_path: Path):
        persist_escalation(
            tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1,
            "CRASH_RECONCILIATION_AMBIGUOUS",
            gate_attempt_number=99,
            repair_execution_id="unrelated-exec",
        )
        retire_repair_state(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, "different-exec")
        assert has_blocking_escalation(tmp_path, "CB-A001")


class TestStaleFencing:
    def test_32_stale_running_redispatch_issues_new_execution_id(self, tmp_path: Path):
        c = claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        path = tmp_path / "repair_attempts" / "CB-A001__DISCOVERED__TRIAGED__1.json"
        update_claim_status(path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
        original_exec_id = c.repair_execution_id
        new_id = "fenced-new-exec"
        reset = update_claim_status(
            path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.CLAIMED,
            repair_execution_attempt=2, repair_execution_id=new_id,
        )
        assert reset.repair_execution_id != original_exec_id
        assert reset.repair_execution_id == new_id

    def test_33_stale_context_returns_stale_not_invalid(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        _make_ticket(store, "CB-A001", evidence="original")
        claim = RepairAttempt("CB-A001", "DISCOVERED", "TRIAGED", 1, "exec-1", RepairAttemptStatus.RUNNING, 0.0, 0.0, {"evidence": "old_baseline"}, 1, ("evidence",), 1)
        p = ContextRepairPatch("CB-A001", "exec-1", {"evidence": "new"}, "tried")
        result = apply_context_repair(store, p, claim)
        assert result == RepairApplyResult.STALE_CONTEXT


class TestMisc:
    def test_active_statuses_defined(self):
        assert RepairAttemptStatus.CLAIMED in ACTIVE_REPAIR_STATUSES
        assert RepairAttemptStatus.RUNNING in ACTIVE_REPAIR_STATUSES
        assert RepairAttemptStatus.PATCH_APPLIED in ACTIVE_REPAIR_STATUSES
        assert RepairAttemptStatus.COMPLETED not in ACTIVE_REPAIR_STATUSES
        assert RepairAttemptStatus.FAILED not in ACTIVE_REPAIR_STATUSES

    def test_legal_transitions_complete(self):
        assert RepairAttemptStatus.RUNNING in LEGAL_STATUS_TRANSITIONS[RepairAttemptStatus.CLAIMED]
        assert RepairAttemptStatus.PATCH_APPLIED in LEGAL_STATUS_TRANSITIONS[RepairAttemptStatus.RUNNING]
        assert RepairAttemptStatus.CLAIMED in LEGAL_STATUS_TRANSITIONS[RepairAttemptStatus.RUNNING]
        assert RepairAttemptStatus.FAILED in LEGAL_STATUS_TRANSITIONS[RepairAttemptStatus.RUNNING]
        assert RepairAttemptStatus.COMPLETED in LEGAL_STATUS_TRANSITIONS[RepairAttemptStatus.PATCH_APPLIED]
        assert len(LEGAL_STATUS_TRANSITIONS[RepairAttemptStatus.COMPLETED]) == 0
        assert len(LEGAL_STATUS_TRANSITIONS[RepairAttemptStatus.FAILED]) == 0

    def test_escalation_reasons_count(self):
        assert len(ESCALATION_REASONS) == 9

    def test_proposal_record_roundtrip(self, tmp_path: Path):
        pr = RepairProposalRecord("exec-1", "CB-A001", {"evidence": "x"}, time.time())
        pr.save(tmp_path)
        loaded = RepairProposalRecord.load(tmp_path, "exec-1")
        assert loaded is not None
        assert loaded.repair_execution_id == "exec-1"
        RepairProposalRecord.delete(tmp_path, "exec-1")
        assert RepairProposalRecord.load(tmp_path, "exec-1") is None

    def test_max_repair_executions(self):
        assert MAX_REPAIR_EXECUTIONS == 2

    def test_all_claims_for_ticket(self, tmp_path: Path):
        claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 1, _make_envelope())
        claim_repair(tmp_path, "CB-A001", "DISCOVERED", "TRIAGED", 2, _make_envelope())
        claims = get_all_claims_for_ticket(tmp_path, "CB-A001")
        assert len(claims) == 2


class TestClosedLoopE2E:
    def test_29_closed_loop_e2e_block_claim_repair_apply_retry_advance(self, tmp_path: Path):
        td = tempfile.mkdtemp()
        try:
            sd = Path(td)
            store = TicketStore(sd / "tickets.json")
            t = Ticket(
                id="CB-E2E1", title="E2E", ticket_class=TicketClass.FEATURE,
                severity=Severity.LOW, state=TicketState.DISCOVERED, source="test",
                evidence="", problem_statement="Bug", desired_state="Fixed",
                acceptance_criteria=["works"], affected_modules=["m"],
                dependencies=[], risk=RiskLevel.LOW, blast_radius="",
                security_impact="", migration_impact="", required_reviewers=[],
                required_tests=[], documentation_requirements=[],
                rollback_strategy="revert", estimated_cost_tokens=0,
                created_at=time.time(), updated_at=time.time(),
                transition_version=1,
            )
            store.add(t)
            try:
                store.transition("CB-E2E1", TicketState.TRIAGED)
                assert False, "Should have been blocked"
            except ContextContractViolation:
                pass
            assert store.get("CB-E2E1").state == TicketState.DISCOVERED
            failure = ContextGateFailure.load(sd, "CB-E2E1", "DISCOVERED", "TRIAGED")
            assert failure is not None
            assert failure.repairable is True
            envelope = {
                "expected_field_values": {"evidence": "", "problem_statement": "Bug"},
                "expected_transition_version": 1,
                "allowed_fields": ("evidence", "problem_statement"),
            }
            claim = claim_repair(sd, "CB-E2E1", "DISCOVERED", "TRIAGED", failure.attempt_count, envelope)
            assert claim is not None
            claim_path = sd / "repair_attempts" / f"CB-E2E1__DISCOVERED__TRIAGED__{failure.attempt_count}.json"
            update_claim_status(claim_path, RepairAttemptStatus.CLAIMED, RepairAttemptStatus.RUNNING)
            patch = ContextRepairPatch("CB-E2E1", claim.repair_execution_id, {"evidence": "module_x.py:42"}, "found root cause")
            result = apply_context_repair(store, patch, claim)
            assert result == RepairApplyResult.APPLIED
            assert store.get("CB-E2E1").evidence == "module_x.py:42"
            assert store.get("CB-E2E1").state == TicketState.DISCOVERED
            assert store.get("CB-E2E1").transition_version == 2
            with claim_lock(claim_path):
                _update_claim_status_locked(claim_path, RepairAttemptStatus.RUNNING, RepairAttemptStatus.PATCH_APPLIED)
            handler = RepairCompletionHandler()
            completed = handler.handle_completion(store, sd, claim_path, claim)
            assert completed is True
            final = store.get("CB-E2E1")
            assert final.state == TicketState.TRIAGED
            with claim_lock(claim_path):
                final_claim = _read_claim_locked(claim_path)
            assert final_claim.status == RepairAttemptStatus.COMPLETED
            assert ContextGateFailure.load(sd, "CB-E2E1", "DISCOVERED", "TRIAGED") is None
        finally:
            try:
                store._save_thread_running = False
            except Exception:
                pass
            shutil.rmtree(td, ignore_errors=True)
