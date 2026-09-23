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
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        
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
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        
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
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        
        path.write_text("{ broken")
        bak_path.write_text("{ also broken")
        
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_read_checkpoint_backup_creation_on_success(self, temp_state_dir):
        """Successful read should update/create backup."""
        bot_name = "backup_bot"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        
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


class TestSetStateDir:
    """Test set_state_dir function."""

    def test_set_state_dir_creates_directory(self, tmp_path):
        """set_state_dir sets _STATE_DIR and creates the directory."""
        new_dir = tmp_path / "new_state"
        assert not new_dir.exists()
        checkpoint_manager.set_state_dir(new_dir)
        assert new_dir.exists()
        assert checkpoint_manager._STATE_DIR == new_dir
        # Restore default
        checkpoint_manager._STATE_DIR = tmp_path / "state"


class TestReadCheckpointEdgeCases:
    """Covers remaining read_checkpoint branches for 100% coverage."""

    def test_bak_too_large_returns_none(self, temp_state_dir):
        """Primary missing, backup exists but exceeds MAX size → returns None."""
        bot_name = "oversized_bak"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        # Create a backup that exceeds the size limit
        large_data = "x" * (checkpoint_manager._MAX_CHECKPOINT_BYTES + 100)
        bak_path.write_text(large_data)
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_bak_valid_dict_restored(self, temp_state_dir):
        """Primary missing, backup has valid dict → restored from backup."""
        bot_name = "restore_bak"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        good_data = {"recovered": True, "status": "ok"}
        bak_path.write_text(json.dumps(good_data))
        assert not path.exists()
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result == good_data

    def test_corrupt_primary_quarantine_and_unlink_fail(self, temp_state_dir):
        """Primary corrupt, quarantine move fails, unlink also fails → returns None."""
        bot_name = "quarantine_fail_bot"
        path = checkpoint_manager.checkpoint_path(bot_name)
        path.write_text("{ invalid json")
        # No backup file exists
        with patch("pathlib.Path.replace", side_effect=OSError("permission denied")):
            with patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
                result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_generic_exception_returns_none(self, temp_state_dir):
        """Unexpected exception in read_checkpoint → returns None."""
        bot_name = "generic_exc_bot"
        path = checkpoint_manager.checkpoint_path(bot_name)
        path.write_text(json.dumps({"key": "val"}))
        with patch("pathlib.Path.read_text", side_effect=RuntimeError("unexpected")):
            result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_non_dict_primary_quarantine_and_unlink_fail(self, temp_state_dir):
        """Primary is a non-dict JSON, quarantine fails, unlink also fails."""
        bot_name = "nondict_quarantine_fail"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        path.write_text(json.dumps(["list", "not", "dict"]))
        with patch("pathlib.Path.replace", side_effect=OSError("permission denied")):
            with patch("pathlib.Path.unlink", side_effect=OSError("permission denied")):
                result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_non_dict_primary_bak_too_large(self, temp_state_dir):
        """Primary is non-dict JSON, backup exists but too large → returns None."""
        bot_name = "nondict_bak_large"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        path.write_text(json.dumps([1, 2, 3]))
        large_bak = "x" * (checkpoint_manager._MAX_CHECKPOINT_BYTES + 100)
        bak_path.write_text(large_bak)
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_non_dict_primary_fallback_corrupt_bak(self, temp_state_dir):
        """Primary is non-dict, backup is corrupt JSON → returns None."""
        bot_name = "nondict_corrupt_bak"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        path.write_text(json.dumps([1, 2, 3]))
        bak_path.write_text("{ invalid")
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_corrupt_primary_bak_non_dict_fallback(self, temp_state_dir):
        """Primary corrupt, backup is non-dict JSON → returns None."""
        bot_name = "corrupt_bak_nondict"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        path.write_text("{ corrupt")
        bak_path.write_text(json.dumps([1, 2, 3]))
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_corrupt_primary_bak_oversized(self, temp_state_dir):
        """Primary corrupt, quarantine succeeds, backup too large → returns None."""
        bot_name = "corrupt_bak_oversized"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        path.write_text("{ corrupt json")
        large_bak = "x" * (checkpoint_manager._MAX_CHECKPOINT_BYTES + 100)
        bak_path.write_text(large_bak)
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_bak_valid_json_but_not_dict_from_bak_path(self, temp_state_dir):
        """Primary missing, backup has valid JSON that's not a dict → returns None."""
        bot_name = "bak_not_dict"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        # Backup has valid JSON but it's a list, not a dict
        bak_path.write_text(json.dumps([1, 2, 3]))
        assert not path.exists()
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_bak_os_error_on_read(self, temp_state_dir):
        """Primary missing, backup passes size but read_text raises OSError → returns None."""
        bot_name = "bak_os_error"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        # Write valid data so stat() passes
        bak_path.write_text(json.dumps({"key": "val"}))
        # Now make read_text fail
        with patch.object(Path, "read_text", side_effect=OSError("disk error")):
            result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_non_dict_primary_bak_read_os_error(self, temp_state_dir):
        """Non-dict primary, quarantine ok, backup passes size but read fails → returns None."""
        bot_name = "nondict_bak_oserror"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        path.write_text(json.dumps([1, 2, 3]))
        bak_path.write_text(json.dumps({"key": "val"}))
        # Patch only the backup's read_text (not the primary's)
        original_read_text = Path.read_text

        call_count = {"n": 0}

        def selective_read_text(self, *args, **kwargs):
            call_count["n"] += 1
            # The second read_text call is the backup
            if "bak" in str(self) and call_count["n"] > 1:
                raise OSError("disk error")
            return original_read_text(self, *args, **kwargs)

        with patch.object(Path, "read_text", selective_read_text):
            result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_raw_size_exceeds_max_after_read(self, temp_state_dir):
        """Primary exists and parses, but raw bytes exceed MAX → returns None."""
        bot_name = "oversized_raw"
        path = checkpoint_manager.checkpoint_path(bot_name)
        # Create data that's valid JSON but the raw bytes exceed the limit
        # We need to bypass the stat() check by mocking stat to return small size
        # but actual read to return large content
        large_content = json.dumps({"data": "x" * (checkpoint_manager._MAX_CHECKPOINT_BYTES + 100)})
        path.write_text(large_content)
        original_stat = path.stat
        def fake_stat():
            s = MagicMock()
            s.st_size = 100  # Under limit for stat check
            return s
        with patch("pathlib.Path.stat", side_effect=fake_stat):
            result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_primary_oversized_from_stat(self, temp_state_dir):
        """Primary exists, stat says too large → returns None immediately."""
        bot_name = "stat_oversized"
        path = checkpoint_manager.checkpoint_path(bot_name)
        path.write_text(json.dumps({"data": "small but lie"}))
        def fake_stat_large():
            s = MagicMock()
            s.st_size = checkpoint_manager._MAX_CHECKPOINT_BYTES + 1000
            return s
        with patch("pathlib.Path.stat", side_effect=fake_stat_large):
            result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_primary_stat_os_error_falls_through_to_bak(self, temp_state_dir):
        """Primary exists but stat() raises OSError → falls through to bak."""
        bot_name = "stat_oserror"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        good_data = {"recovered_from_bak": True}
        path.write_text(json.dumps({"corrupt": True}))
        bak_path.write_text(json.dumps(good_data))
        # Make stat raise OSError
        with patch("pathlib.Path.stat", side_effect=OSError("permission denied")):
            result = checkpoint_manager.read_checkpoint(bot_name)
        # Should fall through to read primary text (since stat failed, it continues)
        # Then quarantine succeeds, and bak fallback gives us the data
        assert result is not None

    def test_non_dict_primary_bak_too_large_quarantine_ok(self, temp_state_dir):
        """Non-dict primary, quarantine succeeds, backup too large → None."""
        bot_name = "nondict_bak_large_qok"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        path.write_text(json.dumps([1, 2, 3]))
        # Create oversized backup
        large_bak = "x" * (checkpoint_manager._MAX_CHECKPOINT_BYTES + 100)
        bak_path.write_text(large_bak)
        result = checkpoint_manager.read_checkpoint(bot_name)
        assert result is None

    def test_bak_write_text_os_error(self, temp_state_dir):
        """Successful read but bak write fails → still returns data."""
        bot_name = "bak_write_fail"
        path = checkpoint_manager.checkpoint_path(bot_name)
        bak_path = checkpoint_manager.checkpoint_backup_path(path)
        data = {"key": "value"}
        path.write_text(json.dumps(data))
        with patch("pathlib.Path.write_text", side_effect=OSError("disk full")):
            result = checkpoint_manager.read_checkpoint(bot_name)
        assert result == data


