"""Regression tests for CB-7300627-9D38: shlex.quote injection protection in _auto_commit.

Verifies that repo_path and commit_msg are safely quoted via shlex.quote in all
git command invocations, preventing shell injection through spaces, quotes,
semicolons, $() payloads, and other metacharacters.
"""
import shlex
import unittest
from unittest.mock import patch, MagicMock

import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))


def _setup_gatekeeper_pass(mock_gk_cls):
    """Configure Gatekeeper mock to return COMPLETE (allows commit)."""
    mock_instance = MagicMock()
    mock_instance.verify_ticket.return_value = {"decision": "COMPLETE"}
    mock_gk_cls.return_value = mock_instance
    return mock_instance


class TestRepoPathShlexQuote(unittest.TestCase):
    """Verify repo_path is safely quoted — spaces and quotes don't split argv."""

    @patch('codebot.api_runner.bash')
    @patch('codebot.api_runner._adapter_instance')
    @patch('codebot.gatekeeper.Gatekeeper')
    def test_repo_path_with_spaces_is_single_arg(self, mock_gk_cls, mock_adapter, mock_bash):
        """repo_path containing spaces must be a single shell argument.

        When WORK_ROOT contains 'my repo' (space), the git -C argument must
        not be split into multiple tokens by the shell.
        """
        _setup_gatekeeper_pass(mock_gk_cls)
        mock_adapter.paths.return_value = MagicMock(
            state_dir='/tmp/state',
            quality_policy=None,
            repository_root='/tmp/my repo',
        )
        # git status returns nothing → no further commands executed, but
        # the status call itself proves quoting correctness
        mock_bash.return_value = {"success": True, "output": "", "error": ""}

        from codebot.api_runner import _auto_commit
        result = _auto_commit("backend_implementer-1",
                              ["/tmp/my repo/Monitor-Manager-Python/x.py"],
                              ticket_id="CB-TEST-1")
        self.assertTrue(result)

        # Verify bash was called (git status)
        self.assertGreaterEqual(mock_bash.call_count, 1)
        cmd = mock_bash.call_args_list[0][0][0]
        # shlex.split should parse the quoted path as a single token
        parts = shlex.split(cmd)
        # git -C <path> status --short → the -C argument is parts[2]
        # repo_path = f"{base}/{repo}" = "/tmp/my repo/Monitor-Manager-Python"
        expected_path = "/tmp/my repo/Monitor-Manager-Python"
        self.assertEqual(parts[2], expected_path,
                         "Path with space must be a single argv element after shlex.split")

    @patch('codebot.api_runner.bash')
    @patch('codebot.api_runner._adapter_instance')
    @patch('codebot.gatekeeper.Gatekeeper')
    def test_repo_path_with_single_quote(self, mock_gk_cls, mock_adapter, mock_bash):
        """repo_path containing a single quote must not break shell quoting.

        shlex.quote uses '\'' escaping for single quotes, which shlex.split
        must correctly round-trip.
        """
        _setup_gatekeeper_pass(mock_gk_cls)
        mock_adapter.paths.return_value = MagicMock(
            state_dir='/tmp/state',
            quality_policy=None,
            repository_root="/tmp/a'b",
        )
        mock_bash.return_value = {"success": True, "output": "", "error": ""}

        from codebot.api_runner import _auto_commit
        result = _auto_commit("backend_implementer-1",
                              ["/tmp/a'b/Monitor-Manager-Python/x.py"],
                              ticket_id="CB-TEST-2")
        self.assertTrue(result)

        cmd = mock_bash.call_args_list[0][0][0]
        parts = shlex.split(cmd)
        # repo_path = f"{base}/{repo}" = "/tmp/a'b/Monitor-Manager-Python"
        expected_path = "/tmp/a'b/Monitor-Manager-Python"
        self.assertEqual(parts[2], expected_path,
                         "Path with single quote must round-trip through shlex.quote/split")

    @patch('codebot.api_runner.bash')
    @patch('codebot.api_runner._adapter_instance')
    @patch('codebot.gatekeeper.Gatekeeper')
    def test_repo_path_with_semicolon_injection(self, mock_gk_cls, mock_adapter, mock_bash):
        """repo_path with semicolon must not execute as separate command.

        A path like '/tmp/a; rm -rf /' must be quoted so the shell sees
        it as a single argument to git -C, not two commands.
        """
        _setup_gatekeeper_pass(mock_gk_cls)
        mock_adapter.paths.return_value = MagicMock(
            state_dir='/tmp/state',
            quality_policy=None,
            repository_root='/tmp/a; rm -rf /',
        )
        mock_bash.return_value = {"success": True, "output": "", "error": ""}

        from codebot.api_runner import _auto_commit
        result = _auto_commit("backend_implementer-1",
                              ["/tmp/a; rm -rf /Monitor-Manager-Python/x.py"],
                              ticket_id="CB-TEST-3")
        self.assertTrue(result)

        cmd = mock_bash.call_args_list[0][0][0]
        parts = shlex.split(cmd)
        # repo_path = f"{base}/{repo}" = "/tmp/a; rm -rf //Monitor-Manager-Python"
        # The dangerous semicolon is inside the quoted path — it's a single argv token
        expected_path = "/tmp/a; rm -rf //Monitor-Manager-Python"
        self.assertEqual(parts[2], expected_path,
                         "Path with semicolon must remain a single argv element")


