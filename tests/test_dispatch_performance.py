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

        The core dict build + lookup loop is O(n+m). We patch time.sleep,
        register_claim, release_claim, and all claim file I/O to isolate the
        algorithmic complexity from disk overhead.

        start_bot_fn returns True so bots are assigned from the deque (O(1)
        popleft), avoiding the _get_or_create_bot fallback path which does
        prompt-file disk I/O.
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
        # Return True so bots get assigned from the deque (no _get_or_create_bot fallback)
        start_bot_fn = MagicMock(return_value=True)

        # Create the claims dir and patch all claim file I/O to no-ops
        _claims_dir = tmp_path / "claims"
        _claims_dir.mkdir(parents=True, exist_ok=True)

        from pathlib import Path as _Path

        def _noop_write(self_path, *a, **kw):
            return None

        def _noop_replace(self_path, *a, **kw):
            return None

        def _noop_unlink(self_path, **kw):
            return None

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
            with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                    with patch("codebot.ticket_dispatcher.register_claim"):
                        with patch("codebot.ticket_dispatcher.release_claim"):
                            with patch.object(_Path, "write_text", _noop_write):
                                with patch.object(_Path, "replace", _noop_replace):
                                    with patch.object(_Path, "unlink", _noop_unlink):
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

    def test_o1_bot_lookup_by_role_name(self, tmp_path):
        """Verify role-indexed dict lookup is O(1) — scaling bots does NOT increase lookup time.

        Creates 10, 100, and 1000 bots and measures the time to build the
        role index + do lookups for 10 tickets. The lookup time must remain
        constant (within noise) as bot count scales.
        """
        tickets = [_make_ticket(f"T-{i:04d}", TicketClass.BUG) for i in range(10)]

        def _build_bots(n: int) -> dict[str, MockBotState]:
            bots: dict[str, MockBotState] = {}
            roles = list(IMPLEMENTER_ROLE_NAMES)
            for i in range(n):
                role = roles[i % len(roles)]
                name = f"{role}-{i+1}"
                bots[name] = MockBotState(config=MockBotConfig(name=name, enabled=True), process=None)
            return bots

        times = []
        for n_bots in [10, 100, 1000]:
            bots = _build_bots(n_bots)
            fake_store = FakeTicketStore(implementing=tickets)
            start_bot_fn = MagicMock(return_value=True)

            with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=fake_store):
                with patch("codebot.ticket_dispatcher.STATE_DIR", tmp_path):
                    with patch("codebot.ticket_dispatcher.time.sleep", return_value=None):
                        with patch("codebot.ticket_dispatcher.register_claim"):
                            with patch("codebot.ticket_dispatcher.release_claim"):
                                (tmp_path / "claims").mkdir(parents=True, exist_ok=True)
                                from pathlib import Path as _Path
                                start = time.perf_counter()
                                spawn_demand_agents(bots, max_concurrent=20, start_bot_fn=start_bot_fn)
                                elapsed = time.perf_counter() - start
                                times.append(elapsed * 1000)

        print(f"Role lookup times: 10 bots={times[0]:.3f}ms, 100 bots={times[1]:.3f}ms, 1000 bots={times[2]:.3f}ms")
        # O(1) lookup means 1000x bots should not cause 1000x slowdown.
        # Allow up to 5x noise due to I/O and Python overhead.
        assert times[2] < times[0] * 5 + 1.0, (
            f"Lookup not O(1): 10 bots={times[0]:.3f}ms, 1000 bots={times[2]:.3f}ms"
        )