class TestReadStateFileEdgeCases:
    """Test _read_state_file edge cases."""

    def test_read_state_file_exists_but_os_error(self, temp_state_dir):
        """State file exists but read_text raises OSError."""
        bot_name = "os_err_state"
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text("{}")
        with patch("pathlib.Path.read_text", side_effect=OSError("permission")):
            result = checkpoint_manager._read_state_file(bot_name)
        assert result == {"_state_error": "corrupt"}


class TestUpdateBotStateEdgeCases:
    """Test update_bot_state edge cases."""

    def test_update_bot_state_existing_corrupt_json(self, temp_state_dir):
        """update_bot_state with existing corrupt JSON → resets to {}."""
        bot_name = "corrupt_update"
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text("{ invalid json!!!")
        checkpoint_manager.update_bot_state(bot_name, "running")
        data = json.loads(state_file.read_text())
        assert data["status"] == "running"

    def test_update_bot_state_existing_non_dict(self, temp_state_dir):
        """update_bot_state with existing non-dict data → resets to {}."""
        bot_name = "nondict_update"
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text(json.dumps(["list", "not", "dict"]))
        checkpoint_manager.update_bot_state(bot_name, "stopped")
        data = json.loads(state_file.read_text())
        assert data["status"] == "stopped"
        assert isinstance(data, dict)

    def test_update_bot_state_lock_exception(self, temp_state_dir):
        """update_bot_state when lock acquisition fails → logs error."""
        bot_name = "lock_fail_update"
        with patch.object(checkpoint_manager, "_state_write_lock", side_effect=OSError("lock error")):
            # Should not raise, just log
            checkpoint_manager.update_bot_state(bot_name, "running")


