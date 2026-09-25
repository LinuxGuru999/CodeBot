#!/usr/bin/env python3
"""Security tests for _sweep_orphan_claims file size validation.

Verifies CB-87087: _sweep_orphan_claims enforces max file size (1KB) before
json.loads() to prevent memory exhaustion from maliciously large claim files.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest


class TestSweepOrphanClaimsFileSize:
    """Verify oversized claim files are skipped during sweep."""

    def test_oversized_claim_file_skipped(self, tmp_path: Path):
        """Claim file >1KB must be skipped without parsing (no memory exhaustion)."""
        import codebot.ticket_dispatcher as td

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        # Create an oversized claim file (>1024 bytes)
        oversized_data = {
            "worker": "dead-bot",
            "at": time.time() - 9999,
            "padding": "X" * 2000,  # Well over 1KB limit
        }
        oversized_file = claims_dir / "T-0001.dead-bot.json"
        oversized_file.write_text(json.dumps(oversized_data), encoding="utf-8")

        # Verify the file is indeed oversized
        assert oversized_file.stat().st_size > td.MAX_CLAIM_FILE_SIZE

        bots: dict[str, Any] = {}  # No active bots

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(td, "STATE_DIR", tmp_path)
            swept = td._sweep_orphan_claims(bots)

        # Oversized file should NOT have been swept (skipped before parse)
        assert swept == 0
        # File should still exist on disk (not deleted, just ignored)
        assert oversized_file.exists()

    def test_normal_claim_file_processed(self, tmp_path: Path):
        """Claim file <=1KB with inactive worker should be swept normally."""
        import codebot.ticket_dispatcher as td

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        # Create a normal-sized claim file for an orphan worker
        normal_data = {"worker": "dead-bot", "at": time.time() - 9999}
        normal_file = claims_dir / "T-0002.dead-bot.json"
        normal_file.write_text(json.dumps(normal_data), encoding="utf-8")

        assert normal_file.stat().st_size <= td.MAX_CLAIM_FILE_SIZE

        bots: dict[str, Any] = {}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(td, "STATE_DIR", tmp_path)
            swept = td._sweep_orphan_claims(bots)

        # Normal orphan claim should be swept
        assert swept == 1
        assert not normal_file.exists()

    def test_mixed_sizes_only_normal_swept(self, tmp_path: Path):
        """With both oversized and normal orphan claims, only normal ones are swept."""
        import codebot.ticket_dispatcher as td

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        # Oversized file — should be skipped
        big_data = {
            "worker": "dead-big",
            "at": time.time() - 9999,
            "payload": "Y" * 2000,
        }
        big_file = claims_dir / "T-0003.dead-big.json"
        big_file.write_text(json.dumps(big_data), encoding="utf-8")

        # Normal file — should be swept
        small_data = {"worker": "dead-small", "at": time.time() - 9999}
        small_file = claims_dir / "T-0004.dead-small.json"
        small_file.write_text(json.dumps(small_data), encoding="utf-8")

        bots: dict[str, Any] = {}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(td, "STATE_DIR", tmp_path)
            swept = td._sweep_orphan_claims(bots)

        assert swept == 1  # Only the small one
        assert big_file.exists()  # Big one untouched
        assert not small_file.exists()  # Small one removed

    def test_exactly_at_limit_processed(self, tmp_path: Path):
        """File exactly at MAX_CLAIM_FILE_SIZE boundary should be processed."""
        import codebot.ticket_dispatcher as td

        claims_dir = tmp_path / "claims"
        claims_dir.mkdir(parents=True, exist_ok=True)

        # Craft a JSON payload that fits within the limit
        base = {"worker": "edge-case-bot", "at": time.time() - 9999}
        serialized = json.dumps(base)
        padding_needed = td.MAX_CLAIM_FILE_SIZE - len(serialized.encode("utf-8"))
        if padding_needed > 0:
            # Add a field whose value fills remaining space precisely
            pad_key = "p"
            pad_val_len = padding_needed - len(f'\', "{pad_key}": "') - 2  # quotes + comma-space overhead
            if pad_val_len > 0:
                base[pad_key] = "Z" * pad_val_len
                serialized = json.dumps(base)

        edge_file = claims_dir / "T-0005.edge-case-bot.json"
        edge_file.write_text(serialized, encoding="utf-8")

        # Ensure it's at or under the limit
        assert edge_file.stat().st_size <= td.MAX_CLAIM_FILE_SIZE

        bots: dict[str, Any] = {}

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(td, "STATE_DIR", tmp_path)
            swept = td._sweep_orphan_claims(bots)

        # Should be swept since it's within limits and worker is orphaned
        assert swept == 1
        assert not edge_file.exists()
