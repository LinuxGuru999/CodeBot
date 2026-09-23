"""Extensive tests for codebot.locks — P0 ticket dispatch guard.

Covers: constants, descriptor handling (int vs TextIO), all flock
operations (SH/EX/UN/NB), blocking vs non-blocking, error paths,
fallback no-op platform, and cross-process concurrency.

Complements tests/test_file_lock.py which focuses on Windows-mocked
paths and bulk TOCTOU contention; this suite targets the public API
of codebot.locks directly via real file I/O on tmp_path.
"""

from __future__ import annotations

import multiprocessing
import multiprocessing.queues
import multiprocessing.synchronize
import os
import threading
import warnings
from pathlib import Path

import pytest

from codebot.locks import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mp_hold_then_release(
    lock_path: str,
    ready_queue: multiprocessing.queues.Queue[str],
    release_event: multiprocessing.synchronize.Event,
    done_queue: multiprocessing.queues.Queue[str],
) -> None:
    """Child: acquire EX, signal ready, wait for release signal, then unlock."""
    fd = os.open(lock_path, os.O_RDWR)
    try:
        flock(fd, LOCK_EX)
        ready_queue.put("acquired")
        _ = release_event.wait(timeout=10)
        flock(fd, LOCK_UN)
        done_queue.put("released")
    except OSError as exc:
        done_queue.put(f"error:{exc}")
    finally:
        os.close(fd)


