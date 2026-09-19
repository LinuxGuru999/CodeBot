"""Unit tests for codebot.prompt_gateway module.

Covers compress_prompt stripping behavior, _infer_state_dir path resolution,
_common_contract generation, build_message assembly, spawn gating logic,
and adapter management.
"""

from __future__ import annotations

import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebot import prompt_gateway


class TestCompressPrompt:
    """Tests for compress_prompt() function."""

    def test_strips_known_prefix(self) -> None:
        """Headings matching _STRIP_PREFIXES are removed along with their content."""
        prompt = """## Mission
Do important work.

## Heartbeat Protocol
Write timestamps every 60s.
More details here.

## Final Section
Keep this.
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert "Heartbeat Protocol" in removed
        assert "Heartbeat Protocol" not in core
        assert "Write timestamps" not in core
        assert "Mission" in core
        assert "Final Section" in core
        assert "Keep this" in core

    def test_preserves_non_stripped_headings(self) -> None:
        """Headings not in _STRIP_PREFIXES are preserved."""
        prompt = """## Important Mission
This must stay.

## Another Section
Also stays.
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert "Important Mission" in core
        assert "Another Section" in core
        assert removed == []

    def test_empty_input(self) -> None:
        core, removed = prompt_gateway.compress_prompt("")
        assert core == ""
        assert removed == []

    def test_no_headings(self) -> None:
        prompt = "Just plain text without any headings."
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert core == "Just plain text without any headings."
        assert removed == []

    def test_multiple_stripped_sections(self) -> None:
        prompt = """## Web Search
Search the web.

## Tool Usage Rules
Use tools carefully.

## Actual Work
Do the thing.
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert len(removed) == 2
        assert "Web Search" in removed
        assert "Tool Usage Rules" in removed
        assert "Actual Work" in core
        assert "Do the thing" in core

    def test_collapses_excessive_newlines(self) -> None:
        """Three or more consecutive newlines are collapsed to two."""
        prompt = "Line one.\n\n\n\n\nLine two."
        core, removed = prompt_gateway.compress_prompt(prompt)
        # Should have at most two newlines between lines
        assert "\n\n\n" not in core

    def test_strips_leading_trailing_whitespace(self) -> None:
        prompt = "\n\n  Content with whitespace.  \n\n"
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert core == "Content with whitespace."

    def test_partial_match_not_stripped(self) -> None:
        """Headings that start with but don't exactly match prefix patterns are kept."""
        # The strip logic uses startswith, so 'Heartbeat' would match 'Heartbeat Protocol'
        # But a heading like 'My Heartbeat Notes' should also be stripped if it starts with a prefix
        prompt = """## Heartbeat Extra
Some extra notes about heartbeats.

## Real Work
Actual work here.
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        # 'Heartbeat Extra' starts with 'Heartbeat Protocol'? No.
        # Let's check what prefixes exist
        assert "Heartbeat Extra" not in removed  # Doesn't match any prefix exactly via startswith
        # Actually, looking at the code: title.startswith(_STRIP_PREFIXES)
        # This checks if title starts with ANY of the prefixes
        # 'Heartbeat Extra' does NOT start with 'Heartbeat Protocol'
        # So it should be kept
        assert "Heartbeat Extra" in core

    def test_exact_prefix_match_stripped(self) -> None:
        prompt = """## Heartbeat Protocol
This gets stripped.

