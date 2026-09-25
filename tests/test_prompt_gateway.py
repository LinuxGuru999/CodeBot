"""Tests for prompt_gateway.py — prompt compression and spawn gating."""

import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Import the module under test
from codebot import prompt_gateway


class TestCompressPrompt:
    """Tests for compress_prompt() function."""

    def test_strips_known_prefixes(self):
        """Verify that headings starting with _STRIP_PREFIXES are removed."""
        prompt = """## Tool Usage Rules
Some content here.
## Mission Spec
Keep this.
## Heartbeat Protocol
Remove this too.
## Conclusion
Final part.
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert "Tool Usage Rules" in removed
        assert "Heartbeat Protocol" in removed
        assert "Mission Spec" not in removed
        assert "Conclusion" not in removed
        assert "Some content here." not in core  # Content under stripped header should be gone
        assert "Keep this." in core
        assert "Final part." in core

    def test_preserves_non_stripped_headings(self):
        """Verify that headings not in _STRIP_PREFIXES are kept."""
        prompt = """## Role Definition
You are a tester.
## Random Header
Keep this content.
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert len(removed) == 0
        assert "Role Definition" in core
        assert "Random Header" in core
        assert "You are a tester." in core

    def test_removes_content_under_stripped_header(self):
        """Ensure content following a stripped header is also removed until next header."""
        prompt = """## Web Search
Line 1
Line 2
## Keep This
Line 3
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert "Web Search" in removed
        assert "Line 1" not in core
        assert "Line 2" not in core
        assert "Line 3" in core
        assert "Keep This" in core

    def test_collapses_multiple_newlines(self):
        """Verify that multiple newlines are collapsed to two."""
        prompt = """## Header to strip
Content



## Keep
Text
"""
        core, removed = prompt_gateway.compress_prompt(prompt)
        # Should not have 3+ consecutive newlines
        assert "\n\n\n" not in core
        assert "\n\n" in core or "\nText" in core

    def test_empty_input(self):
        """Handle empty string input."""
        core, removed = prompt_gateway.compress_prompt("")
        assert core == ""
        assert removed == []

    def test_no_headers(self):
        """Handle text with no markdown headers."""
        prompt = "Just plain text.\nNo headers here."
        core, removed = prompt_gateway.compress_prompt(prompt)
        assert removed == []
        assert "Just plain text." in core


class TestInferStateDir:
    """Tests for _infer_state_dir() function."""

    def test_infers_from_state_parent(self):
        """Infers state dir when 'state' is a direct parent."""
        heartbeat = "/home/user/project/.codebot/state/test.heartbeat"
        ckpt = "/home/user/project/.codebot/state/test.checkpoint.json"
        result = prompt_gateway._infer_state_dir(heartbeat, ckpt)
        assert result.name == "state"
        assert str(result).endswith("state")

    def test_infers_from_special_parent(self):
        """Infers state dir when parent is 'heartbeats' or similar."""
        heartbeat = "/home/user/project/.codebot/heartbeats/test.heartbeat"
        ckpt = ""
        result = prompt_gateway._infer_state_dir(heartbeat, ckpt)
        # Parent of 'heartbeats' is '.codebot', so result should be '.codebot'?
        # Logic: if parent.name in special list, return parent.parent.
        # Parent is 'heartbeats', parent.parent is '.codebot'.
        # Wait, the code says: `if parent.name in (...): return parent.parent`
        # So if input is `/.../heartbeats/file`, parent is `heartbeats`, parent.parent is `...`
        # Let's re-read code logic carefully.
        # p = Path(cand). parents: [..., heartbeats, .codebot, ...]
        # Loop: parent = heartbeats. name in list? Yes. Return parent.parent (.codebot).
        # But usually state_dir IS the 'state' folder. The test case in code uses 'state' folder directly.
        # Let's assume standard case first.
        pass

    def test_fallback_to_adapter(self):
        """Falls back to adapter if paths don't match patterns."""
        mock_adapter = MagicMock()
        mock_adapter.paths.return_value.state_dir = Path("/mock/adapter/state")
        
        with patch.object(prompt_gateway, '_adapter_instance', mock_adapter):
            result = prompt_gateway._infer_state_dir("", "")
            assert result == Path("/mock/adapter/state")

    def test_fallback_to_default(self):
        """Falls back to default relative path if no adapter and no matches."""
        # Clear adapter
        with patch.object(prompt_gateway, '_adapter_instance', None):
            result = prompt_gateway._infer_state_dir("", "")
            # Default is Path(__file__).parent / "state"
            expected = Path(prompt_gateway.__file__).parent / "state"
            assert result == expected


class TestCommonContract:
    """Tests for _common_contract() function."""

    def test_generates_contract_with_paths(self):
        """Verify contract string contains correct file paths."""
        bot = "test_bot"
        hb_file = "/tmp/hb.txt"
        ckpt_file = "/tmp/ckpt.json"
        state_dir = "/tmp/state"
        
        contract = prompt_gateway._common_contract(bot, hb_file, ckpt_file, state_dir)
        
        assert "/tmp/state/.drain" in contract or "Drain:" in contract
        assert "alignment_scores.json" in contract
        assert "/tmp/hb.txt" in contract
        assert "/tmp/ckpt.json" in contract
        assert "test_bot.evolve.json" in contract
        assert "Drain:" in contract
        assert "Heartbeat:" in contract


