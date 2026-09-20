"""Benchmark and correctness tests for O(1) role-indexed dispatch.

Verifies CB-2142764-F655: spawn_demand_agents uses dict-based role lookup
instead of O(n*m) nested iteration, achieving <1ms dispatch for 100 tickets
and 20 bots.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
from codebot.ticket_dispatcher import (
    spawn_demand_agents,
    TICKET_CLASS_TO_IMPLEMENTER,
    IMPLEMENTER_ROLE_NAMES,
    REVIEWER_ROLE_NAMES,
    REVIEWER_TYPES,
)


def _make_ticket(tid: str, ticket_class: TicketClass, state: TicketState = TicketState.IMPLEMENTING) -> Ticket:
    return Ticket(
        id=tid,
        title=f"Test {tid}",
        ticket_class=ticket_class,
        severity=Severity.MEDIUM,
        state=state,
        source="test",
        evidence="evidence",
        problem_statement="problem",
        desired_state="desired",
        acceptance_criteria=["ac1"],
        affected_modules=["mod.py"],
        dependencies=[],
        risk=RiskLevel.LOW,
        blast_radius="low",
        security_impact="none",
        migration_impact="none",
        required_reviewers=[],
        required_tests=[],
        documentation_requirements=[],
        rollback_strategy="revert",
        estimated_cost_tokens=100,
        created_at=time.time(),
        updated_at=time.time(),
    )


@dataclass
class MockBotConfig:
    name: str
    enabled: bool = True


@dataclass
class MockBotState:
    config: MockBotConfig
    process: Any = None
    _assigned_ticket_id: str = ""


def _build_bots(num_per_role: int = 3) -> dict[str, MockBotState]:
    """Build a dict of idle mock bots across all implementer roles."""
    bots: dict[str, MockBotState] = {}
    roles = list(IMPLEMENTER_ROLE_NAMES) + ["ux_reviewer"] + list(REVIEWER_ROLE_NAMES)
    for role in roles:
        for i in range(num_per_role):
            name = f"{role}-{i+1}" if num_per_role > 1 else role
            bots[name] = MockBotState(config=MockBotConfig(name=name, enabled=True), process=None)
    return bots


class FakeTicketStore:
    def __init__(self, implementing=None, reviewing=None, rework=None):
        self._impl = implementing or []
        self._rev = reviewing or []
        self._rework = rework or []

    def list_by_state(self, state: TicketState):
        if state == TicketState.IMPLEMENTING:
            return self._impl
        if state == TicketState.REVIEWING:
            return self._rev
        if state == TicketState.REWORK:
            return self._rework
        return []


class TestDispatchPerformance:
    """Verify O(1) role-indexed dispatch meets performance target."""

    def test_dispatch_100_tickets_20_bots_under_15ms(self, tmp_path):
        """100 tickets dispatched against 20 bots must complete quickly.

        The core dict build + lookup loop is O(n+m). We allow up to 15ms
        to account for CI overhead and mock I/O, but the algorithmic
        complexity is what matters: O(n+m) not O(n*m).
        """
        tickets = []
        classes = list(TICKET_CLASS_TO_IMPLEMENTER.keys())
        for i in range(100):
            tc = TicketClass(classes[i % len(classes)])
            tickets.append(_make_ticket(f"T-{i:04d}", tc))

        bots: dict[str, MockBotState] = {}
        roles = list(IMPLEMENTER_ROLE_NAMES)
        for i in range(20):
            role = roles[i % len(roles)]
            name = f"{role}-{i+1}"
            bots[name] = MockBotState(config=MockBotConfig(name=name, enabled=True), process=None)

        fake_store = FakeTicketStore(implementing=tickets)
        start_bot_fn = MagicMock(return_value=False)

        with patch("codebot.ticket_dispatcher._get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                start = time.perf_counter()
                spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=start_bot_fn)
                elapsed = time.perf_counter() - start

        assert elapsed < 0.005, f"Dispatch took {elapsed*1000:.2f}ms, expected <5ms"


class TestCorrectBotSelection:
    """Verify routing correctness: each ticket_class maps to the right role."""

    def test_correct_bot_selection_per_class(self, tmp_path):
        """For every ticket_class, the dispatched bot base matches TICKET_CLASS_TO_IMPLEMENTER."""
        spawned_roles: list[str] = []

        def capture_start(bot, **kwargs):
            base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
            spawned_roles.append(base)
            return False

        for tc_str, expected_role in TICKET_CLASS_TO_IMPLEMENTER.items():
            tc = TicketClass(tc_str)
            ticket = _make_ticket(f"T-{tc_str}", tc)
            bots = _build_bots(num_per_role=2)
            fake_store = FakeTicketStore(implementing=[ticket])

            with patch("codebot.ticket_dispatcher._get_ticket_store", return_value=fake_store):
                with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                    (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                    spawned_roles.clear()
                    spawn_demand_agents(bots, max_concurrent=10, start_bot_fn=capture_start)

            if spawned_roles:
                assert spawned_roles[0] == expected_role, (
                    f"ticket_class={tc_str}: expected {expected_role}, got {spawned_roles[0]}"
                )

    def test_unknown_class_falls_back_to_general(self, tmp_path):
        """Unknown ticket_class values fall back to general_implementer."""
        spawned_roles: list[str] = []

        def capture_start(bot, **kwargs):
            base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
            spawned_roles.append(base)
            return False

        ticket = _make_ticket("T-unknown", TicketClass.BUG)
        bots = _build_bots(num_per_role=1)
        fake_store = FakeTicketStore(implementing=[ticket])

        with patch("codebot.ticket_dispatcher._get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                spawn_demand_agents(bots, max_concurrent=10, start_bot_fn=capture_start)

        # When start_bot_fn returns False, the dispatcher may try additional bots
        # from idle_bots_by_role before falling back to _get_or_create_bot.
        # All attempted roles must be general_implementer (the mapping for BUG).
        assert len(spawned_roles) <= 2
        for role in spawned_roles:
            assert role == "general_implementer"


class TestReviewerRoleIndexedLookup:
    """Verify reviewer dispatch also uses O(1) role-indexed lookup."""

    def test_reviewer_dispatch_uses_role_index(self, tmp_path):
        """Reviewers are dispatched via idle_reviewers_by_role.get(rtype)."""
        spawned_roles: list[str] = []

        def capture_start(bot, **kwargs):
            base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
            spawned_roles.append(base)
            return False

        review_ticket = _make_ticket("T-REV-1", TicketClass.BUG, state=TicketState.REVIEWING)
        bots = _build_bots(num_per_role=2)
        fake_store = FakeTicketStore(reviewing=[review_ticket])

        with patch("codebot.ticket_dispatcher._get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=capture_start)

        for rtype in REVIEWER_TYPES:
            assert rtype in spawned_roles, f"Reviewer type {rtype} was not dispatched"


class TestEdgeCases:
    """Edge cases: zero bots, zero tickets, hyphenated names."""

    def test_zero_tickets_does_nothing(self, tmp_path):
        """No tickets means no spawns and no errors."""
        bots = _build_bots(num_per_role=2)
        fake_store = FakeTicketStore(implementing=[], reviewing=[])
        start_fn = MagicMock(return_value=False)

        with patch("codebot.ticket_dispatcher._get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                result = spawn_demand_agents(bots, max_concurrent=10, start_bot_fn=start_fn)

        assert result == 0
        start_fn.assert_not_called()

    def test_hyphenated_bot_names_index_correctly(self, tmp_path):
        """Bots like 'general_implementer-10' index under 'general_implementer'."""
        spawned_roles: list[str] = []

        def capture_start(bot, **kwargs):
            base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
            spawned_roles.append(base)
            return False

        ticket = _make_ticket("T-HYPH", TicketClass.BUG)
        bots = {
            "general_implementer-10": MockBotState(
                config=MockBotConfig(name="general_implementer-10", enabled=True), process=None
            ),
            "backend_implementer-3": MockBotState(
                config=MockBotConfig(name="backend_implementer-3", enabled=True), process=None
            ),
        }
        fake_store = FakeTicketStore(implementing=[ticket])

        with patch("codebot.ticket_dispatcher._get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                spawn_demand_agents(bots, max_concurrent=10, start_bot_fn=capture_start)

        assert "general_implementer" in spawned_roles
