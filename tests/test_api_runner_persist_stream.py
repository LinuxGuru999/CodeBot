"""Regression tests for _persist_stream with 100K+ message history.

Ticket: CB-5B5BB
Class: test
Severity: critical

Problem: No test verifies _persist_stream survives 100K+ message history without
exhausting memory, leaving the DoS fix unverified against attacker-influenced
conversation growth.

Desired State: Regression test proves _persist_stream handles 100K+ message
history without OOM and persisted output stays within size bound.

Acceptance Criteria:
- Test constructs 100K+ synthetic message history and calls _persist_stream
  without OOM verified by passing test;
- test asserts persisted stream file exists and stays under 500KB cap;
- test asserts no unbounded allocation by mocking or measuring peak usage;
- pytest passes for new test file.
"""
import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.api_runner import _persist_stream


class TestPersistStream100KHistory:
    """Tests verifying _persist_stream handles 100K+ message history safely."""

    def test_persist_stream_100k_messages_no_oom_and_size_cap(self, tmp_path):
        """Constructs 100K+ synthetic messages and verifies _persist_stream:
        1. Completes without MemoryError (OOM).
        2. Produces a stream file <= 500KB.
        3. Sets the truncated flag.
        4. Persists some messages (not all, due to truncation).
        """
        # Arrange
        num_messages = 100_000
        # Use small messages to keep test runtime reasonable while still
        # generating enough data to trigger truncation logic.
        # Each message is roughly {"role": "user", "content": "msg N ..."}
        messages = [
            {"role": "user", "content": f"msg {i} hello world test content abcdef"}
            for i in range(num_messages)
        ]
        assert len(messages) >= 100_000, "Must construct 100K+ message list"

        bot_name = "test-100k-bot"
        import codebot.api_runner as ar

        # Ensure logs directory exists
        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # Act
        start_time = time.monotonic()
        try:
            with patch.object(ar, "BOTS_DIR", tmp_path):
                _persist_stream(
                    bot_name=bot_name,
                    messages=messages,
                    active_model="test-model",
                    tool_iterations=1,
                    exit_reason="completed",
                )
        except MemoryError:
            pytest.fail("_persist_stream raised MemoryError on 100K messages - DoS vulnerability")
        
        elapsed = time.monotonic() - start_time

        # Assert: Performance bound (should be fast due to bounded accumulation)
        # Allow generous time for CI environments, but it should be reasonably fast
        assert elapsed < 60, f"Persist stream took {elapsed:.2f}s, expected < 60s"

        # Assert: Stream file exists
        stream_path = log_dir / f"{bot_name}.stream.json"
        assert stream_path.exists(), f"Stream file must exist at {stream_path}"

        # Assert: File size <= 500KB (500,000 bytes)
        raw_content = stream_path.read_text(encoding="utf-8")
        file_size_bytes = len(raw_content.encode("utf-8"))
        assert file_size_bytes <= 500_000, (
            f"Stream file size {file_size_bytes} bytes exceeds 500KB cap"
        )

        # Assert: Valid JSON and truncation flag set
        payload = json.loads(raw_content)
        assert payload.get("truncated") is True, (
            "Truncation flag must be set for 100K input exceeding capacity"
        )
        assert "messages" in payload
        
        # Assert: Not all messages persisted (truncation occurred)
        persisted_count = len(payload["messages"])
        assert persisted_count < num_messages, (
            f"Persisted messages ({persisted_count}) must be less than input ({num_messages})"
        )
        assert persisted_count > 0, "At least some messages must be persisted"

    def test_persist_stream_100k_tool_messages_truncation_and_cap(self, tmp_path):
        """Verifies 100K tool messages with large content are truncated per-entry
        and total file stays under 500KB cap.
        """
        # Arrange
        num_messages = 100_000
        # Content larger than the 2000-char tool truncation limit
        large_content = "x" * 3000
        messages = [
            {"role": "tool", "content": large_content, "tool_call_id": f"call_{i}"}
            for i in range(num_messages)
        ]

        bot_name = "test-100k-tool-bot"
        import codebot.api_runner as ar

        log_dir = tmp_path / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)

        # Act
        start_time = time.monotonic()
        with patch.object(ar, "BOTS_DIR", tmp_path):
            _persist_stream(
                bot_name=bot_name,
                messages=messages,
                active_model="test-model",
                tool_iterations=1,
                exit_reason="iteration_limit",
            )
        elapsed = time.monotonic() - start_time
        assert elapsed < 60, f"Took {elapsed:.2f}s, expected < 60s"

        # Assert
        stream_path = log_dir / f"{bot_name}.stream.json"
        assert stream_path.exists()
        
        raw_content = stream_path.read_text(encoding="utf-8")
        file_size_bytes = len(raw_content.encode("utf-8"))
        assert file_size_bytes <= 500_000, f"File size {file_size_bytes} exceeds 500KB"

        payload = json.loads(raw_content)
        assert payload.get("truncated") is True

        # Verify per-entry tool truncation was applied to persisted entries
        for m in payload["messages"]:
            if m.get("role") == "tool" and isinstance(m.get("content"), str):
                # Content should be truncated to 2000 chars + suffix
                max_allowed_len = 2000 + len("...[truncated]")
                assert len(m["content"]) <= max_allowed_len, (
                    f"Tool content length {len(m['content'])} exceeds limit {max_allowed_len}"
                )
