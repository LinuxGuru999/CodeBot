"""Tests for codebot.event_log secret sanitization and event persistence.

Covers _looks_secret pattern matching, _clean_value recursive sanitization,
sanitize_data with nested structures, append_event file I/O, and read_events
with limit enforcement per ticket CB-6099545-F036.
"""

import json
import time
from pathlib import Path

import pytest

from codebot.event_log import (
    MAX_EVENTS,
    _clean_value,
    _looks_secret,
    append_event,
    read_events,
    sanitize_data,
)


class TestLooksSecret:
    """Verify _looks_secret detects known secret prefixes."""

    def test_detects_github_personal_token(self) -> None:
        assert _looks_secret("ghp_1234567890abcdef") is True

    def test_detects_github_oauth_token(self) -> None:
        assert _looks_secret("gho_abcdef1234567890") is True

    def test_detects_openai_api_key(self) -> None:
        assert _looks_secret("sk-proj-abc123") is True

    def test_detects_bearer_token(self) -> None:
        assert _looks_secret("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9") is True

    def test_detects_slack_bot_token(self) -> None:
        assert _looks_secret("xoxb-123-456-789") is True

    def test_non_secret_string_returns_false(self) -> None:
        assert _looks_secret("hello world") is False

    def test_empty_string_returns_false(self) -> None:
        assert _looks_secret("") is False

    def test_partial_prefix_not_detected(self) -> None:
        # 'gh' alone should not trigger; must match full prefix
        assert _looks_secret("gh_token") is False


class TestCleanValue:
    """Verify _clean_value redacts secrets and handles nested structures."""

    def test_redacts_secret_string(self) -> None:
        assert _clean_value("sk-secret-key") == "[redacted]"

    def test_preserves_non_secret_string(self) -> None:
        assert _clean_value("safe-value") == "safe-value"

    def test_truncates_long_string(self) -> None:
        long_val = "a" * 300
        result = _clean_value(long_val)
        assert isinstance(result, str)
        assert len(result) <= 256

    def test_preserves_none(self) -> None:
        assert _clean_value(None) is None

    def test_preserves_bool(self) -> None:
        assert _clean_value(True) is True
        assert _clean_value(False) is False

    def test_preserves_int(self) -> None:
        assert _clean_value(42) == 42

    def test_preserves_float(self) -> None:
        assert _clean_value(3.14) == 3.14

    def test_redacts_nested_dict_secret_values(self) -> None:
        data = {"config": {"api_key": "sk-hidden", "name": "test"}}
        result = _clean_value(data)
        assert isinstance(result, dict)
        # 'api_key' key contains sensitive substring -> dropped entirely
        assert "api_key" not in result.get("config", {})
        assert result["config"]["name"] == "test"

    def test_drops_sensitive_keys_in_dict(self) -> None:
        data = {"password": "secret123", "username": "admin"}
        result = _clean_value(data)
        assert isinstance(result, dict)
        assert "password" not in result
        assert result["username"] == "admin"

    def test_cleans_list_items(self) -> None:
        data = ["safe", "sk-leaked", "also-safe"]
        result = _clean_value(data)
        assert isinstance(result, list)
        assert result[0] == "safe"
        assert result[1] == "[redacted]"
        assert result[2] == "also-safe"

    def test_limits_list_items_to_max(self) -> None:
        data = list(range(30))
        result = _clean_value(data)
        assert isinstance(result, list)
        assert len(result) <= 20

    def test_limits_dict_fields_to_max(self) -> None:
        data = {f"key_{i}": f"val_{i}" for i in range(40)}
        result = _clean_value(data)
        assert isinstance(result, dict)
        assert len(result) <= 32

    def test_depth_limit_converts_to_truncated_string(self) -> None:
        # Depth > 2 should convert to truncated string representation
        deeply_nested = {"a": {"b": {"c": {"d": "deep"}}}}
        result = _clean_value(deeply_nested)
        # At depth 0 -> dict, depth 1 -> dict, depth 2 -> dict, depth 3 -> str truncation
        inner = result.get("a", {}).get("b", {}).get("c", {})
        # At depth=3, the value should be a string (truncated repr of {'d': 'deep'})
        assert isinstance(inner, str)

    def test_non_string_dict_keys_are_skipped(self) -> None:
        data = {1: "one", "valid": "kept"}  # type: ignore[dict-item]
        result = _clean_value(data)
        assert isinstance(result, dict)
        assert 1 not in result
        assert result["valid"] == "kept"

    def test_tuple_treated_as_list(self) -> None:
        data = ("a", "sk-secret", "b")
        result = _clean_value(data)
        assert isinstance(result, list)
        assert result[1] == "[redacted]"


class TestSanitizeData:
    """Verify sanitize_data preserves non-secret fields and strips secrets."""

    def test_preserves_safe_fields(self) -> None:
        data = {"status": "ok", "count": 5, "enabled": True}
        result = sanitize_data(data)
        assert result == {"status": "ok", "count": 5, "enabled": True}

    def test_strips_token_key(self) -> None:
        data = {"token": "ghp_abc123", "user": "alice"}
        result = sanitize_data(data)
        assert "token" not in result
        assert result["user"] == "alice"

    def test_strips_authorization_key(self) -> None:
        data = {"authorization": "Bearer xyz", "action": "read"}
        result = sanitize_data(data)
        assert "authorization" not in result
        assert result["action"] == "read"

    def test_strips_prompt_and_tool_output_keys(self) -> None:
        data = {"prompt": "do something", "tool_output": "result", "step": 1}
        result = sanitize_data(data)
        assert "prompt" not in result
        assert "tool_output" not in result
        assert result["step"] == 1

    def test_nested_secret_value_redacted(self) -> None:
        data = {"config": {"endpoint": "https://api.example.com", "secret": "sk-key"}}
        result = sanitize_data(data)
        assert "secret" not in result.get("config", {})
        assert result["config"]["endpoint"] == "https://api.example.com"

    def test_non_dict_input_returns_empty_dict(self) -> None:
        assert sanitize_data("not-a-dict") == {}  # type: ignore[arg-type]
        assert sanitize_data(None) == {}  # type: ignore[arg-type]
        assert sanitize_data([1, 2]) == {}  # type: ignore[arg-type]

    def test_empty_dict_returns_empty_dict(self) -> None:
        assert sanitize_data({}) == {}


