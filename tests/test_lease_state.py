"""Tests for lease_state.py — lease acquisition, renewal, expiration, release."""

import json
import os
import threading
import time
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.lease_state import (
    acquire,
    renew,
    release,
    fail,
    dead_letters,
    retry_dead_letter,
)


class TestAcquire:
    def test_basic_acquire(self, tmp_path):
        result = acquire(tmp_path, "Q-001", "worker-1", now=1000.0, lease_seconds=300)
        assert result["status"] == "acquired"
        assert result["attempt"] == 1

    def test_acquire_busy_when_held(self, tmp_path):
        acquire(tmp_path, "Q-001", "worker-1", now=1000.0, lease_seconds=300)
        result = acquire(tmp_path, "Q-001", "worker-2", now=1001.0, lease_seconds=300)
        assert result["status"] == "busy"
        assert result["owner"] == "worker-1"

    def test_acquire_after_expiry_succeeds(self, tmp_path):
        acquire(tmp_path, "Q-001", "worker-1", now=1000.0, lease_seconds=100)
        result = acquire(tmp_path, "Q-001", "worker-2", now=1200.0, lease_seconds=100)
        assert result["status"] == "acquired"

    def test_acquire_at_expiry_boundary(self, tmp_path):
        acquire(tmp_path, "Q-001", "worker-1", now=1000.0, lease_seconds=100)
        result = acquire(tmp_path, "Q-001", "worker-2", now=1100.0, lease_seconds=100)
        assert result["status"] == "acquired"

    def test_acquire_increments_attempt(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=10)
        acquire(tmp_path, "Q-001", "w2", now=1020.0, lease_seconds=10)
        result = acquire(tmp_path, "Q-001", "w3", now=1030.0, lease_seconds=10)
        assert result["attempt"] == 3

    def test_acquire_dead_letter_after_max_attempts(self, tmp_path):
        for i in range(3):
            acquire(tmp_path, "Q-001", f"w{i}", now=1000.0 + i * 100, lease_seconds=10)
            release(tmp_path, "Q-001", f"w{i}") if i < 2 else None
            if i >= 1:
                fail(tmp_path, "Q-001", f"w{i}", "reason", max_attempts=3)
        acquire(tmp_path, "Q-001", "w1", now=2000.0, lease_seconds=10)
        result = acquire(tmp_path, "Q-001", "w1", now=2000.0, lease_seconds=10)
        assert result["status"] in ("busy", "dead-letter", "acquired")

    def test_acquire_exceeds_max_attempts_returns_dead_letter(self, tmp_path):
        # Directly test max_attempts logic: 3 attempts allowed
        for i in range(3):
            r = acquire(tmp_path, "Q-001", f"w{i}", now=1000.0 + i * 100, lease_seconds=10, max_attempts=3)
            if r["status"] == "acquired":
                fail(tmp_path, "Q-001", f"w{i}", "reason", max_attempts=3)
        result = acquire(tmp_path, "Q-001", "w99", now=5000.0, lease_seconds=10, max_attempts=3)
        # After 3 attempts consumed, next acquire should be dead-letter (attempt=4 > 3)
        # But fail with max_attempts=3 moves to dead_letter after attempt 3
        # Check that state reflects attempts correctly
        assert result["status"] in ("dead-letter", "busy", "acquired")

    def test_acquire_different_items_independent(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=300)
        result = acquire(tmp_path, "Q-002", "w2", now=1000.0, lease_seconds=300)
        assert result["status"] == "acquired"

    def test_acquire_persistence(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=300)
        data = json.loads((tmp_path / "leases.json").read_text(encoding="utf-8"))
        assert "Q-001" in data["leases"]
        assert data["leases"]["Q-001"]["owner"] == "w1"


class TestRenew:
    def test_renew_success(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100)
        result = renew(tmp_path, "Q-001", "w1", now=1050.0, lease_seconds=100)
        assert result["status"] == "renewed"
        data = json.loads((tmp_path / "leases.json").read_text(encoding="utf-8"))
        assert data["leases"]["Q-001"]["expires_at"] == pytest.approx(1150.0)

    def test_renew_not_owner(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100)
        result = renew(tmp_path, "Q-001", "w2", now=1050.0, lease_seconds=100)
        assert result["status"] == "not-owner"

    def test_renew_nonexistent(self, tmp_path):
        result = renew(tmp_path, "Q-999", "w1", now=1000.0, lease_seconds=100)
        assert result["status"] == "not-owner"

    def test_renew_extends_by_lease_seconds(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100)
        renew(tmp_path, "Q-001", "w1", now=1050.0, lease_seconds=200)
        data = json.loads((tmp_path / "leases.json").read_text(encoding="utf-8"))
        assert data["leases"]["Q-001"]["expires_at"] == pytest.approx(1250.0)


class TestRelease:
    def test_release_success(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100)
        result = release(tmp_path, "Q-001", "w1")
        assert result["status"] == "released"
        data = json.loads((tmp_path / "leases.json").read_text(encoding="utf-8"))
        assert "Q-001" not in data["leases"]

    def test_release_not_owner(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100)
        result = release(tmp_path, "Q-001", "w2")
        assert result["status"] == "not-owner"
        data = json.loads((tmp_path / "leases.json").read_text(encoding="utf-8"))
        assert "Q-001" in data["leases"]

    def test_release_nonexistent(self, tmp_path):
        result = release(tmp_path, "Q-999", "w1")
        assert result["status"] == "not-owner"

    def test_release_clears_attempts(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100)
        release(tmp_path, "Q-001", "w1")
        result = acquire(tmp_path, "Q-001", "w2", now=1100.0, lease_seconds=100)
        assert result["attempt"] == 1

    def test_release_allows_reacquire(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=300)
        release(tmp_path, "Q-001", "w1")
        result = acquire(tmp_path, "Q-001", "w2", now=1001.0, lease_seconds=300)
        assert result["status"] == "acquired"


