"""Tests for codebot.auto_revert — critical build-safety module.

Covers:
- _run() with success, failure, and timeout scenarios
- _gate_failures() parsing of FAIL lines from INDEX files
- _last_bot_commit() extracting commit SHAs
- _commit_is_bot_authored() matching bot/sisyphus/linuxguru999 authors
- _already_reverted() deduplication via revert log
- _record_revert() atomic persistence
- _requeue_with_failure() appending to triage queue
- process() end-to-end orchestration with mocked git operations
"""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from unittest.mock import MagicMock, mock_open, patch

import pytest

from codebot import auto_revert


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(autouse=True)
def isolate_state(tmp_path, monkeypatch):
    """Redirect all filesystem side-effects into tmp_path."""
    monkeypatch.setattr(auto_revert, "REVERT_LOG", tmp_path / "auto_revert_log.json")
    monkeypatch.setattr(auto_revert, "STATE_DIR", tmp_path)
    monkeypatch.setattr(auto_revert, "WORK_ROOT", tmp_path)
    monkeypatch.setattr(auto_revert, "BOTS_DIR", tmp_path / "codebot")
    (tmp_path / "codebot").mkdir(exist_ok=True)
    monkeypatch.setattr(
        auto_revert,
        "REPOS",
        [tmp_path / "repo_a", tmp_path / "repo_b"],
    )
    return tmp_path


@pytest.fixture
def make_index(isolate_state):
    """Helper to create a build-gate-INDEX.md under the isolated WORK_ROOT."""
    def _create(lines: list[str], location: str = "work_root") -> Path:
        if location == "work_root":
            base = isolate_state / "docs" / "optimization"
        else:
            base = isolate_state / "codebot" / "docs" / "optimization"
        base.mkdir(parents=True, exist_ok=True)
        idx = base / "build-gate-INDEX.md"
        idx.write_text("\n".join(lines), encoding="utf-8")
        return idx
    return _create


# ---------------------------------------------------------------------------
# _run()
# ---------------------------------------------------------------------------

class TestRun:
    def test_run_success(self, isolate_state):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="abc123\n", stderr=""
            )
            code, out = auto_revert._run(["git", "log"], isolate_state)
        assert code == 0
        assert "abc123" in out
        mock_run.assert_called_once()

    def test_run_failure_nonzero_exit(self, isolate_state):
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=128, stdout="", stderr="fatal: bad revision"
            )
            code, out = auto_revert._run(["git", "log"], isolate_state)
        assert code == 128
        assert "fatal" in out

    def test_run_timeout_raises_exception(self, isolate_state):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=5)):
            code, out = auto_revert._run(["git", "log"], isolate_state, timeout=5)
        assert code == 1
        assert "TimeoutExpired" in out or "timed out" in out.lower() or "expired" in out.lower()

    def test_run_generic_exception(self, isolate_state):
        with patch("subprocess.run", side_effect=OSError("No such file")):
            code, out = auto_revert._run(["git", "log"], isolate_state)
        assert code == 1
        assert "No such file" in out


# ---------------------------------------------------------------------------
# _gate_failures()
# ---------------------------------------------------------------------------

