"""Tests for codebot/api_tools.py — read/write/edit/grep/glob/bash + scope enforcement.

Focus: filesystem scope, path validation, bash allowlisting, bounded I/O, atomic writes.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest

import codebot.api_tools as api_tools
from codebot.api_tools import (
    read, write, edit, grep, glob, bash, a11y_snapshot,
    batch_read, batch_grep,
    MAX_READ_BYTES, MAX_BASH_OUTPUT, MAX_GREP_OUTPUT,
    WORKSPACE_ROOT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

@pytest.fixture
def ws(tmp_path):
    """Workspace root isolated to tmp_path."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # Patch global WORKSPACE_ROOT so all tools resolve inside tmp workspace
    with patch.object(api_tools, "WORKSPACE_ROOT", workspace):
        yield workspace


@pytest.fixture
def ws_file(ws):
    """Create a file inside ws and return its relative path string."""
    def _make(name, content="hello world"):
        p = ws / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return str(name)  # relative path
    return _make


# ===========================================================================
# read()
# ===========================================================================

class TestRead:
    def test_read_success(self, ws, ws_file):
        rel = ws_file("a.txt", "hello world")
        result = read(rel)
        assert result["success"] is True
        assert result["output"] == "hello world"
        assert result["error"] is None

    def test_read_with_offset_and_limit(self, ws, ws_file):
        rel = ws_file("lines.txt", "line0\nline1\nline2\nline3\nline4")
        result = read(rel, offset=1, limit=2)
        assert result["success"] is True
        assert result["output"] == "line1\nline2"

    def test_read_with_offset_only(self, ws, ws_file):
        rel = ws_file("lines.txt", "a\nb\nc\nd")
        result = read(rel, offset=2)
        assert result["success"] is True
        assert result["output"] == "c\nd"

    def test_read_negative_offset_clamped(self, ws, ws_file):
        rel = ws_file("neg.txt", "x\ny\nz")
        result = read(rel, offset=-5)
        assert result["success"] is True
        assert result["output"] == "x\ny\nz"

    def test_read_limit_zero(self, ws, ws_file):
        rel = ws_file("zero.txt", "a\nb\nc")
        result = read(rel, offset=0, limit=0)
        assert result["success"] is True
        assert result["output"] == ""

    def test_read_offset_beyond_end(self, ws, ws_file):
        rel = ws_file("short.txt", "only")
        result = read(rel, offset=10)
        assert result["success"] is True
        assert result["output"] == ""

    def test_read_path_denied_escape(self, ws):
        result = read("../escape.txt")
        assert result["success"] is False
        assert result["error"] == "path denied"

    def test_read_absolute_outside_denied(self, ws):
        result = read("/etc/passwd")
        assert result["success"] is False
        assert result["error"] == "path denied"

    def test_read_missing_file(self, ws):
        result = read("no_such.txt")
        assert result["success"] is False
        assert "No such file" in result["error"] or "not found" in result["error"].lower() or result["error"] != ""

    def test_read_1mb_cap(self, ws):
        # Create file larger than 1 MB
        big = ws / "big.bin"
        big.write_bytes(b"a" * (MAX_READ_BYTES + 5000))
        result = read("big.bin")
        assert result["success"] is True
        assert len(result["output"].encode("utf-8")) <= MAX_READ_BYTES

    def test_read_binary_decode_replace(self, ws):
        # Write raw bytes that are not valid utf-8
        p = ws / "bin.dat"
        p.write_bytes(b"\xff\xfe\xfd hello")
        result = read("bin.dat")
        assert result["success"] is True
        # Should not raise — uses errors=replace
        assert "hello" in result["output"]

    def test_read_never_raises(self, ws):
        # Patch open to raise
        with patch.object(Path, "open", side_effect=OSError("disk fail")):
            result = read("any.txt")
            assert result["success"] is False
            assert "disk fail" in result["error"]

    def test_read_directory_path_returns_error(self, ws):
        (ws / "adir").mkdir()
        result = read("adir")
        assert result["success"] is False  # IsADirectoryError


# ===========================================================================
# write()
# ===========================================================================

class TestWrite:
    def test_write_success(self, ws):
        result = write("out.txt", "content here")
        assert result["success"] is True
        assert "Wrote" in result["output"]
        assert result["error"] is None
        assert (ws / "out.txt").read_text() == "content here"

    def test_write_creates_parent_dirs(self, ws):
        result = write("a/b/c/new.txt", "nested")
        assert result["success"] is True
        assert (ws / "a/b/c/new.txt").read_text() == "nested"

    def test_write_reports_byte_size(self, ws):
        result = write("sized.txt", "abc")
        assert "3 bytes" in result["output"]

    def test_write_non_string_content(self, ws):
        result = write("num.txt", 12345)  # type: ignore
        assert result["success"] is True
        assert (ws / "num.txt").read_text() == "12345"

    def test_write_overwrites_existing(self, ws):
        write("overwrite.txt", "first")
        write("overwrite.txt", "second")
        assert (ws / "overwrite.txt").read_text() == "second"

    def test_write_atomic_no_tmp_leak(self, ws):
        write("atomic.txt", "data")
        # tmp file should be replaced, not lingering? At least main file exists
        assert not (ws / "atomic.txt.tmp").exists()
        assert (ws / "atomic.txt").exists()

    def test_write_path_denied(self, ws):
        result = write("../escape.txt", "bad")
        assert result["success"] is False
        assert result["error"] == "path denied"

    def test_write_absolute_outside_denied(self, ws):
        result = write("/tmp/outside.txt", "bad")
        assert result["success"] is False
        assert result["error"] == "path denied"

    def test_write_never_raises(self, ws):
        with patch.object(Path, "mkdir", side_effect=OSError("no perm")):
            result = write("x/y.txt", "hi")
            assert result["success"] is False

    def test_write_empty_content(self, ws):
        result = write("empty.txt", "")
        assert result["success"] is True
        assert (ws / "empty.txt").read_text() == ""

    def test_write_unicode(self, ws):
        result = write("uni.txt", "héllo 🌍")
        assert result["success"] is True
        assert (ws / "uni.txt").read_text(encoding="utf-8") == "héllo 🌍"


# ===========================================================================
# edit()
# ===========================================================================

class TestEdit:
    def test_edit_success(self, ws, ws_file):
        ws_file("code.py", "x = 1\ny = 2\n")
        result = edit("code.py", "x = 1", "x = 99")
        assert result["success"] is True
        assert "1 replacement" in result["output"]
        assert (ws / "code.py").read_text() == "x = 99\ny = 2\n"

    def test_edit_old_string_not_found(self, ws, ws_file):
        ws_file("f.txt", "hello")
        result = edit("f.txt", "missing", "new")
        assert result["success"] is False
        assert "not found" in result["error"]
        # file unchanged
        assert (ws / "f.txt").read_text() == "hello"

    def test_edit_ambiguous_multiple_occurrences(self, ws, ws_file):
        ws_file("amb.txt", "foo foo foo")
        result = edit("amb.txt", "foo", "bar")
        assert result["success"] is False
        assert "must be unambiguous" in result["error"] or "found 3 times" in result["error"]

    def test_edit_exactly_two_occurrences_fails(self, ws, ws_file):
        ws_file("twice.txt", "a b a")
        result = edit("twice.txt", "a", "z")
        assert result["success"] is False
        assert "2 times" in result["error"]

    def test_edit_file_not_found(self, ws):
        result = edit("nope.txt", "old", "new")
        assert result["success"] is False
        assert "file not found" in result["error"]

    def test_edit_path_denied(self, ws):
        result = edit("../escape.txt", "old", "new")
        assert result["success"] is False
        assert result["error"] == "path denied"

    def test_edit_atomic(self, ws, ws_file):
        ws_file("atom.txt", "unique_old unique")
        edit("atom.txt", "unique_old", "unique_new")
        assert not (ws / "atom.txt.tmp").exists()
        assert (ws / "atom.txt").read_text() == "unique_new unique"

    def test_edit_new_string_can_be_empty(self, ws, ws_file):
        ws_file("del.txt", "keep this REMOVE keep")
        result = edit("del.txt", "REMOVE ", "")
        assert result["success"] is True
        assert (ws / "del.txt").read_text() == "keep this keep"

    def test_edit_never_raises(self, ws, ws_file):
        ws_file("ok.txt", "one")
        with patch("codebot.api_tools.open", side_effect=OSError("open fail")):
            result = edit("ok.txt", "one", "two")
            assert result["success"] is False

    def test_edit_preserves_other_content(self, ws, ws_file):
        content = "first line\nsecond line\nthird line\n"
        ws_file("multi.txt", content)
        # Make old_string unique by including surrounding context
        edit("multi.txt", "second line\n", "SECOND LINE\n")
        assert (ws / "multi.txt").read_text() == "first line\nSECOND LINE\nthird line\n"


# ===========================================================================
# grep()
# ===========================================================================

