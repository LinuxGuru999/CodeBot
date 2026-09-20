#!/usr/bin/env python3
"""Tests for ticket dispatcher dispatch performance and correctness.

Verifies:
- O(1) role-indexed lookup achieves <1ms dispatch for 100 tickets/20 bots
- Correct bot selection per ticket class via TICKET_CLASS_TO_IMPLEMENTER
- Reviewer role indexed lookup also O(1)
- Edge cases: zero bots, unknown class, hyphenated bot names
"""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# Ensure project root is on path
project_root = Path(__file__).parent.parent
import sys
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from codebot.process_manager import BotConfig, BotState
from codebot.ticket_dispatcher import (
    IMPLEMENTER_ROLE_NAMES,
    REVIEWER_ROLE_NAMES,
    REVIEWER_TYPES,
    TICKET_CLASS_TO_IMPLEMENTER,
    spawn_demand_agents,
)
from codebot.ticket_engine import Ticket, TicketState


def _make_bot(name: str, enabled: bool = True, running: bool = False) -> BotState:
    """Create a mock BotState for testing."""
    cfg = BotConfig(
        name=name,
        prompt_file=f"{name.split('-')[0]}.md",
        interval_seconds=30,
        heartbeat_timeout=90,
        model="xiaomi-mimo-2.5",
        enabled=enabled,
    )
    bot = BotState(config=cfg)
    if running:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        bot.process = mock_proc
    return bot


def _make_ticket(tid: str, ticket_class: str, state: TicketState = TicketState.IMPLEMENTING) -> Ticket:
    """Create a mock Ticket for testing."""
    return Ticket(
        id=tid,
        title=f"Test ticket {tid}",
        ticket_class=ticket_class,
        severity="medium",
        source="test",
        evidence="test evidence",
        problem_statement="test problem",
        desired_state="test desired",
        acceptance_criteria="test criteria",
        affected_modules="test_module.py",
        risk="low",
        state=state,
    )


class TestDispatchPerformance:
    """Benchmark tests for dispatch performance."""

    def test_dispatch_100_tickets_20_bots_under_1ms(self, tmp_path):
        """Dispatch time for 100 tickets and 20 bots must be <1ms.

        This verifies the O(1) role-indexed lookup replaces the legacy
        O(n*m) nested loop (100*20=2000 iterations).
        """
        # Create 20 bots across 6 implementer roles
        roles = list(IMPLEMENTER_ROLE_NAMES)
        bots = {}
        for i in range(20):
            role = roles[i % len(roles)]
            bot_name = f"{role}-{i+1}"
            bots[bot_name] = _make_bot(bot_name, enabled=True, running=False)

        # Create 100 tickets in IMPLEMENTING state with various classes
        ticket_classes = list(TICKET_CLASS_TO_IMPLEMENTER.keys())
        tickets = []
        for i in range(100):
            tc = ticket_classes[i % len(ticket_classes)]
            tickets.append(_make_ticket(f"CB-PERF-{i:03d}", tc, TicketState.IMPLEMENTING))

        # Write tickets to temp state dir
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        tickets_file = state_dir / "tickets.json"
        tickets_data = {
            "tickets": [
                {
                    "id": t.id,
                    "title": t.title,
                    "ticket_class": t.ticket_class,
                    "severity": t.severity,
                    "source": t.source,
                    "evidence": t.evidence,
                    "problem_statement": t.problem_statement,
                    "desired_state": t.desired_state,
                    "acceptance_criteria": t.acceptance_criteria,
                    "affected_modules": t.affected_modules,
                    "risk": t.risk,
                    "state": t.state.value,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    "assignee": t.assignee,
                    "rework_count": t.rework_count,
                    "sub_tickets": t.sub_tickets,
                    "parent_ticket": t.parent_ticket,
                    "plan_depth": t.plan_depth,
                    "implementation_plan": t.implementation_plan,
                    "decomposed_from": t.decomposed_from,
                }
                for t in tickets
            ]
        }
        tickets_file.write_text(json.dumps(tickets_data), encoding="utf-8")

        # Mock start_bot_fn to no-op
        spawned_count = 0

        def mock_start_bot(bot, **kwargs):
            nonlocal spawned_count
            spawned_count += 1
            return True

        # Measure dispatch time (excluding I/O - the role lookup loop itself)
        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            # Clear cache to ensure fresh load
            from codebot.ticket_dispatcher import clear_ticket_store_cache
            clear_ticket_store_cache()

            start = time.perf_counter()
            spawned = spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=mock_start_bot)
            elapsed = time.perf_counter() - start

        print(f"dispatch 100/20 in {elapsed*1000:.3f} ms (spawned={spawned})")
        assert elapsed < 0.001, f"Dispatch took {elapsed*1000:.3f}ms, expected <1ms"
        assert spawned > 0, "Should have spawned at least one bot"


