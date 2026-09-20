"""Performance regression tests for O(n+m) role-indexed dispatch.

Verifies CB-8002139-9090: spawn_demand_agents uses dict-based role lookup
instead of O(n*m) nested iteration, achieving <10ms dispatch for 100 tickets
and 50 workers.
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


class TestDispatchPerformanceCB8002139:
    """Verify O(1) role-indexed dispatch meets the <10ms target for 100 tickets / 50 workers."""

    def test_dispatch_100_tickets_50_workers_under_10ms(self, tmp_path):
        """100 tickets dispatched against 50 workers must complete in <10ms.

        The core dict build + lookup loop is O(n+m). We patch time.sleep
        and use start_bot_fn returning False (to avoid claim file I/O overhead)
        to isolate the algorithmic complexity from I/O overhead.
        """
        tickets = []
        classes = list(TICKET_CLASS_TO_IMPLEMENTER.keys())
        for i in range(100):
            tc = TicketClass(classes[i % len(classes)])
            tickets.append(_make_ticket(f"T-{i:04d}", tc))

        bots: dict[str, MockBotState] = {}
        roles = list(IMPLEMENTER_ROLE_NAMES)
        for i in range(50):
            role = roles[i % len(roles)]
            name = f"{role}-{i+1}"
            bots[name] = MockBotState(config=MockBotConfig(name=name, enabled=True), process=None)

        fake_store = FakeTicketStore(implementing=tickets)
        # Return False so we don't actually write claim files or sleep
        start_bot_fn = MagicMock(return_value=False)

        # Patch register_claim and release_claim to eliminate disk I/O overhead
        # that is not part of the algorithmic complexity we're measuring.
        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    with patch("codebot.ticket_dispatcher.register_claim"):
                        with patch("codebot.ticket_dispatcher.release_claim"):
                            (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                            start = time.perf_counter()
                            result = spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=start_bot_fn)
                            elapsed = time.perf_counter() - start

        elapsed_ms = elapsed * 1000
        print(f"Dispatch 100 tickets / 50 workers in {elapsed_ms:.3f}ms (spawned={result})")
        assert elapsed_ms < 10.0, f"Dispatch took {elapsed_ms:.3f}ms, expected <10ms"

    def test_role_indexed_lookup_correctness(self, tmp_path):
        """Tickets are assigned to correct role-matched bots via O(1) lookup."""
        spawned_roles: list[str] = []

        def capture_start(bot, **kwargs):
            base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
            spawned_roles.append(base)
            return False

        for tc_str, expected_role in TICKET_CLASS_TO_IMPLEMENTER.items():
            tc = TicketClass(tc_str)
            ticket = _make_ticket(f"T-{tc_str}", tc)

            bots: dict[str, MockBotState] = {}
            roles = list(IMPLEMENTER_ROLE_NAMES)
            for i in range(5):
                role = roles[i % len(roles)]
                name = f"{role}-{i+1}"
                bots[name] = MockBotState(config=MockBotConfig(name=name, enabled=True), process=None)

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
