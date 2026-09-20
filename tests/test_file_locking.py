"""Tests for cross-platform file locking in codebot.locks and ticket_engine.py.

Verifies that file locking works correctly on both Unix and Windows,
ensuring no race conditions or errors during concurrent access.

Acceptance Criteria (CB-8225024-07DF):
  - Tests pass on Linux and Windows
  - Verify atomicity and blocking behavior
"""

import json
import os
import sys
import time
import threading
import multiprocessing
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.locks import flock, LOCK_SH, LOCK_EX, LOCK_UN, LOCK_NB
from codebot.ticket_engine import (
    TicketStore,
    create_ticket,
    generate_ticket_id,
    TicketClass,
    Severity,
    RiskLevel,
    TicketState,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_ticket(evidence_suffix: str = "") -> "Ticket":
    """Create a minimal ticket for lock tests."""
    return create_ticket(
        title=f"lock-test-{generate_ticket_id()}",
        ticket_class=TicketClass.BUG,
        severity=Severity.LOW,
        source="test",
        evidence=f"ev-{evidence_suffix}-{generate_ticket_id()}",
        problem_statement="problem",
        desired_state="desired",
        acceptance_criteria=["crit"],
        risk=RiskLevel.LOW,
    )


def _lock_worker(lock_path: str, operation: int, result_queue: multiprocessing.Queue) -> None:
    """Worker function for multiprocessing lock tests."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        flock(fd, operation)
        result_queue.put(("acquired", os.getpid()))
        time.sleep(0.1)
        flock(fd, LOCK_UN)
        result_queue.put(("released", os.getpid()))
    except Exception as e:
        result_queue.put(("error", str(e)))
    finally:
        os.close(fd)


def _lock_and_release_worker(lock_path: str, queue: multiprocessing.Queue) -> None:
    """Worker that acquires LOCK_EX, holds briefly, then releases."""
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    try:
        flock(fd, LOCK_EX)
        queue.put(("locked",))
        time.sleep(0.2)
        flock(fd, LOCK_UN)
        queue.put(("unlocked",))
    finally:
        os.close(fd)


# ===========================================================================
# Unit tests: flock primitives
# ===========================================================================

class TestFlockPrimitives:
    """Direct tests for the flock locking primitives in codebot.locks."""

    def test_lock_ex_and_unlock(self, tmp_path):
        """Exclusive lock can be acquired and released."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        with open(lock_file, "a+") as fd:
            flock(fd, LOCK_EX)
            flock(fd, LOCK_UN)

    def test_lock_sh_and_unlock(self, tmp_path):
        """Shared lock can be acquired and released."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        with open(lock_file, "a+") as fd:
            flock(fd, LOCK_SH)
            flock(fd, LOCK_UN)

    def test_shared_locks_allow_concurrent_readers(self, tmp_path):
        """Multiple file descriptors can hold shared locks simultaneously."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        fd1 = open(lock_file, "a+")
        fd2 = open(lock_file, "a+")
        try:
            flock(fd1, LOCK_SH)
            # fd2 should also be able to acquire LOCK_SH without blocking
            flock(fd2, LOCK_SH)
            # Both hold shared locks
        finally:
            flock(fd1, LOCK_UN)
            flock(fd2, LOCK_UN)
            fd1.close()
            fd2.close()

    def test_exclusive_lock_blocks_exclusive_nb(self, tmp_path):
        """LOCK_EX with LOCK_NB fails when another process holds LOCK_EX."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        fd1 = open(lock_file, "a+")
        fd2 = open(lock_file, "a+")
        try:
            flock(fd1, LOCK_EX)
            with pytest.raises(OSError):
                flock(fd2, LOCK_EX | LOCK_NB)
        finally:
            flock(fd1, LOCK_UN)
            fd1.close()
            fd2.close()

    def test_exclusive_lock_blocks_shared_nb(self, tmp_path):
        """LOCK_SH with LOCK_NB fails when another process holds LOCK_EX."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        fd1 = open(lock_file, "a+")
        fd2 = open(lock_file, "a+")
        try:
            flock(fd1, LOCK_EX)
            with pytest.raises(OSError):
                flock(fd2, LOCK_SH | LOCK_NB)
        finally:
            flock(fd1, LOCK_UN)
            fd1.close()
            fd2.close()

    def test_unlock_allows_other_locks(self, tmp_path):
        """After unlock, another fd can acquire the lock."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        fd1 = open(lock_file, "a+")
        fd2 = open(lock_file, "a+")
        try:
            flock(fd1, LOCK_EX)
            flock(fd1, LOCK_UN)
            # fd2 should now be able to acquire without blocking
            flock(fd2, LOCK_EX | LOCK_NB)
            flock(fd2, LOCK_UN)
        finally:
            fd1.close()
            fd2.close()

    def test_lock_with_file_object(self, tmp_path):
        """flock accepts file objects (not just int fd)."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        with open(lock_file, "a+") as fd:
            flock(fd, LOCK_EX)
            flock(fd, LOCK_UN)

    def test_lock_with_int_fd(self, tmp_path):
        """flock accepts integer file descriptors."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            flock(fd, LOCK_EX)
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)


# ===========================================================================
# Non-blocking behavior
# ===========================================================================

class TestBlockingBehavior:
    """Tests verifying blocking and non-blocking lock behavior."""

    def test_nb_lock_ex_when_free_succeeds(self, tmp_path):
        """LOCK_EX | LOCK_NB succeeds when no lock is held."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            flock(fd, LOCK_EX | LOCK_NB)  # Should not raise
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)

    def test_nb_lock_ex_when_held_fails(self, tmp_path):
        """LOCK_EX | LOCK_NB fails immediately when lock is held."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        fd1 = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        fd2 = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            flock(fd1, LOCK_EX)
            with pytest.raises(OSError):
                flock(fd2, LOCK_EX | LOCK_NB)
            flock(fd1, LOCK_UN)
        finally:
            os.close(fd1)
            os.close(fd2)

    def test_nb_lock_sh_when_ex_held_fails(self, tmp_path):
        """LOCK_SH | LOCK_NB fails when LOCK_EX is held."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        fd1 = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        fd2 = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            flock(fd1, LOCK_EX)
            with pytest.raises(OSError):
                flock(fd2, LOCK_SH | LOCK_NB)
            flock(fd1, LOCK_UN)
        finally:
            os.close(fd1)
            os.close(fd2)

    def test_nb_lock_sh_when_sh_held_succeeds(self, tmp_path):
        """LOCK_SH | LOCK_NB succeeds when another LOCK_SH is held."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        fd1 = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        fd2 = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            flock(fd1, LOCK_SH)
            flock(fd2, LOCK_SH | LOCK_NB)  # Should succeed
            flock(fd1, LOCK_UN)
            flock(fd2, LOCK_UN)
        finally:
            os.close(fd1)
            os.close(fd2)

    def test_lock_constants_are_correct_ints(self):
        """Lock constants should be the expected integer values."""
        assert isinstance(LOCK_SH, int)
        assert isinstance(LOCK_EX, int)
        assert isinstance(LOCK_UN, int)
        assert isinstance(LOCK_NB, int)
        assert LOCK_SH == 1
        assert LOCK_EX == 2
        assert LOCK_UN == 8
        assert LOCK_NB == 4

    def test_lock_file_not_corrupted_after_rapid_cycles(self, tmp_path):
        """Rapid lock/unlock cycles don't corrupt the lock file."""
        lock_file = tmp_path / "rapid.lock"
        lock_file.touch()
        for _ in range(100):
            fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
            try:
                flock(fd, LOCK_EX)
                flock(fd, LOCK_UN)
            finally:
                os.close(fd)
        # File should still be readable
        assert lock_file.exists()


