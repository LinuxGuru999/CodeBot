#!/usr/bin/env python3
"""Tests for alignment_events module."""

import json
import os
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codebot.alignment_events import (
    list_pending_events,
    mark_event_processed,
    set_dirs,
    write_alignment_event,
)


@pytest.fixture
def temp_state_dir(tmp_path):
    """Create a temporary state directory and configure alignment_events to use it."""
    state_dir = tmp_path / "state"
    logs_dir = tmp_path / "logs"
    state_dir.mkdir()
    logs_dir.mkdir()
    events_dir = state_dir / "alignment_events"
    events_dir.mkdir()
    
    # Configure the module to use our temp dirs
    set_dirs(state_dir, logs_dir)
    
    return {
        "state_dir": state_dir,
        "logs_dir": logs_dir,
        "events_dir": events_dir,
    }


class TestMarkEventProcessed:
    """Tests for mark_event_processed() atomic update and payload correctness."""
    
    def test_mark_event_processed_updates_payload_correctly(self, temp_state_dir):
        """Test that mark_event_processed correctly updates all fields."""
        events_dir = temp_state_dir["events_dir"]
        
        # Create an initial unprocessed event
        initial_event = {
            "bot": "test_bot",
            "exit_code": 0,
            "exit_reason": "clean",
            "processed": False,
            "processed_at": None,
            "exit_time": 1726742400.0,
        }
        event_file = events_dir / "test_bot.exit.json"
        event_file.write_text(json.dumps(initial_event))
        
        # Call mark_event_processed
        score = 0.95
        reward = 1.0
        verdict = "aligned"
        mark_event_processed(event_file, initial_event, score, reward, verdict)
        
        # Read the updated file
        updated_content = json.loads(event_file.read_text(encoding="utf-8"))
        
        # Verify all fields are updated correctly
        assert updated_content["processed"] is True
        assert updated_content["processed_at"] is not None
        assert isinstance(updated_content["processed_at"], float)
        assert updated_content["processed_at"] > 1726742400.0  # After exit_time
        assert updated_content["score"] == score
        assert updated_content["reward"] == reward
        assert updated_content["verdict"] == verdict
        
        # Verify original fields are preserved
        assert updated_content["bot"] == "test_bot"
        assert updated_content["exit_code"] == 0
        assert updated_content["exit_reason"] == "clean"
    
    def test_mark_event_processed_atomic_write_tmp_replace(self, temp_state_dir, monkeypatch):
        """Test that mark_event_processed uses atomic tmp+replace pattern."""
        events_dir = temp_state_dir["events_dir"]
        
        # Create an initial event
        initial_event = {
            "bot": "atomic_bot",
            "exit_code": 0,
            "processed": False,
        }
        event_file = events_dir / "atomic_bot.exit.json"
        event_file.write_text(json.dumps(initial_event))
        
        # Track if tmp file was created and replaced
        tmp_file_created = []
        original_replace = Path.replace
        
        def mock_replace(self, target):
            # Verify tmp file exists before replace
            assert self.exists(), f"Tmp file {self} should exist before replace"
            assert str(self).endswith(".tmp"), f"Tmp file should end with .tmp: {self}"
            tmp_file_created.append(str(self))
            return original_replace(self, target)
        
        with patch.object(Path, 'replace', mock_replace):
            mark_event_processed(event_file, initial_event, 0.9, 0.5, "aligned")
        
        # Verify tmp file was used
        assert len(tmp_file_created) == 1
        assert "atomic_bot.exit.json.tmp" in tmp_file_created[0]
        
        # Verify final file exists and is valid
        assert event_file.exists()
        final_content = json.loads(event_file.read_text(encoding="utf-8"))
        assert final_content["processed"] is True
    
    def test_mark_event_processed_fail_open_on_io_error(self, temp_state_dir, caplog):
        """Test that mark_event_processed logs warning but doesn't raise on I/O error."""
        import logging
        caplog.set_level(logging.WARNING)
        
        events_dir = temp_state_dir["events_dir"]
        
        # Create an initial event
        initial_event = {
            "bot": "error_bot",
            "exit_code": 1,
            "processed": False,
        }
        event_file = events_dir / "error_bot.exit.json"
        event_file.write_text(json.dumps(initial_event))
        
        # Mock write_text to raise an exception
        with patch.object(Path, 'write_text', side_effect=PermissionError("Simulated I/O error")):
            # Should NOT raise
            mark_event_processed(event_file, initial_event, 0.5, 0.25, "misaligned")
        
        # Verify warning was logged
        assert len(caplog.records) == 1
        assert caplog.records[0].levelno == logging.WARNING
        assert "Failed to mark event processed" in caplog.records[0].message
        assert "Simulated I/O error" in caplog.records[0].message
        
        # Verify original file is unchanged (no partial write)
        unchanged_content = json.loads(event_file.read_text(encoding="utf-8"))
        assert unchanged_content["processed"] is False  # Should still be False
        assert "score" not in unchanged_content
    
    def test_mark_event_processed_various_verdicts(self, temp_state_dir):
        """Test mark_event_processed with different verdict strings."""
        events_dir = temp_state_dir["events_dir"]
        
        verdicts_to_test = ["aligned", "misaligned", "partial", "unknown", ""]
        
        for i, verdict in enumerate(verdicts_to_test):
            event = {
                "bot": f"verdict_bot_{i}",
                "exit_code": 0,
                "processed": False,
            }
            event_file = events_dir / f"verdict_bot_{i}.exit.json"
            event_file.write_text(json.dumps(event))
            
            mark_event_processed(event_file, event, 0.8, 0.5, verdict)
            
            updated = json.loads(event_file.read_text(encoding="utf-8"))
            assert updated["verdict"] == verdict
            assert updated["processed"] is True
    
    def test_mark_event_processed_score_reward_ranges(self, temp_state_dir):
        """Test mark_event_processed with various score/reward values."""
        events_dir = temp_state_dir["events_dir"]
        
        test_cases = [
            (0.0, 0.0),      # Minimum values
            (1.0, 1.0),      # Maximum values
            (0.5, 0.5),      # Mid values
            (0.95, 1.0),     # High score, max reward
            (0.1, -0.5),     # Low score, negative reward
            (0.0, -1.0),     # Min score, min reward
        ]
        
        for i, (score, reward) in enumerate(test_cases):
            event = {
                "bot": f"range_bot_{i}",
                "exit_code": 0,
                "processed": False,
            }
            event_file = events_dir / f"range_bot_{i}.exit.json"
            event_file.write_text(json.dumps(event))
            
            mark_event_processed(event_file, event, score, reward, "test_verdict")
            
            updated = json.loads(event_file.read_text(encoding="utf-8"))
            assert updated["score"] == score
            assert updated["reward"] == reward
            assert updated["processed"] is True
    
    def test_mark_event_processed_preserves_other_fields(self, temp_state_dir):
        """Test that mark_event_processed preserves all original event fields."""
        events_dir = temp_state_dir["events_dir"]
        
        # Create event with many custom fields
        initial_event = {
            "bot": "preserve_bot",
            "exit_code": 0,
            "exit_reason": "clean",
            "exit_time": 1726742400.0,
            "exit_time_human": "2024-09-19T12:00:00Z",
            "run_duration": 120.5,
            "started_at": 1726742280.0,
            "log_path": "logs/preserve_bot.log",
            "stream_path": "logs/preserve_bot.stream.json",
            "checkpoint_path": "state/preserve_bot.checkpoint.json",
            "heartbeat_age_at_exit": 5.2,
            "log_bytes_at_exit": 1024,
            "processed": False,
            "processed_at": None,
            "version": 1,
            "custom_field": "should_be_preserved",
            "nested": {"key": "value"},
        }
        event_file = events_dir / "preserve_bot.exit.json"
        event_file.write_text(json.dumps(initial_event))
        
        mark_event_processed(event_file, initial_event, 0.9, 0.8, "aligned")
        
        updated = json.loads(event_file.read_text(encoding="utf-8"))
        
        # Verify all original fields are preserved
        for key, value in initial_event.items():
            if key not in ("processed", "processed_at"):
                assert key in updated, f"Field {key} should be preserved"
                assert updated[key] == value, f"Field {key} should have same value"
        
        # Verify only the expected fields were added/changed
        assert updated["processed"] is True
        assert updated["processed_at"] is not None
        assert updated["score"] == 0.9
        assert updated["reward"] == 0.8
        assert updated["verdict"] == "aligned"


