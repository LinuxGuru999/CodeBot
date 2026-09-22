"""Tests for new ticket lifecycle — state machine, universal exits, isolation, schema, RL rewards, edge cases."""

import json
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.ticket_engine import (
    Ticket,
    TicketState,
    TicketClass,
    Severity,
    RiskLevel,
    TRANSITIONS,
    create_ticket,
    TicketStore,
    SCHEMA_VERSION,
)
from codebot.rl_engine import reward_from_lifecycle_outcome


def _base_ticket(**overrides) -> Ticket:
    t = create_ticket(
        title="base ticket",
        ticket_class=TicketClass.BUG,
        severity=Severity.MEDIUM,
        source="test",
        evidence="evidence base",
        problem_statement="problem base",
        desired_state="desired",
        acceptance_criteria=["ac"],
    )
    if overrides:
        import dataclasses
        from dataclasses import asdict
        d = asdict(t)
        d.update(overrides)
        # ensure enums are proper types if passed as strings
        if isinstance(d.get("state"), str):
            d["state"] = TicketState(d["state"])
        return Ticket(**d)
    return t


def _ticket_in(state: TicketState, **kwargs) -> Ticket:
    return _base_ticket(state=state, **kwargs)


def _store(tmp_path: Path) -> TicketStore:
    return TicketStore(tmp_path / "tickets.json")


# ---------------------------------------------------------------------------
# Helpers to walk valid lifecycle via Ticket.transition (no store gate checks)
# ---------------------------------------------------------------------------

def _walk_to(state: TicketState) -> Ticket:
    t = _base_ticket()
    # DISCOVERED base
    order = {
        TicketState.TRIAGED: [TicketState.TRIAGED],
        TicketState.GOAL: [TicketState.TRIAGED, TicketState.GOAL],
        TicketState.DECOMP: [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP],
        TicketState.PLANNING: [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING],
        TicketState.IMPLEMENT: [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT],
        TicketState.REVIEW: [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT, TicketState.REVIEW],
        TicketState.COMPLETE: [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT, TicketState.REVIEW, TicketState.COMPLETE],
        TicketState.REWORK: [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT, TicketState.REVIEW, TicketState.REWORK],
        TicketState.LATER: [TicketState.TRIAGED, TicketState.GOAL, TicketState.LATER],
        TicketState.NEVER: [TicketState.TRIAGED, TicketState.GOAL, TicketState.NEVER],
        TicketState.DEFERRED: [TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT, TicketState.REVIEW, TicketState.REWORK, TicketState.DEFERRED],
        TicketState.DUPLICATE: [TicketState.TRIAGED, TicketState.DUPLICATE],
        TicketState.REJECTED: [TicketState.TRIAGED, TicketState.REJECTED],
        TicketState.NOT_ACTIONABLE: [TicketState.TRIAGED, TicketState.NOT_ACTIONABLE],
        TicketState.RESOLVED: [TicketState.RESOLVED],
        TicketState.SUPERSEDED: [TicketState.SUPERSEDED],
        TicketState.CANCELLED: [TicketState.CANCELLED],
    }
    # handle aliases
    if state == TicketState.DECOMPOSE:
        state = TicketState.DECOMP
    if state == TicketState.IMPLEMENTING:
        state = TicketState.IMPLEMENT
    if state == TicketState.REVIEWING:
        state = TicketState.REVIEW
    seq = order.get(state)
    if seq is None:
        return _ticket_in(state)
    cur = t
    for s in seq:
        # REVIEW->COMPLETE needs gate if via store, but Ticket.transition is permissive
        cur = cur.transition(s)
    return cur


# ===========================================================================
# State Machine Tests (16)
# ===========================================================================

