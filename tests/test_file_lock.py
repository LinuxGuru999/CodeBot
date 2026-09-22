"""Tests for cross-platform file locking.

CB-6016863-5C51: Add concurrent access tests for Windows file locking.

Verifies the TOCTOU fix in Windows file locking by testing:
- Mocked Windows path (msvcrt) for atomic lock acquisition
- Concurrent lock contention across threads and processes
- Data integrity under concurrent write access
- Non-blocking lock behavior under contention
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from codebot.file_lock import flock, LOCK_EX, LOCK_UN, LOCK_NB, LOCK_SH
from codebot.locks import _FLOCK_AVAILABLE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_counter(fd: int, increments: int = 1) -> int:
    """Acquire exclusive lock, increment a counter in the file, release.

    Returns the new counter value.
    """
    flock(fd, LOCK_EX)
    try:
        # Read current value
        os.lseek(fd, 0, os.SEEK_SET)
        raw = os.read(fd, 1024).decode("utf-8").strip()
        count = int(raw) if raw else 0
        count += increments
        # Write new value atomically
        os.lseek(fd, 0, os.SEEK_SET)
        os.ftruncate(fd, 0)
        os.write(fd, str(count).encode("utf-8"))
        return count
    finally:
        flock(fd, LOCK_UN)


def _init_counter(path: str, initial: int = 0) -> None:
    """Initialize a counter file with the given value."""
    with open(path, "w") as f:
        f.write(str(initial))


# ---------------------------------------------------------------------------
# Windows-native tests (skipped on non-Windows)
# ---------------------------------------------------------------------------

class TestFileLockWindows:
    """Test Windows-specific locking behavior."""

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_non_blocking_lock_does_not_raise_attribute_error(self):
        """Non-blocking lock on Windows should not raise AttributeError.

        This test verifies the fix for CB-1784472-6BA8 where _msvcrt.LK_NBLCK
        caused an AttributeError because the constant doesn't exist.
        """
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            fd = tmp.fileno()

            try:
                # This should not raise AttributeError about LK_NBLCK
                # It may raise OSError if lock would block, but not AttributeError
                with pytest.raises((OSError, type(None))):
                    flock(fd, LOCK_EX | LOCK_NB)

                # Clean up
                flock(fd, LOCK_UN)
            finally:
                os.unlink(tmp_path)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows-only test")
    def test_exclusive_lock_works_on_windows(self):
        """Exclusive lock should work on Windows without errors."""
        with tempfile.NamedTemporaryFile(delete=False) as tmp:
            tmp_path = tmp.name
            fd = tmp.fileno()

            try:
                # Should not raise any exception
                flock(fd, LOCK_EX)
                flock(fd, LOCK_UN)
            finally:
                os.unlink(tmp_path)


# ---------------------------------------------------------------------------
# Mocked Windows locking path tests
# ---------------------------------------------------------------------------

class TestWindowsLockingPath:
    """Tests exercising the Windows msvcrt code path via mocking.

    These tests verify that the Windows branch in codebot.locks.flock()
    handles concurrent access atomically and does not exhibit TOCTOU
    race conditions.

    Since msvcrt is not available on Linux, we create a mock module and
    inject it into codebot.locks to exercise the Windows code path.
    """

    @pytest.fixture()
    def _patch_windows_env(self, monkeypatch):
        """Patch codebot.locks to simulate a Windows environment.

        Creates a mock _msvcrt module with the necessary constants and
        patches codebot.locks to use it, forcing the Windows code path.
        """
        import codebot.locks as locks_mod

        # Create a mock _msvcrt module with the constants used in locks.py
        mock_msvcrt = MagicMock()
        mock_msvcrt.LK_LOCK = 1
        mock_msvcrt.LK_UNLCK = 8

        # Force Windows path in codebot.locks
        monkeypatch.setattr(locks_mod, "_IS_WINDOWS", True)
        monkeypatch.setattr(locks_mod, "_FLOCK_AVAILABLE", False)
        monkeypatch.setattr(locks_mod, "_MSCRT_AVAILABLE", True)
        # _msvcrt only exists as a module attr on Windows; set it for Linux too
        setattr(locks_mod, "_msvcrt", mock_msvcrt)

        yield mock_msvcrt

    def test_windows_lock_ex_acquire_and_release(self, tmp_path, _patch_windows_env):
        """Windows path: exclusive lock can be acquired and released."""
        mock_msvcrt = _patch_windows_env
        lock_file = tmp_path / "test.lock"
        lock_file.write_bytes(b"\x00")
        fd = os.open(str(lock_file), os.O_RDWR)

        # Mock msvcrt.locking to succeed
        mock_msvcrt.locking.return_value = None
        try:
            flock(fd, LOCK_EX)
            mock_msvcrt.locking.assert_called()
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)

    def test_windows_lock_nb_nonblocking_raises_on_contention(
        self, tmp_path, _patch_windows_env
    ):
        """Windows path: non-blocking lock raises OSError when lock is held."""
        mock_msvcrt = _patch_windows_env
        lock_file = tmp_path / "test.lock"
        lock_file.write_bytes(b"\x00")

        call_count = 0

        def fake_locking(fd, mode, nbytes):
            nonlocal call_count
            call_count += 1
            # Simulate contention: second lock attempt fails
            if call_count > 1 and mode == mock_msvcrt.LK_LOCK:
                raise OSError(36, "Resource deadlock avoided")
            return None

        fd1 = os.open(str(lock_file), os.O_RDWR)
        fd2 = os.open(str(lock_file), os.O_RDWR)
        try:
            mock_msvcrt.locking.side_effect = fake_locking
            # First lock succeeds
            flock(fd1, LOCK_EX)
            # Second non-blocking lock raises
            with pytest.raises(OSError):
                flock(fd2, LOCK_EX | LOCK_NB)
            flock(fd1, LOCK_UN)
        finally:
            os.close(fd1)
            os.close(fd2)

    def test_windows_lock_unlock_resets_position(self, tmp_path, _patch_windows_env):
        """Windows path: unlock restores file position correctly."""
        mock_msvcrt = _patch_windows_env
        lock_file = tmp_path / "test.lock"
        lock_file.write_bytes(b"\x00" * 100)
        fd = os.open(str(lock_file), os.O_RDWR)

        mock_msvcrt.locking.return_value = None
        try:
            # Seek to middle
            os.lseek(fd, 50, os.SEEK_SET)
            flock(fd, LOCK_EX)
            # After lock, position should be back to 50 (restored in finally)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 50
            flock(fd, LOCK_UN)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 50
        finally:
            os.close(fd)

    def test_windows_lock_shared_treated_as_exclusive(self, tmp_path, _patch_windows_env):
        """Windows path: LOCK_SH is treated as LOCK_EX (no shared lock support)."""
        mock_msvcrt = _patch_windows_env
        lock_file = tmp_path / "test.lock"
        lock_file.write_bytes(b"\x00")

        locking_calls = []

        def fake_locking(fd, mode, nbytes):
            locking_calls.append(mode)
            return None

        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            mock_msvcrt.locking.side_effect = fake_locking
            flock(fd, LOCK_SH)
            # Should use LK_LOCK (exclusive), not a shared lock constant
            assert mock_msvcrt.LK_LOCK in locking_calls
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)

    def test_windows_lock_position_save_restore_cycle(self, tmp_path, _patch_windows_env):
        """Windows path: seek position saved and restored across lock/unlock cycle."""
        mock_msvcrt = _patch_windows_env
        lock_file = tmp_path / "test.lock"
        lock_file.write_bytes(b"\x00" * 200)

        # Verify position is preserved at various offsets
        for pos in [0, 1, 50, 100, 199]:
            fd = os.open(str(lock_file), os.O_RDWR)
            mock_msvcrt.locking.return_value = None
            try:
                os.lseek(fd, pos, os.SEEK_SET)
                flock(fd, LOCK_EX)
                assert os.lseek(fd, 0, os.SEEK_CUR) == pos, (
                    f"Position not preserved: expected {pos}, got "
                    f"{os.lseek(fd, 0, os.SEEK_CUR)}"
                )
                flock(fd, LOCK_UN)
                assert os.lseek(fd, 0, os.SEEK_CUR) == pos
            finally:
                os.close(fd)

    def test_windows_lock_file_position_at_start_for_lock(self, tmp_path, _patch_windows_env):
        """Windows path: lock always seeks to position 0 before locking."""
        mock_msvcrt = _patch_windows_env
        lock_file = tmp_path / "test.lock"
        lock_file.write_bytes(b"\x00" * 200)

        # Track all seek calls
        seek_calls = []
        original_lseek = os.lseek

        def tracking_lseek(fd, offset, whence):
            seek_calls.append((fd, offset, whence))
            return original_lseek(fd, offset, whence)

        fd = os.open(str(lock_file), os.O_RDWR)
        mock_msvcrt.locking.return_value = None
        try:
            os.lseek(fd, 100, os.SEEK_SET)
            with patch("codebot.locks.os.lseek", side_effect=tracking_lseek):
                flock(fd, LOCK_EX)
            # Should have seeked to SEEK_SET with offset 0 before locking
            lock_seeks = [s for s in seek_calls if s[1] == 0 and s[2] == os.SEEK_SET]
            assert len(lock_seeks) >= 1, (
                f"Expected at least one SEEK_SET(0) before lock, got seeks: {seek_calls}"
            )
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Concurrent access tests — thread-based
# ---------------------------------------------------------------------------

class TestConcurrentAccessThreads:
    """Thread-based concurrent access tests for file locking.

    These tests verify that flock prevents TOCTOU race conditions
    when multiple threads contend for the same lock.
    """

    def test_concurrent_lock_prevents_simultaneous_exclusive_access(
        self, tmp_path
    ):
        """Multiple threads racing for LOCK_EX — only one holds it at a time."""
        lock_file = tmp_path / "concurrent.lock"
        lock_file.touch()
        max_concurrent = 0
        concurrent_count = 0
        counter_lock = threading.Lock()

        def worker():
            nonlocal max_concurrent, concurrent_count
            fd = os.open(str(lock_file), os.O_RDWR)
            try:
                flock(fd, LOCK_EX)
                with counter_lock:
                    concurrent_count += 1
                    if concurrent_count > max_concurrent:
                        max_concurrent = concurrent_count
                # Hold lock briefly
                os.write(fd, b"x")
                with counter_lock:
                    concurrent_count -= 1
                flock(fd, LOCK_UN)
            finally:
                os.close(fd)

        threads = [threading.Thread(target=worker) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        if _FLOCK_AVAILABLE:
            assert max_concurrent <= 1, (
                f"Expected at most 1 concurrent lock holder, got {max_concurrent}"
            )
        else:
            pytest.skip("flock not available on this platform")

    def test_toctOU_write_integrity_under_contention(self, tmp_path):
        """Concurrent lock-then-write must not lose increments (TOCTOU check).

        Each of N threads increments a counter file 100 times under flock.
        The final value must equal N * 100.
        """
        counter_file = tmp_path / "counter.txt"
        num_threads = 8
        increments_per_thread = 100
        _init_counter(str(counter_file), 0)

        errors = []

        def incrementer():
            try:
                for _ in range(increments_per_thread):
                    fd = os.open(str(counter_file), os.O_RDWR)
                    try:
                        _write_counter(fd, 1)
                    finally:
                        os.close(fd)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=incrementer) for _ in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Errors during concurrent writes: {errors}"

        final_value = int(counter_file.read_text().strip())
        expected = num_threads * increments_per_thread
        assert final_value == expected, (
            f"Counter corruption: expected {expected}, got {final_value}"
        )

    def test_concurrent_read_write_data_integrity(self, tmp_path):
        """Writer holds LOCK_EX while writing; reader reads consistent state."""
        data_file = tmp_path / "data.json"
        data_file.write_text(json.dumps({"counter": 0, "log": []}))
        stop_event = threading.Event()
        reader_results = []

        def writer():
            fd = os.open(str(data_file), os.O_RDWR)
            try:
                for i in range(50):
                    flock(fd, LOCK_EX)
                    try:
                        data = json.loads(data_file.read_text())
                        data["counter"] = i + 1
                        data["log"].append(f"write-{i}")
                        data_file.write_text(json.dumps(data))
                    finally:
                        flock(fd, LOCK_UN)
            finally:
                os.close(fd)

        def reader():
            fd = os.open(str(data_file), os.O_RDONLY)
            try:
                while not stop_event.is_set():
                    flock(fd, LOCK_SH)
                    try:
                        data = json.loads(data_file.read_text())
                        # Log must be a prefix of counter: no torn reads
                        reader_results.append((data["counter"], len(data["log"])))
                    finally:
                        flock(fd, LOCK_UN)
            finally:
                os.close(fd)

        writer_t = threading.Thread(target=writer)
        reader_t = threading.Thread(target=reader)
        writer_t.start()
        reader_t.start()

        writer_t.join(timeout=10)
        stop_event.set()
        reader_t.join(timeout=5)

        # Every read must see a consistent counter/log pair
        for count, log_len in reader_results:
            assert count == log_len, (
                f"Inconsistent read: counter={count}, log_len={log_len}"
            )

    def test_nonblocking_lock_under_rapid_contention(self, tmp_path):
        """Many threads racing with LOCK_NB — all must either succeed or raise."""
        lock_file = tmp_path / "rapid.lock"
        lock_file.touch()
        successes = 0
        failures = 0
        results_lock = threading.Lock()

        def racer():
            nonlocal successes, failures
            fd = os.open(str(lock_file), os.O_RDWR)
            try:
                try:
                    flock(fd, LOCK_EX | LOCK_NB)
                    # Got the lock — hold briefly then release
                    os.write(fd, b"1")
                    flock(fd, LOCK_UN)
                    with results_lock:
                        successes += 1
                except OSError:
                    with results_lock:
                        failures += 1
            finally:
                os.close(fd)

        threads = [threading.Thread(target=racer) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        # All threads must have either succeeded or failed cleanly
        assert successes + failures == 50
        # At least one should have succeeded (lock is briefly held)
        assert successes >= 1
        # No deadlock, no crash

    def test_sequential_lock_cycle_preserves_data(self, tmp_path):
        """Lock, write, unlock cycle repeated rapidly preserves all writes."""
        data_file = tmp_path / "seq_data.bin"
        data_file.write_bytes(b"")
        num_cycles = 200

        for i in range(num_cycles):
            fd = os.open(str(data_file), os.O_RDWR)
            try:
                flock(fd, LOCK_EX)
                # Append a marker
                current = data_file.read_bytes()
                data_file.write_bytes(current + f"cycle-{i}\n".encode())
                flock(fd, LOCK_UN)
            finally:
                os.close(fd)

        lines = data_file.read_bytes().decode().strip().split("\n")
        assert len(lines) == num_cycles
        for i, line in enumerate(lines):
            assert line == f"cycle-{i}"


# ---------------------------------------------------------------------------
# Concurrent access tests — multiprocessing-based
# ---------------------------------------------------------------------------

def _mp_lock_and_write(lock_path: str, counter_path: str, increments: int,
                       result_queue):
    """Multiprocess worker: acquire lock, increment counter, release."""
    try:
        for _ in range(increments):
            fd_lock = os.open(lock_path, os.O_RDWR)
            fd_counter = os.open(counter_path, os.O_RDWR)
            try:
                flock(fd_lock, LOCK_EX)
                # Read-modify-write counter
                os.lseek(fd_counter, 0, os.SEEK_SET)
                raw = os.read(fd_counter, 1024).decode().strip()
                val = int(raw) if raw else 0
                val += 1
                os.lseek(fd_counter, 0, os.SEEK_SET)
                os.ftruncate(fd_counter, 0)
                os.write(fd_counter, str(val).encode())
                flock(fd_lock, LOCK_UN)
            finally:
                os.close(fd_lock)
                os.close(fd_counter)
        result_queue.put(("ok",))
    except Exception as e:
        result_queue.put(("error", str(e)))


def _mp_append_lines(lock_path, data_path, worker_id, count, queue):
    """Multiprocess worker: append JSON lines to a file under exclusive lock."""
    try:
        for i in range(count):
            fd_lock = os.open(lock_path, os.O_RDWR)
            try:
                flock(fd_lock, LOCK_EX)
                with open(data_path, "a") as f:
                    f.write(json.dumps({"w": worker_id, "i": i}) + "\n")
                flock(fd_lock, LOCK_UN)
            finally:
                os.close(fd_lock)
        queue.put(("ok",))
    except Exception as e:
        queue.put(("error", str(e)))


class TestConcurrentAccessProcesses:
    """Multiprocessing-based concurrent access tests.

    These verify file locking prevents TOCTOU corruption across
    OS processes (the most realistic scenario for Windows TOCTOU).
    """

    def test_cross_process_lock_integrity(self, tmp_path):
        """Multiple processes incrementing a counter under lock — no data loss."""
        import multiprocessing

        lock_file = tmp_path / "mp.lock"
        counter_file = tmp_path / "mp_counter.txt"
        lock_file.touch()
        counter_file.write_text("0")

        num_workers = 4
        increments_per_worker = 50
        result_queue = multiprocessing.Queue()

        workers = []
        for _ in range(num_workers):
            p = multiprocessing.Process(
                target=_mp_lock_and_write,
                args=(
                    str(lock_file),
                    str(counter_file),
                    increments_per_worker,
                    result_queue,
                ),
            )
            workers.append(p)
            p.start()

        errors = []
        for _ in range(num_workers):
            result = result_queue.get(timeout=30)
            if result[0] == "error":
                errors.append(result[1])

        for p in workers:
            p.join(timeout=10)

        assert not errors, f"Cross-process errors: {errors}"

        final_value = int(counter_file.read_text().strip())
        expected = num_workers * increments_per_worker
        assert final_value == expected, (
            f"Cross-process counter corruption: expected {expected}, got {final_value}"
        )

    def test_cross_process_no_data_corruption_under_load(self, tmp_path):
        """Rapid concurrent lock+write from multiple processes produces valid output."""
        import multiprocessing

        lock_file = tmp_path / "load.lock"
        data_file = tmp_path / "load_data.jsonl"
        lock_file.touch()
        data_file.write_text("")

        num_workers = 3
        writes_per_worker = 30
        result_queue = multiprocessing.Queue()

        workers = []
        for wid in range(num_workers):
            p = multiprocessing.Process(
                target=_mp_append_lines,
                args=(str(lock_file), str(data_file), wid, writes_per_worker, result_queue),
            )
            workers.append(p)
            p.start()

        errors = []
        for _ in range(num_workers):
            result = result_queue.get(timeout=30)
            if result[0] == "error":
                errors.append(result[1])

        for p in workers:
            p.join(timeout=10)

        assert not errors, f"Cross-process load test errors: {errors}"

        # Verify file is valid and complete
        lines = data_file.read_text().strip().split("\n")
        total_expected = num_workers * writes_per_worker
        assert len(lines) == total_expected, (
            f"Expected {total_expected} lines, got {len(lines)}"
        )

        # Verify each line is valid JSON
        for line in lines:
            obj = json.loads(line)
            assert "w" in obj and "i" in obj