# ===========================================================================
# Cross-process lock tests
# ===========================================================================

class TestFlockCrossProcess:
    """Tests for flock behavior across multiple processes using multiprocessing."""

    def test_cross_process_exclusive_lock(self, tmp_path):
        """Two processes cannot hold LOCK_EX simultaneously."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        result_queue = multiprocessing.Queue()

        # Process 1 acquires LOCK_EX
        p1 = multiprocessing.Process(
            target=_lock_worker,
            args=(str(lock_file), LOCK_EX, result_queue),
        )
        p1.start()

        # Wait for process 1 to acquire
        msg = result_queue.get(timeout=5)
        assert msg[0] == "acquired"

        # Process 2 tries non-blocking LOCK_EX — should fail
        fd2 = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            with pytest.raises(OSError):
                flock(fd2, LOCK_EX | LOCK_NB)
        finally:
            os.close(fd2)

        p1.join(timeout=5)
        assert p1.exitcode == 0

    def test_cross_process_shared_locks(self, tmp_path):
        """Multiple processes can hold LOCK_SH simultaneously."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        result_queue = multiprocessing.Queue()

        p1 = multiprocessing.Process(
            target=_lock_worker,
            args=(str(lock_file), LOCK_SH, result_queue),
        )
        p2 = multiprocessing.Process(
            target=_lock_worker,
            args=(str(lock_file), LOCK_SH, result_queue),
        )
        p1.start()
        p2.start()

        # Both should acquire successfully
        acquired = []
        for _ in range(2):
            msg = result_queue.get(timeout=5)
            acquired.append(msg[0])
        assert acquired.count("acquired") == 2

        p1.join(timeout=5)
        p2.join(timeout=5)
        assert p1.exitcode == 0
        assert p2.exitcode == 0

    def test_cross_process_unlock_unblocks(self, tmp_path):
        """After process releases lock, another can acquire it."""
        lock_file = tmp_path / "test.lock"
        lock_file.touch()
        result_queue = multiprocessing.Queue()

        p1 = multiprocessing.Process(
            target=_lock_and_release_worker,
            args=(str(lock_file), result_queue),
        )
        p1.start()

        # Wait for lock to be acquired and released
        msg = result_queue.get(timeout=5)
        assert msg[0] == "locked"
        msg = result_queue.get(timeout=5)
        assert msg[0] == "unlocked"

        p1.join(timeout=5)
        assert p1.exitcode == 0

        # Now another process should be able to acquire
        fd = os.open(str(lock_file), os.O_RDWR | os.O_CREAT)
        try:
            flock(fd, LOCK_EX | LOCK_NB)  # Should succeed now
            flock(fd, LOCK_UN)
        finally:
            os.close(fd)


