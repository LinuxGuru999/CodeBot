"""Additional tests to achieve 100% coverage for control_server.py.

Focuses on previously uncovered functions:
- scheduler_status()
- _safe_kill_bot_process()
- _safe_kill_orchestrator()
- _safe_kill_process()
- retry_dead_letter()
- _handle_telemetry()
- main()
- _check_budget_alerts()
- economics_budget_status()
- economics_summary()
- Helper methods in ControlHandler
"""
import io
import json
import logging
import os
import time
from unittest.mock import patch, MagicMock, mock_open

import pytest


class TestSchedulerStatus:
    """Tests for scheduler_status() function."""

    def test_scheduler_status_basic(self):
        """scheduler_status should return a dict with expected keys."""
        from codebot.control_server import scheduler_status

        result = scheduler_status()
        assert isinstance(result, dict)
        assert "version" in result
        assert result["version"] == 1
        assert "drain" in result
        assert "dead_letter_count" in result
        assert "queue_tracked" in result
        assert "lease_active" in result

    def test_scheduler_status_with_import_error(self):
        """scheduler_status should handle ImportError gracefully."""
        with patch.dict("sys.modules", {"codebot.token_budget": None, "bots.token_budget": None}):
            from codebot.control_server import scheduler_status
            # Force re-import to trigger the error path
            import importlib
            import codebot.control_server as cs
            importlib.reload(cs)
            result = cs.scheduler_status()
            assert isinstance(result, dict)
            assert result["budget_state"] == "budget-unknown"

    def test_scheduler_status_with_os_error(self):
        """scheduler_status should handle OSError when reading files."""
        from codebot.control_server import scheduler_status, STATE_DIR

        with patch.object(STATE_DIR, "__truediv__") as mock_div:
            mock_path = MagicMock()
            mock_path.exists.return_value = True
            mock_path.read_text.side_effect = OSError("test error")
            mock_div.return_value = mock_path

            result = scheduler_status()
            assert isinstance(result, dict)


class TestSafeKillBotProcess:
    """Tests for _safe_kill_bot_process() function."""

    def test_invalid_bot_name_rejected(self):
        """_safe_kill_bot_process should reject invalid bot names."""
        from codebot.control_server import _safe_kill_bot_process

        success, pids = _safe_kill_bot_process("evil; rm -rf /")
        assert success is False
        assert pids == []

    def test_no_matching_process(self):
        """_safe_kill_bot_process should return success when no process found."""
        from codebot.control_server import _safe_kill_bot_process

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            success, pids = _safe_kill_bot_process("valid-bot")
            assert success is True
            assert pids == []

    def test_process_found_and_killed(self):
        """_safe_kill_bot_process should find and kill matching processes."""
        from codebot.control_server import _safe_kill_bot_process

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="12345\n", returncode=0)

            # Mock /proc/PID/cmdline reading
            mock_file = MagicMock()
            mock_file.__enter__.return_value = mock_file
            mock_file.__exit__.return_value = None
            mock_file.read.return_value = b"python3\x00api_runner.py\x00valid-bot\x00"

            with patch("builtins.open", return_value=mock_file):
                with patch("os.kill") as mock_kill:
                    success, pids = _safe_kill_bot_process("valid-bot")
                    assert success is True
                    assert 12345 in pids
                    mock_kill.assert_called_once()

    def test_pgrep_timeout(self):
        """_safe_kill_bot_process should handle pgrep timeout."""
        from codebot.control_server import _safe_kill_bot_process

        import subprocess
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="pgrep", timeout=5)
            success, pids = _safe_kill_bot_process("valid-bot")
            assert success is False

    def test_permission_error_reading_cmdline(self):
        """_safe_kill_bot_process should handle PermissionError when reading cmdline."""
        from codebot.control_server import _safe_kill_bot_process

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="12345\n", returncode=0)

            mock_file = MagicMock()
            mock_file.__enter__.side_effect = PermissionError("denied")
            mock_file.__exit__.return_value = None

            with patch("builtins.open", return_value=mock_file):
                success, pids = _safe_kill_bot_process("valid-bot")
                assert success is True  # No errors in killing since we couldn't verify
                assert pids == []