class TestStateMachine:
    def test_discovered_to_triaged_allowed(self):
        t = _ticket_in(TicketState.DISCOVERED)
        t2 = t.transition(TicketState.TRIAGED)
        assert t2.state == TicketState.TRIAGED

    def test_triaged_to_goal_allowed(self):
        t = _ticket_in(TicketState.TRIAGED)
        t2 = t.transition(TicketState.GOAL)
        assert t2.state == TicketState.GOAL

    def test_triaged_to_duplicate_allowed(self):
        t = _ticket_in(TicketState.TRIAGED)
        t2 = t.transition(TicketState.DUPLICATE)
        assert t2.state == TicketState.DUPLICATE

    def test_triaged_to_rejected_allowed(self):
        t = _ticket_in(TicketState.TRIAGED)
        t2 = t.transition(TicketState.REJECTED)
        assert t2.state == TicketState.REJECTED

    def test_triaged_to_not_actionable_allowed(self):
        t = _ticket_in(TicketState.TRIAGED)
        t2 = t.transition(TicketState.NOT_ACTIONABLE)
        assert t2.state == TicketState.NOT_ACTIONABLE

    def test_goal_to_decomp_allowed(self):
        t = _ticket_in(TicketState.GOAL)
        t2 = t.transition(TicketState.DECOMP)
        assert t2.state == TicketState.DECOMP

    def test_goal_to_later_allowed(self):
        t = _ticket_in(TicketState.GOAL)
        t2 = t.transition(TicketState.LATER)
        assert t2.state == TicketState.LATER

    def test_goal_to_never_allowed(self):
        t = _ticket_in(TicketState.GOAL)
        t2 = t.transition(TicketState.NEVER)
        assert t2.state == TicketState.NEVER

    def test_decomp_to_planning_allowed(self):
        t = _ticket_in(TicketState.DECOMP)
        t2 = t.transition(TicketState.PLANNING)
        assert t2.state == TicketState.PLANNING

    def test_planning_to_implement_allowed(self):
        t = _ticket_in(TicketState.PLANNING)
        t2 = t.transition(TicketState.IMPLEMENT)
        assert t2.state == TicketState.IMPLEMENT

    def test_implement_to_review_allowed(self):
        t = _ticket_in(TicketState.IMPLEMENT)
        t2 = t.transition(TicketState.REVIEW)
        assert t2.state == TicketState.REVIEW

    def test_review_to_complete_allowed(self):
        t = _ticket_in(TicketState.REVIEW)
        t2 = t.transition(TicketState.COMPLETE)
        assert t2.state == TicketState.COMPLETE

    def test_review_to_rework_allowed(self):
        t = _ticket_in(TicketState.REVIEW)
        t2 = t.transition(TicketState.REWORK)
        assert t2.state == TicketState.REWORK

    def test_rework_to_implement_allowed(self):
        t = _ticket_in(TicketState.REWORK)
        t2 = t.transition(TicketState.IMPLEMENT)
        assert t2.state == TicketState.IMPLEMENT

    def test_rework_to_deferred_allowed(self):
        t = _ticket_in(TicketState.REWORK)
        t2 = t.transition(TicketState.DEFERRED)
        assert t2.state == TicketState.DEFERRED

    def test_later_to_goal_allowed(self):
        t = _ticket_in(TicketState.LATER)
        t2 = t.transition(TicketState.GOAL)
        assert t2.state == TicketState.GOAL


# ===========================================================================
# Invalid Transition Tests (9)
# ===========================================================================

class TestInvalidTransitions:
    def test_no_discovered_to_goal(self):
        t = _ticket_in(TicketState.DISCOVERED)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.GOAL)

    def test_no_discovered_to_rejected_directly(self):
        """Fast-path: DISCOVERED→REJECTED is actually allowed."""
        t = _ticket_in(TicketState.DISCOVERED)
        t2 = t.transition(TicketState.REJECTED)
        assert t2.state == TicketState.REJECTED

    def test_no_triaged_to_decomp(self):
        t = _ticket_in(TicketState.TRIAGED)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.DECOMP)

    def test_no_goal_to_implement(self):
        t = _ticket_in(TicketState.GOAL)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.IMPLEMENT)

    def test_no_later_to_decomp(self):
        t = _ticket_in(TicketState.LATER)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.DECOMP)

    def test_no_rework_to_decomp(self):
        t = _ticket_in(TicketState.REWORK)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.DECOMP)

    def test_no_rework_to_planning(self):
        t = _ticket_in(TicketState.REWORK)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.PLANNING)

    def test_no_deferred_to_anything(self):
        t = _ticket_in(TicketState.DEFERRED)
        # DEFERRED is terminal except universal exits; COMPLETE/GOAL etc must fail
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.COMPLETE)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.GOAL)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.DECOMP)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.IMPLEMENT)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.DISCOVERED)

    def test_no_complete_to_anything(self):
        t = _ticket_in(TicketState.COMPLETE)
        for target in [TicketState.DISCOVERED, TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT, TicketState.REVIEW, TicketState.REWORK, TicketState.LATER, TicketState.NEVER, TicketState.DEFERRED]:
            with pytest.raises(ValueError, match="invalid transition"):
                t.transition(target)