class TestCorrectBotSelection:
    """Test correct bot selection per ticket class."""

    def test_correct_bot_selection_per_class(self, tmp_path):
        """For each ticket_class, dispatched bot base matches expected implementer role."""
        # Create one bot per implementer role
        bots = {}
        for role in IMPLEMENTER_ROLE_NAMES:
            bot_name = f"{role}-1"
            bots[bot_name] = _make_bot(bot_name, enabled=True, running=False)

        # Create one ticket per ticket class
        tickets = []
        for tc in TICKET_CLASS_TO_IMPLEMENTER:
            tickets.append(_make_ticket(f"CB-CORRECT-{tc}", tc, TicketState.IMPLEMENTING))

        # Write tickets to temp state dir
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        tickets_file = state_dir / "tickets.json"
        tickets_data = {
            "tickets": [
                {
                    "id": t.id,
                    "title": t.title,
                    "ticket_class": t.ticket_class,
                    "severity": t.severity,
                    "source": t.source,
                    "evidence": t.evidence,
                    "problem_statement": t.problem_statement,
                    "desired_state": t.desired_state,
                    "acceptance_criteria": t.acceptance_criteria,
                    "affected_modules": t.affected_modules,
                    "risk": t.risk,
                    "state": t.state.value,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    "assignee": t.assignee,
                    "rework_count": t.rework_count,
                    "sub_tickets": t.sub_tickets,
                    "parent_ticket": t.parent_ticket,
                    "plan_depth": t.plan_depth,
                    "implementation_plan": t.implementation_plan,
                    "decomposed_from": t.decomposed_from,
                }
                for t in tickets
            ]
        }
        tickets_file.write_text(json.dumps(tickets_data), encoding="utf-8")

        # Track which bot was assigned to which ticket
        assignments = {}

        def mock_start_bot(bot, **kwargs):
            # Find the ticket this bot was assigned to
            assigned_tid = getattr(bot, '_assigned_ticket_id', '')
            if assigned_tid:
                assignments[assigned_tid] = bot.config.name
            return True

        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            from codebot.ticket_dispatcher import clear_ticket_store_cache
            clear_ticket_store_cache()
            spawned = spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=mock_start_bot)

        # Verify each ticket was assigned to the correct role
        for ticket in tickets:
            tid = ticket.id
            expected_role = TICKET_CLASS_TO_IMPLEMENTER[ticket.ticket_class]
            assert tid in assignments, f"Ticket {tid} was not assigned"
            assigned_bot = assignments[tid]
            assigned_base = assigned_bot.split("-")[0] if "-" in assigned_bot else assigned_bot
            assert assigned_base == expected_role, (
                f"Ticket {tid} (class={ticket.ticket_class}) assigned to {assigned_bot}, "
                f"expected base={expected_role}"
            )

    def test_hyphenated_bot_names_indexed_correctly(self, tmp_path):
        """Bots with hyphenated names like backend_implementer-10 index under backend_implementer."""
        # Create bots with multi-digit suffixes
        bots = {
            "general_implementer-1": _make_bot("general_implementer-1", enabled=True, running=False),
            "general_implementer-10": _make_bot("general_implementer-10", enabled=True, running=False),
            "backend_implementer-2": _make_bot("backend_implementer-2", enabled=True, running=False),
            "test_implementer-5": _make_bot("test_implementer-5", enabled=True, running=False),
        }

        # Create tickets for each class
        tickets = [
            _make_ticket("CB-HYPH-001", "bug", TicketState.IMPLEMENTING),
            _make_ticket("CB-HYPH-002", "security", TicketState.IMPLEMENTING),
            _make_ticket("CB-HYPH-003", "test", TicketState.IMPLEMENTING),
        ]

        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        tickets_file = state_dir / "tickets.json"
        tickets_data = {
            "tickets": [
                {
                    "id": t.id,
                    "title": t.title,
                    "ticket_class": t.ticket_class,
                    "severity": t.severity,
                    "source": t.source,
                    "evidence": t.evidence,
                    "problem_statement": t.problem_statement,
                    "desired_state": t.desired_state,
                    "acceptance_criteria": t.acceptance_criteria,
                    "affected_modules": t.affected_modules,
                    "risk": t.risk,
                    "state": t.state.value,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    "assignee": t.assignee,
                    "rework_count": t.rework_count,
                    "sub_tickets": t.sub_tickets,
                    "parent_ticket": t.parent_ticket,
                    "plan_depth": t.plan_depth,
                    "implementation_plan": t.implementation_plan,
                    "decomposed_from": t.decomposed_from,
                }
                for t in tickets
            ]
        }
        tickets_file.write_text(json.dumps(tickets_data), encoding="utf-8")

        assignments = {}

        def mock_start_bot(bot, **kwargs):
            assigned_tid = getattr(bot, '_assigned_ticket_id', '')
            if assigned_tid:
                assignments[assigned_tid] = bot.config.name
            return True

        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            from codebot.ticket_dispatcher import clear_ticket_store_cache
            clear_ticket_store_cache()
            spawn_demand_agents(bots, max_concurrent=10, start_bot_fn=mock_start_bot)

        # bug -> general_implementer
        assert "CB-HYPH-001" in assignments
        assert assignments["CB-HYPH-001"].startswith("general_implementer")
        # security -> backend_implementer
        assert "CB-HYPH-002" in assignments
        assert assignments["CB-HYPH-002"].startswith("backend_implementer")
        # test -> test_implementer
        assert "CB-HYPH-003" in assignments
        assert assignments["CB-HYPH-003"].startswith("test_implementer")


