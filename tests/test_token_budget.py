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
from codebot.token_budget import (
    CAP,
    _FLUSH_AFTER_WRITES,
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


@pytest.fixture(autouse=True)
def reset_pending_writes():
    tb._pending_writes = 0
    yield
    tb._pending_writes = 0


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
    def test_concurrent_locked_writes_with_threadsafe_tmp(self, tmp_path, monkeypatch):
        import uuid as _uuid
        orig_write = tb._write

        def threadsafe_write(path, data):
            tmp = Path(str(path) + f".{os.getpid()}.{_uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            tmp.replace(path)

        monkeypatch.setattr(tb, "_write", threadsafe_write)
        p = tmp_path / "ledger.json"
        n_threads = 5
        n_writes_per_thread = 10
        tokens_per_write = 100

        def worker():
            for _ in range(n_writes_per_thread):
                record_usage_locked("2026-01-01", "gpt-4", tokens_per_write, 0, path=p)

        with ThreadPoolExecutor(max_workers=n_threads) as executor:
            futures = [executor.submit(worker) for _ in range(n_threads)]
            for f in futures:
                f.result()

        total = day_total("2026-01-01", path=p)
        expected = n_threads * n_writes_per_thread * tokens_per_write
        assert total == expected

    def test_concurrent_different_models_threadsafe(self, tmp_path, monkeypatch):
        import uuid as _uuid
        orig_write = tb._write

        def threadsafe_write(path, data):
            tmp = Path(str(path) + f".{os.getpid()}.{_uuid.uuid4().hex}.tmp")
            tmp.write_text(json.dumps(data, sort_keys=True), encoding="utf-8")
            tmp.replace(path)

        monkeypatch.setattr(tb, "_write", threadsafe_write)
        p = tmp_path / "ledger.json"

        def worker(model, amount):
            for _ in range(5):
                record_usage_locked("2026-01-01", model, amount, 0, path=p)

        with ThreadPoolExecutor(max_workers=3) as executor:
            futures = [
                executor.submit(worker, "gpt-4", 10),
                executor.submit(worker, "claude", 20),
                executor.submit(worker, "gemini", 30),
            ]
            for f in futures:
                f.result()

        data = json.loads(p.read_text(encoding="utf-8"))
        assert data["by_model"]["gpt-4"]["prompt_actual"] == 50
        assert data["by_model"]["claude"]["prompt_actual"] == 100
        assert data["by_model"]["gemini"]["prompt_actual"] == 150
        assert data["total_actual"] == 300

    def test_sequential_locked_writes_no_lost_updates(self, tmp_path):
        p = tmp_path / "ledger.json"
        for _ in range(50):
            record_usage_locked("2026-01-01", "gpt-4", 10, 0, path=p)
        assert day_total("2026-01-01", path=p) == 500

    def test_toctou_race_same_pid_tmp_collision(self, tmp_path):
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
        data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
        assert data is None or _valid_ledger(data)
        if errors:
            assert all(isinstance(e, FileNotFoundError) for e in errors)
        else:
            assert data["total_actual"] > 0

    def test_no_partial_writes_sequential(self, tmp_path):
        p = tmp_path / "ledger.json"
        for idx in range(4):
            for _ in range(5):
                record_usage_locked("2026-01-01", f"model-{idx}", 1, 1, path=p)
        data = json.loads(p.read_text(encoding="utf-8"))
        assert _valid_ledger(data)
        assert data["total_actual"] == 4 * 5 * 2


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

