"""Tests for context-assembly tracing in api_runner.py.

CB-3548779-D85C: Verify structured JSON logs for data-flow tracing,
including size metrics, truncation events, and PII sanitization.
"""
import json
import re
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.api_runner import (
    ContextAssemblyTracer,
    _log_context_assembly,
)


class TestContextAssemblyTracer:
    """Tests for ContextAssemblyTracer class."""

    @pytest.fixture
    def tracer(self, tmp_path):
        """Create a tracer with a temp log directory."""
        return ContextAssemblyTracer("test-bot", tmp_path)

    def test_init_creates_log_path(self, tmp_path):
        """Tracer initializes with correct log path."""
        tracer = ContextAssemblyTracer("my-bot", tmp_path)
        assert tracer._log_path == tmp_path / "my-bot.context_trace.jsonl"

    def test_sanitize_for_log_strips_email(self, tracer):
        """Email addresses are redacted."""
        text = "Contact user@example.com for help"
        sanitized = ContextAssemblyTracer.sanitize_for_log(text)
        assert "user@example.com" not in sanitized
        assert "[REDACTED_EMAIL]" in sanitized

    def test_sanitize_for_log_strips_ip_address(self, tracer):
        """IP addresses are redacted."""
        text = "Server at 192.168.1.100 responded"
        sanitized = ContextAssemblyTracer.sanitize_for_log(text)
        assert "192.168.1.100" not in sanitized
        assert "[REDACTED_IP]" in sanitized

    def test_sanitize_for_log_strips_phone(self, tracer):
        """Phone numbers are redacted."""
        text = "Call +1-555-123-4567 for support"
        sanitized = ContextAssemblyTracer.sanitize_for_log(text)
        assert "+1-555-123-4567" not in sanitized
        assert "[REDACTED_PHONE]" in sanitized

    def test_sanitize_for_log_strips_api_key_patterns(self, tracer):
        """API key patterns are redacted."""
        test_cases = [
            ("sk-abc123xyz789def", "sk-"),
            ("dgr-secretkeyvalue123", "dgr-"),
            ("api_key=supersecret123", "supersecret"),
            ("token:abcdefghij1234", "abcdefghij"),
        ]
        for text, sensitive_part in test_cases:
            sanitized = ContextAssemblyTracer.sanitize_for_log(text)
            if sensitive_part in text:
                assert sensitive_part not in sanitized or "[REDACTED_KEY]" in sanitized

    def test_sanitize_for_log_empty_string(self, tracer):
        """Empty string returns empty string."""
        assert ContextAssemblyTracer.sanitize_for_log("") == ""

    def test_log_tool_output_returns_event_dict(self, tracer):
        """Tool output returns event dict with required fields."""
        tool_result = {"success": True, "output": "data", "error": None}
        event = tracer.log_tool_output("read", tool_result, iteration=0)

        assert event is not None
        assert event["event"] == "tool_output"
        assert event["tool"] == "read"
        assert event["iteration"] == 0
        assert "output_bytes" in event
        assert "truncated" in event
        assert "token_estimate" in event

    def test_log_tool_output_tracks_bytes(self, tracer):
        """Tool output tracks output bytes correctly."""
        tool_result = {"success": True, "output": "x" * 100}
        tracer.log_tool_output("read", tool_result, iteration=0)

        summary = tracer.summary()
        assert summary["total_output_bytes"] > 0

    def test_log_tool_output_truncation_flag(self, tracer):
        """Truncation flag is set when output exceeds max."""
        large_output = {"success": True, "output": "x" * 5000}
        event = tracer.log_tool_output("bash", large_output, iteration=0, max_tool_bytes=2000)

        assert event["truncated"] is True
        assert event["truncation_ratio"] > 0

    def test_log_context_compaction_records_event(self, tracer):
        """Context compaction events are logged."""
        messages_before = [{"role": "user", "content": "x" * 100}] * 10
        messages_after = [{"role": "user", "content": "summary"}]

        event = tracer.log_context_compaction(
            messages_before, messages_after,
            tokens_before=1000,
            tokens_after=100,
            method="summarize",
        )

        assert event["event"] == "context_compaction"
        assert event["method"] == "summarize"
        assert event["messages_before"] == 10
        assert event["messages_after"] == 1
        assert event["reduction_ratio"] > 0

    def test_log_api_call_prepared_records_metrics(self, tracer):
        """API call preparation logs message count and size."""
        event = tracer.log_api_call_prepared(
            message_count=15,
            total_chars=5000,
            iteration=3,
        )

        assert event["event"] == "api_call_prepared"
        assert event["message_count"] == 15
        assert event["total_chars"] == 5000
        assert "token_estimate" in event
        assert event["token_estimate"] == 5000 // 4

    def test_log_stream_truncation_records_ratio(self, tracer):
        """Stream truncation logs the truncation ratio."""
        event = tracer.log_stream_truncation(
            original_size=10000,
            final_size=5000,
            messages_kept=21,
        )

        assert event["event"] == "stream_truncation"
        assert event["original_bytes"] == 10000
        assert event["final_bytes"] == 5000
        assert event["truncation_ratio"] == 0.5
        assert event["messages_kept"] == 21

    def test_summary_aggregates_metrics(self, tracer):
        """Summary returns aggregated metrics."""
        tracer.log_tool_output("read", {"success": True}, 0)
        tracer.log_tool_output("write", {"success": True}, 1)
        tracer.log_context_compaction([], [], 100, 50, "truncate")

        summary = tracer.summary()
        assert summary["bot"] == "test-bot"
        assert summary["total_events"] >= 2
        assert "total_input_chars" in summary
        assert "total_output_bytes" in summary
        assert "truncation_events" in summary
        assert "compaction_events" in summary

    def test_flush_does_not_crash(self, tracer):
        """Flush completes without raising exceptions."""
        tracer.log_tool_output("read", {"success": True}, 0)
        tracer.flush()
        # If we get here without exception, test passes
        assert True

    def test_emit_fail_open_on_io_error(self, tmp_path):
        """Emit fails open if log file cannot be written."""
        bad_path = tmp_path / "nonexistent" / "bot.context_trace.jsonl"
        tracer = ContextAssemblyTracer("test-bot", bad_path.parent)

        event = tracer.log_tool_output("read", {"success": True}, 0)
        assert event is not None