# ===========================================================================
# TicketStore file locking integration tests
# ===========================================================================

class TestTicketStoreFileLocking:
    """Tests for TicketStore's use of file locking for atomicity."""

    def _make_store(self, tmp_path):
        return TicketStore(tmp_path / "tickets.json")

    def test_save_uses_lock_file(self, tmp_path):
        """TicketStore creates and uses a .lock file during save."""
        path = tmp_path / "tickets.json"
        store = self._make_store(tmp_path)
        t = _create_ticket("lock-usage")
        store.add(t)
        store.flush()

        lock_path = path.with_suffix(".lock")
        assert lock_path.exists(), "Lock file should be created during save"
        store.close()

    def test_concurrent_adds_no_data_loss(self, tmp_path):
        """Multiple threads adding tickets concurrently lose no data."""
        store = self._make_store(tmp_path)
        num_threads = 8
        tickets_per_thread = 10
        all_ids = []
        id_lock = threading.Lock()

        def worker(thread_id):
            local_ids = []
            for i in range(tickets_per_thread):
                t = _create_ticket(f"t{thread_id}-{i}")
                store.add(t)
                local_ids.append(t.id)
            with id_lock:
                all_ids.extend(local_ids)

        threads = [threading.Thread(target=worker, args=(tid,)) for tid in range(num_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        store.flush()
        store.close()

        assert store.count() == num_threads * tickets_per_thread

        # Reload and verify
        store2 = TicketStore(tmp_path / "tickets.json")
        for tid in all_ids:
            assert store2.get(tid) is not None, f"Ticket {tid} lost after concurrent adds"
        store2.close()

    def test_concurrent_adds_and_transitions(self, tmp_path):
        """Concurrent adds and transitions don't corrupt state."""
        store = self._make_store(tmp_path)

        # Pre-create some tickets
        tickets = []
        for i in range(10):
            t = _create_ticket(f"pre-{i}")
            store.add(t)
            tickets.append(t)
        store.flush()

        errors = []

        def adder():
            try:
                for i in range(5):
                    t = _create_ticket(f"added-{i}")
                    store.add(t)
            except Exception as e:
                errors.append(e)

        def transitioner():
            try:
                for t in tickets[:5]:
                    try:
                        store.transition(t.id, TicketState.VALIDATING)
                    except (ValueError, KeyError):
                        pass  # Transition may fail due to state — that's ok
            except Exception as e:
                errors.append(e)

        threads = []
        for _ in range(3):
            threads.append(threading.Thread(target=adder))
            threads.append(threading.Thread(target=transitioner))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        assert not errors, f"Concurrent operations raised: {errors}"
        store.flush()
        store.close()

        # Verify store is consistent
        store2 = TicketStore(tmp_path / "tickets.json")
        assert store2.count() >= 10  # At least the original 10
        store2.close()

    def test_persistence_after_concurrent_writes(self, tmp_path):
        """Data is correctly persisted after concurrent writes."""
        path = tmp_path / "tickets.json"
        store1 = self._make_store(tmp_path)

        ids = []
        for i in range(20):
            t = _create_ticket(f"persist-{i}")
            store1.add(t)
            ids.append(t.id)

        store1.flush()
        store1.close()

        # Reload
        store2 = TicketStore(path)
        assert store2.count() == 20
        for tid in ids:
            assert store2.get(tid) is not None
        store2.close()

    def test_atomic_save_under_contention(self, tmp_path):
        """_save() under contention produces valid JSON."""
        store = self._make_store(tmp_path)

        for i in range(50):
            t = _create_ticket(f"atomic-{i}")
            store.add(t)

        errors = []

        def saver():
            try:
                store.flush()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=saver) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert not errors
        store.close()

        # Verify the file is valid JSON
        data = json.loads((tmp_path / "tickets.json").read_text(encoding="utf-8"))
        assert "tickets" in data
        assert len(data["tickets"]) == 50

    def test_lock_file_cleanup_on_close(self, tmp_path):
        """Lock file exists during operation but file is usable after close."""
        path = tmp_path / "tickets.json"
        store = self._make_store(tmp_path)

        t = _create_ticket("cleanup-test")
        store.add(t)
        store.flush()

        lock_path = path.with_suffix(".lock")
        assert lock_path.exists()

        store.close()

        # After close, should be able to open and read the file
        data = json.loads(path.read_text(encoding="utf-8"))
        assert len(data["tickets"]) == 1

    def test_multiple_stores_same_path_is_safe(self, tmp_path):
        """Two TicketStore instances on the same path don't corrupt data."""
        path = tmp_path / "tickets.json"
        store1 = TicketStore(path)

        t1 = _create_ticket("multi-1")
        store1.add(t1)
        store1.flush()

        # Open second store on same path (sees t1 from disk)
        store2 = TicketStore(path)
        assert store2.count() == 1  # store2 sees t1 from disk

        t2 = _create_ticket("multi-2")
        store2.add(t2)
        store2.flush()
        assert store2.count() == 2

        store1.close()
        store2.close()

        # Verify after both close — data from both stores is persisted
        store3 = TicketStore(path)
        assert store3.count() == 2
        store3.close()

    def test_shared_read_lock_during_load(self, tmp_path):
        """TicketStore load acquires and releases lock correctly."""
        path = tmp_path / "tickets.json"
        store1 = self._make_store(tmp_path)

        t = _create_ticket("read-lock-test")
        store1.add(t)
        store1.flush()
        store1.close()

        # Loading should acquire lock and release it
        store2 = TicketStore(path)
        assert store2.count() == 1
        store2.close()

    def test_concurrent_flush_produces_valid_json(self, tmp_path):
        """Multiple concurrent flush() calls produce valid JSON."""
        path = tmp_path / "tickets.json"
        store = TicketStore(path)

        for i in range(10):
            t = _create_ticket(f"flush-{i}")
            store.add(t)

        errors = []

        def flush_worker():
            try:
                store.flush()
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=flush_worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        store.close()

        assert not errors, f"Concurrent flush raised: {errors}"

        # Verify JSON is valid
        data = json.loads(path.read_text(encoding="utf-8"))
        assert "tickets" in data
        assert len(data["tickets"]) == 10

    def test_wal_concurrent_writes_not_corrupted(self, tmp_path):
        """Concurrent WAL writes don't corrupt the WAL file."""
        path = tmp_path / "tickets.json"
        store = TicketStore(path)

        errors = []

        def add_worker(worker_id):
            try:
                for i in range(5):
                    t = _create_ticket(f"wal-{worker_id}-{i}")
                    store.add(t)
            except Exception as e:
                errors.append(e)

        threads = [
            threading.Thread(target=add_worker, args=(wid,)) for wid in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)

        store.flush()
        store.close()

        assert not errors
        assert store.count() == 25  # 5 workers * 5 tickets

        # Reload and verify
        store2 = TicketStore(path)
        assert store2.count() == 25
        store2.close()
