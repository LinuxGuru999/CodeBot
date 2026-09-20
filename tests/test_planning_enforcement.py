"""Tests for planning prerequisite enforcement in ticket state machine.

Ticket: CB-5543897-A22F
Verifies that tickets with medium+ risk cannot transition from READY to
IMPLEMENTING without an implementation plan, and that the risk threshold
is configurable.
"""

import json
import pytest
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.ticket_engine import (
    Ticket,
    TicketState,
    TicketClass,
    Severity,
    RiskLevel,
    create_ticket,
    TicketStore,
)
from codebot.implementation_planner import PlanStore, generate_plan


def advance_to_ready(store: TicketStore, ticket_id: str) -> None:
    """Public helper to move a ticket through DISCOVERED -> VALIDATING -> TRIAGED -> READY."""
    store.transition(ticket_id, TicketState.VALIDATING)
    store.transition(ticket_id, TicketState.TRIAGED)
    store.transition(ticket_id, TicketState.READY)


class TestPlanningPrerequisiteEnforcement:
    """Verify READY -> IMPLEMENTING requires a plan for medium+ risk."""

    def test_medium_risk_blocked_without_plan(self, tmp_path: Path) -> None:
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "medium ticket", TicketClass.FEATURE, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
            risk=RiskLevel.MEDIUM,
        )
        store.add(t)
        advance_to_ready(store, t.id)

        with pytest.raises(ValueError, match="requires an implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENTING)

    def test_high_risk_blocked_without_plan(self, tmp_path: Path) -> None:
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "high ticket", TicketClass.FEATURE, Severity.HIGH,
            "test", "ev", "prob", "desired", ["ac"],
            risk=RiskLevel.HIGH,
        )
        store.add(t)
        advance_to_ready(store, t.id)

        with pytest.raises(ValueError, match="requires an implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENTING)

    def test_critical_risk_blocked_without_plan(self, tmp_path: Path) -> None:
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "critical ticket", TicketClass.SECURITY, Severity.CRITICAL,
            "test", "ev", "prob", "desired", ["ac"],
            risk=RiskLevel.CRITICAL,
        )
        store.add(t)
        advance_to_ready(store, t.id)

        with pytest.raises(ValueError, match="requires an implementation plan"):
            store.transition(t.id, TicketState.IMPLEMENTING)

    def test_low_risk_allowed_without_plan(self, tmp_path: Path) -> None:
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "low ticket", TicketClass.BUG, Severity.LOW,
            "test", "ev", "prob", "desired", ["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t)
        advance_to_ready(store, t.id)

        # Low risk should bypass planning requirement
        updated = store.transition(t.id, TicketState.IMPLEMENTING)
        assert updated.state == TicketState.IMPLEMENTING

    def test_medium_risk_allowed_with_plan(self, tmp_path: Path) -> None:
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "planned ticket", TicketClass.FEATURE, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
            risk=RiskLevel.MEDIUM,
        )
        store.add(t)
        advance_to_ready(store, t.id)

        # Generate and save a plan
        plan = generate_plan(
            ticket_id=t.id,
            risk="medium",
            affected_modules=["codebot/test.py"],
            dependencies=[],
            acceptance_criteria=["ac"],
        )
        plan_store = PlanStore(tmp_path)
        plan_store.save(plan)

        # Now transition should succeed
        updated = store.transition(t.id, TicketState.IMPLEMENTING)
        assert updated.state == TicketState.IMPLEMENTING

    def test_error_message_includes_risk_level(self, tmp_path: Path) -> None:
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "msg ticket", TicketClass.FEATURE, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
            risk=RiskLevel.HIGH,
        )
        store.add(t)
        advance_to_ready(store, t.id)

        with pytest.raises(ValueError, match="risk=high"):
            store.transition(t.id, TicketState.IMPLEMENTING)

    def test_error_message_suggests_planning(self, tmp_path: Path) -> None:
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "suggest ticket", TicketClass.FEATURE, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
            risk=RiskLevel.MEDIUM,
        )
        store.add(t)
        advance_to_ready(store, t.id)

        with pytest.raises(ValueError, match="PLANNING"):
            store.transition(t.id, TicketState.IMPLEMENTING)

    def test_non_implementing_transitions_unaffected(self, tmp_path: Path) -> None:
        """READY -> DEFERRED should still work regardless of plan status."""
        store = TicketStore(tmp_path / "tickets.json")
        t = create_ticket(
            "defer ticket", TicketClass.FEATURE, Severity.MEDIUM,
            "test", "ev", "prob", "desired", ["ac"],
            risk=RiskLevel.MEDIUM,
        )
        store.add(t)
        advance_to_ready(store, t.id)

        # DEFERRED is allowed from READY without a plan
        updated = store.transition(t.id, TicketState.DEFERRED)
        assert updated.state == TicketState.DEFERRED


class TestRiskThresholdConfigurable:
    """Verify the risk threshold for planning enforcement is configurable."""

    def test_default_threshold_is_medium(self) -> None:
        from codebot.ticket_engine import get_min_risk_for_planning
        assert get_min_risk_for_planning() == RiskLevel.MEDIUM

    def test_threshold_change_affects_enforcement(self, tmp_path: Path) -> None:
        """When threshold is raised to HIGH, medium-risk tickets pass without plan."""
        import codebot.ticket_engine as te
        try:
            te.set_min_risk_for_planning(RiskLevel.HIGH)

            store = TicketStore(tmp_path / "tickets.json")
            t = create_ticket(
                "threshold ticket", TicketClass.FEATURE, Severity.MEDIUM,
                "test", "ev", "prob", "desired", ["ac"],
                risk=RiskLevel.MEDIUM,
            )
            store.add(t)
            advance_to_ready(store, t.id)

            # With threshold at HIGH, MEDIUM risk should pass
            updated = store.transition(t.id, TicketState.IMPLEMENTING)
            assert updated.state == TicketState.IMPLEMENTING
        finally:
            te.set_min_risk_for_planning(RiskLevel.MEDIUM)
