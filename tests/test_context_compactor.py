"""Tests for codebot/context_compactor.py — agent session memory management."""

import sys
from pathlib import Path

import pytest

# Ensure project root is on sys.path for direct imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import codebot.context_compactor as cc


# ---------------------------------------------------------------------------
# estimate_tokens tests
# ---------------------------------------------------------------------------

class TestEstimateTokens:
    def test_basic_char_division(self):
        # 8 chars / 4 = 2 tokens
        assert cc.estimate_tokens("abcdefgh") == 2

    def test_minimum_is_one_for_empty_string(self):
        assert cc.estimate_tokens("") == 1

    def test_minimum_is_one_for_short_string(self):
        assert cc.estimate_tokens("a") == 1
        assert cc.estimate_tokens("abc") == 1

    def test_exact_multiple(self):
        assert cc.estimate_tokens("abcd") == 1  # 4 // 4 = 1
        assert cc.estimate_tokens("abcdefgh") == 2  # 8 // 4 = 2

    def test_large_text(self):
        text = "x" * 4000
        assert cc.estimate_tokens(text) == 1000


# ---------------------------------------------------------------------------
# estimate_messages_tokens tests
# ---------------------------------------------------------------------------

class TestEstimateMessagesTokens:
    def test_single_message_string_content(self):
        msgs = [{"role": "user", "content": "hello world"}]
        # "hello world" = 11 chars -> 2 tokens + 4 overhead = 6
        result = cc.estimate_messages_tokens(msgs)
        assert result == 6

    def test_multiple_messages(self):
        msgs = [
            {"role": "user", "content": "hi"},       # 1 + 4 = 5 (max(1, 2//4)=1)
            {"role": "assistant", "content": "hey"}, # 1 + 4 = 5
        ]
        assert cc.estimate_messages_tokens(msgs) == 10

    def test_list_content_with_dict_parts(self):
        msgs = [{
            "role": "user",
            "content": [
                {"text": "part one"},   # 8 chars -> 2 tokens
                {"text": "part two"},   # 8 chars -> 2 tokens
            ]
        }]
        # 2 + 2 + 4 overhead = 8
        assert cc.estimate_messages_tokens(msgs) == 8

    def test_list_content_with_string_parts(self):
        msgs = [{
            "role": "user",
            "content": ["hello", "world"]
        }]
        # 1 + 1 + 4 = 6
        assert cc.estimate_messages_tokens(msgs) == 6

    def test_missing_content_key_defaults_to_zero_plus_overhead(self):
        msgs = [{"role": "user"}]
        # content defaults to "" -> max(1, 0) = 1 + 4 = 5
        assert cc.estimate_messages_tokens(msgs) == 5

    def test_early_exit_with_limit(self):
        msgs = [
            {"role": "user", "content": "a" * 400},      # 100 + 4 = 104
            {"role": "user", "content": "b" * 400},      # would push over limit
        ]
        # With limit=100, first message alone (104) exceeds limit
        result = cc.estimate_messages_tokens(msgs, limit=100)
        assert result > 100

    def test_no_early_exit_when_under_limit(self):
        msgs = [
            {"role": "user", "content": "hi"},  # 1 + 4 = 5
        ]
        result = cc.estimate_messages_tokens(msgs, limit=1000)
        assert result == 5

    def test_empty_messages_list(self):
        assert cc.estimate_messages_tokens([]) == 0


# ---------------------------------------------------------------------------
# needs_compaction tests
# ---------------------------------------------------------------------------

class TestNeedsCompaction:
    def test_returns_false_for_small_history(self):
        msgs = [{"role": "user", "content": "hello"}]
        assert cc.needs_compaction(msgs) is False

    def test_returns_true_when_exceeding_threshold(self):
        # threshold = 120000 * 0.9 = 108000 tokens
        # Each msg ~ 10000 tokens + 4 overhead
        large_content = "x" * 40000  # 10000 tokens
        msgs = [{"role": "user", "content": large_content}] * 12
        assert cc.needs_compaction(msgs) is True

    def test_custom_max_tokens(self):
        msgs = [{"role": "user", "content": "x" * 400}]  # 100 tokens + 4 = 104
        # threshold = 100 * 0.9 = 90
        assert cc.needs_compaction(msgs, max_tokens=100) is True
        # threshold = 200 * 0.9 = 180
        assert cc.needs_compaction(msgs, max_tokens=200) is False


# ---------------------------------------------------------------------------
# compact_messages tests — system prompt preservation
# ---------------------------------------------------------------------------

