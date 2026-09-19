"""Adversarial tests for CB-2195514-9B12: Gatekeeper fail-closed enforcement in _auto_commit."""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestAutoCommitGatekeeperFailClosed(unittest.TestCase):
    """Verify _auto_commit blocks commits when Gatekeeper fails or errors out."""

    def setUp(self):
        self.mock_adapter_patcher = patch('codebot.api_runner._adapter_instance')
        self.mock_run_patcher = patch('codebot.api_runner.subprocess.run')
        self.mock_adapter = self.mock_adapter_patcher.start()
        self.mock_run = self.mock_run_patcher.start()
        
        self.mock_adapter.paths.return_value = MagicMock(
            state_dir='/tmp/state',
            quality_policy=None,
            repository_root='/tmp/repo'
        )
        
        self.addCleanup(self.mock_adapter_patcher.stop)
        self.addCleanup(self.mock_run_patcher.stop)

    @patch('codebot.gatekeeper.Gatekeeper')
    def test_gatekeeper_exception_blocks_commit(self, mock_gk_cls):
        """If Gatekeeper raises, commit must be blocked (fail-closed)."""
        mock_gk_cls.side_effect = RuntimeError("Gatekeeper DB corrupted")
        from codebot.api_runner import _auto_commit
        
        result = _auto_commit("test_bot", ["codebot/api_runner.py"])
        self.assertFalse(result)
        self.mock_run.assert_not_called()

    @patch('codebot.gatekeeper.Gatekeeper')
    def test_gatekeeper_rework_blocks_commit(self, mock_gk_cls):
        """If Gatekeeper returns REWORK, commit must be blocked."""
        mock_instance = MagicMock()
        mock_instance.verify_ticket.return_value = {"decision": "REWORK", "failed_gates": ["pytest"]}
        mock_gk_cls.return_value = mock_instance
        from codebot.api_runner import _auto_commit
        
        result = _auto_commit("test_bot", ["codebot/api_runner.py"])
        self.assertFalse(result)
        self.mock_run.assert_not_called()

    @patch('codebot.gatekeeper.Gatekeeper')
    def test_gatekeeper_pass_allows_commit(self, mock_gk_cls):
        """If Gatekeeper returns COMPLETE, commit proceeds normally."""
        mock_instance = MagicMock()
        mock_instance.verify_ticket.return_value = {"decision": "COMPLETE"}
        mock_gk_cls.return_value = mock_instance
        self.mock_run.return_value = MagicMock(returncode=0)
        from codebot.api_runner import _auto_commit
        
        result = _auto_commit("test_bot", ["codebot/api_runner.py"])
        self.assertTrue(result)
        self.assertGreaterEqual(self.mock_run.call_count, 1)

if __name__ == '__main__':
    unittest.main()