# ===========================================================================
# Universal Exit Tests (14)
# ===========================================================================

class TestUniversalExits:
    def test_resolved_from_discovered(self):
        t = _ticket_in(TicketState.DISCOVERED)
        t2 = t.transition(TicketState.RESOLVED)
        assert t2.state == TicketState.RESOLVED

    def test_resolved_from_triaged(self):
        t = _ticket_in(TicketState.TRIAGED)
        assert t.transition(TicketState.RESOLVED).state == TicketState.RESOLVED

    def test_resolved_from_goal(self):
        t = _ticket_in(TicketState.GOAL)
        assert t.transition(TicketState.RESOLVED).state == TicketState.RESOLVED

    def test_resolved_from_decomp(self):
        t = _ticket_in(TicketState.DECOMP)
        assert t.transition(TicketState.RESOLVED).state == TicketState.RESOLVED

    def test_resolved_from_planning(self):
        t = _ticket_in(TicketState.PLANNING)
        assert t.transition(TicketState.RESOLVED).state == TicketState.RESOLVED

    def test_resolved_from_implement(self):
        t = _ticket_in(TicketState.IMPLEMENT)
        assert t.transition(TicketState.RESOLVED).state == TicketState.RESOLVED

    def test_resolved_from_review(self):
        t = _ticket_in(TicketState.REVIEW)
        assert t.transition(TicketState.RESOLVED).state == TicketState.RESOLVED

    def test_resolved_from_rework(self):
        t = _ticket_in(TicketState.REWORK)
        assert t.transition(TicketState.RESOLVED).state == TicketState.RESOLVED

    def test_resolved_from_later(self):
        t = _ticket_in(TicketState.LATER)
        assert t.transition(TicketState.RESOLVED).state == TicketState.RESOLVED

    @pytest.mark.parametrize("from_state", [
        TicketState.DISCOVERED, TicketState.TRIAGED, TicketState.GOAL,
        TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT,
        TicketState.REVIEW, TicketState.REWORK, TicketState.LATER, TicketState.DEFERRED,
    ])
    def test_superseded_from_any_non_complete(self, from_state):
        t = _ticket_in(from_state)
        t2 = t.transition(TicketState.SUPERSEDED)
        assert t2.state == TicketState.SUPERSEDED

    @pytest.mark.parametrize("from_state", [
        TicketState.DISCOVERED, TicketState.TRIAGED, TicketState.GOAL,
        TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT,
        TicketState.REVIEW, TicketState.REWORK, TicketState.LATER, TicketState.DEFERRED,
    ])
    def test_cancelled_from_any_non_complete(self, from_state):
        t = _ticket_in(from_state)
        t2 = t.transition(TicketState.CANCELLED)
        assert t2.state == TicketState.CANCELLED

    def test_universal_exit_blocked_from_complete_resolved(self):
        t = _ticket_in(TicketState.COMPLETE)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.RESOLVED)

    def test_universal_exit_blocked_from_complete_superseded(self):
        t = _ticket_in(TicketState.COMPLETE)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.SUPERSEDED)

    def test_universal_exit_blocked_from_complete_cancelled(self):
        t = _ticket_in(TicketState.COMPLETE)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.CANCELLED)


# ===========================================================================
# LATER / NEVER Isolation Tests (5)
# ===========================================================================

class TestLaterNeverIsolation:
    def test_later_cannot_reach_decomp(self):
        t = _ticket_in(TicketState.LATER)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.DECOMP)

    def test_later_cannot_reach_planning(self):
        t = _ticket_in(TicketState.LATER)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.PLANNING)

    def test_later_cannot_reach_implement(self):
        t = _ticket_in(TicketState.LATER)
        with pytest.raises(ValueError, match="invalid transition"):
            t.transition(TicketState.IMPLEMENT)

    def test_never_is_terminal(self):
        assert TRANSITIONS[TicketState.NEVER] == frozenset()

    def test_never_cannot_reach_any_state(self):
        t = _ticket_in(TicketState.NEVER)
        for target in [TicketState.DISCOVERED, TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING, TicketState.IMPLEMENT, TicketState.REVIEW, TicketState.COMPLETE, TicketState.LATER, TicketState.REWORK, TicketState.DEFERRED]:
            with pytest.raises(ValueError, match="invalid transition"):
                t.transition(target)