class TestCommitMsgShlexQuote(unittest.TestCase):
    """Verify commit_msg is safely quoted — injection payloads don't break out."""

    @patch('codebot.api_runner.bash')
    @patch('codebot.api_runner._adapter_instance')
    @patch('codebot.gatekeeper.Gatekeeper')
    def test_commit_msg_with_semicolon_injection_is_quoted(self, mock_gk_cls, mock_adapter, mock_bash):
        """commit_msg with semicolon injection must remain a single -m argument.

        If bot_name is "a'; rm -rf /; echo '", the commit message becomes:
        "bot: a'; rm -rf /; echo ' auto-commit after task completion"
        This must NOT be split into multiple shell tokens.
        """
        _setup_gatekeeper_pass(mock_gk_cls)
        mock_adapter.paths.return_value = MagicMock(
            state_dir='/tmp/state',
            quality_policy=None,
            repository_root='/tmp/repo',
        )

        # git status shows changes, add succeeds, commit succeeds, push succeeds
        mock_bash.side_effect = [
            {"success": True, "output": "M file.py", "error": ""},  # status
            {"success": True, "output": "", "error": ""},           # add
            {"success": True, "output": "", "error": ""},           # commit
            {"success": True, "output": "", "error": ""},           # push
        ]

        from codebot.api_runner import _auto_commit
        evil_bot = "a'; rm -rf /; echo '"
        result = _auto_commit(evil_bot,
                              ["/tmp/repo/Monitor-Manager-Python/x.py"],
                              ticket_id="CB-TEST-4")
        self.assertTrue(result)

        # The commit call should be the third bash call (index 2)
        self.assertGreaterEqual(mock_bash.call_count, 3)
        commit_cmd = mock_bash.call_args_list[2][0][0]

        # Parse the commit command — the -m argument must be the full
        # bot-generated commit message as a SINGLE token
        parts = shlex.split(commit_cmd)
        # Expected: ['git', '-C', '/tmp/repo', 'commit', '-m', '<msg>']
        self.assertEqual(len(parts), 6,
                         f"Commit command must have exactly 6 argv elements, got {len(parts)}: {parts}")
        self.assertEqual(parts[0], 'git')
        self.assertEqual(parts[3], 'commit')
        self.assertEqual(parts[4], '-m')

        # The commit message must be intact as one argument
        expected_msg = f"bot: {evil_bot} auto-commit after task completion"
        self.assertEqual(parts[5], expected_msg,
                         "Commit message with injection payload must be a single -m argument")

    @patch('codebot.api_runner.bash')
    @patch('codebot.api_runner._adapter_instance')
    @patch('codebot.gatekeeper.Gatekeeper')
    def test_commit_msg_with_double_quote_is_quoted(self, mock_gk_cls, mock_adapter, mock_bash):
        """commit_msg with double quotes must not break shell quoting.

        shlex.quote wraps in single quotes, so embedded double quotes are safe.
        """
        _setup_gatekeeper_pass(mock_gk_cls)
        mock_adapter.paths.return_value = MagicMock(
            state_dir='/tmp/state',
            quality_policy=None,
            repository_root='/tmp/repo',
        )
        mock_bash.side_effect = [
            {"success": True, "output": "M file.py", "error": ""},  # status
            {"success": True, "output": "", "error": ""},           # add
            {"success": True, "output": "", "error": ""},           # commit
            {"success": True, "output": "", "error": ""},           # push
        ]

        from codebot.api_runner import _auto_commit
        evil_bot = 'test"bot'
        result = _auto_commit(evil_bot,
                              ["/tmp/repo/Monitor-Manager-Python/x.py"],
                              ticket_id="CB-TEST-5")
        self.assertTrue(result)

        commit_cmd = mock_bash.call_args_list[2][0][0]
        parts = shlex.split(commit_cmd)
        self.assertEqual(len(parts), 6,
                         f"Commit command must have exactly 6 argv elements, got {len(parts)}: {parts}")

        expected_msg = f"bot: {evil_bot} auto-commit after task completion"
        self.assertEqual(parts[5], expected_msg,
                         "Commit message with double quotes must be a single -m argument")

    @patch('codebot.api_runner.bash')
    @patch('codebot.api_runner._adapter_instance')
    @patch('codebot.gatekeeper.Gatekeeper')
    def test_commit_msg_with_dollar_parens_is_quoted(self, mock_gk_cls, mock_adapter, mock_bash):
        """commit_msg with $(cmd) must not be executed as command substitution.

        A payload like '$(id)' in bot_name must remain literal after quoting.
        """
        _setup_gatekeeper_pass(mock_gk_cls)
        mock_adapter.paths.return_value = MagicMock(
            state_dir='/tmp/state',
            quality_policy=None,
            repository_root='/tmp/repo',
        )
        mock_bash.side_effect = [
            {"success": True, "output": "M file.py", "error": ""},  # status
            {"success": True, "output": "", "error": ""},           # add
            {"success": True, "output": "", "error": ""},           # commit
            {"success": True, "output": "", "error": ""},           # push
        ]

        from codebot.api_runner import _auto_commit
        evil_bot = "$(id)"
        result = _auto_commit(evil_bot,
                              ["/tmp/repo/Monitor-Manager-Python/x.py"],
                              ticket_id="CB-TEST-6")
        self.assertTrue(result)

        commit_cmd = mock_bash.call_args_list[2][0][0]
        parts = shlex.split(commit_cmd)

        expected_msg = f"bot: {evil_bot} auto-commit after task completion"
        self.assertEqual(parts[5], expected_msg,
                         "Commit message with $(cmd) must remain literal after shlex.quote")