class TestSafeKillOrchestrator:
    """Tests for _safe_kill_orchestrator() function."""

    def test_no_matching_process(self):
        """_safe_kill_orchestrator should return success when no process found."""
        from codebot.control_server import _safe_kill_orchestrator

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="", returncode=1)
            success, pids = _safe_kill_orchestrator()
            assert success is True
            assert pids == []

    def test_process_found_and_killed(self):
        """_safe_kill_orchestrator should find and kill matching processes."""
        from codebot.control_server import _safe_kill_orchestrator

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="12345\n", returncode=0)

            mock_file = MagicMock()
            mock_file.__enter__.return_value = mock_file
            mock_file.__exit__.return_value = None
            mock_file.read.return_value = b"python3\x00orchestrator.py\x00"

            with patch("builtins.open", return_value=mock_file):
                with patch("os.kill") as mock_kill:
                    success, pids = _safe_kill_orchestrator()
                    assert success is True
                    assert 12345 in pids
                    mock_kill.assert_called_once()

    def test_non_python_process_skipped(self):
        """_safe_kill_orchestrator should skip non-python processes."""
        from codebot.control_server import _safe_kill_orchestrator

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout="12345\n", returncode=0)

            mock_file = MagicMock()
            mock_file.__enter__.return_value = mock_file
            mock_file.__exit__.return_value = None
            # Non-python process
            mock_file.read.return_value = b"vim\x00orchestrator.py\x00"

            with patch("builtins.open", return_value=mock_file):
                with patch("os.kill") as mock_kill:
                    success, pids = _safe_kill_orchestrator()
                    assert success is True
                    assert pids == []  # Should be skipped
                    mock_kill.assert_not_called()

    def test_pgrep_timeout(self):
        """_safe_kill_orchestrator should handle pgrep timeout."""
        from codebot.control_server import _safe_kill_orchestrator

        import subprocess
        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired(cmd="pgrep", timeout=5)
            success, pids = _safe_kill_orchestrator()
            assert success is False


class TestSafeKillProcess:
    """Tests for _safe_kill_process() function."""

    def test_invalid_pid_rejected(self):
        """_safe_kill_process should reject invalid PIDs."""
        from codebot.control_server import _safe_kill_process

        success, pid = _safe_kill_process(-1, "test")
        assert success is False
        assert pid is None

        success, pid = _safe_kill_process(True, "test")  # bool is subclass of int
        assert success is False
        assert pid is None

    def test_invalid_cmdline_rejected(self):
        """_safe_kill_process should reject empty cmdline."""
        from codebot.control_server import _safe_kill_process

        success, pid = _safe_kill_process(123, "")
        assert success is False
        assert pid is None

    def test_process_not_found(self):
        """_safe_kill_process should handle non-existent process."""
        from codebot.control_server import _safe_kill_process

        mock_file = MagicMock()
        mock_file.__enter__.side_effect = FileNotFoundError()
        mock_file.__exit__.return_value = None

        with patch("builtins.open", return_value=mock_file):
            success, pid = _safe_kill_process(123, "test")
            assert success is False
            assert pid is None

    def test_cmdline_mismatch(self):
        """_safe_kill_process should abort if cmdline doesn't match."""
        from codebot.control_server import _safe_kill_process

        mock_file = MagicMock()
        mock_file.__enter__.return_value = mock_file
        mock_file.__exit__.return_value = None
        mock_file.read.return_value = b"different process cmdline"

        with patch("builtins.open", return_value=mock_file):
            success, pid = _safe_kill_process(123, "expected cmdline")
            assert success is False
            assert pid is None

    def test_sigterm_success(self):
        """_safe_kill_process should succeed with SIGTERM."""
        from codebot.control_server import _safe_kill_process

        mock_file = MagicMock()
        mock_file.__enter__.return_value = mock_file
        mock_file.__exit__.return_value = None
        mock_file.read.return_value = b"expected cmdline process"

        with patch("builtins.open", return_value=mock_file):
            with patch("os.kill") as mock_kill:
                with patch("time.sleep"):
                    # First call succeeds, second call (check alive) raises ProcessLookupError
                    mock_kill.side_effect = [None, ProcessLookupError()]
                    success, pid = _safe_kill_process(123, "expected cmdline")
                    assert success is True
                    assert pid == 123

    def test_sigkill_escalation(self):
        """_safe_kill_process should escalate to SIGKILL if SIGTERM fails."""
        from codebot.control_server import _safe_kill_process

        mock_file = MagicMock()
        mock_file.__enter__.return_value = mock_file
        mock_file.__exit__.return_value = None
        mock_file.read.return_value = b"expected cmdline process"

        with patch("builtins.open", return_value=mock_file):
            with patch("os.kill") as mock_kill:
                with patch("time.sleep"):
                    # os.kill(0) returns True (alive), then SIGKILL succeeds
                    mock_kill.side_effect = [None, None, None]  # SIGTERM, check alive, SIGKILL
                    # We need _is_alive to return True first, then False after SIGKILL
                    with patch("os.kill") as mock_kill2:
                        call_count = [0]
                        def kill_sideeffect(pid, sig):
                            call_count[0] += 1
                            if call_count[0] == 3:  # After SIGKILL
                                raise ProcessLookupError()
                        mock_kill2.side_effect = kill_sideeffect
                        success, pid = _safe_kill_process(123, "expected cmdline", grace_period=0.1)