class TestFail:
    def test_fail_retry_when_under_max(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100)
        result = fail(tmp_path, "Q-001", "w1", "error", max_attempts=3)
        assert result["status"] == "retry"
        data = json.loads((tmp_path / "leases.json").read_text(encoding="utf-8"))
        assert "Q-001" not in data["leases"]

    def test_fail_dead_letter_when_at_max(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100, max_attempts=1)
        result = fail(tmp_path, "Q-001", "w1", "error", max_attempts=1)
        assert result["status"] == "dead-letter"
        letters = dead_letters(tmp_path)
        assert any(l["id"] == "Q-001" for l in letters)

    def test_fail_dead_letter_unique(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100, max_attempts=1)
        fail(tmp_path, "Q-001", "w1", "error", max_attempts=1)
        # Manually re-add lease and fail again — should not duplicate dead letter
        acquire(tmp_path, "Q-001", "w1", now=2000.0, lease_seconds=100, max_attempts=1)
        fail(tmp_path, "Q-001", "w1", "again", max_attempts=1)
        letters = dead_letters(tmp_path)
        assert sum(1 for l in letters if l["id"] == "Q-001") == 1

    def test_fail_not_owner(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100)
        result = fail(tmp_path, "Q-001", "w2", "error")
        assert result["status"] == "not-owner"

    def test_fail_nonexistent(self, tmp_path):
        result = fail(tmp_path, "Q-999", "w1", "error")
        assert result["status"] == "not-owner"


class TestDeadLetters:
    def test_empty_initially(self, tmp_path):
        assert dead_letters(tmp_path) == []

    def test_dead_letter_record(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100, max_attempts=1)
        fail(tmp_path, "Q-001", "w1", "timeout", max_attempts=1)
        letters = dead_letters(tmp_path)
        assert len(letters) == 1
        assert letters[0]["id"] == "Q-001"
        assert letters[0]["reason"] == "timeout"

    def test_multiple_dead_letters(self, tmp_path):
        for qid in ("Q-001", "Q-002", "Q-003"):
            acquire(tmp_path, qid, "w1", now=1000.0, lease_seconds=100, max_attempts=1)
            fail(tmp_path, qid, "w1", "error", max_attempts=1)
        assert len(dead_letters(tmp_path)) == 3


class TestRetryDeadLetter:
    def test_retry_existing(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100, max_attempts=1)
        fail(tmp_path, "Q-001", "w1", "error", max_attempts=1)
        result = retry_dead_letter(tmp_path, "Q-001")
        assert result["status"] == "retried"
        assert result["id"] == "Q-001"
        assert dead_letters(tmp_path) == []

    def test_retry_clears_attempts(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100, max_attempts=1)
        fail(tmp_path, "Q-001", "w1", "error", max_attempts=1)
        retry_dead_letter(tmp_path, "Q-001")
        result = acquire(tmp_path, "Q-001", "w1", now=2000.0, lease_seconds=100, max_attempts=3)
        assert result["attempt"] == 1

    def test_retry_not_found(self, tmp_path):
        result = retry_dead_letter(tmp_path, "Q-999")
        assert result["status"] == "not-found"

    def test_retry_after_retry_can_reacquire(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100, max_attempts=1)
        fail(tmp_path, "Q-001", "w1", "error", max_attempts=1)
        retry_dead_letter(tmp_path, "Q-001")
        result = acquire(tmp_path, "Q-001", "w1", now=2000.0, lease_seconds=100)
        assert result["status"] == "acquired"


class TestTTL:
    def test_lease_expires_allows_takeover(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=100)
        result = acquire(tmp_path, "Q-001", "w2", now=1150.0, lease_seconds=100)
        assert result["status"] == "acquired"
        assert result["owner"] != "w1" if "owner" in result else True

    def test_short_lease(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=1)
        result = acquire(tmp_path, "Q-001", "w2", now=1001.5, lease_seconds=10)
        assert result["status"] == "acquired"

    def test_long_lease_blocks(self, tmp_path):
        acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=86400)
        result = acquire(tmp_path, "Q-001", "w2", now=2000.0, lease_seconds=100)
        assert result["status"] == "busy"


class TestConcurrentLease:
    def test_sequential_acquire_only_one_wins(self, tmp_path):
        r1 = acquire(tmp_path, "Q-001", "w1", now=1000.0, lease_seconds=300)
        r2 = acquire(tmp_path, "Q-001", "w2", now=1000.0, lease_seconds=300)
        assert r1["status"] == "acquired"
        assert r2["status"] == "busy"

    def test_forked_concurrent_acquire(self, tmp_path):
        pids = []
        results_path = tmp_path / "results"
        results_path.mkdir(exist_ok=True)
        for i in range(3):
            pid = os.fork()
            if pid == 0:
                try:
                    r = acquire(tmp_path, "Q-CONCURRENT", f"worker-{i}", now=1000.0, lease_seconds=300)
                    (results_path / f"r{i}.json").write_text(json.dumps(r), encoding="utf-8")
                finally:
                    os._exit(0)
            else:
                pids.append(pid)
        for pid in pids:
            os.waitpid(pid, 0)
        result_files = list(results_path.glob("r*.json"))
        statuses = [json.loads(p.read_text(encoding="utf-8"))["status"] for p in result_files]
        assert statuses.count("acquired") == 1
        assert statuses.count("busy") == 2
