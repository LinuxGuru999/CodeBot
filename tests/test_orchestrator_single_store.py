"""Tests for CB-9165641-2750: Single TicketStore instance per orchestrator tick.

Verifies:
1. Only one tickets.json read/parse per tick (constructor count == 1)
2. Helper functions accept TicketStore instance as argument
3. Benchmark shows <10ms overhead for ticket queries at 1k+ tickets
"""

from __future__ import annotations

import inspect
import json
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_ticket(ticket_id: str, state: str = "READY", ticket_class: str = "bug") -> dict:
    """Create a minimal ticket dict for seeding tickets.json."""
    return {
        "id": ticket_id,
        "title": f"Test ticket {ticket_id}",
        "ticket_class": ticket_class,
        "severity": "low",
        "state": state,
        "problem_statement": "test",
        "desired_state": "test",
        "acceptance_criteria": ["test"],
        "evidence": f"test evidence for {ticket_id}",
        "source": "test",
        "affected_modules": [],
        "dependencies": [],
        "risk": "low",
        "blast_radius": "",
        "security_impact": "none",
        "migration_impact": "none",
        "required_reviewers": [],
        "required_tests": [],
        "documentation_requirements": [],
        "rollback_strategy": "revert",
        "estimated_cost_tokens": 1000,
        "rework_count": 0,
        "created_at": time.time(),
        "updated_at": time.time(),
    }


