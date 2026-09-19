#!/usr/bin/env python3
"""Tests for Checkpoint Manager.

Covers atomic writes, recovery from corruption, state management,
and restart budget logic.
"""

import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Import the module under test
from codebot import checkpoint_manager


@pytest.fixture
def temp_state_dir(tmp_path):
    """Provide a temporary directory for state operations."""
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    # Patch the global _STATE_DIR in the module
    with patch.object(checkpoint_manager, '_STATE_DIR', state_dir):
        yield state_dir


class TestCheckpointBasics:
    """Test basic checkpoint read/write operations."""

    def test_checkpoint_path_generation(self, temp_state_dir):
        """Verify checkpoint path is constructed correctly."""
        expected = temp_state_dir / "test_bot.checkpoint.json"
        actual = checkpoint_manager.checkpoint_path("test_bot")
        assert actual == expected

    def test_write_checkpoint_handoff_creates_valid_json(self, temp_state_dir):
        """Test that write_checkpoint_handoff creates a valid JSON file with metadata."""
        bot_name = "test_bot"
        payload = {"task": "scan", "progress": 50}
        
        before_time = time.time()
        checkpoint_manager.write_checkpoint_handoff(bot_name, payload)
        after_time = time.time()
        
        path = checkpoint_manager.checkpoint_path(bot_name)
        assert path.exists()
        
        data = json.loads(path.read_text())
        assert data["bot"] == bot_name
        assert data["task"] == "scan"
        assert data["progress"] == 50
        assert "updated_at" in data
        assert before_time <= data["updated_at"] <= after_time
        assert "updated_at_human" in data

    def test_init_checkpoint_creates_file_if_missing(self, temp_state_dir):
        """Test init_checkpoint creates a new file with defaults."""
        bot_name = "new_bot"
        path = checkpoint_manager.checkpoint_path(bot_name)
        
        assert not path.exists()
        checkpoint_manager.init_checkpoint(bot_name, scan_iteration=5)
        assert path.exists()
        
        data = json.loads(path.read_text())
        assert data["scan_iteration"] == 5
        assert data["current_task"] is None
        assert data["completed"] == []
        assert data["reason"] == "init"

    def test_init_checkpoint_skips_existing_file(self, temp_state_dir):
        """Test init_checkpoint does not overwrite existing file."""
        bot_name = "existing_bot"
        path = checkpoint_manager.checkpoint_path(bot_name)
        
        # Create manually
        path.write_text(json.dumps({"custom": "data"}))
        
        checkpoint_manager.init_checkpoint(bot_name)
        
        data = json.loads(path.read_text())
        assert data == {"custom": "data"}

    def test_read_checkpoint_returns_none_if_missing(self, temp_state_dir):
        """Test read_checkpoint returns None for non-existent file."""
        result = checkpoint_manager.read_checkpoint("missing_bot")
        assert result is None

    def test_read_checkpoint_returns_data_if_valid(self, temp_state_dir):
        """Test read_checkpoint parses valid JSON."""
        bot_name = "valid_bot"
        expected_data = {"status": "running", "step": 3}
        path = checkpoint_manager.checkpoint_path(bot_name)
        path.write_text(json.dumps(expected_data))
        
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result == expected_data


class TestCheckpointRecovery:
    """Test recovery from corrupt or missing checkpoints."""

    def test_read_checkpoint_falls_back_to_bak_if_primary_corrupt(self, temp_state_dir):
        """If primary is corrupt JSON, fallback to .bak."""
        bot_name = "corrupt_bot"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = path.with_suffix(".checkpoint.bak")
        
        good_data = {"recovered": True}
        bad_data = "{ invalid json"
        
        # Write good backup first
        bak_path.write_text(json.dumps(good_data))
        # Write corrupt primary
        path.write_text(bad_data)
        
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result == good_data
        # Verify primary was moved/deleted or handled (logic moves it to bak or deletes)
        # The implementation tries to rename corrupt to bak, but bak exists, so it might unlink primary
        assert not path.exists() or path.read_text() != bad_data

    def test_read_checkpoint_handles_non_dict_json(self, temp_state_dir):
        """If JSON is not a dict (e.g., list), treat as corrupt and fallback."""
        bot_name = "list_bot"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = path.with_suffix(".checkpoint.bak")
        
        good_data = {"valid": "dict"}
        bad_data = ["not", "a", "dict"]
        
        bak_path.write_text(json.dumps(good_data))
        path.write_text(json.dumps(bad_data))
        
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result == good_data

    def test_read_checkpoint_returns_none_if_both_corrupt(self, temp_state_dir):
        """If both primary and backup are corrupt, return None."""
        bot_name = "double_corrupt_bot"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = path.with_suffix(".checkpoint.bak")
        
        path.write_text("{ broken")
        bak_path.write_text("{ also broken")
        
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_read_checkpoint_backup_creation_on_success(self, temp_state_dir):
        """Successful read should update/create backup."""
        bot_name = "backup_bot"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = path.with_suffix(".checkpoint.bak")
        
        data = {"keep": "safe"}
        path.write_text(json.dumps(data))
        
        # Ensure no bak exists initially
        if bak_path.exists():
            bak_path.unlink()
            
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result == data
        assert bak_path.exists()
        assert json.loads(bak_path.read_text()) == data


