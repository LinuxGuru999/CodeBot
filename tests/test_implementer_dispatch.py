"""Regression tests for implementer dispatch pipeline.

Verifies that spawn_demand_agents correctly assigns tickets to implementer
bots across all ticket classes, prunes stale dynamic workers on failure,
and distributes budget fairly across roles.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from codebot.ticket_engine import Ticket, TicketClass, TicketState, Severity, RiskLevel
from codebot.ticket_dispatcher import (
    spawn_demand_agents,
    TICKET_CLASS_TO_IMPLEMENTER,
    IMPLEMENTER_ROLE_NAMES,
)


def _make_ticket(tid: str, ticket_class: TicketClass, state: TicketState = TicketState.IMPLEMENTATION_READY) -> Ticket:
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


class FakeTicketStore:
    def __init__(self, implementing=None, reviewing=None, rework=None):
        self._impl = implementing or []
        self._rev = reviewing or []
        self._rework = rework or []
        self.transitions: list[tuple[str, TicketState, str]] = []

    def list_by_state(self, state: TicketState):
        if state in (TicketState.IMPLEMENTING, TicketState.IMPLEMENTATION_READY):
            return self._impl
        if state == TicketState.REVIEWING:
            return self._rev
        if state == TicketState.REWORK:
            return self._rework
        return []

    def transition(self, ticket_id: str, state: TicketState, actor: str = "") -> None:
        self.transitions.append((ticket_id, state, actor))

    def get(self, ticket_id: str):
        for t in self._impl + self._rev + self._rework:
            if t.id == ticket_id:
                return t
        return None


@pytest.fixture(autouse=True)
def reset_dispatch_claim_indexes():
    import codebot.ticket_dispatcher as td

    td._claim_index.clear()
    td._claims_by_ticket_id.clear()
    yield
    td._claim_index.clear()
    td._claims_by_ticket_id.clear()


class TestMultiClassDispatch:

    def test_spawn_assigns_tickets_to_correct_implementer_roles(self, tmp_path):
        tickets = [
            _make_ticket("T-BUG", TicketClass.BUG),
            _make_ticket("T-SEC", TicketClass.SECURITY),
            _make_ticket("T-TEST", TicketClass.TEST),
            _make_ticket("T-DOC", TicketClass.DOCUMENTATION),
        ]
        bots: dict[str, MockBotState] = {}
        for role in IMPLEMENTER_ROLE_NAMES:
            bots[role] = MockBotState(config=MockBotConfig(name=role, enabled=True), process=None)

        spawned_bots: list[str] = []

        def capture_start(bot, **kwargs):
            spawned_bots.append(bot.config.name)
            return True

        fake_store = FakeTicketStore(implementing=tickets)

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    with patch("codebot.ticket_dispatcher.write_implementation_packet"):
                        (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                        result = spawn_demand_agents(
                            bots, max_concurrent=100, start_bot_fn=capture_start, store=fake_store
                        )

        assert result >= 4
        assigned = {b._assigned_ticket_id for b in bots.values() if b._assigned_ticket_id}
        assert len(assigned) >= 4
        role_to_ticket = {
            b.config.name: b._assigned_ticket_id
            for b in bots.values()
            if b._assigned_ticket_id
        }
        for tid, expected_role in [("T-BUG", "general_implementer"), ("T-SEC", "backend_implementer"),
                                     ("T-TEST", "test_implementer"), ("T-DOC", "documentation_implementer")]:
            matching = [name for name, assigned_tid in role_to_ticket.items() if assigned_tid == tid]
            if matching:
                base = matching[0].split("-")[0] if "-" in matching[0] else matching[0]
                assert base == expected_role, f"{tid} assigned to {matching[0]}, expected {expected_role}"


class TestStaleDynamicBotPruning:

    def test_dynamic_bot_disabled_after_failed_spawn(self, tmp_path):
        ticket = _make_ticket("T-SEC-1", TicketClass.SECURITY)
        dynamic_name = "backend_implementer-3"
        bots: dict[str, MockBotState] = {
            dynamic_name: MockBotState(config=MockBotConfig(name=dynamic_name, enabled=True), process=None),
        }

        def failing_start(bot, **kwargs):
            return False

        fake_store = FakeTicketStore(implementing=[ticket])

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    with patch("codebot.ticket_dispatcher.write_implementation_packet"):
                        (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                        spawn_demand_agents(
                            bots, max_concurrent=100, start_bot_fn=failing_start, store=fake_store
                        )

        assert bots[dynamic_name].config.enabled is False

    def test_static_bot_not_disabled_after_failed_spawn(self, tmp_path):
        ticket = _make_ticket("T-BUG-1", TicketClass.BUG)
        static_name = "general_implementer"
        bots: dict[str, MockBotState] = {
            static_name: MockBotState(config=MockBotConfig(name=static_name, enabled=True), process=None),
        }

        def failing_start(bot, **kwargs):
            return False

        fake_store = FakeTicketStore(implementing=[ticket])

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    with patch("codebot.ticket_dispatcher.write_implementation_packet"):
                        (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                        spawn_demand_agents(
                            bots, max_concurrent=100, start_bot_fn=failing_start, store=fake_store
                        )

        assert bots[static_name].config.enabled is True


class TestBudgetFairness:

    def test_no_single_role_monopolizes_budget(self, tmp_path):
        tickets = []
        for i in range(6):
            tickets.append(_make_ticket(f"T-BUG-{i}", TicketClass.BUG))
        for i in range(6):
            tickets.append(_make_ticket(f"T-SEC-{i}", TicketClass.SECURITY))

        bots: dict[str, MockBotState] = {}
        for role in IMPLEMENTER_ROLE_NAMES:
            bots[role] = MockBotState(config=MockBotConfig(name=role, enabled=True), process=None)

        spawn_counts: dict[str, int] = {}

        def counting_start(bot, **kwargs):
            base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
            spawn_counts[base] = spawn_counts.get(base, 0) + 1
            return True

        fake_store = FakeTicketStore(implementing=tickets)

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    with patch("codebot.ticket_dispatcher.write_implementation_packet"):
                        (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                        spawn_demand_agents(
                            bots, max_concurrent=100, start_bot_fn=counting_start,
                            store=fake_store, implementation_limit=6,
                        )

        assert len(spawn_counts) >= 2, f"Only {len(spawn_counts)} role(s) dispatched: {spawn_counts}"
        for role, count in spawn_counts.items():
            assert count <= 4, f"{role} got {count} spawns, exceeding per-role cap"


class TestDisabledImplementerReEnable:

    def test_disabled_implementer_reenabled_when_tickets_exist(self, tmp_path):
        ticket = _make_ticket("T-TEST-1", TicketClass.TEST)
        bots: dict[str, MockBotState] = {
            "test_implementer": MockBotState(
                config=MockBotConfig(name="test_implementer", enabled=False), process=None
            ),
        }

        spawned = []

        def capture_start(bot, **kwargs):
            spawned.append(bot.config.name)
            return True

        fake_store = FakeTicketStore(implementing=[ticket])

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    with patch("codebot.ticket_dispatcher.write_implementation_packet"):
                        (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                        spawn_demand_agents(
                            bots, max_concurrent=100, start_bot_fn=capture_start, store=fake_store
                        )

        assert bots["test_implementer"].config.enabled is True
        assert "test_implementer" in spawned