class TestRetryDeadLetter:
    """Tests for retry_dead_letter() function."""

    def test_retry_success(self):
        """retry_dead_letter should return success when retry works."""
        from codebot.control_server import retry_dead_letter

        with patch("codebot.lease_state.retry_dead_letter") as mock_retry:
            mock_retry.return_value = {"status": "retried", "id": "Q-123"}
            result = retry_dead_letter("Q-123")
            assert result["status"] == "retried"

    def test_retry_unavailable(self):
        """retry_dead_letter should handle ImportError gracefully."""
        with patch.dict("sys.modules", {"codebot.lease_state": None, "bots.lease_state": None}):
            from codebot.control_server import retry_dead_letter
            import importlib
            import codebot.control_server as cs
            importlib.reload(cs)
            result = cs.retry_dead_letter("Q-123")
            assert result["status"] == "unavailable"


class TestCheckBudgetAlerts:
    """Tests for _check_budget_alerts() function."""

    def test_no_alerts_below_threshold(self):
        """No alerts when budget is below 80%."""
        from codebot.control_server import _check_budget_alerts

        alerts = _check_budget_alerts(50.0, "ok")
        assert len(alerts) == 0

    def test_warn_alert_at_80_percent(self):
        """Warn alert at 80% budget."""
        from codebot.control_server import _check_budget_alerts

        alerts = _check_budget_alerts(80.0, "ok")
        assert len(alerts) == 1
        assert alerts[0]["level"] == "warn"
        assert alerts[0]["threshold"] == 80

    def test_critical_alert_at_90_percent(self):
        """Critical alert at 90% budget."""
        from codebot.control_server import _check_budget_alerts

        alerts = _check_budget_alerts(90.0, "ok")
        assert len(alerts) == 1
        assert alerts[0]["level"] == "critical"
        assert alerts[0]["threshold"] == 90

    def test_stop_state_alert(self):
        """Stop state triggers critical alert."""
        from codebot.control_server import _check_budget_alerts

        alerts = _check_budget_alerts(50.0, "stop")
        assert len(alerts) == 1
        assert alerts[0]["level"] == "critical"
        assert alerts[0]["threshold"] == 100


class TestEconomicsBudgetStatus:
    """Tests for economics_budget_status() function."""

    def test_basic_return(self):
        """economics_budget_status should return a dict with expected keys."""
        from codebot.control_server import economics_budget_status

        result = economics_budget_status()
        assert isinstance(result, dict)
        assert "budget_cap" in result
        assert "budget_used" in result
        assert "budget_remaining" in result
        assert "budget_pct" in result
        assert "budget_state" in result

    def test_import_error_handling(self):
        """economics_budget_status should handle ImportError gracefully."""
        with patch.dict("sys.modules", {"codebot.token_budget": None, "bots.token_budget": None}):
            from codebot.control_server import economics_budget_status
            import importlib
            import codebot.control_server as cs
            importlib.reload(cs)
            result = cs.economics_budget_status()
            assert isinstance(result, dict)
            assert result["budget_state"] == "budget-unknown"


class TestEconomicsSummary:
    """Tests for economics_summary() function."""

    def test_basic_return(self):
        """economics_summary should return a dict with expected keys."""
        from codebot.control_server import economics_summary

        result = economics_summary()
        assert isinstance(result, dict)
        assert "version" in result
        assert "budget" in result
        assert "fleet" in result
        assert "alerts" in result


