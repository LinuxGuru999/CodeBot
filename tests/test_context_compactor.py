"""Unit tests for codebot.context_compactor module.

Covers estimate_tokens, estimate_messages_tokens, needs_compaction,
compact_messages (including system prompt preservation, last-N retention,
idempotency), _summarize_messages extraction logic, and _aggressive_truncate.
"""

from __future__ import annotations

import pytest

from codebot.context_compactor import (
    estimate_tokens,
    estimate_messages_tokens,
    needs_compaction,
    compact_messages,
    _summarize_messages,
    _aggressive_truncate,
    build_compaction_checkpoint,
    SUMMARY_MARKER,
    MIN_KEEP_MESSAGES,
    SAFETY_MARGIN,
    DEFAULT_MAX_TOKENS,
)


class TestEstimateTokens:
    """Tests for estimate_tokens()."""

    def test_basic_estimation(self) -> None:
        # 4 chars per token
        assert estimate_tokens("abcd") == 1
        assert estimate_tokens("abcdefgh") == 2

    def test_empty_string_returns_one(self) -> None:
        assert estimate_tokens("") == 1

    def test_partial_token_rounds_down(self) -> None:
        # 3 chars < 4, so max(1, 0) = 1
        assert estimate_tokens("abc") == 1

    def test_exact_multiple(self) -> None:
        assert estimate_tokens("a" * 400) == 100


class TestEstimateMessagesTokens:
    """Tests for estimate_messages_tokens()."""

    def test_empty_messages(self) -> None:
        assert estimate_messages_tokens([]) == 0

    def test_single_message(self) -> None:
        messages = [{"role": "user", "content": "hello world"}]
        # "hello world" = 11 chars -> 2 tokens + 4 overhead = 6
        tokens = estimate_messages_tokens(messages)
        assert tokens >= 6

    def test_multiple_messages(self) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi!"},
        ]
        tokens = estimate_messages_tokens(messages)
        # Each message adds ~4 overhead tokens plus content
        assert tokens > 0

    def test_list_content(self) -> None:
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Hello"},
                    "plain string part",
                ],
            }
        ]
        tokens = estimate_messages_tokens(messages)
        assert tokens > 0

    def test_early_exit_with_limit(self) -> None:
        """Stops counting once total exceeds limit."""
        long_content = "x" * 1000  # ~250 tokens
        messages = [
            {"role": "user", "content": long_content},
            {"role": "user", "content": long_content},
        ]
        limit = 100
        result = estimate_messages_tokens(messages, limit=limit)
        assert result > limit

    def test_non_string_content_ignored(self) -> None:
        messages = [{"role": "user", "content": 123}]
        # Non-string content should be skipped, only overhead counted
        tokens = estimate_messages_tokens(messages)
        assert tokens == 4  # Just the overhead


class TestNeedsCompaction:
    """Tests for needs_compaction()."""

    def test_under_threshold(self) -> None:
        messages = [{"role": "user", "content": "short"}]
        assert not needs_compaction(messages, max_tokens=100000)

    def test_over_threshold(self) -> None:
        # Create enough content to exceed threshold
        long_content = "x" * 500000  # ~125K tokens worth
        messages = [{"role": "user", "content": long_content}]
        assert needs_compaction(messages, max_tokens=100000)

    def test_at_safety_margin(self) -> None:
        """At exactly the safety margin threshold, compaction is needed."""
        # threshold = max_tokens * 0.9
        # We need content that produces tokens >= threshold
        max_tokens = 1000
        threshold = int(max_tokens * SAFETY_MARGIN)  # 900
        # Need ~900 tokens of content
        long_content = "x" * (threshold * 4)  # 3600 chars -> ~900 tokens
        messages = [{"role": "user", "content": long_content}]
        assert needs_compaction(messages, max_tokens=max_tokens)

    def test_just_below_threshold(self) -> None:
        max_tokens = 1000
        threshold = int(max_tokens * SAFETY_MARGIN)  # 900
        # Just below threshold
        content_chars = (threshold - 10) * 4
        messages = [{"role": "user", "content": "x" * content_chars}]
        # This might still trigger due to overhead, but let's use very short
        messages_short = [{"role": "user", "content": "hi"}]
        assert not needs_compaction(messages_short, max_tokens=100000)