## Other Stuff
This stays.
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert "Heartbeat Protocol" in removed
        assert "Other Stuff" in core

    def test_content_after_stripped_heading_removed(self) -> None:
        """All lines under a stripped heading are removed until next heading."""
        prompt = """## Web Search
Line 1 under web search.
Line 2 under web search.
Line 3 under web search.

## Keep This
Important content.
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert "Web Search" in removed
        assert "Line 1 under web search" not in core
        assert "Line 2 under web search" not in core
        assert "Line 3 under web search" not in core
        assert "Keep This" in core
        assert "Important content" in core


class TestInferStateDir:
    """Tests for _infer_state_dir() function."""

    def test_from_heartbeat_path(self, tmp_path: Path) -> None:
        """Resolves state dir from heartbeat file path."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        hb_file = str(state_dir / "test_bot.heartbeat")
        result = prompt_gateway._infer_state_dir(hb_file, "")
        assert result == state_dir

    def test_from_checkpoint_path(self, tmp_path: Path) -> None:
        """Resolves state dir from checkpoint file path."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        ckpt_file = str(state_dir / "test_bot.checkpoint.json")
        result = prompt_gateway._infer_state_dir("", ckpt_file)
        assert result == state_dir

    def test_heartbeat_preferred_over_checkpoint(self, tmp_path: Path) -> None:
        """Heartbeat path is checked first."""
        state_dir1 = tmp_path / "state1"
        state_dir2 = tmp_path / "state2"
        state_dir1.mkdir()
        state_dir2.mkdir()
        hb_file = str(state_dir1 / "bot.heartbeat")
        ckpt_file = str(state_dir2 / "bot.checkpoint.json")
        result = prompt_gateway._infer_state_dir(hb_file, ckpt_file)
        assert result == state_dir1

    def test_fallback_to_special_parent_dirs(self, tmp_path: Path) -> None:
        """Falls back to parent of special directories like heartbeats, checkpoints."""
        # Create path like /base/heartbeats/bot.heartbeat
        heartbeats_dir = tmp_path / "heartbeats"
        heartbeats_dir.mkdir()
        hb_file = str(heartbeats_dir / "bot.heartbeat")
        result = prompt_gateway._infer_state_dir(hb_file, "")
        assert result == tmp_path

    def test_fallback_for_checkpoints_dir(self, tmp_path: Path) -> None:
        checkpoints_dir = tmp_path / "checkpoints"
        checkpoints_dir.mkdir()
        ckpt_file = str(checkpoints_dir / "bot.checkpoint.json")
        result = prompt_gateway._infer_state_dir("", ckpt_file)
        assert result == tmp_path

    def test_fallback_for_alignment_events_dir(self, tmp_path: Path) -> None:
        align_dir = tmp_path / "alignment_events"
        align_dir.mkdir()
        hb_file = str(align_dir / "event.json")
        result = prompt_gateway._infer_state_dir(hb_file, "")
        assert result == tmp_path

    def test_fallback_for_claims_dir(self, tmp_path: Path) -> None:
        claims_dir = tmp_path / "claims"
        claims_dir.mkdir()
        hb_file = str(claims_dir / "claim.json")
        result = prompt_gateway._infer_state_dir(hb_file, "")
        assert result == tmp_path

    def test_default_fallback_when_no_special_parent(self, tmp_path: Path) -> None:
        """When no special parent found, returns immediate parent."""
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        hb_file = str(subdir / "bot.heartbeat")
        result = prompt_gateway._infer_state_dir(hb_file, "")
        assert result == subdir

    def test_adapter_fallback(self, tmp_path: Path) -> None:
        """Falls back to adapter when no path context available."""
        mock_adapter = MagicMock()
        mock_adapter.paths.return_value.state_dir = tmp_path / "adapter_state"
        prompt_gateway.set_project_adapter(mock_adapter)
        try:
            result = prompt_gateway._infer_state_dir("", "")
            assert result == tmp_path / "adapter_state"
        finally:
            prompt_gateway.set_project_adapter(None)

    def test_adapter_exception_fallback(self) -> None:
        """When adapter raises exception, falls back to default."""
        mock_adapter = MagicMock()
        mock_adapter.paths.side_effect = RuntimeError("boom")
        prompt_gateway.set_project_adapter(mock_adapter)
        try:
            result = prompt_gateway._infer_state_dir("", "")
            # Should fall back to module's parent / state
            assert result.name == "state"
        finally:
            prompt_gateway.set_project_adapter(None)

    def test_both_paths_empty(self) -> None:
        """When both paths are empty, uses adapter or default."""
        prompt_gateway.set_project_adapter(None)
        result = prompt_gateway._infer_state_dir("", "")
        assert result.name == "state"


class TestCommonContract:
    """Tests for _common_contract() function."""

    def test_contains_drain_instruction(self) -> None:
        contract = prompt_gateway._common_contract(
            "test_bot",
            "/state/test_bot.heartbeat",
            "/state/test_bot.checkpoint.json",
            "/state",
        )
        assert ".drain" in contract
        assert ".update_lock" in contract

    def test_contains_heartbeat_instruction(self) -> None:
        contract = prompt_gateway._common_contract(
            "test_bot",
            "/state/test_bot.heartbeat",
            "/state/test_bot.checkpoint.json",
            "/state",
        )
        assert "Heartbeat" in contract
        assert "120s" in contract

    def test_contains_checkpoint_instruction(self) -> None:
        contract = prompt_gateway._common_contract(
            "test_bot",
            "/state/test_bot.heartbeat",
            "/state/test_bot.checkpoint.json",
            "/state",
        )
        assert "Checkpoint" in contract
        assert "<4KB JSON" in contract

    def test_contains_alignment_instruction(self) -> None:
        contract = prompt_gateway._common_contract(
            "test_bot",
            "/state/test_bot.heartbeat",
            "/state/test_bot.checkpoint.json",
            "/state",
        )
        assert "Alignment" in contract
        assert "alignment_scores.json" in contract

    def test_contains_research_instruction(self) -> None:
        contract = prompt_gateway._common_contract(
            "test_bot",
            "/state/test_bot.heartbeat",
            "/state/test_bot.checkpoint.json",
            "/state",
        )
        assert "web_search" in contract
        assert "web_fetch" in contract

    def test_uses_provided_state_dir(self) -> None:
        contract = prompt_gateway._common_contract(
            "test_bot",
            "/custom/state/test_bot.heartbeat",
            "/custom/states/test_bot.checkpoint.json",
            "/custom/state",
        )
        assert "/custom/state/.drain" in contract


class TestBuildMessage:
    """Tests for build_message() function."""

    def test_basic_assembly(self) -> None:
        msg = prompt_gateway.build_message(
            bot="test_bot",
            model="gpt-4",
            prompt_text="## Mission\nDo work.",
            heartbeat_file="/state/test_bot.heartbeat",
            ckpt_file="/state/test_bot.checkpoint.json",
            ckpt_block="",
            state_dir="/state",
            logs_dir="/logs",
            prompt_name="test_bot.md",
        )
        assert "Sisyphus" in msg
        assert "test_bot" in msg
        assert "gpt-4" in msg
        assert "SHARED INFRA CONTRACT" in msg
        assert "Mission" in msg
        assert "Do work" in msg

    def test_includes_ckpt_block(self) -> None:
        ckpt_block = "CHECKPOINT HANDOFF: resume from step 3"
        msg = prompt_gateway.build_message(
            bot="test_bot",
            model="gpt-4",
            prompt_text="## Mission\nDo work.",
            heartbeat_file="/state/test_bot.heartbeat",
            ckpt_file="/state/test_bot.checkpoint.json",
            ckpt_block=ckpt_block,
            state_dir="/state",
            logs_dir="/logs",
            prompt_name="test_bot.md",
        )
        assert ckpt_block in msg

    def test_compresses_prompt(self) -> None:
        prompt_with_boilerplate = """## Mission
