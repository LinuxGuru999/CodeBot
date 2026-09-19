#!/usr/bin/env python3
"""Tests for codebot/auto_revert.py — critical build-safety module."""

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codebot.auto_revert import (
    _run,
    _gate_failures,
    _last_bot_commit,
    _commit_is_bot_authored,
    _revert_log,
    _already_reverted,
    _record_revert,
    _requeue_with_failure,
    process,
    REVERT_LOG,
)


class TestRun:
    """Tests for _run() subprocess wrapper."""

    def test_run_success(self, tmp_path):
        """Test successful command execution."""
        returncode, output = _run(["echo", "hello"], tmp_path)
        assert returncode == 0
        assert "hello" in output

    def test_run_failure(self, tmp_path):
        """Test command with non-zero exit code."""
        returncode, output = _run(["false"], tmp_path)
        assert returncode != 0

    def test_run_timeout(self, tmp_path):
        """Test command timeout handling."""
        # Use a command that sleeps longer than timeout
        returncode, output = _run(["sleep", "10"], tmp_path, timeout=1)
        assert returncode != 0
        assert "timed out" in output.lower() or "timeout" in output.lower()

    def test_run_exception(self, tmp_path):
        """Test exception handling in subprocess."""
        with patch("subprocess.run", side_effect=Exception("mock error")):
            returncode, output = _run(["fake_cmd"], tmp_path)
            assert returncode == 1
            assert "mock error" in output


class TestGateFailures:
    """Tests for _gate_failures() parsing logic."""

    def test_no_index_file(self, tmp_path):
        """Test when INDEX file doesn't exist."""
        with patch("codebot.auto_revert.BOTS_DIR", tmp_path):
            failures = _gate_failures()
            assert failures == []

    def test_parse_fail_entries(self, tmp_path):
        """Test parsing FAIL entries from INDEX."""
        index_dir = tmp_path / "docs" / "optimization"
        index_dir.mkdir(parents=True)
        index_file = index_dir / "build-gate-INDEX.md"
        index_file.write_text(
            "PASS some_file.py\n"
            "FAIL another_file.py - syntax error\n"
            "PASS third_file.py\n"
            "FAIL fourth_file.js - test failed\n"
        )

        with patch("codebot.auto_revert.BOTS_DIR", tmp_path):
            failures = _gate_failures()
            assert len(failures) == 2
            assert any("another_file.py" in f["line"] for f in failures)
            assert any("fourth_file.js" in f["line"] for f in failures)
            # Check files are extracted
            assert any("another_file.py" in f["files"] for f in failures)

    def test_parse_no_failures(self, tmp_path):
        """Test INDEX with no FAIL entries."""
        index_dir = tmp_path / "docs" / "optimization"
        index_dir.mkdir(parents=True)
        index_file = index_dir / "build-gate-INDEX.md"
        index_file.write_text("PASS file1.py\nPASS file2.py\n")

        with patch("codebot.auto_revert.BOTS_DIR", tmp_path):
            failures = _gate_failures()
            assert failures == []

    def test_parse_exception_handling(self, tmp_path):
        """Test graceful handling of malformed INDEX."""
        index_dir = tmp_path / "docs" / "optimization"
        index_dir.mkdir(parents=True)
        index_file = index_dir / "build-gate-INDEX.md"
        # Write content that might cause issues
        index_file.write_text("Some random content without proper format")

        with patch("codebot.auto_revert.BOTS_DIR", tmp_path):
            failures = _gate_failures()
            # Should not raise, may return empty or partial results
            assert isinstance(failures, list)


