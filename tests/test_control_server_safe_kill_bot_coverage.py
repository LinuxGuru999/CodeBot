"""Comprehensive tests for _safe_kill_bot_process to achieve 100% coverage.

Tests the PID-file-based implementation that replaces pgrep/pkill.
All tests mock _read_pid_file and _verify_cmdline instead of subprocess.run.
"""
import signal
import unittest
from unittest.mock import patch, MagicMock

from codebot.control_server import _safe_kill_bot_process, _atomic_signal_pid


class TestSafeKillBotProcessCoverage(unittest.TestCase):
    """Exhaustive branch coverage for _safe_kill_bot_process using PID files."""

    # --- Branch: Invalid bot name ---
    def test_invalid_bot_name_returns_false(self):
        """Invalid bot name is rejected immediately."""
        success, pids = _safe_kill_bot_process("invalid; name!")
        self.assertFalse(success)
        self.assertEqual(pids, [])

    def test_empty_bot_name_returns_false(self):
        """Empty bot name is rejected."""
        success, pids = _safe_kill_bot_process("")
        self.assertFalse(success)
        self.assertEqual(pids, [])

    def test_bot_name_with_spaces_rejected(self):
        """Bot name with spaces is rejected by validate_bot_name."""
        success, pids = _safe_kill_bot_process("my bot")
        self.assertFalse(success)
        self.assertEqual(pids, [])

    # --- Branch: No PID file found ---
    @patch("codebot.control_server._read_pid_file")
    def test_no_pid_file_returns_true_empty(self, mock_read_pid):
        """No PID file -> success with empty list (no process to kill)."""
        mock_read_pid.return_value = None
        success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])
        mock_read_pid.assert_called_once_with("my-bot")

    # --- Branch: PID file exists but cmdline verification fails ---
    @patch("codebot.control_server._verify_cmdline")
    @patch("codebot.control_server._read_pid_file")
    def test_pid_file_exists_cmdline_verify_fails_no_cleanup(self, mock_read_pid, mock_verify):
        """PID file exists but cmdline doesn't match -> skip kill, do NOT delete PID file (DoS prevention)."""
        mock_read_pid.return_value = 1234
        mock_verify.return_value = False

        success, pids = _safe_kill_bot_process("my-bot")

        self.assertTrue(success)
        self.assertEqual(pids, [])
        mock_verify.assert_called_once_with(1234, "my-bot")
        # PID file must NOT be deleted on verification failure to prevent DoS race

    # --- Branch: PID file exists, cmdline verifies, kill succeeds ---
    @patch("codebot.control_server._resolve_control_state_dir")
    @patch("codebot.control_server._atomic_signal_pid")
    @patch("codebot.control_server._verify_cmdline")
    @patch("codebot.control_server._read_pid_file")
    def test_pid_file_exists_cmdline_verifies_kill_succeeds(self, mock_read_pid, mock_verify, mock_signal, mock_resolve):
        """PID file exists, cmdline matches, signal sent successfully."""
        mock_read_pid.return_value = 1234
        mock_verify.return_value = True
        mock_signal.return_value = True

        mock_pid_file = MagicMock()
        mock_pid_file.exists.return_value = True
        mock_state_dir = MagicMock()
        mock_state_dir.__truediv__.return_value = mock_pid_file
        mock_resolve.return_value = mock_state_dir

        success, pids = _safe_kill_bot_process("my-bot")

        self.assertTrue(success)
        self.assertEqual(pids, [1234])
        mock_signal.assert_called_once()
        call_args = mock_signal.call_args
        self.assertEqual(call_args.args[0], 1234)
        self.assertEqual(call_args.args[1], signal.SIGTERM)
        mock_pid_file.unlink.assert_called_once()

    # --- Branch: PID file exists, cmdline verifies, pidfd signaling unavailable (fail closed) ---
    @patch("codebot.control_server._resolve_control_state_dir")
    @patch("codebot.control_server._atomic_signal_pid")
    @patch("codebot.control_server._verify_cmdline")
    @patch("codebot.control_server._read_pid_file")
    def test_pid_file_exists_cmdline_verifies_pidfd_unavailable(self, mock_read_pid, mock_verify, mock_signal, mock_resolve):
        """PID file exists, cmdline matches, but pidfd signaling fails - safe failure."""
        mock_read_pid.return_value = 1234
        mock_verify.return_value = True
        mock_signal.return_value = False  # pidfd unavailable

        mock_pid_file = MagicMock()
        mock_pid_file.exists.return_value = True
        mock_state_dir = MagicMock()
        mock_state_dir.__truediv__.return_value = mock_pid_file
        mock_resolve.return_value = mock_state_dir

        success, pids = _safe_kill_bot_process("my-bot")

        self.assertTrue(success)  # Operation succeeded (no exception)
        self.assertEqual(pids, [])  # But no PID was killed
        mock_signal.assert_called_once()
        call_args = mock_signal.call_args
        self.assertEqual(call_args.args[0], 1234)
        self.assertEqual(call_args.args[1], signal.SIGTERM)
        # PID file should NOT be cleaned up when signaling fails
        mock_pid_file.unlink.assert_not_called()

    # --- Branch: PID file exists, cmdline verifies, ProcessLookupError ---
    @patch("codebot.control_server._atomic_signal_pid", side_effect=ProcessLookupError)
    @patch("codebot.control_server._verify_cmdline", return_value=True)
    @patch("codebot.control_server._read_pid_file", return_value=1234)
    def test_pid_file_exists_kill_process_lookup_error(self, mock_read_pid, mock_verify, mock_signal):
        """Process already exited -> return success with PID."""
        success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [1234])

    @patch("codebot.control_server._atomic_signal_pid")
    @patch("codebot.control_server._verify_cmdline")
    @patch("codebot.control_server._read_pid_file")
    def test_pid_file_exists_kill_permission_error(self, mock_read_pid, mock_verify, mock_signal):
        """Permission denied killing -> log warning, return success with PID."""
        mock_read_pid.return_value = 1234
        mock_verify.return_value = True
        mock_signal.side_effect = PermissionError
        
        success, pids = _safe_kill_bot_process("my-bot")
        
        self.assertTrue(success)
        self.assertEqual(pids, [1234])

    @patch("codebot.control_server._atomic_signal_pid")
    @patch("codebot.control_server._verify_cmdline")
    @patch("codebot.control_server._read_pid_file")
    def test_pid_file_exists_kill_generic_exception(self, mock_read_pid, mock_verify, mock_signal):
        """Generic exception during kill -> log warning, return success with PID."""
        mock_read_pid.return_value = 1234
        mock_verify.return_value = True
        mock_signal.side_effect = OSError("signal failed")
        
        success, pids = _safe_kill_bot_process("my-bot")
        
        self.assertTrue(success)
        self.assertEqual(pids, [1234])

    # --- Branch: PID file cleanup fails silently ---
    @patch("codebot.control_server._resolve_control_state_dir")
    @patch("codebot.control_server._atomic_signal_pid")
    @patch("codebot.control_server._verify_cmdline")
    @patch("codebot.control_server._read_pid_file")
    def test_pid_file_cleanup_os_error_ignored(self, mock_read_pid, mock_verify, mock_signal, mock_resolve):
        """PID file unlink raises OSError -> ignored silently."""
        mock_read_pid.return_value = 1234
        mock_verify.return_value = True
        mock_signal.return_value = True

        mock_pid_file = MagicMock()
        mock_pid_file.exists.return_value = True
        mock_pid_file.unlink.side_effect = OSError("cannot unlink")
        mock_state_dir = MagicMock()
        mock_state_dir.__truediv__.return_value = mock_pid_file
        mock_resolve.return_value = mock_state_dir

        success, pids = _safe_kill_bot_process("my-bot")

        self.assertTrue(success)
        self.assertEqual(pids, [1234])
        mock_pid_file.unlink.assert_called_once()

    # --- Branch: Cmdline verification fails, PID file NOT cleaned up (DoS prevention) ---
    @patch("codebot.control_server._verify_cmdline")
    @patch("codebot.control_server._read_pid_file")
    def test_cmdline_verify_fails_pid_file_preserved(self, mock_read_pid, mock_verify):
        """Cmdline verify fails -> PID file preserved to prevent DoS race condition."""
        mock_read_pid.return_value = 1234
        mock_verify.return_value = False

        success, pids = _safe_kill_bot_process("my-bot")

        self.assertTrue(success)
        self.assertEqual(pids, [])
        # No file operations should occur when verification fails

    # --- Branch: _read_pid_file returns non-integer (corrupt file) ---
    @patch("codebot.control_server._read_pid_file")
    def test_pid_file_corrupt_returns_none(self, mock_read_pid):
        """Corrupt PID file (non-digit content) treated as None."""
        # _read_pid_file already handles this internally, returning None
        mock_read_pid.return_value = None
        success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])


if __name__ == "__main__":
    unittest.main()
