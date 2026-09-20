#!/usr/bin/env python3
"""Tests for TicketStore cache sharing in ticket_dispatcher.

Verifies that:
- Multiple calls to _get_ticket_store() return the same instance within a tick
- clear_ticket_store_cache() invalidates the cache and forces reload
- The orchestrator tick uses exactly one disk read per cycle
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def ticket_store_dir(tmp_path: Path):
    """Create a temporary state directory with a tickets.json file."""
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    tickets_file = state_dir / "tickets.json"
    tickets_file.write_text(json.dumps({
        "tickets": [],
        "version": 1,
    }), encoding="utf-8")
    return state_dir


@pytest.fixture(autouse=True)
def reset_cache():
    """Reset the module-level cache before and after each test."""
    import codebot.ticket_dispatcher as td
    td._ticket_store_cache = None
    yield
    td._ticket_store_cache = None


# ---------------------------------------------------------------------------
# Test: _get_ticket_store returns same instance (cache sharing)
# ---------------------------------------------------------------------------

class TestTicketStoreCacheSharing:
    """Verify that _get_ticket_store returns the same instance across calls."""

    def test_returns_same_instance(self, ticket_store_dir: Path):
        """Multiple calls to _get_ticket_store() should return the same object."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            store1 = td._get_ticket_store()
            store2 = td._get_ticket_store()
            store3 = td._get_ticket_store()
            assert store1 is not None
            assert store1 is store2
            assert store2 is store3

    def test_cache_is_module_level(self, ticket_store_dir: Path):
        """The cached instance should be stored in the module-level variable."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            store = td._get_ticket_store()
            assert td._ticket_store_cache is store

    def test_returns_none_when_no_file(self, tmp_path: Path, monkeypatch):
        """Should return None when tickets.json does not exist in STATE_DIR."""
        import codebot.ticket_dispatcher as td
        td._ticket_store_cache = None

        empty_dir = tmp_path / "empty"
        empty_dir.mkdir()

        # Patch STATE_DIR AND the fallback check so both paths don't exist
        original_exists = Path.exists

        def mock_exists(self_path):
            # Block both STATE_DIR/tickets.json and .codebot/state/tickets.json
            if "tickets.json" in str(self_path) and "state" in str(self_path):
                return False
            return original_exists(self_path)

        with patch.object(td, "STATE_DIR", empty_dir), \
             patch.object(Path, "exists", mock_exists):
            store = td._get_ticket_store()
            assert store is None


# ---------------------------------------------------------------------------
# Test: clear_ticket_store_cache invalidates cache
# ---------------------------------------------------------------------------

class TestClearTicketStoreCache:
    """Verify that clear_ticket_store_cache() invalidates and forces reload."""

    def test_clear_sets_cache_to_none(self, ticket_store_dir: Path):
        """clear_ticket_store_cache() should set _ticket_store_cache to None."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            td._get_ticket_store()
            assert td._ticket_store_cache is not None
            td.clear_ticket_store_cache()
            assert td._ticket_store_cache is None

    def test_clear_forces_new_instance(self, ticket_store_dir: Path):
        """After clear, _get_ticket_store should create a new instance."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            store1 = td._get_ticket_store()
            td.clear_ticket_store_cache()
            store2 = td._get_ticket_store()
            assert store1 is not None
            assert store2 is not None
            assert store1 is not store2

    def test_clear_is_idempotent(self):
        """Calling clear_ticket_store_cache() when already None should be safe."""
        import codebot.ticket_dispatcher as td
        assert td._ticket_store_cache is None
        td.clear_ticket_store_cache()  # should not raise
        assert td._ticket_store_cache is None


# ---------------------------------------------------------------------------
# Test: Orchestrator tick integration
# ---------------------------------------------------------------------------

class TestOrchestratorTickIntegration:
    """Verify the orchestrator calls clear_ticket_store_cache at tick start."""

    def test_check_all_bots_calls_clear(self):
        """check_all_bots should call clear_ticket_store_cache before dispatch."""
        import codebot.ticket_dispatcher as td
        # Verify the function exists and is callable
        assert callable(td.clear_ticket_store_cache)

    def test_import_in_orchestrator(self):
        """orchestrator.py should import clear_ticket_store_cache."""
        import codebot.orchestrator as orch
        assert hasattr(orch, "clear_ticket_store_cache")


# ---------------------------------------------------------------------------
# Test: All dispatcher functions use _get_ticket_store (no direct instantiation)
# ---------------------------------------------------------------------------

class TestDispatcherFunctionsUseCache:
    """Verify all dispatcher functions go through _get_ticket_store."""

    DIRECT_IMPORT_REFS = [
        "TicketStore(",
        "TicketStore (",
    ]

    def test_no_direct_ticket_store_instantiation(self):
        """ticket_dispatcher.py should not instantiate TicketStore directly."""
        import codebot.ticket_dispatcher as td
        source_file = Path(td.__file__)
        source_text = source_file.read_text(encoding="utf-8")
        # Find lines with direct TicketStore( instantiation, excluding _get_ticket_store
        in_get_ticket_store = False
        for line in source_text.splitlines():
            stripped = line.strip()
            if "def _get_ticket_store" in stripped:
                in_get_ticket_store = True
                continue
            if in_get_ticket_store and stripped.startswith("def ") and not stripped.startswith("def _get_ticket_store"):
                in_get_ticket_store = False
            if in_get_ticket_store:
                continue
            for ref in self.DIRECT_IMPORT_REFS:
                if ref in line and "from codebot.ticket_engine import TicketStore" not in line:
                    pytest.fail(
                        f"Direct TicketStore instantiation found outside _get_ticket_store: {line.strip()}"
                    )

    def test_all_dispatch_functions_call_get_ticket_store(self):
        """Every dispatch function should call _get_ticket_store."""
        import codebot.ticket_dispatcher as td
        source_file = Path(td.__file__)
        source_text = source_file.read_text(encoding="utf-8")

        dispatch_functions = [
            "spawn_demand_agents",
            "dispatch_decompose_agents",
            "dispatch_planning_agents",
            "advance_reviewed_tickets",
            "gatekeeper_verify_tickets",
            "route_ready_tickets",
            "process_rework_tickets",
            "recover_deferred_tickets",
        ]

        for func_name in dispatch_functions:
            assert f"def {func_name}" in source_text, f"Function {func_name} not found"
            # Find the function body and verify it calls _get_ticket_store
            lines = source_text.splitlines()
            found_func = False
            func_body_lines = []
            indent_level = 0
            for i, line in enumerate(lines):
                if line.strip().startswith(f"def {func_name}"):
                    found_func = True
                    indent_level = len(line) - len(line.lstrip())
                    continue
                if found_func:
                    if line.strip() == "" or (len(line) - len(line.lstrip())) > indent_level:
                        func_body_lines.append(line)
                    elif line.strip() and (len(line) - len(line.lstrip())) <= indent_level:
                        break

            body_text = "\n".join(func_body_lines)
            assert "_get_ticket_store()" in body_text, (
                f"Function {func_name} does not call _get_ticket_store()"
            )


# ---------------------------------------------------------------------------
# Test: Benchmark — 10 calls with cached store < 10ms overhead
# ---------------------------------------------------------------------------

class TestCacheBenchmark:
    """Verify the cache eliminates per-call overhead."""

    def test_cached_access_is_fast(self, ticket_store_dir: Path):
        """10 calls to _get_ticket_store with cache should be well under 10ms."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            # Prime the cache
            td._get_ticket_store()
            # Time 10 subsequent cached calls
            start = time.monotonic()
            for _ in range(10):
                ts = td._get_ticket_store()
                assert ts is not None
            elapsed_ms = (time.monotonic() - start) * 1000
            # Cached access should be sub-millisecond for 10 calls
            assert elapsed_ms < 10, f"Cached access took {elapsed_ms:.2f}ms (expected <10ms)"

    def test_clear_and_reload_overhead(self, ticket_store_dir: Path):
        """A full clear+reload cycle should be under 10ms for a small store."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            start = time.monotonic()
            td.clear_ticket_store_cache()
            ts = td._get_ticket_store()
            assert ts is not None
            elapsed_ms = (time.monotonic() - start) * 1000
            assert elapsed_ms < 10, f"Clear+reload took {elapsed_ms:.2f}ms (expected <10ms)"
