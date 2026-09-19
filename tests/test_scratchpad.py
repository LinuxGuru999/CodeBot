"""Tests for codebot/scratchpad.py — agent session persistence and handoff."""

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# Ensure project root is on sys.path for direct imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import codebot.scratchpad as sp


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestConstants:
    def test_scratchpad_version(self):
        assert sp.SCRATCHPAD_VERSION == 1

    def test_max_scratchpad_bytes(self):
        assert sp.MAX_SCRATCHPAD_BYTES == 8 * 1024


# ---------------------------------------------------------------------------
# ScratchpadState — construction and defaults
# ---------------------------------------------------------------------------

class TestScratchpadStateDefaults:
    def test_default_fields(self):
        s = sp.ScratchpadState()
        assert s.ticket_id == ""
        assert s.agent_name == ""
        assert s.phase == "init"
        assert s.completed_steps == []
        assert s.remaining_steps == []
        assert s.files_changed == []
        assert s.last_tool_call == ""
        assert s.last_tool_result_summary == ""
        assert s.error_message == ""
        assert s.context_summary == ""
        assert s.iteration == 0
        assert s.started_at == 0.0
        assert s.updated_at == 0.0
        assert s.version == sp.SCRATCHPAD_VERSION

    def test_custom_fields(self):
        s = sp.ScratchpadState(
            ticket_id="CB-123",
            agent_name="builder",
            phase="running",
            iteration=5,
            started_at=1.0,
            updated_at=2.0,
        )
        assert s.ticket_id == "CB-123"
        assert s.agent_name == "builder"
        assert s.phase == "running"
        assert s.iteration == 5

    def test_lists_are_independent_instances(self):
        """Default lists must not be shared between instances."""
        s1 = sp.ScratchpadState()
        s2 = sp.ScratchpadState()
        s1.completed_steps.append("step1")
        assert s2.completed_steps == []


# ---------------------------------------------------------------------------
# Serialization — to_dict / to_json
# ---------------------------------------------------------------------------

class TestSerialization:
    def test_to_dict_returns_all_fields(self):
        s = sp.ScratchpadState(ticket_id="CB-999", iteration=3)
        d = s.to_dict()
        assert isinstance(d, dict)
        assert d["ticket_id"] == "CB-999"
        assert d["iteration"] == 3
        assert "phase" in d
        assert "version" in d

    def test_to_json_is_valid_json(self):
        s = sp.ScratchpadState(ticket_id="CB-1")
        j = s.to_json()
        parsed = json.loads(j)
        assert parsed["ticket_id"] == "CB-1"

    def test_roundtrip_dict(self):
        original = sp.ScratchpadState(
            ticket_id="CB-200",
            agent_name="tester",
            phase="running",
            completed_steps=["step_a", "step_b"],
            remaining_steps=["step_c"],
            files_changed=["file.py"],
            iteration=7,
            started_at=100.0,
            updated_at=200.0,
        )
        d = original.to_dict()
        restored = sp.ScratchpadState.from_dict(d)
        assert restored.ticket_id == original.ticket_id
        assert restored.agent_name == original.agent_name
        assert restored.completed_steps == original.completed_steps
        assert restored.remaining_steps == original.remaining_steps
        assert restored.files_changed == original.files_changed
        assert restored.iteration == original.iteration

    def test_roundtrip_json(self):
        original = sp.ScratchpadState(
            ticket_id="CB-300",
            completed_steps=["a", "b"],
            context_summary="Long context here...",
        )
        j = original.to_json()
        restored = sp.ScratchpadState.from_json(j)
        assert restored.ticket_id == original.ticket_id
        assert restored.completed_steps == original.completed_steps
        assert restored.context_summary == original.context_summary

    def test_from_dict_ignores_unknown_keys(self):
        data = sp.ScratchpadState(ticket_id="CB-400").to_dict()
        data["unknown_field"] = "should be ignored"
        data["another_extra"] = 42
        restored = sp.ScratchpadState.from_dict(data)
        assert restored.ticket_id == "CB-400"
        assert not hasattr(restored, "unknown_field")

    def test_from_dict_handles_missing_keys(self):
        """from_dict should use defaults for any missing fields."""
        restored = sp.ScratchpadState.from_dict({})
        assert restored.ticket_id == ""
        assert restored.phase == "init"
        assert restored.completed_steps == []


