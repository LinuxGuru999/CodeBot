"""Adversarial tests for CB-2195514-9B12: Gatekeeper fail-closed enforcement in _auto_commit.
Also covers CB-9254952-EE3B: Gatekeeper exception must BLOCK commit, never allow it.
"""
import unittest
from unittest.mock import patch, MagicMock
import sys
import os
import logging

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

class TestAutoCommitGatekeeperFailClosed(unittest.TestCase):
    """Verify _auto_commit blocks commits when Gatekeeper fails or errors out."""

    def setUp(self):
        self.mock_adapter_patcher = patch('codebot.api_runner._adapter_instance')
        self.mock_bash_patcher = patch('codebot.api_runner.bash')
        self.mock_adapter = self.mock_adapter_patcher.start()
        self.mock_bash = self.mock_bash_patcher.start()
        
        self.mock_adapter.paths.return_value = MagicMock(
            state_dir='/tmp/state',
            quality_policy=None,
            repository_root='/tmp/repo'
        )
        
        self.addCleanup(self.mock_adapter_patcher.stop)
        self.addCleanup(self.mock_bash_patcher.stop)

    @patch('codebot.gatekeeper.Gatekeeper')
    def test_gatekeeper_exception_blocks_commit(self, mock_gk_cls):
        """If Gatekeeper raises, commit must be blocked (fail-closed).
        
        CB-9254952-EE3B: This is the primary scenario — a buggy or malicious
        change that crashes the gatekeeper must NOT bypass quality gates.
        """
        mock_instance = MagicMock()
        mock_instance.verify_ticket.side_effect = RuntimeError("Gatekeeper DB corrupted")
        mock_gk_cls.return_value = mock_instance
        from codebot.api_runner import _auto_commit

        import io
        import contextlib
        log_capture = io.StringIO()
        with contextlib.redirect_stdout(log_capture):
            result = _auto_commit("test-bot", ["Monitor-Manager-Python/test.py"], ticket_id="CB-TEST-123")
        self.assertFalse(result, "Commit must be blocked when gatekeeper raises an exception")
        self.assertTrue(mock_instance.verify_ticket.called,
                        "verify_ticket must be called — proves RuntimeError path was exercised")
        output = log_capture.getvalue()
        self.assertIn("blocking commit", output.lower(),
                       "Log must contain 'blocking commit' when gatekeeper raises exception")
        for call in self.mock_bash.call_args_list:
            cmd = str(call)
            self.assertNotIn("git add", cmd, "No git add should execute when gatekeeper crashes")
            self.assertNotIn("git commit", cmd, "No git commit should execute when gatekeeper crashes")
            self.assertNotIn("git push", cmd, "No git push should execute when gatekeeper crashes")
        self.mock_bash.assert_not_called(), "No git operations should execute when gatekeeper crashes"

    @patch('codebot.gatekeeper.Gatekeeper')
    def test_gatekeeper_exception_logs_blocking(self, mock_gk_cls):
        """When gatekeeper raises, log must say 'BLOCKING commit' not 'allowing commit'.
        
        CB-9254952-EE3B: Verifies the log message reflects the fail-closed policy.
        """
        mock_gk_cls.side_effect = RuntimeError("Gatekeeper DB corrupted")
        from codebot.api_runner import _auto_commit
        from codebot.api_runner import _log
        import io
        import contextlib
        
        log_capture = io.StringIO()
        with contextlib.redirect_stdout(log_capture):
            _auto_commit("test_bot", ["codebot/api_runner.py"])
        
        output = log_capture.getvalue()
        self.assertIn("BLOCKING commit", output,
                       "Log must say 'BLOCKING commit' when gatekeeper raises exception")
        self.assertNotIn("allowing commit", output,
                          "Log must NOT say 'allowing commit' — that was the old unsafe behavior")

    @patch('codebot.gatekeeper.Gatekeeper')
    def test_gatekeeper_rework_blocks_commit(self, mock_gk_cls):
        """If Gatekeeper returns REWORK, commit must be blocked."""
        mock_instance = MagicMock()
        mock_instance.verify_ticket.return_value = {"decision": "REWORK", "failed_gates": ["pytest"]}
        mock_gk_cls.return_value = mock_instance
        from codebot.api_runner import _auto_commit
        
        result = _auto_commit("test_bot", ["codebot/api_runner.py"])
        self.assertFalse(result, "Commit must be blocked when gatekeeper returns REWORK")
        self.mock_bash.assert_not_called(), "No git operations should execute when gatekeeper returns REWORK"

    @patch('codebot.gatekeeper.Gatekeeper')
    def test_gatekeeper_pass_allows_commit(self, mock_gk_cls):
        """If Gatekeeper returns COMPLETE, commit proceeds normally."""
        mock_instance = MagicMock()
        mock_instance.verify_ticket.return_value = {"decision": "COMPLETE"}
        mock_gk_cls.return_value = mock_instance
        self.mock_bash.return_value = {"success": True, "output": "M file.py", "error": ""}
        from codebot.api_runner import _auto_commit
        
        result = _auto_commit("test_bot", ["/tmp/repo/Monitor-Manager-Python/codebot/api_runner.py"], ticket_id="CB-TEST-GK")
        # bash should have been called for git status at minimum (repo matched)
        self.assertGreaterEqual(self.mock_bash.call_count, 1,
                                 "git operations should execute when gatekeeper returns COMPLETE")

    @patch('codebot.gatekeeper.Gatekeeper')
    def test_gatekeeper_import_error_blocks_commit(self, mock_gk_cls):
        """If Gatekeeper cannot be imported, commit must be blocked (fail-closed)."""
        # Force ImportError when Gatekeeper is used
        mock_gk_cls.side_effect = ImportError("no module named 'codebot.gatekeeper'")
        from codebot.api_runner import _auto_commit
        
        result = _auto_commit("test_bot", ["codebot/api_runner.py"])
        self.assertFalse(result, "Commit must be blocked when gatekeeper import fails")
        self.mock_bash.assert_not_called(), "No git operations should execute when gatekeeper is unavailable"

    def test_adapter_none_blocks_commit(self):
        """If adapter is None (gatekeeper unavailable), commit must be blocked."""
        # Set adapter to None
        import codebot.api_runner as api_runner
        original = api_runner._adapter_instance
        try:
            api_runner._adapter_instance = None
            from codebot.api_runner import _auto_commit
            
            result = _auto_commit("test_bot", ["codebot/api_runner.py"])
            self.assertFalse(result, "Commit must be blocked when adapter is None")
            self.mock_bash.assert_not_called()
        finally:
            api_runner._adapter_instance = original

    @patch('codebot.gatekeeper.Gatekeeper')
    def test_gatekeeper_syntax_error_blocks_commit(self, mock_gk_cls):
        """If gatekeeper policy file is unparseable (SyntaxError), commit must be blocked.
        
        CB-9254952-EE3B: This is the exact attack vector — modifying quality_gates.yaml
        to be unparseable should NOT bypass quality gates.
        """
        mock_gk_cls.side_effect = SyntaxError("invalid syntax in quality_gates.yaml")
        from codebot.api_runner import _auto_commit
        
        result = _auto_commit("test_bot", ["codebot/api_runner.py"])
        self.assertFalse(result, "Commit must be blocked when gatekeeper raises SyntaxError")
        self.mock_bash.assert_not_called()

if __name__ == '__main__':
    unittest.main()