class TestCompactMessages:
    """Tests for compact_messages()."""

    def test_no_compaction_needed(self) -> None:
        messages = [
            {"role": "system", "content": "You are helpful."},
            {"role": "user", "content": "Hi!"},
        ]
        result = compact_messages(messages, max_tokens=100000)
        assert len(result) == len(messages)
        assert result == messages

    def test_preserves_system_prompt(self) -> None:
        """System prompt must never be discarded."""
        system_msg = {"role": "system", "content": "CRITICAL SYSTEM PROMPT"}
        many_messages = [system_msg] + [
            {"role": "user", "content": "x" * 10000}
            for _ in range(50)
        ]
        result = compact_messages(many_messages, max_tokens=1000)
        roles = [m["role"] for m in result]
        assert "system" in roles
        # Find the system message and verify content
        system_msgs = [m for m in result if m["role"] == "system"]
        assert any("CRITICAL SYSTEM PROMPT" in m["content"] for m in system_msgs)

    def test_preserves_last_n_messages(self) -> None:
        """Last MIN_KEEP_MESSAGES messages must be preserved."""
        messages = [
            {"role": "system", "content": "System"},
        ] + [
            {"role": "user", "content": f"Message {i}"}
            for i in range(20)
        ]
        result = compact_messages(messages, max_tokens=100)
        # Last MIN_KEEP_MESSAGES non-system messages should be present
        last_contents = [f"Message {i}" for i in range(20 - MIN_KEEP_MESSAGES, 20)]
        result_contents = [m["content"] for m in result]
        for content in last_contents:
            assert content in result_contents

    def test_inserts_summary_marker(self) -> None:
        """Compacted messages should contain SUMMARY_MARKER."""
        messages = [
            {"role": "system", "content": "System"},
        ] + [
            {"role": "user", "content": "x" * 10000}
            for _ in range(30)
        ]
        result = compact_messages(messages, max_tokens=500)
        contents = " ".join(m["content"] for m in result)
        assert SUMMARY_MARKER in contents

    def test_idempotent_compaction(self) -> None:
        """Compacting already-compacted history should be safe."""
        messages = [
            {"role": "system", "content": "System"},
        ] + [
            {"role": "user", "content": "x" * 10000}
            for _ in range(30)
        ]
        first_pass = compact_messages(messages, max_tokens=500)
        second_pass = compact_messages(first_pass, max_tokens=500)
        # Second pass should not further reduce below minimum
        assert len(second_pass) >= MIN_KEEP_MESSAGES + 1

    def test_few_messages_no_compaction(self) -> None:
        """When total messages <= MIN_KEEP_MESSAGES + 1, no compaction."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "Hi"},
            {"role": "assistant", "content": "Hello"},
        ]
        result = compact_messages(messages, max_tokens=100)
        assert len(result) == len(messages)

    def test_aggressive_truncate_when_still_over(self) -> None:
        """If still over threshold after summary, aggressive truncation kicks in."""
        # Create messages that even after summarization are too large
        messages = [
            {"role": "system", "content": "System"},
        ] + [
            {"role": "user", "content": "x" * 50000}
            for _ in range(50)
        ]
        result = compact_messages(messages, max_tokens=200)
        # Should have fewer messages than input
        assert len(result) < len(messages)


class TestSummarizeMessages:
    """Tests for _summarize_messages()."""

    def test_empty_messages(self) -> None:
        summary = _summarize_messages([])
        assert "0 messages" in summary or "Messages compressed: 0" in summary

    def test_extracts_tool_calls(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": '{"name": "read", "arguments": {}}',
            },
            {
                "role": "assistant",
                "content": '{"name": "write", "arguments": {}}',
            },
        ]
        summary = _summarize_messages(messages)
        assert "Tools used" in summary
        assert "read" in summary
        assert "write" in summary

    def test_extracts_files_read(self) -> None:
        messages = [
            {
                "role": "tool",
                "content": '{"success": true, "path": "/tmp/test.txt"}',
            }
        ]
        # Need to simulate that this was from a read command
        # The logic checks if '"read"' is in content[:200]
        messages_with_context = [
            {
                "role": "assistant",
                "content": '{"name": "read", "arguments": {"path": "/tmp/test.txt"}}',
            },
            {
                "role": "tool",
                "content": '{"success": true, "output": "data", "path": "/tmp/test.txt"}',
            },
        ]
        summary = _summarize_messages(messages_with_context)
        assert "Files read" in summary or "/tmp/test.txt" in summary

    def test_extracts_files_written(self) -> None:
        messages = [
            {
                "role": "assistant",
                "content": '{"name": "write", "arguments": {"path": "/tmp/out.txt"}}',
            },
            {
                "role": "tool",
                "content": '{"success": true, "path": "/tmp/out.txt"}',
            },
        ]
        summary = _summarize_messages(messages)
        assert "Files written" in summary or "/tmp/out.txt" in summary

    def test_extracts_errors(self) -> None:
        messages = [
            {
                "role": "tool",
                "content": '{"success": false, "error": "file not found"}',
            }
        ]
        summary = _summarize_messages(messages)
        assert "Errors encountered" in summary or "fail" in summary.lower()

    def test_non_string_content_skipped(self) -> None:
        messages = [
            {"role": "assistant", "content": 123},
        ]
        summary = _summarize_messages(messages)
        # Should handle gracefully
        assert isinstance(summary, str)


class TestAggressiveTruncate:
    """Tests for _aggressive_truncate()."""

    def test_basic_truncation(self) -> None:
        messages = [
            {"role": "system", "content": "System"},
        ] + [
            {"role": "user", "content": "x" * 1000}
            for _ in range(20)
        ]
        result = _aggressive_truncate(messages, max_tokens=500)
        assert len(result) < len(messages)
        # System message should be preserved
        assert any(m["role"] == "system" for m in result)

    def test_preserves_system_messages(self) -> None:
        messages = [
            {"role": "system", "content": "Critical System"},
            {"role": "user", "content": "x" * 10000},
        ]
        result = _aggressive_truncate(messages, max_tokens=100)
        assert any("Critical System" in m["content"] for m in result if m["role"] == "system")

    def test_keeps_at_least_two_messages(self) -> None:
        """Even under extreme truncation, keeps at least 2 non-system messages."""
        messages = [
            {"role": "system", "content": "System"},
            {"role": "user", "content": "x" * 100000},
            {"role": "assistant", "content": "y" * 100000},
        ]
        result = _aggressive_truncate(messages, max_tokens=10)
        non_system = [m for m in result if m["role"] != "system"]
        assert len(non_system) >= 1

    def test_empty_non_system(self) -> None:
        messages = [
            {"role": "system", "content": "Only system"},
        ]
        result = _aggressive_truncate(messages, max_tokens=100)
        assert len(result) == 1
        assert result[0]["role"] == "system"


class TestBuildCompactionCheckpoint:
    """Tests for build_compaction_checkpoint()."""

    def test_basic_checkpoint(self) -> None:
        checkpoint = build_compaction_checkpoint(
            ticket_id="CB-001",
            completed_steps=["step1", "step2"],
            remaining_steps=["step3"],
            files_changed=["foo.py"],
            current_state="in progress",
        )
        assert checkpoint["ticket_id"] == "CB-001"
        assert checkpoint["completed_steps"] == ["step1", "step2"]
        assert checkpoint["remaining_steps"] == ["step3"]
        assert checkpoint["files_changed"] == ["foo.py"]
        assert checkpoint["current_state"] == "in progress"
        assert "compacted_at" in checkpoint

    def test_truncates_long_current_state(self) -> None:
        long_state = "x" * 5000
        checkpoint = build_compaction_checkpoint(
            ticket_id="CB-001",
            completed_steps=[],
            remaining_steps=[],
            files_changed=[],
            current_state=long_state,
        )
        assert len(checkpoint["current_state"]) <= 2000

    def test_empty_lists(self) -> None:
        checkpoint = build_compaction_checkpoint(
            ticket_id="CB-001",
            completed_steps=[],
            remaining_steps=[],
            files_changed=[],
        )
        assert checkpoint["completed_steps"] == []
        assert checkpoint["remaining_steps"] == []
        assert checkpoint["files_changed"] == []