# ---------------------------------------------------------------------------
# mark_step_complete
# ---------------------------------------------------------------------------

class TestMarkStepComplete:
    def test_adds_to_completed(self):
        s = sp.ScratchpadState()
        s.mark_step_complete("analyze")
        assert "analyze" in s.completed_steps

    def test_moves_from_remaining(self):
        s = sp.ScratchpadState(remaining_steps=["analyze", "implement"])
        s.mark_step_complete("analyze")
        assert "analyze" not in s.remaining_steps
        assert "analyze" in s.completed_steps

    def test_does_not_duplicate(self):
        s = sp.ScratchpadState()
        s.mark_step_complete("step1")
        s.mark_step_complete("step1")
        assert s.completed_steps.count("step1") == 1

    def test_updates_timestamp(self):
        s = sp.ScratchpadState(updated_at=0.0)
        before = time.time()
        s.mark_step_complete("step")
        after = time.time()
        assert before <= s.updated_at <= after


# ---------------------------------------------------------------------------
# mark_error
# ---------------------------------------------------------------------------

class TestMarkError:
    def test_sets_error_message(self):
        s = sp.ScratchpadState()
        s.mark_error("something broke")
        assert s.error_message == "something broke"

    def test_truncates_long_error_to_500(self):
        s = sp.ScratchpadState()
        long_msg = "x" * 1000
        s.mark_error(long_msg)
        assert len(s.error_message) == 500

    def test_sets_phase_to_error(self):
        s = sp.ScratchpadState(phase="running")
        s.mark_error("fail")
        assert s.phase == "error"

    def test_updates_timestamp(self):
        s = sp.ScratchpadState(updated_at=0.0)
        before = time.time()
        s.mark_error("fail")
        after = time.time()
        assert before <= s.updated_at <= after


# ---------------------------------------------------------------------------
# set_phase
# ---------------------------------------------------------------------------

class TestSetPhase:
    def test_sets_phase(self):
        s = sp.ScratchpadState()
        s.set_phase("running")
        assert s.phase == "running"

    def test_updates_timestamp(self):
        s = sp.ScratchpadState(updated_at=0.0)
        before = time.time()
        s.set_phase("done")
        after = time.time()
        assert before <= s.updated_at <= after


# ---------------------------------------------------------------------------
# truncate_for_size
# ---------------------------------------------------------------------------

class TestTruncateForSize:
    def test_small_scratchpad_unchanged(self):
        s = sp.ScratchpadState(
            ticket_id="CB-1",
            completed_steps=["step1"],
        )
        original_json = s.to_json()
        s.truncate_for_size()
        assert s.to_json() == original_json

    def test_large_context_summary_is_halved(self):
        s = sp.ScratchpadState(
            context_summary="x" * 10000,
        )
        s.truncate_for_size()
        # context_summary should have been halved at least once
        assert len(s.context_summary) < 10000

    def test_large_completed_steps_trimmed_to_three(self):
        # Use a large context_summary to push serialized size over 8KB.
        # The truncation loop first halves context_summary repeatedly (until ≤100),
        # then trims completed_steps to last 3.
        s = sp.ScratchpadState(
            completed_steps=[f"completed_step_{i}_with_long_description_xxxx" for i in range(50)],
            context_summary="x" * 30000,
        )
        s.truncate_for_size()
        assert len(s.completed_steps) <= 3

    def test_large_remaining_steps_trimmed_to_three(self):
        # No completed_steps, so after context_summary is small enough,
        # the truncation falls through to the remaining_steps branch.
        s = sp.ScratchpadState(
            remaining_steps=[f"remaining_step_{i}_with_long_description_xxxx" for i in range(50)],
            context_summary="x" * 30000,
        )
        s.truncate_for_size()
        assert len(s.remaining_steps) <= 3

    def test_result_stays_under_max_bytes(self):
        """After truncation, serialized size must be <= MAX_SCRATCHPAD_BYTES."""
        s = sp.ScratchpadState(
            context_summary="x" * 50000,
            completed_steps=[f"step{i}" for i in range(50)],
            remaining_steps=[f"step{i}" for i in range(50)],
        )
        s.truncate_for_size()
        size = len(s.to_json().encode("utf-8"))
        assert size <= sp.MAX_SCRATCHPAD_BYTES


# ---------------------------------------------------------------------------
# load_scratchpad
# ---------------------------------------------------------------------------