def _seed_tickets_json(path: Path, tickets: list[dict]) -> None:
    """Write tickets.json in the format TicketStore expects: {"tickets": [...]}."""
    path.write_text(json.dumps({"tickets": tickets}, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# Test 1: Single read per tick — get_ticket_store cache hit count
# ---------------------------------------------------------------------------

def test_single_read_per_tick():
    """Verify that get_ticket_store() creates TicketStore exactly once between
    clear_ticket_store_cache() calls, even when called multiple times.

    This tests the module-level caching in ticket_dispatcher, which is the
    mechanism the orchestrator relies on to ensure single-read-per-tick.
    """
    import codebot.ticket_dispatcher as td_mod
    from codebot.ticket_engine import TicketStore

    original_init = TicketStore.__init__
    constructions = 0

    def counting_init(self, *args, **kwargs):
        nonlocal constructions
        constructions += 1
        return original_init(self, *args, **kwargs)

    with tempfile.TemporaryDirectory() as tmpdir:
        tickets_path = Path(tmpdir) / "tickets.json"
        tickets = [_make_fake_ticket(f"CB-{i}", "READY") for i in range(5)]
        _seed_tickets_json(tickets_path, tickets)

        # Directly mutate the module-level STATE_DIR so get_ticket_store resolves our temp path.
        # get_ticket_store() reads STATE_DIR as a global at call time, so direct assignment works.
        original_state_dir = td_mod.STATE_DIR
        td_mod.STATE_DIR = Path(tmpdir)

        try:
            with patch.object(TicketStore, "__init__", counting_init):
                td_mod.clear_ticket_store_cache()
                constructions = 0

                # Verify our patched path is actually found by get_ticket_store
                expected_path = td_mod.STATE_DIR / "tickets.json"
                assert expected_path.exists(), f"Test setup error: {expected_path} does not exist"

                # First call — should construct once
                store1 = td_mod.get_ticket_store()
                assert store1 is not None, (
                    f"get_ticket_store() returned None. "
                    f"STATE_DIR={td_mod.STATE_DIR}, exists={expected_path.exists()}"
                )
                assert constructions == 1, f"First get_ticket_store() should construct once, got {constructions}"

                # Second call — should hit cache, no new construction
                store2 = td_mod.get_ticket_store()
                assert constructions == 1, (
                    f"Second get_ticket_store() should use cache, "
                    f"but got {constructions} constructions"
                )

                # Third call — still cached
                store3 = td_mod.get_ticket_store()
                assert constructions == 1, (
                    f"Third get_ticket_store() should still use cache, "
                    f"but got {constructions} constructions"
                )

                # Same instance
                assert store1 is store2 is store3, "All calls should return the same instance"

                # Clean up
                store1.close()
        finally:
            td_mod.STATE_DIR = original_state_dir
            td_mod.clear_ticket_store_cache()


# ---------------------------------------------------------------------------
# Test 2: Helpers accept store argument
# ---------------------------------------------------------------------------

def test_helpers_accept_store_arg():
    """Verify all dispatcher and service helpers accept 'store' as keyword argument."""
    from codebot import ticket_dispatcher, dispatch_service, orchestrator_services

    dispatcher_funcs = [
        ticket_dispatcher.spawn_demand_agents,
        ticket_dispatcher.dispatch_decompose_agents,
        ticket_dispatcher.dispatch_planning_agents,
        ticket_dispatcher.advance_reviewed_tickets,
        ticket_dispatcher.gatekeeper_verify_tickets,
        ticket_dispatcher.route_ready_tickets,
        ticket_dispatcher.process_rework_tickets,
        ticket_dispatcher.recover_deferred_tickets,
    ]

    service_funcs = [
        dispatch_service.get_pipeline_state,
        dispatch_service.apply_agent_availability,
        dispatch_service.transition_ticket_on_success,
        dispatch_service.transition_ticket_on_error,
        orchestrator_services.get_pipeline_state,
        orchestrator_services.apply_agent_availability,
    ]

    for fn in dispatcher_funcs + service_funcs:
        sig = inspect.signature(fn)
        param_names = list(sig.parameters.keys())
        assert "store" in param_names, (
            f"{fn.__module__}.{fn.__qualname__} missing 'store' parameter. "
            f"Current params: {param_names}"
        )
        store_param = sig.parameters["store"]
        assert store_param.default is None, (
            f"{fn.__module__}.{fn.__qualname__} 'store' param must default to None, "
            f"got {store_param.default!r}"
        )


# ---------------------------------------------------------------------------
# Test 3: Benchmark <10ms overhead at 1k+ tickets
# ---------------------------------------------------------------------------

def test_ticket_query_overhead_1k():
    """Benchmark that shared-store queries complete in <10ms for 1100 tickets."""
    from codebot.ticket_engine import TicketStore, TicketState

    with tempfile.TemporaryDirectory() as tmpdir:
        tickets_path = Path(tmpdir) / "tickets.json"

        # Generate 1100 tickets across various states
        states = ["DISCOVERED", "VALIDATING", "TRIAGED", "READY", "DECOMPOSE",
                  "PLANNING", "IMPLEMENTING", "REVIEWING", "VERIFYING", "COMPLETE",
                  "REWORK", "DEFERRED", "REJECTED"]
        tickets = []
        for i in range(1100):
            state = states[i % len(states)]
            tickets.append(_make_fake_ticket(f"CB-BENCH-{i}", state))

        _seed_tickets_json(tickets_path, tickets)

        # Load once (this is the single-read we're optimizing for)
        store = TicketStore(tickets_path)

        # Benchmark the query sweep that happens each tick
        t0 = time.perf_counter()
        for _ in range(10):
            # Simulate what get_pipeline_state does — iterate all states
            counts: dict[str, int] = {}
            for state in TicketState:
                items = store.list_by_state(state)
                if items:
                    counts[state.value] = len(items)
            # Simulate a few get() calls (like transition_ticket_on_success)
            for i in range(0, 1100, 100):
                store.get(f"CB-BENCH-{i}")

        elapsed_ms = (time.perf_counter() - t0) * 1000
        per_sweep_ms = elapsed_ms / 10

        print(f"\nPipeline sweep over 1100 tickets: {per_sweep_ms:.3f}ms per sweep")
        print(f"Ticket counts: {counts}")

        assert per_sweep_ms < 10.0, (
            f"Ticket query sweep took {per_sweep_ms:.3f}ms for 1100 tickets, "
            f"expected <10ms"
        )

        store.close()