class TestCompactMessagesSystemPreservation:
    def test_system_prompt_never_discarded(self):
        system_msg = {"role": "system", "content": "You are a helpful assistant."}
        user_msgs = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
        messages = [system_msg] + user_msgs

        # Use very low max_tokens to force compaction
        result = cc.compact_messages(messages, max_tokens=30)

        system_results = [m for m in result if m.get("role") == "system"]
        assert len(system_results) == 1
        assert system_results[0]["content"] == "You are a helpful assistant."

    def test_multiple_system_prompts_preserved(self):
        sys1 = {"role": "system", "content": "System 1"}
        sys2 = {"role": "system", "content": "System 2"}
        user_msgs = [{"role": "user", "content": f"msg {i}"} for i in range(10)]
        messages = [sys1, sys2] + user_msgs

        result = cc.compact_messages(messages, max_tokens=30)

        system_results = [m for m in result if m.get("role") == "system"]
        assert len(system_results) == 2


# ---------------------------------------------------------------------------
# compact_messages tests — last 4 messages retained
# ---------------------------------------------------------------------------

class TestCompactMessagesRecentRetention:
    def test_last_four_non_system_messages_retained(self):
        messages = [{"role": "user", "content": f"message_{i}"} for i in range(10)]
        # Force compaction with tiny max_tokens
        result = cc.compact_messages(messages, max_tokens=30)

        # Extract non-summary, non-system messages
        content_texts = [m["content"] for m in result if m.get("role") != "system"]
        # The last 4 original messages should appear
        for i in range(6, 10):
            assert f"message_{i}" in content_texts

    def test_summary_marker_inserted(self):
        messages = [{"role": "user", "content": f"message_{i}"} for i in range(10)]
        result = cc.compact_messages(messages, max_tokens=30)

        all_content = " ".join(m.get("content", "") for m in result)
        assert cc.SUMMARY_MARKER in all_content

    def test_no_compaction_when_under_threshold(self):
        messages = [{"role": "user", "content": "short"} for _ in range(3)]
        result = cc.compact_messages(messages, max_tokens=120_000)
        # Should return unchanged since under threshold and few messages
        assert result == messages

    def test_no_compaction_when_too_few_messages(self):
        # MIN_KEEP_MESSAGES + 1 = 5; with <= 5 messages, returns early
        messages = [{"role": "user", "content": "x"} for _ in range(5)]
        result = cc.compact_messages(messages, max_tokens=1)
        assert result == messages


# ---------------------------------------------------------------------------
# compact_messages tests — idempotent compaction
# ---------------------------------------------------------------------------

class TestCompactMessagesIdempotency:
    def test_compacting_already_compacted_history_is_safe(self):
        messages = [{"role": "user", "content": f"message_{i}"} for i in range(10)]
        first_pass = cc.compact_messages(messages, max_tokens=30)
        second_pass = cc.compact_messages(first_pass, max_tokens=30)

        # Second pass should not crash or lose the summary marker
        all_content = " ".join(m.get("content", "") for m in second_pass)
        assert cc.SUMMARY_MARKER in all_content

    def test_idempotent_does_not_duplicate_summary_markers_excessively(self):
        messages = [{"role": "user", "content": f"msg_{i}"} for i in range(10)]
        first_pass = cc.compact_messages(messages, max_tokens=30)
        second_pass = cc.compact_messages(first_pass, max_tokens=30)

        count_first = sum(
            1 for m in first_pass if cc.SUMMARY_MARKER in m.get("content", "")
        )
        count_second = sum(
            1 for m in second_pass if cc.SUMMARY_MARKER in m.get("content", "")
        )
        # Should not grow unboundedly
        assert count_second <= count_first + 1


# ---------------------------------------------------------------------------
# Constants verification
# ---------------------------------------------------------------------------

class TestConstants:
    def test_safety_margin_value(self):
        assert cc.SAFETY_MARGIN == 0.9

    def test_min_keep_messages_value(self):
        assert cc.MIN_KEEP_MESSAGES == 4

    def test_default_max_tokens_value(self):
        assert cc.DEFAULT_MAX_TOKENS == 120_000

    def test_chars_per_token_value(self):
        assert cc.CHARS_PER_TOKEN == 4

    def test_summary_marker_value(self):
        assert cc.SUMMARY_MARKER == "[CONTEXT COMPACTED]"


# ---------------------------------------------------------------------------
# build_compaction_checkpoint tests
# ---------------------------------------------------------------------------

class TestBuildCompactionCheckpoint:
    def test_returns_correct_structure(self):
        ckpt = cc.build_compaction_checkpoint(
            ticket_id="CB-123",
            completed_steps=["step1"],
            remaining_steps=["step2"],
            files_changed=["file.py"],
            current_state="working",
        )
        assert ckpt["ticket_id"] == "CB-123"
        assert ckpt["completed_steps"] == ["step1"]
        assert ckpt["remaining_steps"] == ["step2"]
        assert ckpt["files_changed"] == ["file.py"]
        assert ckpt["current_state"] == "working"
        assert "compacted_at" in ckpt

    def test_current_state_truncated_to_2000_chars(self):
        long_state = "x" * 3000
        ckpt = cc.build_compaction_checkpoint(
            ticket_id="T",
            completed_steps=[],
            remaining_steps=[],
            files_changed=[],
            current_state=long_state,
        )
        assert len(ckpt["current_state"]) == 2000