class TestLoadScratchpad:
    def test_loads_existing_valid_file(self, tmp_path):
        state = sp.ScratchpadState(
            agent_name="builder",
            ticket_id="CB-500",
            phase="running",
        )
        sp.save_scratchpad(tmp_path, state)

        loaded = sp.load_scratchpad(tmp_path, "builder")
        assert loaded.ticket_id == "CB-500"
        assert loaded.phase == "running"
        assert loaded.agent_name == "builder"

    def test_returns_fresh_state_when_no_file(self, tmp_path):
        loaded = sp.load_scratchpad(tmp_path, "nonexistent")
        assert loaded.agent_name == "nonexistent"
        assert loaded.phase == "init"
        assert loaded.version == sp.SCRATCHPAD_VERSION

    def test_returns_fresh_state_on_corrupt_json(self, tmp_path):
        corrupt_path = tmp_path / "corrupt.scratchpad.json"
        corrupt_path.write_text("{bad json!!!", encoding="utf-8")

        loaded = sp.load_scratchpad(tmp_path, "corrupt")
        assert loaded.agent_name == "corrupt"
        assert loaded.phase == "init"

    def test_returns_fresh_state_on_wrong_version(self, tmp_path):
        state = sp.ScratchpadState(agent_name="oldver", ticket_id="CB-X")
        d = state.to_dict()
        d["version"] = 999  # wrong version
        path = tmp_path / "oldver.scratchpad.json"
        path.write_text(json.dumps(d), encoding="utf-8")

        loaded = sp.load_scratchpad(tmp_path, "oldver")
        assert loaded.agent_name == "oldver"
        assert loaded.phase == "init"
        assert loaded.version == sp.SCRATCHPAD_VERSION

    def test_returns_fresh_state_on_missing_fields(self, tmp_path):
        """A scratchpad saved by an older schema should still load gracefully."""
        path = tmp_path / "partial.scratchpad.json"
        path.write_text(json.dumps({"ticket_id": "CB-Y", "version": 1}), encoding="utf-8")

        loaded = sp.load_scratchpad(tmp_path, "partial")
        assert loaded.ticket_id == "CB-Y"
        assert loaded.phase == "init"  # default


# ---------------------------------------------------------------------------
# save_scratchpad — atomic write
# ---------------------------------------------------------------------------

class TestSaveScratchpad:
    def test_creates_file(self, tmp_path):
        state = sp.ScratchpadState(agent_name="writer")
        sp.save_scratchpad(tmp_path, state)

        expected = tmp_path / "writer.scratchpad.json"
        assert expected.exists()

    def test_content_is_valid_json(self, tmp_path):
        state = sp.ScratchpadState(agent_name="json_check", ticket_id="CB-1")
        sp.save_scratchpad(tmp_path, state)

        content = (tmp_path / "json_check.scratchpad.json").read_text(encoding="utf-8")
        parsed = json.loads(content)
        assert parsed["ticket_id"] == "CB-1"

    def test_creates_parent_directory(self, tmp_path):
        deep_dir = tmp_path / "sub" / "dir"
        state = sp.ScratchpadState(agent_name="deep")
        sp.save_scratchpad(deep_dir, state)

        assert (deep_dir / "deep.scratchpad.json").exists()

    def test_no_tmp_file_left_after_save(self, tmp_path):
        state = sp.ScratchpadState(agent_name="clean")
        sp.save_scratchpad(tmp_path, state)

        tmp_files = list(tmp_path.glob("*.tmp"))
        assert tmp_files == []

    def test_updates_timestamp_on_save(self, tmp_path):
        state = sp.ScratchpadState(agent_name="ts", updated_at=0.0)
        before = time.time()
        sp.save_scratchpad(tmp_path, state)
        after = time.time()

        loaded = sp.load_scratchpad(tmp_path, "ts")
        assert before <= loaded.updated_at <= after

    def test_size_limit_enforced_on_save(self, tmp_path):
        """A scratchpad that exceeds MAX should be truncated during save."""
        state = sp.ScratchpadState(
            agent_name="big",
            context_summary="y" * 40000,
            completed_steps=[f"step{i}" for i in range(50)],
        )
        sp.save_scratchpad(tmp_path, state)

        path = tmp_path / "big.scratchpad.json"
        size = len(path.read_bytes())
        assert size <= sp.MAX_SCRATCHPAD_BYTES