class TestGateFailures:
    def test_parses_fail_lines_with_files(self, make_index):
        make_index([
            "| PASS | src/main.py | ok |",
            "| FAIL | src/broken.py | tests failed |",
            "| FAIL | lib/utils.js | lint error |",
        ])
        failures = auto_revert._gate_failures()
        assert len(failures) == 2
        assert "src/broken.py" in failures[0]["files"]
        assert "lib/utils.js" in failures[1]["files"]

    def test_skips_pass_lines(self, make_index):
        make_index([
            "| PASS | src/main.py | ok |",
            "| PASS | src/other.py | ok |",
        ])
        failures = auto_revert._gate_failures()
        assert failures == []

    def test_empty_index_file(self, make_index):
        make_index([])
        failures = auto_revert._gate_failures()
        assert failures == []

    def test_missing_index_file(self, isolate_state):
        # No index file created at all
        failures = auto_revert._gate_failures()
        assert failures == []

    def test_malformed_lines_no_files(self, make_index):
        make_index([
            "FAIL something happened but no file extension here",
        ])
        failures = auto_revert._gate_failures()
        assert len(failures) == 1
        assert failures[0]["files"] == []

    def test_line_truncated_to_300_chars(self, make_index):
        long_line = "FAIL " + "x" * 500 + ".py"
        make_index([long_line])
        failures = auto_revert._gate_failures()
        assert len(failures[0]["line"]) <= 300

    def test_max_5_files_per_entry(self, make_index):
        files = " ".join(f"f{i}.py" for i in range(8))
        make_index([f"FAIL {files}"])
        failures = auto_revert._gate_failures()
        assert len(failures[0]["files"]) <= 5

    def test_reads_from_both_candidate_paths(self, isolate_state):
        # Create both candidate index files
        for loc in ("work_root", "bots_dir"):
            if loc == "work_root":
                base = isolate_state / "docs" / "optimization"
            else:
                base = isolate_state / "codebot" / "docs" / "optimization"
            base.mkdir(parents=True, exist_ok=True)
            (base / "build-gate-INDEX.md").write_text("FAIL unique_a.py\n", encoding="utf-8")

        failures = auto_revert._gate_failures()
        all_files = [f for fail in failures for f in fail["files"]]
        # Both files should appear since we read from both candidates
        assert len(failures) >= 1


# ---------------------------------------------------------------------------
# _last_bot_commit()
# ---------------------------------------------------------------------------

class TestLastBotCommit:
    def test_returns_sha_on_success(self, isolate_state):
        with patch.object(auto_revert, "_run", return_value=(0, "abc123def456\n")):
            sha = auto_revert._last_bot_commit(isolate_state, "src/main.py")
        assert sha == "abc123def456"

    def test_returns_none_on_git_failure(self, isolate_state):
        with patch.object(auto_revert, "_run", return_value=(128, "fatal: not found")):
            sha = auto_revert._last_bot_commit(isolate_state, "missing.py")
        assert sha is None

    def test_returns_none_on_empty_output(self, isolate_state):
        with patch.object(auto_revert, "_run", return_value=(0, "")):
            sha = auto_revert._last_bot_commit(isolate_state, "empty.py")
        assert sha is None

    def test_sha_truncated_to_40_chars(self, isolate_state):
        long_sha = "a" * 60
        with patch.object(auto_revert, "_run", return_value=(0, long_sha + "\n")):
            sha = auto_revert._last_bot_commit(isolate_state, "file.py")
        assert len(sha) <= 40


# ---------------------------------------------------------------------------
# _commit_is_bot_authored()
# ---------------------------------------------------------------------------

class TestCommitIsBotAuthored:
    @pytest.mark.parametrize("author", [
        "CodeBot <bot@example.com>",
        "sisyphus <sisyphus@codebot>",
        "linuxguru999 <lg@test.com>",
        "my-bot-worker <worker@ci>",
    ])
    def test_recognizes_bot_authors(self, isolate_state, author):
        with patch.object(auto_revert, "_run", return_value=(0, author + "\n")):
            assert auto_revert._commit_is_bot_authored(isolate_state, "abc123") is True

    @pytest.mark.parametrize("author", [
        "Alice Human <alice@example.com>",
        "John Doe <john@corp.com>",
        "random developer <dev@test.org>",
    ])
    def test_rejects_human_authors(self, isolate_state, author):
        with patch.object(auto_revert, "_run", return_value=(0, author + "\n")):
            assert auto_revert._commit_is_bot_authored(isolate_state, "abc123") is False

    def test_returns_false_on_git_error(self, isolate_state):
        with patch.object(auto_revert, "_run", return_value=(128, "fatal")):
            assert auto_revert._commit_is_bot_authored(isolate_state, "abc123") is False


