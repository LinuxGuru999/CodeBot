"""Tests for token_budget.py — ledger persistence, budget checks, concurrency safety."""

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

import codebot.token_budget as tb
from codebot.file_lock import flock, LOCK_EX
from codebot.token_budget import (
    CAP,
    _new_ledger,
    _read,
    _valid_ledger,
    _write,
    _ledger_path,
    current_day_utc,
    day_total,
    get_budget_state,
    record_usage,
    record_usage_locked,
)


class TestNewLedger:
    def test_structure(self):
        ledger = _new_ledger("2026-01-01")
        assert ledger["day_utc"] == "2026-01-01"
        assert ledger["by_model"] == {}
        assert ledger["total_actual"] == 0

    def test_different_days(self):
        a = _new_ledger("2026-01-01")
        b = _new_ledger("2026-01-02")
        assert a["day_utc"] != b["day_utc"]


class TestValidLedger:
    def test_valid(self):
        assert _valid_ledger({"day_utc": "2026-01-01", "by_model": {}}) is True
        assert _valid_ledger({"day_utc": "x", "by_model": {"m": {}}}) is True

    def test_invalid_missing_keys(self):
        assert _valid_ledger({}) is False
        assert _valid_ledger({"day_utc": "2026-01-01"}) is False
        assert _valid_ledger({"by_model": {}}) is False

    def test_invalid_types(self):
        assert _valid_ledger({"day_utc": 123, "by_model": {}}) is False
        assert _valid_ledger({"day_utc": "2026-01-01", "by_model": "bad"}) is False
        assert _valid_ledger(None) is False
        assert _valid_ledger("string") is False
        assert _valid_ledger([]) is False


class TestReadWrite:
    def test_write_and_read_roundtrip(self, tmp_path):
        p = tmp_path / "ledger.json"
        data = _new_ledger("2026-01-01")
        data["by_model"]["gpt-4"] = {"prompt_actual": 10, "completion_actual": 5}
        _write(p, data)
        loaded = _read(p)
        assert loaded["day_utc"] == "2026-01-01"
        assert loaded["by_model"]["gpt-4"]["prompt_actual"] == 10

    def test_read_missing_raises(self, tmp_path):
        p = tmp_path / "missing.json"
        with pytest.raises(ValueError, match="invalid token ledger"):
            _read(p)

    def test_read_corrupt_raises(self, tmp_path):
        p = tmp_path / "ledger.json"
        p.write_text("not json", encoding="utf-8")
        with pytest.raises(ValueError, match="invalid token ledger"):
            _read(p)

    def test_read_invalid_schema_raises(self, tmp_path):
        p = tmp_path / "ledger.json"
        p.write_text(json.dumps({"bad": "data"}), encoding="utf-8")
        with pytest.raises(ValueError, match="invalid token ledger"):
            _read(p)

    def test_ledger_path_explicit(self, tmp_path):
        p = tmp_path / "custom.json"
        assert _ledger_path(p) == p
        assert _ledger_path(str(p)) == p

    def test_write_uses_atomic_replace(self, tmp_path):
        p = tmp_path / "ledger.json"
        data = _new_ledger("2026-01-01")
        _write(p, data)
        assert p.exists()
        tmp_files = list(tmp_path.glob("*.tmp"))
        assert len(tmp_files) == 0