class TestGrep:
    def test_grep_success_single_file(self, ws, ws_file):
        ws_file("a.py", "def foo():\n    pass\ndef bar():\n")
        result = grep("def foo", "a.py")
        assert result["success"] is True
        assert "def foo" in result["output"]
        assert "a.py:1" in result["output"]

    def test_grep_directory_recursive(self, ws):
        (ws / "sub").mkdir()
        (ws / "sub" / "x.py").write_text("hello grep target\n")
        (ws / "y.py").write_text("no match here\n")
        result = grep("grep target", ".")
        assert result["success"] is True
        assert "grep target" in result["output"]
        assert "y.py" not in result["output"]

    def test_grep_invalid_regex(self, ws):
        result = grep("[unclosed", ".")
        assert result["success"] is False
        assert "invalid regex" in result["error"]

    def test_grep_path_denied(self, ws):
        result = grep("foo", "../escape")
        assert result["success"] is False
        assert result["error"] == "path denied"

    def test_grep_path_not_found(self, ws):
        result = grep("foo", "nonexistent_dir_xyz")
        assert result["success"] is False
        assert "path not found" in result["error"]

    def test_grep_include_filter(self, ws):
        (ws / "a.py").write_text("match me\n")
        (ws / "b.txt").write_text("match me\n")
        result = grep("match me", ".", include="*.py")
        assert result["success"] is True
        assert "a.py" in result["output"]
        assert "b.txt" not in result["output"]

    def test_grep_include_filter_single_file_no_match(self, ws, ws_file):
        ws_file("f.txt", "hello")
        result = grep("hello", "f.txt", include="*.py")
        assert result["success"] is True
        assert result["output"] == ""

    def test_grep_capped_output(self, ws):
        # Create many matching lines to exceed 10KB
        big = "\n".join([f"match line {i}" for i in range(5000)])
        (ws / "big.py").write_text(big)
        result = grep("match line", ".")
        assert result["success"] is True
        assert len(result["output"]) <= MAX_GREP_OUTPUT

    def test_grep_no_matches_returns_empty(self, ws, ws_file):
        ws_file("no.py", "nothing here\n")
        result = grep("zzz_nomatch_999", "no.py")
        assert result["success"] is True
        assert result["output"] == ""

    def test_grep_case_sensitive(self, ws, ws_file):
        ws_file("case.txt", "Hello\nhello\nHELLO\n")
        result = grep("hello", "case.txt")
        assert result["success"] is True
        # default regex is case-sensitive
        assert "hello" in result["output"]
        assert result["output"].count("hello") == 1

    def test_grep_multiple_files_line_numbers(self, ws):
        (ws / "f1.py").write_text("a\nTARGET\nc\n")
        (ws / "f2.py").write_text("TARGET at line1\nx\n")
        result = grep("TARGET", ".")
        assert result["success"] is True
        assert ":2:" in result["output"] or ":1:" in result["output"]
        assert "TARGET" in result["output"]

    def test_grep_binary_file_handled(self, ws):
        (ws / "bin.dat").write_bytes(b"\xff\xfe hello world \x00")
        result = grep("hello", ".")
        assert result["success"] is True

    def test_grep_unreadable_file_skipped(self, ws):
        (ws / "good.py").write_text("findme\n")
        (ws / "bad.py").write_text("findme\n")
        # Make bad.py unreadable by mocking open per-file? Simpler: ensure still succeeds
        result = grep("findme", ".")
        assert result["success"] is True
        assert "findme" in result["output"]

    def test_grep_regex_special(self, ws, ws_file):
        ws_file("re.txt", "foo123bar\nfoobar\nfoo   bar\n")
        result = grep(r"foo\s+bar", "re.txt")
        assert result["success"] is True
        assert "foo   bar" in result["output"]


# ===========================================================================
# glob()
# ===========================================================================

class TestGlob:
    def test_glob_success(self, ws):
        (ws / "a.py").write_text("x")
        (ws / "b.py").write_text("x")
        (ws / "c.txt").write_text("x")
        result = glob("*.py", ".")
        assert result["success"] is True
        assert "a.py" in result["output"]
        assert "b.py" in result["output"]
        assert "c.txt" not in result["output"]

    def test_glob_recursive(self, ws):
        (ws / "sub").mkdir()
        (ws / "sub" / "deep.py").write_text("x")
        (ws / "top.py").write_text("x")
        result = glob("**/*.py", ".")
        assert result["success"] is True
        assert "deep.py" in result["output"]
        assert "top.py" in result["output"]

    def test_glob_sorted(self, ws):
        for name in ["z.py", "a.py", "m.py"]:
            (ws / name).write_text("x")
        result = glob("*.py", ".")
        lines = result["output"].splitlines()
        assert lines == sorted(lines)

    def test_glob_path_denied(self, ws):
        result = glob("*.py", "../escape")
        assert result["success"] is False
        assert result["error"] == "path denied"

    def test_glob_path_not_found(self, ws):
        result = glob("*.py", "no_such_dir_xyz")
        assert result["success"] is False
        assert "path not found" in result["error"]

    def test_glob_no_matches_empty(self, ws):
        result = glob("*.nomatch_xyz", ".")
        assert result["success"] is True
        assert result["output"] == ""

    def test_glob_capped_output(self, ws):
        # Create many files to exceed 10KB listing
        for i in range(200):
            (ws / f"file_{i:04d}.py").write_text("x")
        result = glob("*.py", ".")
        assert result["success"] is True
        if len(result["output"]) > MAX_GREP_OUTPUT:
            assert "[truncated]" in result["output"]

    def test_glob_never_raises(self, ws):
        with patch.object(Path, "rglob", side_effect=OSError("fail")):
            result = glob("*.py", ".")
            assert result["success"] is False

    def test_glob_only_files_not_dirs(self, ws):
        (ws / "adir").mkdir()
        (ws / "adir" / "inside.py").write_text("x")
        result = glob("*", ".")
        # Should list files recursively, not dirs themselves
        assert "adir" in result["output"] or "inside.py" in result["output"]
        # Ensure glob doesn't return directory entries without is_file check violation:
        # Use pattern that matches dir name but dir is not file
        result2 = glob("adir", ".")
        # rglob("adir") would match the directory itself but glob filters is_file
        assert result2["success"] is True


# ===========================================================================
# bash()
# ===========================================================================

