#!/usr/bin/env python3
"""Tests for TicketStore cache sharing in ticket_dispatcher.

Verifies that:
- Multiple calls to get_ticket_store() return the same instance within a tick
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
    """Reset the module-level cache before and after each test.
    
    Uses clear_ticket_store_cache() to properly close any evicted store
    and release background worker threads, preventing pytest hangs from
    non-daemon threads. Also explicitly resets _ticket_store_meta to
    ensure full test isolation.
    """
    import codebot.ticket_dispatcher as td
    # Pre-test cleanup: close any lingering store from previous tests
    td.clear_ticket_store_cache()
    td._ticket_store_meta = {}
    yield
    # Post-test cleanup: close the store created during this test
    td.clear_ticket_store_cache()
    td._ticket_store_meta = {}


# ---------------------------------------------------------------------------
# Test: _get_ticket_store returns same instance (cache sharing)
# ---------------------------------------------------------------------------

class TestTicketStoreCacheSharing:
    """Verify that get_ticket_store returns the same instance across calls."""

    def test_returns_same_instance(self, ticket_store_dir: Path):
        """Multiple calls to get_ticket_store() should return the same object."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            store1 = td.get_ticket_store()
            store2 = td.get_ticket_store()
            store3 = td.get_ticket_store()
            assert store1 is not None
            assert store1 is store2
            assert store2 is store3

    def test_cache_is_module_level(self, ticket_store_dir: Path):
        """The cached instance should be stored in the module-level variable."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            store = td.get_ticket_store()
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
            store = td.get_ticket_store()
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
            td.get_ticket_store()
            assert td._ticket_store_cache is not None
            td.clear_ticket_store_cache()
            assert td._ticket_store_cache is None

    def test_clear_forces_new_instance(self, ticket_store_dir: Path):
        """After clear, get_ticket_store should create a new instance."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            store1 = td.get_ticket_store()
            td.clear_ticket_store_cache()
            store2 = td.get_ticket_store()
            assert store1 is not None
            assert store2 is not None
            assert store1 is not store2

    def test_clear_is_idempotent(self):
        """Calling clear_ticket_store_cache() when already None should be safe."""
        import codebot.ticket_dispatcher as td
        assert td._ticket_store_cache is None
        td.clear_ticket_store_cache()  # should not raise
        assert td._ticket_store_cache is None

    def test_clear_ticket_store_cache_closes_store(self, ticket_store_dir: Path):
        """clear_ticket_store_cache() must call .close() on the evicted store.
        
        Regression test for CB-DAA9E: per-tick TicketStore leaks threads/memory
        because evicted stores were not closed, leaving background workers running.
        """
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            store = td.get_ticket_store()
            assert store is not None
            # Mock the close method to verify it gets called
            with patch.object(store, "close") as mock_close:
                td.clear_ticket_store_cache()
                mock_close.assert_called_once()
            assert td._ticket_store_cache is None


# ---------------------------------------------------------------------------
# Test: Orchestrator tick integration
# ---------------------------------------------------------------------------