class TestListPendingEvents:
    """Existing tests for list_pending_events() - preserved from original file."""
    
    def test_list_pending_returns_only_unprocessed(self, temp_state_dir):
        """Test that only events with processed=False are returned."""
        events_dir = temp_state_dir["events_dir"]
        
        # Create a processed event
        processed_event = {
            "bot": "bot_processed",
            "exit_code": 0,
            "exit_reason": "clean",
            "processed": True,
            "processed_at": 1726742400.0,
            "score": 0.95,
            "reward": 1.0,
            "verdict": "aligned",
        }
        processed_file = events_dir / "bot_processed.exit.json"
        processed_file.write_text(json.dumps(processed_event))
        
        # Create an unprocessed event
        unprocessed_event = {
            "bot": "bot_unprocessed",
            "exit_code": 1,
            "exit_reason": "error",
            "processed": False,
            "processed_at": None,
        }
        unprocessed_file = events_dir / "bot_unprocessed.exit.json"
        unprocessed_file.write_text(json.dumps(unprocessed_event))
        
        # Create another unprocessed event
        unprocessed_event2 = {
            "bot": "bot_unprocessed2",
            "exit_code": None,
            "exit_reason": "stuck",
            "processed": False,
            "processed_at": None,
        }
        unprocessed_file2 = events_dir / "bot_unprocessed2.exit.json"
        unprocessed_file2.write_text(json.dumps(unprocessed_event2))
        
        pending = list_pending_events()
        
        # Should return exactly 2 unprocessed events
        assert len(pending) == 2
        
        # Extract bot names from results
        returned_bots = {event_data["bot"] for _, event_data in pending}
        assert returned_bots == {"bot_unprocessed", "bot_unprocessed2"}
        
        # Verify processed event is NOT in results
        processed_bots = {event_data["bot"] for _, event_data in pending if event_data.get("processed")}
        assert len(processed_bots) == 0
    
    def test_list_pending_skips_corrupt_json(self, temp_state_dir):
        """Test that corrupt JSON files are skipped gracefully."""
        events_dir = temp_state_dir["events_dir"]
        
        # Create a valid unprocessed event
        valid_event = {
            "bot": "bot_valid",
            "exit_code": 0,
            "exit_reason": "clean",
            "processed": False,
        }
        valid_file = events_dir / "bot_valid.exit.json"
        valid_file.write_text(json.dumps(valid_event))
        
        # Create a corrupt JSON file
        corrupt_file = events_dir / "bot_corrupt.exit.json"
        corrupt_file.write_text("{ invalid json content \n missing quotes }")
        
        # Create another corrupt file (empty)
        empty_corrupt_file = events_dir / "bot_empty_corrupt.exit.json"
        empty_corrupt_file.write_text("")
        
        # Create another valid unprocessed event
        valid_event2 = {
            "bot": "bot_valid2",
            "exit_code": 1,
            "exit_reason": "error",
            "processed": False,
        }
        valid_file2 = events_dir / "bot_valid2.exit.json"
        valid_file2.write_text(json.dumps(valid_event2))
        
        pending = list_pending_events()
        
        # Should return exactly 2 valid unprocessed events (corrupt ones skipped)
        assert len(pending) == 2
        
        returned_bots = {event_data["bot"] for _, event_data in pending}
        assert returned_bots == {"bot_valid", "bot_valid2"}
    
    def test_list_pending_empty_directory(self, temp_state_dir):
        """Test that empty directory returns empty list."""
        events_dir = temp_state_dir["events_dir"]
        
        # Ensure directory is empty (it should be from fixture)
        assert len(list(events_dir.glob("*.exit.json"))) == 0
        
        pending = list_pending_events()
        
        assert pending == []
        assert isinstance(pending, list)
    
    def test_list_pending_mixed_scenarios(self, temp_state_dir):
        """Test mixed scenario: valid, processed, corrupt, and edge cases."""
        events_dir = temp_state_dir["events_dir"]
        
        # Valid unprocessed
        (events_dir / "valid1.exit.json").write_text(json.dumps({"bot": "v1", "processed": False}))
        (events_dir / "valid2.exit.json").write_text(json.dumps({"bot": "v2", "processed": False}))
        
        # Processed (should be excluded)
        (events_dir / "proc1.exit.json").write_text(json.dumps({"bot": "p1", "processed": True}))
        (events_dir / "proc2.exit.json").write_text(json.dumps({"bot": "p2", "processed": True, "score": 0.8}))
        
        # Corrupt files (should be skipped)
        (events_dir / "corrupt1.exit.json").write_text("not json at all")
        (events_dir / "corrupt2.exit.json").write_text('{"bot": "c2"')  # incomplete
        (events_dir / "corrupt3.exit.json").write_text("null")  # valid JSON but not dict - may cause issues
        
        # Edge case: processed field missing (should default to False per .get())
        (events_dir / "missing_field.exit.json").write_text(json.dumps({"bot": "mf"}))
        
        pending = list_pending_events()
        
        # Should have: v1, v2, mf (3 total)
        assert len(pending) == 3
        
        returned_bots = {event_data["bot"] for _, event_data in pending}
        assert returned_bots == {"v1", "v2", "mf"}


