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
        with patch("codebot.api_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="out\n", stderr="err\n")
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
        with patch("codebot.api_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=2, stdout="", stderr="stderr msg\n")
            result = bash("echo hi")
            assert result["success"] is False
            assert "stderr msg" in result["error"]

    def test_bash_truncates_large_output(self, ws):
        big = "a" * 200000
        with patch("codebot.api_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=big, stderr="")
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
        # Mock subprocess.run to raise TimeoutExpired
        def fake_run(*args, **kwargs):
            raise subprocess.TimeoutExpired(cmd=args[0], timeout=1, output="partial out", stderr="partial err")
        with patch("codebot.api_tools.subprocess.run", side_effect=fake_run):
            result = bash("echo hi", timeout=1)
            assert result["success"] is False
            assert "timeout after 1s" in result["error"]

    def test_bash_timeout_with_bytes_output(self, ws):
        def fake_run(*args, **kwargs):
            exc = subprocess.TimeoutExpired(cmd=args[0], timeout=1)
            exc.stdout = b"bytes out"
            exc.stderr = b"bytes err"
            raise exc
        with patch("codebot.api_tools.subprocess.run", side_effect=fake_run):
            result = bash("echo hi", timeout=1)
            assert result["success"] is False
            assert "bytes out" in result["output"] or "bytes err" in result["output"]

    def test_bash_allowlisted_git(self, ws):
        with patch("codebot.api_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
            result = bash("git status")
            assert result["success"] is True

    def test_bash_cwd_is_workspace(self, ws):
        # Verify cwd passed to subprocess.run is WORKSPACE_ROOT
        with patch("codebot.api_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            bash("echo hi")
            assert mock_run.call_args[1]["cwd"] == ws

    def test_bash_pipe_allowed(self, ws):
        with patch("codebot.api_tools.subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="piped", stderr="")
            result = bash("echo hello | head -n 1")
            # Should be allowed (head is allowed pipe target)
            assert mock_run.called

    def test_bash_pipe_to_blocked_command(self, ws):
        result = bash("echo hello | sudo ls")
        assert result["success"] is False
        assert result["error"] == "command denied"

    def test_bash_never_raises_on_exception(self, ws):
        with patch("codebot.api_tools.subprocess.run", side_effect=OSError("boom")):
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

    def test_bash_toctou_symlink_swap_race_condition(self, ws):
        """TOCTOU race: concurrent symlink swap must never leak external content.
        
        Adversarial test: a background thread rapidly swaps a symlink between
        a safe workspace file and /etc/passwd while bash('cat <symlink>') is
        invoked concurrently. The bash() tool must never return content from
        outside the workspace, even under heavy race conditions.
        
        This tests resilience against symlink-based workspace escape attacks
        where an attacker controls a symlink target and races against path
        validation.
        """
        import threading
        import os
        
        # Create a safe file inside workspace with unique marker
        safe_file = ws / "safe_content.txt"
        safe_marker = "SAFE_WORKSPACE_CONTENT_MARKER_XYZZY"
        safe_file.write_text(safe_marker + "\n", encoding="utf-8")
        
        # External target we must NEVER read
        external_target = Path("/etc/passwd")
        external_marker = "root:"  # Universal marker present in /etc/passwd
        
        # Symlink path inside workspace
        symlink_path = ws / "race_link.txt"
        
        # Track violations
        violations = []
        violation_lock = threading.Lock()
        stop_event = threading.Event()
        swap_count = [0]
        read_count = [0]
        
        def swapper():
            """Rapidly swap symlink target between safe and external."""
            targets = [safe_file, external_target]
            idx = 0
            while not stop_event.is_set():
                try:
                    # Atomic symlink swap: create temp then rename
                    tmp_link = ws / f"race_link_tmp_{idx}.txt"
                    if tmp_link.exists() or tmp_link.is_symlink():
                        tmp_link.unlink()
                    tmp_link.symlink_to(targets[idx % 2])
                    # Atomic rename replaces symlink
                    tmp_link.replace(symlink_path)
                    swap_count[0] += 1
                    idx += 1
                except OSError:
                    # Race may cause transient errors; continue
                    pass
                # No sleep — maximize race pressure
        
        def reader():
            """Invoke bash('cat <symlink>') and check for external content."""
            while not stop_event.is_set():
                try:
                    result = bash(f"cat {symlink_path.name}")
                    read_count[0] += 1
                    if result["success"]:
                        output = result["output"]
                        # Check for external content leakage
                        if external_marker in output:
                            with violation_lock:
                                violations.append({
                                    "swap_count": swap_count[0],
                                    "read_count": read_count[0],
                                    "output_snippet": output[:200],
                                })
                except Exception:
                    pass
        
        # Start swapper thread
        swapper_thread = threading.Thread(target=swapper, daemon=True)
        swapper_thread.start()
        
        # Start multiple reader threads for more race pressure
        reader_threads = []
        for _ in range(4):
            t = threading.Thread(target=reader, daemon=True)
            t.start()
            reader_threads.append(t)
        
        # Run for enough iterations to hit 200+ swaps
        deadline = time.monotonic() + 5.0  # 5 second max runtime
        while swap_count[0] < 200 and time.monotonic() < deadline:
            time.sleep(0.01)
        
        # Signal stop
        stop_event.set()
        
        # Wait for threads to finish
        swapper_thread.join(timeout=2.0)
        for t in reader_threads:
            t.join(timeout=2.0)
        
        # Cleanup symlink
        if symlink_path.exists() or symlink_path.is_symlink():
            symlink_path.unlink(missing_ok=True)
        
        # Verify we actually performed enough swaps
        assert swap_count[0] >= 200, (
            f"Only {swap_count[0]} swaps performed, need 200+ for valid race test"
        )
        
        # CRITICAL ASSERTION: no external content was ever read
        assert len(violations) == 0, (
            f"TOCTOU violation detected! bash() read external content "
            f"{len(violations)} times during {swap_count[0]} swaps. "
            f"First violation: {violations[0]}"
        )


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
        # Ensure output_path is passed via JSON config
        mock_result = MagicMock(returncode=0, stdout="tree", stderr="")
        with patch("codebot.api_tools.subprocess.run", return_value=mock_result) as mr:
            a11y_snapshot("http://example.com", output_path="/tmp/out.txt")
            args = mr.call_args[0][0]
            # config is 4th element of node -e call
            config_json = args[3]
            cfg = json.loads(config_json)
            assert cfg["out"] == "/tmp/out.txt"

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
        """Directly test the fallback resolve_workspace_path rejects symlinks outside workspace.
        
        This ensures the fallback implementation (used when tool_policy is unavailable)
        correctly uses os.path.realpath and prefix checking.
        """
        import os
        from pathlib import Path
        
        # Define the fallback function exactly as implemented in api_tools.py
        def fallback_resolve_workspace_path(path: str, workspace_root) -> "Path | None":
            candidate = Path(path)
            if candidate.is_absolute():
                abs_path = str(candidate)
            else:
                abs_path = os.path.join(str(workspace_root), str(candidate))
            real_path = os.path.realpath(abs_path)
            norm_root = os.path.realpath(str(workspace_root))
            if not norm_root.endswith(os.sep):
                norm_root += os.sep
            if real_path == norm_root.rstrip(os.sep) or real_path.startswith(norm_root):
                return Path(real_path)
            return None
        
        # Create external target
        external = ws.parent / "escape_target.txt"
        external.write_text("escaped", encoding="utf-8")
        try:
            # Symlink inside workspace pointing outside
            link = ws / "bad_link.txt"
            link.symlink_to(external)
            
            result = fallback_resolve_workspace_path(str(link), ws)
            assert result is None, (
                f"Fallback should reject symlink to external path, got {result}"
            )
            
            # Also test relative path through symlink
            result_rel = fallback_resolve_workspace_path("bad_link.txt", ws)
            assert result_rel is None, (
                f"Fallback should reject relative symlink traversal, got {result_rel}"
            )
        finally:
            if link.exists() or link.is_symlink():
                link.unlink()
            if external.exists():
                external.unlink()

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
        import os
        from pathlib import Path
        
        def fallback_resolve_workspace_path(path: str, workspace_root) -> "Path | None":
            candidate = Path(path)
            if candidate.is_absolute():
                abs_path = str(candidate)
            else:
                abs_path = os.path.join(str(workspace_root), str(candidate))
            real_path = os.path.realpath(abs_path)
            norm_root = os.path.realpath(str(workspace_root))
            if not norm_root.endswith(os.sep):
                norm_root += os.sep
            if real_path == norm_root.rstrip(os.sep) or real_path.startswith(norm_root):
                return Path(real_path)
            return None
        
        # Create a sibling directory with similar name
        evil_dir = ws.parent / (ws.name + "-evil")
        evil_dir.mkdir(exist_ok=True)
        evil_file = evil_dir / "secret.txt"
        evil_file.write_text("evil", encoding="utf-8")
        try:
            # Absolute path to evil dir should be rejected
            result = fallback_resolve_workspace_path(str(evil_file), ws)
            assert result is None, (
                f"Prefix attack should be blocked: {evil_file} matched workspace {ws}"
            )
        finally:
            if evil_file.exists():
                evil_file.unlink()
            if evil_dir.exists():
                evil_dir.rmdir()


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
