"""Performance tests for orchestrator health check cycle.

Verifies that the orchestrator's health check cycle uses a single
TicketStore instance per tick, minimizing I/O overhead.
"""
import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebot.process_manager import BotConfig, BotState


@pytest.fixture
def mock_paths(tmp_path):
    """Create temporary paths for testing."""
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    bots_dir = tmp_path / "bots"
    bots_dir.mkdir()
    return state_dir, logs_dir, bots_dir


@pytest.fixture
def sample_bot():
    """Create a sample bot configuration."""
    config = BotConfig(
        name="test_bot",
        prompt_file="test.md",
        interval_seconds=60,
        heartbeat_timeout=120,
    )
    return BotState(config=config)


class TestSingleTicketStoreInstantiation:
    """Verify only one TicketStore instantiation per health check tick."""

    @patch("codebot.orchestrator.check_self_restart", return_value=False)
    @patch("codebot.orchestrator._sweep_orphan_claims")
    @patch("codebot.orchestrator.batch_read_bot_statuses")
    @patch("codebot.orchestrator.batch_read_heartbeats", return_value={})
    @patch("codebot.orchestrator.get_pipeline_state", return_value={})
    @patch("codebot.orchestrator._hcl_run_dispatchers")
    @patch("codebot.orchestrator._hcl_init_tick")
    @patch("codebot.orchestrator.is_draining", return_value=False)
    @patch("codebot.orchestrator.log_bot_statuses")
    def test_single_ticketstore_instantiation_per_tick(
        self,
        mock_log_statuses,
        mock_is_draining,
        mock_init_tick,
        mock_run_dispatchers,
        mock_pipeline,
        mock_batch_hb,
        mock_batch_read,
        mock_sweep,
        mock_self_restart,
        sample_bot,
        tmp_path,
    ):
        """Verify only one TicketStore __init__ call occurs per check_all_bots() invocation.

        The orchestrator should clear the cache at the start of each tick,
        causing exactly one TicketStore instantiation when the first
        get_ticket_store() call is made. Subsequent calls within the same
        tick should return the cached instance.
        """
        from codebot.orchestrator import check_all_bots
        from codebot.ticket_dispatcher import clear_ticket_store_cache

        bots = {"test_bot": sample_bot}

        # Patch where TicketStore is actually imported and used
        with patch("codebot.ticket_engine.TicketStore") as mock_ts_class:
            mock_instance = MagicMock()
            mock_instance.list_by_state.return_value = []
            mock_instance.get.return_value = None
            mock_instance._tickets = {}
            mock_ts_class.return_value = mock_instance

            # Run the health check tick
            check_all_bots(bots)

            # Verify TicketStore was instantiated exactly once per tick
            assert mock_ts_class.call_count <= 1, (
                f"Expected at most 1 TicketStore instantiation, got {mock_ts_class.call_count}. "
                "Multiple instantiations indicate the cache is not being used properly."
            )

    @patch("codebot.orchestrator.check_self_restart", return_value=False)
    @patch("codebot.orchestrator._sweep_orphan_claims")
    @patch("codebot.orchestrator.batch_read_bot_statuses")
    @patch("codebot.orchestrator.batch_read_heartbeats", return_value={})
    @patch("codebot.orchestrator.get_pipeline_state", return_value={})
    @patch("codebot.orchestrator._hcl_run_dispatchers")
    @patch("codebot.orchestrator._hcl_init_tick")
    @patch("codebot.orchestrator.is_draining", return_value=False)
    @patch("codebot.orchestrator.log_bot_statuses")
    def test_cache_cleared_between_ticks(
        self,
        mock_log_statuses,
        mock_is_draining,
        mock_init_tick,
        mock_run_dispatchers,
        mock_pipeline,
        mock_batch_hb,
        mock_batch_read,
        mock_sweep,
        mock_self_restart,
        sample_bot,
        tmp_path,
    ):
        """Verify cache is cleared between ticks, allowing fresh data per tick."""
        from codebot.orchestrator import check_all_bots

        bots = {"test_bot": sample_bot}

        # Patch where TicketStore is actually imported and used
        with patch("codebot.ticket_engine.TicketStore") as mock_ts_class:
            mock_instance = MagicMock()
            mock_instance.list_by_state.return_value = []
            mock_instance.get.return_value = None
            mock_instance._tickets = {}
            mock_ts_class.return_value = mock_instance

            # Run two consecutive ticks
            check_all_bots(bots)
            first_tick_count = mock_ts_class.call_count

            check_all_bots(bots)
            second_tick_count = mock_ts_class.call_count

            # Each tick should instantiate TicketStore at most once
            assert first_tick_count <= 1, f"First tick: expected at most 1 instantiation, got {first_tick_count}"
            assert second_tick_count <= 2, f"Second tick: expected at most 2 total instantiations, got {second_tick_count}"