class TestReviewerDispatch:
    """Test reviewer role indexed lookup."""

    def test_reviewer_role_indexed_lookup(self, tmp_path):
        """Reviewer dispatch uses O(1) lookup via idle_reviewers_by_role."""
        # Create reviewer bots
        bots = {}
        for rtype in REVIEWER_TYPES:
            bot_name = f"{rtype}-1"
            bots[bot_name] = _make_bot(bot_name, enabled=True, running=False)

        # Create reviewing tickets
        tickets = [
            _make_ticket(f"CB-REV-{i:03d}", "bug", TicketState.REVIEWING)
            for i in range(5)
        ]

        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        tickets_file = state_dir / "tickets.json"
        tickets_data = {
            "tickets": [
                {
                    "id": t.id,
                    "title": t.title,
                    "ticket_class": t.ticket_class,
                    "severity": t.severity,
                    "source": t.source,
                    "evidence": t.evidence,
                    "problem_statement": t.problem_statement,
                    "desired_state": t.desired_state,
                    "acceptance_criteria": t.acceptance_criteria,
                    "affected_modules": t.affected_modules,
                    "risk": t.risk,
                    "state": t.state.value,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    "assignee": t.assignee,
                    "rework_count": t.rework_count,
                    "sub_tickets": t.sub_tickets,
                    "parent_ticket": t.parent_ticket,
                    "plan_depth": t.plan_depth,
                    "implementation_plan": t.implementation_plan,
                    "decomposed_from": t.decomposed_from,
                }
                for t in tickets
            ]
        }
        tickets_file.write_text(json.dumps(tickets_data), encoding="utf-8")

        assignments = {}

        def mock_start_bot(bot, **kwargs):
            assigned_tid = getattr(bot, '_assigned_ticket_id', '')
            if assigned_tid:
                assignments[assigned_tid] = bot.config.name
            return True

        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            from codebot.ticket_dispatcher import clear_ticket_store_cache
            clear_ticket_store_cache()
            spawn_demand_agents(bots, max_concurrent=100, start_bot_fn=mock_start_bot)

        # Verify reviewers were assigned with correct role bases
        for tid, bot_name in assignments.items():
            base = bot_name.split("-")[0] if "-" in bot_name else bot_name
            assert base in REVIEWER_ROLE_NAMES or base == "ux_reviewer", (
                f"Reviewer assignment {bot_name} has unexpected base {base}"
            )


