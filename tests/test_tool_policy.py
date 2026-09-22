"""Tests for tool_policy module.

Focus: Security boundaries, path resolution, and command blocklisting.
"""

import subprocess
from unittest.mock import patch, MagicMock

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
from codebot.api_tools import bash


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

    def test_shell_metacharacters_blocked(self):
        """Shell control operators are blocked to prevent command chaining."""
        cmds = ["ls && pwd", "echo hello > out.txt", "cd src && pytest"]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is None, f"Should block shell control command: {cmd}"

    def test_pipes_allowed_without_workspace_root(self):
        """Pipes without workspace_root still work (no path validation)."""
        cmds = ["cat file | grep foo", "ls | sort"]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is not None, f"Blocked pipe command: {cmd}"
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

    def test_python_c_blocked(self):
        """python3 -c with arbitrary code is rejected to prevent command injection."""
        cmds = [
            "python3 -c 'import os; os.system(\"id\")'",
            "python3 -c 'print(1)'",
            "python -c 'pass'",
            "python3 -c \"import subprocess; subprocess.run(['ls'])\"",
        ]
        for cmd in cmds:
            result = allowlisted_command(cmd)
            assert result is None, f"Should block python -c: {cmd}"

    def test_python_m_and_script_allowed(self):
        """python3 -m and python script.py still work."""
        cmds = [
            "python3 -m pytest",
            "python script.py",
            "python3 -m http.server",
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


class TestPipeEdgeCases:
    """Tests for pipe handling edge cases in allowlisted_command."""

    def test_pipe_empty_left_side_blocked(self):
        """Pipe with empty left side should be blocked."""
        assert allowlisted_command("| ls") is None
        assert allowlisted_command("  | cat file") is None

    def test_pipe_empty_right_side_blocked(self):
        """Pipe with empty right side should be blocked."""
        assert allowlisted_command("ls |") is None
        assert allowlisted_command("cat file |  ") is None

    def test_multiple_consecutive_pipes_handled(self):
        """Multiple pipes should be handled correctly if all segments valid."""
        result = allowlisted_command("ls | grep foo | head -n 5")
        assert result is not None
        assert result == ["ls | grep foo | head -n 5"]

    def test_multiple_pipes_with_empty_segment_blocked(self):
        """Multiple pipes with empty segment in middle should be blocked."""
        assert allowlisted_command("ls | | head") is None
        assert allowlisted_command("cat file |  | grep foo") is None

    def test_pipe_as_first_token_blocked(self):
        """Command starting with pipe should be blocked."""
        assert allowlisted_command("| grep foo") is None

    def test_pipe_as_last_token_blocked(self):
        """Command ending with pipe should be blocked."""
        assert allowlisted_command("ls |") is None

    def test_path_traversal_in_right_side_args_blocked(self):
        """Path traversal beyond simple '..' in right-side args should be blocked."""
        assert allowlisted_command("ls | cat ../../etc/passwd") is None
        assert allowlisted_command("echo foo | grep ../../../secret") is None

    def test_absolute_path_in_pipe_without_workspace_allowed(self):
        """Without workspace_root, absolute paths in pipes are not blocked (no context)."""
        # When no workspace_root is provided, path validation is skipped
        result = allowlisted_command("ls | cat /etc/passwd")
        assert result is not None

    def test_non_allowed_pipe_target_blocked(self):
        """Pipe targets that are blocklisted commands should be blocked."""
        assert allowlisted_command("ls | sudo rm -rf /") is None
        assert allowlisted_command("cat file | chmod 777 /etc") is None
        assert allowlisted_command("echo foo | kill 1234") is None

    def test_recursive_left_side_validation(self):
        """Left-side of pipe must also pass all validations recursively."""
        # Blocked command on left side
        assert allowlisted_command("sudo ls | grep foo") is None
        # Path traversal on left side
        assert allowlisted_command("cat ../secret | grep foo") is None
        # Dangerous git on left side
        assert allowlisted_command("git push --force | head") is None

    def test_complex_pipe_chain_all_valid(self):
        """Complex valid pipe chain should pass."""
        cmd = "find . -name '*.py' | grep -v test | wc -l"
        result = allowlisted_command(cmd)
        assert result is not None
        assert result == [cmd]

    def test_pipe_with_shell_metacharacters_in_segments(self):
        """Pipe segments containing shell metacharacters should be blocked."""
        assert allowlisted_command("ls | grep $(whoami)") is None
        assert allowlisted_command("cat file | echo `id`") is None


class TestBareCommandSandboxEscape:
    """Tests for sandbox escape prevention on file-operating bare commands.

    Verifies that absolute paths outside the workspace are rejected for
    cat, ls, find, cp, mv, rm, head, tail, wc, touch, mkdir when
    workspace_root is provided.
    """

    @pytest.fixture
    def ws(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src").mkdir()
        (workspace / "src" / "file.txt").write_text("hello")
        return workspace

    def test_cat_etc_passwd_denied(self, ws):
        """cat /etc/passwd must be denied."""
        assert validate_command("cat /etc/passwd", workspace_root=ws) is None

    def test_rm_absolute_outside_denied(self, ws):
        """rm -f /etc/file must be denied."""
        assert validate_command("rm -f /etc/file", workspace_root=ws) is None
        assert validate_command("rm -rf /tmp/foo", workspace_root=ws) is None

    def test_ls_root_denied(self, ws):
        """ls / must be denied."""
        assert validate_command("ls /", workspace_root=ws) is None
        assert validate_command("ls /etc", workspace_root=ws) is None

    def test_find_outside_denied(self, ws):
        """find /etc -name passwd must be denied."""
        assert validate_command("find /etc -name passwd", workspace_root=ws) is None
        assert validate_command("find / -type f", workspace_root=ws) is None

    def test_cp_outside_denied(self, ws):
        """cp with absolute path outside workspace denied."""
        assert validate_command("cp /etc/passwd /tmp/stolen", workspace_root=ws) is None

    def test_mv_outside_denied(self, ws):
        """mv with absolute path outside workspace denied."""
        assert validate_command("mv /etc/shadow /tmp/", workspace_root=ws) is None

    def test_head_tail_wc_outside_denied(self, ws):
        """head/tail/wc with absolute paths outside workspace denied."""
        assert validate_command("head /etc/passwd", workspace_root=ws) is None
        assert validate_command("tail -n 5 /var/log/syslog", workspace_root=ws) is None
        assert validate_command("wc -l /etc/hosts", workspace_root=ws) is None

    def test_touch_mkdir_outside_denied(self, ws):
        """touch/mkdir with absolute paths outside workspace denied."""
        assert validate_command("touch /tmp/backdoor", workspace_root=ws) is None
        assert validate_command("mkdir /opt/evil", workspace_root=ws) is None

    def test_symlink_escape_denied(self, ws):
        """Paths resolving outside workspace via symlinks are blocked."""
        # Create a symlink pointing outside the workspace
        link_path = ws / "escape_link"
        link_path.symlink_to("/etc")
        # resolve_workspace_path uses .resolve() which follows symlinks
        # So accessing /workspace/escape_link/passwd resolves to /etc/passwd
        assert validate_command("cat escape_link/passwd", workspace_root=ws) is None

    def test_symlink_component_denied(self, ws):
        """Paths containing symlink components are denied even if they resolve inside workspace.
        
        This prevents TOCTOU race conditions where a symlink could be swapped
        between validation and execution to escape the sandbox.
        """
        # Create a valid file inside workspace
        target_file = ws / "src" / "real_file.txt"
        target_file.write_text("secret data")
        
        # Create a symlink INSIDE workspace pointing to another file INSIDE workspace
        symlink_path = ws / "src" / "link_to_real"
        symlink_path.symlink_to(target_file)
        
        # Even though the symlink resolves to a valid workspace path,
        # it must be denied because the path contains a symlink component
        assert validate_command(f"cat src/link_to_real", workspace_root=ws) is None
        assert validate_command(f"cat {symlink_path}", workspace_root=ws) is None
        
        # Also test with directory symlinks inside workspace
        subdir = ws / "real_subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("data")
        dir_symlink = ws / "linked_subdir"
        dir_symlink.symlink_to(subdir)
        
        assert validate_command("cat linked_subdir/file.txt", workspace_root=ws) is None
        assert validate_command(f"ls {dir_symlink}", workspace_root=ws) is None

    def test_glob_symlink_bypass_denied(self, ws):
        """Glob characters in path positions are denied to prevent shell expansion smuggling symlinks.
        
        Shell expansion of *, ?, [], {}, ~ can expand to paths containing symlinks,
        bypassing the symlink component check during validation.
        """
        # Glob patterns should be denied for file-operating commands
        assert validate_command("cat *", workspace_root=ws) is None
        assert validate_command("ls ?.txt", workspace_root=ws) is None
        assert validate_command("cat [abc].txt", workspace_root=ws) is None
        assert validate_command("ls {a,b}.txt", workspace_root=ws) is None
        assert validate_command("cat ~/file.txt", workspace_root=ws) is None
        
        # Verify legitimate non-glob commands still work
        assert validate_command("cat src/file.txt", workspace_root=ws) is not None
        assert validate_command("ls src/", workspace_root=ws) is not None

    def test_tilde_expansion_outside_workspace_denied(self, ws):
        """Tilde expansion to paths outside workspace must be rejected.

        Shell expansion of ~ resolves to user home directory which is outside
        the workspace boundary. The validation layer must deny ~ in path
        positions before shell expansion occurs.
        """
        # Tilde alone expands to home directory (outside workspace)
        assert validate_command("cat ~/file.txt", workspace_root=ws) is None
        assert validate_command("ls ~", workspace_root=ws) is None
        assert validate_command("head ~/../etc/passwd", workspace_root=ws) is None
        assert validate_command("find ~ -name secret", workspace_root=ws) is None
        # Tilde with username also denied
        assert validate_command("cat ~root/.ssh/id_rsa", workspace_root=ws) is None
        assert validate_command("ls ~admin/logs", workspace_root=ws) is None

    def test_glob_pattern_within_workspace_accepted(self, ws):
        """Glob patterns within workspace are rejected per security policy.

        Current implementation rejects ALL glob characters (*, ?, [], {}) in
        path positions for file-operating commands, even when they would only
        match workspace files. This prevents shell expansion from smuggling
        symlinks past validation. Note: find -name patterns are NOT path
        positions and are allowed (they are pattern arguments, not file paths).
        Legitimate non-glob workspace operations continue to work.
        """
        # Glob patterns are denied in path positions
        assert validate_command("cat src/*.txt", workspace_root=ws) is None
        assert validate_command("ls ?.py", workspace_root=ws) is None
        assert validate_command("rm [abc].txt", workspace_root=ws) is None
        assert validate_command("cp *.txt dst/", workspace_root=ws) is None
        # find -name patterns are NOT path positions — they are pattern arguments
        # and are allowed by design (the pattern is matched by find, not expanded by shell)
        assert validate_command("find . -name '*.log'", workspace_root=ws) is not None
        # But equivalent non-glob workspace operations remain allowed
        assert validate_command("cat src/file.txt", workspace_root=ws) is not None
        assert validate_command("ls src/", workspace_root=ws) is not None
        assert validate_command("find . -name 'file.txt'", workspace_root=ws) is not None

    def test_brace_expansion_rejected(self, ws):
        """Brace expansion attempts must be rejected.

        Brace expansion ({a,b}, {1..5}) can generate arbitrary path strings
        that bypass validation. All brace characters in path positions are
        denied for file-operating commands.
        """
        # Simple brace expansion
        assert validate_command("cat {a,b}.txt", workspace_root=ws) is None
        assert validate_command("ls {src,lib}/", workspace_root=ws) is None
        # Nested brace expansion
        assert validate_command("cat {{a,b},{c,d}}.txt", workspace_root=ws) is None
        # Brace expansion with paths
        assert validate_command("rm /tmp/{evil,backdoor}", workspace_root=ws) is None
        assert validate_command("cp {src,dst}/file.txt out/", workspace_root=ws) is None
        # Range expansion syntax
        assert validate_command("ls file{1..10}.txt", workspace_root=ws) is None

    def test_mixed_expansion_scenarios(self, ws):
        """Mixed expansion scenarios combining multiple shell features are rejected.

        Combinations of tilde, glob, and brace expansions create complex attack
        vectors. All such combinations in path positions must be denied.
        Note: find -name pattern arguments are not path positions.
        Also covers --key=value glob smuggling and -f flag glob denial.
        """
        # Tilde + glob in path positions
        assert validate_command("cat ~/src/*.py", workspace_root=ws) is None
        assert validate_command("ls ~/*.{txt,md}", workspace_root=ws) is None
        # Glob + brace in path positions
        assert validate_command("cat {src,lib}/*.py", workspace_root=ws) is None
        assert validate_command("cp {a,b}.txt dst/", workspace_root=ws) is None
        # Tilde + brace in path positions
        assert validate_command("cat ~/{Documents,Downloads}/secret", workspace_root=ws) is None
        # All three combined in path positions
        assert validate_command("ls ~/{src,lib}/*.[ch]", workspace_root=ws) is None
        # Mixed with path traversal
        assert validate_command("cat ~/../etc/{passwd,shadow}", workspace_root=ws) is None
        # find with tilde in path position (start path) is denied
        assert validate_command("find ~ -name '*.py'", workspace_root=ws) is None
        # find -name pattern args are NOT path positions — allowed
        assert validate_command("find . -name '*.{js,ts}'", workspace_root=ws) is not None
        # --key=value glob smuggling denied (covers line 231-232)
        assert validate_command("grep --file=*.txt pattern", workspace_root=ws) is None
        assert validate_command("awk --source=~/evil '{print}' file", workspace_root=ws) is None
        assert validate_command("sed --expression=*/etc/passwd", workspace_root=ws) is None
        # -f flag with glob chars in value denied (covers skip_next_is_path glob check)
        assert validate_command("grep -f *.patterns target.txt", workspace_root=ws) is None
        assert validate_command("awk -f ~/scripts/evil.awk data", workspace_root=ws) is None
        # --key=value with path outside workspace denied
        assert validate_command("grep --file=/etc/passwd pattern", workspace_root=ws) is None
        # --key=value with .. traversal denied
        assert validate_command("grep --file=../secret pattern", workspace_root=ws) is None
        # --key=value pattern-supplier flags set pattern_skipped (covers lines 243-250)
        # grep --regexp=value marks pattern as consumed, next token is path
        assert validate_command("grep --regexp=foo src/file.txt", workspace_root=ws) is not None
        # sed --expression=value marks pattern as consumed
        assert validate_command("sed --expression=s/a/b/ src/file.txt", workspace_root=ws) is not None
        # awk --source=value marks pattern as consumed
        assert validate_command("awk --source=1 src/file.txt", workspace_root=ws) is not None
        # grep -e=value marks pattern as consumed
        assert validate_command("grep -e=pattern src/file.txt", workspace_root=ws) is not None

    def test_workspace_relative_commands_allowed(self, ws):
        """Legitimate workspace operations continue to function."""
        assert validate_command("cat src/file.txt", workspace_root=ws) is not None
        assert validate_command("ls src/", workspace_root=ws) is not None
        assert validate_command("find . -name '*.py'", workspace_root=ws) is not None
        assert validate_command("cp src/file.txt src/backup.txt", workspace_root=ws) is not None
        assert validate_command("mv src/file.txt src/moved.txt", workspace_root=ws) is not None
        assert validate_command("rm src/file.txt", workspace_root=ws) is not None
        assert validate_command("head -n 5 src/file.txt", workspace_root=ws) is not None
        assert validate_command("wc -l src/file.txt", workspace_root=ws) is not None
        assert validate_command("touch new_file.txt", workspace_root=ws) is not None
        assert validate_command("mkdir new_dir", workspace_root=ws) is not None

    def test_absolute_path_inside_workspace_allowed(self, ws):
        """Absolute paths inside workspace are allowed."""
        abs_file = str((ws / "src" / "file.txt").resolve())
        assert validate_command(f"cat {abs_file}", workspace_root=ws) is not None
        abs_dir = str((ws / "src").resolve())
        assert validate_command(f"ls {abs_dir}", workspace_root=ws) is not None

    def test_bare_command_no_args_allowed(self, ws):
        """Bare commands without path args still work."""
        assert validate_command("ls", workspace_root=ws) is not None
        assert validate_command("pwd", workspace_root=ws) is not None

    def test_non_file_commands_not_affected(self, ws):
        """Commands not in FILE_OPERATING_COMMANDS are unaffected by bare command check."""
        # echo is not a file-operating bare command
        assert validate_command("echo hello", workspace_root=ws) is not None

    def test_grep_etc_passwd_denied(self, ws):
        """grep . /etc/passwd must be denied (acceptance criterion 1)."""
        assert validate_command("grep . /etc/passwd", workspace_root=ws) is None

    def test_sed_shadow_denied(self, ws):
        """sed -n p /etc/shadow must be denied (acceptance criterion 2)."""
        assert validate_command("sed -n p /etc/shadow", workspace_root=ws) is None

    def test_awk_passwd_denied(self, ws):
        """awk '{print}' /etc/passwd must be denied (acceptance criterion 3)."""
        assert validate_command("awk '{print}' /etc/passwd", workspace_root=ws) is None

    def test_grep_workspace_allowed(self, ws):
        """grep/sed/awk with workspace-relative paths remain allowed (regression)."""
        assert validate_command("grep pattern src/file.txt", workspace_root=ws) is not None
        assert validate_command("sed s/a/b/ src/file.txt", workspace_root=ws) is not None
        assert validate_command("awk '{print $1}' src/file.txt", workspace_root=ws) is not None

    def test_piped_bare_commands_validated_per_segment(self, ws):
        """Each pipe segment is independently validated for bare commands."""
        # Valid pipe chain
        assert validate_command("find . -name '*.py' | wc -l", workspace_root=ws) is not None
        # Escape attempt in right segment
        assert validate_command("ls src | cat /etc/passwd", workspace_root=ws) is None
        # Escape attempt in left segment
        assert validate_command("cat /etc/passwd | grep root", workspace_root=ws) is None

    def test_find_exec_blocked(self, ws):
        """find -exec is blocked to prevent arbitrary command execution."""
        assert validate_command("find . -name test.txt -exec cat /etc/passwd \\;", workspace_root=ws) is None
        assert validate_command("find . -exec rm {} \\;", workspace_root=ws) is None


class TestShellInterpreterBlocking:
    """Tests for blocking shell interpreter invocation via flags and file paths."""

    @pytest.fixture
    def ws(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        return workspace

    def test_bash_script_path_blocked(self, ws):
        """bash /tmp/exploit.sh must be denied."""
        assert validate_command("bash /tmp/exploit.sh", workspace_root=ws) is None
        assert validate_command("bash ./script.sh", workspace_root=ws) is None

    def test_sh_script_path_blocked(self, ws):
        """sh ./script.sh must be denied."""
        assert validate_command("sh ./script.sh", workspace_root=ws) is None

    def test_bash_interactive_flag_blocked(self, ws):
        """bash -i must be denied."""
        assert validate_command("bash -i", workspace_root=ws) is None

    def test_bash_complex_flags_blocked(self, ws):
        """bash --noprofile -c 'cat /etc/passwd' must be denied."""
        assert validate_command("bash --noprofile -c 'cat /etc/passwd'", workspace_root=ws) is None

    def test_zsh_flag_blocked(self, ws):
        """zsh -c 'cat /etc/passwd' must be denied."""
        assert validate_command("zsh -c 'cat /etc/passwd'", workspace_root=ws) is None

    def test_perl_script_path_blocked(self, ws):
        """perl /tmp/x.pl must be denied."""
        assert validate_command("perl /tmp/x.pl", workspace_root=ws) is None

    def test_python_c_still_blocked(self, ws):
        """python3 -c must remain blocked."""
        assert validate_command("python3 -c 'import os'", workspace_root=ws) is None

    def test_perl_e_blocked(self, ws):
        """perl -e must be blocked."""
        assert validate_command("perl -e 'print 1'", workspace_root=ws) is None

    def test_legitimate_bash_version_allowed(self, ws):
        """bash --version should be allowed."""
        assert validate_command("bash --version", workspace_root=ws) is not None

    def test_legitimate_python_m_allowed(self, ws):
        """python3 -m pytest should be allowed."""
        assert validate_command("python3 -m pytest", workspace_root=ws) is not None

    def test_legitimate_git_status_allowed(self, ws):
        """git status should be allowed."""
        assert validate_command("git status", workspace_root=ws) is not None

    def test_legitimate_ls_allowed(self, ws):
        """ls should be allowed."""
        assert validate_command("ls src/", workspace_root=ws) is not None

    def test_find_execdir_blocked(self, ws):
        """find -execdir is blocked: executes commands from matched file dirs."""
        assert validate_command("find . -execdir sh -c \"cat /etc/passwd\" \\;", workspace_root=ws) is None
        assert validate_command("find . -execdir rm {} \\;", workspace_root=ws) is None
        assert validate_command("find . -name '*.py' -execdir ls {} \\;", workspace_root=ws) is None

    def test_find_ok_blocked(self, ws):
        """find -ok is blocked: prompts then executes arbitrary commands."""
        assert validate_command("find . -ok rm {} \\;", workspace_root=ws) is None
        assert validate_command("find . -name test.txt -ok cat {} \\;", workspace_root=ws) is None

    def test_find_okdir_blocked(self, ws):
        """find -okdir is blocked: prompts then executes from matched file dirs."""
        assert validate_command("find . -okdir ls \\;", workspace_root=ws) is None
        assert validate_command("find . -okdir rm {} \\;", workspace_root=ws) is None

    def test_find_exec_variants_blocked_without_workspace(self):
        """All find command-execution actions blocked even without workspace_root."""
        assert validate_command("find . -exec rm {} \\;") is None
        assert validate_command("find . -execdir sh -c \"cat /etc/passwd\" \\;") is None
        assert validate_command("find . -ok rm {} \\;") is None
        assert validate_command("find . -okdir ls \\;") is None

    def test_find_exec_variants_blocked_in_pipes(self, ws):
        """find execution actions blocked in any pipeline segment."""
        assert validate_command("find . -execdir ls {} \\; | wc -l", workspace_root=ws) is None
        assert validate_command("find . -ok rm {} \\; | wc -l", workspace_root=ws) is None
        assert validate_command("find . -okdir ls \\; | wc -l", workspace_root=ws) is None


class TestBashToolSandboxBoundary:
    """End-to-end tests for bash() sandbox enforcement.

    Verifies that the bash() tool function properly enforces sandbox boundaries
    by testing through the actual tool interface, not just validate_command().
    """

    @pytest.fixture
    def ws(self, tmp_path):
        """Create a temporary workspace with a src directory and file."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src").mkdir()
        (workspace / "src" / "file.txt").write_text("hello world")
        return workspace

    @pytest.fixture(autouse=True)
    def patch_workspace_root(self, ws):
        """Patch WORKSPACE_ROOT in api_tools to use our temp workspace."""
        with patch("codebot.api_tools.WORKSPACE_ROOT", ws):
            yield ws

    def test_bash_cat_etc_passwd_denied(self):
        """bash('cat /etc/passwd') must be denied end-to-end."""
        result = bash("cat /etc/passwd")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_rm_outside_denied(self):
        """bash('rm -f /etc/file') must be denied end-to-end."""
        result = bash("rm -f /etc/file")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_ls_root_denied(self):
        """bash('ls /') must be denied end-to-end."""
        result = bash("ls /")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_find_root_denied(self):
        """bash('find / -name x') must be denied end-to-end."""
        result = bash("find / -name x")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_symlink_denied(self, ws):
        """bash() must deny commands accessing paths via symlink escape."""
        # Create a symlink pointing outside the workspace
        link_path = ws / "escape_link"
        link_path.symlink_to("/etc")
        result = bash("cat escape_link/passwd")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_traversal_denied(self):
        """bash() must deny path traversal attempts."""
        result = bash("cat ../etc/passwd")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_workspace_relative_allowed(self, ws):
        """bash('cat src/file.txt') succeeds with mocked subprocess."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "hello world"
        mock_result.stderr = ""
        with patch("codebot.api_tools.subprocess.run", return_value=mock_result) as mock_run:
            result = bash("cat src/file.txt")
            assert result["success"] is True
            assert result["error"] is None
            mock_run.assert_called_once()
            call_kwargs = mock_run.call_args
            assert call_kwargs[1]["cwd"] == ws