class TestAllGitCommandsUseShlexQuote(unittest.TestCase):
    """Verify every git command in _auto_commit uses shlex.quote on repo_path."""

    @patch('codebot.api_runner.bash')
    @patch('codebot.api_runner._adapter_instance')
    @patch('codebot.gatekeeper.Gatekeeper')
    def test_all_git_c_args_are_quoted(self, mock_gk_cls, mock_adapter, mock_bash):
        """Every 'git -C <path>' call must have the path safely quoted.

        We inject a dangerous repo_path and verify shlex.split produces
        the correct single token for -C in every call.
        """
        _setup_gatekeeper_pass(mock_gk_cls)
        dangerous_path = "/tmp/path with spaces; echo pwned"
        mock_adapter.paths.return_value = MagicMock(
            state_dir='/tmp/state',
            quality_policy=None,
            repository_root=dangerous_path,
        )
        mock_bash.side_effect = [
            {"success": True, "output": "M file.py", "error": ""},  # status
            {"success": True, "output": "", "error": ""},           # add
            {"success": True, "output": "", "error": ""},           # commit
            {"success": True, "output": "", "error": ""},           # push
        ]

        from codebot.api_runner import _auto_commit
        result = _auto_commit("backend_implementer-1",
                              [f"{dangerous_path}/Monitor-Manager-Python/x.py"],
                              ticket_id="CB-TEST-7")
        self.assertTrue(result)

        # All 4 git calls should have the dangerous path as a single argv token
        expected_full_path = f"{dangerous_path}/Monitor-Manager-Python"
        for i, call_args in enumerate(mock_bash.call_args_list):
            cmd = call_args[0][0]
            parts = shlex.split(cmd)
            # git -C <path> ... → -C is parts[1], path is parts[2]
            self.assertEqual(parts[0], 'git', f"Call {i}: must start with 'git'")
            self.assertEqual(parts[1], '-C', f"Call {i}: second arg must be '-C'")
            self.assertEqual(parts[2], expected_full_path,
                             f"Call {i}: -C path must be the full expected_path as single token, "
                             f"got {parts[2]!r}")


if __name__ == '__main__':
    unittest.main()
