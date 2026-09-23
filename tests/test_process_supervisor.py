"""Tests for codebot/process_supervisor.py

Covers:
- UnixProcessSupervisor.restart calls os.execv with correct arguments
- ProcessSupervisor ABC enforces restart() abstract method
- Mock ProcessSupervisor can be injected without triggering actual process replacement
"""

import sys
from unittest.mock import MagicMock, patch

import pytest

from codebot.process_supervisor import (
    ProcessSupervisor,
    UnixProcessSupervisor,
    DefaultProcessSupervisor,
)


class TestUnixProcessSupervisor:
    def test_restart_calls_execv_with_correct_args(self):
        """UnixProcessSupervisor.restart calls os.execv with sys.executable and sys.argv."""
        supervisor = UnixProcessSupervisor()
        with patch("codebot.process_supervisor.os.execv") as mock_execv:
            supervisor.restart()
            mock_execv.assert_called_once_with(sys.executable, [sys.executable] + sys.argv)

    def test_restart_logs_before_execv(self, caplog):
        """UnixProcessSupervisor.restart logs the command before calling execv."""
        import logging
        caplog.set_level(logging.INFO)
        supervisor = UnixProcessSupervisor()
        with patch("codebot.process_supervisor.os.execv"):
            supervisor.restart()
        assert any("Executing self-restart" in record.message for record in caplog.records)


class TestProcessSupervisorABC:
    def test_cannot_instantiate_abc_directly(self):
        """ProcessSupervisor is an ABC; direct instantiation raises TypeError."""
        with pytest.raises(TypeError):
            ProcessSupervisor()  # type: ignore[abstract]

    def test_restart_is_abstract(self):
        """ProcessSupervisor defines restart() as an abstract method."""
        assert hasattr(ProcessSupervisor, "restart")
        # The method should be abstract
        assert getattr(ProcessSupervisor.restart, "__isabstractmethod__", False)

    def test_unix_supervisor_is_subclass(self):
        """UnixProcessSupervisor is a concrete subclass of ProcessSupervisor."""
        assert issubclass(UnixProcessSupervisor, ProcessSupervisor)

    def test_default_alias_is_unix_supervisor(self):
        """DefaultProcessSupervisor is an alias for UnixProcessSupervisor."""
        assert DefaultProcessSupervisor is UnixProcessSupervisor


class TestMockProcessSupervisor:
    def test_mock_supervisor_does_not_call_execv(self):
        """A mock ProcessSupervisor can be used without triggering os.execv."""
        mock_supervisor = MagicMock(spec=ProcessSupervisor)
        mock_supervisor.restart.return_value = None

        # Verify the mock doesn't call execv
        with patch("codebot.process_supervisor.os.execv") as mock_execv:
            mock_supervisor.restart()
            mock_execv.assert_not_called()

        # Verify restart was called on the mock
        mock_supervisor.restart.assert_called_once()

    def test_mock_supervisor_injected_into_state_manager(self):
        """Mock ProcessSupervisor can be injected into state_manager.check_self_restart."""
        from pathlib import Path
        import tempfile

        from codebot.state_manager import check_self_restart

        mock_supervisor = MagicMock(spec=ProcessSupervisor)
        mock_supervisor.restart.return_value = None

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

                    # Our mock's restart should have been called
                    mock_supervisor.restart.assert_called_once()

    def test_mock_supervisor_injected_into_orchestrator_services(self):
        """Mock ProcessSupervisor can be injected into orchestrator_services.check_self_restart."""
        from pathlib import Path
        import tempfile

        from codebot.orchestrator_services import check_self_restart

        mock_supervisor = MagicMock(spec=ProcessSupervisor)
        mock_supervisor.restart.return_value = None

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

                    # Our mock's restart should have been called
                    mock_supervisor.restart.assert_called_once()
            finally:
                os_mod._paths_restart_file = original_restart_file
