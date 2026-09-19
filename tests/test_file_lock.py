"""Tests for cross-platform file locking."""
import sys
import unittest
from unittest.mock import patch, MagicMock

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

if __name__ == '__main__':
    unittest.main()