class TestWriteAlignmentEvent:
    """Tests for write_alignment_event() - additional coverage."""
    
    def test_write_alignment_event_creates_valid_json(self, temp_state_dir):
        """Test that write_alignment_event creates valid JSON with all expected fields."""
        state_dir = temp_state_dir["state_dir"]
        logs_dir = temp_state_dir["logs_dir"]
        events_dir = state_dir / "alignment_events"
        
        # Create a dummy log file
        log_file = logs_dir / "test_bot_write.log"
        log_file.write_text("Test log content")
        
        # Write an alignment event
        write_alignment_event("test_bot_write", 0, "clean")
        
        # Verify event file was created
        event_file = events_dir / "test_bot_write.exit.json"
        assert event_file.exists()
        
        # Verify JSON is valid and has expected fields
        event = json.loads(event_file.read_text(encoding="utf-8"))
        assert event["bot"] == "test_bot_write"
        assert event["exit_code"] == 0
        assert event["exit_reason"] == "clean"
        assert event["processed"] is False
        assert event["processed_at"] is None
        assert "exit_time" in event
        assert "exit_time_human" in event
        assert "log_path" in event
        assert "stream_path" in event
        assert "checkpoint_path" in event
        assert event["version"] == 1
    
    def test_write_alignment_event_fail_open_on_error(self, temp_state_dir, caplog):
        """Test that write_alignment_event logs warning but doesn't raise on I/O error."""
        import logging
        caplog.set_level(logging.WARNING)
        
        # Mock mkdir to raise an exception
        with patch.object(Path, 'mkdir', side_effect=PermissionError("Simulated mkdir error")):
            # Should NOT raise
            write_alignment_event("error_bot_write", 1, "error")
        
        # Verify warning was logged
        assert len(caplog.records) >= 1
        warning_messages = [r.message for r in caplog.records if r.levelno == logging.WARNING]
        assert any("Failed to write alignment event" in msg for msg in warning_messages)
"}}]}