"""Tests for readiness, batch_scheduler, manifest_schema, and lease_state.

Covers the 4 modules copied from Monitor bots/ into codebot/ with fixed imports.
Focuses on pure-function APIs that don't require running infrastructure.
"""

import json
import time
from pathlib import Path

import pytest

from codebot.lease_state import acquire, renew, release, fail, dead_letters, retry_dead_letter
from codebot.batch_scheduler import extract_tier_from_tags, needs_approval, filter_approved, order_by_tier
from codebot.readiness import is_due, noop_ok, queue_has_work, load_approved_ids, filter_unapproved_items


class TestLeaseState:
    def test_acquire_new_lease(self, tmp_path):
        result = acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60)
        assert result["status"] == "acquired"
        assert result["attempt"] == 1

    def test_acquire_busy_lease(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60)
        result = acquire(tmp_path, "item-1", "worker-b", now=1030.0, lease_seconds=60)
        assert result["status"] == "busy"
        assert result["owner"] == "worker-a"

    def test_acquire_expired_lease(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60)
        result = acquire(tmp_path, "item-1", "worker-b", now=1061.0, lease_seconds=60)
        assert result["status"] == "acquired"
        assert result["attempt"] == 2

    def test_acquire_dead_letter_after_max_attempts(self, tmp_path):
        for i in range(3):
            acquire(tmp_path, "item-1", f"w-{i}", now=1000.0 + i * 100, lease_seconds=10)
        result = acquire(tmp_path, "item-1", "w-final", now=2000.0, lease_seconds=10)
        assert result["status"] == "dead-letter"

    def test_renew_valid_owner(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60)
        result = renew(tmp_path, "item-1", "worker-a", now=1030.0, lease_seconds=60)
        assert result["status"] == "renewed"

    def test_renew_wrong_owner(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60)
        result = renew(tmp_path, "item-1", "worker-b", now=1030.0, lease_seconds=60)
        assert result["status"] == "not-owner"

    def test_release_valid_owner(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60)
        result = release(tmp_path, "item-1", "worker-a")
        assert result["status"] == "released"
        acquired = acquire(tmp_path, "item-1", "worker-b", now=1001.0, lease_seconds=60)
        assert acquired["status"] == "acquired"

    def test_release_wrong_owner(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60)
        result = release(tmp_path, "item-1", "worker-b")
        assert result["status"] == "not-owner"

    def test_fail_retry_below_max(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60)
        result = fail(tmp_path, "item-1", "worker-a", reason="error", max_attempts=3)
        assert result["status"] == "retry"

    def test_fail_dead_letter_at_max(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60, max_attempts=1)
        result = fail(tmp_path, "item-1", "worker-a", reason="error", max_attempts=1)
        assert result["status"] == "dead-letter"
        dl = dead_letters(tmp_path)
        assert len(dl) == 1
        assert dl[0]["id"] == "item-1"

    def test_retry_dead_letter(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60, max_attempts=1)
        fail(tmp_path, "item-1", "worker-a", reason="error", max_attempts=1)
        result = retry_dead_letter(tmp_path, "item-1")
        assert result["status"] == "retried"
        assert dead_letters(tmp_path) == []

    def test_retry_nonexistent_dead_letter(self, tmp_path):
        result = retry_dead_letter(tmp_path, "no-such-item")
        assert result["status"] == "not-found"

    def test_persistence_across_calls(self, tmp_path):
        acquire(tmp_path, "item-1", "worker-a", now=1000.0, lease_seconds=60)
        state_file = tmp_path / "leases.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert "item-1" in state["leases"]


class TestBatchScheduler:
    def test_extract_tier_valid(self):
        assert extract_tier_from_tags(["T4", "security"]) == "T4"
        assert extract_tier_from_tags(["T1"]) == "T1"
        assert extract_tier_from_tags(["T11"]) == "T11"

    def test_extract_tier_invalid(self):
        assert extract_tier_from_tags(["T99"]) is None
        assert extract_tier_from_tags(["security"]) is None
        assert extract_tier_from_tags([]) is None
        assert extract_tier_from_tags(None) is None

    def test_needs_approval_high_tier(self):
        assert needs_approval({"source_tier": "T4"}) is True
        assert needs_approval({"tags": ["T5"]}) is True

    def test_needs_approval_critical_complexity(self):
        assert needs_approval({"complexity": "critical"}) is True

    def test_needs_approval_low_tier(self):
        assert needs_approval({"source_tier": "T1"}) is False
        assert needs_approval({"tags": ["T2"]}) is False
        assert needs_approval({}) is False

    def test_filter_approved_tags_scrutiny(self):
        items = [
            {"id": "a", "source_tier": "T1"},
            {"id": "b", "source_tier": "T4"},
        ]
        approved, blocked = filter_approved(items)
        assert len(approved) == 2
        assert len(blocked) == 0
        assert approved[1].get("scrutiny") is True
        assert "scrutiny" not in approved[0]

    def test_order_by_tier(self):
        names = ["bot-c", "bot-a", "bot-b"]
        tier_map = {"bot-a": 1, "bot-b": 2, "bot-c": 1}
        next_run = {"bot-a": 100.0, "bot-b": 50.0, "bot-c": 200.0}
        result = order_by_tier(names, tier_map, next_run)
        assert result[0] == "bot-a"
        assert result[1] == "bot-c"
        assert result[2] == "bot-b"


