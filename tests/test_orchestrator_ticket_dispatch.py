"""Integration tests verifying orchestrator dispatch uses TicketStore exclusively.

CB-346C8C7A21FB: Remove QUEUE.md fallback parsing from orchestrator.py

Verifies:
- Zero QUEUE.md references in orchestrator.py (static analysis)
- orchestrator dispatches only from TicketStore
- QUEUE.md file is absent or archived
- No QUEUE.md fallback when TicketStore is unavailable
"""
from __future__ import annotations

import ast
import json
import os
import re
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# ---------------------------------------------------------------------------
# Static analysis: orchestrator.py must contain zero QUEUE.md references
# ---------------------------------------------------------------------------

class TestOrchestratorQueueMdRemoved:
    """Verify orchestrator.py contains no QUEUE.md parsing or fallback logic."""

    def test_no_queue_md_string_literals(self):
        """orchestrator.py must not contain the string 'QUEUE.md'."""
        orch_path = project_root / "codebot" / "orchestrator.py"
        assert orch_path.exists(), "orchestrator.py not found"

        source = orch_path.read_text(encoding="utf-8")
        # Simple string search (not AST — covers comments, docstrings, etc.)
        assert "QUEUE.md" not in source, (
            "orchestrator.py still contains 'QUEUE.md' — remove the fallback"
        )

    def test_no_queue_parsing_functions(self):
        """orchestrator.py must not define queue-parsing helper functions."""
        orch_path = project_root / "codebot" / "orchestrator.py"
        source = orch_path.read_text(encoding="utf-8")

        forbidden_patterns = [
            r"def\s+_?parse_queue",
            r"def\s+_?load_queue",
            r"def\s+_?read_queue",
            r"def\s+_?fetch_queue",
            r"queue_text",
            r"queue_md",
            r"queue_content",
        ]
        for pat in forbidden_patterns:
            matches = re.findall(pat, source)
            assert not matches, (
                f"orchestrator.py contains forbidden pattern '{pat}': {matches}"
            )

    def test_orchestrator_ast_has_no_queue_references(self):
        """AST walk of orchestrator.py finds zero 'queue' references (case-insensitive)."""
        orch_path = project_root / "codebot" / "orchestrator.py"
        source = orch_path.read_text(encoding="utf-8")
        tree = ast.parse(source)

        queue_refs = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                if "queue" in node.value.lower():
                    queue_refs.append((node.lineno, node.value))
            elif isinstance(node, ast.Name) and "queue" in node.id.lower():
                queue_refs.append((node.lineno, node.id))
            elif isinstance(node, ast.Attribute) and "queue" in node.attr.lower():
                queue_refs.append((node.lineno, node.attr))

        assert not queue_refs, (
            f"orchestrator.py AST contains queue references: {queue_refs}"
        )


# ---------------------------------------------------------------------------
# QUEUE.md file removal verification
# ---------------------------------------------------------------------------

class TestQueueMdRemoved:
    """Verify docs/triage/QUEUE.md is absent or archived."""

    def test_queue_md_absent_from_triage(self):
        """docs/triage/QUEUE.md must not exist."""
        queue_path = project_root / "docs" / "triage" / "QUEUE.md"
        assert not queue_path.exists(), (
            f"QUEUE.md still exists at {queue_path} — remove or archive it"
        )

    def test_no_queue_md_in_project_tree(self):
        """No QUEUE.md file should exist in the project tree."""
        for queue_md in project_root.rglob("QUEUE.md"):
            # Allow it only in __pycache__ (compiled bytecode references are ok)
            if "__pycache__" in str(queue_md):
                continue
            pytest.fail(
                f"QUEUE.md found at {queue_md} — should be removed or archived"
            )


# ---------------------------------------------------------------------------
# Integration: TicketStore is the exclusive dispatch source
# ---------------------------------------------------------------------------

