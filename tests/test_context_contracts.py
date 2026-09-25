"""Regression tests for context contracts enforcement.

Covers contract registry completeness, resolution semantics, field presence,
validators, enforcement in TicketStore, batch atomicity, failure artifacts,
bounded blocking, schema integrity, caller fixes, and direct-write invariants.
All tests use tmp_path, are deterministic, require no running orchestrator.
"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from codebot.context_contracts import (
    CONTRACTS,
    ContextContractViolation,
    ContextGateFailure,
    MissingTransitionContract,
    SOURCE_PRODUCERS,
    SPECIAL_VALIDATORS,
    TRANSITION_VALIDATORS,
    check_transition_contract,
    cleanup_gate_failure,
    field_present,
    get_producer_role,
    handle_gate_failure,
    resolve_contract,
)
from codebot.ticket_engine import (
    TRANSITIONS,
    RiskLevel,
    Severity,
    Ticket,
    TicketClass,
    TicketState,
    TicketStore,
)


def _make_ticket(
    tmp_path: Path,
    ticket_id: str = "CB-001",
    state: TicketState = TicketState.DISCOVERED,
    **overrides,
) -> tuple[TicketStore, Ticket]:
    defaults = dict(
        id=ticket_id,
        title="Test ticket",
        ticket_class=TicketClass.FEATURE,
        severity=Severity.LOW,
        state=state,
        source="test",
        evidence="file.py:42",
        problem_statement="Bug in X",
        desired_state="Fixed X",
        acceptance_criteria=["X works"],
        affected_modules=["mod_a"],
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
    )
    defaults.update(overrides)
    t = Ticket(**defaults)
    store = TicketStore(tmp_path / "tickets.json")
    store.add(t)
    return store, t


# ---------------------------------------------------------------------------
# 1. Contract registry coverage
# ---------------------------------------------------------------------------

class TestContractCoverage:
    def test_contract_registry_covers_all_transitions(self):
        for src, dests in TRANSITIONS.items():
            for dst in dests:
                try:
                    resolve_contract(src.value, dst.value)
                except MissingTransitionContract:
                    pytest.fail(f"Missing contract: {src.value}->{dst.value}")


# ---------------------------------------------------------------------------
# 2-4. Resolution semantics
# ---------------------------------------------------------------------------

class TestResolveContract:
    def test_resolve_contract_exact_match(self):
        fields = resolve_contract("DISCOVERED", "TRIAGED")
        assert "evidence" in fields
        assert "problem_statement" in fields

    def test_resolve_contract_wildcard_exit(self):
        assert resolve_contract("ANY_STATE", "RESOLVED") == ()
        assert resolve_contract("ANY_STATE", "CANCELLED") == ()
        assert resolve_contract("ANY_STATE", "SUPERSEDED") == ("superseded_by",)

    def test_resolve_contract_missing_raises(self):
        with pytest.raises(MissingTransitionContract):
            resolve_contract("FAKE_SOURCE", "FAKE_DEST")

    def test_missing_contract_is_valueerror(self):
        assert issubclass(MissingTransitionContract, ValueError)
        with pytest.raises(ValueError):
            resolve_contract("NOPE", "NOPE")


# ---------------------------------------------------------------------------
# 5-6. Field presence determinism
# ---------------------------------------------------------------------------

class TestFieldPresent:
    def test_field_present_deterministic(self):
        assert field_present("x", "hello") is True
        assert field_present("x", "") is False
        assert field_present("x", "   ") is False
        assert field_present("x", None) is False
        assert field_present("x", []) is False
        assert field_present("x", [1]) is True
        assert field_present("x", {}) is False
        assert field_present("x", {"a": 1}) is True
        assert field_present("x", 0) is False
        assert field_present("x", 1) is True
        assert field_present("x", 0.0) is False
        assert field_present("x", 3.14) is True
        assert field_present("x", True) is True


# ---------------------------------------------------------------------------
# 7-8. Special validators
# ---------------------------------------------------------------------------

class TestSpecialValidators:
    def test_special_validators_rework_count(self):
        v = SPECIAL_VALIDATORS["rework_count"]
        assert v(1) is True
        assert v(0) is False
        assert v(-1) is False
        assert v("not_int") is False

    def test_special_validators_attempts(self):
        v = SPECIAL_VALIDATORS["attempts"]
        assert v(1) is True
        assert v(0) is False
        assert v(5) is True

    def test_implement_review_uses_attempts_not_current_attempt_id(self):
        fields = resolve_contract("IMPLEMENT", "REVIEW")
        assert "attempts" in fields
        assert "current_attempt_id" not in fields


# ---------------------------------------------------------------------------
# 9-11. Transition validators
# ---------------------------------------------------------------------------

class TestTransitionValidators:
    def test_transition_validators_goal_decomp(self):
        now = SimpleNamespace(goal_disposition="NOW")
        later = SimpleNamespace(goal_disposition="LATER")
        assert TRANSITION_VALIDATORS[("GOAL", "DECOMP")](now) is True
        assert TRANSITION_VALIDATORS[("GOAL", "DECOMP")](later) is False

    def test_transition_validators_goal_later(self):
        now = SimpleNamespace(goal_disposition="NOW")
        later = SimpleNamespace(goal_disposition="LATER")
        assert TRANSITION_VALIDATORS[("GOAL", "LATER")](later) is True
        assert TRANSITION_VALIDATORS[("GOAL", "LATER")](now) is False

    def test_transition_validators_goal_never(self):
        never = SimpleNamespace(goal_disposition="NEVER")
        now = SimpleNamespace(goal_disposition="NOW")
        assert TRANSITION_VALIDATORS[("GOAL", "NEVER")](never) is True
        assert TRANSITION_VALIDATORS[("GOAL", "NEVER")](now) is False


# ---------------------------------------------------------------------------
# 12-13. Check contract returns separated concerns
# ---------------------------------------------------------------------------

class TestCheckContractSeparation:
    def test_check_contract_returns_separate_missing_and_invariants(self):
        later_ticket = SimpleNamespace(goal_disposition="LATER", problem_statement="X")
        ok, missing, inv = check_transition_contract(later_ticket, "GOAL", "DECOMP")
        assert ok is False
        assert "goal_disposition" not in missing
        assert len(inv) == 1
        assert "GOAL->DECOMP" in inv[0]

    def test_check_contract_passes_with_complete_context(self):
        good = SimpleNamespace(evidence="file.py:1", problem_statement="Bug")
        ok, missing, inv = check_transition_contract(good, "DISCOVERED", "TRIAGED")
        assert ok is True
        assert missing == []
        assert inv == []

    def test_check_contract_fails_with_missing_field(self):
        bad = SimpleNamespace(evidence="", problem_statement="Bug")
        ok, missing, inv = check_transition_contract(bad, "DISCOVERED", "TRIAGED")
        assert ok is False
        assert "evidence" in missing


# ---------------------------------------------------------------------------
# 14-15. Producer role totality
# ---------------------------------------------------------------------------

class TestProducerRole:
    def test_get_producer_role_total_for_all_source_states(self):
        for src in SOURCE_PRODUCERS:
            role = get_producer_role(src, "ANY")
            assert role, f"Empty role for {src}"

    def test_get_producer_role_unknown_state_returns_platform(self):
        assert get_producer_role("UNKNOWN_STATE", "ANY") == "platform"


# ---------------------------------------------------------------------------
# 16-17. Enforcement in TicketStore
# ---------------------------------------------------------------------------

class TestEnforcement:
    def test_enforcement_blocks_transition_in_store(self, tmp_path: Path):
        store, _t = _make_ticket(tmp_path, evidence="")
        with pytest.raises(ContextContractViolation) as exc_info:
            store.transition("CB-001", TicketState.TRIAGED)
        e = exc_info.value
        assert e.ticket_id == "CB-001"
        assert "evidence" in e.missing_fields
        assert isinstance(e.failed_invariants, list)
        assert isinstance(e, ValueError)

    def test_enforcement_allows_transition_with_complete_context(self, tmp_path: Path):
        store, _t = _make_ticket(
            tmp_path,
            evidence="file.py:1",
            problem_statement="Bug in X",
        )
        result = store.transition("CB-001", TicketState.TRIAGED)
        assert result.state == TicketState.TRIAGED


# ---------------------------------------------------------------------------
# 18. Batch atomic rollback
# ---------------------------------------------------------------------------

class TestBatchAtomicity:
    def test_batch_transition_atomic_rollback_on_gate_failure(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        t1 = Ticket(
            id="CB-A01", title="T1", ticket_class=TicketClass.FEATURE,
            severity=Severity.LOW, state=TicketState.DISCOVERED, source="test",
            evidence="f.py:1", problem_statement="P1", desired_state="D",
            acceptance_criteria=["a"], affected_modules=["m"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="", security_impact="",
            migration_impact="", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="r",
            estimated_cost_tokens=0, created_at=0.0, updated_at=0.0,
        )
        t2 = Ticket(
            id="CB-A02", title="T2", ticket_class=TicketClass.FEATURE,
            severity=Severity.LOW, state=TicketState.DISCOVERED, source="test",
            evidence="f.py:2", problem_statement="P2", desired_state="D",
            acceptance_criteria=["a"], affected_modules=["m"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="", security_impact="",
            migration_impact="", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="r",
            estimated_cost_tokens=0, created_at=0.0, updated_at=0.0,
        )
        t3 = Ticket(
            id="CB-A03", title="T3", ticket_class=TicketClass.FEATURE,
            severity=Severity.LOW, state=TicketState.DISCOVERED, source="test",
            evidence="", problem_statement="P3", desired_state="D",
            acceptance_criteria=["a"], affected_modules=["m"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="", security_impact="",
            migration_impact="", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="r",
            estimated_cost_tokens=0, created_at=0.0, updated_at=0.0,
        )
        store.add(t1)
        store.add(t2)
        store.add(t3)
        with pytest.raises(ContextContractViolation):
            store.batch_transition([
                ("CB-A01", TicketState.TRIAGED, None),
                ("CB-A02", TicketState.TRIAGED, None),
                ("CB-A03", TicketState.TRIAGED, None),
            ])
        assert store.get("CB-A01").state == TicketState.DISCOVERED
        assert store.get("CB-A02").state == TicketState.DISCOVERED
        assert store.get("CB-A03").state == TicketState.DISCOVERED


# ---------------------------------------------------------------------------
# 19-22. Failure artifacts and bounded blocking
# ---------------------------------------------------------------------------

class TestFailureArtifacts:
    def test_first_gate_failure_creates_repairable_artifact(self, tmp_path: Path):
        store, _t = _make_ticket(tmp_path, evidence="")
        with pytest.raises(ContextContractViolation):
            store.transition("CB-001", TicketState.TRIAGED)
        artifact = ContextGateFailure.load(tmp_path, "CB-001", "DISCOVERED", "TRIAGED")
        assert artifact is not None
        assert artifact.repairable is True
        assert artifact.repair_exhausted is False
        assert artifact.attempt_count == 1
        assert "evidence" in artifact.missing_fields

    def test_second_gate_failure_marks_exhausted(self, tmp_path: Path):
        store, _t = _make_ticket(tmp_path, evidence="")
        with pytest.raises(ContextContractViolation):
            store.transition("CB-001", TicketState.TRIAGED)
        with pytest.raises(ContextContractViolation):
            store.transition("CB-001", TicketState.TRIAGED)
        artifact = ContextGateFailure.load(tmp_path, "CB-001", "DISCOVERED", "TRIAGED")
        assert artifact is not None
        assert artifact.attempt_count == 2
        assert artifact.repairable is False
        assert artifact.repair_exhausted is True

    def test_attempt_count_saturates_at_two(self, tmp_path: Path):
        store, _t = _make_ticket(tmp_path, evidence="")
        for _ in range(5):
            with pytest.raises(ContextContractViolation):
                store.transition("CB-001", TicketState.TRIAGED)
        artifact = ContextGateFailure.load(tmp_path, "CB-001", "DISCOVERED", "TRIAGED")
        assert artifact.attempt_count == 2

    def test_failure_attempts_isolated_by_transition(self, tmp_path: Path):
        sd = tmp_path
        f1 = ContextGateFailure(
            "CB-X1", "PLANNING", "IMPLEMENT",
            ("desired_state",), (), 1, 0.0, "planner", "fix", True, False,
        )
        f1.save(sd)
        f2 = ContextGateFailure(
            "CB-X1", "REWORK", "IMPLEMENT",
            ("reviewer_feedback",), (), 1, 0.0, "rework", "fix", True, False,
        )
        f2.save(sd)
        loaded1 = ContextGateFailure.load(sd, "CB-X1", "PLANNING", "IMPLEMENT")
        loaded2 = ContextGateFailure.load(sd, "CB-X1", "REWORK", "IMPLEMENT")
        assert loaded1.attempt_count == 1
        assert loaded2.attempt_count == 1
        assert loaded1.missing_fields == ("desired_state",)
        assert loaded2.missing_fields == ("reviewer_feedback",)


# ---------------------------------------------------------------------------
# 23. Cleanup on success
# ---------------------------------------------------------------------------

class TestCleanup:
    def test_successful_transition_cleans_up_artifact_after_commit(self, tmp_path: Path):
        store, _t = _make_ticket(tmp_path, evidence="")
        with pytest.raises(ContextContractViolation):
            store.transition("CB-001", TicketState.TRIAGED)
        assert ContextGateFailure.load(tmp_path, "CB-001", "DISCOVERED", "TRIAGED") is not None
        fixed = replace(store.get("CB-001"), evidence="file.py:1")
        store._tickets["CB-001"] = fixed
        store._dirty_ids.add("CB-001")
        store.transition("CB-001", TicketState.TRIAGED)
        assert ContextGateFailure.load(tmp_path, "CB-001", "DISCOVERED", "TRIAGED") is None


# ---------------------------------------------------------------------------
# 24. Universal exits always pass
# ---------------------------------------------------------------------------

class TestUniversalExits:
    def test_universal_exits_always_pass(self, tmp_path: Path):
        store, _t = _make_ticket(tmp_path, evidence="", problem_statement="")
        result = store.transition("CB-001", TicketState.CANCELLED)
        assert result.state == TicketState.CANCELLED


# ---------------------------------------------------------------------------
# 25. Schema integrity
# ---------------------------------------------------------------------------

class TestSchemaIntegrity:
    def test_all_contract_fields_exist_on_ticket_schema(self):
        ticket_fields = set(Ticket.__dataclass_fields__.keys())
        for (src, dst), fields in CONTRACTS.items():
            if src == "*":
                continue
            for f in fields:
                assert f in ticket_fields, (
                    f"Contract ({src},{dst}) references field '{f}' "
                    f"not found on Ticket dataclass"
                )


# ---------------------------------------------------------------------------
# 26. Batch failure preserves prior artifacts
# ---------------------------------------------------------------------------

class TestBatchArtifactPreservation:
    def test_batch_failure_does_not_cleanup_prior_gate_artifacts(self, tmp_path: Path):
        store = TicketStore(tmp_path / "tickets.json")
        ta = Ticket(
            id="CB-B01", title="A", ticket_class=TicketClass.FEATURE,
            severity=Severity.LOW, state=TicketState.DISCOVERED, source="test",
            evidence="", problem_statement="P", desired_state="D",
            acceptance_criteria=["a"], affected_modules=["m"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="", security_impact="",
            migration_impact="", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="r",
            estimated_cost_tokens=0, created_at=0.0, updated_at=0.0,
        )
        tb = Ticket(
            id="CB-B02", title="B", ticket_class=TicketClass.FEATURE,
            severity=Severity.LOW, state=TicketState.DISCOVERED, source="test",
            evidence="other.py:1", problem_statement="", desired_state="D",
            acceptance_criteria=["a"], affected_modules=["m"], dependencies=[],
            risk=RiskLevel.LOW, blast_radius="", security_impact="",
            migration_impact="", required_reviewers=[], required_tests=[],
            documentation_requirements=[], rollback_strategy="r",
            estimated_cost_tokens=0, created_at=0.0, updated_at=0.0,
        )
        store.add(ta)
        store.add(tb)
        with pytest.raises(ContextContractViolation):
            store.transition("CB-B01", TicketState.TRIAGED)
        prior_artifact = ContextGateFailure.load(tmp_path, "CB-B01", "DISCOVERED", "TRIAGED")
        assert prior_artifact is not None
        fixed_a = replace(store.get("CB-B01"), evidence="f.py:1")
        store._tickets["CB-B01"] = fixed_a
        store._dirty_ids.add("CB-B01")
        with pytest.raises(ContextContractViolation):
            store.batch_transition([
                ("CB-B01", TicketState.TRIAGED, None),
                ("CB-B02", TicketState.TRIAGED, None),
            ])
        assert store.get("CB-B01").state == TicketState.DISCOVERED
        assert store.get("CB-B02").state == TicketState.DISCOVERED
        assert ContextGateFailure.load(tmp_path, "CB-B01", "DISCOVERED", "TRIAGED") is not None


# ---------------------------------------------------------------------------
# 27. Same-state mutations preserve ticket state
# ---------------------------------------------------------------------------

class TestSameStateMutations:
    def test_same_state_mutations_preserve_ticket_state(self, tmp_path: Path):
        store, t = _make_ticket(
            tmp_path,
            state=TicketState.COMPLETE,
            evidence="f.py:1",
        )
        original_state = t.state
        updated = replace(t, commit_sha="abc123", committed_at=time.time())
        assert updated.state == original_state
        store._tickets[t.id] = updated
        store._dirty_ids.add(t.id)
        assert store.get(t.id).state == original_state
        updated2 = replace(updated, implementation_approvals=["reviewer"])
        assert updated2.state == original_state


# ---------------------------------------------------------------------------
# 28. Exception attributes
# ---------------------------------------------------------------------------

class TestExceptionAttributes:
    def test_context_contract_violation_carries_both_lists(self):
        e = ContextContractViolation(
            ticket_id="CB-T1",
            source_state="GOAL",
            dest_state="DECOMP",
            missing_fields=["problem_statement"],
            failed_invariants=["GOAL->DECOMP"],
        )
        assert e.missing_fields == ["problem_statement"]
        assert e.failed_invariants == ["GOAL->DECOMP"]
        assert isinstance(e, ValueError)
        assert "missing" in str(e)
        assert "invariant" in str(e)

    def test_context_contract_violation_empty_lists(self):
        e = ContextContractViolation(
            ticket_id="CB-T2",
            source_state="X",
            dest_state="Y",
            missing_fields=[],
            failed_invariants=[],
        )
        assert "unknown" in str(e)