class TestBotStateManagement:
    """Test bot state file operations (separate from checkpoint)."""

    def test_update_bot_state_creates_file(self, temp_state_dir):
        """Verify update_bot_state creates state file."""
        bot_name = "state_bot"
        state_file = temp_state_dir / f"{bot_name}.state.json"
        
        assert not state_file.exists()
        checkpoint_manager.update_bot_state(bot_name, "running", restart_count=1)
        assert state_file.exists()
        
        data = json.loads(state_file.read_text())
        assert data["status"] == "running"
        assert data["restart_count"] == 1
        assert "last_update" in data

    def test_update_bot_state_overwrites_status(self, temp_state_dir):
        """Verify subsequent calls update fields."""
        bot_name = "update_bot"
        
        checkpoint_manager.update_bot_state(bot_name, "starting")
        time.sleep(0.01) # Ensure timestamp difference if needed, though logic overwrites
        checkpoint_manager.update_bot_state(bot_name, "stopped", consecutive_errors=5)
        
        state_file = temp_state_dir / f"{bot_name}.state.json"
        data = json.loads(state_file.read_text())
        
        assert data["status"] == "stopped"
        assert data["consecutive_errors"] == 5

    def test_read_state_file_returns_empty_dict_if_missing(self, temp_state_dir):
        """_read_state_file should return empty dict for missing file."""
        # Access private function via module
        result = checkpoint_manager._read_state_file("nonexistent")
        assert result == {}

    def test_read_state_file_handles_corrupt_json(self, temp_state_dir):
        """_read_state_file should handle corrupt JSON gracefully."""
        bot_name = "corrupt_state_bot"
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text("{ invalid")
        
        result = checkpoint_manager._read_state_file(bot_name)
        assert result == {"_state_error": "corrupt"}


class TestRestartBudgetAndErrorLogic:
    """Test logic for restart budgets and error disabling."""

    def test_manifest_restart_budget_not_exceeded_initially(self, temp_state_dir):
        """Fresh manifest should not exceed budget."""
        manifest = {"name": "fresh_bot", "max_restarts": 3}
        now = time.time()
        
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, now)
        assert not exceeded
        assert reason == ""

    def test_manifest_restart_budget_exceeded(self, temp_state_dir):
        """Exceeding max_restarts within 1h should trigger flag."""
        bot_name = "budget_bot"
        manifest = {"name": bot_name, "max_restarts": 2}
        now = time.time()
        
        # Simulate state with 2 recent restarts
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_data = {
            "restart_timestamps": [now - 100, now - 200],
            "restart_count": 2
        }
        state_file.write_text(json.dumps(state_data))
        
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, now)
        assert exceeded
        assert "restart-budget-exceeded" in reason

    def test_manifest_restart_budget_old_timestamps_ignored(self, temp_state_dir):
        """Timestamps older than 1h should not count."""
        bot_name = "old_bot"
        manifest = {"name": bot_name, "max_restarts": 1}
        now = time.time()
        
        # 2 hours ago
        old_time = now - 7300 
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_data = {"restart_timestamps": [old_time]}
        state_file.write_text(json.dumps(state_data))
        
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, now)
        assert not exceeded

    def test_manifest_error_disabled_trigger(self, temp_state_dir):
        """Consecutive errors >= threshold should disable bot."""
        bot_name = "error_bot"
        manifest = {"name": bot_name}
        
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_data = {"consecutive_errors": 3}
        state_file.write_text(json.dumps(state_data))
        
        disabled, reason = checkpoint_manager._manifest_error_disabled(manifest, max_consecutive=3)
        assert disabled
        assert "error-disabled" in reason

    def test_manifest_error_disabled_not_triggered_below_threshold(self, temp_state_dir):
        """Errors below threshold should not disable."""
        bot_name = "safe_bot"
        manifest = {"name": bot_name}
        
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_data = {"consecutive_errors": 2}
        state_file.write_text(json.dumps(state_data))
        
        disabled, reason = checkpoint_manager._manifest_error_disabled(manifest, max_consecutive=3)
        assert not disabled

    def test_public_api_restart_budget(self, temp_state_dir):
        """Public wrapper is_manifest_restart_budget_exceeded works."""
        bot_name = "public_bot"
        manifest = {"name": bot_name, "max_restarts": 1}
        now = time.time()
        
        # Setup state to exceed
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text(json.dumps({"restart_timestamps": [now - 10]}))
        
        assert checkpoint_manager.is_manifest_restart_budget_exceeded(manifest, now) is True

    def test_public_api_error_disabled(self, temp_state_dir):
        """Public wrapper is_manifest_error_disabled works."""
        bot_name = "public_error_bot"
        manifest = {"name": bot_name}
        
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text(json.dumps({"consecutive_errors": 5}))
        
        assert checkpoint_manager.is_manifest_error_disabled(manifest) is True
