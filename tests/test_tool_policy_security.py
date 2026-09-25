"""Comprehensive security test suite for sandbox escape vectors in tool_policy.

This module validates that the sandbox boundary enforced by tool_policy.py
correctly blocks identified escape vectors:
1. Path smuggling via --key=value syntax
2. Workspace root None rejection for absolute paths
3. Glob/tilde/brace expansion edge cases
4. TOCTOU symlink swap simulation
5. python -m module allowlist enforcement
6. Shell metacharacter injection in bare command arguments

These tests are regression guards for security fixes and ensure that
vulnerabilities do not re-emerge silently.
"""

import os
import pytest
from pathlib import Path
from codebot.tool_policy import validate_command, resolve_workspace_path


class TestShellMetacharacterInjection:
    """Tests for shell metacharacter injection in bare command arguments.

    Attackers may try to inject shell metacharacters into command arguments
    to perform command injection. The validator must reject any argument
    containing metacharacters like ;|&$`\\<>!{} when embedded in tokens.
    
    Acceptance Criteria for CB-09948:
    - Command with 'file; rm' returns None
    - Command with '$(id)' returns None  
    - Legitimate paths without metacharacters pass
    - Unit tests verify each blocked pattern
    """

    @pytest.fixture
    def ws(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src").mkdir()
        (workspace / "src" / "file.txt").write_text("hello")
        return workspace

    def test_semicolon_injection_returns_none(self, ws):
        """Command with semicolon embedded in argument must return None (CB-09948 AC)."""
        # Semicolon embedded in a quoted/escaped argument should be rejected
        assert validate_command("cat 'file;rm'", workspace_root=ws) is None
        assert validate_command('cat "file;rm"', workspace_root=ws) is None
        assert validate_command("cat file\\;rm", workspace_root=ws) is None

    def test_dollar_paren_injection_returns_none(self, ws):
        """Command with '$(id)' must return None (CB-09948 AC)."""
        # Note: $(...) is already blocked by the SHELL_CONTROL_TOKENS check
        # but we also block $ embedded in tokens via EMBEDDED_METACHARACTERS
        assert validate_command("cat $(id)", workspace_root=ws) is None
        assert validate_command("cat name$(id).txt", workspace_root=ws) is None

    def test_legitimate_paths_pass(self, ws):
        """Legitimate paths without metacharacters must pass (CB-09948 AC)."""
        result = validate_command("cat src/file.txt", workspace_root=ws)
        assert result is not None
        result2 = validate_command("ls -la src", workspace_root=ws)
        assert result2 is not None

    def test_pipe_injection_blocked(self, ws):
        """Pipe character embedded in argument must be blocked."""
        assert validate_command("cat 'file|rm'", workspace_root=ws) is None
        assert validate_command('cat "name|evil.txt"', workspace_root=ws) is None

    def test_ampersand_injection_blocked(self, ws):
        """Ampersand embedded in argument must be blocked."""
        assert validate_command("cat 'file&rm'", workspace_root=ws) is None
        assert validate_command('cat "name&evil.txt"', workspace_root=ws) is None

    def test_backtick_injection_blocked(self, ws):
        """Backtick embedded in argument must be blocked."""
        assert validate_command("cat `id`", workspace_root=ws) is None
        assert validate_command("cat 'name`id`.txt'", workspace_root=ws) is None

    def test_backslash_injection_blocked(self, ws):
        """Backslash embedded in argument must be blocked."""
        assert validate_command("cat 'file\\x00'", workspace_root=ws) is None

    def test_less_than_injection_blocked(self, ws):
        """Less-than embedded in argument must be blocked."""
        assert validate_command("cat 'file<evil'", workspace_root=ws) is None

    def test_greater_than_injection_blocked(self, ws):
        """Greater-than embedded in argument must be blocked."""
        assert validate_command("cat 'file>evil'", workspace_root=ws) is None

    def test_exclamation_injection_blocked(self, ws):
        """Exclamation embedded in argument must be blocked."""
        assert validate_command("cat 'file!evil'", workspace_root=ws) is None

    def test_brace_injection_blocked(self, ws):
        """Brace characters embedded in argument must be blocked."""
        assert validate_command("cat 'file{evil}'", workspace_root=ws) is None
        assert validate_command("cat '{a,b}.txt'", workspace_root=ws) is None

    def test_standalone_chaining_allowed(self, ws):
        """Standalone chaining operators && ; | must still be allowed."""
        # These are allowed as standalone tokens for command chaining
        result = validate_command("ls && cat file.txt", workspace_root=ws)
        assert result is not None
        result2 = validate_command("ls ; cat file.txt", workspace_root=ws)
        assert result2 is not None
        result3 = validate_command("ls | cat", workspace_root=ws)
        assert result3 is not None

    def test_flag_value_with_metachar_blocked(self, ws):
        """Flag values containing metacharacters must be blocked."""
        assert validate_command("cat '--output=file;rm'", workspace_root=ws) is None
        assert validate_command('cat "--name=file|evil"', workspace_root=ws) is None
        # Note: grep --regexp='a|b' with pipe in regex pattern is a legitimate use case
        # and is allowed when the pattern is properly quoted. The EMBEDDED_METACHARACTERS
        # check only blocks metachars in non-flag tokens. Flag values are checked separately.
        # This test verifies that unquoted metachars in flag values are blocked:
        assert validate_command("cat --output='file;rm'", workspace_root=ws) is None


class TestPathSmuggling:
    """Tests for path smuggling via command-line flags like --key=value.

    Attackers may try to smuggle absolute paths into file-operating commands
    by hiding them behind flags that look like options (e.g., cat --file=/etc/passwd).
    The validator must treat these as paths if they appear in path positions.
    """

    @pytest.fixture
    def ws(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src").mkdir()
        (workspace / "src" / "file.txt").write_text("hello")
        return workspace

    def test_key_value_path_smuggling_cat(self, ws):
        """cat --file=/etc/passwd must be denied."""
        assert validate_command("cat --file=/etc/passwd", workspace_root=ws) is None

    def test_double_dash_path_smuggling(self, ws):
        """cat -- /etc/passwd must be denied (end-of-options separator)."""
        # The -- separator tells the command to stop parsing flags.
        # Everything after -- is a path. /etc/passwd is outside workspace.
        assert validate_command("cat -- /etc/passwd", workspace_root=ws) is None

    def test_long_flag_with_absolute_path(self, ws):
        """ls --directory=/etc must be denied."""
        assert validate_command("ls --directory=/etc", workspace_root=ws) is None

    def test_mixed_smuggling_in_pipe(self, ws):
        """Pipe segment with smuggled path must be denied."""
        assert validate_command("ls | cat -- /etc/shadow", workspace_root=ws) is None


class TestWorkspaceRootNoneRejection:
    """Tests for workspace_root=None rejection of absolute paths.

    Even when no workspace root is provided, file-operating commands should
    not be allowed to access arbitrary absolute paths on the host system.
    This prevents sandbox escape when the workspace context is missing or
    explicitly set to None.
    """

    def test_cat_etc_passwd_no_workspace(self):
        """cat /etc/passwd must be denied even without workspace_root."""
        assert validate_command("cat /etc/passwd", workspace_root=None) is None

    def test_ls_root_no_workspace(self):
        """ls / must be denied even without workspace_root."""
        assert validate_command("ls /", workspace_root=None) is None

    def test_find_etc_no_workspace(self):
        """find /etc -name passwd must be denied even without workspace_root."""
        assert validate_command("find /etc -name passwd", workspace_root=None) is None

    def test_relative_traversal_no_workspace(self):
        """cat ../../etc/passwd must be denied even without workspace_root."""
        assert validate_command("cat ../../etc/passwd", workspace_root=None) is None

    def test_legitimate_relative_allowed_no_workspace(self):
        """cat file.txt should be allowed when no workspace_root is set (no path validation)."""
        # Without workspace_root, we only block absolute paths and traversal for file ops.
        # Relative paths without .. are allowed because we can't resolve them securely.
        assert validate_command("cat file.txt", workspace_root=None) is not None

    def test_legitimate_command_no_workspace(self):
        """ls -la should be allowed when no workspace_root is set."""
        assert validate_command("ls -la", workspace_root=None) is not None


class TestGlobExpansionEdgeCases:
    """Tests for glob, tilde, and brace expansion edge cases.

    Shell expansion characters (*, ?, [], {}, ~) in path positions can be
    used to smuggle symlinks or expand to unexpected paths. These must be
    denied for file-operating commands to prevent shell expansion from
    bypassing path validation.
    """

    @pytest.fixture
    def ws(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src").mkdir()
        (workspace / "src" / "file.txt").write_text("hello")
        (workspace / "src" / "other.txt").write_text("world")
        return workspace

    def test_star_glob_denied(self, ws):
        """cat * must be denied due to glob character."""
        assert validate_command("cat *", workspace_root=ws) is None

    def test_question_mark_glob_denied(self, ws):
        """ls ?.txt must be denied due to glob character."""
        assert validate_command("ls ?.txt", workspace_root=ws) is None

    def test_bracket_glob_denied(self, ws):
        """cat [abc].txt must be denied due to glob character."""
        assert validate_command("cat [abc].txt", workspace_root=ws) is None

    def test_brace_expansion_denied(self, ws):
        """ls {a,b}.txt must be denied due to brace expansion character."""
        assert validate_command("ls {a,b}.txt", workspace_root=ws) is None

    def test_tilde_expansion_denied(self, ws):
        """cat ~/file.txt must be denied due to tilde expansion."""
        assert validate_command("cat ~/file.txt", workspace_root=ws) is None

    def test_glob_in_middle_denied(self, ws):
        """cat src/*.txt must be denied due to glob character."""
        assert validate_command("cat src/*.txt", workspace_root=ws) is None

    def test_legitimate_no_glob_allowed(self, ws):
        """cat src/file.txt must be allowed (no glob chars)."""
        assert validate_command("cat src/file.txt", workspace_root=ws) is not None

    def test_legitimate_find_with_quoted_glob_allowed(self, ws):
        """find . -name '*.py' should be allowed if * is in pattern position (skipped)."""
        # The current implementation skips args after -name, so *.py is not checked for glob chars.
        # This is correct because -name expects a pattern.
        assert validate_command("find . -name '*.py'", workspace_root=ws) is not None

    def test_tilde_in_home_dir_denied(self, ws):
        """ls ~ must be denied."""
        assert validate_command("ls ~", workspace_root=ws) is None


class TestTOCTOUMitigation:
    """Tests for Time-of-Check-Time-of-Use (TOCTOU) symlink swap mitigation.

    An attacker could create a symlink inside the workspace pointing to a
    sensitive file, pass validation, then swap the symlink to point elsewhere
    before execution. To mitigate this, we deny any path component that is
    a symlink within the workspace boundary.
    """

    @pytest.fixture
    def ws(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src").mkdir()
        (workspace / "src" / "real_file.txt").write_text("secret data")
        return workspace

    def test_symlink_component_in_workspace_denied(self, ws):
        """Path containing a symlink component inside workspace must be denied."""
        # Create a symlink inside workspace pointing to a real file inside workspace
        link_path = ws / "src" / "link_to_real"
        link_path.symlink_to(ws / "src" / "real_file.txt")
        
        # Accessing via symlink should be denied
        assert validate_command("cat src/link_to_real", workspace_root=ws) is None

    def test_directory_symlink_in_workspace_denied(self, ws):
        """Path traversing a directory symlink inside workspace must be denied."""
        # Create a real directory
        real_dir = ws / "real_subdir"
        real_dir.mkdir()
        (real_dir / "file.txt").write_text("data")
        
        # Create a symlink to the directory inside workspace
        dir_link = ws / "linked_subdir"
        dir_link.symlink_to(real_dir)
        
        # Accessing file via directory symlink should be denied
        assert validate_command("cat linked_subdir/file.txt", workspace_root=ws) is None

    def test_symlink_pointing_outside_workspace_denied(self, ws):
        """Symlink inside workspace pointing outside must be denied."""
        # Create a symlink inside workspace pointing to /etc
        link_path = ws / "escape_link"
        link_path.symlink_to("/etc")
        
        # Accessing via this symlink should be denied
        assert validate_command("cat escape_link/passwd", workspace_root=ws) is None

    def test_dangling_symlink_denied(self, ws):
        """Dangling symlink inside workspace must be denied."""
        # Create a symlink pointing to non-existent target
        link_path = ws / "dangling_link"
        link_path.symlink_to("/nonexistent/path")
        
        # Accessing via dangling symlink should be denied
        assert validate_command("cat dangling_link", workspace_root=ws) is None

    def test_chain_of_symlinks_denied(self, ws):
        """Chain of symlinks inside workspace must be denied."""
        # Create first symlink
        link1 = ws / "link1"
        link1.symlink_to(ws / "link2")
        
        # Create second symlink
        link2 = ws / "link2"
        link2.symlink_to(ws / "src" / "real_file.txt")
        
        # Accessing via chain should be denied
        assert validate_command("cat link1", workspace_root=ws) is None

    def test_legitimate_non_symlink_path_allowed(self, ws):
        """Legitimate path without symlinks must be allowed."""
        assert validate_command("cat src/real_file.txt", workspace_root=ws) is not None

    def test_resolve_workspace_path_detects_symlink(self, ws):
        """resolve_workspace_path must return None for paths with symlink components."""
        link_path = ws / "src" / "link_to_real"
        link_path.symlink_to(ws / "src" / "real_file.txt")
        
        result = resolve_workspace_path(str(link_path), ws)
        assert result is None

    def test_resolve_workspace_path_allows_normal_path(self, ws):
        """resolve_workspace_path must allow normal paths."""
        result = resolve_workspace_path("src/real_file.txt", ws)
        assert result is not None
        assert result.exists()

    def test_resolve_workspace_path_outside_returns_none(self, ws):
        """resolve_workspace_path must return None for paths outside workspace."""
        result = resolve_workspace_path("/etc/passwd", ws)
        assert result is None

    def test_resolve_workspace_path_symlink_error_handling(self, ws):
        """resolve_workspace_path must handle OSError during symlink check."""
        from unittest.mock import patch
        # Mock Path.is_symlink to raise OSError
        with patch('pathlib.Path.is_symlink', side_effect=OSError("Permission denied")):
            result = resolve_workspace_path("src/file.txt", ws)
            assert result is None


class TestCoverageEdgeCases:
    """Tests to ensure 100% coverage of tool_policy.py branches."""

    @pytest.fixture
    def ws(self, tmp_path):
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        (workspace / "src").mkdir()
        (workspace / "src" / "file.txt").write_text("hello")
        return workspace

    def test_empty_argv_validation(self, ws):
        """_validate_bare_command_paths returns True for empty argv."""
        from codebot.tool_policy import _validate_bare_command_paths
        assert _validate_bare_command_paths([], ws) is True

    def test_non_file_operating_command_validation(self, ws):
        """_validate_bare_command_paths returns True for non-file commands."""
        from codebot.tool_policy import _validate_bare_command_paths
        assert _validate_bare_command_paths(["echo", "hello"], ws) is True

    def test_flag_equals_glob_smuggling(self, ws):
        """--flag=*glob* must be denied."""
        assert validate_command("cat --output=*.txt", workspace_root=ws) is None

    def test_flag_equals_traversal_smuggling(self, ws):
        """--flag=../path must be denied."""
        assert validate_command("cat --file=../etc/passwd", workspace_root=ws) is None

    def test_flag_equals_valid_path(self, ws):
        """--flag=/workspace/path must be allowed if inside workspace."""
        abs_path = str((ws / "src" / "file.txt").resolve())
        assert validate_command(f"cat --file={abs_path}", workspace_root=ws) is not None

    def test_grep_pattern_skipping(self, ws):
        """grep -e pattern file should skip pattern for validation."""
        assert validate_command("grep -e 'foo/bar' src/file.txt", workspace_root=ws) is not None

    def test_sed_script_skipping(self, ws):
        """sed -e script file should skip script for validation."""
        assert validate_command("sed -e 's/foo/bar/' src/file.txt", workspace_root=ws) is not None

    def test_awk_program_skipping(self, ws):
        """awk -f program file should skip program for validation."""
        # Create a dummy awk program file
        prog_file = ws / "prog.awk"
        prog_file.write_text("{print}")
        assert validate_command("awk -f prog.awk src/file.txt", workspace_root=ws) is not None

    def test_git_dangerous_args_blocked(self, ws):
        """Git with --force or --hard must be blocked."""
        assert validate_command("git push --force", workspace_root=ws) is None
        assert validate_command("git reset --hard", workspace_root=ws) is None

    def test_pipeline_empty_segment_blocked(self, ws):
        """Pipeline with empty segment must be blocked."""
        assert validate_command("ls | | cat", workspace_root=ws) is None
        assert validate_command("| ls", workspace_root=ws) is None
        assert validate_command("ls |", workspace_root=ws) is None

    def test_workspace_root_none_absolute_path_blocked(self, ws):
        """Absolute paths for file ops blocked even with workspace_root=None."""
        assert validate_command("cat /etc/passwd", workspace_root=None) is None

    def test_workspace_root_none_traversal_blocked(self, ws):
        """Traversal paths for file ops blocked even with workspace_root=None."""
        assert validate_command("cat ../etc/passwd", workspace_root=None) is None

    def test_find_exec_in_pipeline_blocked(self, ws):
        """find -exec in pipeline segment must be blocked."""
        assert validate_command("ls | find . -exec cat {} \\;", workspace_root=ws) is None

    def test_python_m_allowlist_blocked(self, ws):
        """python -m http.server must be blocked (not in allowlist)."""
        assert validate_command("python3 -m http.server", workspace_root=ws) is None
        assert validate_command("python -m venv /tmp/evil", workspace_root=ws) is None

    def test_python_m_allowlist_allowed(self, ws):
        """python -m pytest must be allowed (in allowlist)."""
        result = validate_command("python3 -m pytest tests/", workspace_root=ws)
        assert result is not None
        result2 = validate_command("python -m json.tool file.json", workspace_root=ws)
        assert result2 is not None

    def test_validate_command_empty_string(self):
        """Empty command returns None."""
        assert validate_command("") is None
        assert validate_command("   ") is None

    def test_validate_command_malformed_quote(self):
        """Malformed command returns None."""
        assert validate_command("unclosed '") is None

    def test_validate_command_blocked_cmd_sudo(self, ws):
        """sudo is blocked."""
        assert validate_command("sudo ls", workspace_root=ws) is None

    def test_validate_command_python_c_blocked(self, ws):
        """python3 -c is blocked."""
        assert validate_command("python3 -c 'import os'", workspace_root=ws) is None

    def test_validate_command_python_m_allowed(self, ws):
        """python3 -m pytest is allowed."""
        assert validate_command("python3 -m pytest", workspace_root=ws) is not None

    def test_validate_command_bash_script_blocked(self, ws):
        """bash script.sh is blocked."""
        assert validate_command("bash script.sh", workspace_root=ws) is None

    def test_validate_command_legitimate_ls(self, ws):
        """ls is allowed."""
        assert validate_command("ls", workspace_root=ws) is not None
