"""Tests for event_log.py — append-only JSONL, sanitization, bounded reads."""

import json
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.event_log import (
    ALLOWED_EVENT_TYPES,
    EVENT_VERSION,
    MAX_DATA_FIELDS,
    MAX_EVENTS,
    MAX_LIST_ITEMS,
    MAX_STRING_CHARS,
    append_event,
    read_events,
    sanitize_data,
)


class TestSanitizeData:
    def test_drops_sensitive_keys(self):
        data = {"token": "secret", "safe": "ok", "api_key": "key123", "count": 5}
        cleaned = sanitize_data(data)
        assert "token" not in cleaned
        assert "api_key" not in cleaned
        assert cleaned["safe"] == "ok"
        assert cleaned["count"] == 5

    def test_drops_prompt_and_tool_output(self):
        for key in ("prompt", "tool_output", "tool_result", "messages", "stdout", "stderr", "transcript"):
            cleaned = sanitize_data({key: "leak", "ok": "yes"})
            assert key not in cleaned, f"key {key} not dropped"

    def test_redacts_secret_prefix_values(self):
        for prefix in ("ghp_", "gho_", "sk-", "Bearer ", "xoxb-"):
            cleaned = sanitize_data({"value": prefix + "secret123"})
            assert cleaned["value"] == "[redacted]"

    def test_truncates_long_strings(self):
        long = "x" * 1000
        cleaned = sanitize_data({"msg": long})
        assert len(cleaned["msg"]) == MAX_STRING_CHARS

    def test_bounds_data_fields(self):
        data = {f"key_{i}": i for i in range(100)}
        cleaned = sanitize_data(data)
        assert len(cleaned) <= MAX_DATA_FIELDS

    def test_bounds_list_items(self):
        cleaned = sanitize_data({"items": list(range(100))})
        assert len(cleaned["items"]) <= MAX_LIST_ITEMS

    def test_nested_sensitive_dropped(self):
        cleaned = sanitize_data({"outer": {"token": "bad", "ok": "yes"}})
        assert "token" not in cleaned["outer"]
        assert cleaned["outer"]["ok"] == "yes"

    def test_depth_limit(self):
        deep = {"a": {"b": {"c": {"d": "deep"}}}}
        cleaned = sanitize_data(deep)
        assert isinstance(cleaned, dict)

    def test_non_dict_like_values_stringified(self):
        cleaned = sanitize_data({"obj": object()})
        assert isinstance(cleaned["obj"], str)

    def test_preserves_safe_numerics(self):
        cleaned = sanitize_data({"count": 42, "ratio": 3.14, "flag": True, "none": None})
        assert cleaned["count"] == 42
        assert cleaned["ratio"] == 3.14
        assert cleaned["flag"] is True
        assert cleaned["none"] is None

    def test_case_insensitive_key_match(self):
        cleaned = sanitize_data({"TOKEN": "bad", "Api_Key": "bad"})
        assert "TOKEN" not in cleaned
        assert "Api_Key" not in cleaned

    def test_private_key_dropped(self):
        cleaned = sanitize_data({"private_key": "-----BEGIN"})
        assert "private_key" not in cleaned

    def test_authorization_dropped(self):
        cleaned = sanitize_data({"authorization": "Bearer token"})
        assert "authorization" not in cleaned


class TestAppendEvent:
    def test_basic_append(self, tmp_path):
        append_event(tmp_path, "execution", {"msg": "hello"})
        events = read_events(tmp_path)
        assert len(events) == 1
        assert events[0]["type"] == "execution"
        assert events[0]["version"] == EVENT_VERSION
        assert events[0]["data"]["msg"] == "hello"
        assert "ts" in events[0]

    def test_allowed_types_preserved(self, tmp_path):
        for etype in ("readiness", "execution", "lease", "telemetry"):
            append_event(tmp_path, etype, {"x": 1})
        events = read_events(tmp_path)
        types = {e["type"] for e in events}
        assert "readiness" in types
        assert "execution" in types

    def test_unknown_type_mapped(self, tmp_path):
        append_event(tmp_path, "not_a_real_type", {"x": 1})
        events = read_events(tmp_path)
        assert events[0]["type"] == "unknown"

    def test_sensitive_data_sanitized_on_append(self, tmp_path):
        append_event(tmp_path, "execution", {"token": "secret", "ok": "yes"})
        events = read_events(tmp_path)
        assert "token" not in events[0]["data"]
        assert events[0]["data"]["ok"] == "yes"

    def test_multiple_appends(self, tmp_path):
        for i in range(5):
            append_event(tmp_path, "usage", {"i": i})
        events = read_events(tmp_path)
        assert len(events) == 5

    def test_append_creates_directory(self, tmp_path):
        nested = tmp_path / "a" / "b"
        append_event(nested, "execution", {"x": 1})
        assert (nested / "events.jsonl").exists()

    def test_append_is_atomic_o_append(self, tmp_path):
        append_event(tmp_path, "execution", {"a": 1})
        append_event(tmp_path, "execution", {"a": 2})
        lines = (tmp_path / "events.jsonl").read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        for line in lines:
            json.loads(line)

    def test_non_dict_data_handled(self, tmp_path):
        append_event(tmp_path, "execution", "not a dict")  # type: ignore[arg-type]
        events = read_events(tmp_path)
        assert events[0]["data"] == {}

    def test_all_allowed_types(self, tmp_path):
        for etype in ALLOWED_EVENT_TYPES:
            append_event(tmp_path, etype, {"v": 1})
        events = read_events(tmp_path, limit=100)
        assert len(events) == len(ALLOWED_EVENT_TYPES)


