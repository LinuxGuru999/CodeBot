"""Tests for event_log.py secret sanitization and event persistence."""

import json
import os
import tempfile
from pathlib import Path

import pytest

from codebot.event_log import (
    MAX_EVENTS,
    EVENT_VERSION,
    _looks_secret,
    _clean_value,
    sanitize_data,
    append_event,
    read_events,
)


class TestLooksSecret:
    """Test _looks_secret pattern matching for common secret prefixes."""

    def test_detects_github_personal_access_token(self):
        assert _looks_secret("ghp_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") is True

    def test_detects_github_oauth_token(self):
        assert _looks_secret("gho_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") is True

    def test_detects_stripe_api_key(self):
        assert _looks_secret("sk_live_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx") is True

    def test_detects_bearer_token_with_space(self):
        assert _looks_secret("Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9") is True

    def test_detects_slack_bot_token(self):
        assert _looks_secret("xoxb-123456789012-1234567890123-abcdefghijklmnopqrstuvwx") is True

    def test_returns_false_for_normal_string(self):
        assert _looks_secret("hello world") is False

    def test_returns_false_for_empty_string(self):
        assert _looks_secret("") is False

    def test_returns_false_for_non_matching_prefix(self):
        assert _looks_secret("not-a-secret-123") is False


class TestCleanValue:
    """Test _clean_value recursive sanitization."""

    def test_redacts_secret_string_values(self):
        result = _clean_value({"key": "ghp_secrettoken"})
        assert result == {"key": "[redacted]"}

    def test_preserves_non_secret_strings(self):
        result = _clean_value({"key": "normal value"})
        assert result == {"key": "normal value"}

    def test_truncates_long_strings(self):
        long_str = "a" * 300
        result = _clean_value({"key": long_str})
        assert len(result["key"]) <= 256

    def test_recursively_cleans_nested_dicts(self):
        data = {
            "outer": {
                "inner": "ghp_secret",
                "safe": "value"
            }
        }
        result = _clean_value(data)
        assert result == {"outer": {"inner": "[redacted]", "safe": "value"}}

    def test_recursively_cleans_lists(self):
        data = {"items": ["ghp_secret", "safe", "sk-another"]}
        result = _clean_value(data)
        assert result == {"items": ["[redacted]", "safe", "[redacted]"]}

    def test_limits_list_items(self):
        long_list = list(range(30))
        result = _clean_value({"items": long_list})
        assert len(result["items"]) == 20

    def test_preserves_primitives(self):
        data = {"int": 42, "float": 3.14, "bool": True, "none": None}
        result = _clean_value(data)
        assert result == data

    def test_limits_depth_to_prevent_infinite_recursion(self):
        deep = {"l1": {"l2": {"l3": {"l4": "ghp_secret"}}}}
        result = _clean_value(deep)
        # At depth > 2, values are converted to string and truncated
        assert isinstance(result["l1"]["l2"]["l3"], str)

    def test_removes_sensitive_keys(self):
        data = {
            "api_key": "should_be_removed",
            "password": "also_removed",
            "safe_field": "kept"
        }
        result = _clean_value(data)
        assert "api_key" not in result
        assert "password" not in result
        assert result["safe_field"] == "kept"


class TestSanitizeData:
    """Test sanitize_data with nested structures."""

    def test_removes_sensitive_keys_and_redacts_values(self):
        data = {
            "user": "alice",
            "token": "ghp_xxx",
            "config": {
                "api_key": "sk_xxx",
                "timeout": 30
            }
        }
        result = sanitize_data(data)
        assert "token" not in result
        assert "api_key" not in result["config"]
        assert result["user"] == "alice"
        assert result["config"]["timeout"] == 30

    def test_preserves_non_secret_fields(self):
        data = {"message": "hello", "count": 5}
        result = sanitize_data(data)
        assert result == data

    def test_returns_empty_dict_for_non_dict_input(self):
        assert sanitize_data("string") == {}
        assert sanitize_data([1, 2, 3]) == {}
        assert sanitize_data(None) == {}


class TestAppendEvent:
    """Test append_event file I/O."""

    def test_writes_valid_jsonl_record(self, tmp_path):
        append_event(tmp_path, "usage", {"action": "test"})
        event_file = tmp_path / "events.jsonl"
        assert event_file.exists()
        line = event_file.read_text().strip()
        record = json.loads(line)
        assert record["version"] == EVENT_VERSION
        assert record["type"] == "usage"
        assert "ts" in record
        assert record["data"]["action"] == "test"

    def test_sanitizes_secrets_before_writing(self, tmp_path):
        append_event(tmp_path, "execution", {"token": "ghp_secret"})
        event_file = tmp_path / "events.jsonl"
        line = event_file.read_text().strip()
        record = json.loads(line)
        assert "token" not in record["data"]

    def test_creates_state_dir_if_missing(self, tmp_path):
        nested = tmp_path / "sub" / "dir"
        append_event(nested, "readiness", {})
        assert (nested / "events.jsonl").exists()

    def test_appends_multiple_events(self, tmp_path):
        append_event(tmp_path, "usage", {"id": 1})
        append_event(tmp_path, "execution", {"id": 2})
        lines = (tmp_path / "events.jsonl").read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["type"] == "usage"
        assert json.loads(lines[1])["type"] == "execution"


class TestReadEvents:
    """Test read_events with limit enforcement."""

    def test_returns_empty_list_when_no_events(self, tmp_path):
        assert read_events(tmp_path) == []

    def test_returns_events_in_order(self, tmp_path):
        append_event(tmp_path, "usage", {"n": 1})
        append_event(tmp_path, "execution", {"n": 2})
        events = read_events(tmp_path)
        assert len(events) == 2
        assert events[0]["type"] == "usage"
        assert events[1]["type"] == "execution"

    def test_respects_max_events_limit(self, tmp_path):
        for i in range(MAX_EVENTS + 10):
            append_event(tmp_path, "event", {"i": i})
        events = read_events(tmp_path)
        assert len(events) == MAX_EVENTS
        # Should return the last MAX_EVENTS records
        assert events[0]["data"]["i"] == 10

    def test_respects_custom_limit(self, tmp_path):
        for i in range(10):
            append_event(tmp_path, "event", {"i": i})
        events = read_events(tmp_path, limit=3)
        assert len(events) == 3
        assert events[-1]["data"]["i"] == 9

    def test_skips_invalid_json_lines(self, tmp_path):
        event_file = tmp_path / "events.jsonl"
        event_file.write_text("invalid json\n")
        append_event(tmp_path, "usage", {"ok": True})
        events = read_events(tmp_path)
        assert len(events) == 1
        assert events[0]["type"] == "usage"

    def test_sanitizes_data_on_read(self, tmp_path):
        # Write raw event with secret (simulating direct file write bypassing append_event)
        event_file = tmp_path / "events.jsonl"
        record = {
            "version": EVENT_VERSION,
            "type": "telemetry",
            "ts": 1234567890.0,
            "data": {"token": "ghp_leaked"}
        }
        event_file.write_text(json.dumps(record) + "\n")
        events = read_events(tmp_path)
        assert len(events) == 1
        assert "token" not in events[0]["data"]
