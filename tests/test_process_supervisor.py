"""Tests for codebot/process_supervisor.py.

Covers:
- DefaultProcessSupervisor.restart_self calls os.execv with correct arguments
- Mock ProcessSupervisor can be injected without triggering actual process replacement
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from codebot.process_supervisor import DefaultProcessSupervisor, ProcessSupervisor


class TestDefaultProcessSupervisor:
    def test_restart_self_calls_execv_with_correct_args(self):
        """DefaultProcessSupervisor.restart_self calls os.execv with sys.executable and sys.argv."""
        supervisor = DefaultProcessSupervisor()
        with patch("codebot.process_supervisor.os.execv") as mock_execv:
            supervisor.restart_self()
            mock_execv.assert_called_once_with(sys.executable, [sys.executable] + sys.argv)

    def test_restart_self_logs_before_execv(self, caplog):
        """DefaultProcessSupervisor.restart_self logs the command before calling execv."""
        import logging
        caplog.set_level(logging.INFO)
        supervisor = DefaultProcessSupervisor()
        with patch("codebot.process_supervisor.os.execv"):
            supervisor.restart_self()
        assert any("Executing self-restart" in record.message for record in caplog.records)


class TestMockProcessSupervisor:
    def test_mock_supervisor_does_not_call_execv(self):
        """A mock ProcessSupervisor can be used without triggering os.execv."""
        mock_supervisor = MagicMock(spec=ProcessSupervisor)
        mock_supervisor.restart_self.return_value = None

        # Verify the mock doesn't call execv
        with patch("codebot.process_supervisor.os.execv") as mock_execv:
            mock_supervisor.restart_self()
            mock_execv.assert_not_called()

        # Verify restart_self was called on the mock
        mock_supervisor.restart_self.assert_called_once()

    def test_mock_supervisor_injected_into_state_manager(self):
        """Mock ProcessSupervisor can be injected into state_manager.check_self_restart."""
        from pathlib import Path
        import tempfile
        import json

        from codebot.state_manager import check_self_restart

        mock_supervisor = MagicMock(spec=ProcessSupervisor)
        mock_supervisor.restart_self.return_value = None

        # Create a temporary restart file to trigger the restart path
        with tempfile.TemporaryDirectory() as tmpdir:
            restart_file = Path(tmpdir) / ".restart"
            restart_file.write_text("test")

            # Create mock paths
            mock_paths = MagicMock()
            mock_paths.restart_file = restart_file

            # Create mock bots (empty dict for simplicity)
            bots = {}

            # Mock stop_fn
            stop_fn = MagicMock()

            with patch("codebot.state_manager.get_paths", return_value=mock_paths):
                with patch("codebot.process_supervisor.os.execv") as mock_execv:
                    # Call with supervisor kwarg
                    result = check_self_restart(bots, stop_fn, supervisor=mock_supervisor)

                    # Should return True (restart triggered)
                    assert result is True

                    # execv should NOT have been called
                    mock_execv.assert_not_called()

                    # Our mock's restart_self should have been called
                    mock_supervisor.restart_self.assert_called_once()

    def test_mock_supervisor_injected_into_orchestrator_services(self):
        """Mock ProcessSupervisor can be injected into orchestrator_services.check_self_restart."""
        from pathlib import Path
        import tempfile

        from codebot.orchestrator_services import check_self_restart

        mock_supervisor = MagicMock(spec=ProcessSupervisor)
        mock_supervisor.restart_self.return_value = None

        # Create a temporary restart file to trigger the restart path
        with tempfile.TemporaryDirectory() as tmpdir:
            restart_file = Path(tmpdir) / ".restart"
            restart_file.write_text("test")

            # Patch the module-level _paths_restart_file
            import codebot.orchestrator_services as os_mod
            original_restart_file = os_mod._paths_restart_file

            try:
                os_mod._paths_restart_file = restart_file

                # Create mock bots (empty dict for simplicity)
                bots = {}

                with patch("codebot.process_supervisor.os.execv") as mock_execv:
                    # Call with supervisor kwarg
                    result = check_self_restart(bots, supervisor=mock_supervisor)

                    # Should return True (restart triggered)
                    assert result is True

                    # execv should NOT have been called
                    mock_execv.assert_not_called()

                    # Our mock's restart_self should have been called
                    mock_supervisor.restart_self.assert_called_once()
            finally:
                os_mod._paths_restart_file = original_restart_file


class TestProcessSupervisorProtocol:
    def test_default_supervisor_implements_protocol(self):
        """DefaultProcessSupervisor satisfies the ProcessSupervisor Protocol."""
        supervisor = DefaultProcessSupervisor()
        # hasattr check for structural subtyping
        assert hasattr(supervisor, "restart_self")
        assert callable(supervisor.restart_self)