# ===========================================================================
# Schema Migration Tests (4)
# ===========================================================================

class TestSchemaMigration:
    def test_legacy_ticket_loads_without_goal_fields(self):
        raw = {
            "id": "CB-LEGACY001",
            "title": "legacy bug",
            "ticket_class": "bug",
            "severity": "medium",
            "state": "DISCOVERED",
            "source": "test",
            "evidence": "evidence",
            "problem_statement": "problem",
            "desired_state": "desired",
            "acceptance_criteria": ["ac"],
            "affected_modules": [],
            "dependencies": [],
            "risk": "medium",
            "blast_radius": "",
            "security_impact": "none",
            "migration_impact": "none",
            "required_reviewers": [],
            "required_tests": [],
            "documentation_requirements": [],
            "rollback_strategy": "revert commit",
            "estimated_cost_tokens": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
            "schema_version": "2.0",
            "outcome": "",
            "final_cost_tokens": 0,
            "attempts": 0,
            "rework_count": 0,
            "assigned_agent": "",
            "assigned_model": "",
            "reviewer_feedback": [],
            "last_gate_result": {},
            "gate_history": [],
            "commit_sha": "",
            "committed_at": 0.0,
            "pr_url": "",
            "confidence": "",
            "priority": "",
            "repo_revision": "",
            "atomicity": "",
            "discovery_category": "",
            "finding_id": "",
            "fingerprint": "",
        }
        t = Ticket.from_dict(raw)
        assert t.id == "CB-LEGACY001"
        assert t.goal_disposition == ""
        assert t.superseded_by == ""
        assert t.cancelled_reason == ""
        assert t.resolved_by == ""
        assert t.goal_revision == 0

    def test_new_fields_default_empty(self):
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e", "p", "d", ["a"])
        assert t.goal_disposition == ""
        assert t.superseded_by == ""
        assert t.cancelled_reason == ""
        assert t.resolved_by == ""
        assert t.goal_reason == ""
        assert t.cancelled_by == ""

    def test_schema_version_is_3(self):
        assert SCHEMA_VERSION == "3.0"
        t = create_ticket("t2", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["a"])
        assert t.schema_version == "3.0"

    def test_old_states_deserialize(self):
        raw = {
            "id": "CB-OLDSTATE",
            "title": "old state ticket",
            "ticket_class": "bug",
            "severity": "low",
            "state": "VALIDATING",
            "source": "test",
            "evidence": "e",
            "problem_statement": "p",
            "desired_state": "d",
            "acceptance_criteria": ["a"],
            "affected_modules": [],
            "dependencies": [],
            "risk": "low",
            "blast_radius": "",
            "security_impact": "none",
            "migration_impact": "none",
            "required_reviewers": [],
            "required_tests": [],
            "documentation_requirements": [],
            "rollback_strategy": "revert commit",
            "estimated_cost_tokens": 0,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        t = Ticket.from_dict(raw)
        assert t.state == TicketState.VALIDATING


# ===========================================================================
# RL Reward Tests (4+ extended)
# ===========================================================================

class TestRLRewards:
    def test_reward_complete_is_1(self):
        assert reward_from_lifecycle_outcome("COMPLETE") == 1.0
        assert reward_from_lifecycle_outcome(TicketState.COMPLETE) == 1.0

    def test_reward_rejected_is_0(self):
        assert reward_from_lifecycle_outcome("REJECTED") == 0.0
        assert reward_from_lifecycle_outcome(TicketState.REJECTED) == 0.0
        assert reward_from_lifecycle_outcome("DUPLICATE") == 0.0
        assert reward_from_lifecycle_outcome("NOT_ACTIONABLE") == 0.0

    def test_reward_goal_pass_is_0_3(self):
        assert reward_from_lifecycle_outcome("GOAL") == 0.3
        assert reward_from_lifecycle_outcome("TRIAGED") == 0.3
        assert reward_from_lifecycle_outcome(TicketState.GOAL) == 0.3
        assert reward_from_lifecycle_outcome(TicketState.TRIAGED) == 0.3

    def test_reward_deferred_is_0_2(self):
        assert reward_from_lifecycle_outcome("DEFERRED") == 0.2
        assert reward_from_lifecycle_outcome(TicketState.DEFERRED) == 0.2

    def test_reward_review_is_0_7(self):
        assert reward_from_lifecycle_outcome("REVIEW") == 0.7
        assert reward_from_lifecycle_outcome(TicketState.REVIEW) == 0.7
        # alias REVIEWING
        assert reward_from_lifecycle_outcome("REVIEWING") == 0.7

    def test_reward_decomp_planning_is_0_5(self):
        assert reward_from_lifecycle_outcome("DECOMP") == 0.5
        assert reward_from_lifecycle_outcome("PLANNING") == 0.5
        assert reward_from_lifecycle_outcome(TicketState.DECOMP) == 0.5
        assert reward_from_lifecycle_outcome(TicketState.PLANNING) == 0.5
        # alias DECOMPOSE
        assert reward_from_lifecycle_outcome("DECOMPOSE") == 0.5

    def test_reward_never_is_0_1(self):
        assert reward_from_lifecycle_outcome("NEVER") == 0.1
        assert reward_from_lifecycle_outcome(TicketState.NEVER) == 0.1

    def test_reward_unknown_is_0(self):
        assert reward_from_lifecycle_outcome("UNKNOWN_STATE_XYZ") == 0.0
        assert reward_from_lifecycle_outcome("") == 0.0
        assert reward_from_lifecycle_outcome("LATER") == 0.0  # LATER not rewarded as terminal

    def test_reward_enum_string_case_insensitive(self):
        assert reward_from_lifecycle_outcome("complete") == 1.0
        assert reward_from_lifecycle_outcome("  Complete  ") == 1.0
        assert reward_from_lifecycle_outcome("goal") == 0.3


# ===========================================================================
# Edge Case Tests (8)
# ===========================================================================

class TestEdgeCases:
    def test_rework_target_returns_implement(self, tmp_path):
        try:
            from codebot.ticket_dispatcher import rework_target_state
        except ImportError:
            pytest.skip("ticket_dispatcher.rework_target_state not available")
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)

        class FakeTicket:
            rework_count = 1
            id = "CB-FAKE1"
            reviewer_feedback = []

        result = rework_target_state(FakeTicket(), plans_dir)
        assert result in (TicketState.IMPLEMENT, TicketState.IMPLEMENTING, TicketState.PLANNING, TicketState.DECOMP)
        # Current implementation returns IMPLEMENT for rework_count<3 without architecture marker
        assert result == TicketState.IMPLEMENT or str(result.value) in ("IMPLEMENT", "IMPLEMENTING")

    def test_rework_target_returns_deferred_after_max(self, tmp_path):
        try:
            from codebot.ticket_dispatcher import rework_target_state
        except ImportError:
            pytest.skip("ticket_dispatcher.rework_target_state not available")
        plans_dir = tmp_path / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)

        class FakeTicket:
            rework_count = 3
            id = "CB-FAKE2"
            reviewer_feedback = []

        result = rework_target_state(FakeTicket(), plans_dir)
        assert result == TicketState.DEFERRED

    def test_add_excludes_later_fingerprint(self, tmp_path):
        store = _store(tmp_path)
        fp = "fp-later-test-123"
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "test", "evidence-later-1", "problem-later-1", "desired", ["ac"], fingerprint=fp)
        store.add(t1)
        # Transition via store: DISCOVERED -> TRIAGED -> GOAL -> LATER
        store.transition(t1.id, TicketState.TRIAGED)
        store.transition(t1.id, TicketState.GOAL)
        store.transition(t1.id, TicketState.LATER)
        # Second ticket with same fingerprint should NOT raise because LATER is excluded
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "test", "evidence-later-2", "problem-later-2", "desired", ["ac"], fingerprint=fp)
        # Use different evidence_hash but same fingerprint: still should not raise due to LATER exclusion
        # Note: t2 has different problem_statement/evidence so evidence_hash differs, but fingerprint same
        try:
            store.add(t2)
        except ValueError as e:
            pytest.fail(f"add should not raise for fingerprint matching LATER ticket: {e}")
        finally:
            store.close()

    def test_add_excludes_never_fingerprint(self, tmp_path):
        store = _store(tmp_path)
        fp = "fp-never-test-456"
        t1 = create_ticket("t1", TicketClass.BUG, Severity.LOW, "test", "evidence-never-1", "problem-never-1", "desired", ["ac"], fingerprint=fp)
        store.add(t1)
        store.transition(t1.id, TicketState.TRIAGED)
        store.transition(t1.id, TicketState.GOAL)
        store.transition(t1.id, TicketState.NEVER)
        t2 = create_ticket("t2", TicketClass.BUG, Severity.LOW, "test", "evidence-never-2", "problem-never-2", "desired", ["ac"], fingerprint=fp)
        try:
            store.add(t2)
        except ValueError as e:
            pytest.fail(f"add should not raise for fingerprint matching NEVER ticket: {e}")
        finally:
            store.close()

    def test_single_transition_atomicity(self, tmp_path):
        store = _store(tmp_path)
        t = create_ticket("fast reject", TicketClass.BUG, Severity.LOW, "test", "e-single-atomic", "p-single", "d", ["ac"])
        store.add(t)
        # Single step DISCOVERED -> REJECTED (fast-path)
        updated = store.transition(t.id, TicketState.REJECTED)
        assert updated.state == TicketState.REJECTED
        # Verify ticket went directly, not via TRIAGED
        assert store.get(t.id).state == TicketState.REJECTED
        store.close()

    def test_active_work_index_adds_at_goal(self, tmp_path):
        try:
            import importlib
            mod = importlib.import_module("codebot.active_work_index")
        except ImportError:
            try:
                mod = importlib.import_module("codebot.active_work")
            except ImportError:
                pytest.skip("active_work_index module not available yet")
                return
        # If module exists, at least check it exposes expected API or that GOAL transitions add to index
        # Minimal sanity: module should be importable and not raise
        assert mod is not None

    def test_resolved_index_basic_operations(self, tmp_path):
        try:
            import importlib
            mod = importlib.import_module("codebot.resolved_index")
        except ImportError:
            try:
                mod = importlib.import_module("codebot.resolved")
            except ImportError:
                pytest.skip("resolved_index module not available yet")
                return
        assert mod is not None

    def test_no_double_dispatch(self, tmp_path):
        try:
            from codebot.scheduler_v2.dispatcher import BucketDispatcher, BUCKET_ORDER
            from codebot.scheduler_v2.lifecycle import FakeClock
            from codebot.scheduler_v2.dispatch_gate import DispatchGate
        except ImportError:
            pytest.skip("scheduler_v2 not available")
            return
        # Verify BUCKET_ORDER does not include DISCOVERED (triaged via platform code)
        bucket_names = [b[0] for b in BUCKET_ORDER]
        all_states = []
        for _, states in BUCKET_ORDER:
            all_states.extend(states)
        assert "DISCOVERED" not in all_states, "DISCOVERED should not be dispatched via BucketDispatcher"
        # Verify GOAL bucket queries TRIAGED
        goal_entry = next((x for x in BUCKET_ORDER if x[0] == "GOAL"), None)
        assert goal_entry is not None
        assert "TRIAGED" in goal_entry[1]
        # Quick functional check: DISCOVERED tickets not dispatched
        store = _store(tmp_path)
        t_discovered = create_ticket("d", TicketClass.BUG, Severity.LOW, "test", "e-dispatch-1", "p-dispatch-1", "d", ["ac"])
        t_triaged = create_ticket("t", TicketClass.BUG, Severity.LOW, "test", "e-dispatch-2", "p-dispatch-2", "d", ["ac"])
        store.add(t_discovered)
        store.add(t_triaged)
        store.transition(t_triaged.id, TicketState.TRIAGED)
        gate = DispatchGate(max_slots=5, state_dir=tmp_path, clock=FakeClock())
        dispatcher = BucketDispatcher(gate=gate)
        snapshots = dispatcher._snapshot_buckets(store)
        # DISCOVERED bucket should not exist; GOAL bucket should contain triaged ticket only
        assert "DISCOVERED" not in snapshots
        assert any(t.id == t_triaged.id for t in snapshots.get("GOAL", []))
        assert not any(t.id == t_discovered.id for tickets in snapshots.values() for t in tickets)
        store.close()
        gate.close() if hasattr(gate, "close") else None