class TestAppendEvent:
    """Verify append_event writes valid JSONL with sanitized data."""

    def test_creates_events_file(self, tmp_path: Path) -> None:
        append_event(tmp_path, "readiness", {"status": "ready"})
        events_file = tmp_path / "events.jsonl"
        assert events_file.exists()

    def test_writes_valid_jsonl_record(self, tmp_path: Path) -> None:
        append_event(tmp_path, "execution", {"task": "build"})
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["version"] == 1
        assert record["type"] == "execution"
        assert isinstance(record["ts"], float)
        assert record["data"] == {"task": "build"}

    def test_unknown_event_type_becomes_unknown(self, tmp_path: Path) -> None:
        append_event(tmp_path, "not-a-real-type", {"x": 1})
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        record = json.loads(lines[-1])
        assert record["type"] == "unknown"

    def test_sanitizes_secrets_before_writing(self, tmp_path: Path) -> None:
        append_event(tmp_path, "usage", {"api_key": "sk-secret", "model": "gpt-4"})
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        record = json.loads(lines[-1])
        assert "api_key" not in record["data"]
        assert record["data"]["model"] == "gpt-4"

    def test_appends_multiple_events(self, tmp_path: Path) -> None:
        append_event(tmp_path, "readiness", {"a": 1})
        append_event(tmp_path, "packing", {"b": 2})
        append_event(tmp_path, "execution", {"c": 3})
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 3

    def test_handles_non_dict_data_gracefully(self, tmp_path: Path) -> None:
        append_event(tmp_path, "telemetry", "not-a-dict")  # type: ignore[arg-type]
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        record = json.loads(lines[-1])
        assert record["data"] == {}

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c"
        append_event(nested, "retry", {"attempt": 2})
        assert (nested / "events.jsonl").exists()


class TestReadEvents:
    """Verify read_events respects limits and returns sanitized records."""

    def test_returns_empty_when_no_file(self, tmp_path: Path) -> None:
        assert read_events(tmp_path) == []

    def test_reads_all_events_within_limit(self, tmp_path: Path) -> None:
        for i in range(5):
            append_event(tmp_path, "readiness", {"i": i})
        events = read_events(tmp_path, limit=10)
        assert len(events) == 5

    def test_respects_limit_parameter(self, tmp_path: Path) -> None:
        for i in range(10):
            append_event(tmp_path, "execution", {"i": i})
        events = read_events(tmp_path, limit=3)
        assert len(events) == 3
        # Should return the LAST 3 events (tail)
        assert events[0]["data"]["i"] == 7
        assert events[1]["data"]["i"] == 8
        assert events[2]["data"]["i"] == 9

    def test_caps_at_max_events(self, tmp_path: Path) -> None:
        for i in range(MAX_EVENTS + 20):
            append_event(tmp_path, "telemetry", {"i": i})
        events = read_events(tmp_path, limit=9999)
        assert len(events) == MAX_EVENTS

    def test_minimum_limit_is_one(self, tmp_path: Path) -> None:
        append_event(tmp_path, "readiness", {"x": 1})
        events = read_events(tmp_path, limit=0)
        assert len(events) == 1

    def test_invalid_limit_defaults_to_max(self, tmp_path: Path) -> None:
        for i in range(5):
            append_event(tmp_path, "usage", {"i": i})
        events = read_events(tmp_path, limit="invalid")  # type: ignore[arg-type]
        assert len(events) == 5

    def test_skips_malformed_json_lines(self, tmp_path: Path) -> None:
        events_file = tmp_path / "events.jsonl"
        events_file.write_text(
            '{"version":1,"type":"readiness","ts":1.0,"data":{"ok":true}}\n'
            "not-valid-json\n"
            '{"version":1,"type":"packing","ts":2.0,"data":{"step":1}}\n',
            encoding="utf-8",
        )
        events = read_events(tmp_path)
        assert len(events) == 2

    def test_skips_wrong_version_records(self, tmp_path: Path) -> None:
        events_file = tmp_path / "events.jsonl"
        events_file.write_text(
            '{"version":99,"type":"readiness","ts":1.0,"data":{}}\n'
            '{"version":1,"type":"packing","ts":2.0,"data":{"v":1}}\n',
            encoding="utf-8",
        )
        events = read_events(tmp_path)
        assert len(events) == 1
        assert events[0]["data"]["v"] == 1

    def test_re_sanitizes_data_on_read(self, tmp_path: Path) -> None:
        # Write a record that somehow contains a secret in data (simulating
        # older un-sanitized entries or external writes)
        events_file = tmp_path / "events.jsonl"
        record = {
            "version": 1,
            "type": "execution",
            "ts": time.time(),
            "data": {"password": "leaked!", "safe": "value"},
        }
        events_file.write_text(json.dumps(record) + "\n", encoding="utf-8")
        events = read_events(tmp_path)
        assert len(events) == 1
        assert "password" not in events[0]["data"]
        assert events[0]["data"]["safe"] == "value"

    def test_negative_limit_treated_as_one(self, tmp_path: Path) -> None:
        append_event(tmp_path, "readiness", {"x": 1})
        events = read_events(tmp_path, limit=-5)
        assert len(events) == 1