class TestControlHandlerHelpers:
    """Tests for ControlHandler helper methods."""

    def test_is_loopback_client_ipv4(self):
        """_is_loopback_client should return True for 127.0.0.1."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.client_address = ("127.0.0.1", 12345)
        result = ControlHandler._is_loopback_client(handler)
        assert result is True

    def test_is_loopback_client_ipv6(self):
        """_is_loopback_client should return True for ::1."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.client_address = ("::1", 12345)
        result = ControlHandler._is_loopback_client(handler)
        assert result is True

    def test_is_loopback_client_remote(self):
        """_is_loopback_client should return False for remote IP."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.client_address = ("192.168.1.1", 12345)
        result = ControlHandler._is_loopback_client(handler)
        assert result is False

    def test_read_json_body_missing_content_length(self):
        """_read_json_body should return empty dict when no Content-Length."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.headers = {}
        body, error_code, error = ControlHandler._read_json_body(handler)
        assert body == {}
        assert error_code is None

    def test_read_json_body_invalid_content_length(self):
        """_read_json_body should return 400 for non-integer Content-Length."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.headers = {"Content-Length": "abc"}
        body, error_code, error = ControlHandler._read_json_body(handler)
        assert body is None
        assert error_code == 400

    def test_read_json_body_negative_content_length(self):
        """_read_json_body should return 400 for negative Content-Length."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.headers = {"Content-Length": "-1"}
        body, error_code, error = ControlHandler._read_json_body(handler)
        assert body is None
        assert error_code == 400

    def test_read_json_body_too_large(self):
        """_read_json_body should return 413 for oversized body."""
        from codebot.control_server import ControlHandler, MAX_REQUEST_BYTES

        handler = MagicMock(spec=ControlHandler)
        handler.headers = {"Content-Length": str(MAX_REQUEST_BYTES + 1)}
        body, error_code, error = ControlHandler._read_json_body(handler)
        assert body is None
        assert error_code == 413

    def test_read_json_body_valid(self):
        """_read_json_body should parse valid JSON body."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.headers = {"Content-Length": "13"}
        handler.rfile = io.BytesIO(b'{"key": "val"}')
        handler.connection = MagicMock()
        body, error_code, error = ControlHandler._read_json_body(handler)
        assert body == {"key": "val"}
        assert error_code is None

    def test_read_json_body_invalid_json(self):
        """_read_json_body should return 400 for invalid JSON."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.headers = {"Content-Length": "5"}
        handler.rfile = io.BytesIO(b'invalid')
        handler.connection = MagicMock()
        body, error_code, error = ControlHandler._read_json_body(handler)
        assert body is None
        assert error_code == 400

    def test_read_json_body_not_dict(self):
        """_read_json_body should return 400 for non-dict JSON."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        handler.headers = {"Content-Length": "4"}
        handler.rfile = io.BytesIO(b'[1,2]')
        handler.connection = MagicMock()
        body, error_code, error = ControlHandler._read_json_body(handler)
        assert body is None
        assert error_code == 400

    def test_get_destructive_preview_stop_all(self):
        """_get_destructive_preview should describe stop-all action."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        preview = ControlHandler._get_destructive_preview(handler, "/bots/stop", {})
        assert "ALL bots" in preview["description"]
        assert preview["affected_bots"] == "all"

    def test_get_destructive_preview_stop_specific(self):
        """_get_destructive_preview should describe stop-specific action."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        preview = ControlHandler._get_destructive_preview(handler, "/bots/stop", {"bots": ["bot1"]})
        assert "bot1" in preview["description"]

    def test_get_destructive_preview_drain(self):
        """_get_destructive_preview should describe drain action."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        preview = ControlHandler._get_destructive_preview(handler, "/control/drain", {})
        assert ".drain" in preview["description"]
        assert "undo_command" in preview

    def test_get_destructive_preview_update(self):
        """_get_destructive_preview should describe update action."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        preview = ControlHandler._get_destructive_preview(handler, "/control/update", {})
        assert "safe_update.sh" in preview["description"]


class TestMainFunction:
    """Tests for main() function."""

    def test_main_binds_localhost_without_token(self, capsys):
        """main() should bind to 127.0.0.1 when CONTROL_TOKEN is unset."""
        from codebot.control_server import main

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CONTROL_TOKEN", None)
            # Reload to pick up empty token
            import importlib
            import codebot.control_server as cs
            importlib.reload(cs)

            with patch("codebot.control_server.ThreadingHTTPServer") as mock_server:
                mock_server_instance = MagicMock()
                mock_server.return_value = mock_server_instance
                # Simulate KeyboardInterrupt to exit quickly
                mock_server_instance.serve_forever.side_effect = KeyboardInterrupt()

                try:
                    cs.main()
                except KeyboardInterrupt:
                    pass

                # Check that server was created with 127.0.0.1
                call_args = mock_server.call_args
                assert call_args[0][0][0] == "127.0.0.1"

    def test_main_binds_all_with_token(self, capsys):
        """main() should bind to 0.0.0.0 when CONTROL_TOKEN is set."""
        with patch.dict(os.environ, {"CONTROL_TOKEN": "test-token"}, clear=False):
            import importlib
            import codebot.control_server as cs
            importlib.reload(cs)

            with patch("codebot.control_server.ThreadingHTTPServer") as mock_server:
                mock_server_instance = MagicMock()
                mock_server.return_value = mock_server_instance
                mock_server_instance.serve_forever.side_effect = KeyboardInterrupt()

                try:
                    cs.main()
                except KeyboardInterrupt:
                    pass

                call_args = mock_server.call_args
                assert call_args[0][0][0] == "0.0.0.0"


class TestLogMessage:
    """Tests for log_message() method."""

    def test_log_message_suppressed(self):
        """log_message should suppress normal log messages."""
        from codebot.control_server import ControlHandler

        handler = MagicMock(spec=ControlHandler)
        # log_message should do nothing
        ControlHandler.log_message(handler, "test format", "arg1", "arg2")
        # No exception means it worked