def _mp_try_nonblocking(
    lock_path: str,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    """Child: try non-blocking EX, report success or OSError."""
    fd = os.open(lock_path, os.O_RDWR)
    try:
        try:
            flock(fd, LOCK_EX | LOCK_NB)
            result_queue.put("acquired")
            flock(fd, LOCK_UN)
        except OSError:
            result_queue.put("blocked")
    finally:
        os.close(fd)


def _mp_increment_counter(
    lock_path: str,
    counter_path: str,
    increments: int,
    result_queue: multiprocessing.queues.Queue[str],
) -> None:
    """Child: increment counter under exclusive lock increments times."""
    try:
        for _ in range(increments):
            fd_lock = os.open(lock_path, os.O_RDWR)
            fd_counter = os.open(counter_path, os.O_RDWR)
            try:
                flock(fd_lock, LOCK_EX)
                _ = os.lseek(fd_counter, 0, os.SEEK_SET)
                raw = os.read(fd_counter, 4096).decode().strip()
                val = int(raw) if raw else 0
                val += 1
                _ = os.lseek(fd_counter, 0, os.SEEK_SET)
                _ = os.ftruncate(fd_counter, 0)
                _ = os.write(fd_counter, str(val).encode())
                flock(fd_lock, LOCK_UN)
            finally:
                os.close(fd_lock)
                os.close(fd_counter)
        result_queue.put("ok")
    except OSError as exc:
        result_queue.put(f"error:{exc}")


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

class TestLockConstants:
    def test_constants_equal_expected_values_when_imported(self) -> None:
        assert LOCK_SH == 1
        assert LOCK_EX == 2
        assert LOCK_NB == 4
        assert LOCK_UN == 8

    def test_flags_combine_via_bitwise_or_when_combined(self) -> None:
        assert (LOCK_EX | LOCK_NB) == 6
        assert (LOCK_SH | LOCK_NB) == 5
        assert (LOCK_EX & LOCK_NB) == 0
        assert (LOCK_UN & LOCK_NB) == 0


# ---------------------------------------------------------------------------
# Descriptor handling
# ---------------------------------------------------------------------------

class TestFlockDescriptorHandling:
    def test_flock_succeeds_when_fd_is_int(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "int_fd.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd, LOCK_EX)
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)

    def test_flock_succeeds_when_fd_is_file_object(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "obj_fd.lock"
        lock_file.touch()
        with open(str(lock_file), "w+") as f:
            flock(f, LOCK_EX)
            flock(f, LOCK_UN)

    def test_flock_succeeds_with_shared_when_fd_is_file_object(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "shared_obj.lock"
        lock_file.touch()
        with open(str(lock_file), "r+") as f:
            flock(f, LOCK_SH)
            flock(f, LOCK_UN)


# ---------------------------------------------------------------------------
# Basic operations
# ---------------------------------------------------------------------------

class TestFlockBasicOperations:
    def test_exclusive_lock_held_when_acquired(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "basic_ex.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd, LOCK_EX)
            fd2 = os.open(str(lock_file), os.O_RDWR)
            try:
                with pytest.raises(OSError):
                    flock(fd2, LOCK_EX | LOCK_NB)
            finally:
                os.close(fd2)
        finally:
            flock(fd, LOCK_UN)
            os.close(fd)

    def test_shared_lock_held_when_acquired(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "basic_sh.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd, LOCK_SH)
            fd2 = os.open(str(lock_file), os.O_RDWR)
            try:
                flock(fd2, LOCK_SH | LOCK_NB)
                flock(fd2, LOCK_UN)
            finally:
                os.close(fd2)
        finally:
            flock(fd, LOCK_UN)
            os.close(fd)

    def test_unlock_releases_when_exclusive_held(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "unlock_ex.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        fd2 = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd, LOCK_EX)
            flock(fd, LOCK_UN)
            flock(fd2, LOCK_EX | LOCK_NB)
            flock(fd2, LOCK_UN)
        finally:
            os.close(fd)
            os.close(fd2)

    def test_unlock_succeeds_when_no_prior_lock(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "unlock_noprior.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)

    def test_relock_succeeds_when_same_fd_locked_twice(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "relock.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd, LOCK_EX)
            flock(fd, LOCK_EX)
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)


# ---------------------------------------------------------------------------
# Blocking vs non-blocking
# ---------------------------------------------------------------------------

class TestFlockBlockingAndNonBlocking:
    def test_nonblocking_exclusive_raises_when_exclusive_held(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "nb_ex.lock"
        lock_file.touch()
        fd_holder = os.open(str(lock_file), os.O_RDWR)
        fd_try = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd_holder, LOCK_EX)
            with pytest.raises(OSError):
                flock(fd_try, LOCK_EX | LOCK_NB)
        finally:
            flock(fd_holder, LOCK_UN)
            os.close(fd_holder)
            os.close(fd_try)

    def test_nonblocking_shared_raises_when_exclusive_held(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "nb_sh_ex.lock"
        lock_file.touch()
        fd_holder = os.open(str(lock_file), os.O_RDWR)
        fd_try = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd_holder, LOCK_EX)
            with pytest.raises(OSError):
                flock(fd_try, LOCK_SH | LOCK_NB)
        finally:
            flock(fd_holder, LOCK_UN)
            os.close(fd_holder)
            os.close(fd_try)

    def test_nonblocking_shared_succeeds_when_shared_held(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "nb_sh_sh.lock"
        lock_file.touch()
        fd_holder = os.open(str(lock_file), os.O_RDWR)
        fd_try = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd_holder, LOCK_SH)
            flock(fd_try, LOCK_SH | LOCK_NB)
            flock(fd_try, LOCK_UN)
        finally:
            flock(fd_holder, LOCK_UN)
            os.close(fd_holder)
            os.close(fd_try)

    def test_shared_allows_concurrent_when_both_request_shared(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "sh_concurrent.lock"
        lock_file.touch()
        fd1 = os.open(str(lock_file), os.O_RDWR)
        fd2 = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd1, LOCK_SH)
            flock(fd2, LOCK_SH)
            flock(fd2, LOCK_UN)
            flock(fd1, LOCK_UN)
        finally:
            os.close(fd1)
            os.close(fd2)

    def test_blocking_exclusive_waits_when_lock_held(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "blocking.lock"
        lock_file.touch()
        fd_holder = os.open(str(lock_file), os.O_RDWR)
        fd_waiter = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd_holder, LOCK_EX)

            waiter_started = threading.Event()
            waiter_acquired = threading.Event()

            def waiter() -> None:
                waiter_started.set()
                flock(fd_waiter, LOCK_EX)
                waiter_acquired.set()
                flock(fd_waiter, LOCK_UN)

            t = threading.Thread(target=waiter)
            t.start()

            _ = waiter_started.wait(timeout=5)
            assert not waiter_acquired.is_set()

            flock(fd_holder, LOCK_UN)
            _ = waiter_acquired.wait(timeout=5)
            assert waiter_acquired.is_set()
            t.join(timeout=5)
            assert not t.is_alive()
        finally:
            os.close(fd_holder)
            os.close(fd_waiter)


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

class TestFlockErrorHandling:
    def test_flock_raises_when_fd_is_closed_file_object(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "closed_obj.lock"
        lock_file.touch()
        f = open(str(lock_file), "w+")
        f.close()
        with pytest.raises((ValueError, OSError)):
            flock(f, LOCK_EX)

    def test_flock_raises_when_fd_is_closed_int(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "closed_int.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        os.close(fd)
        with pytest.raises(OSError):
            flock(fd, LOCK_EX)

    def test_flock_raises_when_fd_is_negative(self) -> None:
        with pytest.raises((ValueError, OSError)):
            flock(-1, LOCK_EX)

    def test_flock_raises_when_fd_is_invalid_large(self) -> None:
        with pytest.raises(OSError):
            flock(999999, LOCK_EX)


# ---------------------------------------------------------------------------
# Fallback / no-op platform
# ---------------------------------------------------------------------------

class TestFlockFallbackPlatform:
    def test_flock_emits_warning_when_platform_unsupported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codebot.locks as locks_mod

        monkeypatch.setattr(locks_mod, "_FLOCK_AVAILABLE", False)
        monkeypatch.setattr(locks_mod, "_MSCRT_AVAILABLE", False)
        if hasattr(locks_mod.flock, "_warned"):
            delattr(locks_mod.flock, "_warned")

        lock_file = tmp_path / "fallback_warn.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                flock(fd, LOCK_EX)
                assert len(w) == 1
                assert issubclass(w[0].category, RuntimeWarning)
                assert "File locking not available" in str(w[0].message)
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)
            if hasattr(locks_mod.flock, "_warned"):
                delattr(locks_mod.flock, "_warned")

    def test_flock_warns_only_once_when_called_twice(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codebot.locks as locks_mod

        monkeypatch.setattr(locks_mod, "_FLOCK_AVAILABLE", False)
        monkeypatch.setattr(locks_mod, "_MSCRT_AVAILABLE", False)
        if hasattr(locks_mod.flock, "_warned"):
            delattr(locks_mod.flock, "_warned")

        lock_file = tmp_path / "fallback_once.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            with warnings.catch_warnings(record=True) as w:
                warnings.simplefilter("always")
                flock(fd, LOCK_EX)
                flock(fd, LOCK_UN)
                flock(fd, LOCK_SH)
                flock(fd, LOCK_UN)
                assert len(w) == 1
        finally:
            os.close(fd)
            if hasattr(locks_mod.flock, "_warned"):
                delattr(locks_mod.flock, "_warned")

    def test_flock_is_noop_when_platform_unsupported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import codebot.locks as locks_mod

        monkeypatch.setattr(locks_mod, "_FLOCK_AVAILABLE", False)
        monkeypatch.setattr(locks_mod, "_MSCRT_AVAILABLE", False)
        if hasattr(locks_mod.flock, "_warned"):
            delattr(locks_mod.flock, "_warned")

        lock_file = tmp_path / "fallback_noop.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                flock(fd, LOCK_EX)
                flock(fd, LOCK_SH | LOCK_NB)
                flock(fd, LOCK_UN)
        finally:
            os.close(fd)
            if hasattr(locks_mod.flock, "_warned"):
                delattr(locks_mod.flock, "_warned")


# ---------------------------------------------------------------------------
# Concurrency — cross-process and position
# ---------------------------------------------------------------------------

class TestFlockConcurrency:
    def test_file_position_preserved_when_lock_acquired(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "pos.lock"
        _ = lock_file.write_bytes(b"\x00" * 200)
        fd = os.open(str(lock_file), os.O_RDWR)
        try:
            _ = os.lseek(fd, 50, os.SEEK_SET)
            flock(fd, LOCK_EX)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 50
            flock(fd, LOCK_UN)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 50
            _ = os.lseek(fd, 123, os.SEEK_SET)
            flock(fd, LOCK_SH)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 123
            flock(fd, LOCK_UN)
            assert os.lseek(fd, 0, os.SEEK_CUR) == 123
        finally:
            os.close(fd)

    def test_cross_process_nonblocking_fails_when_exclusive_held(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "mp_nb.lock"
        lock_file.touch()
        ready_q: multiprocessing.queues.Queue[str] = multiprocessing.Queue()
        done_q: multiprocessing.queues.Queue[str] = multiprocessing.Queue()
        release_evt: multiprocessing.synchronize.Event = multiprocessing.Event()
        p = multiprocessing.Process(
            target=_mp_hold_then_release,
            args=(str(lock_file), ready_q, release_evt, done_q),
        )
        p.start()
        try:
            msg = ready_q.get(timeout=10)
            assert msg == "acquired"
            fd = os.open(str(lock_file), os.O_RDWR)
            try:
                with pytest.raises(OSError):
                    flock(fd, LOCK_EX | LOCK_NB)
            finally:
                os.close(fd)
            result_q: multiprocessing.queues.Queue[str] = multiprocessing.Queue()
            p2 = multiprocessing.Process(
                target=_mp_try_nonblocking, args=(str(lock_file), result_q)
            )
            p2.start()
            p2.join(timeout=10)
            blocked = result_q.get(timeout=5)
            assert blocked == "blocked"
            p2.close()
            release_evt.set()
            done_msg = done_q.get(timeout=10)
            assert done_msg == "released"
            fd2 = os.open(str(lock_file), os.O_RDWR)
            try:
                flock(fd2, LOCK_EX | LOCK_NB)
                flock(fd2, LOCK_UN)
            finally:
                os.close(fd2)
        finally:
            p.join(timeout=10)
            if p.is_alive():
                p.terminate()
                p.join(timeout=5)

    def test_cross_process_counter_correct_when_two_processes_contend(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "mp_counter.lock"
        counter_file = tmp_path / "mp_counter.txt"
        lock_file.touch()
        _ = counter_file.write_text("0")
        num_workers = 2
        increments = 30
        result_q: multiprocessing.queues.Queue[str] = multiprocessing.Queue()
        workers: list[multiprocessing.Process] = []
        for _ in range(num_workers):
            proc = multiprocessing.Process(
                target=_mp_increment_counter,
                args=(str(lock_file), str(counter_file), increments, result_q),
            )
            workers.append(proc)
            proc.start()
        for proc in workers:
            proc.join(timeout=30)
            assert not proc.is_alive()
        results: list[str] = []
        while not result_q.empty():
            val = result_q.get(timeout=2)
            results.append(val)
        assert results.count("ok") == num_workers
        final = int(counter_file.read_text().strip())
        assert final == num_workers * increments

    def test_blocking_acquire_succeeds_after_release_when_thread_waits(self, tmp_path: Path) -> None:
        lock_file = tmp_path / "thread_block.lock"
        lock_file.touch()
        fd_holder = os.open(str(lock_file), os.O_RDWR)
        fd_waiter = os.open(str(lock_file), os.O_RDWR)
        try:
            flock(fd_holder, LOCK_EX)
            acquired = threading.Event()
            done = threading.Event()

            def blocking_waiter() -> None:
                flock(fd_waiter, LOCK_EX)
                acquired.set()
                flock(fd_waiter, LOCK_UN)
                done.set()

            t = threading.Thread(target=blocking_waiter)
            t.start()
            _ = acquired.wait(timeout=0.2)
            assert not acquired.is_set()
            flock(fd_holder, LOCK_UN)
            _ = acquired.wait(timeout=5)
            assert acquired.is_set()
            _ = done.wait(timeout=5)
            assert done.is_set()
            t.join(timeout=5)
            assert not t.is_alive()
        finally:
            os.close(fd_holder)
            os.close(fd_waiter)
