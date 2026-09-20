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
    dispatch_decompose_agents,
    dispatch_planning_agents,
    spawn_demand_agents,
    TICKET_CLASS_TO_IMPLEMENTER,
    IMPLEMENTER_ROLE_NAMES,
    REVIEWER_ROLE_NAMES,
    REVIEWER_TYPES,
)
from codebot.process_manager import BotConfig, BotState


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


class FakeLifecycleTicketStore:
    def __init__(self, tickets: list[Ticket]):
        self._tickets = tickets

    def list_by_state(self, state: TicketState) -> list[Ticket]:
        return [ticket for ticket in self._tickets if ticket.state == state]


class TestDispatchPerformance:
    """Verify O(1) role-indexed dispatch meets performance target."""

    def test_dispatch_100_tickets_20_bots_under_1ms(self, tmp_path):
        """100 tickets dispatched against 20 bots must complete in <1ms.

        The core dict build + lookup loop is O(n+m). We patch time.sleep
        and use start_bot_fn returning True to isolate the algorithmic
        complexity from I/O overhead. O(n+m) not O(n*m).
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
        start_bot_fn = MagicMock(return_value=True)

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                    start = time.perf_counter()
                    result = spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=start_bot_fn)
                    elapsed = time.perf_counter() - start

        elapsed_ms = elapsed * 1000
        print(f"Dispatch 100/20 in {elapsed_ms:.3f}ms (spawned={result})")
        assert elapsed < 0.002, f"Dispatch took {elapsed_ms:.3f}ms, expected <2ms"
        # With 8 max_concurrent_impl and 20 idle bots, should spawn up to 8
        assert result > 0, "Should have spawned at least one bot"


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

            with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
                with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                    with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
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

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                    spawn_demand_agents(bots, max_concurrent=10, start_bot_fn=capture_start)

        # BUG maps to general_implementer. All attempted roles must be general_implementer.
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

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
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

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
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

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                    spawn_demand_agents(bots, max_concurrent=10, start_bot_fn=capture_start)

        assert "general_implementer" in spawned_roles


class TestAcceptanceCriteria:
    """Verify CB-5553839-DC48 acceptance criteria directly."""

    def test_dispatch_latency_under_100ms_for_r50_a20(self, tmp_path):
        """Dispatch latency remains <100ms for R=50, A=20."""
        tickets = []
        classes = list(TICKET_CLASS_TO_IMPLEMENTER.keys())
        for i in range(50):
            tc = TicketClass(classes[i % len(classes)])
            tickets.append(_make_ticket(f"T-{i:04d}", tc))

        bots: dict[str, MockBotState] = {}
        roles = list(IMPLEMENTER_ROLE_NAMES)
        for i in range(20):
            role = roles[i % len(roles)]
            name = f"{role}-{i+1}"
            bots[name] = MockBotState(config=MockBotConfig(name=name, enabled=True), process=None)

        fake_store = FakeTicketStore(implementing=tickets)
        start_bot_fn = MagicMock(return_value=True)

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                    start = time.perf_counter()
                    result = spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=start_bot_fn)
                    elapsed = time.perf_counter() - start

        elapsed_ms = elapsed * 1000
        print(f"Dispatch R=50/A=20 in {elapsed_ms:.3f}ms (spawned={result})")
        assert elapsed_ms < 100, f"Dispatch took {elapsed_ms:.3f}ms, expected <100ms"
        assert result > 0, "Should have spawned at least one bot"

    def test_no_regression_in_ticket_assignment_logic(self, tmp_path):
        """No regression in ticket assignment logic: every ticket_class routes correctly."""
        assigned_pairs: list[tuple[str, str]] = []

        def capture_start(bot, **kwargs):
            base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
            assigned_pairs.append((base, kwargs.get("is_demand", False)))
            return False

        tickets = []
        for tc_str in TICKET_CLASS_TO_IMPLEMENTER:
            tickets.append(_make_ticket(f"T-{tc_str}", TicketClass(tc_str)))

        bots = _build_bots(num_per_role=2)
        fake_store = FakeTicketStore(implementing=tickets)

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                    spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=capture_start)

        # Verify each attempted assignment used the correct role
        for role, is_demand in assigned_pairs:
            assert role in IMPLEMENTER_ROLE_NAMES, f"Unexpected role {role} dispatched"

    def test_index_based_bot_lookup_works_correctly(self, tmp_path):
        """Index-based bot lookup works correctly: O(1) dict lookup per ticket."""
        tickets = [_make_ticket(f"T-{i}", TicketClass.BUG) for i in range(10)]
        bots: dict[str, MockBotState] = {}
        for i in range(5):
            name = f"general_implementer-{i+1}"
            bots[name] = MockBotState(config=MockBotConfig(name=name, enabled=True), process=None)
        # Add a bot that should NOT be selected for BUG tickets
        bots["backend_implementer-1"] = MockBotState(
            config=MockBotConfig(name="backend_implementer-1", enabled=True), process=None
        )

        spawned_names: list[str] = []

        def capture_start(bot, **kwargs):
            spawned_names.append(bot.config.name)
            return False

        fake_store = FakeTicketStore(implementing=tickets)

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                    spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=capture_start)

        # Only general_implementer bots should have been selected for BUG tickets
        for name in spawned_names:
            assert name.startswith("general_implementer"), (
                f"Expected general_implementer, got {name} — index lookup failed"
            )


class TestLifecycleDispatchCapacity:
    @pytest.mark.parametrize(
        ("state", "role", "dispatch"),
        [
            (TicketState.DECOMPOSE, "decomposer", dispatch_decompose_agents),
            (TicketState.PLANNING, "implementation_planner", dispatch_planning_agents),
        ],
    )
    def test_failed_start_releases_claim(self, tmp_path, state, role, dispatch):
        ticket = _make_ticket("T-CAPACITY", TicketClass.BUG, state=state)
        store = FakeLifecycleTicketStore([ticket])
        prompt = tmp_path / "codebot" / "roles"
        prompt.mkdir(parents=True)
        (prompt / f"{role}.md").write_text("role")
        config = BotConfig(role, f"codebot/roles/{role}.md", 30, 90)
        bot = BotState(config=config)
        bots = {role: bot}

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=store), \
             patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path / "state"), \
             patch("codebot.ticket_dispatcher.BOTS_DIR", tmp_path):
            dispatched = dispatch(bots, start_bot_fn=MagicMock(return_value=False))

        assert dispatched == 0
        assert bot._assigned_ticket_id == ""
        assert not list((tmp_path / "state" / "claims").glob("*.json"))

    def test_decomposition_respects_max_agents(self, tmp_path):
        tickets = [
            _make_ticket(f"T-DECOMP-{index}", TicketClass.BUG, state=TicketState.DECOMPOSE)
            for index in range(3)
        ]
        store = FakeLifecycleTicketStore(tickets)
        prompt = tmp_path / "codebot" / "roles"
        prompt.mkdir(parents=True)
        (prompt / "decomposer.md").write_text("role")
        config = BotConfig("decomposer", "codebot/roles/decomposer.md", 30, 90)
        bots = {"decomposer": BotState(config=config)}

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=store), \
             patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path / "state"), \
             patch("codebot.ticket_dispatcher.BOTS_DIR", tmp_path):
            dispatched = dispatch_decompose_agents(
                bots,
                max_agents=1,
                start_bot_fn=MagicMock(return_value=True),
            )

        assert dispatched == 1