Do important work.

## Heartbeat Protocol
Write timestamps every 60s.
This is boilerplate.

## Final Note
Keep this.
"""
        msg = prompt_gateway.build_message(
            bot="test_bot",
            model="gpt-4",
            prompt_text=prompt_with_boilerplate,
            heartbeat_file="/state/test_bot.heartbeat",
            ckpt_file="/state/test_bot.checkpoint.json",
            ckpt_block="",
            state_dir="/state",
            logs_dir="/logs",
            prompt_name="test_bot.md",
        )
        assert "Heartbeat Protocol" not in msg
        assert "Write timestamps" not in msg
        assert "Final Note" in msg

    def test_logs_token_reduction(self, caplog: pytest.LogCaptureFixture) -> None:
        import logging
        caplog.set_level(logging.INFO, logger="prompt_gateway")
        prompt_gateway.build_message(
            bot="test_bot",
            model="gpt-4",
            prompt_text="## Mission\nShort prompt.",
            heartbeat_file="/state/test_bot.heartbeat",
            ckpt_file="/state/test_bot.checkpoint.json",
            ckpt_block="",
            state_dir="/state",
            logs_dir="/logs",
            prompt_name="test_bot.md",
        )
        assert any("stripped" in record.message for record in caplog.records)


class TestRunningCount:
    """Tests for running_count() function."""

    def test_empty_bots(self) -> None:
        assert prompt_gateway.running_count({}) == 0

    def test_all_running(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None  # Still running
        bots = {
            "bot1": MagicMock(process=mock_proc),
            "bot2": MagicMock(process=mock_proc),
        }
        assert prompt_gateway.running_count(bots) == 2

    def test_all_finished(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = 0  # Finished
        bots = {
            "bot1": MagicMock(process=mock_proc),
            "bot2": MagicMock(process=mock_proc),
        }
        assert prompt_gateway.running_count(bots) == 0

    def test_mixed_states(self) -> None:
        running_proc = MagicMock()
        running_proc.poll.return_value = None
        finished_proc = MagicMock()
        finished_proc.poll.return_value = 1
        bots = {
            "bot1": MagicMock(process=running_proc),
            "bot2": MagicMock(process=finished_proc),
            "bot3": MagicMock(process=None),  # No process
        }
        assert prompt_gateway.running_count(bots) == 1

    def test_no_process_attribute(self) -> None:
        bots = {
            "bot1": MagicMock(),  # No process attribute
        }
        assert prompt_gateway.running_count(bots) == 0


class TestSpawnAllowed:
    """Tests for spawn_allowed() function."""

    def test_under_capacity_and_gap_met(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        bots = {f"bot{i}": MagicMock(process=mock_proc) for i in range(10)}
        # Reset last spawn to ensure gap is met
        prompt_gateway._last_spawn_ts = time.time() - 100
        allowed, reason = prompt_gateway.spawn_allowed(bots)
        assert allowed is True
        assert "slot available" in reason

    def test_at_capacity(self) -> None:
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        # Create enough bots to hit the cap
        num_bots = int(prompt_gateway.MAX_CONCURRENT)
        bots = {f"bot{i}": MagicMock(process=mock_proc) for i in range(num_bots)}
        prompt_gateway._last_spawn_ts = time.time() - 100
        allowed, reason = prompt_gateway.spawn_allowed(bots)
        assert allowed is False
        assert "cap" in reason.lower() or str(num_bots) in reason

    def test_gap_not_met(self) -> None:
        bots = {}
        # Set last spawn to now so gap is 0
        prompt_gateway._last_spawn_ts = time.time()
        allowed, reason = prompt_gateway.spawn_allowed(bots)
        assert allowed is False
        assert "gap" in reason.lower()

    def test_gap_met_after_wait(self) -> None:
        bots = {}
        prompt_gateway._last_spawn_ts = time.time() - 100
        allowed, reason = prompt_gateway.spawn_allowed(bots)
        assert allowed is True


class TestNoteSpawn:
    """Tests for note_spawn() function."""

    def test_updates_timestamp(self) -> None:
        old_ts = prompt_gateway._last_spawn_ts
        time.sleep(0.01)  # Small delay to ensure different timestamp
        prompt_gateway.note_spawn()
        assert prompt_gateway._last_spawn_ts > old_ts


class TestEstimateTokens:
    """Tests for estimate_tokens() function."""

    def test_basic_estimate(self) -> None:
        # Rough estimate: 4 chars per token
        text = "a" * 400
        tokens = prompt_gateway.estimate_tokens(text)
        assert tokens == 100

    def test_empty_string_returns_one(self) -> None:
        assert prompt_gateway.estimate_tokens("") == 1

    def test_short_string_returns_one(self) -> None:
        assert prompt_gateway.estimate_tokens("hi") == 1


class TestAdapterManagement:
    """Tests for set_project_adapter() and get_adapter()."""

    def test_set_and_get_adapter(self) -> None:
        mock_adapter = MagicMock()
        prompt_gateway.set_project_adapter(mock_adapter)
        assert prompt_gateway.get_adapter() is mock_adapter
        # Clean up
        prompt_gateway.set_project_adapter(None)

    def test_get_adapter_returns_none_initially(self) -> None:
        prompt_gateway.set_project_adapter(None)
        assert prompt_gateway.get_adapter() is None