class TestReadEvents:
    def test_missing_file_returns_empty(self, tmp_path):
        assert read_events(tmp_path) == []

    def test_limit_bounded(self, tmp_path):
        for i in range(10):
            append_event(tmp_path, "execution", {"i": i})
        events = read_events(tmp_path, limit=3)
        assert len(events) == 3
        assert events[-1]["data"]["i"] == 9

    def test_limit_capped_at_max(self, tmp_path):
        for i in range(5):
            append_event(tmp_path, "execution", {"i": i})
        events = read_events(tmp_path, limit=9999)
        assert len(events) == min(5, MAX_EVENTS)

    def test_limit_min_one(self, tmp_path):
        append_event(tmp_path, "execution", {"i": 0})
        events = read_events(tmp_path, limit=0)
        assert len(events) == 1

    def test_skips_corrupt_lines(self, tmp_path):
        append_event(tmp_path, "execution", {"ok": 1})
        path = tmp_path / "events.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write("not json\n")
        append_event(tmp_path, "execution", {"ok": 2})
        events = read_events(tmp_path)
        assert len(events) == 2

    def test_skips_wrong_version(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": 999, "type": "execution", "ts": time.time(), "data": {"x": 1}}) + "\n", encoding="utf-8")
        append_event(tmp_path, "execution", {"x": 2})
        events = read_events(tmp_path)
        assert len(events) == 1
        assert events[0]["data"]["x"] == 2

    def test_skips_non_dict_data(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": EVENT_VERSION, "type": "execution", "ts": time.time(), "data": "bad"}) + "\n", encoding="utf-8")
        assert read_events(tmp_path) == []

    def test_skips_non_string_type(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": EVENT_VERSION, "type": 123, "ts": time.time(), "data": {}}) + "\n", encoding="utf-8")
        assert read_events(tmp_path) == []

    def test_skips_non_dict_record(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps([1, 2, 3]) + "\n", encoding="utf-8")
        assert read_events(tmp_path) == []

    def test_data_re_sanitized_on_read(self, tmp_path):
        path = tmp_path / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"version": EVENT_VERSION, "type": "execution", "ts": time.time(), "data": {"token": "leak", "ok": "yes"}}) + "\n", encoding="utf-8")
        events = read_events(tmp_path)
        assert "token" not in events[0]["data"]

    def test_invalid_limit_falls_back(self, tmp_path):
        append_event(tmp_path, "execution", {"x": 1})
        events = read_events(tmp_path, limit="bad")  # type: ignore[arg-type]
        assert len(events) == 1

    def test_tail_behavior(self, tmp_path):
        for i in range(20):
            append_event(tmp_path, "execution", {"i": i})
        events = read_events(tmp_path, limit=5)
        assert events[0]["data"]["i"] == 15


class TestEventProvenance:
    def test_record_has_all_fields(self, tmp_path):
        append_event(tmp_path, "execution", {"detail": "test"})
        events = read_events(tmp_path)
        for key in ("version", "type", "ts", "data"):
            assert key in events[0]

    def test_timestamp_is_recent(self, tmp_path):
        before = time.time()
        append_event(tmp_path, "execution", {"x": 1})
        after = time.time()
        events = read_events(tmp_path)
        assert before <= events[0]["ts"] <= after

    def test_type_preserved(self, tmp_path):
        append_event(tmp_path, "discovery-trigger", {"x": 1})
        assert read_events(tmp_path)[0]["type"] == "discovery-trigger"