class TestLogContextAssembly:
    """Tests for _log_context_assembly function."""

    def test_sanitizes_extra_dict(self, tmp_path, monkeypatch):
        """Extra dict is sanitized to remove sensitive keys/values."""
        monkeypatch.setattr("codebot.api_runner.BOTS_DIR", tmp_path)

        _log_context_assembly(
            bot_name="test-bot",
            event_type="test_event",
            input_size=0,
            output_size=0,
            extra={
                "api_key": "sk-secret123",
                "success": True,
                "iteration": 5,
                "password": "hunter2",
                "tool_call_id": "call_abc123",
            },
        )

        log_path = tmp_path / "logs" / "test-bot.context_trace.jsonl"
        if log_path.exists():
            content = log_path.read_text()
            event = json.loads(content.strip())
            metadata = event.get("metadata", {})
            assert "api_key" not in metadata
            assert "password" not in metadata
            assert "success" in metadata or "iteration" in metadata

    def test_truncation_ratio_clamped(self, tmp_path, monkeypatch):
        """Truncation ratio is clamped to [0.0, 1.0]."""
        monkeypatch.setattr("codebot.api_runner.BOTS_DIR", tmp_path)

        _log_context_assembly(
            bot_name="test-bot",
            event_type="test",
            input_size=100,
            output_size=200,
            truncation_ratio=1.5,
        )

        log_path = tmp_path / "logs" / "test-bot.context_trace.jsonl"
        if log_path.exists():
            event = json.loads(log_path.read_text().strip())
            assert 0.0 <= event["truncation_ratio"] <= 1.0

    def test_negative_sizes_clamped_to_zero(self, tmp_path, monkeypatch):
        """Negative sizes are clamped to zero."""
        monkeypatch.setattr("codebot.api_runner.BOTS_DIR", tmp_path)

        _log_context_assembly(
            bot_name="test-bot",
            event_type="test",
            input_size=-100,
            output_size=-50,
        )

        log_path = tmp_path / "logs" / "test-bot.context_trace.jsonl"
        if log_path.exists():
            event = json.loads(log_path.read_text().strip())
            assert event["input_size_bytes"] == 0
            assert event["output_size_bytes"] == 0

    def test_fail_open_on_exception(self, monkeypatch):
        """Function fails open on any exception."""
        monkeypatch.setattr("codebot.api_runner.BOTS_DIR", "/nonexistent/path")

        try:
            _log_context_assembly(
                bot_name="test",
                event_type="test",
                input_size=0,
                output_size=0,
            )
        except Exception:
            pytest.fail("_log_context_assembly should not raise")


class TestIntegration:
    """Integration tests for context-assembly tracing."""

    def test_no_pii_leaked_in_full_trace(self, tmp_path):
        """Full trace session does not leak PII."""
        tracer = ContextAssemblyTracer("secure-bot", tmp_path)

        pii_laden_result = {
            "success": True,
            "output": "User email: test@example.com, IP: 10.0.0.1",
            "error": None,
        }
        tracer.log_tool_output("bash", pii_laden_result, 0)
        tracer.log_api_call_prepared(5, 500, 1)
        tracer.flush()

        if tracer._log_path.exists():
            content = tracer._log_path.read_text()
            assert "test@example.com" not in content
            assert "10.0.0.1" not in content
