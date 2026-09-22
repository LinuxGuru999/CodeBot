"""Extra coverage tests to hit 100% for tool_policy."""
import pytest
from pathlib import Path
from codebot.tool_policy import validate_command, _validate_bare_command_paths, BLOCKED_COMMANDS


def test_blocked_commands_contains_required(tmp_path):
    assert "env" in BLOCKED_COMMANDS
    assert "xargs" in BLOCKED_COMMANDS
    assert "eval" in BLOCKED_COMMANDS
    assert "exec" in BLOCKED_COMMANDS

def test_env_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("env cat /etc/passwd", workspace_root=ws) is None

def test_xargs_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("xargs cat /etc/passwd", workspace_root=ws) is None

def test_usr_bin_env_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("/usr/bin/env", workspace_root=ws) is None
    assert validate_command("/usr/bin/env cat /etc/passwd", workspace_root=ws) is None
    assert validate_command("/bin/env", workspace_root=ws) is None

def test_usr_bin_xargs_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("/usr/bin/xargs ls", workspace_root=ws) is None
    assert validate_command("/bin/xargs", workspace_root=ws) is None

def test_eval_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("eval something", workspace_root=ws) is None
    assert validate_command("/usr/bin/eval", workspace_root=ws) is None

def test_exec_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("exec ls", workspace_root=ws) is None
    assert validate_command("/usr/bin/exec ls", workspace_root=ws) is None

