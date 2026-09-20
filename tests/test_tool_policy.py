"""Tests for tool_policy module.

Focus: Security boundaries, path resolution, and command blocklisting.
"""

import pytest
from pathlib import Path
from codebot.tool_policy import (
    resolve_workspace_path,
    allowlisted_command,
    validate_command,
    SHELL_METACHARACTERS,
    BLOCKED_COMMANDS,
    DANGEROUS_GIT_ARGS,
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


class TestBlocklistedCommand:
    """Tests for command validation via blocklist."""

    def test_empty_command(self):
        """Empty command returns None."""
        assert allowlisted_command("") is None
        assert allowlisted_command("   ") is None

    def test_shell_metacharacters_allowed(self):
        """Shell metacharacters are allowed (subprocess uses shell=True with cwd sandbox)."""
        cmds = ["ls && pwd", "cat file | grep foo", "echo hello > out.txt", "cd src && pytest"]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked shell command: {cmd}"
            assert result == [cmd]

    def test_blocked_commands_rejected(self):
        """Dangerous commands in BLOCKED_COMMANDS are rejected."""
        for cmd_name in ["sudo", "su", "mkfs", "dd", "shutdown", "reboot", "init",
                         "kill", "killall", "pkill", "apt", "apt-get", "yum", "dnf", "brew",
                         "chmod", "chown", "chgrp"]:
            result = allowlisted_command(f"{cmd_name} something")
            assert result is None, f"Failed to block dangerous command: {cmd_name}"

    def test_normal_commands_allowed(self):
        """Normal commands pass through."""
        cmds = ["ls", "pwd", "date", "echo hello", "cat file.txt", "find . -name '*.py'",
                "wc -l file.txt", "head -n 5 file.txt", "tail file.txt", "cp a b", "mv a b",
                "mkdir new_dir", "touch file.txt", "df -h", "free -m", "sleep 1"]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid command: {cmd}"

    def test_network_commands_allowed(self):
        """Network commands are fully allowed (user decided 'Allow all')."""
        cmds = [
            "curl http://example.com",
            "curl -X POST http://localhost:8000/api",
            "wget http://example.com/file.tar.gz",
            "nc -zv localhost 8080",
            "netcat localhost 9090",
            "socat TCP-LISTEN:8080 STDOUT",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked network command: {cmd}"

    def test_path_traversal_blocked_in_args(self):
        """Arguments containing '..' are blocked."""
        cmds = ["ls ../secret", "cat ../../etc/passwd", "find .. -name x"]
        for cmd in cmds:
            assert allowlisted_command(cmd) is None, f"Failed to block traversal: {cmd}"

    def test_git_normal_commands_allowed(self):
        """All normal git commands pass (no subcommand allowlist)."""
        cmds = [
            "git status",
            "git diff",
            "git log --oneline",
            "git add file.py",
            "git commit -m 'msg'",
            "git push origin main",
            "git pull",
            "git fetch --all",
            "git checkout main",
            "git branch feature-x",
            "git merge develop",
            "git stash",
            "git tag v1.0",
            "git show HEAD",
            "git grep pattern",
            "git rev-parse HEAD",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid git command: {cmd}"

    def test_git_dangerous_args_blocked(self):
        """Git commands with dangerous force/hard args are blocked."""
        cmds = [
            "git push --force",
            "git push -f",
            "git reset --hard",
        ]
        for cmd in cmds:
            assert allowlisted_command(cmd) is None, f"Failed to block dangerous git: {cmd}"

    def test_git_amend_allowed(self):
        """git commit --amend is now allowed (not in blocklist)."""
        result = allowlisted_command("git commit --amend")
        assert result is not None

    def test_git_rebase_allowed(self):
        """git rebase is now allowed (not in blocklist)."""
        result = allowlisted_command("git rebase main")
        assert result is not None

    def test_rm_allowed(self):
        """rm is allowed (not in blocklist; runs within workspace cwd)."""
        assert allowlisted_command("rm file.txt") is not None
        assert allowlisted_command("rm -rf dir") is not None
        assert allowlisted_command("rm -f file.txt") is not None
        assert allowlisted_command("rm -i file") is not None

    def test_pytest_any_args_allowed(self):
        """Pytest with any args passes (no flag allowlist)."""
        cmds = [
            "pytest -q",
            "pytest -x -v",
            "pytest -k test_login",
            "pytest --tb=short",
            "pytest --pdb",
            "pytest -s",
            "pytest --cov=src",
            "pytest -n auto",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid pytest: {cmd}"

    def test_python_commands_allowed(self):
        """Python commands pass through."""
        cmds = [
            "python3 -c 'print(1)'",
            "python3 -m pytest",
            "python script.py",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid python: {cmd}"

    def test_gh_any_subcommand_allowed(self):
        """GH CLI with any subcommand passes (no subcommand allowlist)."""
        cmds = [
            "gh pr list",
            "gh issue create",
            "gh repo view",
            "gh api repos/owner/name",
            "gh pr merge --admin",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked valid gh command: {cmd}"

    def test_pipes_allowed(self):
        """Pipes to any target work (no pipe target allowlist)."""
        cmds = [
            "git log | head -n 5",
            "cat file | grep pattern",
            "ls | sort",
            "find . -name '*.py' | wc -l",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked piped command: {cmd}"

    def test_malformed_command(self):
        """Malformed commands return None."""
        assert allowlisted_command("unclosed 'quote") is None

    def test_validate_command_is_alias(self):
        """validate_command and allowlisted_command are the same function."""
        assert validate_command is allowlisted_command

    def test_workspace_root_path_validation(self, tmp_path):
        """With workspace_root provided, absolute paths outside are blocked."""
        ws = tmp_path / "workspace"
        ws.mkdir()
        # Relative path inside workspace — allowed
        result = validate_command("ls src/file.py", workspace_root=ws)
        assert result is not None
        # Absolute path outside workspace — blocked
        result = validate_command("ls /etc/passwd", workspace_root=ws)
        assert result is None
        # Path traversal — blocked
        result = validate_command("ls ../outside", workspace_root=ws)
        assert result is None