class TestLastBotCommit:
    """Tests for _last_bot_commit() git log lookup."""

    def test_commit_found(self, tmp_path):
        """Test finding a commit for a file."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "test-bot"], cwd=tmp_path, check=True, capture_output=True)

        # Create and commit a file
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")
        subprocess.run(["git", "add", "test.py"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        sha = _last_bot_commit(tmp_path, "test.py")
        assert sha is not None
        assert len(sha) == 40

    def test_commit_not_found(self, tmp_path):
        """Test when file has no commits."""
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)

        sha = _last_bot_commit(tmp_path, "nonexistent.py")
        assert sha is None

    def test_git_error(self, tmp_path):
        """Test graceful handling of git errors."""
        # Not a git repo
        sha = _last_bot_commit(tmp_path, "file.py")
        assert sha is None


class TestCommitIsBotAuthored:
    """Tests for _commit_is_bot_authored() author checking."""

    @pytest.mark.parametrize("author_name,expected", [
        ("codebot <bot@example.com>", True),
        ("sisyphus <sisyphus@example.com>", True),
        ("linuxguru999 <linux@example.com>", True),
        ("John Doe <john@example.com>", False),
        ("Human Developer <human@example.com>", False),
    ])
    def test_author_detection(self, tmp_path, author_name, expected):
        """Test bot author detection logic."""
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_path, check=True, capture_output=True)

        # Create and commit a file
        test_file = tmp_path / "test.py"
        test_file.write_text("print('hello')")
        subprocess.run(["git", "add", "test.py"], cwd=tmp_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_path, check=True, capture_output=True)

        # Get the commit SHA
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            check=True
        )
        sha = result.stdout.strip()

        # Mock the git log output to simulate different authors
        with patch("codebot.auto_revert._run") as mock_run:
            mock_run.return_value = (0, author_name)
            is_bot = _commit_is_bot_authored(tmp_path, sha)
            assert is_bot == expected

    def test_git_error_handling(self, tmp_path):
        """Test graceful handling of git errors."""
        is_bot = _commit_is_bot_authored(tmp_path, "invalid_sha")
        assert is_bot is False


class TestRevertLog:
    """Tests for revert log management functions."""

    def test_revert_log_empty(self, tmp_path):
        """Test reading non-existent revert log."""
        with patch("codebot.auto_revert.REVERT_LOG", tmp_path / "revert_log.json"):
            log = _revert_log()
            assert log == []

    def test_revert_log_valid(self, tmp_path):
        """Test reading valid revert log."""
        log_file = tmp_path / "revert_log.json"
        test_data = [{"sha": "abc123", "repo": "test"}]
        log_file.write_text(json.dumps(test_data))

        with patch("codebot.auto_revert.REVERT_LOG", log_file):
            log = _revert_log()
            assert log == test_data

    def test_revert_log_corrupt(self, tmp_path):
        """Test handling corrupt revert log."""
        log_file = tmp_path / "revert_log.json"
        log_file.write_text("not valid json")

        with patch("codebot.auto_revert.REVERT_LOG", log_file):
            log = _revert_log()
            assert log == []

    def test_already_reverted_true(self, tmp_path):
        """Test detecting already reverted commit."""
        log_file = tmp_path / "revert_log.json"
        test_data = [{"sha": "abc123", "repo": "test"}]
        log_file.write_text(json.dumps(test_data))

        with patch("codebot.auto_revert.REVERT_LOG", log_file):
            assert _already_reverted("abc123") is True
            assert _already_reverted("xyz789") is False

    def test_record_revert(self, tmp_path):
        """Test recording a revert entry."""
        log_file = tmp_path / "revert_log.json"

        with patch("codebot.auto_revert.REVERT_LOG", log_file):
            entry = {"sha": "def456", "repo": "test", "file": "file.py", "at": time.time()}
            _record_revert(entry)

            log = _revert_log()
            assert len(log) == 1
            assert log[0]["sha"] == "def456"

    def test_record_revert_limit(self, tmp_path):
        """Test revert log size limit (50 entries)."""
        log_file = tmp_path / "revert_log.json"

        with patch("codebot.auto_revert.REVERT_LOG", log_file):
            # Record 60 entries
            for i in range(60):
                _record_revert({"sha": f"sha{i}", "repo": "test", "file": "file.py", "at": time.time()})

            log = _revert_log()
            assert len(log) <= 50
            # Most recent entries should be kept
            assert any("sha59" in str(entry) for entry in log)


class TestRequeueWithFailure:
    """Tests for _requeue_with_failure() queue management."""

    def test_requeue_success(self, tmp_path):
        """Test adding requeue entry to QUEUE.md."""
        queue_dir = tmp_path / "docs" / "triage"
        queue_dir.mkdir(parents=True)
        queue_file = queue_dir / "QUEUE.md"
        queue_file.write_text("# Queue\n\n")

        with patch("codebot.auto_revert.WORK_ROOT", tmp_path):
            _requeue_with_failure("test_hint", "test_failure")

            content = queue_file.read_text()
            assert "auto-revert test_hint" in content
            assert "test_failure" in content

    def test_requeue_no_duplicate(self, tmp_path):
        """Test preventing duplicate requeue entries."""
        queue_dir = tmp_path / "docs" / "triage"
        queue_dir.mkdir(parents=True)
        queue_file = queue_dir / "QUEUE.md"
        initial_content = "# Queue\n\nauto-revert test_hint\n"
        queue_file.write_text(initial_content)

        with patch("codebot.auto_revert.WORK_ROOT", tmp_path):
            _requeue_with_failure("test_hint", "test_failure")

            content = queue_file.read_text()
            # Should only have one entry
            assert content.count("auto-revert test_hint") == 1

    def test_requeue_no_queue_file(self, tmp_path):
        """Test graceful handling when QUEUE.md doesn't exist."""
        with patch("codebot.auto_revert.WORK_ROOT", tmp_path):
            # Should not raise
            _requeue_with_failure("test_hint", "test_failure")