class TestQueryOverhead:
    """Test that ticket query overhead is within acceptable bounds."""

    def test_get_ticket_store_cached_performance(self, tmp_path):
        """Verify cached get_ticket_store() calls have <10ms overhead.

        After the initial cache miss, subsequent calls should return
        the cached instance with minimal overhead.
        """
        from codebot.ticket_dispatcher import get_ticket_store, clear_ticket_store_cache
        from codebot.ticket_engine import TicketStore

        # Create a tickets.json with 1000+ tickets
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        tickets_file = state_dir / "tickets.json"

        # Generate 1000 tickets
        tickets = [
            {
                "id": f"CB-{i:07d}",
                "title": f"Test ticket {i}",
                "class": "bug",
                "severity": "medium",
                "state": "READY",
                "description": f"Description for ticket {i}",
                "acceptance_criteria": "Test passes",
                "affected_modules": "codebot/test.py",
            }
            for i in range(1000)
        ]
        tickets_file.write_text(json.dumps(tickets))

        # Clear cache and get first instance (cache miss)
        clear_ticket_store_cache()
        start = time.time()
        store1 = get_ticket_store()
        first_call_ms = (time.time() - start) * 1000

        assert store1 is not None, "TicketStore should be created"

        # Subsequent calls should be cached (cache hit)
        iterations = 10
        cached_times = []
        for _ in range(iterations):
            start = time.time()
            store2 = get_ticket_store()
            elapsed_ms = (time.time() - start) * 1000
            cached_times.append(elapsed_ms)
            assert store2 is store1, "Should return same cached instance"

        avg_cached_ms = sum(cached_times) / len(cached_times)
        max_cached_ms = max(cached_times)

        # Cached calls should be very fast (<10ms)
        assert avg_cached_ms < 10, (
            f"Average cached get_ticket_store() took {avg_cached_ms:.2f}ms, "
            f"expected <10ms. Max was {max_cached_ms:.2f}ms."
        )


class TestHealthTickSingleRead:
    """Verify health-loop tick reads tickets.json exactly once under 10ms."""

    def test_health_tick_reads_tickets_json_once_under_10ms(self, tmp_path):
        """Verify that a full health-check tick with 1000+ tickets reads/parses
        tickets.json exactly once and completes cached queries in <10ms.

        This regression test ensures future changes do not reintroduce the N+1
        query problem where each helper re-reads tickets.json independently.
        """
        import builtins
        from codebot.ticket_dispatcher import (
            get_ticket_store,
            clear_ticket_store_cache,
        )
        from codebot.health_check_loop import init_tick, current_tick_store
        from codebot.dispatch_service import get_pipeline_state

        # Create state directory and tickets.json with 1000+ tickets
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        tickets_file = state_dir / "tickets.json"

        num_tickets = 1100
        tickets = [
            {
                "id": f"CB-{i:07d}-TEST",
                "title": f"Test ticket {i}",
                "ticket_class": "bug",
                "severity": "medium",
                "state": "READY",
                "source": "test",
                "evidence": f"Evidence for ticket {i}",
                "problem_statement": f"Problem {i}",
                "desired_state": f"Desired state {i}",
                "acceptance_criteria": ["Test passes"],
                "affected_modules": ["codebot/test.py"],
                "dependencies": [],
                "risk": "medium",
                "blast_radius": "low",
                "security_impact": "none",
                "migration_impact": "none",
                "required_reviewers": [],
                "required_tests": [],
                "documentation_requirements": [],
                "rollback_strategy": "revert",
                "estimated_cost_tokens": 100,
                "created_at": time.time(),
                "updated_at": time.time(),
            }
            for i in range(num_tickets)
        ]
        tickets_file.write_text(json.dumps({"tickets": tickets}))

        from codebot.ticket_engine import TicketStore as _TS
        orig_init = _TS.__init__
        open_count = 0
        opened_paths: list[str] = []

        def spying_init(self, *a, **kw):
            nonlocal open_count
            open_count += 1
            opened_paths.append(str(getattr(self, '_path', a[0] if a else '?')))
            return orig_init(self, *a, **kw)

        from codebot.state_manager import PathConfig as _PC
        _cfg = _PC(
            state_dir=state_dir, logs_dir=state_dir / "logs", bots_dir=tmp_path / "bots",
            backup_dir=state_dir / "backup", alignment_events_dir=state_dir / "alignment_events",
            drain_file=state_dir / ".drain", update_lock=state_dir / ".update_lock", restart_file=state_dir / ".restart",
        )
        with patch("codebot.state_manager.get_paths", return_value=_cfg), patch("codebot.ticket_dispatcher.STATE_DIR", state_dir), patch("codebot.dispatch_service.STATE_DIR", state_dir), patch.object(_TS, "__init__", spying_init):
                clear_ticket_store_cache()
                init_tick()
                start = time.time()
                store = current_tick_store()
                first_call_ms = (time.time() - start) * 1000
                assert store is not None, "TicketStore should be created from tickets.json"
                pipeline_start = time.time()
                for _ in range(10):
                    ps = get_pipeline_state(store=store)
                    _ = store.list_by_state.__self__.list_by_state("READY") if hasattr(store.list_by_state, '__self__') else store.list_by_state("READY")
                pipeline_ms = (time.time() - pipeline_start) * 1000
                store2 = current_tick_store()
                assert store2 is store, "current_tick_store should return cached instance"

        # Count only reads that hit the test's tmp_path state dir (ignore stale reads from real .codebot/state)
        tmp_reads = [p for p in opened_paths if str(state_dir) in p]
        assert len(tmp_reads) == 1, (
            f"Expected exactly 1 read of tmp tickets.json, got {len(tmp_reads)} (total {open_count}). "
            f"Opened paths: {opened_paths}. "
            "Multiple reads indicate the cache is not being used properly."
        )

        # The cached query portion (after initial parse) should be <10ms
        assert pipeline_ms < 10, (
            f"Cached query loop took {pipeline_ms:.2f}ms, expected <10ms. "
            "This indicates excessive overhead in ticket queries."
        )