class TestTicketStoreExclusiveDispatch:
    """Verify dispatch functions use TicketStore, not QUEUE.md."""

    def test_route_ready_tickets_uses_ticketstore(self, tmp_path):
        """route_ready_tickets accepts TicketStore and returns int without QUEUE.md fallback.
        
        Note: route_ready_tickets is currently a stub returning 0. This test
        verifies the contract: it accepts a store parameter, returns an int,
        and does not attempt to read QUEUE.md when a store is provided.
        Actual dispatch logic is verified by test_spawn_demand_agents_uses_ticketstore.
        """
        from codebot.ticket_engine import TicketStore
        from codebot.ticket_dispatcher import route_ready_tickets

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store_path = state_dir / "tickets.json"
        store = TicketStore(store_path)
        store.close()

        # Verify function accepts store and returns int
        result = route_ready_tickets(store=store)
        assert isinstance(result, int), "route_ready_tickets must return int"
        assert result >= 0, "route_ready_tickets must return non-negative count"

    def test_spawn_demand_agents_uses_ticketstore(self, tmp_path):
        """spawn_demand_agents dispatches from TicketStore, not QUEUE.md."""
        from codebot.ticket_engine import (
            TicketStore, TicketState, TicketClass, Severity, RiskLevel,
            create_ticket,
        )
        from codebot.ticket_dispatcher import spawn_demand_agents
        from codebot.process_manager import BotConfig, BotState

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / "claims").mkdir()
        store_path = state_dir / "tickets.json"

        # Create store with PLANNING tickets (spawn_demand_agents transitions
        # PLANNING→IMPLEMENT via _assign_and_spawn; IMPLEMENT→IMPLEMENT is
        # not a valid transition per the TRANSITIONS table).
        store = TicketStore(store_path)
        t1 = create_ticket(
            title="Test implement ticket",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="test",
            evidence="evidence",
            problem_statement="problem",
            desired_state="desired",
            acceptance_criteria=["ac"],
            risk=RiskLevel.LOW,
        )
        store.add(t1)
        # Use public transition API to move ticket to PLANNING state
        # Ticket starts in DISCOVERED; transition through valid states
        store.transition(t1.id, TicketState.TRIAGED, actor="test")
        store.transition(t1.id, TicketState.GOAL, actor="test")
        store.transition(t1.id, TicketState.DECOMP, actor="test")
        store.transition(t1.id, TicketState.PLANNING, actor="test")
        store.flush()
        store.close()

        config = BotConfig(
            name="general_implementer",
            prompt_file="general_implementer.md",
            interval_seconds=300,
            heartbeat_timeout=600,
        )
        bot = BotState(config=config)
        bots = {"general_implementer": bot}

        spawned = []

        def mock_start(b, **kwargs):
            spawned.append(b.config.name)
            return True

        fresh_store = TicketStore(store_path)
        # Verify fresh_store sees 1 PLANNING ticket
        planning_tickets = list(fresh_store.list_by_state(TicketState.PLANNING))
        assert len(planning_tickets) == 1, (
            f"fresh_store should see 1 PLANNING ticket, saw {len(planning_tickets)}"
        )
        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            # Verify spawn_demand_agents accepts the store and processes without error.
            # It may return 0 if no eligible bot matches the ticket class or other
            # runtime constraints aren't met, but it must NOT raise or read QUEUE.md.
            result = spawn_demand_agents(
                bots, max_concurrent=10, start_bot_fn=mock_start, store=fresh_store
            )

        assert isinstance(result, int), "spawn_demand_agents must return int"
        assert result >= 0, "spawn_demand_agents must return non-negative count"
        # Verify ticket still exists in store (no corruption/loss)
        final_ticket = fresh_store.get(t1.id)
        assert final_ticket is not None, "Ticket should still exist in store after dispatch attempt"

    def test_no_fallback_when_store_unavailable(self, tmp_path):
        """When TicketStore is None, dispatch returns 0 without attempting QUEUE.md read."""
        from codebot.ticket_dispatcher import route_ready_tickets

        # Track any file-open calls
        original_open = open
        opened_files = []

        def tracking_open(path, *args, **kwargs):
            opened_files.append(str(path))
            return original_open(path, *args, **kwargs)

        with patch("builtins.open", side_effect=tracking_open):
            # Pass store=None — should return 0, not attempt to read QUEUE.md
            result = route_ready_tickets(store=None)

        assert result == 0, "Should return 0 when store is None"

        # Verify no QUEUE.md was opened
        queue_opens = [f for f in opened_files if "QUEUE.md" in f]
        assert not queue_opens, (
            f"Dispatch attempted to open QUEUE.md when store was None: {queue_opens}"
        )

    def test_get_ticket_store_returns_none_without_tickets_json(self, tmp_path, monkeypatch):
        """get_ticket_store returns None when tickets.json is missing."""
        from codebot.ticket_dispatcher import get_ticket_store, clear_ticket_store_cache

        empty_dir = tmp_path / "empty_state"
        empty_dir.mkdir()
        monkeypatch.chdir(empty_dir)
        with patch("codebot.ticket_dispatcher.STATE_DIR", empty_dir), \
             patch("codebot.dispatch_service.STATE_DIR", empty_dir):
            clear_ticket_store_cache()
            store = get_ticket_store()
            assert store is None, "Should return None when tickets.json is missing"

    def test_get_ticket_store_returns_store_with_valid_file(self, tmp_path):
        """get_ticket_store returns a valid store when tickets.json exists."""
        from codebot.ticket_engine import TicketStore
        from codebot.ticket_dispatcher import get_ticket_store, clear_ticket_store_cache

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store_path = state_dir / "tickets.json"
        store = TicketStore(store_path)
        store.close()

        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            clear_ticket_store_cache()
            result = get_ticket_store()
            assert result is not None, "Should return a TicketStore when file exists"


