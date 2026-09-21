"""Tests for commit-on-COMPLETE: per-ticket scoped commits with SHA traceability."""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.completion_commit import commit_ticket_files, build_commit_message


def _git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, timeout=15)
    assert r.returncode == 0, f"git {' '.join(args)} failed: {r.stderr[:200]}"
    return r.stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t.t")
    _git(repo, "config", "user.name", "t")
    (repo / "base.txt").write_text("base\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


class TestBuildCommitMessage:
    def test_message_contains_ticket_id(self):
        assert "[CB-1]" in build_commit_message("CB-1", "Fix thing")

    def test_empty_ticket_id_still_formats(self):
        assert isinstance(build_commit_message("", "x"), str)


class TestCommitTicketFiles:
    def test_commits_only_listed_files(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "a.py").write_text("a\n")
        (repo / "b.py").write_text("b\n")
        ok, sha = commit_ticket_files(repo, "CB-1", "Ticket one", ["a.py"])
        assert ok is True and len(sha) == 40
        names = _git(repo, "show", "--name-only", "--pretty=format:", sha).split()
        assert names == ["a.py"]
        assert "[CB-1]" in _git(repo, "log", "-1", "--pretty=%B")

    def test_records_sha_traceability(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "a.py").write_text("a\n")
        ok, sha = commit_ticket_files(repo, "CB-2", "Ticket two", ["a.py"])
        assert ok is True
        assert sha == _git(repo, "rev-parse", "HEAD")

    def test_skips_missing_and_out_of_tree(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "a.py").write_text("a\n")
        ok, sha = commit_ticket_files(repo, "CB-3", "T", ["a.py", "nope.py", "/etc/hostname"])
        assert ok is True and len(sha) == 40
        names = _git(repo, "show", "--name-only", "--pretty=format:", sha).split()
        assert names == ["a.py"]

    def test_no_files_is_failure(self, tmp_path):
        repo = _make_repo(tmp_path)
        ok, sha = commit_ticket_files(repo, "CB-4", "T", [])
        assert ok is False and sha == ""

    def test_nothing_dirty_returns_head_sha(self, tmp_path):
        repo = _make_repo(tmp_path)
        head = _git(repo, "rev-parse", "HEAD")
        ok, sha = commit_ticket_files(repo, "CB-5", "T", ["base.txt"])
        assert ok is True and sha == head

    def test_not_a_repo_is_fail_open(self, tmp_path):
        plain = tmp_path / "plain"
        plain.mkdir()
        (plain / "a.py").write_text("a\n")
        ok, sha = commit_ticket_files(plain, "CB-6", "T", ["a.py"])
        assert ok is False and sha == ""


class TestPushGating:
    def test_push_disabled_by_default(self, tmp_path, monkeypatch):
        from codebot.completion_commit import push_current_branch
        monkeypatch.delenv("GITHUB_DRY_RUN", raising=False)
        ok, out = push_current_branch(tmp_path)
        assert ok is False and "DRY_RUN" in out

    def test_push_disabled_when_dry_run_on(self, tmp_path, monkeypatch):
        from codebot.completion_commit import push_current_branch
        monkeypatch.setenv("GITHUB_DRY_RUN", "1")
        ok, out = push_current_branch(tmp_path)
        assert ok is False

    def test_push_attempts_when_dry_run_off(self, tmp_path, monkeypatch):
        import subprocess as _sp
        from codebot.completion_commit import push_current_branch
        monkeypatch.setenv("GITHUB_DRY_RUN", "0")
        calls = []
        real_run = _sp.run
        def fake_run(*a, **k):
            calls.append(a[0])
            class R: returncode = 0; stdout = "ok"; stderr = ""
            return R()
        monkeypatch.setattr(_sp, "run", fake_run)
        ok, _ = push_current_branch(tmp_path, env={})
        assert ok is True
        assert calls and calls[0][:4] == ["git", "-C", str(tmp_path), "push"]
        assert "--force" not in " ".join(calls[0])

    def test_sync_issue_needs_gh_auth(self, tmp_path, monkeypatch):
        import subprocess as _sp
        from codebot.completion_commit import sync_ticket_issue
        class R: returncode = 1; stdout = ""; stderr = "auth?"
        monkeypatch.setattr(_sp, "run", lambda *a, **k: R())
        ok, reason = sync_ticket_issue("CB-1", "t", "abc")
        assert ok is False and "auth" in reason.lower()


class TestSyncRolesRegistered:
    def test_git_sync_registered(self):
        from codebot.role_registry import get_role, RoleCategory
        r = get_role("git_sync")
        assert r is not None and r.category == RoleCategory.CONTROL

    def test_github_mirror_registered(self):
        from codebot.role_registry import get_role, RoleCategory
        r = get_role("github_mirror")
        assert r is not None and r.category == RoleCategory.CONTROL

    def test_role_count_bumped(self):
        from codebot.role_registry import ALL_ROLES
        assert len(ALL_ROLES) == 31


class TestTicketBranchFlow:
    def test_branch_name_sanitized(self):
        from codebot.completion_commit import _branch_name
        assert _branch_name("CB-1") == "cb/CB-1"
        assert _branch_name("a/b c") == "cb/a-b-c"

    def test_commit_on_branch_lands_there(self, tmp_path):
        repo = _make_repo(tmp_path)
        (repo / "a.py").write_text("a\n")
        ok, sha = commit_ticket_files(repo, "CB-B1", "branch work", ["a.py"], branch=True)
        assert ok is True and len(sha) == 40
        assert _git(repo, "rev-parse", "--abbrev-ref", "HEAD") == "cb/CB-B1"
        names = _git(repo, "show", "--name-only", "--pretty=format:", sha).split()
        assert names == ["a.py"]

    def test_open_pr_idempotent_on_existing(self, tmp_path, monkeypatch):
        import subprocess as _sp
        from codebot.completion_commit import open_pull_request
        monkeypatch.setattr(_sp, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("no subprocess")))
        import codebot.completion_commit as cc
        monkeypatch.setattr(cc, "_run_git", lambda *a, **k: (True, ""))
        seq = iter([
            (True, "https://github.com/o/r/pull/7\n"),
        ])
        real_pr_list = None
        def fake_run(cmd, **kw):
            assert cmd[:3] == ["gh", "pr", "list"]
            class R: returncode = 0; stdout = "https://github.com/o/r/pull/7\n"; stderr = ""
            return R()
        monkeypatch.setattr(_sp, "run", fake_run)
        ok, url = open_pull_request(tmp_path, "CB-B2", "t", "abc123")
        assert ok is True and url == "https://github.com/o/r/pull/7"

    def test_auto_merge_shape(self, tmp_path, monkeypatch):
        import subprocess as _sp
        from codebot.completion_commit import auto_merge_pull_request
        calls = []
        def fake_run(cmd, **kw):
            calls.append(cmd)
            class R: returncode = 0; stdout = "merged"; stderr = ""
            return R()
        monkeypatch.setattr(_sp, "run", fake_run)
        ok, _ = auto_merge_pull_request("https://github.com/o/r/pull/7")
        assert ok is True
        assert calls[0][:4] == ["gh", "pr", "merge", "https://github.com/o/r/pull/7"]
        assert "--auto" in calls[0] and "--squash" in calls[0]
        assert "--force" not in " ".join(calls[0])

    def test_record_commit_stores_pr_url(self, tmp_path):
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel
        store = TicketStore(tmp_path / "t.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e9", "p", "d", ["a"], risk=RiskLevel.LOW)
        store.add(t)
        updated = store.record_commit(t.id, "abc123", "https://github.com/o/r/pull/7")
        assert updated is not None and updated.pr_url == "https://github.com/o/r/pull/7"
        assert store.get(t.id).pr_url == "https://github.com/o/r/pull/7"
        store.close()

    def test_record_commit(self, tmp_path):
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel
        store = TicketStore(tmp_path / "t.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e1", "p", "d", ["a"], risk=RiskLevel.LOW)
        store.add(t)
        updated = store.record_commit(t.id, "abc123")
        assert updated is not None and updated.commit_sha == "abc123"
        assert store.get(t.id).commit_sha == "abc123"
        store.close()

    def test_record_commit_idempotent(self, tmp_path):
        from codebot.ticket_engine import TicketStore, create_ticket, TicketClass, Severity, RiskLevel
        store = TicketStore(tmp_path / "t.json")
        t = create_ticket("t", TicketClass.BUG, Severity.LOW, "s", "e2", "p", "d", ["a"], risk=RiskLevel.LOW)
        store.add(t)
        store.record_commit(t.id, "abc123")
        again = store.record_commit(t.id, "abc123")
        assert again is not None and again.commit_sha == "abc123"
        assert store.get(t.id).commit_sha == "abc123"
        store.close()

    def test_record_commit_missing_ticket(self, tmp_path):
        from codebot.ticket_engine import TicketStore
        store = TicketStore(tmp_path / "t.json")
        assert store.record_commit("CB-NOPE", "abc") is None
        store.close()