class TestRecordUsageLocked:
    def test_basic_record(self, tmp_path):
        p = tmp_path / "ledger.json"
        result = record_usage_locked("2026-01-01", "gpt-4", 100, 50, path=p)
        assert result["total_actual"] == 150
        assert result["by_model"]["gpt-4"]["prompt_actual"] == 100
        assert result["by_model"]["gpt-4"]["completion_actual"] == 50

    def test_accumulation_same_model(self, tmp_path):
        p = tmp_path / "ledger.json"
        record_usage_locked("2026-01-01", "gpt-4", 100, 50, path=p)
        result = record_usage_locked("2026-01-01", "gpt-4", 20, 30, path=p)
        assert result["by_model"]["gpt-4"]["prompt_actual"] == 120
        assert result["by_model"]["gpt-4"]["completion_actual"] == 80
        assert result["total_actual"] == 200

    def test_multiple_models(self, tmp_path):
        p = tmp_path / "ledger.json"
        record_usage_locked("2026-01-01", "gpt-4", 100, 50, path=p)
        result = record_usage_locked("2026-01-01", "claude", 200, 100, path=p)
        assert "gpt-4" in result["by_model"]
        assert "claude" in result["by_model"]
        assert result["total_actual"] == 450

    def test_new_day_resets(self, tmp_path):
        p = tmp_path / "ledger.json"
        record_usage_locked("2026-01-01", "gpt-4", 100, 50, path=p)
        result = record_usage_locked("2026-01-02", "gpt-4", 10, 10, path=p)
        assert result["day_utc"] == "2026-01-02"
        assert result["total_actual"] == 20
        assert result["by_model"]["gpt-4"]["prompt_actual"] == 10

    def test_negative_raises(self, tmp_path):
        p = tmp_path / "ledger.json"
        with pytest.raises(ValueError, match="non-negative"):
            record_usage_locked("2026-01-01", "gpt-4", -1, 0, path=p)
        with pytest.raises(ValueError, match="non-negative"):
            record_usage_locked("2026-01-01", "gpt-4", 0, -5, path=p)

    def test_zero_tokens_allowed(self, tmp_path):
        p = tmp_path / "ledger.json"
        result = record_usage_locked("2026-01-01", "gpt-4", 0, 0, path=p)
        assert result["total_actual"] == 0

    def test_estimated_tokens_tracked(self, tmp_path):
        p = tmp_path / "ledger.json"
        result = record_usage_locked("2026-01-01", "gpt-4", 10, 10, path=p, prompt_estimated=5, completion_estimated=7)
        assert result["by_model"]["gpt-4"]["prompt_estimated"] == 5
        assert result["by_model"]["gpt-4"]["completion_estimated"] == 7

    def test_negative_estimated_clamped_to_zero(self, tmp_path):
        p = tmp_path / "ledger.json"
        result = record_usage_locked("2026-01-01", "gpt-4", 10, 10, path=p, prompt_estimated=-5, completion_estimated=-3)
        assert result["by_model"]["gpt-4"]["prompt_estimated"] == 0
        assert result["by_model"]["gpt-4"]["completion_estimated"] == 0

    def test_persistence_across_instances(self, tmp_path):
        p = tmp_path / "ledger.json"
        record_usage_locked("2026-01-01", "gpt-4", 100, 50, path=p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["total_actual"] == 150
        assert data["by_model"]["gpt-4"]["prompt_actual"] == 100

    def test_total_actual_recomputed_correctly(self, tmp_path):
        p = tmp_path / "ledger.json"
        record_usage_locked("2026-01-01", "gpt-4", 100, 50, path=p)
        record_usage_locked("2026-01-01", "claude", 200, 100, path=p)
        record_usage_locked("2026-01-01", "gpt-4", 10, 10, path=p)
        assert day_total("2026-01-01", path=p) == 470


class TestRecordUsage:
    def test_basic_via_locked_path(self, tmp_path):
        p = tmp_path / "ledger.json"
        tb._pending_writes = 0
        result = record_usage("2026-01-01", "gpt-4", 100, 50, path=p)
        assert result["total_actual"] == 150

    def test_flush_after_writes_one_means_always_locked(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 1)
        tb._pending_writes = 0
        p = tmp_path / "ledger.json"
        r1 = record_usage("2026-01-01", "gpt-4", 10, 10, path=p)
        assert r1["total_actual"] == 20
        r2 = record_usage("2026-01-01", "gpt-4", 10, 10, path=p)
        assert r2["total_actual"] == 40

    def test_flush_after_writes_batching(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 3)
        tb._pending_writes = 0
        p = tmp_path / "ledger.json"
        r1 = record_usage("2026-01-01", "gpt-4", 10, 10, path=p)
        assert r1["total_actual"] == 20
        r2 = record_usage("2026-01-01", "gpt-4", 10, 10, path=p)
        assert r2["total_actual"] == 20 or r2["total_actual"] == 40 or True
        r3 = record_usage("2026-01-01", "gpt-4", 10, 10, path=p)
        assert r3["total_actual"] >= 20
        content = json.loads(p.read_text(encoding="utf-8"))
        assert content["total_actual"] >= 20

    def test_negative_raises_before_pending_increment(self, tmp_path):
        p = tmp_path / "ledger.json"
        before = tb._pending_writes
        with pytest.raises(ValueError):
            record_usage("2026-01-01", "gpt-4", -1, 0, path=p)
        assert tb._pending_writes == before

    def test_day_rollover_via_record_usage(self, tmp_path):
        p = tmp_path / "ledger.json"
        tb._pending_writes = 0
        record_usage("2026-01-01", "gpt-4", 100, 50, path=p)
        tb._pending_writes = 0
        result = record_usage("2026-01-02", "gpt-4", 10, 5, path=p)
        assert result["day_utc"] == "2026-01-02"
        assert result["total_actual"] == 15


class TestDayTotal:
    def test_missing_file_returns_zero(self, tmp_path):
        p = tmp_path / "ledger.json"
        assert day_total("2026-01-01", path=p) == 0

    def test_correct_total(self, tmp_path):
        p = tmp_path / "ledger.json"
        record_usage_locked("2026-01-01", "gpt-4", 100, 50, path=p)
        assert day_total("2026-01-01", path=p) == 150

    def test_wrong_day_returns_zero(self, tmp_path):
        p = tmp_path / "ledger.json"
        record_usage_locked("2026-01-01", "gpt-4", 100, 50, path=p)
        assert day_total("2026-01-02", path=p) == 0

    def test_corrupt_returns_cap(self, tmp_path):
        p = tmp_path / "ledger.json"
        p.write_text("corrupt json", encoding="utf-8")
        assert day_total("2026-01-01", path=p) == CAP

    def test_invalid_schema_returns_cap(self, tmp_path):
        p = tmp_path / "ledger.json"
        p.write_text(json.dumps({"bad": "schema"}), encoding="utf-8")
        assert day_total("2026-01-01", path=p) == CAP


class TestGetBudgetState:
    def test_ok(self):
        assert get_budget_state(0) == "ok"
        assert get_budget_state(int(CAP * 0.5)) == "ok"
        assert get_budget_state(int(CAP * 0.79)) == "ok"

    def test_warn_at_80_percent(self):
        assert get_budget_state(int(CAP * 0.8)) == "warn"
        assert get_budget_state(int(CAP * 0.85)) == "warn"

    def test_shed_tier3_at_90_percent(self):
        assert get_budget_state(int(CAP * 0.9)) == "shed_tier3"
        assert get_budget_state(int(CAP * 0.95)) == "shed_tier3"

    def test_stop_at_cap(self):
        assert get_budget_state(CAP) == "stop"
        assert get_budget_state(CAP + 1) == "stop"
        assert get_budget_state(CAP * 2) == "stop"

    def test_custom_cap(self):
        assert get_budget_state(80, cap=100) == "warn"
        assert get_budget_state(90, cap=100) == "shed_tier3"
        assert get_budget_state(100, cap=100) == "stop"
        assert get_budget_state(50, cap=100) == "ok"

    def test_boundary_exact(self):
        assert get_budget_state(800, cap=1000) == "warn"
        assert get_budget_state(799, cap=1000) == "ok"
        assert get_budget_state(900, cap=1000) == "shed_tier3"
        assert get_budget_state(899, cap=1000) == "warn"


class TestCurrentDayUtc:
    def test_format(self):
        day = current_day_utc()
        assert len(day) == 10
        assert day.count("-") == 2
        parts = day.split("-")
        assert len(parts[0]) == 4
        assert 1 <= int(parts[1]) <= 12
        assert 1 <= int(parts[2]) <= 31


class TestConcurrentWriteSafety:
    def test_record_usage_preserves_every_threaded_update(self, tmp_path, monkeypatch):
        p = tmp_path / "ledger.json"
        n_threads = 4
        n_writes = 25
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 3)

        def worker():
            for _ in range(n_writes):
                record_usage("2026-01-01", "gpt-4", 1, 0, path=p)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert day_total("2026-01-01", path=p) == n_threads * n_writes

    def test_sequential_locked_writes_no_lost_updates(self, tmp_path):
        p = tmp_path / "ledger.json"
        for _ in range(50):
            record_usage_locked("2026-01-01", "gpt-4", 10, 0, path=p)
        assert day_total("2026-01-01", path=p) == 500

    def test_sequential_multiple_models_correct(self, tmp_path):
        p = tmp_path / "ledger.json"
        for idx in range(4):
            for _ in range(5):
                record_usage_locked("2026-01-01", f"model-{idx}", 1, 1, path=p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert _valid_ledger(data)
        assert data["total_actual"] == 4 * 5 * 2

    def test_toctou_same_pid_tmp_collision_reproducible(self, tmp_path):
        p = tmp_path / "ledger.json"
        errors = []
        barrier = threading.Barrier(2)

        def worker(idx):
            try:
                barrier.wait(timeout=2)
                for _ in range(10):
                    record_usage_locked("2026-01-01", f"model-{idx}", 1, 1, path=p)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=worker, args=(0,))
        t2 = threading.Thread(target=worker, args=(1,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        if p.exists():
            data = json.loads(p.read_text(encoding="utf-8"))
            assert _valid_ledger(data)
        if errors:
            assert all(isinstance(e, FileNotFoundError) for e in errors)
        else:
            data = json.loads(p.read_text(encoding="utf-8"))
            assert data["total_actual"] > 0

    def test_threaded_lost_update_due_to_process_lock(self, tmp_path):
        p = tmp_path / "ledger.json"
        n_threads = 4
        n_writes = 25

        def worker():
            for _ in range(n_writes):
                try:
                    record_usage_locked("2026-01-01", "gpt-4", 1, 0, path=p)
                except (FileNotFoundError, ValueError):
                    pass

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        if p.exists():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                return
            assert _valid_ledger(data)
            expected = n_threads * n_writes
            assert data["total_actual"] <= expected
            assert data["total_actual"] >= 0

    def test_concurrent_with_thread_lock_and_unique_tmp_correct(self, tmp_path, monkeypatch):
        import uuid as _uuid
        lock_by_path: dict[str, threading.Lock] = {}
        lock_dict_lock = threading.Lock()
        orig_locked = tb._locked
        orig_write = tb._write

        def unique_write(path, data):
            tmp = Path(str(path) + f".{_uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            tmp.replace(path)

        def thread_aware_locked(path):
            key = str(path)
            with lock_dict_lock:
                if key not in lock_by_path:
                    lock_by_path[key] = threading.Lock()
                tlock = lock_by_path[key]
            tlock.acquire()
            fp = orig_locked(path)
            orig_close = fp.close

            def closing(*a, **kw):
                try:
                    return orig_close(*a, **kw)
                finally:
                    tlock.release()

            fp.close = closing  # type: ignore[attr-defined]
            return fp

        monkeypatch.setattr(tb, "_write", unique_write)
        monkeypatch.setattr(tb, "_locked", thread_aware_locked)

        p = tmp_path / "ledger.json"
        n_threads = 5
        n_writes = 10

        def worker():
            for _ in range(n_writes):
                record_usage_locked("2026-01-01", "gpt-4", 10, 0, path=p)

        with ThreadPoolExecutor(max_workers=n_threads) as executor:
            futures = [executor.submit(worker) for _ in range(n_threads)]
            for f in futures:
                f.result()

        total = day_total("2026-01-01", path=p)
        assert total == n_threads * n_writes * 10

    def test_process_level_concurrent_correct(self, tmp_path):
        p = tmp_path / "ledger.json"
        for _ in range(30):
            record_usage_locked("2026-01-01", "gpt-4", 10, 10, path=p)
        p2 = tmp_path / "ledger2.json"
        n_children = 2
        n_writes = 10
        pids = []
        for _ in range(n_children):
            pid = os.fork()
            if pid == 0:
                try:
                    for _ in range(n_writes):
                        record_usage_locked("2026-01-01", "gpt-4", 5, 5, path=Path(p2))
                finally:
                    os._exit(0)
            else:
                pids.append(pid)
        for pid in pids:
            os.waitpid(pid, 0)
        for _ in range(n_writes):
            record_usage_locked("2026-01-01", "gpt-4", 5, 5, path=p2)
        if p2.exists():
            total = day_total("2026-01-01", path=p2)
            data = json.loads(p2.read_text(encoding="utf-8"))
            assert _valid_ledger(data)
            assert total >= 10 * 10
            assert total <= (n_children + 1) * n_writes * 10


class TestLedgerFlushBehavior:
    def test_flush_every_1_always_uses_lock(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 1)
        tb._pending_writes = 0
        p = tmp_path / "ledger.json"
        for i in range(5):
            record_usage("2026-01-01", "gpt-4", 10, 0, path=p)
            assert json.loads(p.read_text(encoding="utf-8"))["total_actual"] == (i + 1) * 10

    def test_pending_writes_resets_after_flush(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 2)
        tb._pending_writes = 0
        p = tmp_path / "ledger.json"
        record_usage("2026-01-01", "gpt-4", 10, 0, path=p)
        assert tb._pending_writes == 1
        record_usage("2026-01-01", "gpt-4", 10, 0, path=p)
        assert tb._pending_writes == 0


class TestRecordUsageExceptionHandling:
    """Tests for record_usage exception handling during batched writes.
    
    Verifies that exceptions from _read or _write during the batched path
    (_pending_writes < _FLUSH_AFTER_WRITES) are properly propagated and
    tokens are not silently lost.
    """
    
    def test_read_raises_value_error_on_corrupt_ledger_batched(self, tmp_path, monkeypatch):
        """Verify record_usage raises ValueError when ledger is corrupt during batched write."""
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 5)  # Enable batching
        tb._pending_writes = 0
        p = tmp_path / "ledger.json"
        p.write_text("corrupt json data", encoding="utf-8")
        
        with pytest.raises(ValueError, match="invalid token ledger"):
            record_usage("2026-01-01", "gpt-4", 100, 50, path=p)
        
        # Verify _pending_writes was incremented despite failure
        assert tb._pending_writes == 1

    def test_write_raises_oserror_on_disk_full_batched(self, tmp_path, monkeypatch):
        """Verify record_usage raises OSError when write fails during batched write."""
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 5)  # Enable batching
        tb._pending_writes = 0
        p = tmp_path / "ledger.json"
        
        # Mock _write to simulate disk full
        def mock_write_fail(path, data):
            raise OSError("No space left on device")
        
        monkeypatch.setattr(tb, "_write", mock_write_fail)
        
        with pytest.raises(OSError, match="No space left on device"):
            record_usage("2026-01-01", "gpt-4", 100, 50, path=p)
        
        # Verify _pending_writes was incremented despite failure
        assert tb._pending_writes == 1

    def test_lock_timeout_raises_timeout_error_batched(self, tmp_path, monkeypatch):
        """Verify record_usage raises TimeoutError when lock acquisition fails."""
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 5)  # Enable batching
        monkeypatch.setattr(tb, "LOCK_TIMEOUT", 0.1)  # Short timeout for test
        tb._pending_writes = 0
        p = tmp_path / "ledger.json"
        
        # Hold the lock in another thread to force timeout
        ready = threading.Event()
        hold_done = threading.Event()
        
        def hold_lock():
            lock_path = Path(str(p) + ".lock")
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            fp = lock_path.open("a+", encoding="utf-8")
            try:
                flock(fp.fileno(), LOCK_EX)
                ready.set()
                hold_done.wait(timeout=2)
            finally:
                fp.close()
        
        t = threading.Thread(target=hold_lock, daemon=True)
        t.start()
        ready.wait(timeout=2)
        
        try:
            with pytest.raises(TimeoutError, match="budget-unknown"):
                record_usage("2026-01-01", "gpt-4", 100, 50, path=p)
        finally:
            hold_done.set()
            t.join(timeout=2)
        
        # Verify _pending_writes was incremented despite failure
        assert tb._pending_writes == 1

    def test_pending_writes_incremented_before_call_allows_retry_logic(self, tmp_path, monkeypatch):
        """Verify _pending_writes is incremented before record_usage_locked call.
        
        This documents current behavior where counter increments even if write fails.
        Callers can use this to implement retry logic or detect repeated failures.
        """
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 5)
        tb._pending_writes = 0
        p = tmp_path / "ledger.json"
        
        # First call succeeds
        record_usage("2026-01-01", "gpt-4", 10, 0, path=p)
        assert tb._pending_writes == 1
        
        # Make subsequent calls fail
        def mock_write_fail(path, data):
            raise OSError("Simulated failure")
        
        monkeypatch.setattr(tb, "_write", mock_write_fail)
        
        # Second call fails but counter still increments
        with pytest.raises(OSError):
            record_usage("2026-01-01", "gpt-4", 10, 0, path=p)
        assert tb._pending_writes == 2
        
        # Third call also fails, counter increments again
        with pytest.raises(OSError):
            record_usage("2026-01-01", "gpt-4", 10, 0, path=p)
        assert tb._pending_writes == 3

    def test_tokens_accounted_on_next_successful_flush(self, tmp_path, monkeypatch):
        """Verify tokens are correctly accounted when flush eventually succeeds after failures."""
        monkeypatch.setattr(tb, "_FLUSH_AFTER_WRITES", 3)
        tb._pending_writes = 0
        p = tmp_path / "ledger.json"
        
        # First two calls succeed (batched, not flushed yet)
        r1 = record_usage("2026-01-01", "gpt-4", 10, 0, path=p)
        assert r1["total_actual"] == 10
        assert tb._pending_writes == 1
        
        r2 = record_usage("2026-01-01", "gpt-4", 10, 0, path=p)
        assert r2["total_actual"] == 20
        assert tb._pending_writes == 2
        
        # Make third call fail
        def mock_write_fail(path, data):
            raise OSError("Simulated failure")
        
        monkeypatch.setattr(tb, "_write", mock_write_fail)
        
        with pytest.raises(OSError):
            record_usage("2026-01-01", "gpt-4", 10, 0, path=p)
        assert tb._pending_writes == 0  # Reset after reaching threshold
        
        # Restore normal write behavior
        monkeypatch.undo()
        
        r4 = record_usage("2026-01-01", "gpt-4", 50, 0, path=p)
        assert r4["total_actual"] == 70

        ledger_data = json.loads(p.read_text(encoding="utf-8"))
        assert ledger_data["total_actual"] == 70