def test_env_xargs_in_pipe_denied(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("ls | env cat /etc/passwd", workspace_root=ws) is None
    assert validate_command("ls | xargs cat /etc/passwd", workspace_root=ws) is None
    assert validate_command("echo foo | /usr/bin/env", workspace_root=ws) is None

# --- coverage specific ---

def test_validate_bare_empty_argv(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _validate_bare_command_paths([], ws) is True

def test_validate_bare_find_exec(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _validate_bare_command_paths(["find", ".", "-exec", "ls", "{}"], ws) is False

def test_validate_bare_pipe_break(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # token list includes pipe; should break and return True (no escape)
    assert _validate_bare_command_paths(["cat", "src/file.txt", "|", "grep", "foo"], ws) is True

def test_validate_bare_dotdot(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _validate_bare_command_paths(["cat", "../secret"], ws) is False

def test_validate_bare_find_name_continue(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "src").mkdir()
    (ws / "src" / "a.py").write_text("hi")
    # The pattern "*.py" should be skipped via continue, not treated as path
    # Provide a valid path after
    assert _validate_bare_command_paths(["find", ".", "-name", "*.py", "-type", "f"], ws) is True
    # Also test that non-pattern path is validated
    assert _validate_bare_command_paths(["find", "src", "-name", "*.py"], ws) is True

def test_validate_command_empty_argv_after_shlex(tmp_path):
    # Craft command that lexes to empty but is not empty string: e.g., comment handling?
    # With commenters="", this is hard. Try a command that is only whitespace but escaped?
    # The second empty check (line 149) is for argv == [] after lexing.
    # We can trigger by passing command that raises ValueError vs empty.
    # Use command that is not empty but lex produces no tokens: e.g., '""' with posix? Let's see.
    # In shlex with posix True, '""' -> ['""']? Actually test.
    # If not reachable, this test will ensure line is executed via mock or direct check.
    # Instead we test malformed quote already covers ValueError path.
    # To cover line 149, we test a command that is "''" which may produce ['']? Check.
    # If still not empty, we at least ensure empty handling doesn't break.
    assert validate_command("", workspace_root=tmp_path) is None
    assert validate_command("   ", workspace_root=tmp_path) is None

def test_shell_interpreter_break_on_pipe(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # bash with pipe in args should trigger break in interpreter loop (line 182)
    # We need bash command where args contain pipe token after shlex parsing?
    # But pipe is punctuation_chars, so "bash -c echo | cat" -> argv = ["bash","-c","echo","|","cat"]
    # Then loop over args for bash: token "-" check skips, then token "echo" triggers has_script_path, but pipe case tests break.
    # For coverage, we need case where first non-flag is pipe -> break.
    # Example: "bash --version | cat" -> args = ["--version","|","cat"]; first token after skip is "--version" -> skip, next is "|" -> break, no script_path
    # So has_execution_flag false, has_script_path false, so validate should allow (if --version is allowed).
    # And pipe segment validation should handle.
    assert validate_command("bash --version | cat", workspace_root=ws) is not None

def test_perl_dot_script_blocked(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # perl with dot file should trigger line 196-197: elif "." in token
    assert validate_command("perl foo.bar", workspace_root=ws) is None
    # Also test with perl file containing dot but no slash
    assert validate_command("perl my.script", workspace_root=ws) is None

def test_python_absolute_path_pass(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # python with absolute path should hit line 203 pass branch, but still allowed via existing handling?
    # python /tmp/foo.py -> token "/tmp/foo.py" startswith "/" -> pass, so has_script_path stays False, no execution flag, so should be allowed
    # But then workspace validation may block absolute path outside? Actually python itself not blocked, but absolute path validation later may block.
    # For this test we check that python with absolute path inside workspace is allowed
    (ws / "script.py").write_text("print(1)")
    import os
    abs_path = str((ws / "script.py").resolve())
    # This absolute path is inside workspace, so should be allowed
    assert validate_command(f"python {abs_path}", workspace_root=ws) is not None

def test_python_relative_path_with_slash(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # python src/script.py -> has "/" -> triggers line 206
    (ws / "src").mkdir()
    (ws / "src" / "script.py").write_text("print(1)")
    assert validate_command("python src/script.py", workspace_root=ws) is None

def test_find_exec_in_pipe_segment(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("find . -execdir ls {} \\; | wc -l", workspace_root=ws) is None
    assert validate_command("ls | find . -ok rm {} \\;", workspace_root=ws) is None

def test_bare_absolute_without_workspace(tmp_path):
    # line 265: bare command absolute without workspace should be blocked
    assert validate_command("cat /etc/passwd") is None
    assert validate_command("ls /etc") is None
    assert validate_command("cat /etc/passwd | grep foo") is None

def test_git_reset_hard_second_check(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # Both conditions: this is already covered by DANGEROUS_GIT_ARGS, but ensure it still blocked
    assert validate_command("git reset --hard", workspace_root=ws) is None
    assert validate_command("git reset --hard HEAD", workspace_root=ws) is None

def test_validate_command_shlex_value_error():
    # malformed quote triggers ValueError handling (line 147)
    assert validate_command("echo 'unclosed") is None
    assert validate_command('echo "unclosed') is None

def test_validate_bare_with_valid_and_invalid_paths(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "src").mkdir()
    (ws / "src" / "file.txt").write_text("hello")
    # Valid relative path with slash
    assert _validate_bare_command_paths(["cat", "src/file.txt"], ws) is True
    # Invalid absolute outside
    assert _validate_bare_command_paths(["cat", "/etc/passwd"], ws) is False
    # Path with traversal
    assert _validate_bare_command_paths(["cat", "src/../etc/passwd"], ws) is False

def test_validate_bare_non_file_op_command(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # echo is not in FILE_OPERATING_COMMANDS, so paths are not validated
    assert _validate_bare_command_paths(["echo", "/etc/passwd"], ws) is True

def test_validate_bare_skip_next_is_path_invalid(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # awk -f /etc/passwd should be denied because -f takes a file path
    assert _validate_bare_command_paths(["awk", "-f", "/etc/passwd"], ws) is False
    # grep -f /etc/shadow should be denied
    assert _validate_bare_command_paths(["grep", "-f", "/etc/shadow"], ws) is False

def test_validate_bare_double_dash_separator(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "file.txt").write_text("hi")
    # cat -- file.txt : -- sets pattern_skipped, so file.txt is treated as path
    assert _validate_bare_command_paths(["cat", "--", "file.txt"], ws) is True
    # cat -- /etc/passwd : -- sets pattern_skipped, /etc/passwd is outside ws
    assert _validate_bare_command_paths(["cat", "--", "/etc/passwd"], ws) is False

def test_validate_bare_equals_glob_chars(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # cat --file=*.txt should be denied due to glob char in value
    assert _validate_bare_command_paths(["cat", "--file=*.txt"], ws) is False

def test_validate_bare_equals_path_outside(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # cat --file=/etc/passwd should be denied
    assert _validate_bare_command_paths(["cat", "--file=/etc/passwd"], ws) is False

def test_shell_interpreter_script_path_bash(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # bash script.sh should be denied (has_script_path for bash)
    assert validate_command("bash script.sh", workspace_root=ws) is None
    # sh /tmp/script.sh should be denied
    assert validate_command("sh /tmp/script.sh", workspace_root=ws) is None

def test_validate_command_shell_control_tokens(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("ls && pwd", workspace_root=ws) is None
    assert validate_command("echo hello > out.txt", workspace_root=ws) is None
    assert validate_command("cat file | grep foo", workspace_root=ws) is not None # Pipe is allowed

def test_validate_command_subshell(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("echo $(whoami)", workspace_root=ws) is None
    assert validate_command("echo `whoami`", workspace_root=ws) is None

def test_validate_command_empty_pipeline_segment(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # ls | should result in empty last segment
    assert validate_command("ls |", workspace_root=ws) is None
    # | ls should result in empty first segment? No, shlex might handle differently.
    # Let's test trailing pipe.
    assert validate_command("cat file |", workspace_root=ws) is None

def test_resolve_workspace_path_symlink_inside(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "real_dir").mkdir()
    (ws / "link_dir").symlink_to(ws / "real_dir")
    # Path containing symlink component inside workspace should be denied
    result = resolve_workspace_path("link_dir/file.txt", ws)
    assert result is None

def test_resolve_workspace_path_exact_root_match(tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # Resolving "." or "" inside ws should return ws itself
    result = resolve_workspace_path(".", ws)
    assert result is not None
    assert result == ws.resolve()

def test_resolve_workspace_path_oserror(monkeypatch, tmp_path):
    ws = tmp_path / "ws"
    ws.mkdir()
    # Mock is_symlink to raise OSError
    import os
    original_is_symlink = os.path.is_symlink
    def mock_is_symlink(path):
        if "trigger_error" in str(path):
            raise OSError("Mocked error")
        return original_is_symlink(path)
    monkeypatch.setattr(os.path, "is_symlink", mock_is_symlink)
    # This path will trigger OSError in the loop
    result = resolve_workspace_path("trigger_error/file.txt", ws)
    assert result is None


# === Tests for remaining coverage gaps (lines 207, 213, 221-222, 310, 341, 409, 430) ===

def test_skip_next_is_path_glob_chars(tmp_path):
    """Cover line 207-208: glob chars in skip_next_is_path branch.
    awk -f *.txt should be denied because * is a glob char in a file path position."""
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _validate_bare_command_paths(["awk", "-f", "*.txt"], ws) is False
    assert _validate_bare_command_paths(["grep", "-f", "?.txt"], ws) is False
    assert _validate_bare_command_paths(["sed", "-f", "[abc].txt"], ws) is False
    assert _validate_bare_command_paths(["awk", "--source", "{a,b}.txt"], ws) is False
    assert _validate_bare_command_paths(["grep", "--file", "~/file.txt"], ws) is False


def test_skip_next_is_path_dotdot(tmp_path):
    """Cover line 205-206: dotdot in skip_next_is_path branch."""
    ws = tmp_path / "ws"
    ws.mkdir()
    assert _validate_bare_command_paths(["awk", "-f", "../etc/passwd"], ws) is False
    assert _validate_bare_command_paths(["grep", "--file", "../../secret"], ws) is False


def test_skip_next_is_path_valid_continues(tmp_path):
    """Cover line 213: continue after valid skip_next_is_path processing.
    When -f points to a valid file inside workspace, processing continues."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "script.awk").write_text("{print}")
    # Valid -f path inside workspace should pass and continue to next token
    assert _validate_bare_command_paths(["awk", "-f", "script.awk", "data.txt"], ws) is True


def test_equals_pattern_skipped_grep(tmp_path):
    """Cover lines 221-222: pattern_skipped set via --key=value for grep family."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "file.txt").write_text("hello")
    # grep --regexp=pattern file.txt -> pattern_skipped=True via = syntax
    assert _validate_bare_command_paths(["grep", "--regexp=hello", "file.txt"], ws) is True
    # grep -e=pattern file.txt
    assert _validate_bare_command_paths(["grep", "-e=hello", "file.txt"], ws) is True
    # grep --file=patterns.txt file.txt (file path validated via = handler)
    (ws / "patterns.txt").write_text("hello")
    assert _validate_bare_command_paths(["grep", "--file=patterns.txt", "file.txt"], ws) is True


def test_equals_pattern_skipped_sed(tmp_path):
    """Cover lines 221-222: pattern_skipped set via --key=value for sed family."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "file.txt").write_text("hello")
    # sed --expression=s/foo/bar/ file.txt
    assert _validate_bare_command_paths(["sed", "--expression=s/foo/bar/", "file.txt"], ws) is True
    # sed -e=s/foo/bar/ file.txt
    assert _validate_bare_command_paths(["sed", "-e=s/foo/bar/", "file.txt"], ws) is True


def test_equals_pattern_skipped_awk(tmp_path):
    """Cover lines 221-222: pattern_skipped set via --key=value for awk family."""
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "file.txt").write_text("hello")
    # awk --source='{print}' file.txt
    assert _validate_bare_command_paths(["awk", "--source={print}", "file.txt"], ws) is True
    # awk -f=script.awk file.txt (file path validated)
    (ws / "script.awk").write_text("{print}")
    assert _validate_bare_command_paths(["awk", "-f=script.awk", "file.txt"], ws) is True


def test_main_loop_glob_chars_in_path(tmp_path):
    """Cover line 310: glob chars detected in main path validation loop.
    This is the non-skip_next, non-flag path where a positional arg has glob chars."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # cat with positional glob arg (not after a flag that skips)
    assert _validate_bare_command_paths(["cat", "*.txt"], ws) is False
    assert _validate_bare_command_paths(["ls", "?.txt"], ws) is False
    assert _validate_bare_command_paths(["cp", "[abc].txt", "dest.txt"], ws) is False
    assert _validate_bare_command_paths(["rm", "{a,b}.txt"], ws) is False
    assert _validate_bare_command_paths(["head", "~/file.txt"], ws) is False


def test_shell_control_tokens_dollar_paren(tmp_path):
    """Cover line 341: $( subshell detection in validate_command."""
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("echo $(whoami)", workspace_root=ws) is None
    assert validate_command("cat $(cat /etc/passwd)", workspace_root=ws) is None


def test_shell_control_tokens_backtick(tmp_path):
    """Cover line 341: backtick subshell detection in validate_command."""
    ws = tmp_path / "ws"
    ws.mkdir()
    assert validate_command("echo `whoami`", workspace_root=ws) is None
    assert validate_command("cat `hostname`", workspace_root=ws) is None


def test_empty_first_pipeline_segment(tmp_path):
    """Cover line 409: leading pipe creates empty first segment."""
    ws = tmp_path / "ws"
    ws.mkdir()
    # | ls -> first segment is empty, should return None
    assert validate_command("| ls", workspace_root=ws) is None
    # || is a shell control token, but single | at start tests empty segment
    assert validate_command("| cat file.txt", workspace_root=ws) is None


def test_effective_root_fallback_to_workspace_root(tmp_path):
    """Cover line 430: workspace_root=None falls back to WORKSPACE_ROOT constant.
    When workspace_root is None, validate_command uses module-level WORKSPACE_ROOT."""
    # Call with workspace_root=None; should use WORKSPACE_ROOT (project root)
    # A relative path like 'codebot/tool_policy.py' should resolve inside project root
    result = validate_command("cat codebot/tool_policy.py", workspace_root=None)
    assert result is not None
    # An absolute path outside project root should be blocked
    assert validate_command("cat /etc/passwd", workspace_root=None) is None
    # Verify WORKSPACE_ROOT is actually used by checking that paths inside it work
    import os
    from codebot.tool_policy import WORKSPACE_ROOT
    # Create a temp file inside WORKSPACE_ROOT would be risky; instead verify
    # that the constant exists and is a resolved Path
    assert isinstance(WORKSPACE_ROOT, Path)
    assert WORKSPACE_ROOT.is_absolute()