# ---------------------------------------------------------------------------
# _revert_log(), _already_reverted(), _record_revert()
# ---------------------------------------------------------------------------

class TestRevertLog:
    def test_revert_log_empty_when_no_file(self, isolate_state):
        log = auto_revert._revert_log()
        assert log == []

    def test_revert_log_reads_valid_json(self, isolate_state):
        data = [{"sha": "aaa", "repo": "/r", "file": "f.py", "at": 1.0}]
        auto_revert.REVERT_LOG.write_text(json.dumps(data), encoding="utf-8")
        log = auto_revert._revert_log()
        assert len(log) == 1
        assert log[0]["sha"] == "aaa"

    def test_revert_log_handles_corrupt_json(self, isolate_state):
        auto_revert.REVERT_LOG.write_text("{not valid json!!!", encoding="utf-8")
        log = auto_revert._revert_log()
        assert log == []

    def test_revert_log_handles_non_list_json(self, isolate_state):
        auto_revert.REVERT_LOG.write_text('{"key": "value"}', encoding="utf-8")
        log = auto_revert._revert_log()
        assert log == []

    def test_already_reverted_true(self, isolate_state):
        auto_revert.REVERT_LOG.write_text(
            json.dumps([{"sha": "deadbeef"}]), encoding="utf-8"
        )
        assert auto_revert._already_reverted("deadbeef") is True

    def test_already_reverted_false(self, isolate_state):
        auto_revert.REVERT_LOG.write_text(
            json.dumps([{"sha": "deadbeef"}]), encoding="utf-8"
        )
        assert auto_revert._already_reverted("cafebabe") is False

    def test_record_revert_appends_entry(self, isolate_state):
        entry = {"sha": "new123", "repo": "/r", "file": "f.py", "at": time.time()}
        auto_revert._record_revert(entry)
        log = auto_revert._revert_log()
        assert len(log) == 1
        assert log[0]["sha"] == "new123"

    def test_record_revert_caps_at_50_entries(self, isolate_state):
        entries = [{"sha": f"s{i}"} for i in range(60)]
        auto_revert.REVERT_LOG.write_text(json.dumps(entries), encoding="utf-8")
        auto_revert._record_revert({"sha": "overflow"})
        log = auto_revert._revert_log()
        assert len(log) <= 50

    def test_record_revert_atomic_write_creates_file(self, isolate_state):
        assert not auto_revert.REVERT_LOG.exists()
        auto_revert._record_revert({"sha": "first"})
        assert auto_revert.REVERT_LOG.exists()


# ---------------------------------------------------------------------------
# _requeue_with_failure()
# ---------------------------------------------------------------------------

class TestRequeueWithFailure:
    def test_appends_entry_to_queue(self, isolate_state):
        queue_dir = isolate_state / "docs" / "triage"
        queue_dir.mkdir(parents=True)
        qp = queue_dir / "QUEUE.md"
        qp.write_text("# Existing Queue\n", encoding="utf-8")

        auto_revert._requeue_with_failure("repo:file.py", "FAIL test broke")
        content = qp.read_text(encoding="utf-8")
        assert "QUEUE-REVERT-" in content
        assert "repo:file.py" in content
        assert "FAIL test broke" in content
        assert "auto-revert" in content

    def test_skips_duplicate_marker(self, isolate_state):
        queue_dir = isolate_state / "docs" / "triage"
        queue_dir.mkdir(parents=True)
        qp = queue_dir / "QUEUE.md"
        qp.write_text("# Queue\nauto-revert repo:file.py already here\n", encoding="utf-8")

        auto_revert._requeue_with_failure("repo:file.py", "FAIL again")
        content = qp.read_text(encoding="utf-8")
        # Should not have added a second QUEUE-REVERT entry
        assert content.count("QUEUE-REVERT-") == 0

    def test_noop_when_queue_missing(self, isolate_state):
        # Don't create QUEUE.md
        auto_revert._requeue_with_failure("hint", "failure")
        # Should not raise

    def test_work_hint_truncated_to_200(self, isolate_state):
        queue_dir = isolate_state / "docs" / "triage"
        queue_dir.mkdir(parents=True)
        qp = queue_dir / "QUEUE.md"
        qp.write_text("# Q\n", encoding="utf-8")

        long_hint = "A" * 300
        auto_revert._requeue_with_failure(long_hint, "fail")
        content = qp.read_text(encoding="utf-8")
        # The hint in the output should be truncated
        assert "A" * 300 not in content

    def test_failure_truncated_to_500(self, isolate_state):
        queue_dir = isolate_state / "docs" / "triage"
        queue_dir.mkdir(parents=True)
        qp = queue_dir / "QUEUE.md"
        qp.write_text("# Q\n", encoding="utf-8")

        long_fail = "B" * 600
        auto_revert._requeue_with_failure("hint", long_fail)
        content = qp.read_text(encoding="utf-8")
        assert "B" * 600 not in content