class TestManifestRestartBudgetEdgeCases:
    """Test _manifest_restart_budget_exceeded edge cases."""

    def test_max_restarts_zero(self, temp_state_dir):
        """max_restarts=0 → budget not exceeded."""
        manifest = {"name": "zero_budget", "max_restarts": 0}
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, time.time())
        assert not exceeded
        assert reason == ""

    def test_max_restarts_negative(self, temp_state_dir):
        """max_restarts negative → budget not exceeded."""
        manifest = {"name": "neg_budget", "max_restarts": -1}
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, time.time())
        assert not exceeded
        assert reason == ""

    def test_max_restarts_non_int_string(self, temp_state_dir):
        """max_restarts is a non-numeric string → falls back to 5."""
        manifest = {"name": "bad_budget", "max_restarts": "abc"}
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, time.time())
        # Falls back to max_r=5, with no state → not exceeded
        assert not exceeded

    def test_state_corrupt(self, temp_state_dir):
        """State file corrupt → returns exceeded with state-corrupt reason."""
        bot_name = "corrupt_state_budget"
        manifest = {"name": bot_name, "max_restarts": 5}
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text("{ corrupt")
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, time.time())
        assert exceeded
        assert "state-corrupt" in reason

    def test_timestamps_not_list(self, temp_state_dir):
        """restart_timestamps is a string instead of list → treated as empty."""
        bot_name = "not_list_ts"
        manifest = {"name": bot_name, "max_restarts": 5}
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text(json.dumps({"restart_timestamps": "not a list"}))
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, time.time())
        assert not exceeded

    def test_timestamps_contain_non_numeric(self, temp_state_dir):
        """restart_timestamps contain non-numeric values → filtered out."""
        bot_name = "mixed_ts"
        manifest = {"name": bot_name, "max_restarts": 5}
        now = time.time()
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text(json.dumps({
            "restart_timestamps": [now - 10, "not_a_number", now - 20]
        }))
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, now)
        # 2 recent timestamps, max_r=5 → not exceeded
        assert not exceeded

    def test_no_name_in_manifest(self, temp_state_dir):
        """Manifest missing 'name' key → uses empty string."""
        manifest = {"max_restarts": 5}
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, time.time())
        assert not exceeded

    def test_missing_max_restarts_key(self, temp_state_dir):
        """Manifest missing 'max_restarts' → defaults to 5."""
        manifest = {"name": "default_budget"}
        exceeded, reason = checkpoint_manager._manifest_restart_budget_exceeded(manifest, time.time())
        assert not exceeded


class TestManifestErrorDisabledEdgeCases:
    """Test _manifest_error_disabled edge cases."""

    def test_state_corrupt(self, temp_state_dir):
        """State corrupt → disabled with state-corrupt reason."""
        bot_name = "err_corrupt_state"
        manifest = {"name": bot_name}
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text("{ corrupt")
        disabled, reason = checkpoint_manager._manifest_error_disabled(manifest)
        assert disabled
        assert "state-corrupt" in reason

    def test_consecutive_errors_non_int(self, temp_state_dir):
        """consecutive_errors is a non-numeric string → treated as 0."""
        bot_name = "err_nonint"
        manifest = {"name": bot_name}
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text(json.dumps({"consecutive_errors": "not_a_number"}))
        disabled, reason = checkpoint_manager._manifest_error_disabled(manifest, max_consecutive=3)
        assert not disabled

    def test_no_name_in_manifest(self, temp_state_dir):
        """Manifest missing 'name' → uses empty string, no state file."""
        manifest = {}
        disabled, reason = checkpoint_manager._manifest_error_disabled(manifest)
        assert not disabled


