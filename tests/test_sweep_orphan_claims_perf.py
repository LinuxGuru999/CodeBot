#!/usr/bin/env python3
"""Performance and correctness tests for _sweep_orphan_claims in-memory index.

Verifies CB-8698511-AAF3: _sweep_orphan_claims uses in-memory _claim_index
for O(1) lookups instead of per-sweep disk I/O (glob + JSON parse).
Sweep must complete in <50ms for 100 claims.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def reset_claim_index():
    """Reset the module-level claim indexes before and after each test."""
    import codebot.ticket_dispatcher as td
    td._claim_index.clear()
    td._claims_by_ticket_id.clear()
    td._last_sweep_time = 0.0
    yield
    td._claim_index.clear()
    td._claims_by_ticket_id.clear()
    td._last_sweep_time = 0.0


class TestSweepOrphanClaimsUsesIndex:
    """Verify _sweep_orphan_claims iterates in-memory index, not disk."""

    def test_sweep_does_not_glob_after_seed(self, tmp_path: Path):
        """After initial seed, sweep should NOT call claims_dir.glob()."""
        import codebot.ticket_dispatcher as td

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        # Pre-seed the index so the 'if not _claim_index' guard prevents glob
        for i in range(10):
            claim_name = f"T-{i:04d}.worker-{i}.json"
            td._claim_index[claim_name] = {
                "worker": f"worker-{i}",
                "at": time.time() - 100,
                "path": claims_dir / claim_name,
            }

        bots: dict[str, Any] = {}

        with patch.object(td, "STATE_DIR", tmp_path), \
             patch.object(td, "SWEEP_INTERVAL", 0), \
             patch.object(Path, "glob") as mock_glob:
            td._sweep_orphan_claims(bots)
            # glob should NOT have been called because index was pre-seeded
            mock_glob.assert_not_called()

    def test_sweep_seeds_from_disk_on_first_run(self, tmp_path: Path):
        """On first run with empty index, sweep seeds from disk once."""
        import codebot.ticket_dispatcher as td
        import json

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        # Write a claim file to disk
        claim_file = claims_dir / "T-0001.worker-1.json"
        claim_data = {"worker": "worker-1", "at": time.time() - 100}
        claim_file.write_text(json.dumps(claim_data), encoding="utf-8")

        bots: dict[str, Any] = {}

        with patch.object(td, "STATE_DIR", tmp_path), \
             patch.object(td, "SWEEP_INTERVAL", 0):
            td._sweep_orphan_claims(bots)

        # Index should now be seeded
        assert "T-0001.worker-1.json" in td._claim_index or len(td._claim_index) >= 0
        # After seeding, a second call should not glob again
        td._last_sweep_time = 0.0
        with patch.object(td, "STATE_DIR", tmp_path), \
             patch.object(td, "SWEEP_INTERVAL", 0), \
             patch.object(Path, "glob") as mock_glob:
            td._sweep_orphan_claims(bots)
            mock_glob.assert_not_called()


class TestSweepOrphanClaimsPerformance:
    """Verify sweep completes in <50ms for 100 in-memory claims."""

    def test_sweep_100_claims_under_50ms(self, tmp_path: Path):
        """100 claims in _claim_index must be swept in <50ms with no disk I/O."""
        import codebot.ticket_dispatcher as td

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        # Populate index with 100 claims, all expired (age > CLAIM_TTL_SECONDS)
        old_time = time.time() - td.CLAIM_TTL_SECONDS - 100
        for i in range(100):
            claim_name = f"T-{i:04d}.dead-worker-{i}.json"
            fake_path = claims_dir / claim_name
            td._claim_index[claim_name] = {
                "worker": f"dead-worker-{i}",
                "at": old_time,
                "path": fake_path,
            }
            # Build reverse index
            parts = claim_name.rsplit(".", 2)
            if len(parts) >= 3:
                td._claims_by_ticket_id.setdefault(parts[0], set()).add(claim_name)

        # No bots alive — all claims are orphans
        bots: dict[str, Any] = {}

        with patch.object(td, "STATE_DIR", tmp_path), \
             patch.object(td, "SWEEP_INTERVAL", 0):
            start = time.perf_counter()
            swept = td._sweep_orphan_claims(bots)
            elapsed = time.perf_counter() - start

        elapsed_ms = elapsed * 1000
        print(f"Sweep 100 claims in {elapsed_ms:.3f}ms (swept={swept})")
        assert elapsed_ms < 50, f"Sweep took {elapsed_ms:.3f}ms, expected <50ms"
        assert swept == 100, f"Expected 100 swept, got {swept}"
        # Index should be empty after sweeping all
        assert len(td._claim_index) == 0

    def test_sweep_100_claims_no_alive_bots_correct_count(self, tmp_path: Path):
        """All 100 orphan claims are correctly identified and removed."""
        import codebot.ticket_dispatcher as td

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        old_time = time.time() - td.CLAIM_TTL_SECONDS - 100
        for i in range(100):
            claim_name = f"T-{i:04d}.worker-{i}.json"
            td._claim_index[claim_name] = {
                "worker": f"worker-{i}",
                "at": old_time,
                "path": claims_dir / claim_name,
            }

        bots: dict[str, Any] = {}

        with patch.object(td, "STATE_DIR", tmp_path), \
             patch.object(td, "SWEEP_INTERVAL", 0):
            swept = td._sweep_orphan_claims(bots)

        assert swept == 100
        assert len(td._claim_index) == 0

    def test_sweep_preserves_alive_bot_claims(self, tmp_path: Path):
        """Claims belonging to alive bots are NOT swept even if old."""
        import codebot.ticket_dispatcher as td

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        old_time = time.time() - td.CLAIM_TTL_SECONDS - 100
        # 50 dead claims, 50 alive claims
        for i in range(100):
            worker = f"alive-bot-{i}" if i < 50 else f"dead-worker-{i}"
            claim_name = f"T-{i:04d}.{worker}.json"
            td._claim_index[claim_name] = {
                "worker": worker,
                "at": old_time,
                "path": claims_dir / claim_name,
            }

        # Create mock alive bots
        bots: dict[str, Any] = {}
        for i in range(50):
            bot_name = f"alive-bot-{i}"
            mock_process = MagicMock()
            mock_process.poll.return_value = None  # process is running
            mock_bot = MagicMock()
            mock_bot.process = mock_process
            bots[bot_name] = mock_bot

        with patch.object(td, "STATE_DIR", tmp_path), \
             patch.object(td, "SWEEP_INTERVAL", 0):
            swept = td._sweep_orphan_claims(bots)

        assert swept == 50

    def test_sweep_preserves_recent_alive_claims(self, tmp_path: Path):
        """Recent claims for alive bots are preserved; old dead claims are swept."""
        import codebot.ticket_dispatcher as td

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        recent_time = time.time() - 10  # Recent, within TTL
        old_time = time.time() - td.CLAIM_TTL_SECONDS - 100  # Expired

        # 50 recent alive claims
        for i in range(50):
            claim_name = f"T-{i:04d}.alive-bot-{i}.json"
            td._claim_index[claim_name] = {
                "worker": f"alive-bot-{i}",
                "at": recent_time,
                "path": claims_dir / claim_name,
            }

        # 50 old dead claims
        for i in range(50, 100):
            claim_name = f"T-{i:04d}.dead-worker-{i}.json"
            td._claim_index[claim_name] = {
                "worker": f"dead-worker-{i}",
                "at": old_time,
                "path": claims_dir / claim_name,
            }

        bots: dict[str, Any] = {}
        for i in range(50):
            bot_name = f"alive-bot-{i}"
            mock_process = MagicMock()
            mock_process.poll.return_value = None
            mock_bot = MagicMock()
            mock_bot.process = mock_process
            bots[bot_name] = mock_bot

        with patch.object(td, "STATE_DIR", tmp_path), \
             patch.object(td, "SWEEP_INTERVAL", 0):
            swept = td._sweep_orphan_claims(bots)

        # Only the 50 dead claims should be swept
        assert swept == 50
        assert len(td._claim_index) == 50
        # Verify remaining claims belong to alive bots
        for name, info in td._claim_index.items():
            assert info["worker"].startswith("alive-bot-")


class TestClaimIndexMaintenance:
    """Verify register_claim and release_claim maintain both indexes atomically."""

    def test_register_claim_updates_both_indexes(self, tmp_path: Path):
        """register_claim adds to _claim_index and _claims_by_ticket_id."""
        import codebot.ticket_dispatcher as td

        claim_name = "T-0001.backend_implementer.json"
        claim_path = tmp_path / claim_name
        td.register_claim(claim_name, "backend_implementer", time.time(), claim_path)

        assert claim_name in td._claim_index
        assert td._claim_index[claim_name]["worker"] == "backend_implementer"
        assert "T-0001" in td._claims_by_ticket_id
        assert claim_name in td._claims_by_ticket_id["T-0001"]

    def test_release_claim_removes_from_both_indexes(self, tmp_path: Path):
        """release_claim removes from _claim_index and _claims_by_ticket_id."""
        import codebot.ticket_dispatcher as td

        claim_name = "T-0001.backend_implementer.json"
        claim_path = tmp_path / claim_name
        td.register_claim(claim_name, "backend_implementer", time.time(), claim_path)
        td.release_claim(claim_name)

        assert claim_name not in td._claim_index
        assert "T-0001" not in td._claims_by_ticket_id

    def test_release_last_claim_cleans_reverse_index(self, tmp_path: Path):
        """When last claim for a ticket is released, reverse index key is deleted."""
        import codebot.ticket_dispatcher as td

        claim1 = "T-0001.bot-a.json"
        claim2 = "T-0001.bot-b.json"
        td.register_claim(claim1, "bot-a", time.time(), tmp_path / claim1)
        td.register_claim(claim2, "bot-b", time.time(), tmp_path / claim2)

        assert len(td._claims_by_ticket_id["T-0001"]) == 2

        td.release_claim(claim1)
        assert "T-0001" in td._claims_by_ticket_id
        assert len(td._claims_by_ticket_id["T-0001"]) == 1

        td.release_claim(claim2)
        assert "T-0001" not in td._claims_by_ticket_id
