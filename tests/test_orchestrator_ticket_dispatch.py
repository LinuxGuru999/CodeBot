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
import os
import re
import sys
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
        """route_ready_tickets dispatches from TicketStore.list_by_state, not QUEUE.md."""
        from codebot.ticket_engine import (
            TicketStore, TicketState, TicketClass, Severity, RiskLevel,
            create_ticket,
        )
        from codebot.ticket_dispatcher import route_ready_tickets

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        store_path = state_dir / "tickets.json"

        # Create a store with READY tickets
        store = TicketStore(store_path)
        t1 = create_ticket(
            title="Test ticket 1",
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
        store.transition(t1.id, TicketState.VALIDATING)
        store.transition(t1.id, TicketState.TRIAGED)
        store.transition(t1.id, TicketState.READY)
        store.flush()

        # Verify list_by_state returns READY tickets
        ready_tickets = store.list_by_state(TicketState.READY)
        assert len(ready_tickets) >= 1, "Should have at least 1 READY ticket"

        # Verify route_ready_tickets uses the store (not QUEUE.md)
        routed = route_ready_tickets(store=store)
        assert routed >= 1, "Should have routed at least 1 READY ticket"

        # Verify the ticket was actually transitioned (in-memory state)
        updated_ticket = store.get(t1.id)
        assert updated_ticket.state in (TicketState.PLANNING, TicketState.DECOMPOSE), (
            f"Ticket should have been routed from READY, got {updated_ticket.state}"
        )

        # Also verify persistence after flush
        store.flush()
        store.close()
        reloaded_store = TicketStore(store_path)
        reloaded_ticket = reloaded_store.get(t1.id)
        assert reloaded_ticket.state in (TicketState.PLANNING, TicketState.DECOMPOSE), (
            f"Persisted ticket should be routed, got {reloaded_ticket.state}"
        )
        reloaded_store.close()

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

        # Create store with IMPLEMENTING tickets
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
        store.transition(t1.id, TicketState.VALIDATING)
        store.transition(t1.id, TicketState.TRIAGED)
        store.transition(t1.id, TicketState.READY)
        store.transition(t1.id, TicketState.IMPLEMENTING)
        store.flush()
        store.close()

        # Create mock bots
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
        with patch("codebot.ticket_dispatcher.STATE_DIR", state_dir):
            result = spawn_demand_agents(
                bots, max_concurrent=10, start_bot_fn=mock_start, store=fresh_store
            )

        assert result >= 1, "Should have spawned at least one demand agent"
        assert len(spawned) >= 1, "start_bot_fn should have been called"

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
        # Change CWD to empty_dir so the fallback Path(".codebot/state/tickets.json")
        # resolves under empty_dir (which has no tickets.json), not in project root
        monkeypatch.chdir(empty_dir)
        with patch("codebot.ticket_dispatcher.STATE_DIR", empty_dir):
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
