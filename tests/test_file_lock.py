"""Tests for cross-platform file locking."""
import sys
import os
import tempfile
import unittest
from unittest.mock import patch, MagicMock, call

# Ensure the module can be imported even if fcntl is missing
class TestFileLockImport(unittest.TestCase):
    def test_import_without_fcntl(self):
        """Verify that file_lock.py imports successfully even if fcntl is unavailable."""
        # Mock the import system to raise ImportError for fcntl
        original_import = __builtins__.__import__

        def mock_import(name, *args, **kwargs):
            if name == 'fcntl':
                raise ImportError("No module named 'fcntl'")
            return original_import(name, *args, **kwargs)

        try:
            with patch('builtins.__import__', side_effect=mock_import):
                # Remove from cache if present to force re-import
                if 'codebot.file_lock' in sys.modules:
                    del sys.modules['codebot.file_lock']
                
                # This should not raise an exception
                from codebot import file_lock
                
                # Verify flags are set correctly
                self.assertFalse(file_lock._FLOCK_AVAILABLE)
                self.assertIsNone(file_lock._fcntl)
        finally:
            # Restore original import
            pass

    def test_import_with_fcntl(self):
        """Verify normal import when fcntl is available."""
        if 'codebot.file_lock' in sys.modules:
            del sys.modules['codebot.file_lock']
        
        from codebot import file_lock
        
        # On Unix-like systems, fcntl should be available
        if sys.platform != "win32":
            self.assertTrue(file_lock._FLOCK_AVAILABLE)
            self.assertIsNotNone(file_lock._fcntl)

class TestWindowsLockingTOCTOU(unittest.TestCase):
    """Test that Windows msvcrt locking does not have TOCTOU race via seeking."""

    @unittest.skipUnless(sys.platform == "win32", "Windows-specific test")
    def test_locking_does_not_seek_on_windows(self):
        """Verify that flock() on Windows does not perform lseek operations.
        
        The original implementation had a TOCTOU race:
          1. lseek to save position
          2. lseek to start of file
          3. locking call (race window between 1-3)
          4. lseek to restore position
        
        The fix should lock at current position without any seeking.
        """
        from codebot import file_lock
        
        # Create a temp file and get fd
        fd, path = tempfile.mkstemp()
        try:
            with patch.object(os, 'lseek', wraps=os.lseek) as mock_lseek:
                # Attempt to lock the file
                file_lock.flock(fd, file_lock.LOCK_EX)
                
                # Verify lseek was NOT called during locking
                # If seeking occurs, it indicates the TOCTOU vulnerability exists
                lseek_calls = [c for c in mock_lseek.call_args_list if c[0][0] == fd]
                self.assertEqual(
                    len(lseek_calls), 0,
                    f"flock() performed {len(lseek_calls)} lseek() calls on Windows, "
                    "indicating TOCTOU race condition. Locking should be atomic at current position."
                )
                
                # Unlock
                file_lock.flock(fd, file_lock.LOCK_UN)
        finally:
            os.close(fd)
            os.unlink(path)

    def test_locking_api_on_mocked_windows(self):
        """Test Windows locking path with mocked msvcrt to verify no seeking."""
        # Force Windows path by mocking platform and msvcrt
        with patch.object(sys, 'platform', 'win32'):
            with patch('codebot.file_lock._IS_WINDOWS', True):
                with patch('codebot.file_lock._FLOCK_AVAILABLE', False):
                    with patch('codebot.file_lock._MSCRT_AVAILABLE', True):
                        mock_msvcrt = MagicMock()
                        mock_msvcrt.LK_LOCK = 1
                        mock_msvcrt.LK_NBLCK = 2
                        mock_msvcrt.LK_UNLCK = 3
                        mock_msvcrt.locking = MagicMock()
                        
                        with patch('codebot.file_lock._msvcrt', mock_msvcrt):
                            with patch.object(os, 'lseek') as mock_lseek:
                                from codebot import file_lock as fl_win
                                
                                # Create temp file
                                fd, path = tempfile.mkstemp()
                                try:
                                    # Lock
                                    fl_win.flock(fd, fl_win.LOCK_EX)
                                    
                                    # Verify msvcrt.locking was called
                                    mock_msvcrt.locking.assert_called_once()
                                    
                                    # CRITICAL: Verify os.lseek was NOT called
                                    # This proves the fix eliminates the TOCTOU window
                                    self.assertEqual(
                                        mock_lseek.call_count, 0,
                                        "Windows flock() should not call lseek(); "
                                        "locking must happen at current position atomically"
                                    )
                                    
                                    # Reset for unlock test
                                    mock_msvcrt.locking.reset_mock()
                                    mock_lseek.reset_mock()
                                    
                                    # Unlock
                                    fl_win.flock(fd, fl_win.LOCK_UN)
                                    
                                    # Unlock also should not seek
                                    self.assertEqual(
                                        mock_lseek.call_count, 0,
                                        "Windows flock(LOCK_UN) should not call lseek()"
                                    )
                                finally:
                                    os.close(fd)
                                    os.unlink(path)


if __name__ == '__main__':
    unittest.main()