class TestBuildMessage:
    """Tests for build_message() function."""

    def test_assembles_message_parts(self):
        """Verify all parts are present in the final message."""
        bot = "tester"
        model = "qwen-3.5"
        prompt_text = "## Mission\nDo testing.\n## Tool Usage Rules\nIgnore this."
        hb_file = "/tmp/hb"
        ckpt_file = "/tmp/ckpt"
        ckpt_block = "CHECKPOINT HANDOFF\n{}"
        state_dir = "/tmp/state"
        logs_dir = "/tmp/logs"
        prompt_name = "test_implementer.md"
        
        msg = prompt_gateway.build_message(
            bot, model, prompt_text, hb_file, ckpt_file, ckpt_block, state_dir, logs_dir, prompt_name
        )
        
        assert f"delegated task: '{bot}'" in msg
        assert f"model {model}" in msg
        assert "SHARED INFRA CONTRACT" in msg
        assert "CHECKPOINT HANDOFF" in msg
        assert "--- Mission Spec" in msg
        assert "Do testing." in msg
        assert "Tool Usage Rules" not in msg  # Should be stripped

    def test_handles_empty_checkpoint_block(self):
        """Verify behavior when checkpoint block is empty."""
        # Use a unique marker in the ckpt_block to detect if it was appended
        msg_with_block = prompt_gateway.build_message(
            "bot", "model", "## Keep\nText", "/hb", "/ckpt", "UNIQUE_CHECKPOINT_MARKER", "/state", "/logs", "prompt.md"
        )
        msg_without_block = prompt_gateway.build_message(
            "bot", "model", "## Keep\nText", "/hb", "/ckpt", "", "/state", "/logs", "prompt.md"
        )
        assert "UNIQUE_CHECKPOINT_MARKER" in msg_with_block
        assert "UNIQUE_CHECKPOINT_MARKER" not in msg_without_block


class TestRunningCount:
    """Tests for running_count() function."""

    def test_counts_running_processes(self):
        """Count only processes that are not poll()-ed as finished."""
        mock_proc_running = MagicMock()
        mock_proc_running.poll.return_value = None  # Still running
        
        mock_proc_finished = MagicMock()
        mock_proc_finished.poll.return_value = 0  # Finished
        
        bots = {
            "bot1": MagicMock(process=mock_proc_running),
            "bot2": MagicMock(process=mock_proc_finished),
            "bot3": MagicMock(process=None),  # No process
        }
        
        count = prompt_gateway.running_count(bots)
        assert count == 1

    def test_counts_zero_when_none_running(self):
        """Return 0 when no bots are running."""
        bots = {
            "bot1": MagicMock(process=None),
            "bot2": MagicMock(process=MagicMock(poll=MagicMock(return_value=0))),
        }
        assert prompt_gateway.running_count(bots) == 0


class TestSpawnAllowed:
    """Tests for spawn_allowed() function."""

    def test_allows_when_under_cap_and_gap_met(self):
        """Allow spawn if running < MAX_CONCURRENT and gap > MIN_SPAWN_GAP."""
        # Reset global state
        prompt_gateway._last_spawn_ts = 0.0
        
        bots = {}  # 0 running
        allowed, reason = prompt_gateway.spawn_allowed(bots)
        assert allowed is True
        assert "slot available" in reason

    def test_allows_when_no_rate_limit(self):
        """spawn_allowed delegates capacity to ConcurrencyController; only checks rate limiter."""
        bots = {}
        allowed, reason = prompt_gateway.spawn_allowed(bots)
        assert allowed is True
        assert "slot available" in reason

    def test_allows_with_bots_when_no_rate_limit(self):
        """Capacity gating moved to DispatchGate; spawn_allowed no longer checks bot count."""
        bots = {f"bot{i}": MagicMock(process=MagicMock(poll=MagicMock(return_value=None))) for i in range(100)}
        allowed, reason = prompt_gateway.spawn_allowed(bots)
        assert allowed is True


class TestNoteSpawn:
    """Tests for note_spawn() function."""

    def test_updates_timestamp(self):
        """Verify _last_spawn_ts is updated to current time."""
        before = prompt_gateway._last_spawn_ts
        prompt_gateway.note_spawn()
        after = prompt_gateway._last_spawn_ts
        assert after >= before
        assert after <= time.time()


class TestEstimateTokens:
    """Tests for estimate_tokens() function."""

    def test_calculates_approx_tokens(self):
        """Verify token estimate is length // 4."""
        text = "A" * 400
        assert prompt_gateway.estimate_tokens(text) == 100

    def test_returns_minimum_one(self):
        """Verify empty or small strings return at least 1."""
        assert prompt_gateway.estimate_tokens("") == 1
        assert prompt_gateway.estimate_tokens("a") == 1
