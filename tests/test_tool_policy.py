"""Tests for tool_policy module.

Focus: Security boundaries, path resolution, and command allowlisting.
"""

import pytest
from pathlib import Path
from codebot.tool_policy import (
    resolve_workspace_path,
    allowlisted_command,
    SHELL_METACHARACTERS,
    ALLOWED_PIPE_TARGETS,
    DANGEROUS_GIT_ARGS,
    DANGEROUS_GH_ARGS,
)


class TestResolveWorkspacePath:
    """Tests for path resolution security."""

    @pytest.fixture
    def workspace_root(self, tmp_path):
        return tmp_path / "workspace"

    def test_resolve_valid_relative_path(self, workspace_root):
        """Valid relative path resolves correctly."""
        workspace_root.mkdir()
        (workspace_root / "src").mkdir()
        result = resolve_workspace_path("src/file.py", workspace_root)
        assert result == (workspace_root / "src" / "file.py").resolve()

    def test_resolve_valid_absolute_path(self, workspace_root):
        """Valid absolute path inside workspace resolves correctly."""
        workspace_root.mkdir()
        (workspace_root / "src").mkdir()
        abs_path = str((workspace_root / "src" / "file.py").resolve())
        result = resolve_workspace_path(abs_path, workspace_root)
        assert result == (workspace_root / "src" / "file.py").resolve()

    def test_resolve_escape_via_relative(self, workspace_root):
        """Relative path escaping workspace returns None."""
        workspace_root.mkdir()
        result = resolve_workspace_path("../escape.txt", workspace_root)
        assert result is None

    def test_resolve_escape_via_absolute(self, workspace_root):
        """Absolute path outside workspace returns None."""
        workspace_root.mkdir()
        result = resolve_workspace_path("/etc/passwd", workspace_root)
        assert result is None

    def test_resolve_nonexistent_path_inside(self, workspace_root):
        """Non-existent path inside workspace is allowed (strict=False)."""
        workspace_root.mkdir()
        result = resolve_workspace_path("new_file.txt", workspace_root)
        assert result is not None
        assert result.parent == workspace_root


class TestAllowlistedCommand:
    """Tests for command parsing and allowlisting."""

    def test_empty_command(self):
        """Empty command returns None."""
        assert allowlisted_command("") is None
        assert allowlisted_command("   ") is None

    def test_shell_metacharacters_blocked(self):
        """Commands with shell metacharacters are blocked."""
        for char in SHELL_METACHARACTERS:
            cmd = f"ls {char} file"
            assert allowlisted_command(cmd) is None, f"Failed to block char: {char}"

    def test_allowed_bare_commands(self):
        """Simple allowed commands pass through."""
        cmds = ["ls", "pwd", "date", "echo hello", "cat file.txt"]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid command: {cmd}"
            assert result == cmd.split() or result[0] == cmd.split()[0]

    def test_path_traversal_blocked_in_args(self):
        """Arguments containing '..' are blocked."""
        cmds = ["ls ../secret", "cat ../../etc/passwd", "find .. -name x"]
        for cmd in cmds:
            assert allowlisted_command(cmd) is None, f"Failed to block traversal: {cmd}"

    def test_git_allowed_subcommands(self):
        """Allowed git subcommands pass."""
        cmds = [
            "git status",
            "git diff",
            "git log --oneline",
            "git add file.py",
            "git commit -m 'msg'",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid git command: {cmd}"

    def test_git_dangerous_args_blocked(self):
        """Dangerous git args are blocked."""
        cmds = [
            "git push --force",
            "git reset --hard",
            "git rebase main",
            "git commit --amend",
        ]
        for cmd in cmds:
            assert allowlisted_command(cmd) is None, f"Failed to block dangerous git: {cmd}"

    def test_git_c_flag_validation(self):
        """Git -C flag validates path safety."""
        assert allowlisted_command("git -C safe/path status") is not None
        assert allowlisted_command("git -C ../unsafe status") is None

    def test_rm_flags_restricted(self):
        """rm only allows -f, blocks other flags."""
        assert allowlisted_command("rm -f file.txt") is not None
        assert allowlisted_command("rm -rf dir") is None
        assert allowlisted_command("rm -i file") is None

    def test_pytest_allowed_args(self):
        """Pytest with allowed args passes."""
        cmds = [
            "pytest -q",
            "pytest -x -v",
            "pytest -k test_login",
            "pytest --tb=short",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid pytest: {cmd}"

    def test_pytest_disallowed_args(self):
        """Pytest with disallowed args blocked."""
        assert allowlisted_command("pytest --pdb") is None
        assert allowlisted_command("pytest -s") is None

    def test_python_allowed_flags(self):
        """Python with allowed flags passes."""
        cmds = [
            "python3 -c 'print(1)'",
            "python3 -m pytest",
            "py_compile file.py",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid python: {cmd}"

    def test_gh_allowed_subcommands(self):
        """GH CLI allowed subcommands pass."""
        cmds = [
            "gh pr list",
            "gh issue create",
            "gh repo view",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid gh: {cmd}"

    def test_gh_dangerous_args_blocked(self):
        """GH CLI dangerous args blocked."""
        cmds = [
            "gh api -X DELETE /repos",
            "gh pr merge --admin",
            "gh repo delete --hostname evil.com",
        ]
        for cmd in cmds:
            assert allowlisted_command(cmd) is None, f"Failed to block dangerous gh: {cmd}"

    def test_pipe_allowed_targets(self):
        """Pipes to allowed targets work."""
        cmd = "git log | head -n 5"
        result = allowlisted_command(cmd)
        assert result is not None
        assert "|" in result
        assert result[result.index("|") + 1] == "head"

    def test_pipe_disallowed_targets(self):
        """Pipes to disallowed targets blocked."""
        cmd = "git log | bash"
        assert allowlisted_command(cmd) is None
        cmd = "cat file | rm -rf /"
        assert allowlisted_command(cmd) is None

    def test_malformed_command(self):
        """Malformed commands return None."""
        assert allowlisted_command("unclosed 'quote") is None

    def test_unknown_command(self):
        """Unknown commands are blocked."""
        assert allowlisted_command("curl http://example.com") is None
        assert allowlisted_command("wget file") is None