class TestReadiness:
    def test_is_due_no_next_run(self):
        assert is_due({}, 1000.0, None) is True

    def test_is_due_zero_next_run(self):
        assert is_due({}, 1000.0, 0.0) is True

    def test_is_due_future(self):
        assert is_due({}, 1000.0, 2000.0) is False

    def test_is_due_past(self):
        assert is_due({}, 2000.0, 1000.0) is True

    def test_is_due_invalid_type(self):
        assert is_due({}, 1000.0, "invalid") is True

    def test_noop_ok_no_cap(self):
        assert noop_ok({}, 5) is True

    def test_noop_ok_below_cap(self):
        assert noop_ok({"noop_cap": 3}, 2) is True

    def test_noop_ok_at_cap(self):
        assert noop_ok({"noop_cap": 3}, 3) is False

    def test_noop_ok_above_cap(self):
        assert noop_ok({"noop_cap": 3}, 10) is False

    def test_noop_ok_invalid_counter(self):
        assert noop_ok({"noop_cap": 3}, None) is True

    def test_queue_has_work_empty(self):
        assert queue_has_work("scan", None, "") is False
        assert queue_has_work("scan", None, None) is False

    def test_queue_has_work_with_items(self):
        text = "| ID | Source | Title | Complexity | Status | Owner | Notes |\n|---|---|---|---|---|---|---|\n| Q-001 | bot | Fix bug | small | confirmed | | |\n"
        assert queue_has_work("scan", None, text) is True

    def test_queue_has_work_filtered(self):
        text = "| ID | Source | Title | Complexity | Status | Owner | Notes |\n|---|---|---|---|---|---|---|\n| Q-001 | bot | Fix bug | small | confirmed | | |\n"
        assert queue_has_work("scan", ["small"], text) is True
        assert queue_has_work("scan", ["critical"], text) is False

    def test_load_approved_ids_missing_dir(self, tmp_path):
        result = load_approved_ids(str(tmp_path / "nonexistent"))
        assert isinstance(result, set)

    def test_load_approved_ids_with_file(self, tmp_path):
        approved_file = tmp_path / "approved_ids.json"
        approved_file.write_text(json.dumps(["T-001", "T-002"]))
        result = load_approved_ids(str(tmp_path))
        assert "T-001" in result
        assert "T-002" in result

    def test_filter_unapproved_items(self):
        text = "| ID | Title |\n|---|---|\n| T-001 | Fix |\n| T-002 | Add |\n"
        result = filter_unapproved_items(text, {"T-001"})
        assert "T-001" in result


def _valid_manifest(**overrides) -> dict:
    base = {
        "name": "test_bot",
        "kind": "scan",
        "prompt_file": "TEST_BOT.md",
        "model": "default",
        "runner": "api",
        "enabled": True,
        "interval_seconds": 300,
        "heartbeat_timeout": 600,
        "tier_priority": 1,
        "max_restarts": 3,
        "clean_exit_wait": True,
        "session_timeout": 1800,
        "input": [{"path": "QUEUE.md", "type": "queue"}],
        "output": [{"path": "logs/test.log", "type": "log"}],
        "noop_cap": 0,
    }
    base.update(overrides)
    return base


class TestManifestSchema:
    def test_validate_manifest_minimal_valid(self):
        from codebot.manifest_schema import validate_manifest
        ok, errors = validate_manifest(_valid_manifest())
        assert ok is True
        assert errors == []

    def test_validate_manifest_missing_name(self):
        from codebot.manifest_schema import validate_manifest
        m = _valid_manifest()
        del m["name"]
        ok, errors = validate_manifest(m)
        assert ok is False
        assert any("name" in e.lower() for e in errors)

    def test_validate_manifest_invalid_type(self):
        from codebot.manifest_schema import validate_manifest
        ok, errors = validate_manifest("not a dict")
        assert ok is False

    def test_validate_manifest_bad_kind(self):
        from codebot.manifest_schema import validate_manifest
        ok, errors = validate_manifest(_valid_manifest(kind="invalid"))
        assert ok is False
        assert any("kind" in e.lower() for e in errors)

    def test_load_manifest_from_file(self, tmp_path):
        from codebot.manifest_schema import load_manifest
        manifest_file = tmp_path / "test.json"
        manifest_file.write_text(json.dumps(_valid_manifest()))
        result = load_manifest(manifest_file)
        assert result["name"] == "test_bot"

    def test_load_all_manifests_empty_dir(self, tmp_path):
        from codebot.manifest_schema import load_all_manifests
        result = load_all_manifests(tmp_path)
        assert isinstance(result, dict)
        assert len(result) == 0