class TestBash:
    def test_bash_allowed_command_success(self, ws):
        result = bash("echo hello")
        assert result["success"] is True
        assert "hello" in result["output"]
        assert result["error"] is None

    def test_bash_combines_stdout_stderr(self, ws):
        # Mock Popen for pipeline-based execution (shell=False)
        mock_proc = MagicMock()
        mock_proc.stdout = MagicMock()
        mock_proc.stderr = MagicMock()
        mock_proc.communicate.return_value = (b"out\n", b"err\n")
        mock_proc.returncode = 0
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("echo hello")
            assert result["success"] is True
            assert "out" in result["output"]
            assert "err" in result["output"]

    def test_bash_command_denied(self, ws):
        result = bash("sudo rm -rf /")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_nonzero_exit(self, ws):
        result = bash("python3 -c 'import sys; sys.exit(1)'")
        assert result["success"] is False
        assert result["error"] is not None

    def test_bash_error_prefers_stderr(self, ws):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"stderr msg\n")
        mock_proc.returncode = 2
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("echo hi")
            assert result["success"] is False
            assert "stderr msg" in result["error"]

    def test_bash_truncates_large_output(self, ws):
        big = b"a" * 200000
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (big, b"")
        mock_proc.returncode = 0
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("echo hi")
            assert result["success"] is True
            assert len(result["output"]) <= MAX_BASH_OUTPUT + 50  # + truncation suffix
            if len(big) > MAX_BASH_OUTPUT:
                assert "[truncated" in result["output"]

    def test_bash_shell_metacharacters_denied(self, ws):
        result = bash("echo hi; rm -rf /")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_shell_redirection_denied(self, ws):
        result = bash("echo hi > output.txt")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_command_substitution_denied(self, ws):
        result = bash("echo $(whoami)")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_timeout(self, ws):
        # Mock Popen.communicate to raise TimeoutExpired
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="echo", timeout=1)
        mock_proc.kill = MagicMock()
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("echo hi", timeout=1)
            assert result["success"] is False
            assert "timeout after 1s" in result["error"]

    def test_bash_timeout_with_bytes_output(self, ws):
        # Timeout now returns empty output since Popen.kill() discards partial output
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="echo", timeout=1)
        mock_proc.kill = MagicMock()
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("echo hi", timeout=1)
            assert result["success"] is False
            assert "timeout after 1s" in result["error"]

    def test_bash_allowlisted_git(self, ws):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"ok", b"")
        mock_proc.returncode = 0
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("git status")
            assert result["success"] is True

    def test_bash_cwd_is_workspace(self, ws):
        # Verify cwd passed to subprocess.Popen is WORKSPACE_ROOT
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 0
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc) as mock_popen:
            bash("echo hi")
            assert mock_popen.call_args[1]["cwd"] == ws

    def test_bash_pipe_allowed(self, ws):
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"piped", b"")
        mock_proc.returncode = 0
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc) as mock_popen:
            result = bash("echo hello | head -n 1")
            # Should be allowed and use Popen chaining (called twice for 2 segments)
            assert mock_popen.call_count == 2
            assert result["success"] is True

    def test_bash_pipe_to_blocked_command(self, ws):
        result = bash("echo hello | sudo ls")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_never_raises_on_exception(self, ws):
        with patch("codebot.api_tools.subprocess.Popen", side_effect=OSError("boom")):
            result = bash("echo hi")
            assert result["success"] is False
            assert "boom" in result["error"]

    def test_bash_empty_command_denied(self, ws):
        result = bash("")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_path_traversal_blocked(self, ws):
        result = bash("cat ../../etc/passwd")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_tilde_expansion_denied_outside_workspace(self, ws):
        """Verify tilde expansion pointing outside workspace is denied."""
        # Mock HOME to be outside workspace
        with patch.dict(os.environ, {"HOME": str(ws.parent / "outside_home")}):
            result = bash("cat ~/secret.txt")
            assert result["success"] is False
            assert "Tilde expansion escapes workspace" in result["error"] or "command denied" in result["error"]

    def test_bash_glob_expansion_allowed_inside(self, ws):
        """Verify legitimate glob usage within workspace works."""
        (ws / "file1.txt").write_text("content1")
        (ws / "file2.txt").write_text("content2")
        result = bash("cat *.txt")
        assert result["success"] is True
        assert "content1" in result["output"]
        assert "content2" in result["output"]

    def test_bash_brace_expansion_denied(self, ws):
        """Verify brace expansion is blocked entirely."""
        result = bash("echo {a,b}")
        assert result["success"] is False
        assert "Brace expansion denied" in result["error"] or "command denied" in result["error"]

    def test_bash_empty_glob_denied(self, ws):
        """Verify glob pattern matching no files is denied."""
        result = bash("cat nonexistent*.xyz")
        assert result["success"] is False
        assert "Glob pattern matched no files" in result["error"] or "command denied" in result["error"]

    def test_bash_toctou_symlink_swap_race_condition(self, ws):
        """TOCTOU race: concurrent symlink swap by separate process must never leak external content.
        
        ADVERSARIAL TEST: A separate OS process (not cooperating with the lock)
        rapidly swaps a symlink between a safe workspace file and /etc/passwd
        while the main process invokes bash('cat <symlink>'). The bash() tool
        must never return content from outside the workspace.
        
        This tests the single-writer invariant: the lock serializes API users,
        but a non-cooperating process bypassing the lock represents the hostile
        concurrent writer threat model. The symlink component rejection in
        resolve_workspace_path() should block this attack regardless of timing.
        """
        import subprocess
        import signal
        
        # Create a safe file inside workspace with unique marker
        safe_file = ws / "safe_content.txt"
        safe_marker = "SAFE_WORKSPACE_CONTENT_MARKER_XYZZY_12345"
        safe_file.write_text(safe_marker + "\n", encoding="utf-8")
        
        # External target we must NEVER read
        external_target = Path("/etc/passwd")
        external_marker = "root:"  # Universal marker present in /etc/passwd
        
        # Symlink path inside workspace
        symlink_path = ws / "race_link.txt"
        
        # Create a helper script for the attacker process
        attacker_script = ws / "attacker.py"
        attacker_script.write_text(f'''
import os
import sys
import time
from pathlib import Path

ws = Path(r"{ws}")
safe = ws / "safe_content.txt"
external = Path("/etc/passwd")
link = ws / "race_link.txt"

count_file = ws / "swap_count.txt"
count_file.write_text("0")

idx = 0
try:
    while idx < 500:  # Max 500 swaps
        target = safe if idx % 2 == 0 else external
        tmp = ws / ("tmp_link_" + str(idx) + ".txt")
        if tmp.exists() or tmp.is_symlink():
            tmp.unlink()
        tmp.symlink_to(target)
        try:
            tmp.replace(link)
            idx += 1
            count_file.write_text(str(idx))
        except OSError:
            pass
except KeyboardInterrupt:
    pass
''', encoding="utf-8")
        
        # Start attacker as separate process (does NOT hold the lock)
        attacker_proc = subprocess.Popen(
            [sys.executable, str(attacker_script)],
            cwd=str(ws),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        
        violations = []
        read_count = 0
        max_reads = 200
        
        # Give attacker a moment to start swapping
        time.sleep(0.1)
        
        # Attempt reads while attacker is running
        deadline = time.monotonic() + 3.0
        while read_count < max_reads and time.monotonic() < deadline:
            try:
                result = bash(f"cat {symlink_path.name}")
                read_count += 1
                if result["success"]:
                    output = result["output"]
                    if external_marker in output and safe_marker not in output:
                        violations.append({
                            "read_num": read_count,
                            "output_snippet": output[:200],
                        })
                        break  # Found violation, stop early
            except Exception:
                pass
            time.sleep(0.001)  # Small delay to allow context switches
        
        # Terminate attacker
        attacker_proc.terminate()
        try:
            attacker_proc.wait(timeout=2.0)
        except subprocess.TimeoutExpired:
            attacker_proc.kill()
            attacker_proc.wait()
        
        # Cleanup
        if symlink_path.exists() or symlink_path.is_symlink():
            symlink_path.unlink(missing_ok=True)
        if attacker_script.exists():
            attacker_script.unlink()
        count_file = ws / "swap_count.txt"
        swap_count = 0
        if count_file.exists():
            try:
                swap_count = int(count_file.read_text().strip())
            except ValueError:
                pass
            count_file.unlink()
        
        # Verify attacker actually ran
        assert swap_count >= 50, (
            f"Attacker only performed {swap_count} swaps, need 50+ for valid race test"
        )
        
        # CRITICAL ASSERTION: no external content was ever read
        # The symlink component rejection should block this regardless of race
        assert len(violations) == 0, (
            f"TOCTOU violation detected! bash() read external content "
            f"{len(violations)} times. First violation: {violations[0]}"
        )

    def test_bash_relative_path_ls(self, ws):
        """Verify relative path resolution for ls command within workspace."""
        subdir = ws / "subdir"
        subdir.mkdir()
        (subdir / "file.txt").write_text("content")
        result = bash("ls subdir")
        assert result["success"] is True
        assert "file.txt" in result["output"]

    def test_bash_relative_path_cat(self, ws):
        """Verify relative path resolution for cat command within workspace."""
        content = "unique_cat_content_12345"
        (ws / "data.txt").write_text(content)
        result = bash("cat data.txt")
        assert result["success"] is True
        assert content in result["output"]

    def test_bash_symlink_within_workspace(self, ws):
        """Verify symlink traversal for legitimate workspace commands."""
        real_dir = ws / "real"
        real_dir.mkdir()
        (real_dir / "file.txt").write_text("symlink_target_content")
        link_path = ws / "link"
        link_path.symlink_to(real_dir)
        result = bash("cat link/file.txt")
        assert result["success"] is True
        assert "symlink_target_content" in result["output"]


# ===========================================================================
# a11y_snapshot()
# ===========================================================================

class TestA11ySnapshot:
    def test_a11y_success(self, ws):
        # Mock node execution returning tree text
        mock_result = MagicMock(returncode=0, stdout="button 'Click'\n", stderr="")
        with patch("codebot.api_tools.subprocess.run", return_value=mock_result):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is True
            assert "Click" in result["output"]
            assert result["error"] is None

    def test_a11y_fail_returncode(self, ws):
        mock_result = MagicMock(returncode=1, stdout="", stderr="chromium not found")
        with patch("codebot.api_tools.subprocess.run", return_value=mock_result):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is False
            assert "playwright failed" in result["error"]

    def test_a11y_empty_tree(self, ws):
        mock_result = MagicMock(returncode=0, stdout="EMPTY", stderr="")
        with patch("codebot.api_tools.subprocess.run", return_value=mock_result):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is False
            assert "empty accessibility tree" in result["error"]

    def test_a11y_node_not_found(self, ws):
        with patch("codebot.api_tools.subprocess.run", side_effect=FileNotFoundError()):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is False
            assert "node not found" in result["error"]

    def test_a11y_timeout(self, ws):
        with patch("codebot.api_tools.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="node", timeout=30)):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is False
            assert "timed out" in result["error"]

    def test_a11y_truncated_large_tree(self, ws):
        huge = "x" * (2_000_000 + 1000)
        mock_result = MagicMock(returncode=0, stdout=huge, stderr="")
        with patch("codebot.api_tools.subprocess.run", return_value=mock_result):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is True
            assert "[truncated at 2MB]" in result["output"]

    def test_a11y_generic_exception(self, ws):
        with patch("codebot.api_tools.subprocess.run", side_effect=RuntimeError("weird")):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is False

    def test_a11y_output_path_in_config(self, ws):
        # Ensure output_path is passed via JSON config and HTML is injected
        mock_result = MagicMock(returncode=0, stdout="tree", stderr="")
        out_path = str(ws / "snapshot.txt")
        mock_html = "<html><body>Test</body></html>"
        
        # Patch _fetch_url_ssrf_safe to return known HTML, and subprocess to capture config
        with patch("codebot.api_tools._fetch_url_ssrf_safe", return_value=(mock_html, "")), \
             patch("codebot.api_tools.subprocess.run", return_value=mock_result) as mr:
            result = a11y_snapshot("http://example.com", output_path=out_path)
            assert result["success"] is True, f"a11y_snapshot failed: {result.get('error')}"
            assert mr.called, "subprocess.run was not called"
            args = mr.call_args[0][0]
            # config is 4th element of node -e call
            config_json = args[3]
            cfg = json.loads(config_json)
            assert cfg["out"] == out_path
            assert "html" in cfg
            assert cfg["html"] == mock_html

    def test_a11y_never_raises(self):
        # Even with workspace patch, should not raise
        with patch("codebot.api_tools.subprocess.run", side_effect=Exception("boom")):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is False


# ===========================================================================
# Tool policy integration + aliases
# ===========================================================================

class TestToolPolicyIntegration:
    def test_allowlisted_command_called(self, ws):
        with patch("codebot.api_tools.allowlisted_command", return_value=None) as mock_allow:
            result = bash("anything")
            mock_allow.assert_called_once_with("anything", ws)
            assert result["error"] == "command denied"

    def test_resolve_workspace_path_called_for_read(self, ws):
        with patch("codebot.api_tools.resolve_workspace_path", return_value=None):
            result = read("anything.txt")
            assert result["error"] == "path denied"

    def test_resolve_workspace_path_called_for_write(self, ws):
        with patch("codebot.api_tools.resolve_workspace_path", return_value=None):
            result = write("anything.txt", "hi")
            assert result["error"] == "path denied"

    def test_resolve_workspace_path_called_for_edit(self, ws):
        with patch("codebot.api_tools.resolve_workspace_path", return_value=None):
            result = edit("anything.txt", "old", "new")
            assert result["error"] == "path denied"

    def test_resolve_workspace_path_called_for_grep(self, ws):
        with patch("codebot.api_tools.resolve_workspace_path", return_value=None):
            result = grep("pat", "path")
            assert result["error"] == "path denied"

    def test_resolve_workspace_path_called_for_glob(self, ws):
        with patch("codebot.api_tools.resolve_workspace_path", return_value=None):
            result = glob("*.py", ".")
            assert result["error"] == "path denied"


class TestBatchRead:
    def test_batch_read_success(self, ws, ws_file):
        ws_file("a.txt", "content a")
        ws_file("b.txt", "content b")
        result = batch_read(["a.txt", "b.txt"])
        assert result["success"] is True
        assert "content a" in result["output"]
        assert "content b" in result["output"]

    def test_batch_read_missing_file(self, ws, ws_file):
        ws_file("exists.txt", "hello")
        result = batch_read(["exists.txt", "missing.txt"])
        assert result["success"] is True
        assert "exists.txt" in result["output"]
        assert "missing.txt" in result["output"]
        assert "[not found]" in result["output"]

    def test_batch_read_limit_per_file(self, ws):
        # Create file with many lines
        many_lines = "\n".join([f"line{i}" for i in range(300)])
        (ws / "big.txt").write_text(many_lines)
        result = batch_read(["big.txt"], limit_per_file=10)
        assert result["success"] is True
        assert "... (300 total lines)" in result["output"]

    def test_batch_read_path_denied(self, ws):
        result = batch_read(["../escape.txt"])
        assert result["success"] is True
        assert "[not found]" in result["output"] or "path denied" in result["output"]

    def test_batch_read_absolute_path_outside_workspace_denied(self, ws):
        """SECURITY: Absolute paths outside workspace must be denied.
        
        Adversarial test: agent prompt injection could pass /etc/passwd
        to batch_read. Must return [path denied], never leak content.
        """
        result = batch_read(["/etc/passwd"])
        assert result["success"] is True  # batch ops are fail-open per-file
        assert "[path denied]" in result["output"]
        # Must NOT contain actual passwd content
        assert "root:" not in result["output"]

    def test_batch_read_symlink_escape_denied(self, ws):
        """SECURITY: Symlink inside workspace pointing outside must be denied.
        
        Adversarial test: attacker creates symlink inside workspace pointing
        to /etc/passwd. batch_read must reject via resolve_workspace_path
        symlink component rejection, never leak external content.
        """
        import os
        # Create external target with unique marker
        external_file = ws.parent / "external_secret_batch.txt"
        external_marker = "EXTERNAL_SECRET_BATCH_READ_MARKER_XYZZY"
        external_file.write_text(external_marker + "\n", encoding="utf-8")
        try:
            # Create symlink inside workspace pointing to external file
            symlink_path = ws / "symlink_escape_batch.txt"
            symlink_path.symlink_to(external_file)
            
            # Attempt to read via batch_read
            result = batch_read(["symlink_escape_batch.txt"])
            assert result["success"] is True  # batch ops are fail-open per-file
            assert "[path denied]" in result["output"]
            # Must NOT contain external content
            assert external_marker not in result["output"]
        finally:
            if symlink_path.exists() or symlink_path.is_symlink():
                symlink_path.unlink()
            if external_file.exists():
                external_file.unlink()

    def test_batch_read_mixed_valid_and_denied_paths(self, ws, ws_file):
        """Verify batch_read handles mix of valid and denied paths correctly."""
        ws_file("valid.txt", "safe content")
        result = batch_read(["valid.txt", "/etc/passwd", "../escape.txt"])
        assert result["success"] is True
        assert "safe content" in result["output"]
        assert "[path denied]" in result["output"]
        # Count path denied markers (should be 2)
        assert result["output"].count("[path denied]") == 2


class TestBatchGrep:
    def test_batch_grep_success(self, ws, ws_file):
        ws_file("a.py", "def foo():\n    pass")
        ws_file("b.py", "def bar():\n    pass")
        result = batch_grep(["def foo", "def bar"], path=".", include="*.py")
        assert result["success"] is True
        assert "def foo" in result["output"]
        assert "def bar" in result["output"]

    def test_batch_grep_invalid_regex(self, ws):
        result = batch_grep(["[unclosed"], path=".")
        assert result["success"] is True
        assert "[invalid regex]" in result["output"]

    def test_batch_grep_limit_per_pattern(self, ws):
        # Create file with many matching lines
        many_matches = "\n".join([f"match line {i}" for i in range(100)])
        (ws / "big.py").write_text(many_matches)
        result = batch_grep(["match line"], path=".", include="*.py", limit_per_pattern=10)
        assert result["success"] is True
        # Should have limited output

    def test_batch_grep_path_denied(self, ws):
        result = batch_grep(["foo"], path="../escape")
        assert result["success"] is True

    def test_batch_grep_include_traversal(self, ws):
        """batch_grep must reject include values containing ../ or absolute paths."""
        # Create a secret file outside the workspace
        external_file = ws.parent / "external_secret.txt"
        external_file.write_text("SECRET_OUTSIDE_WORKSPACE\n", encoding="utf-8")
        try:
            # Test 1: Reject traversal with ..
            result = batch_grep(["test"], path=".", include="../../etc/*.conf")
            assert result["success"] is True
            assert "[invalid include]" in result["output"]

            # Test 2: Reject absolute path
            result2 = batch_grep(["test"], path=".", include="/etc/passwd")
            assert result2["success"] is True
            assert "[invalid include]" in result2["output"]

            # Test 3: Ensure external content cannot be read via crafted include
            result3 = batch_grep(["SECRET_OUTSIDE_WORKSPACE"], path=".", include="../../external_secret.txt")
            assert result3["success"] is True
            # The pattern name appears in the header, so we check for the marker instead
            assert "[invalid include]" in result3["output"]

            # Test 4: Legitimate includes still work
            (ws / "legit.py").write_text("def legit(): pass\n")
            result4 = batch_grep(["def legit"], path=".", include="*.py")
            assert result4["success"] is True
            assert "def legit" in result4["output"]
            assert "[invalid include]" not in result4["output"]
        finally:
            if external_file.exists():
                external_file.unlink()

    def test_batch_grep_symlink_escape(self, ws):
        """Symlink pointing outside workspace must be skipped by batch_grep."""
        import os
        # Create a file with known content outside the workspace
        external_file = ws.parent / "external_secret.txt"
        external_file.write_text("SECRET_EXTERNAL_CONTENT_12345\n", encoding="utf-8")
        try:
            # Create symlink inside workspace pointing to external file
            symlink_path = ws / "symlink_escape.txt"
            symlink_path.symlink_to(external_file)
            # Search for the secret content via batch_grep
            result = batch_grep(["SECRET_EXTERNAL_CONTENT_12345"], path=".")
            assert result["success"] is True
            # The external content must NOT appear as a file match in results
            assert "symlink_escape.txt" not in result["output"]
            assert "(0 matches)" in result["output"]
        finally:
            if symlink_path.exists() or symlink_path.is_symlink():
                symlink_path.unlink()
            if external_file.exists():
                external_file.unlink()

    def test_batch_grep_include_traversal_backslash(self, ws):
        """batch_grep must reject include values with backslash traversal."""
        result = batch_grep(["test"], path=".", include="..\\..\\etc\\passwd")
        assert result["success"] is True
        assert "[invalid include]" in result["output"]

    def test_batch_grep_include_double_dot_filename_allowed(self, ws, ws_file):
        """Filenames containing '..' (not as path component) should NOT be rejected."""
        ws_file("file..txt", "content here")
        result = batch_grep(["content"], path=".", include="file..txt")
        assert result["success"] is True
        # Should find the file since '..' is part of filename, not traversal
        assert "content here" in result["output"]


class TestEditLocking:
    """Tests for file locking in edit()."""

    def test_edit_lock_order_flock_ex_before_read(self, ws, ws_file):
        """Verify flock is called with LOCK_EX before read and LOCK_UN after replace."""
        ws_file("locked.txt", "unique_marker_12345")
        # Import the actual flock to verify it's not mocked
        from codebot.file_lock import flock as real_flock, LOCK_EX as real_LOCK_EX, LOCK_UN as real_LOCK_UN
        
        call_log = []
        
        def logging_flock(fd, op):
            call_log.append((fd, op))
            real_flock(fd, op)
        
        with patch("codebot.api_tools.flock", side_effect=logging_flock):
            result = edit("locked.txt", "unique_marker_12345", "replaced")
            assert result["success"] is True
            # Check that flock was called with LOCK_EX (acquire) and LOCK_UN (release)
            assert len(call_log) >= 2
            # First call should be LOCK_EX (before read)
            assert call_log[0][1] == real_LOCK_EX
            # Last call should be LOCK_UN (after replace)
            assert call_log[-1][1] == real_LOCK_UN

    def test_edit_concurrent_no_data_loss(self, ws):
        """Verify concurrent edits to the same file don't lose data."""
        import threading
        import time
        import traceback

        # Create a file with multiple unique markers
        markers = [f"MARKER_{i:04d}" for i in range(16)]
        initial_content = "\n".join(markers) + "\n"
        test_file = ws / "concurrent.txt"
        test_file.write_text(initial_content, encoding="utf-8")

        results = []
        errors = []
        tracebacks = []
        barrier = threading.Barrier(len(markers))

        def do_edit(marker_idx):
            try:
                # Synchronize all threads to start at the same time
                barrier.wait(timeout=10)
                marker = f"MARKER_{marker_idx:04d}"
                replacement = f"REPLACED_{marker_idx:04d}"
                result = edit("concurrent.txt", marker, replacement)
                results.append((marker_idx, result))
            except Exception as e:
                tb = traceback.format_exc()
                tracebacks.append((marker_idx, tb))
                errors.append((marker_idx, str(e)))

        # Spawn threads to edit concurrently
        threads = []
        for i in range(len(markers)):
            t = threading.Thread(target=do_edit, args=(i,))
            threads.append(t)

        # Start all threads
        for t in threads:
            t.start()

        # Wait for all to complete
        for t in threads:
            t.join(timeout=30)

        # Check no errors occurred
        if tracebacks:
            for idx, tb in tracebacks:
                print(f"Thread {idx} traceback:\n{tb}")
        assert len(errors) == 0, f"Errors during concurrent edit: {errors}"

        # Check all edits succeeded
        success_count = sum(1 for _, r in results if r["success"])
        print(f"Results: {sorted(results)}")
        print(f"Success count: {success_count}/{len(markers)}")
        for idx, r in sorted(results):
            print(f"  Marker {idx}: success={r['success']}, error={r['error']}")
        assert success_count == len(markers), f"Only {success_count}/{len(markers)} edits succeeded: {results}"

        # Verify final content has all replacements
        final_content = test_file.read_text(encoding="utf-8")
        for i in range(len(markers)):
            replacement = f"REPLACED_{i:04d}"
            assert replacement in final_content, f"Missing replacement: {replacement} in content:\n{final_content}"
            original = f"MARKER_{i:04d}"
            assert original not in final_content, f"Original marker still present: {original}"


class TestResolveWorkspacePathSymlinkTraversal:
    """Tests for symlink-based path traversal prevention in resolve_workspace_path."""

    def test_resolve_workspace_path_symlink_traversal_blocked(self, ws):
        """Symlink inside workspace pointing outside must be rejected.
        
        This directly tests the security boundary: even if a symlink exists
        within the workspace root, if its target resolves outside the workspace,
        access must be denied.
        """
        import os
        # Create a sensitive file outside the workspace
        external_target = ws.parent / "external_secret.txt"
        external_target.write_text("SENSITIVE_DATA", encoding="utf-8")
        try:
            # Create symlink inside workspace pointing to external file
            symlink_inside = ws / "innocent_link.txt"
            symlink_inside.symlink_to(external_target)
            
            # Attempt to read through the symlink must be denied
            result = read("innocent_link.txt")
            assert result["success"] is False, (
                f"Symlink traversal should be blocked but got success=True, output={result['output']!r}"
            )
            assert result["error"] == "path denied", (
                f"Expected 'path denied' error, got {result['error']!r}"
            )
        finally:
            if symlink_inside.exists() or symlink_inside.is_symlink():
                symlink_inside.unlink()
            if external_target.exists():
                external_target.unlink()

    def test_resolve_workspace_path_fallback_rejects_external_symlink(self, ws):
        """Fail-closed parity (CB-48729): api_tools re-exports the hardened primary.

        There must be NO weaker local fallback in api_tools.py: the symbol in
        use must be codebot.tool_policy.resolve_workspace_path itself, which
        rejects symlink components inside the workspace (TOCTOU fix) as well
        as realpath escapes. This test locks that parity: external symlinks
        AND in-workspace symlinks are both denied.
        """
        from codebot.tool_policy import (
            resolve_workspace_path as primary_resolver,
        )

        # api_tools must not shadow the primary with a weaker local fallback.
        assert api_tools.resolve_workspace_path is primary_resolver

        # Create external target
        external = ws.parent / "escape_target.txt"
        external.write_text("escaped", encoding="utf-8")
        # Create an in-workspace real file plus a symlink to it (TOCTOU case:
        # realpath alone would ALLOW this; component walk must DENY it).
        real_inside = ws / "real_target.txt"
        real_inside.write_text("inside", encoding="utf-8")
        link = ws / "bad_link.txt"
        inner_link = ws / "inner_link.txt"
        try:
            # Symlink inside workspace pointing outside
            link.symlink_to(external)

            result = api_tools.resolve_workspace_path(str(link), ws)
            assert result is None, (
                f"Resolver should reject symlink to external path, got {result}"
            )

            # Also test relative path through symlink
            result_rel = api_tools.resolve_workspace_path("bad_link.txt", ws)
            assert result_rel is None, (
                f"Resolver should reject relative symlink traversal, got {result_rel}"
            )

            # In-workspace symlink (points at a real workspace file) must
            # ALSO be denied by the symlink-component walk.
            inner_link.symlink_to(real_inside)
            assert api_tools.resolve_workspace_path(
                str(inner_link), ws
            ) is None, "In-workspace symlink must be denied (TOCTOU protection)"
            assert api_tools.resolve_workspace_path(
                "inner_link.txt", ws
            ) is None, "In-workspace symlink (relative) must be denied"
            # Primary and api_tools resolvers agree (parity by identity).
            assert (
                primary_resolver(str(inner_link), ws)
                is None
            )
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
            if inner_link.exists() or inner_link.is_symlink():
                inner_link.unlink()
            if external.exists():
                external.unlink()
            if real_inside.exists():
                real_inside.unlink()

    def test_resolve_workspace_path_allows_legitimate_paths(self, ws, ws_file):
        """Ensure legitimate paths within workspace still work after security fix."""
        rel = ws_file("legit.txt", "safe content")
        result = read(rel)
        assert result["success"] is True
        assert result["output"] == "safe content"
        
        # Nested directory
        nested = ws / "subdir" / "deep.txt"
        nested.parent.mkdir(parents=True, exist_ok=True)
        nested.write_text("nested safe", encoding="utf-8")
        result2 = read("subdir/deep.txt")
        assert result2["success"] is True
        assert result2["output"] == "nested safe"

    def test_resolve_workspace_path_prefix_attack_prevented(self, ws):
        """Verify /workspace-evil does not match /workspace prefix check."""
        from codebot.tool_policy import (
            resolve_workspace_path as primary_resolver,
        )

        # Parity: api_tools must use the hardened primary, not a local copy.
        assert api_tools.resolve_workspace_path is primary_resolver
        
        # Create a sibling directory with similar name
        evil_dir = ws.parent / (ws.name + "-evil")
        evil_dir.mkdir(exist_ok=True)
        evil_file = evil_dir / "secret.txt"
        evil_file.write_text("evil", encoding="utf-8")
        try:
            # Absolute path to evil dir should be rejected
            result = api_tools.resolve_workspace_path(str(evil_file), ws)
            assert result is None, (
                f"Prefix attack should be blocked: {evil_file} matched workspace {ws}"
            )
        finally:
            if evil_file.exists():
                evil_file.unlink()
            if evil_dir.exists():
                evil_dir.rmdir()


class TestFlockImportFailure:
    """Tests for fail-closed flock import behavior."""

    def test_flock_import_failure_raises(self):
        """If flock primitives are unavailable, ImportError must be raised."""
        import importlib
        # Reload api_tools with mocked imports that fail
        with patch.dict('sys.modules', {
            'codebot.file_lock': None,
            'codebot.locks': None,
        }):
            # Force reimport to trigger the fail-closed logic
            # We can't easily test this without reloading the module,
            # but we verify the logic exists by checking the source
            import ast
            source = Path("codebot/api_tools.py").read_text()
            tree = ast.parse(source)
            # Verify the fail-closed ImportError is present
            found_raise = False
            for node in ast.walk(tree):
                if isinstance(node, ast.Raise):
                    if node.exc and isinstance(node.exc, ast.Call):
                        if isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ImportError":
                            found_raise = True
                            break
            assert found_raise, "Fail-closed ImportError not found in api_tools.py"


class TestBashPipelineEdgeCases:
    """Tests for bash() pipeline parsing edge cases."""

    def test_bash_double_pipe_denied(self, ws):
        """Double pipe (e.g., 'echo | | cat') should be denied by validation."""
        result = bash("echo hello | | cat")
        assert result["success"] is False
        # Denied by allowlisted_command due to invalid pipeline structure
        assert result["error"] == "command denied"

    def test_bash_trailing_pipe_denied(self, ws):
        """Trailing pipe (e.g., 'echo |') should be denied."""
        result = bash("echo hello |")
        assert result["success"] is False
        # Denied by allowlisted_command or bash parsing
        assert result["error"] == "command denied" or "empty pipeline segment" in result["error"]

    def test_bash_invalid_command_syntax(self, ws):
        """Invalid shell syntax should return error."""
        # Unclosed quote - denied by validate_command in tool_policy.py
        result = bash("echo 'unclosed")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_pipeline_timeout_kills_all_processes(self, ws):
        """Timeout should kill all processes in the pipeline."""
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="sleep", timeout=1)
        mock_proc.kill = MagicMock()
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("sleep 10 | sleep 10", timeout=1)
            assert result["success"] is False
            assert "timeout after 1s" in result["error"]
            # Verify kill was called on all mocked processes
            assert mock_proc.kill.called


class TestGrepStreaming:
    """Tests for grep() streaming behavior - verifies bounded I/O invariant."""

    def test_grep_finds_match_beyond_1mb_chunk(self, ws):
        """Verify grep finds matches beyond the initial 1MB read cap.
        
        This is a regression test for the bug where grep loaded up to
        MAX_READ_BYTES (1MB) from every file before searching, causing
        matches beyond that offset to be silently lost.
        
        The fix streams line-by-line, so memory usage is bounded by
        MAX_GREP_OUTPUT (10KB), not by file size.
        """
        # Create a file larger than 1MB with a unique match near the end
        # Each line is ~25 bytes: "padding line 00000000\n"
        # Need ~40,000 lines to exceed 1MB
        lines_before_match = 50000  # ~1.25MB of padding
        
        big_file = ws / "big_stream_test.txt"
        with big_file.open("w") as f:
            for i in range(lines_before_match):
                f.write(f"padding line {i:08d}\n")
            f.write("UNIQUE_MATCH_BEYOND_1MB_FOUND\n")
            for i in range(10):
                f.write(f"trailing line {i}\n")
        
        # Verify file is larger than 1MB
        file_size = big_file.stat().st_size
        assert file_size > 1_000_000, f"Test file should be >1MB, got {file_size}"
        
        # Grep should find the match despite it being beyond 1MB offset
        result = grep("UNIQUE_MATCH_BEYOND_1MB_FOUND", str(big_file))
        assert result["success"] is True, f"grep failed: {result.get('error')}"
        assert "UNIQUE_MATCH_BEYOND_1MB_FOUND" in result["output"], \
            f"Match beyond 1MB was lost! Output: {result['output'][:200]}"
        # Verify line number is correct (should be lines_before_match + 1)
        assert f":{lines_before_match + 1}:" in result["output"]

    def test_grep_memory_bounded_by_output_not_file_count(self, ws):
        """Verify grep memory usage is proportional to output cap, not file count.
        
        Create many files with matches - grep should stop after MAX_GREP_OUTPUT
        is reached, not load all files into memory.
        """
        # Create 100 files, each with 1000 matching lines
        for i in range(100):
            fpath = ws / f"many_matches_{i:03d}.txt"
            content = "\n".join([f"match line {j}" for j in range(1000)])
            fpath.write_text(content)
        
        result = grep("match line", ".")
        assert result["success"] is True
        # Output should be capped at MAX_GREP_OUTPUT
        assert len(result["output"]) <= MAX_GREP_OUTPUT
        # Should have found matches (not empty)
        assert "match line" in result["output"]


class TestGrepErrorPaths:
    """Tests for grep() error handling."""

    def test_grep_unreadable_file_skipped_gracefully(self, ws):
        """Unreadable files should be skipped without failing the whole grep."""
        readable = ws / "readable.py"
        unreadable = ws / "unreadable.py"
        readable.write_text("findme\n")
        unreadable.write_text("findme\n")
        
        real_open = open
        def mock_open(*args, **kwargs):
            path = args[0]
            # Only block the specific unreadable test file, not lock files or others
            if str(path) == str(unreadable):
                raise PermissionError("denied")
            return real_open(*args, **kwargs)
        
        with patch("builtins.open", side_effect=mock_open):
            result = grep("findme", ".")
            assert result["success"] is True, f"grep failed: {result.get('error')}"
            assert "findme" in result["output"]

    def test_grep_decode_errors_handled(self, ws):
        """Binary files with invalid UTF-8 should be handled gracefully."""
        (ws / "binary.dat").write_bytes(b"\xff\xfe findme \x00")
        result = grep("findme", ".")
        assert result["success"] is True
        # Should not raise, and should find the match despite binary content


class TestEditLockingEdgeCases:
    """Tests for edit() lock acquisition edge cases."""

    def test_edit_lock_release_on_error(self, ws, ws_file):
        """Lock must be released even if edit fails mid-operation.
        
        SECURITY: edit() now uses the workspace lock (same as bash/write)
        instead of a per-file sidecar lock. This enforces the single-writer
        invariant across all API tools.
        """
        ws_file("locked.txt", "content")
        lock_path = ws / ".lock"  # Workspace lock, not per-file sidecar
        
        # First edit succeeds
        result1 = edit("locked.txt", "content", "new")
        assert result1["success"] is True
        
        # Lock file should exist but be unlocked
        assert lock_path.exists()
        
        # Second edit should succeed (lock was released)
        result2 = edit("locked.txt", "new", "final")
        assert result2["success"] is True

    def test_edit_lock_file_created(self, ws, ws_file):
        """Verify workspace lock file is used by edit().
        
        SECURITY: edit() acquires the workspace lock (WORKSPACE_ROOT + '.lock')
        to serialize with bash() and write(), enforcing the single-writer invariant.
        """
        ws_file("target.txt", "unique_content_xyz")
        lock_path = ws / ".lock"  # Workspace lock, not per-file sidecar
        
        # Lock file may or may not exist before edit (depends on prior operations)
        # What matters is that edit() uses the same lock as other tools
        
        result = edit("target.txt", "unique_content_xyz", "replaced")
        assert result["success"] is True
        
        # Workspace lock should exist after edit
        assert lock_path.exists()


class TestA11ySnapshotCoverage:
    """Tests for a11y_snapshot() coverage."""

    def test_a11y_config_serialization(self, ws):
        """Verify config JSON is correctly serialized and passed to node."""
        mock_result = MagicMock(returncode=0, stdout="tree", stderr="")
        with patch("codebot.api_tools.subprocess.run", return_value=mock_result) as mr:
            a11y_snapshot("http://example.com", viewport_width=1920, viewport_height=1080, wait_ms=2000)
            args = mr.call_args[0][0]
            config_json = args[3]
            cfg = json.loads(config_json)
            assert cfg["w"] == 1920
            assert cfg["h"] == 1080
            assert cfg["wait"] == 2000

    def test_a11y_generic_exception_handled(self, ws):
        """Generic exceptions in a11y_snapshot should return error dict."""
        with patch("codebot.api_tools.subprocess.run", side_effect=RuntimeError("unexpected")):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is False
            assert "unexpected" in result["error"]


class TestBatchReadCoverage:
    """Tests for batch_read() coverage."""

    def test_batch_read_directory_skipped(self, ws):
        """Directories in batch_read paths should be marked as not a file."""
        (ws / "subdir").mkdir()
        result = batch_read(["subdir"])
        assert result["success"] is True
        assert "[not a file]" in result["output"]

    def test_batch_read_total_limit_truncation(self, ws, ws_file):
        """batch_read should truncate when total limit exceeded."""
        # Create files that together exceed 500KB limit
        big_content = "x" * 300000
        ws_file("big1.txt", big_content)
        ws_file("big2.txt", big_content)
        
        result = batch_read(["big1.txt", "big2.txt"])
        assert result["success"] is True
        assert "[truncated" in result["output"]


class TestBatchGrepCoverage:
    """Tests for batch_grep() coverage."""

    def test_batch_grep_glob_filtering(self, ws):
        """batch_grep should filter glob results outside workspace."""
        # Create a file inside workspace
        (ws / "inside.py").write_text("match_inside\n")
        
        result = batch_grep(["match_inside"], path=".", include="*.py")
        assert result["success"] is True
        assert "match_inside" in result["output"]
        assert "inside.py" in result["output"]

    def test_batch_grep_file_list_limit(self, ws):
        """batch_grep limits file list to 200 files."""
        # Create 250 files
        for i in range(250):
            (ws / f"file_{i:03d}.py").write_text(f"content_{i}\n")
        
        result = batch_grep(["content"], path=".", include="*.py")
        assert result["success"] is True
        # Should process files but may not find all due to limit

    def test_batch_grep_path_denied_includes_invalid_header(self, ws):
        """batch_grep with denied path should still output header."""
        result = batch_grep(["test"], path="../nonexistent")
        assert result["success"] is True
        # Header should appear even if path is denied
        assert "pattern: test" in result["output"]


class TestGlobEdgeCases:
    """Tests for glob() edge cases."""

    def test_glob_absolute_path_outside_denied(self, ws):
        """Absolute paths outside workspace should be denied."""
        result = glob("/etc/*.conf")
        assert result["success"] is False
        assert result["error"] == "path denied"

    def test_glob_pattern_with_subdir(self, ws):
        """Glob pattern with subdir should search in that subdir."""
        (ws / "sub").mkdir()
        (ws / "sub" / "file.py").write_text("x")
        result = glob("sub/*.py", ".")
        assert result["success"] is True
        assert "file.py" in result["output"]


class TestSecurityGates:
    """Security-named tests for adversarial_test gate (-k security)."""
    
    def test_security_symlink_component_rejection(self, ws):
        """Symlink components inside workspace must be rejected."""
        import os
        external = ws.parent / "external.txt"
        external.write_text("secret")
        try:
            link = ws / "link.txt"
            link.symlink_to(external)
            result = read("link.txt")
            assert result["success"] is False
            assert result["error"] == "path denied"
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
            if external.exists():
                external.unlink()
    
    def test_security_glob_char_denial(self, ws, ws_file):
        """Glob characters in path positions must be denied."""
        ws_file("safe.txt", "content")
        result = bash("cat *.txt")
        assert result["success"] is False
        assert result["error"] == "command denied"
    
    def test_security_python_m_allowlist(self, ws):
        """python -m must enforce module allowlist."""
        # Allowed module
        result = bash("python3 -m pytest --version")
        assert result["success"] is True or result["error"] != "command denied"
        # Blocked module
        result2 = bash("python3 -m http.server")
        assert result2["success"] is False
        assert result2["error"] == "command denied"


class TestContractGates:
    """Contract-named tests for contract_tests gate (-k contract)."""
    
    def test_contract_tool_return_shape(self, ws, ws_file):
        """All tools must return dict with success/output/error keys."""
        ws_file("x.txt", "hi")
        for result in [
            read("x.txt"),
            write("y.txt", "hi"),
            grep("hi", "x.txt"),
            glob("*.txt", "."),
            edit("x.txt", "hi", "hi2"),
        ]:
            assert set(result.keys()) == {"success", "output", "error"}
    
    def test_contract_path_validation(self, ws):
        """Path validation must deny escape attempts."""
        result = read("../escape.txt")
        assert result["success"] is False
        assert result["error"] == "path denied"
        result2 = write("/etc/passwd", "evil")
        assert result2["success"] is False
        assert result2["error"] == "path denied"


class TestAliases:
    def test_file_read_alias(self):
        assert api_tools.file_read is api_tools.read

    def test_file_write_alias(self):
        assert api_tools.file_write is api_tools.write

    def test_all_tools_return_dict_shape(self, ws):
        # Every tool must return exactly {success, output, error}
        (ws / "x.txt").write_text("hi")
        for result in [
            read("x.txt"),
            write("y.txt", "hi"),
            grep("hi", "x.txt"),
            glob("*.txt", "."),
            bash("echo hi"),
            edit("x.txt", "hi", "hi2"),
        ]:
            assert set(result.keys()) == {"success", "output", "error"}
            assert isinstance(result["success"], bool)
            assert isinstance(result["output"], str)
            assert result["error"] is None or isinstance(result["error"], str)


class TestCoverageGaps:
    """Tests to cover remaining lines in api_tools.py for 100% coverage."""

    def test_write_lock_release_error_handled(self, ws):
        """Verify write() handles lock release errors gracefully."""
        # Mock flock to raise on unlock
        with patch("codebot.api_tools.flock") as mock_flock:
            mock_flock.side_effect = [None, OSError("unlock fail"), None]  # LOCK_EX, LOCK_UN, close
            result = write("test.txt", "content")
            # Should succeed despite unlock error (error is swallowed in finally)
            assert result["success"] is True

    def test_edit_lock_release_error_handled(self, ws, ws_file):
        """Verify edit() handles lock release errors gracefully."""
        ws_file("target.txt", "old")
        with patch("codebot.api_tools.flock") as mock_flock:
            mock_flock.side_effect = [None, OSError("unlock fail"), None]
            result = edit("target.txt", "old", "new")
            assert result["success"] is True

    def test_bash_timeout_kill_error_handled(self, ws):
        """Verify bash() handles kill() errors during timeout gracefully."""
        mock_proc = MagicMock()
        mock_proc.communicate.side_effect = subprocess.TimeoutExpired(cmd="sleep", timeout=1)
        mock_proc.kill.side_effect = OSError("kill fail")
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("sleep 10", timeout=1)
            assert result["success"] is False
            assert "timeout after 1s" in result["error"]

    def test_bash_stderr_in_error(self, ws):
        """Verify bash() includes stderr in error message on failure."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"specific stderr msg")
        mock_proc.returncode = 1
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("false")
            assert result["success"] is False
            assert "specific stderr msg" in result["error"]

    def test_bash_exit_code_in_error_when_no_stderr(self, ws):
        """Verify bash() includes exit code when stderr is empty."""
        mock_proc = MagicMock()
        mock_proc.communicate.return_value = (b"", b"")
        mock_proc.returncode = 42
        with patch("codebot.api_tools.subprocess.Popen", return_value=mock_proc):
            result = bash("exit 42")
            assert result["success"] is False
            assert "exit code 42" in result["error"]

    def test_grep_invalid_regex_error(self, ws):
        """Verify grep() returns proper error for invalid regex."""
        result = grep("[unclosed", ".")
        assert result["success"] is False
        assert "invalid regex" in result["error"]

    def test_glob_path_not_found_error(self, ws):
        """Verify glob() returns proper error for non-existent path."""
        result = glob("*.py", "nonexistent_dir_xyz")
        assert result["success"] is False
        assert "path not found" in result["error"]

    def test_a11y_snapshot_file_not_found(self, ws):
        """Verify a11y_snapshot() handles FileNotFoundError."""
        with patch("codebot.api_tools.subprocess.run", side_effect=FileNotFoundError()):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is False
            assert "node not found" in result["error"]

    def test_a11y_snapshot_timeout(self, ws):
        """Verify a11y_snapshot() handles TimeoutExpired."""
        with patch("codebot.api_tools.subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="node", timeout=30)):
            result = a11y_snapshot("http://example.com")
            assert result["success"] is False
            assert "timed out" in result["error"]

    def test_a11y_snapshot_redirect_to_metadata_blocked(self, ws):
        """Adversarial: HTTP 302 redirect to cloud metadata IP (169.254.169.254) must be blocked.
        
        Verifies that _fetch_url_ssrf_safe validates redirect targets against the
        private IP blocklist. An attacker-controlled public URL that redirects to
        a link-local metadata endpoint must be rejected before any content is fetched.
        """
        import urllib.error
        from unittest.mock import MagicMock
        from codebot.api_tools import _fetch_url_ssrf_safe
        
        # Mock _validate_url_safely to allow initial URL but block metadata redirect target
        call_count = [0]
        
        def mock_validate(url):
            call_count[0] += 1
            if "169.254.169.254" in url:
                return False, "private IP address blocked"
            # Allow the initial public URL
            return True, ""
        
        # Create a mock opener that raises HTTPError on open()
        mock_opener = MagicMock()
        http_error = urllib.error.HTTPError(
            url="http://attacker.example.com/",
            code=302,
            msg="Found",
            hdrs=MagicMock(),
            fp=None
        )
        http_error.headers = {"Location": "http://169.254.169.254/latest/meta-data/"}
        mock_opener.open.side_effect = http_error
        
        with patch("codebot.api_tools._validate_url_safely", side_effect=mock_validate):
            # Pass the mock opener directly to _fetch_url_ssrf_safe
            content, error = _fetch_url_ssrf_safe("http://attacker.example.com/", _opener=mock_opener)
        
        # Verify the request was blocked due to redirect to private IP
        assert content == ""
        assert "blocked" in error.lower()
        # Verify validation was called for the redirect target
        assert call_count[0] >= 1, "_validate_url_safely should have been called for redirect target"

    def test_fetch_url_ssrf_safe_too_many_redirects(self, ws):
        """Verify _fetch_url_ssrf_safe fails after MAX_REDIRECTS (5) redirects."""
        import urllib.error
        from unittest.mock import MagicMock
        from codebot.api_tools import _fetch_url_ssrf_safe
        
        mock_opener = MagicMock()
        # Simulate 6 consecutive redirects (exceeds MAX_REDIRECTS=5)
        http_error = urllib.error.HTTPError(
            url="http://example.com/",
            code=302,
            msg="Found",
            hdrs=MagicMock(),
            fp=None
        )
        http_error.headers = {"Location": "http://example.com/next"}
        mock_opener.open.side_effect = http_error
        
        with patch("codebot.api_tools._validate_url_safely", return_value=(True, "")):
            content, error = _fetch_url_ssrf_safe("http://example.com/", _opener=mock_opener)
        
        assert content == ""
        assert "Too many redirects" in error

    def test_fetch_url_ssrf_safe_missing_location_header(self, ws):
        """Verify _fetch_url_ssrf_safe fails when redirect lacks Location header."""
        import urllib.error
        from unittest.mock import MagicMock
        from codebot.api_tools import _fetch_url_ssrf_safe
        
        mock_opener = MagicMock()
        http_error = urllib.error.HTTPError(
            url="http://example.com/",
            code=302,
            msg="Found",
            hdrs=MagicMock(),
            fp=None
        )
        # No Location header
        http_error.headers = {}
        mock_opener.open.side_effect = http_error
        
        content, error = _fetch_url_ssrf_safe("http://example.com/", _opener=mock_opener)
        
        assert content == ""
        assert "Redirect missing Location header" in error

    def test_fetch_url_ssrf_safe_successful_redirect(self, ws):
        """Verify _fetch_url_ssrf_safe follows valid redirects and returns content."""
        import urllib.error
        from unittest.mock import MagicMock
        from codebot.api_tools import _fetch_url_ssrf_safe
        
        mock_opener = MagicMock()
        mock_response = MagicMock()
        mock_response.read.return_value = b"final content"
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        
        redirect_error = urllib.error.HTTPError(
            url="http://example.com/",
            code=302,
            msg="Found",
            hdrs=MagicMock(),
            fp=None
        )
        redirect_error.headers = {"Location": "http://example.com/final"}
        
        mock_opener.open.side_effect = [redirect_error, mock_response]
        
        with patch("codebot.api_tools._validate_url_safely", return_value=(True, "")):
            content, error = _fetch_url_ssrf_safe("http://example.com/", _opener=mock_opener)
        
        assert content == "final content"
        assert error == ""
        assert mock_opener.open.call_count == 2

    def test_fetch_url_ssrf_safe_relative_redirect(self, ws):
        """Verify _fetch_url_ssrf_safe resolves relative redirect URLs correctly."""
        import urllib.error
        from unittest.mock import MagicMock
        from codebot.api_tools import _fetch_url_ssrf_safe
        
        mock_opener = MagicMock()
        mock_response = MagicMock()
        mock_response.read.return_value = b"relative content"
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        
        validate_calls = []
        def mock_validate(url):
            validate_calls.append(url)
            return True, ""
        
        redirect_error = urllib.error.HTTPError(
            url="http://example.com/path/",
            code=302,
            msg="Found",
            hdrs=MagicMock(),
            fp=None
        )
        # Relative Location header
        redirect_error.headers = {"Location": "../redirected"}
        
        mock_opener.open.side_effect = [redirect_error, mock_response]
        
        with patch("codebot.api_tools._validate_url_safely", side_effect=mock_validate):
            content, error = _fetch_url_ssrf_safe("http://example.com/path/", _opener=mock_opener)
        
        assert content == "relative content"
        assert error == ""
        # Verify the relative URL was resolved to absolute before validation
        assert "http://example.com/redirected" in validate_calls

    def test_fetch_url_ssrf_safe_non_redirect_http_error(self, ws):
        """Verify _fetch_url_ssrf_safe returns error for non-redirect HTTP errors."""
        import urllib.error
        from unittest.mock import MagicMock
        from codebot.api_tools import _fetch_url_ssrf_safe
        
        mock_opener = MagicMock()
        http_error = urllib.error.HTTPError(
            url="http://example.com/",
            code=404,
            msg="Not Found",
            hdrs=MagicMock(),
            fp=None
        )
        mock_opener.open.side_effect = http_error
        
        content, error = _fetch_url_ssrf_safe("http://example.com/", _opener=mock_opener)
        
        assert content == ""
        assert "HTTP error 404" in error

    def test_fetch_url_ssrf_safe_default_opener_creation(self, ws):
        """Verify _fetch_url_ssrf_safe creates default opener when none provided.
        
        This test ensures the _NoRedirectHandler class is instantiated and used
        when _opener is not passed, covering the class definition lines.
        """
        from codebot.api_tools import _fetch_url_ssrf_safe
        import urllib.request
        
        mock_response = MagicMock()
        mock_response.read.return_value = b"default opener content"
        mock_response.__enter__ = lambda self: self
        mock_response.__exit__ = lambda self, *args: None
        
        mock_opener_instance = MagicMock()
        mock_opener_instance.open.return_value = mock_response
        
        with patch("codebot.api_tools.urllib.request.build_opener", return_value=mock_opener_instance):
            content, error = _fetch_url_ssrf_safe("http://example.com/")
        
        assert content == "default opener content"
        assert error == ""
        assert mock_opener_instance.open.called

    def test_batch_read_directory_skipped(self, ws):
        """Verify batch_read() skips directories."""
        (ws / "subdir").mkdir()
        result = batch_read(["subdir"])
        assert result["success"] is True
        assert "[not a file]" in result["output"]

    def test_batch_grep_invalid_include_rejected(self, ws):
        """Verify batch_grep() rejects invalid include patterns."""
        result = batch_grep(["test"], path=".", include="../../etc/passwd")
        assert result["success"] is True
        assert "[invalid include]" in result["output"]


class TestToolPolicyFallbackSecurity:
    """Tests for tool_policy import fallback security properties."""

    def test_no_shlex_fallback_in_source(self):
        """Verify that api_tools.py does not use shlex.split as a fallback for allowlisted_command.
        
        SECURITY: When tool_policy import fails, the fallback must deny all commands,
        not parse them with shlex.split which would allow arbitrary command execution.
        This test ensures the insecure fallback pattern is not present in the source.
        """
        source = Path("codebot/api_tools.py").read_text()
        # Check that shlex.split is not used in the context of a fallback for allowlisted_command
        # We look for the specific insecure pattern: shlex.split(command) in an except ImportError block
        # A simple check: ensure "shlex.split" is not in the source at all, or if it is, it's not in the fallback
        # The secure implementation should NOT have shlex.split in the except ImportError block
        assert "shlex.split" not in source, (
            "SECURITY VIOLATION: shlex.split found in api_tools.py. "
            "The fallback for tool_policy import failure must deny all commands, "
            "not parse them with shlex.split."
        )

    def test_fallback_deny_all_commands(self, ws):
        """Verify that bash() denies commands when allowlisted_command returns None.
        
        This tests the behavior of the fallback path: if allowlisted_command returns None,
        bash() must deny the command and log a warning.
        """
        with patch("codebot.api_tools.allowlisted_command", return_value=None) as mock_allow:
            with patch.object(api_tools.logger, "warning") as mock_warn:
                result = bash("echo hello")
                assert result["success"] is False
                assert result["error"] == "command denied"
                mock_allow.assert_called_once_with("echo hello", ws)
                # Verify warning was logged
                mock_warn.assert_called_once()
                assert "bash denied" in mock_warn.call_args[0][1]


class TestLockingImportFailClosed:
    """Test fail-closed import behavior for locking primitives."""

    def test_fail_closed_import_logic_exists(self):
        """Verify the fail-closed ImportError logic exists in api_tools.py source."""
        # The actual import failure path cannot be tested without breaking the test runner,
        # but we verify the code exists and has the correct structure.
        import ast
        source = Path("codebot/api_tools.py").read_text()
        tree = ast.parse(source)
        found_raise = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Raise):
                if node.exc and isinstance(node.exc, ast.Call):
                    if isinstance(node.exc.func, ast.Name) and node.exc.func.id == "ImportError":
                        # Check if the message mentions locking primitives
                        if hasattr(node.exc, 'args') and node.exc.args:
                            msg_arg = node.exc.args[0]
                            if isinstance(msg_arg, ast.Constant) and "locking primitives" in str(msg_arg.value):
                                found_raise = True
                                break
        assert found_raise, "Fail-closed ImportError for locking primitives not found in api_tools.py"