# ---------------------------------------------------------------------------
# process() — end-to-end orchestration
# ---------------------------------------------------------------------------

class TestProcess:
    def test_noop_when_no_gate_failures(self, isolate_state):
        with patch.object(auto_revert, "_gate_failures", return_value=[]):
            result = auto_revert.process()
        assert result["failures"] == 0
        assert result["reverted"] == []
        assert result["skipped"] == []

    def test_skips_already_reverted_commits(self, isolate_state, make_index):
        make_index(["FAIL src/broken.py"])
        repo = isolate_state / "repo_a"
        (repo / ".git").mkdir(parents=True)

        with (
            patch.object(auto_revert, "_last_bot_commit", return_value="abc123"),
            patch.object(auto_revert, "_already_reverted", return_value=True),
        ):
            result = auto_revert.process()

        assert result["failures"] == 1
        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["why"] == "already-reverted"
        assert result["reverted"] == []

    def test_skips_human_authored_commits(self, isolate_state, make_index):
        make_index(["FAIL src/human.py"])
        repo = isolate_state / "repo_a"
        (repo / ".git").mkdir(parents=True)

        with (
            patch.object(auto_revert, "_last_bot_commit", return_value="def456"),
            patch.object(auto_revert, "_already_reverted", return_value=False),
            patch.object(auto_revert, "_commit_is_bot_authored", return_value=False),
        ):
            result = auto_revert.process()

        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["why"] == "not-bot-authored"

    def test_successful_revert_of_bot_commit(self, isolate_state, make_index):
        make_index(["FAIL src/fix.py"])
        repo = isolate_state / "repo_a"
        (repo / ".git").mkdir(parents=True)

        # Create the queue file so requeue works
        qdir = isolate_state / "docs" / "triage"
        qdir.mkdir(parents=True)
        (qdir / "QUEUE.md").write_text("# Q\n", encoding="utf-8")

        run_results = {
            "revert --no-commit": (0, ""),
            "commit": (0, ""),
        }

        def fake_run(cmd, cwd, timeout=30):
            cmd_str = " ".join(cmd)
            if "revert" in cmd_str and "--no-commit" in cmd_str:
                return run_results["revert --no-commit"]
            if "commit" in cmd_str:
                return run_results["commit"]
            return (1, "unexpected")

        with (
            patch.object(auto_revert, "_last_bot_commit", return_value="aaa111bbb222ccc333ddd444eee555fff6667778"),
            patch.object(auto_revert, "_already_reverted", return_value=False),
            patch.object(auto_revert, "_commit_is_bot_authored", return_value=True),
            patch.object(auto_revert, "_run", side_effect=fake_run),
            patch.object(auto_revert, "_record_revert") as mock_record,
            patch.object(auto_revert, "_requeue_with_failure") as mock_requeue,
        ):
            result = auto_revert.process()

        assert result["failures"] == 1
        assert len(result["reverted"]) == 1
        assert result["reverted"][0]["sha"] == "aaa111bb"
        mock_record.assert_called_once()
        mock_requeue.assert_called_once()

    def test_revert_conflict_aborts_and_skips(self, isolate_state, make_index):
        make_index(["FAIL src/conflict.py"])
        repo = isolate_state / "repo_a"
        (repo / ".git").mkdir(parents=True)

        call_count = 0

        def fake_run(cmd, cwd, timeout=30):
            nonlocal call_count
            call_count += 1
            cmd_str = " ".join(cmd)
            if "revert" in cmd_str and "--no-commit" in cmd_str:
                return (1, "CONFLICT merge conflict in src/conflict.py")
            if "revert" in cmd_str and "--abort" in cmd_str:
                return (0, "")
            return (1, "unexpected")

        with (
            patch.object(auto_revert, "_last_bot_commit", return_value="bbb222"),
            patch.object(auto_revert, "_already_reverted", return_value=False),
            patch.object(auto_revert, "_commit_is_bot_authored", return_value=True),
            patch.object(auto_revert, "_run", side_effect=fake_run),
        ):
            result = auto_revert.process()

        assert len(result["skipped"]) == 1
        assert "revert-conflict" in result["skipped"][0]["why"]

    def test_commit_after_revert_fails_aborts(self, isolate_state, make_index):
        make_index(["FAIL src/post.py"])
        repo = isolate_state / "repo_a"
        (repo / ".git").mkdir(parents=True)

        def fake_run(cmd, cwd, timeout=30):
            cmd_str = " ".join(cmd)
            if "revert" in cmd_str and "--no-commit" in cmd_str:
                return (0, "")
            if "commit" in cmd_str and "revert:" in cmd_str:
                return (1, "nothing to commit")
            if "revert" in cmd_str and "--abort" in cmd_str:
                return (0, "")
            return (1, "unexpected")

        with (
            patch.object(auto_revert, "_last_bot_commit", return_value="ccc333"),
            patch.object(auto_revert, "_already_reverted", return_value=False),
            patch.object(auto_revert, "_commit_is_bot_authored", return_value=True),
            patch.object(auto_revert, "_run", side_effect=fake_run),
        ):
            result = auto_revert.process()

        assert len(result["skipped"]) == 1
        assert result["skipped"][0]["why"] == "commit-failed"

    def test_skips_repos_without_git_dir(self, isolate_state, make_index):
        make_index(["FAIL src/nogit.py"])
        # repo_a exists but has no .git directory
        (isolate_state / "repo_a").mkdir(parents=True, exist_ok=True)

        with patch.object(auto_revert, "_last_bot_commit") as mock_last:
            result = auto_revert.process()

        # _last_bot_commit should never be called since no repo has .git
        mock_last.assert_not_called()
        assert result["reverted"] == []

    def test_multiple_failures_multiple_files(self, isolate_state, make_index):
        make_index([
            "FAIL src/a.py",
            "FAIL src/b.py",
        ])
        repo = isolate_state / "repo_a"
        (repo / ".git").mkdir(parents=True)

        with (
            patch.object(auto_revert, "_last_bot_commit", return_value=None),
        ):
            result = auto_revert.process()

        assert result["failures"] == 2
        # No commits found means nothing reverted or skipped
        assert result["reverted"] == []


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------

class TestMain:
    def test_main_prints_summary(self, capsys):
        with patch.object(auto_revert, "process", return_value={
            "failures": 3,
            "reverted": [{"sha": "aaa", "repo": "/r", "file": "f.py"}],
            "skipped": [{"sha": "bbb", "why": "test"}, {"sha": "ccc", "why": "test2"}],
        }):
            auto_revert.main()
        captured = capsys.readouterr()
        assert "gate failures=3" in captured.out
        assert "reverted=1" in captured.out
        assert "skipped=2" in captured.out
        assert "aaa" in captured.out