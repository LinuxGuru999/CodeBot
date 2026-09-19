#!/usr/bin/env python3
"""Tests for alignment_events.list_pending_events() filtering logic."""

import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot.alignment_events import list_pending_events, set_dirs


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


def test_list_pending_returns_only_unprocessed(temp_state_dir):
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


def test_list_pending_skips_corrupt_json(temp_state_dir):
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


def test_list_pending_empty_directory(temp_state_dir):
    """Test that empty directory returns empty list."""
    events_dir = temp_state_dir["events_dir"]
    
    # Ensure directory is empty (it should be from fixture)
    assert len(list(events_dir.glob("*.exit.json"))) == 0
    
    pending = list_pending_events()
    
    assert pending == []
    assert isinstance(pending, list)


def test_list_pending_mixed_scenarios(temp_state_dir):
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
"}}]}