class TestManifestRestartRecord:
    """Test _manifest_restart_record function."""

    def test_restart_record_creates_entry(self, temp_state_dir):
        """_manifest_restart_record appends timestamp to state."""
        bot_name = "restart_rec_bot"
        now = time.time()
        checkpoint_manager._manifest_restart_record(bot_name, now)
        state = checkpoint_manager._read_state_file(bot_name)
        assert "restart_timestamps" in state
        assert now in state["restart_timestamps"]
        assert state["last_update"] == now
        assert state["last_restart"] == now
        assert state["restart_count"] == 1

    def test_restart_record_with_existing_non_list_timestamps(self, temp_state_dir):
        """_manifest_restart_record handles non-list restart_timestamps."""
        bot_name = "restart_rec_bad_ts"
        now = time.time()
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text(json.dumps({"restart_timestamps": "not a list"}))
        checkpoint_manager._manifest_restart_record(bot_name, now)
        state = checkpoint_manager._read_state_file(bot_name)
        assert isinstance(state["restart_timestamps"], list)
        assert now in state["restart_timestamps"]

    def test_restart_record_with_non_numeric_timestamps(self, temp_state_dir):
        """_manifest_restart_record filters out non-numeric timestamps."""
        bot_name = "restart_rec_mixed"
        now = time.time()
        state_file = temp_state_dir / f"{bot_name}.state.json"
        state_file.write_text(json.dumps({"restart_timestamps": ["bad", 42, True]}))
        checkpoint_manager._manifest_restart_record(bot_name, now)
        state = checkpoint_manager._read_state_file(bot_name)
        # Only 42 and True (=1.0) are numeric
        assert now in state["restart_timestamps"]
        assert state["restart_count"] >= 1


class TestWriteJsonAtomic:
    """Test _write_json_atomic edge cases."""

    def test_write_non_dict_non_list(self, temp_state_dir):
        """_write_json_atomic with a non-dict/list value uses str()."""
        path = temp_state_dir / "str.txt"
        checkpoint_manager._write_json_atomic(path, "just a string")
        assert path.read_text() == "just a string"


class TestCheckpointBackupPathNonJson:
    """Test checkpoint_backup_path for non-.json suffix."""

    def test_non_json_suffix(self, temp_state_dir):
        """Non-.json suffix → appends .bak."""
        path = temp_state_dir / "testfile.dat"
        result = checkpoint_manager.checkpoint_backup_path(path)
        assert result == Path(str(path) + ".bak")


class TestStateWriteLockWindows:
    """Regression test for Windows/fallback path in _state_write_lock.

    Verifies that state locking works without AttributeError when the
    underlying platform lacks both fcntl and msvcrt (forcing the no-op
    fallback in codebot.locks.flock).
    """

    def test_state_write_lock_no_fcntl_no_attribute_error(self, temp_state_dir):
        """_state_write_lock yields without AttributeError on no-lock platforms.

        Simulates a platform without fcntl or msvcrt by temporarily replacing
        codebot.locks.flock with a no-op function, then verifies that
        _state_write_lock still enters/exits cleanly and creates the .state.lock
        file.
        """
        bot_name = "windows_sim_bot"
        lock_path = temp_state_dir / f"{bot_name}.state.lock"

        # Save original flock
        import codebot.locks
        import codebot.file_lock
        original_flock = codebot.locks.flock
        original_cm_flock = checkpoint_manager.flock

        # Replace with a no-op that proves it was called
        call_count = {"count": 0}

        def noop_flock(fd, operation):
            call_count["count"] += 1

        try:
            # Monkeypatch the flock used by checkpoint_manager (imported via
            # from codebot.file_lock import flock, so patching locks/file_lock
            # alone does not affect checkpoint_manager.flock)
            codebot.locks.flock = noop_flock
            codebot.file_lock.flock = noop_flock
            checkpoint_manager.flock = noop_flock

            # Enter the context manager - should not raise AttributeError
            with checkpoint_manager._state_write_lock(bot_name):
                # Verify lock file was created
                assert lock_path.exists()

            # Verify flock was called twice (LOCK_EX on enter, LOCK_UN on exit)
            assert call_count["count"] == 2

        finally:
            # Restore original flock
            codebot.locks.flock = original_flock
            codebot.file_lock.flock = original_flock
            checkpoint_manager.flock = original_cm_flock
