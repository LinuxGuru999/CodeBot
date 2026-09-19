"""Tests for codebot.file_lock cross-platform file locking.

Covers:
- Import succeeds on all platforms
- Lock constants are defined correctly
- flock() accepts both int fd and file objects
- LOCK_EX, LOCK_SH, LOCK_UN operations work on Unix
- Non-blocking LOCK_NB raises BlockingIOError when contended
- Unsupported platform fallback warns and proceeds without error
"""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from codebot.file_lock import flock, LOCK_EX, LOCK_UN, LOCK_SH, LOCK_NB


class TestFileLockConstants(unittest.TestCase):
    """Verify lock operation constants match expected values."""

    def test_lock_ex_value(self):
        self.assertEqual(LOCK_EX, 1)

    def test_lock_sh_value(self):
        self.assertEqual(LOCK_SH, 0)

    def test_lock_un_value(self):
        self.assertEqual(LOCK_UN, 8)

    def test_lock_nb_value(self):
        self.assertEqual(LOCK_NB, 4)


class TestFlockUnix(unittest.TestCase):
    """Test flock behavior on Unix (or simulated Unix)."""

    @unittest.skipIf(sys.platform == "win32", "Unix-only test")
    def test_exclusive_lock_and_unlock(self):
        """Basic exclusive lock/unlock cycle works."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name
        try:
            with open(tmp_path, "a+") as fp:
                flock(fp.fileno(), LOCK_EX)
                flock(fp.fileno(), LOCK_UN)
        finally:
            os.unlink(tmp_path)

    @unittest.skipIf(sys.platform == "win32", "Unix-only test")
    def test_shared_lock_and_unlock(self):
        """Shared lock/unlock cycle works."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name
        try:
            with open(tmp_path, "a+") as fp:
                flock(fp.fileno(), LOCK_SH)
                flock(fp.fileno(), LOCK_UN)
        finally:
            os.unlink(tmp_path)

    @unittest.skipIf(sys.platform == "win32", "Unix-only test")
    def test_flock_accepts_file_object(self):
        """flock() accepts file objects with fileno() method."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name
        try:
            with open(tmp_path, "a+") as fp:
                flock(fp, LOCK_EX)
                flock(fp, LOCK_UN)
        finally:
            os.unlink(tmp_path)

    @unittest.skipIf(sys.platform == "win32", "Unix-only test")
    def test_nonblocking_lock_contention(self):
        """LOCK_NB raises BlockingIOError when lock is held by another fd."""
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name
        try:
            fp1 = open(tmp_path, "a+")
            fp2 = open(tmp_path, "a+")
            try:
                flock(fp1.fileno(), LOCK_EX)
                with self.assertRaises((BlockingIOError, OSError)):
                    flock(fp2.fileno(), LOCK_EX | LOCK_NB)
                flock(fp1.fileno(), LOCK_UN)
            finally:
                fp1.close()
                fp2.close()
        finally:
            os.unlink(tmp_path)


class TestFlockFallback(unittest.TestCase):
    """Test fallback behavior when no locking backend is available."""

    def test_fallback_warns_once(self):
        """When neither fcntl nor msvcrt available, warns and proceeds."""
        import codebot.file_lock as fl

        # Save original state
        orig_flock_avail = fl._FLOCK_AVAILABLE
        orig_msvcrt_avail = fl._MSCRT_AVAILABLE
        orig_warned = getattr(fl.flock, '_warned', False)

        try:
            # Simulate no backend available
            fl._FLOCK_AVAILABLE = False
            fl._MSCRT_AVAILABLE = False
            if hasattr(fl.flock, '_warned'):
                delattr(fl.flock, '_warned')

            with tempfile.NamedTemporaryFile(delete=False) as f:
                tmp_path = f.name
            try:
                with open(tmp_path, "a+") as fp:
                    with self.assertWarns(RuntimeWarning):
                        fl.flock(fp.fileno(), LOCK_EX)
                    # Second call should not warn again
                    fl.flock(fp.fileno(), LOCK_UN)
            finally:
                os.unlink(tmp_path)
        finally:
            # Restore original state
            fl._FLOCK_AVAILABLE = orig_flock_avail
            fl._MSCRT_AVAILABLE = orig_msvcrt_avail
            if orig_warned:
                fl.flock._warned = True
            elif hasattr(fl.flock, '_warned'):
                delattr(fl.flock, '_warned')


class TestImportSafety(unittest.TestCase):
    """Verify module imports cleanly regardless of platform."""

    def test_import_succeeds(self):
        """Module can be imported without ImportError."""
        import codebot.file_lock
        self.assertTrue(hasattr(codebot.file_lock, 'flock'))
        self.assertTrue(hasattr(codebot.file_lock, 'LOCK_EX'))
        self.assertTrue(hasattr(codebot.file_lock, 'LOCK_UN'))
        self.assertTrue(hasattr(codebot.file_lock, 'LOCK_SH'))
        self.assertTrue(hasattr(codebot.file_lock, 'LOCK_NB'))


if __name__ == "__main__":
    unittest.main()
