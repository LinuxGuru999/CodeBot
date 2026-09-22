"""Comprehensive tests for _safe_kill_bot_process to achieve 100% coverage.

Addresses reviewer feedback: Python Coverage Gate FAIL - _safe_kill_bot_process
has only 21.8% coverage (12/55 statements). This test file exercises ALL branches.
"""
import signal
import subprocess
import unittest
from unittest.mock import MagicMock, patch, mock_open

from codebot.control_server import _safe_kill_bot_process


class TestSafeKillBotProcessCoverage(unittest.TestCase):
    """Exhaustive branch coverage for _safe_kill_bot_process."""

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

    # --- Branch: pgrep returns empty stdout ---
    @patch("codebot.control_server.subprocess.run")
    def test_pgrep_empty_stdout_returns_true_empty(self, mock_run):
        """No matching processes -> success with empty list."""
        mock_run.return_value = MagicMock(stdout="", returncode=1)
        success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])

    @patch("codebot.control_server.subprocess.run")
    def test_pgrep_whitespace_only_returns_true_empty(self, mock_run):
        """Whitespace-only stdout -> success with empty list."""
        mock_run.return_value = MagicMock(stdout="   \n  \n", returncode=0)
        success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])

    # --- Branch: pgrep returns non-digit lines ---
    @patch("codebot.control_server.subprocess.run")
    def test_pgrep_non_digit_lines_skipped(self, mock_run):
        """Non-digit lines in pgrep output are skipped."""
        mock_run.return_value = MagicMock(stdout="abc\ndef\n", returncode=0)
        success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])

    @patch("codebot.control_server.subprocess.run")
    def test_pgrep_mixed_digit_and_non_digit(self, mock_run):
        """Mixed digit/non-digit lines: only digits are candidates."""
        mock_run.return_value = MagicMock(stdout="abc\n1234\nxyz\n", returncode=0)
        # PID 1234 exists but cmdline doesn't match
        with patch("builtins.open", side_effect=FileNotFoundError):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])

    # --- Branch: FileNotFoundError reading /proc/PID/cmdline ---
    @patch("codebot.control_server.subprocess.run")
    def test_cmdline_file_not_found_continues(self, mock_run):
        """Process exited before cmdline read -> skip PID, continue."""
        mock_run.return_value = MagicMock(stdout="1234\n5678", returncode=0)
        with patch("builtins.open", side_effect=FileNotFoundError):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])

    # --- Branch: PermissionError reading /proc/PID/cmdline ---
    @patch("codebot.control_server.subprocess.run")
    def test_cmdline_permission_error_continues(self, mock_run):
        """Permission denied reading cmdline -> skip PID, continue."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        with patch("builtins.open", side_effect=PermissionError):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])

    # --- Branch: Generic Exception reading /proc/PID/cmdline ---
    @patch("codebot.control_server.subprocess.run")
    def test_cmdline_generic_exception_continues(self, mock_run):
        """Generic exception reading cmdline -> skip PID, continue."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        with patch("builtins.open", side_effect=OSError("disk error")):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])

    # --- Branch: Exact match + is_python -> kill ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_exact_match_python_killed(self, mock_run, mock_kill):
        """Exact argv match with python process -> SIGTERM sent."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        fake_cmdline = b"python3\x00api_runner.py\x00my-bot\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [1234])
        mock_kill.assert_called_once_with(1234, signal.SIGTERM)

    # --- Branch: Exact match but NOT python -> skip with warning ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_exact_match_not_python_skipped(self, mock_run, mock_kill):
        """Exact argv match but not a python process -> NOT killed."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        # No 'python' in any argv element
        fake_cmdline = b"ruby\x00api_runner.py\x00my-bot\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])
        mock_kill.assert_not_called()

    # --- Branch: No exact match -> skip with debug ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_no_exact_match_skipped(self, mock_run, mock_kill):
        """Cmdline doesn't have exact consecutive argv match -> NOT killed."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        # 'api_runner.py my-bot' as single arg (substring, not separate argv)
        fake_cmdline = b"python3\x00manage.py\x00api_runner.py my-bot\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])
        mock_kill.assert_not_called()

    # --- Branch: Substring in different argv positions -> NOT killed ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_substring_in_non_consecutive_args_not_killed(self, mock_run, mock_kill):
        """Target appears in cmdline but not as consecutive exact args -> NOT killed."""
        mock_run.return_value = MagicMock(stdout="9999", returncode=0)
        # api_runner.py and my-bot exist but separated by other args
        fake_cmdline = b"python3\x00api_runner.py\x00--verbose\x00my-bot\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])
        mock_kill.assert_not_called()

    # --- Branch: Multiple PIDs, some match, some don't ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_multiple_pids_selective_kill(self, mock_run, mock_kill):
        """Multiple candidate PIDs: only verified ones are killed."""
        mock_run.return_value = MagicMock(stdout="1111\n2222\n3333", returncode=0)

        call_count = [0]
        def open_side_effect(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                # PID 1111: exact match + python -> kill
                return mock_open(read_data=b"python3\x00api_runner.py\x00my-bot\x00")()
            elif call_count[0] == 2:
                # PID 2222: no match -> skip
                return mock_open(read_data=b"python3\x00other_script.py\x00")()
            else:
                # PID 3333: exact match + python -> kill
                return mock_open(read_data=b"python3\x00api_runner.py\x00my-bot\x00")()

        with patch("builtins.open", side_effect=open_side_effect):
            success, pids = _safe_kill_bot_process("my-bot")

        self.assertTrue(success)
        self.assertEqual(sorted(pids), [1111, 3333])
        self.assertEqual(mock_kill.call_count, 2)

    # --- Branch: ProcessLookupError during os.kill ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_kill_process_lookup_error_continues(self, mock_run, mock_kill):
        """Process already exited during kill -> continue gracefully."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        fake_cmdline = b"python3\x00api_runner.py\x00my-bot\x00"
        m = mock_open(read_data=fake_cmdline)
        mock_kill.side_effect = ProcessLookupError
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [1234])  # PID was verified, just already gone

    # --- Branch: PermissionError during os.kill ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_kill_permission_error_continues(self, mock_run, mock_kill):
        """Permission denied during kill -> log warning, continue."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        fake_cmdline = b"python3\x00api_runner.py\x00my-bot\x00"
        m = mock_open(read_data=fake_cmdline)
        mock_kill.side_effect = PermissionError
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [1234])  # PID was verified

    # --- Branch: Generic Exception during os.kill ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_kill_generic_exception_continues(self, mock_run, mock_kill):
        """Generic exception during kill -> log warning, continue."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        fake_cmdline = b"python3\x00api_runner.py\x00my-bot\x00"
        m = mock_open(read_data=fake_cmdline)
        mock_kill.side_effect = OSError("signal failed")
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [1234])  # PID was verified

    # --- Branch: subprocess.TimeoutExpired ---
    @patch("codebot.control_server.subprocess.run")
    def test_pgrep_timeout_returns_false(self, mock_run):
        """pgrep timeout -> return False."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd="pgrep", timeout=5)
        success, pids = _safe_kill_bot_process("my-bot")
        self.assertFalse(success)
        self.assertEqual(pids, [])

    # --- Branch: Generic Exception in outer try ---
    @patch("codebot.control_server.subprocess.run")
    def test_pgrep_generic_exception_returns_false(self, mock_run):
        """Unexpected exception in pgrep -> return False."""
        mock_run.side_effect = RuntimeError("unexpected")
        success, pids = _safe_kill_bot_process("my-bot")
        self.assertFalse(success)
        self.assertEqual(pids, [])

    # --- Branch: Empty cmdline bytes (all nulls) ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_empty_cmdline_bytes_not_killed(self, mock_run, mock_kill):
        """Empty cmdline (kernel thread) -> not killed."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        fake_cmdline = b"\x00\x00\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])
        mock_kill.assert_not_called()

    # --- Branch: Single argv element (no consecutive pair possible) ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_single_argv_element_not_killed(self, mock_run, mock_kill):
        """Only one argv element -> can't have consecutive match -> not killed."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        fake_cmdline = b"python3\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])
        mock_kill.assert_not_called()

    # --- Branch: Bot name with special regex chars (escaped properly) ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_bot_name_with_hyphens_and_underscores(self, mock_run, mock_kill):
        """Bot names with hyphens/underscores work correctly."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        fake_cmdline = b"python3\x00api_runner.py\x00my-test_bot\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-test_bot")
        self.assertTrue(success)
        self.assertEqual(pids, [1234])
        mock_kill.assert_called_once()

    # --- Branch: Unicode decode errors in cmdline handled gracefully ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_cmdline_with_invalid_utf8_handled(self, mock_run, mock_kill):
        """Invalid UTF-8 in cmdline decoded with errors='replace'."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        # Invalid UTF-8 byte sequence
        fake_cmdline = b"python3\x00api_runner.py\x00my-bot\x00\xff\xfe\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [1234])
        mock_kill.assert_called_once()

    # --- Branch: Multiple kills, one fails mid-way ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_multiple_pids_one_kill_fails(self, mock_run, mock_kill):
        """Multiple verified PIDs: if one kill fails, others still attempted."""
        mock_run.return_value = MagicMock(stdout="1111\n2222", returncode=0)

        call_count = [0]
        def open_side_effect(*args, **kwargs):
            call_count[0] += 1
            return mock_open(read_data=b"python3\x00api_runner.py\x00my-bot\x00")()

        # First kill succeeds, second raises PermissionError
        mock_kill.side_effect = [None, PermissionError]

        with patch("builtins.open", side_effect=open_side_effect):
            success, pids = _safe_kill_bot_process("my-bot")

        self.assertTrue(success)
        self.assertEqual(sorted(pids), [1111, 2222])
        self.assertEqual(mock_kill.call_count, 2)

    # --- Branch: pgrep returns trailing newlines and spaces ---
    @patch("codebot.control_server.subprocess.run")
    def test_pgrep_trailing_whitespace_handled(self, mock_run):
        """pgrep output with trailing whitespace/newlines parsed correctly."""
        mock_run.return_value = MagicMock(stdout="  1234  \n  5678  \n\n", returncode=0)
        with patch("builtins.open", side_effect=FileNotFoundError):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [])

    # --- Branch: Case-insensitive python detection ---
    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_python_detection_case_insensitive(self, mock_run, mock_kill):
        """Python detection is case-insensitive (PYTHON, Python, python)."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        # Uppercase PYTHON
        fake_cmdline = b"PYTHON3\x00api_runner.py\x00my-bot\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [1234])
        mock_kill.assert_called_once()

    @patch("codebot.control_server.os.kill")
    @patch("codebot.control_server.subprocess.run")
    def test_python_in_path_detected(self, mock_run, mock_kill):
        """Python detected when full path contains 'python'."""
        mock_run.return_value = MagicMock(stdout="1234", returncode=0)
        # Full path to python interpreter
        fake_cmdline = b"/usr/bin/python3.11\x00api_runner.py\x00my-bot\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, pids = _safe_kill_bot_process("my-bot")
        self.assertTrue(success)
        self.assertEqual(pids, [1234])
        mock_kill.assert_called_once()


if __name__ == "__main__":
    unittest.main()