class TestProcess:
    """Tests for main process() function."""

    def test_process_no_failures(self, tmp_path):
        """Test process with no gate failures."""
        with patch("codebot.auto_revert._gate_failures", return_value=[]):
            result = process()
            assert result["failures"] == 0
            assert result["reverted"] == []
            assert result["skipped"] == []

    def test_process_revert_success(self, tmp_path):
        """Test successful revert flow."""
        # Mock gate failure
        fail_entry = {
            "line": "FAIL test.py - syntax error",
            "files": ["test.py"],
            "source": "test"
        }

        # Mock repo setup
        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        with patch("codebot.auto_revert._gate_failures", return_value=[fail_entry]), \
             patch("codebot.auto_revert.REPOS", [repo_path]), \
             patch("codebot.auto_revert._last_bot_commit", return_value="abc123"), \
             patch("codebot.auto_revert._commit_is_bot_authored", return_value=True), \
             patch("codebot.auto_revert._already_reverted", return_value=False), \
             patch("codebot.auto_revert._run") as mock_run, \
             patch("codebot.auto_revert._record_revert") as mock_record, \
             patch("codebot.auto_revert._requeue_with_failure") as mock_requeue:

            # Mock successful revert and commit
            mock_run.side_effect = [(0, ""), (0, "")]  # revert success, commit success

            result = process()

            assert result["failures"] == 1
            assert len(result["reverted"]) == 1
            assert result["reverted"][0]["sha"] == "abc123"
            mock_record.assert_called_once()
            mock_requeue.assert_called_once()

    def test_process_skip_already_reverted(self, tmp_path):
        """Test skipping already reverted commits."""
        fail_entry = {
            "line": "FAIL test.py - error",
            "files": ["test.py"],
            "source": "test"
        }

        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        with patch("codebot.auto_revert._gate_failures", return_value=[fail_entry]), \
             patch("codebot.auto_revert.REPOS", [repo_path]), \
             patch("codebot.auto_revert._last_bot_commit", return_value="abc123"), \
             patch("codebot.auto_revert._already_reverted", return_value=True):

            result = process()

            assert len(result["skipped"]) == 1
            assert result["skipped"][0]["why"] == "already-reverted"
            assert result["reverted"] == []

    def test_process_skip_human_authored(self, tmp_path):
        """Test skipping human-authored commits."""
        fail_entry = {
            "line": "FAIL test.py - error",
            "files": ["test.py"],
            "source": "test"
        }

        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        with patch("codebot.auto_revert._gate_failures", return_value=[fail_entry]), \
             patch("codebot.auto_revert.REPOS", [repo_path]), \
             patch("codebot.auto_revert._last_bot_commit", return_value="abc123"), \
             patch("codebot.auto_revert._already_reverted", return_value=False), \
             patch("codebot.auto_revert._commit_is_bot_authored", return_value=False):

            result = process()

            assert len(result["skipped"]) == 1
            assert result["skipped"][0]["why"] == "not-bot-authored"
            assert result["reverted"] == []

    def test_process_skip_revert_conflict(self, tmp_path):
        """Test skipping when revert has conflicts."""
        fail_entry = {
            "line": "FAIL test.py - error",
            "files": ["test.py"],
            "source": "test"
        }

        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        with patch("codebot.auto_revert._gate_failures", return_value=[fail_entry]), \
             patch("codebot.auto_revert.REPOS", [repo_path]), \
             patch("codebot.auto_revert._last_bot_commit", return_value="abc123"), \
             patch("codebot.auto_revert._already_reverted", return_value=False), \
             patch("codebot.auto_revert._commit_is_bot_authored", return_value=True), \
             patch("codebot.auto_revert._run") as mock_run:

            # Mock revert failure (conflict)
            mock_run.side_effect = [(1, "conflict error"), (0, "")]  # revert fails, abort succeeds

            result = process()

            assert len(result["skipped"]) == 1
            assert "revert-conflict" in result["skipped"][0]["why"]
            assert result["reverted"] == []

    def test_process_skip_commit_failed(self, tmp_path):
        """Test skipping when commit after revert fails."""
        fail_entry = {
            "line": "FAIL test.py - error",
            "files": ["test.py"],
            "source": "test"
        }

        repo_path = tmp_path / "test_repo"
        repo_path.mkdir()
        (repo_path / ".git").mkdir()

        with patch("codebot.auto_revert._gate_failures", return_value=[fail_entry]), \
             patch("codebot.auto_revert.REPOS", [repo_path]), \
             patch("codebot.auto_revert._last_bot_commit", return_value="abc123"), \
             patch("codebot.auto_revert._already_reverted", return_value=False), \
             patch("codebot.auto_revert._commit_is_bot_authored", return_value=True), \
             patch("codebot.auto_revert._run") as mock_run:

            # Mock revert success but commit failure
            mock_run.side_effect = [(0, ""), (1, "commit failed")]  # revert succeeds, commit fails

            result = process()

            assert len(result["skipped"]) == 1
            assert result["skipped"][0]["why"] == "commit-failed"
            assert result["reverted"] == []


class TestMain:
    """Tests for main() CLI entry point."""

    def test_main_execution(self, capsys):
        """Test main() prints summary."""
        with patch("codebot.auto_revert.process") as mock_process:
            mock_process.return_value = {
                "failures": 2,
                "reverted": [{"sha": "abc", "repo": "test", "file": "file.py"}],
                "skipped": [{"sha": "xyz", "repo": "test", "file": "file2.py", "why": "test"}]
            }

            # Import and call main (need to reload to pick up mocks)
            import importlib
            import codebot.auto_revert as ar
            importlib.reload(ar)
            ar.main()

            captured = capsys.readouterr()
            assert "gate failures=2" in captured.out
            assert "reverted=1" in captured.out
            assert "skipped=1" in captured.out