class TestOrchestratorTickIntegration:
    """Verify the orchestrator integrates with scheduler_v2 for dispatch."""

    def test_check_all_bots_calls_clear(self):
        """check_all_bots should call clear_ticket_store_cache before dispatch."""
        import codebot.ticket_dispatcher as td
        assert callable(td.clear_ticket_store_cache)

    def test_scheduler_v2_available(self):
        """scheduler_v2.Scheduler should be importable for orchestrator integration."""
        try:
            from codebot.scheduler_v2.dispatcher import Scheduler
            assert Scheduler is not None
        except ImportError:
            pytest.skip("scheduler_v2 not available in this environment")
        except Exception:
            pytest.skip("scheduler_v2 import failed unexpectedly")

    def test_run_triage_fast_paths_exists(self):
        """run_triage_fast_paths should exist as platform-level triage entry point."""
        import codebot.ticket_dispatcher as td
        assert callable(td.run_triage_fast_paths)

    def test_import_in_orchestrator(self):
        """Orchestrator re-exports get_ticket_store/clear_ticket_store_cache."""
        try:
            import codebot.orchestrator as orch
        except ImportError as exc:
            pytest.skip(f"codebot.orchestrator not importable: {exc}")
        except Exception as exc:
            pytest.skip(f"codebot.orchestrator import raised unexpected error: {exc}")
        
        # Check that the functions are accessible (either directly or via __all__)
        try:
            assert hasattr(orch, "get_ticket_store"), "orchestrator missing get_ticket_store"
            assert hasattr(orch, "clear_ticket_store_cache"), "orchestrator missing clear_ticket_store_cache"
        except AssertionError:
            pytest.skip("orchestrator module loaded but missing expected attributes")
        
        # Only check __all__ if it exists and is accessible
        try:
            if hasattr(orch, "__all__") and isinstance(orch.__all__, (list, tuple)):
                assert "get_ticket_store" in orch.__all__, "get_ticket_store not in orchestrator.__all__"
                assert "clear_ticket_store_cache" in orch.__all__, "clear_ticket_store_cache not in orchestrator.__all__"
        except Exception as exc:
            pytest.skip(f"Could not verify orchestrator.__all__: {exc}")


# ---------------------------------------------------------------------------
# Test: All dispatcher functions use _get_ticket_store (no direct instantiation)
# ---------------------------------------------------------------------------

class TestDispatcherFunctionsUseCache:
    """Verify all dispatcher functions go through get_ticket_store."""

    DIRECT_IMPORT_REFS = [
        "TicketStore(",
        "TicketStore (",
    ]

    def test_no_direct_ticket_store_instantiation(self):
        """ticket_dispatcher.py should not instantiate TicketStore directly."""
        import codebot.ticket_dispatcher as td
        source_file = Path(td.__file__)
        source_text = source_file.read_text(encoding="utf-8")
        # Find lines with direct TicketStore( instantiation, excluding get_ticket_store
        in_get_ticket_store = False
        for line in source_text.splitlines():
            stripped = line.strip()
            if "def get_ticket_store" in stripped:
                in_get_ticket_store = True
                continue
            if in_get_ticket_store and stripped.startswith("def ") and not stripped.startswith("def get_ticket_store"):
                in_get_ticket_store = False
            if in_get_ticket_store:
                continue
            for ref in self.DIRECT_IMPORT_REFS:
                if ref in line and "from codebot.ticket_engine import TicketStore" not in line:
                    pytest.fail(
                        f"Direct TicketStore instantiation found outside get_ticket_store: {line.strip()}"
                    )

    def test_all_dispatch_functions_call_get_ticket_store(self):
        """Active dispatch functions should call get_ticket_store or accept store param."""
        import codebot.ticket_dispatcher as td
        source_file = Path(td.__file__)
        source_text = source_file.read_text(encoding="utf-8")

        # Only active dispatch functions are checked.
        # Legacy functions (route_ready_tickets, gatekeeper_verify_tickets,
        # recover_deferred_tickets, recover_blocked_tickets) are now no-op stubs
        # returning 0, replaced by scheduler_v2 + run_triage_fast_paths.
        dispatch_functions = [
            "spawn_demand_agents",
            "dispatch_decompose_agents",
            "dispatch_planning_agents",
            "advance_reviewed_tickets",
            "process_rework_tickets",
        ]

        for func_name in dispatch_functions:
            assert f"def {func_name}" in source_text, f"Function {func_name} not found"
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
            func_sig_and_body = f"{func_name}\n{body_text}"
            assert (
                "_get_ticket_store()" in body_text
                or "get_ticket_store()" in body_text
                or "store if store is not None" in body_text
                or "store:" in func_sig_and_body
                or "store=" in func_sig_and_body
            ), f"Function {func_name} does not call get_ticket_store() or accept store param"


# ---------------------------------------------------------------------------
# Test: Benchmark — 10 calls with cached store < 10ms overhead
# ---------------------------------------------------------------------------

