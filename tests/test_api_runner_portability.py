"""Tests for CB-2561480-3E71: Remove hardcoded Monitor repo paths from api_runner.py.

Verifies that repository detection in _auto_commit is delegated to ProjectAdapter
and no hardcoded Monitor-specific strings remain in api_runner.py.
"""
import pytest
from pathlib import Path
from unittest.mock import MagicMock, patch
import sys
import os

# Ensure codebot is importable
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.project_adapter import ProjectAdapter, ComponentDef


class TestGetReposForPath:
    """Test the new get_repos_for_path adapter method."""

    def test_monitor_adapter_returns_manager_for_manager_path(self):
        from codebot.monitor_adapter import MonitorAdapter
        adapter = MonitorAdapter()
        repos = adapter.get_repos_for_path("/home/user/Work/Monitor-Manager-Python/src/foo.py")
        assert "Monitor-Manager-Python" in repos

    def test_monitor_adapter_returns_client_for_client_path(self):
        from codebot.monitor_adapter import MonitorAdapter
        adapter = MonitorAdapter()
        repos = adapter.get_repos_for_path("/some/root/Monitor-Client-Python/agent/main.py")
        assert "Monitor-Client-Python" in repos

    def test_monitor_adapter_returns_lib_common_for_shared_path(self):
        from codebot.monitor_adapter import MonitorAdapter
        adapter = MonitorAdapter()
        repos = adapter.get_repos_for_path("/root/lib-common/utils.py")
        # lib-common maps to Monitor-Manager-Python per original logic
        assert "Monitor-Manager-Python" in repos

    def test_monitor_adapter_returns_empty_for_unknown_path(self):
        from codebot.monitor_adapter import MonitorAdapter
        adapter = MonitorAdapter()
        repos = adapter.get_repos_for_path("/tmp/random/file.txt")
        assert repos == []

    def test_abstract_method_exists(self):
        """Verify get_repos_for_path is part of the ABC."""
        assert hasattr(ProjectAdapter, 'get_repos_for_path')


class TestAutoCommitPortability:
    """Test that _auto_commit uses adapter instead of hardcoded strings."""

    def test_auto_commit_no_hardcoded_strings(self):
        """Verify api_runner.py source contains no Monitor-specific repo names."""
        runner_path = Path(__file__).parent.parent / "codebot" / "api_runner.py"
        content = runner_path.read_text(encoding="utf-8")
        assert "Monitor-Manager-Python" not in content, \
            "Hardcoded 'Monitor-Manager-Python' found in api_runner.py"
        assert "Monitor-Client-Python" not in content, \
            "Hardcoded 'Monitor-Client-Python' found in api_runner.py"

    def test_auto_commit_uses_adapter_get_repos(self):
        """Verify _auto_commit calls adapter.get_repos_for_path when adapter is set."""
        from codebot import api_runner
        
        mock_adapter = MagicMock(spec=ProjectAdapter)
        mock_adapter.get_repos_for_path.return_value = ["TestRepo"]
        mock_paths = MagicMock()
        mock_paths.repository_root = Path("/test/root")
        mock_adapter.paths.return_value = mock_paths
        
        original_adapter = api_runner._adapter_instance
        try:
            api_runner._adapter_instance = mock_adapter
            
            with patch.object(api_runner, 'bash') as mock_bash:
                mock_bash.return_value = {"success": False, "output": "", "error": "nothing to commit"}
                api_runner._auto_commit("test_bot", ["/test/root/TestRepo/file.py"])
                
                mock_adapter.get_repos_for_path.assert_called()
        finally:
            api_runner._adapter_instance = original_adapter