# ---------------------------------------------------------------------------
# clear_scratchpad
# ---------------------------------------------------------------------------

class TestClearScratchpad:
    def test_removes_file(self, tmp_path):
        state = sp.ScratchpadState(agent_name="clearme")
        sp.save_scratchpad(tmp_path, state)
        assert (tmp_path / "clearme.scratchpad.json").exists()

        sp.clear_scratchpad(tmp_path, "clearme")
        assert not (tmp_path / "clearme.scratchpad.json").exists()

    def test_no_error_when_file_does_not_exist(self, tmp_path):
        # Should not raise
        sp.clear_scratchpad(tmp_path, "nonexistent")


# ---------------------------------------------------------------------------
# create_handoff_note
# ---------------------------------------------------------------------------

class TestCreateHandoffNote:
    def test_contains_all_fields(self):
        state = sp.ScratchpadState(
            ticket_id="CB-600",
            agent_name="builder",
            phase="running",
            iteration=3,
            completed_steps=["analyze", "implement"],
            remaining_steps=["test"],
            files_changed=["file1.py", "file2.py"],
            error_message="oops",
            context_summary="Some context",
        )
        note = sp.create_handoff_note(state)
        assert "=== HANDOFF NOTE ===" in note
        assert "CB-600" in note
        assert "builder" in note
        assert "running" in note
        assert "3" in note
        assert "analyze" in note
        assert "implement" in note
        assert "test" in note
        assert "file1.py" in note
        assert "file2.py" in note
        assert "oops" in note
        assert "Some context" in note
        assert "====================" in note

    def test_handles_empty_state(self):
        state = sp.ScratchpadState()
        note = sp.create_handoff_note(state)
        assert "=== HANDOFF NOTE ===" in note
        assert "====================" in note

    def test_completed_steps_limited_to_5(self):
        state = sp.ScratchpadState(
            completed_steps=[f"step{i}" for i in range(20)],
        )
        note = sp.create_handoff_note(state)
        # Only last 5 should appear
        assert "step0" not in note
        assert "step19" in note

    def test_remaining_steps_limited_to_5(self):
        state = sp.ScratchpadState(
            remaining_steps=[f"step{i}" for i in range(20)],
        )
        note = sp.create_handoff_note(state)
        # Only first 5 should appear
        assert "step0" in note
        assert "step19" not in note

    def test_context_summary_truncated_to_500(self):
        state = sp.ScratchpadState(
            context_summary="z" * 1000,
        )
        note = sp.create_handoff_note(state)
        # The context line should not contain the full 1000 chars
        assert "z" * 1000 not in note


# ---------------------------------------------------------------------------
# Round-trip: save → load preserves data
# ---------------------------------------------------------------------------

class TestSaveLoadRoundTrip:
    def test_full_roundtrip(self, tmp_path):
        original = sp.ScratchpadState(
            ticket_id="CB-700",
            agent_name="roundtrip",
            phase="working",
            completed_steps=["a", "b", "c"],
            remaining_steps=["d"],
            files_changed=["main.py"],
            last_tool_call="read",
            last_tool_result_summary="ok",
            context_summary="session context",
            iteration=12,
            started_at=1000.0,
        )
        sp.save_scratchpad(tmp_path, original)
        loaded = sp.load_scratchpad(tmp_path, "roundtrip")

        assert loaded.ticket_id == "CB-700"
        assert loaded.agent_name == "roundtrip"
        assert loaded.phase == "working"
        assert loaded.completed_steps == ["a", "b", "c"]
        assert loaded.remaining_steps == ["d"]
        assert loaded.files_changed == ["main.py"]
        assert loaded.last_tool_call == "read"
        assert loaded.last_tool_result_summary == "ok"
        assert loaded.context_summary == "session context"
        assert loaded.iteration == 12

    def test_overwrite_preserves_latest(self, tmp_path):
        s1 = sp.ScratchpadState(agent_name="overwrite", ticket_id="CB-v1")
        sp.save_scratchpad(tmp_path, s1)

        s2 = sp.ScratchpadState(agent_name="overwrite", ticket_id="CB-v2")
        sp.save_scratchpad(tmp_path, s2)

        loaded = sp.load_scratchpad(tmp_path, "overwrite")
        assert loaded.ticket_id == "CB-v2"