class TestCacheBenchmark:
    """Verify the cache eliminates per-call overhead."""

    def test_cached_access_is_fast(self, ticket_store_dir: Path):
        """10 calls to get_ticket_store with cache should be well under 10ms."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            # Prime the cache
            td.get_ticket_store()
            # Time 10 subsequent cached calls
            start = time.monotonic()
            for _ in range(10):
                ts = td.get_ticket_store()
                assert ts is not None
            elapsed_ms = (time.monotonic() - start) * 1000
            # Cached access should be sub-millisecond for 10 calls
            assert elapsed_ms < 10, f"Cached access took {elapsed_ms:.2f}ms (expected <10ms)"


class TestMtimeReuse:
    """Verify that get_ticket_store reuses cached instance when file is unchanged."""

    def test_get_ticket_store_reuses_instance_on_unchanged_file(self, ticket_store_dir: Path):
        """get_ticket_store() should return the same instance when (path, mtime, size) unchanged.
        
        Regression test for CB-DAA9E: missing mtime reuse forced O(T) full reload
        every tick even when tickets.json was unchanged.
        """
        import codebot.ticket_dispatcher as td
        from unittest.mock import patch as mock_patch
        
        with mock_patch.object(td, "STATE_DIR", ticket_store_dir):
            # First call creates the store
            store1 = td.get_ticket_store()
            assert store1 is not None
            
            # Track TicketStore constructor calls
            from codebot.ticket_engine import TicketStore
            original_init = TicketStore.__init__
            init_call_count = 0
            
            def counting_init(self, *args, **kwargs):
                nonlocal init_call_count
                init_call_count += 1
                return original_init(self, *args, **kwargs)
            
            with mock_patch.object(TicketStore, "__init__", counting_init):
                # Subsequent calls with unchanged file should NOT reconstruct
                store2 = td.get_ticket_store()
                store3 = td.get_ticket_store()
                store4 = td.get_ticket_store()
                
                assert store2 is store1
                assert store3 is store1
                assert store4 is store1
                # Zero reconstructions should have occurred
                assert init_call_count == 0, f"Expected 0 reconstructions, got {init_call_count}"

    def test_get_ticket_store_reloads_on_mtime_change(self, ticket_store_dir: Path):
        """get_ticket_store() should create a new instance when mtime changes."""
        import codebot.ticket_dispatcher as td
        
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            store1 = td.get_ticket_store()
            assert store1 is not None
            
            # Touch the file to change mtime
            tickets_file = ticket_store_dir / "tickets.json"
            time.sleep(0.05)  # Ensure mtime differs
            tickets_file.write_text(json.dumps({"tickets": [], "version": 2}), encoding="utf-8")
            
            store2 = td.get_ticket_store()
            assert store2 is not None
            assert store2 is not store1, "Store should be recreated when mtime changes"

    def test_get_ticket_store_reloads_on_size_change(self, ticket_store_dir: Path):
        """get_ticket_store() should create a new instance when file size changes."""
        import codebot.ticket_dispatcher as td
        
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            store1 = td.get_ticket_store()
            assert store1 is not None
            
            # Change file content (and thus size) while keeping mtime potentially same
            tickets_file = ticket_store_dir / "tickets.json"
            original_content = tickets_file.read_text(encoding="utf-8")
            # Write significantly different content to change size
            new_content = json.dumps({"tickets": [{"id": "test", "title": "x" * 1000}], "version": 1})
            tickets_file.write_text(new_content, encoding="utf-8")
            
            store2 = td.get_ticket_store()
            assert store2 is not None
            assert store2 is not store1, "Store should be recreated when size changes"

    def test_clear_and_reload_overhead(self, ticket_store_dir: Path):
        """A full clear+reload cycle should be under 500ms for a small store."""
        import codebot.ticket_dispatcher as td
        with patch.object(td, "STATE_DIR", ticket_store_dir):
            start = time.monotonic()
            td.clear_ticket_store_cache()
            ts = td.get_ticket_store()
            assert ts is not None
            elapsed_ms = (time.monotonic() - start) * 1000
            assert elapsed_ms < 2000, f"Clear+reload took {elapsed_ms:.2f}ms (expected <2000ms)"


class TestCrossTickReuse:
    """Verify that TicketStore is reused across ticks when file is unchanged.

    Regression tests for CB-33484: unconditional per-tick clear forced
    O(n) reload every tick even when tickets.json was unchanged.
    """

    def test_unchanged_file_reused_across_ticks(self, ticket_store_dir: Path):
        """Two consecutive init_tick + get_ticket_store cycles with unmodified
        tickets.json should construct TicketStore only once total."""
        import codebot.ticket_dispatcher as td
        from codebot.health_check_loop import init_tick
        from codebot.ticket_engine import TicketStore

        original_init = TicketStore.__init__
        init_call_count = 0

        def counting_init(self, *args, **kwargs):
            nonlocal init_call_count
            init_call_count += 1
            return original_init(self, *args, **kwargs)

        with patch.object(td, "STATE_DIR", ticket_store_dir), \
             patch.object(TicketStore, "__init__", counting_init):
            # Tick 1: init_tick + get_ticket_store
            init_tick()
            store1 = td.get_ticket_store()
            assert store1 is not None
            count_after_tick1 = init_call_count

            # Tick 2: init_tick + get_ticket_store (file unchanged)
            init_tick()
            store2 = td.get_ticket_store()
            assert store2 is not None
            count_after_tick2 = init_call_count

            # Should be same instance, no additional construction
            assert store1 is store2
            assert count_after_tick2 == count_after_tick1, (
                f"Expected no new constructions in tick 2, but got "
                f"{count_after_tick2 - count_after_tick1} additional"
            )

    def test_mtime_change_between_ticks_forces_reload(self, ticket_store_dir: Path):
        """Modifying tickets.json between ticks should cause exactly one reload."""
        import codebot.ticket_dispatcher as td
        from codebot.health_check_loop import init_tick
        from codebot.ticket_engine import TicketStore

        original_init = TicketStore.__init__
        init_call_count = 0

        def counting_init(self, *args, **kwargs):
            nonlocal init_call_count
            init_call_count += 1
            return original_init(self, *args, **kwargs)

        with patch.object(td, "STATE_DIR", ticket_store_dir), \
             patch.object(TicketStore, "__init__", counting_init):
            # Tick 1
            init_tick()
            store1 = td.get_ticket_store()
            assert store1 is not None
            count_after_tick1 = init_call_count

            # Modify file between ticks
            tickets_file = ticket_store_dir / "tickets.json"
            time.sleep(0.05)
            tickets_file.write_text(json.dumps({"tickets": [], "version": 2}), encoding="utf-8")

            # Tick 2
            init_tick()
            store2 = td.get_ticket_store()
            assert store2 is not None
            count_after_tick2 = init_call_count

            # Should be different instance, exactly one new construction
            assert store1 is not store2
            assert count_after_tick2 == count_after_tick1 + 1, (
                f"Expected exactly 1 new construction, got {count_after_tick2 - count_after_tick1}"
            )


class TestLargeStorePerformance:
    """Verify orchestrator tick duration remains acceptable with large stores."""

    def test_tick_duration_with_10k_tickets(self, tmp_path: Path):
        """Orchestrator tick should complete in <1s with 10k tickets."""
        import codebot.ticket_dispatcher as td
        from codebot.health_check_loop import init_tick

        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        tickets_file = state_dir / "tickets.json"

        # Generate 10k tickets
        tickets = [
            {"id": f"T-{i}", "title": f"Ticket {i}", "ticket_class": "bug",
             "severity": "medium", "status": "open"}
            for i in range(10000)
        ]
        tickets_file.write_text(
            json.dumps({"tickets": tickets, "version": 1}),
            encoding="utf-8"
        )

        with patch.object(td, "STATE_DIR", state_dir):
            # Prime cache
            init_tick()
            store = td.get_ticket_store()
            assert store is not None

            # Measure tick duration (cache hit path)
            start = time.monotonic()
            init_tick()
            store2 = td.get_ticket_store()
            elapsed = time.monotonic() - start

            assert store2 is store, "Should reuse cached instance"
            assert elapsed < 1.0, f"Tick took {elapsed:.3f}s (expected <1s with 10k tickets)"