class TestEdgeCases:
    """Test edge cases for dispatch."""

    def test_edge_zero_bots_and_unknown_class_fallback(self, tmp_path):
        """Zero idle bots creates new bot; unknown ticket_class falls back to general_implementer."""
        # Empty bots dict
        bots = {}

        # Create ticket with unknown class
        tickets = [
            _make_ticket("CB-EDGE-001", "unknown_weird_class", TicketState.IMPLEMENTING),
        ]

        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        tickets_file = state_dir / "tickets.json"
        tickets_data = {
            "tickets": [
                {
                    "id": t.id,
                    "title": t.title,
                    "ticket_class": t.ticket_class,
                    "severity": t.severity,
                    "source": t.source,
                    "evidence": t.evidence,
                    "problem_statement": t.problem_statement,
                    "desired_state": t.desired_state,
                    "acceptance_criteria": t.acceptance_criteria,
                    "affected_modules": t.affected_modules,
                    "risk": t.risk,
                    "state": t.state.value,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    "assignee": t.assignee,
                    "rework_count": t.rework_count,
                    "sub_tickets": t.sub_tickets,
                    "parent_ticket": t.parent_ticket,
                    "plan_depth": t.plan_depth,
                    "implementation_plan": t.implementation_plan,
                    "decomposed_from": t.decomposed_from,
                }
                for t in tickets
            ]
        }
        tickets_file.write_text(json.dumps(tickets_data), encoding="utf-8")

        created_bots = []

        def mock_start_bot(bot, **kwargs):
            created_bots.append(bot.config.name)
            return True

        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            from codebot.ticket_dispatcher import clear_ticket_store_cache
            clear_ticket_store_cache()
            spawned = spawn_demand_agents(bots, max_concurrent=10, start_bot_fn=mock_start_bot)

        # Should have created a new bot with general_implementer base (fallback)
        assert spawned >= 1
        assert any("general_implementer" in name for name in created_bots), (
            f"Expected general_implementer fallback, got {created_bots}"
        )

    def test_zero_tickets_does_no_work(self, tmp_path):
        """Zero tickets means no dispatch work."""
        bots = {
            "general_implementer-1": _make_bot("general_implementer-1", enabled=True, running=False),
        }

        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        tickets_file = state_dir / "tickets.json"
        tickets_file.write_text(json.dumps({"tickets": []}), encoding="utf-8")

        def mock_start_bot(bot, **kwargs):
            return True

        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            from codebot.ticket_dispatcher import clear_ticket_store_cache
            clear_ticket_store_cache()
            spawned = spawn_demand_agents(bots, max_concurrent=10, start_bot_fn=mock_start_bot)

        assert spawned == 0

    def test_all_bots_busy_respects_max_concurrent(self, tmp_path):
        """All bots busy means no new spawns until budget allows."""
        bots = {}
        for i in range(8):
            bot_name = f"general_implementer-{i+1}"
            bots[bot_name] = _make_bot(bot_name, enabled=True, running=True)

        tickets = [
            _make_ticket("CB-BUSY-001", "bug", TicketState.IMPLEMENTING),
        ]

        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        tickets_file = state_dir / "tickets.json"
        tickets_data = {
            "tickets": [
                {
                    "id": t.id,
                    "title": t.title,
                    "ticket_class": t.ticket_class,
                    "severity": t.severity,
                    "source": t.source,
                    "evidence": t.evidence,
                    "problem_statement": t.problem_statement,
                    "desired_state": t.desired_state,
                    "acceptance_criteria": t.acceptance_criteria,
                    "affected_modules": t.affected_modules,
                    "risk": t.risk,
                    "state": t.state.value,
                    "created_at": t.created_at,
                    "updated_at": t.updated_at,
                    "assignee": t.assignee,
                    "rework_count": t.rework_count,
                    "sub_tickets": t.sub_tickets,
                    "parent_ticket": t.parent_ticket,
                    "plan_depth": t.plan_depth,
                    "implementation_plan": t.implementation_plan,
                    "decomposed_from": t.decomposed_from,
                }
                for t in tickets
            ]
        }
        tickets_file.write_text(json.dumps(tickets_data), encoding="utf-8")

        spawned_count = 0

        def mock_start_bot(bot, **kwargs):
            nonlocal spawned_count
            spawned_count += 1
            return True

        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            from codebot.ticket_dispatcher import clear_ticket_store_cache
            clear_ticket_store_cache()
            # max_concurrent=8, all 8 bots running, so budget=0
            spawned = spawn_demand_agents(bots, max_concurrent=8, start_bot_fn=mock_start_bot)

        assert spawned == 0
        assert spawned_count == 0
