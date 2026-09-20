"""Tests for context assembly tracing in codebot/api_runner.py.

Covers:
- _log_context_assembly writes structured JSON
- includes size metrics (input/output/truncation/token count)
- no PII leaked in logs
- unit tests verify log format

Ticket: CB-3548779-D85C
"""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure codebot is importable
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.api_runner import _log_context_assembly, BOTS_DIR


class TestLogContextAssembly:
    """Tests for _log_context_assembly structured logging."""

    def test_writes_structured_json(self, tmp_path):
        """Verify that context assembly logs are written as structured JSON."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            _log_context_assembly(
                bot_name="test-bot",
                event_type="tool_result_appended",
                input_size=100,
                output_size=200,
                truncation_ratio=0.5,
                final_token_count=50,
                tool_name="read",
            )
        
        log_path = log_dir / "test-bot.context_trace.jsonl"
        assert log_path.exists(), "Log file should be created"
        
        lines = log_path.read_text().strip().split("\n")
        assert len(lines) >= 1, "Should have at least one log line"
        
        # Parse the first line as JSON
        event = json.loads(lines[0])
        assert "timestamp" in event
        assert "event_type" in event
        assert event["event_type"] == "tool_result_appended"

    def test_includes_size_metrics(self, tmp_path):
        """Verify that logs include input_size_bytes, output_size_bytes, truncation_ratio, final_token_count."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            _log_context_assembly(
                bot_name="metrics-bot",
                event_type="test_event",
                input_size=500,
                output_size=250,
                truncation_ratio=0.5,
                final_token_count=100,
            )
        
        log_path = log_dir / "metrics-bot.context_trace.jsonl"
        event = json.loads(log_path.read_text().strip())
        
        assert event["input_size_bytes"] == 500
        assert event["output_size_bytes"] == 250
        assert event["truncation_ratio"] == 0.5
        assert event["final_token_count"] == 100

    def test_no_pii_leaked_in_logs(self, tmp_path):
        """Verify that sensitive data is not leaked in log output."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        # Simulate PII-like data that should NOT appear in logs
        pii_data = {
            "api_key": "sk-secret-key-12345",
            "password": "super_secret_password",
            "token": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9",
        }
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            _log_context_assembly(
                bot_name="security-bot",
                event_type="test_with_pii",
                input_size=100,
                output_size=100,
                truncation_ratio=1.0,
                final_token_count=25,
                extra=pii_data,
            )
        
        log_path = log_dir / "security-bot.context_trace.jsonl"
        log_content = log_path.read_text()
        
        # Verify PII is not in the log
        for key, value in pii_data.items():
            assert value not in log_content, f"PII value '{value}' should not appear in logs"
        
        # Parse and check metadata is sanitized
        event = json.loads(log_content.strip())
        if "metadata" in event:
            for v in event["metadata"].values():
                assert "sk-secret" not in str(v), "API key fragment should not appear"
                assert "super_secret" not in str(v), "Password should not appear"

    def test_log_format_has_required_fields(self, tmp_path):
        """Verify log entries have all required fields for debugging."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            _log_context_assembly(
                bot_name="format-bot",
                event_type="assembly_step",
                input_size=1000,
                output_size=500,
                truncation_ratio=0.5,
                final_token_count=200,
                tool_name="bash",
            )
        
        log_path = log_dir / "format-bot.context_trace.jsonl"
        event = json.loads(log_path.read_text().strip())
        
        required_fields = [
            "timestamp", "timestamp_human", "event_type",
            "input_size_bytes", "output_size_bytes",
            "truncation_ratio", "final_token_count", "tool_name"
        ]
        
        for field in required_fields:
            assert field in event, f"Missing required field: {field}"

    def test_truncation_ratio_clamped(self, tmp_path):
        """Verify truncation ratio is clamped between 0.0 and 1.0."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            # Test with invalid ratio > 1.0
            _log_context_assembly(
                bot_name="clamp-bot",
                event_type="test",
                input_size=100,
                output_size=200,
                truncation_ratio=2.5,  # Invalid: > 1.0
                final_token_count=50,
            )
        
        log_path = log_dir / "clamp-bot.context_trace.jsonl"
        event = json.loads(log_path.read_text().strip())
        
        assert event["truncation_ratio"] <= 1.0, "Ratio should be clamped to max 1.0"
        assert event["truncation_ratio"] >= 0.0, "Ratio should be clamped to min 0.0"

    def test_negative_sizes_clamped_to_zero(self, tmp_path):
        """Verify negative sizes are clamped to zero."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            _log_context_assembly(
                bot_name="negative-bot",
                event_type="test",
                input_size=-100,
                output_size=-50,
                truncation_ratio=0.5,
                final_token_count=-10,
            )
        
        log_path = log_dir / "negative-bot.context_trace.jsonl"
        event = json.loads(log_path.read_text().strip())
        
        assert event["input_size_bytes"] >= 0
        assert event["output_size_bytes"] >= 0
        assert event["final_token_count"] >= 0

    def test_tool_name_truncated(self, tmp_path):
        """Verify tool name is truncated to prevent oversized entries."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        long_tool_name = "x" * 100
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            _log_context_assembly(
                bot_name="truncate-bot",
                event_type="test",
                input_size=100,
                output_size=100,
                truncation_ratio=1.0,
                final_token_count=25,
                tool_name=long_tool_name,
            )
        
        log_path = log_dir / "truncate-bot.context_trace.jsonl"
        event = json.loads(log_path.read_text().strip())
        
        assert len(event["tool_name"]) <= 64, "Tool name should be truncated to 64 chars"

    def test_extra_metadata_sanitized(self, tmp_path):
        """Verify extra metadata keys and values are sanitized."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        long_key = "x" * 100
        long_value = "y" * 300
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            _log_context_assembly(
                bot_name="sanitize-bot",
                event_type="test",
                input_size=100,
                output_size=100,
                truncation_ratio=1.0,
                final_token_count=25,
                extra={long_key: long_value, "safe_key": 123, "bool_key": True},
            )
        
        log_path = log_dir / "sanitize-bot.context_trace.jsonl"
        event = json.loads(log_path.read_text().strip())
        
        if "metadata" in event:
            for k, v in event["metadata"].items():
                assert len(k) <= 64, f"Metadata key should be truncated: {k}"
                if isinstance(v, str):
                    assert len(v) <= 200, f"Metadata value should be truncated: {v[:50]}..."

    def test_multiple_events_append(self, tmp_path):
        """Verify multiple events are appended to the same log file."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            for i in range(3):
                _log_context_assembly(
                    bot_name="multi-bot",
                    event_type=f"event_{i}",
                    input_size=100 * (i + 1),
                    output_size=50 * (i + 1),
                    truncation_ratio=0.5,
                    final_token_count=25 * (i + 1),
                )
        
        log_path = log_dir / "multi-bot.context_trace.jsonl"
        lines = log_path.read_text().strip().split("\n")
        
        assert len(lines) == 3, "Should have 3 log entries"
        
        # Verify each line is valid JSON
        for line in lines:
            event = json.loads(line)
            assert "event_type" in event

    def test_fail_open_on_error(self, tmp_path):
        """Verify that logging failures don't raise exceptions."""
        bots_dir = tmp_path
        
        # This should not raise even if there's an issue
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            try:
                _log_context_assembly(
                    bot_name="failopen-bot",
                    event_type="test",
                    input_size=100,
                    output_size=100,
                    truncation_ratio=1.0,
                    final_token_count=25,
                )
                # If we get here without exception, the test passes
                assert True
            except Exception as e:
                pytest.fail(f"_log_context_assembly should not raise: {e}")

    def test_timestamp_is_valid_unix_time(self, tmp_path):
        """Verify timestamp is a valid Unix timestamp."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        before = time.time()
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            _log_context_assembly(
                bot_name="time-bot",
                event_type="test",
                input_size=100,
                output_size=100,
                truncation_ratio=1.0,
                final_token_count=25,
            )
        after = time.time()
        
        log_path = log_dir / "time-bot.context_trace.jsonl"
        event = json.loads(log_path.read_text().strip())
        
        ts = event["timestamp"]
        assert before <= ts <= after, "Timestamp should be within expected range"

    def test_bot_specific_log_file(self, tmp_path):
        """Verify each bot writes to its own log file."""
        bots_dir = tmp_path
        log_dir = bots_dir / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        
        with patch("codebot.api_runner.BOTS_DIR", bots_dir):
            _log_context_assembly(
                bot_name="bot-alpha",
                event_type="test",
                input_size=100,
                output_size=100,
                truncation_ratio=1.0,
                final_token_count=25,
            )
            _log_context_assembly(
                bot_name="bot-beta",
                event_type="test",
                input_size=100,
                output_size=100,
                truncation_ratio=1.0,
                final_token_count=25,
            )
        
        alpha_log = log_dir / "bot-alpha.context_trace.jsonl"
        beta_log = log_dir / "bot-beta.context_trace.jsonl"
        
        assert alpha_log.exists(), "Alpha bot should have its own log"
        assert beta_log.exists(), "Beta bot should have its own log"
        assert alpha_log != beta_log, "Logs should be separate files"
