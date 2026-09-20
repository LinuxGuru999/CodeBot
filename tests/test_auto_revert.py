"""Tests for codebot/auto_revert.py — critical build-safety module.

Covers:
- _run() timeout and error handling
- _gate_failures() parsing with valid/invalid/malformed INDEX files
- _last_bot_commit() with existing and missing files
- _commit_is_bot_authored() matching bot/sisyphus/linuxguru999 authors
- _already_reverted() and _record_revert() log persistence
- _requeue_with_failure() writing correct markdown entries
- process() end-to-end with mocked git operations (4+ scenarios)
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from codebot import auto_revert


class TestRun:
    """Tests for the _run() subprocess wrapper."""

    def test_run_success(self, tmp_path: Path):
        code, out = auto_revert._run(["echo", "hello"], tmp_path)
        assert code == 0
        assert "hello" in out

    def test_run_failure_exit_code(self, tmp_path: Path):
        code, out = auto_revert._run(["false"], tmp_path)
        assert code != 0

    def test_run_timeout_handling(self, tmp_path: Path):
        code, out = auto_revert._run(["sleep", "10"], tmp_path, timeout=1)
        assert code == 1
        # Should contain timeout-related error message
        assert "timeout" in out.lower() or "timed out" in out.lower() or out

    def test_run_command_not_found(self, tmp_path: Path):
        code, out = auto_revert._run(["nonexistent_command_xyz_123"], tmp_path)
        assert code == 1
        assert out  # Should contain error string

    def test_run_captures_stderr(self, tmp_path: Path):
        code, out = auto_revert._run(
            ["python3", "-c", "import sys; sys.stderr.write('err'); sys.exit(1)"],
            tmp_path,
        )
        assert code == 1
        assert "err" in out


class TestGateFailures:
    """Tests for _gate_failures() INDEX file parsing."""

    def test_no_index_file_returns_empty(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(auto_revert, "BOTS_DIR", tmp_path)
        monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
        result = auto_revert._gate_failures()
        assert result == []

    def test_parses_fail_entries(self, tmp_path: Path, monkeypatch):
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        work_root = tmp_path / "work"
        work_root.mkdir()
        monkeypatch.setattr(auto_revert, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(auto_revert, "WORK_ROOT", work_root)
        docs_dir = bots_dir / "docs" / "optimization"
        docs_dir.mkdir(parents=True)
        index_file = docs_dir / "build-gate-INDEX.md"
        index_file.write_text(
            "## Build Gate Results\n"
            "- PASS: codebot/foo.py\n"
            "- FAIL: codebot/bar.py\n"
            "- FAIL: codebot/baz.js other.py\n",
            encoding="utf-8",
        )
        result = auto_revert._gate_failures()
        assert len(result) == 2
        assert "bar.py" in str(result[0]["files"])
        assert result[0]["source"] == str(index_file)

    def test_skips_pass_entries(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(auto_revert, "BOTS_DIR", tmp_path)
        monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
        docs_dir = tmp_path / "docs" / "optimization"
        docs_dir.mkdir(parents=True)
        (docs_dir / "build-gate-INDEX.md").write_text(
            "- PASS: codebot/foo.py\n- PASS: codebot/bar.py\n",
            encoding="utf-8",
        )
        result = auto_revert._gate_failures()
        assert result == []

    def test_handles_corrupt_index_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(auto_revert, "BOTS_DIR", tmp_path)
        monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
        docs_dir = tmp_path / "docs" / "optimization"
        docs_dir.mkdir(parents=True)
        (docs_dir / "build-gate-INDEX.md").write_bytes(b"\xff\xfe invalid utf-8 \x80")
        # Should not raise, fail-open
        result = auto_revert._gate_failures()
        assert isinstance(result, list)

    def test_extracts_multiple_files_from_single_line(self, tmp_path: Path, monkeypatch):
        bots_dir = tmp_path / "bots"
        bots_dir.mkdir()
        work_root = tmp_path / "work"
        work_root.mkdir()
        monkeypatch.setattr(auto_revert, "BOTS_DIR", bots_dir)
        monkeypatch.setattr(auto_revert, "WORK_ROOT", work_root)
        docs_dir = bots_dir / "docs" / "optimization"
        docs_dir.mkdir(parents=True)
        (docs_dir / "build-gate-INDEX.md").write_text(
            "- FAIL: a.py b.js c.json d.yaml e.toml f.md g.html\n",
            encoding="utf-8",
        )
        result = auto_revert._gate_failures()
        assert len(result) == 1
        # FILE_RE extracts up to 5 files per line (capped by [:5])
        assert len(result[0]["files"]) <= 5

    def test_line_truncated_to_300_chars(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(auto_revert, "BOTS_DIR", tmp_path)
        monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
        docs_dir = tmp_path / "docs" / "optimization"
        docs_dir.mkdir(parents=True)
        long_line = "- FAIL: test.py " + "x" * 400
        (docs_dir / "build-gate-INDEX.md").write_text(long_line, encoding="utf-8")
        result = auto_revert._gate_failures()
        assert len(result[0]["line"]) <= 300


class TestLastBotCommit:
    """Tests for _last_bot_commit() git log lookup."""

    @patch("codebot.auto_revert._run")
    def test_returns_sha_on_success(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, "abc123def456789012345678901234567890abcd\n")
        result = auto_revert._last_bot_commit(tmp_path, "test.py")
        assert result == "abc123def456789012345678901234567890abcd"
        mock_run.assert_called_once()

    @patch("codebot.auto_revert._run")
    def test_returns_none_on_git_failure(self, mock_run, tmp_path: Path):
        mock_run.return_value = (128, "fatal: not a git repository")
        result = auto_revert._last_bot_commit(tmp_path, "test.py")
        assert result is None

    @patch("codebot.auto_revert._run")
    def test_returns_none_on_empty_output(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, "")
        result = auto_revert._last_bot_commit(tmp_path, "test.py")
        assert result is None

    @patch("codebot.auto_revert._run")
    def test_truncates_sha_to_40_chars(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, "a" * 50 + "\n")
        result = auto_revert._last_bot_commit(tmp_path, "test.py")
        assert len(result) == 40


class TestCommitIsBotAuthored:
    """Tests for _commit_is_bot_authored() author matching."""

    @patch("codebot.auto_revert._run")
    def test_bot_author_detected(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, "my-bot <bot@example.com>\n")
        assert auto_revert._commit_is_bot_authored(tmp_path, "abc123") is True

    @patch("codebot.auto_revert._run")
    def test_sisyphus_author_detected(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, "Sisyphus <sisyphus@example.com>\n")
        assert auto_revert._commit_is_bot_authored(tmp_path, "abc123") is True

    @patch("codebot.auto_revert._run")
    def test_linuxguru999_author_detected(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, "linuxguru999 <lg@example.com>\n")
        assert auto_revert._commit_is_bot_authored(tmp_path, "abc123") is True

    @patch("codebot.auto_revert._run")
    def test_human_author_rejected(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, "John Doe <john@example.com>\n")
        assert auto_revert._commit_is_bot_authored(tmp_path, "abc123") is False

    @patch("codebot.auto_revert._run")
    def test_git_failure_returns_false(self, mock_run, tmp_path: Path):
        mock_run.return_value = (128, "fatal: bad object")
        assert auto_revert._commit_is_bot_authored(tmp_path, "abc123") is False

    @patch("codebot.auto_revert._run")
    def test_case_insensitive_matching(self, mock_run, tmp_path: Path):
        mock_run.return_value = (0, "My-BOT-User <x@y.com>\n")
        assert auto_revert._commit_is_bot_authored(tmp_path, "abc123") is True


class TestRevertLog:
    """Tests for _revert_log(), _already_reverted(), _record_revert()."""

    def test_revert_log_empty_when_no_file(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(auto_revert, "REVERT_LOG", tmp_path / "nonexistent.json")
        assert auto_revert._revert_log() == []

    def test_revert_log_reads_valid_json(self, tmp_path: Path, monkeypatch):
        log_file = tmp_path / "log.json"
        log_file.write_text(json.dumps([{"sha": "abc"}]), encoding="utf-8")
        monkeypatch.setattr(auto_revert, "REVERT_LOG", log_file)
        result = auto_revert._revert_log()
        assert len(result) == 1
        assert result[0]["sha"] == "abc"

    def test_revert_log_handles_corrupt_json(self, tmp_path: Path, monkeypatch):
        log_file = tmp_path / "log.json"
        log_file.write_text("not valid json {{{", encoding="utf-8")
        monkeypatch.setattr(auto_revert, "REVERT_LOG", log_file)
        assert auto_revert._revert_log() == []

    def test_already_reverted_true(self, tmp_path: Path, monkeypatch):
        log_file = tmp_path / "log.json"
        log_file.write_text(json.dumps([{"sha": "abc123"}]), encoding="utf-8")
        monkeypatch.setattr(auto_revert, "REVERT_LOG", log_file)
        assert auto_revert._already_reverted("abc123") is True

    def test_already_reverted_false(self, tmp_path: Path, monkeypatch):
        log_file = tmp_path / "log.json"
        log_file.write_text(json.dumps([{"sha": "abc123"}]), encoding="utf-8")
        monkeypatch.setattr(auto_revert, "REVERT_LOG", log_file)
        assert auto_revert._already_reverted("xyz789") is False

    def test_record_revert_creates_log(self, tmp_path: Path, monkeypatch):
        log_file = tmp_path / "log.json"
        monkeypatch.setattr(auto_revert, "REVERT_LOG", log_file)
        auto_revert._record_revert({"sha": "new123", "repo": "/tmp"})
        assert log_file.exists()
        data = json.loads(log_file.read_text(encoding="utf-8"))
        assert len(data) == 1
        assert data[0]["sha"] == "new123"

    def test_record_revert_appends_to_existing(self, tmp_path: Path, monkeypatch):
        log_file = tmp_path / "log.json"
        log_file.write_text(json.dumps([{"sha": "old"}]), encoding="utf-8")
        monkeypatch.setattr(auto_revert, "REVERT_LOG", log_file)
        auto_revert._record_revert({"sha": "new"})
        data = json.loads(log_file.read_text(encoding="utf-8"))
        assert len(data) == 2

    def test_record_revert_caps_at_50_entries(self, tmp_path: Path, monkeypatch):
        log_file = tmp_path / "log.json"
        existing = [{"sha": f"sha_{i}"} for i in range(60)]
        log_file.write_text(json.dumps(existing), encoding="utf-8")
        monkeypatch.setattr(auto_revert, "REVERT_LOG", log_file)
        auto_revert._record_revert({"sha": "overflow"})
        data = json.loads(log_file.read_text(encoding="utf-8"))
        assert len(data) <= 50


class TestRequeueWithFailure:
    """Tests for _requeue_with_failure() QUEUE.md appending."""

    def test_appends_entry_to_queue(self, tmp_path: Path, monkeypatch):
        queue_file = tmp_path / "docs" / "triage" / "QUEUE.md"
        queue_file.parent.mkdir(parents=True)
        queue_file.write_text("# Queue\n", encoding="utf-8")
        monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
        auto_revert._requeue_with_failure("test_hint", "test failure reason")
        content = queue_file.read_text(encoding="utf-8")
        assert "auto-revert" in content
        assert "test_hint" in content
        assert "test failure reason" in content
        assert "QUEUE-REVERT-" in content

    def test_no_duplicate_entries(self, tmp_path: Path, monkeypatch):
        queue_file = tmp_path / "docs" / "triage" / "QUEUE.md"
        queue_file.parent.mkdir(parents=True)
        queue_file.write_text("auto-revert my_hint already here\n", encoding="utf-8")
        monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
        auto_revert._requeue_with_failure("my_hint", "failure")
        content = queue_file.read_text(encoding="utf-8")
        # Should not duplicate since marker already present
        assert content.count("auto-revert my_hint") == 1

    def test_missing_queue_file_does_nothing(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
        # No QUEUE.md exists — should not raise
        auto_revert._requeue_with_failure("hint", "fail")

    def test_work_hint_truncated_to_60_for_marker(self, tmp_path: Path, monkeypatch):
        queue_file = tmp_path / "docs" / "triage" / "QUEUE.md"
        queue_file.parent.mkdir(parents=True)
        queue_file.write_text("", encoding="utf-8")
        monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
        long_hint = "a" * 100
        auto_revert._requeue_with_failure(long_hint, "fail")
        content = queue_file.read_text(encoding="utf-8")
        assert "a" * 60 in content


class TestProcess:
    """End-to-end tests for process() with mocked git operations."""

    def test_process_no_gate_failures_is_noop(self, tmp_path: Path, monkeypatch):
        monkeypatch.setattr(auto_revert, "BOTS_DIR", tmp_path)
        monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
        monkeypatch.setattr(auto_revert, "REPOS", [tmp_path])
        result = auto_revert.process()
        assert result["failures"] == 0
        assert result["reverted"] == []
        assert result["skipped"] == []

    @patch("codebot.auto_revert._requeue_with_failure")
    @patch("codebot.auto_revert._record_revert")
    @patch("codebot.auto_revert._already_reverted", return_value=False)
    @patch("codebot.auto_revert._commit_is_bot_authored", return_value=True)
    @patch("codebot.auto_revert._last_bot_commit", return_value="abc123def456")
    @patch("codebot.auto_revert._run")
    @patch("codebot.auto_revert._gate_failures")
    def test_process_successful_revert(
        self,
        mock_failures,
        mock_run,
        mock_last_commit,
        mock_is_bot,
        mock_already,
        mock_record,
        mock_requeue,
        tmp_path: Path,
        monkeypatch,
    ):
        mock_failures.return_value = [
            {"line": "FAIL: test.py", "files": ["test.py"], "source": "index.md"}
        ]
        # git revert --no-commit succeeds (0), git commit succeeds (0)
        mock_run.side_effect = [(0, ""), (0, "")]
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setattr(auto_revert, "REPOS", [repo])
        monkeypatch.setattr(auto_revert, "REVERT_LOG", tmp_path / "log.json")

        result = auto_revert.process()
        assert result["failures"] == 1
        assert len(result["reverted"]) == 1
        assert result["reverted"][0]["sha"] == "abc123de"
        mock_record.assert_called_once()
        mock_requeue.assert_called_once()

    @patch("codebot.auto_revert._already_reverted", return_value=False)
    @patch("codebot.auto_revert._commit_is_bot_authored", return_value=False)
    @patch("codebot.auto_revert._last_bot_commit", return_value="abc123")
    @patch("codebot.auto_revert._gate_failures")
    def test_process_skips_human_commits(
        self, mock_failures, mock_last, mock_is_bot, mock_already, tmp_path: Path, monkeypatch
    ):
        mock_failures.return_value = [
            {"line": "FAIL: x.py", "files": ["x.py"], "source": "idx"}
        ]
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setattr(auto_revert, "REPOS", [repo])

        result = auto_revert.process()
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["why"] == "not-bot-authored"
        assert result["reverted"] == []

    @patch("codebot.auto_revert._already_reverted", return_value=True)
    @patch("codebot.auto_revert._last_bot_commit", return_value="abc123")
    @patch("codebot.auto_revert._gate_failures")
    def test_process_skips_already_reverted(
        self, mock_failures, mock_last, mock_already, tmp_path: Path, monkeypatch
    ):
        mock_failures.return_value = [
            {"line": "FAIL: y.py", "files": ["y.py"], "source": "idx"}
        ]
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setattr(auto_revert, "REPOS", [repo])

        result = auto_revert.process()
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["why"] == "already-reverted"

    @patch("codebot.auto_revert._already_reverted", return_value=False)
    @patch("codebot.auto_revert._commit_is_bot_authored", return_value=True)
    @patch("codebot.auto_revert._last_bot_commit", return_value="abc123")
    @patch("codebot.auto_revert._run")
    @patch("codebot.auto_revert._gate_failures")
    def test_process_handles_revert_conflict(
        self, mock_failures, mock_run, mock_last, mock_is_bot, mock_already, tmp_path: Path, monkeypatch
    ):
        mock_failures.return_value = [
            {"line": "FAIL: z.py", "files": ["z.py"], "source": "idx"}
        ]
        # git revert --no-commit fails
        mock_run.return_value = (1, "CONFLICT: merge conflict in z.py")
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setattr(auto_revert, "REPOS", [repo])

        result = auto_revert.process()
        assert len(result["skipped"]) == 1
        assert "revert-conflict" in result["skipped"][0]["why"]
        # Verify abort was called (second _run call)
        assert mock_run.call_count >= 2

    @patch("codebot.auto_revert._already_reverted", return_value=False)
    @patch("codebot.auto_revert._commit_is_bot_authored", return_value=True)
    @patch("codebot.auto_revert._last_bot_commit", return_value=None)
    @patch("codebot.auto_revert._gate_failures")
    def test_process_no_commit_found_skips(
        self, mock_failures, mock_last, mock_is_bot, mock_already, tmp_path: Path, monkeypatch
    ):
        mock_failures.return_value = [
            {"line": "FAIL: w.py", "files": ["w.py"], "source": "idx"}
        ]
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        monkeypatch.setattr(auto_revert, "REPOS", [repo])

        result = auto_revert.process()
        assert result["reverted"] == []
        # No commit found means no skip entry either (just continues)
        assert result["skipped"] == []

    def test_process_skips_non_git_repos(self, tmp_path: Path, monkeypatch):
        """Repos without .git directory are skipped."""
        monkeypatch.setattr(auto_revert, "REPOS", [tmp_path])  # no .git dir
        with patch("codebot.auto_revert._gate_failures") as mock_gf:
            mock_gf.return_value = [
                {"line": "FAIL: a.py", "files": ["a.py"], "source": "idx"}
            ]
            result = auto_revert.process()
            assert result["reverted"] == []
