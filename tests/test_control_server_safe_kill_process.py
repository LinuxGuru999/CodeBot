"""Tests for _safe_kill_process in control_server.py."""
import os
import signal
import unittest
from unittest.mock import patch, mock_open, MagicMock

from codebot.control_server import _safe_kill_process


class TestSafeKillProcess(unittest.TestCase):
    """Test suite for _safe_kill_process function."""

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_cmdline_mismatch_does_not_kill(self, mock_kill, mock_sleep):
        """When /proc/PID/cmdline doesn't match expected, no signal is sent."""
        fake_cmdline = b"python3\x00some_other_script.py\x00arg\x00"
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot")

        self.assertFalse(success)
        self.assertIsNone(killed_pid)
        mock_kill.assert_not_called()
        mock_sleep.assert_not_called()

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_nonexistent_pid_returns_failure(self, mock_kill, mock_sleep):
        """When PID doesn't exist (/proc missing), return failure without signaling."""
        m = mock_open()
        m.side_effect = FileNotFoundError
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(99999, "api_runner.py mybot")

        self.assertFalse(success)
        self.assertIsNone(killed_pid)
        mock_kill.assert_not_called()

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_term_then_exit_success(self, mock_kill, mock_sleep):
        """Process exits after SIGTERM -> only SIGTERM sent, returns success."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"

        # Track call count to simulate process exiting after SIGTERM
        kill_calls = []
        def kill_side_effect(pid, sig):
            kill_calls.append((pid, sig))
            if sig == 0 and len(kill_calls) >= 2:
                # After SIGTERM was sent, process is gone
                raise ProcessLookupError
            if sig == signal.SIGTERM:
                pass  # SIGTERM succeeds

        mock_kill.side_effect = kill_side_effect
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=1.0)

        self.assertTrue(success)
        self.assertEqual(killed_pid, 12345)
        # Should have SIGTERM and at least one liveness check (sig=0)
        sigs_sent = [c[1] for c in kill_calls if c[1] != 0]
        self.assertIn(signal.SIGTERM, sigs_sent)
        self.assertNotIn(signal.SIGKILL, sigs_sent)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_term_then_kill_escalation(self, mock_kill, mock_sleep):
        """Process ignores SIGTERM -> SIGTERM then SIGKILL sent, returns success."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"

        kill_calls = []
        def kill_side_effect(pid, sig):
            kill_calls.append((pid, sig))
            if sig == 0:
                # Process stays alive until SIGKILL is sent
                if any(c[1] == signal.SIGKILL for c in kill_calls[:-1]):
                    raise ProcessLookupError
                return  # still alive
            # Signals succeed

        mock_kill.side_effect = kill_side_effect
        m = mock_open(read_data=fake_cmdline)
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=0.5)

        self.assertTrue(success)
        self.assertEqual(killed_pid, 12345)
        sigs_sent = [c[1] for c in kill_calls if c[1] != 0]
        self.assertIn(signal.SIGTERM, sigs_sent)
        self.assertIn(signal.SIGKILL, sigs_sent)
        # SIGTERM must come before SIGKILL
        term_idx = sigs_sent.index(signal.SIGTERM)
        kill_idx = sigs_sent.index(signal.SIGKILL)
        self.assertLess(term_idx, kill_idx)

    def test_invalid_pid_rejected(self):
        """Non-positive, bool, or non-int pid is rejected immediately."""
        for bad_pid in [0, -1, True, False, "123", 3.14]:
            success, killed_pid = _safe_kill_process(bad_pid, "api_runner.py mybot")
            self.assertFalse(success, f"Expected failure for pid={bad_pid!r}")
            self.assertIsNone(killed_pid)

    def test_empty_cmdline_rejected(self):
        """Empty or non-string expected_cmdline is rejected."""
        for bad_cmd in ["", None, 123]:
            success, killed_pid = _safe_kill_process(12345, bad_cmd)
            self.assertFalse(success, f"Expected failure for cmdline={bad_cmd!r}")
            self.assertIsNone(killed_pid)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_permission_error_on_read_returns_failure(self, mock_kill, mock_sleep):
        """PermissionError reading /proc returns failure without signaling."""
        m = mock_open()
        m.side_effect = PermissionError
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot")

        self.assertFalse(success)
        self.assertIsNone(killed_pid)
        mock_kill.assert_not_called()

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_generic_exception_on_read_returns_failure(self, mock_kill, mock_sleep):
        """Generic Exception reading /proc returns failure without signaling."""
        m = mock_open()
        m.side_effect = OSError("disk error")
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot")

        self.assertFalse(success)
        self.assertIsNone(killed_pid)
        mock_kill.assert_not_called()

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_sigterm_process_lookup_error_returns_success(self, mock_kill, mock_sleep):
        """ProcessLookupError on SIGTERM means process already exited -> success."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"
        m = mock_open(read_data=fake_cmdline)

        def kill_side_effect(pid, sig):
            if sig == signal.SIGTERM:
                raise ProcessLookupError
            return None

        mock_kill.side_effect = kill_side_effect
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot")

        self.assertTrue(success)
        self.assertEqual(killed_pid, 12345)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_sigterm_permission_error_returns_failure(self, mock_kill, mock_sleep):
        """PermissionError on SIGTERM returns failure with pid."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"
        m = mock_open(read_data=fake_cmdline)

        def kill_side_effect(pid, sig):
            if sig == signal.SIGTERM:
                raise PermissionError
            return None

        mock_kill.side_effect = kill_side_effect
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot")

        self.assertFalse(success)
        self.assertEqual(killed_pid, 12345)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_sigterm_generic_exception_returns_failure(self, mock_kill, mock_sleep):
        """Generic Exception on SIGTERM returns failure with pid."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"
        m = mock_open(read_data=fake_cmdline)

        def kill_side_effect(pid, sig):
            if sig == signal.SIGTERM:
                raise OSError("signal failed")
            return None

        mock_kill.side_effect = kill_side_effect
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot")

        self.assertFalse(success)
        self.assertEqual(killed_pid, 12345)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_cmdline_changed_during_grace_period_aborts_sigkill(self, mock_kill, mock_sleep):
        """If cmdline changes during grace period, SIGKILL is aborted."""
        fake_cmdline_initial = b"python3\x00api_runner.py\x00mybot\x00"
        fake_cmdline_changed = b"python3\x00other_script.py\x00"

        open_calls = []
        def open_side_effect(*args, **kwargs):
            open_calls.append(args)
            if len(open_calls) == 1:
                return mock_open(read_data=fake_cmdline_initial)()
            else:
                return mock_open(read_data=fake_cmdline_changed)()

        # Process stays alive throughout grace period
        mock_kill.return_value = None

        with patch("builtins.open", side_effect=open_side_effect):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=0.5)

        self.assertFalse(success)
        self.assertEqual(killed_pid, 12345)
        # SIGKILL should NOT have been sent
        sigs_sent = [c[1] for c in mock_kill.call_args_list if c[0][1] != 0]
        self.assertNotIn(signal.SIGKILL, sigs_sent)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_sigkill_process_lookup_error_returns_success(self, mock_kill, mock_sleep):
        """ProcessLookupError on SIGKILL means process exited -> success."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"
        m = mock_open(read_data=fake_cmdline)

        call_count = [0]
        def kill_side_effect(pid, sig):
            call_count[0] += 1
            if sig == signal.SIGKILL:
                raise ProcessLookupError
            return None

        mock_kill.side_effect = kill_side_effect
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=0.1)

        self.assertTrue(success)
        self.assertEqual(killed_pid, 12345)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_sigkill_permission_error_returns_failure(self, mock_kill, mock_sleep):
        """PermissionError on SIGKILL returns failure with pid."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"
        m = mock_open(read_data=fake_cmdline)

        def kill_side_effect(pid, sig):
            if sig == signal.SIGKILL:
                raise PermissionError
            return None

        mock_kill.side_effect = kill_side_effect
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=0.1)

        self.assertFalse(success)
        self.assertEqual(killed_pid, 12345)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_sigkill_generic_exception_returns_failure(self, mock_kill, mock_sleep):
        """Generic Exception on SIGKILL returns failure with pid."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"
        m = mock_open(read_data=fake_cmdline)

        def kill_side_effect(pid, sig):
            if sig == signal.SIGKILL:
                raise OSError("sigkill failed")
            return None

        mock_kill.side_effect = kill_side_effect
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=0.1)

        self.assertFalse(success)
        self.assertEqual(killed_pid, 12345)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_zombie_after_sigkill_returns_failure(self, mock_kill, mock_sleep):
        """Process still alive after SIGKILL (zombie) returns failure."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"
        m = mock_open(read_data=fake_cmdline)

        # Process never dies
        mock_kill.return_value = None

        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=0.1)

        self.assertFalse(success)
        self.assertEqual(killed_pid, 12345)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_is_alive_permission_error_treats_as_alive(self, mock_kill, mock_sleep):
        """PermissionError on os.kill(p, 0) treats process as alive, proceeds to SIGKILL."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"
        m = mock_open(read_data=fake_cmdline)

        def kill_side_effect(pid, sig):
            if sig == 0:
                raise PermissionError
            return None

        mock_kill.side_effect = kill_side_effect
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=0.1)

        # Should escalate to SIGKILL since _is_alive returns True on PermissionError
        sigs_sent = [c[0][1] for c in mock_kill.call_args_list if c[0][1] != 0]
        self.assertIn(signal.SIGTERM, sigs_sent)
        self.assertIn(signal.SIGKILL, sigs_sent)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_is_alive_generic_exception_treats_as_dead(self, mock_kill, mock_sleep):
        """Generic Exception on os.kill(p, 0) treats process as dead -> success after SIGTERM."""
        fake_cmdline = b"python3\x00api_runner.py\x00mybot\x00"
        m = mock_open(read_data=fake_cmdline)

        def kill_side_effect(pid, sig):
            if sig == 0:
                raise OSError("unexpected")
            return None

        mock_kill.side_effect = kill_side_effect
        with patch("builtins.open", m):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=0.5)

        self.assertTrue(success)
        self.assertEqual(killed_pid, 12345)
        # SIGKILL should NOT be sent since _is_alive returned False
        sigs_sent = [c[0][1] for c in mock_kill.call_args_list if c[0][1] != 0]
        self.assertNotIn(signal.SIGKILL, sigs_sent)

    @patch("codebot.control_server.time.sleep")
    @patch("codebot.control_server.os.kill")
    def test_re_verify_cmdline_none_after_grace_returns_success(self, mock_kill, mock_sleep):
        """If /proc/PID/cmdline disappears during grace period, return success."""
        fake_cmdline_initial = b"python3\x00api_runner.py\x00mybot\x00"

        open_calls = []
        def open_side_effect(*args, **kwargs):
            open_calls.append(args)
            if len(open_calls) == 1:
                return mock_open(read_data=fake_cmdline_initial)()
            else:
                raise FileNotFoundError

        mock_kill.return_value = None

        with patch("builtins.open", side_effect=open_side_effect):
            success, killed_pid = _safe_kill_process(12345, "api_runner.py mybot", grace_period=0.1)

        self.assertTrue(success)
        self.assertEqual(killed_pid, 12345)


if __name__ == "__main__":
    unittest.main()
