"""Tests for cross-platform file locking."""

import os
import sys
import tempfile
import pytest
from codebot.file_lock import flock, LOCK_EX, LOCK_UN, LOCK_NB


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