class TestGatekeeperCommitsOnComplete:
    def test_verify_ticket_commits_and_records_sha(self, tmp_path):
        from unittest.mock import patch
        from codebot.gatekeeper import Gatekeeper
        from codebot.ticket_engine import TicketStore as TSStore, create_ticket, TicketClass, Severity, RiskLevel, TicketState

        state_dir = tmp_path / "state"
        state_dir.mkdir()
        ws = tmp_path / "ws"
        ws.mkdir()
        subprocess.run(["git", "-C", str(ws), "init", "-q"], check=True, timeout=15)
        subprocess.run(["git", "-C", str(ws), "config", "user.email", "t@t.t"], check=True, timeout=15)
        subprocess.run(["git", "-C", str(ws), "config", "user.name", "t"], check=True, timeout=15)
        (ws / "feat.py").write_text("x = 1\n")
        subprocess.run(["git", "-C", str(ws), "add", "-A"], check=True, timeout=15)
        subprocess.run(["git", "-C", str(ws), "commit", "-qm", "init"], check=True, timeout=15)
        (ws / "feat.py").write_text("x = 2\n")

        store = TSStore(state_dir / "tickets.json")
        t = create_ticket("feat", TicketClass.BUG, Severity.LOW, "s", "e3", "p", "d", ["a"],
                          risk=RiskLevel.LOW, affected_modules=["feat.py"])
        store.add(t)
        for st in (TicketState.VALIDATING, TicketState.TRIAGED, TicketState.READY,
                   TicketState.PLANNING, TicketState.IMPLEMENTING,
                   TicketState.REVIEWING, TicketState.VERIFYING):
            store.transition(t.id, st)
        store.record_gate_result(t.id, True)
        store.flush()
        store.close()

        passing = [__import__("codebot.quality_gate", fromlist=["GateEvaluation"]).GateEvaluation(
            "build", __import__("codebot.quality_gate", fromlist=["GateResult"]).GateResult.PASS,
            "echo", "ok", 1.0, True)]
        shared = TSStore(state_dir / "tickets.json")
        with patch("codebot.quality_gate.run_quality_gates_with_cache", return_value=(True, passing)):
            with patch("codebot.ticket_dispatcher.get_ticket_store", return_value=shared):
                gk = Gatekeeper(state_dir=state_dir, workspace=ws)
                with patch.object(gk, "_build_completion_evidence") as mock_ev:
                    from codebot.review_types import CompletionEvidence
                    mock_ev.return_value = CompletionEvidence(
                        requirement_verified=True, acceptance_passed=1, acceptance_failed=0,
                        acceptance_unknown=0, tests_executed=1, tests_passed=1, tests_failed=0,
                        new_tests_added=0, finding_counts={}, build_verified=True,
                        security_checked=True, regression_checked=True,
                        deterministic_gates_passed=True, checklist_complete=True,
                        completion_confidence=1.0,
                    )
                    result = gk.verify_ticket(t.id, "bug", ["feat.py"])
        assert result["decision"] == "COMPLETE"
        got = shared.get(t.id)
        assert got is not None and got.state == TicketState.COMPLETE
        assert got.commit_sha and len(got.commit_sha) == 40
        log = subprocess.run(["git", "-C", str(ws), "log", "-1", "--pretty=%B"],
                             capture_output=True, text=True, timeout=15).stdout
        assert f"[{t.id}]" in log
        shared.close()