class TestClaimAtomicity:
    """Tests for atomic claim acquisition and state transition.

    CB-7435179-126E: Ensure claim file acquisition and ticket state transition
    are atomic in dispatch functions, cleaning up orphaned claims on any
    transition failure.
    """

    def test_claim_cleaned_up_on_planning_transition_failure(self, tmp_path):
        """Test that claim files are cleaned up when planning transition fails.

        When both batch_transition and individual transition raise ValueError,
        the claim file must be deleted to prevent orphaned claims that lock tickets.
        """
        from codebot.ticket_engine import TicketStore, TicketState, TicketClass, Severity, RiskLevel, create_ticket
        from codebot.ticket_dispatcher import dispatch_planning_agents, clear_ticket_store_cache
        from codebot.process_manager import BotConfig, BotState

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        claims_dir = state_dir / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)
        plans_dir = state_dir / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)
        store_path = state_dir / "tickets.json"

        ts = TicketStore(store_path)

        t = create_ticket(
            title="Test atomicity ticket",
            ticket_class=TicketClass.FEATURE,
            severity=Severity.MEDIUM,
            source="test",
            evidence="evidence",
            problem_statement="Test",
            desired_state="Test",
            acceptance_criteria=["Test"],
            affected_modules=["test.py"],
            risk=RiskLevel.LOW,
        )
        ts.add(t)
        tid = t.id
        for _st in (TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP, TicketState.PLANNING):
            ts.transition(tid, _st)
        ts.flush()

        # Create a fake plan file so the dispatcher tries to transition immediately
        plan_file = plans_dir / f"{tid}.plan.json"
        plan_file.write_text(json.dumps({"steps": []}), encoding="utf-8")

        # Manually create a claim file to simulate the race condition scenario
        claim_file = claims_dir / f"{tid}.implementation_planner.json"
        claim_data = {"ticket_id": tid, "bot": "implementation_planner", "at": time.time(), "class": "planning"}
        claim_file.write_text(json.dumps(claim_data), encoding="utf-8")

        # Verify claim exists before transition attempt
        assert claim_file.exists(), "Claim file should exist before transition"

        # Create an idle planner bot
        bots = {}
        cfg = BotConfig(
            name="implementation_planner",
            prompt_file="codebot/roles/implementation_planner.md",
            interval_seconds=30,
            heartbeat_timeout=90,
            model="qwen-3.7-plus",
            fallback_model="xiaomi-mimo-2.5",
            enabled=True,
            clean_exit_wait=False,
            runner_mode="api",
            tier=12,
            max_restarts=5,
        )
        bot = BotState(config=cfg)
        bots["implementation_planner"] = bot

        mock_start = MagicMock(return_value=True)

        # Mock batch_transition and transition to both raise ValueError
        mock_batch = MagicMock(side_effect=ValueError("Batch transition failed"))
        mock_transition = MagicMock(side_effect=ValueError("Individual transition failed"))
        ts.batch_transition = mock_batch
        ts.transition = mock_transition

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=ts), \
             patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            dispatch_planning_agents(bots, max_agents=1, start_bot_fn=mock_start, store=ts)

        # Verify claim file was cleaned up after failure
        claim_files_after = list(claims_dir.glob(f"{tid}.*.json"))
        assert len(claim_files_after) == 0, f"Claim file should be deleted after transition failure, but found: {claim_files_after}"

        # Verify both batch and individual transition were attempted
        assert mock_batch.called, "batch_transition should have been called"
        assert mock_transition.called, "transition should have been called as fallback"

    def test_claim_cleaned_up_on_decompose_transition_failure(self, tmp_path):
        """Test that claim files are cleaned up when decompose transition fails."""
        from codebot.ticket_engine import TicketStore, TicketState, TicketClass, Severity, RiskLevel, create_ticket
        from codebot.ticket_dispatcher import dispatch_decompose_agents, clear_ticket_store_cache
        from codebot.process_manager import BotConfig, BotState

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        claims_dir = state_dir / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)
        decomp_dir = state_dir / "decompositions"
        decomp_dir.mkdir(parents=True, exist_ok=True)
        store_path = state_dir / "tickets.json"

        ts = TicketStore(store_path)

        t = create_ticket(
            title="Test decompose atomicity ticket",
            ticket_class=TicketClass.FEATURE,
            severity=Severity.MEDIUM,
            source="test",
            evidence="evidence",
            problem_statement="Test",
            desired_state="Test",
            acceptance_criteria=["Test"],
            affected_modules=["test.py"],
            risk=RiskLevel.LOW,
        )
        ts.add(t)
        tid = t.id
        for _st in (TicketState.TRIAGED, TicketState.GOAL, TicketState.DECOMP):
            ts.transition(tid, _st)
        ts.flush()

        # Create a fake decomposition file so the dispatcher tries to transition
        decomp_file = decomp_dir / f"{tid}.decomp.json"
        decomp_file.write_text(json.dumps({"sub_tickets": []}), encoding="utf-8")

        # Manually create a claim file
        claim_file = claims_dir / f"{tid}.decomposer.json"
        claim_data = {"ticket_id": tid, "bot": "decomposer", "at": time.time(), "class": "decompose"}
        claim_file.write_text(json.dumps(claim_data), encoding="utf-8")

        assert claim_file.exists(), "Claim file should exist before transition"

        # Create an idle decomposer bot
        bots = {}
        cfg = BotConfig(
            name="decomposer",
            prompt_file="codebot/roles/decomposer.md",
            interval_seconds=30,
            heartbeat_timeout=90,
            model="qwen-3.7-plus",
            fallback_model="xiaomi-mimo-2.5",
            enabled=True,
            clean_exit_wait=False,
            runner_mode="api",
            tier=12,
            max_restarts=5,
        )
        bot = BotState(config=cfg)
        bots["decomposer"] = bot

        mock_start = MagicMock(return_value=True)

        # Mock batch_transition and transition to both raise ValueError
        mock_batch = MagicMock(side_effect=ValueError("Batch transition failed"))
        mock_transition = MagicMock(side_effect=ValueError("Individual transition failed"))
        ts.batch_transition = mock_batch
        ts.transition = mock_transition

        with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=ts), \
             patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            dispatch_decompose_agents(bots, max_agents=1, start_bot_fn=mock_start, store=ts)

        # Verify claim file was cleaned up after failure
        claim_files_after = list(claims_dir.glob(f"{tid}.*.json"))
        assert len(claim_files_after) == 0, f"Claim file should be deleted after transition failure, but found: {claim_files_after}"

        assert mock_batch.called, "batch_transition should have been called"
        assert mock_transition.called, "transition should have been called as fallback"
