"""Tests for codebot/stale_branch_detector.py — branch age + stale threshold logic."""
import time
from unittest.mock import patch, MagicMock

import pytest

from codebot.stale_branch_detector import BranchRecord, CleanupAction, StaleBranchDetector

NOW = 1_000_000.0


@pytest.fixture
def detector():
    return StaleBranchDetector(stale_threshold_seconds=7 * 86400, warning_period_seconds=86400)


@pytest.fixture
def fresh_branch():
    return BranchRecord(branch_name="feat/fresh", ticket_id="CB-1", last_activity_ts=NOW, last_commit_sha="abc123")

@pytest.fixture
def stale_branch():
    return BranchRecord(branch_name="feat/stale", ticket_id="CB-2", last_activity_ts=NOW - 8 * 86400, last_commit_sha="def456")

@pytest.fixture
def abandoned_branch():
    return BranchRecord(branch_name="feat/abandoned", ticket_id="CB-99", last_activity_ts=NOW, last_commit_sha="xyz789")


class TestBranchRecord:
    def test_frozen(self):
        r = BranchRecord(branch_name="a", ticket_id="CB-1", last_activity_ts=NOW, last_commit_sha="abc")
        with pytest.raises((AttributeError, TypeError)):
            r.branch_name = "b"  # type: ignore

    def test_metadata_default(self):
        r = BranchRecord(branch_name="a", ticket_id="CB-1", last_activity_ts=NOW, last_commit_sha="abc")
        assert r.metadata == {}

    def test_metadata_custom(self):
        r = BranchRecord(branch_name="a", ticket_id="CB-1", last_activity_ts=NOW, last_commit_sha="abc", metadata={"key": "val"})
        assert r.metadata["key"] == "val"


class TestCleanupAction:
    def test_defaults(self):
        a = CleanupAction(action="none", branch_name="feat/x")
        assert a.scheduled_at == pytest.approx(0.0)
        assert a.reason == ""

    def test_frozen(self):
        a = CleanupAction(action="warn", branch_name="feat/x")
        with pytest.raises((AttributeError, TypeError)):
            a.action = "delete"  # type: ignore


class TestRegisterAndGet:
    def test_register_and_get(self, detector, fresh_branch):
        detector.register(fresh_branch)
        assert detector.get_branch("feat/fresh") == fresh_branch

    def test_get_missing_returns_none(self, detector):
        assert detector.get_branch("nonexistent") is None

    def test_register_overwrites(self, detector, fresh_branch):
        detector.register(fresh_branch)
        updated = BranchRecord(branch_name="feat/fresh", ticket_id="CB-1", last_activity_ts=NOW + 100, last_commit_sha="new")
        detector.register(updated)
        assert detector.get_branch("feat/fresh").last_commit_sha == "new"

    def test_list_all_branches(self, detector, fresh_branch, stale_branch):
        detector.register(fresh_branch)
        detector.register(stale_branch)
        all_b = detector.list_all_branches()
        assert len(all_b) == 2
        assert {b.branch_name for b in all_b} == {"feat/fresh", "feat/stale"}

    def test_list_empty(self, detector):
        assert detector.list_all_branches() == []


class TestDetectStale:
    def test_no_stale_when_fresh(self, detector, fresh_branch):
        detector.register(fresh_branch)
        assert detector.detect_stale(now=NOW) == []

    def test_stale_by_age(self, detector, stale_branch):
        detector.register(stale_branch)
        stale = detector.detect_stale(now=NOW)
        assert len(stale) == 1
        assert stale[0].branch_name == "feat/stale"

    def test_exactly_at_threshold_not_stale(self, detector):
        r = BranchRecord(branch_name="feat/edge", ticket_id="CB-1", last_activity_ts=NOW - 7 * 86400, last_commit_sha="abc")
        detector.register(r)
        assert detector.detect_stale(now=NOW) == []

    def test_one_second_past_threshold_stale(self, detector):
        r = BranchRecord(branch_name="feat/edge", ticket_id="CB-1", last_activity_ts=NOW - 7 * 86400 - 1, last_commit_sha="abc")
        detector.register(r)
        assert len(detector.detect_stale(now=NOW)) == 1

    def test_stale_by_abandoned_ticket(self, detector, abandoned_branch):
        detector.register(abandoned_branch)
        stale = detector.detect_stale(now=NOW, abandoned_tickets={"CB-99"})
        assert len(stale) == 1
        assert stale[0].ticket_id == "CB-99"

    def test_fresh_with_abandoned_still_stale(self, detector, fresh_branch):
        fresh_branch2 = BranchRecord(branch_name="feat/fresh2", ticket_id="CB-99", last_activity_ts=NOW, last_commit_sha="abc")
        detector.register(fresh_branch2)
        stale = detector.detect_stale(now=NOW, abandoned_tickets={"CB-99"})
        assert len(stale) == 1

    def test_not_abandoned_not_stale(self, detector, fresh_branch):
        detector.register(fresh_branch)
        stale = detector.detect_stale(now=NOW, abandoned_tickets={"OTHER"})
        assert stale == []

    def test_multiple_mixed(self, detector, fresh_branch, stale_branch, abandoned_branch):
        detector.register(fresh_branch)
        detector.register(stale_branch)
        detector.register(abandoned_branch)
        stale = detector.detect_stale(now=NOW, abandoned_tickets={"CB-99"})
        assert len(stale) == 2
        assert {b.branch_name for b in stale} == {"feat/stale", "feat/abandoned"}

    def test_empty_abandoned_set(self, detector, stale_branch):
        detector.register(stale_branch)
        assert len(detector.detect_stale(now=NOW, abandoned_tickets=set())) == 1

    def test_none_abandoned_defaults_empty(self, detector, stale_branch):
        detector.register(stale_branch)
        assert len(detector.detect_stale(now=NOW, abandoned_tickets=None)) == 1

    def test_no_branches(self, detector):
        assert detector.detect_stale(now=NOW) == []

    def test_custom_threshold(self):
        d = StaleBranchDetector(stale_threshold_seconds=100, warning_period_seconds=10)
        r = BranchRecord(branch_name="feat/a", ticket_id="CB-1", last_activity_ts=NOW - 101, last_commit_sha="abc")
        d.register(r)
        assert len(d.detect_stale(now=NOW)) == 1
        r2 = BranchRecord(branch_name="feat/b", ticket_id="CB-2", last_activity_ts=NOW - 99, last_commit_sha="abc")
        d.register(r2)
        stale = d.detect_stale(now=NOW)
        assert len(stale) == 1
        assert stale[0].branch_name == "feat/a"


class TestScheduleCleanup:
    def test_not_found_returns_none(self, detector):
        result = detector.schedule_cleanup("nonexistent", now=NOW)
        assert result.action == "none"
        assert "not found" in result.reason

    def test_not_stale_returns_none(self, detector, fresh_branch):
        detector.register(fresh_branch)
        result = detector.schedule_cleanup("feat/fresh", now=NOW)
        assert result.action == "none"
        assert "not stale" in result.reason

    def test_first_call_issues_warning(self, detector, stale_branch):
        detector.register(stale_branch)
        result = detector.schedule_cleanup("feat/stale", now=NOW)
        assert result.action == "warn"
        assert result.reason == "initial warning"
        assert result.scheduled_at == pytest.approx(NOW)

    def test_second_call_within_warning_period_stays_warn(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        result = detector.schedule_cleanup("feat/stale", now=NOW + 3600)
        assert result.action == "warn"
        assert result.reason == "warning period active"

    def test_after_warning_period_returns_delete(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        result = detector.schedule_cleanup("feat/stale", now=NOW + 86401)
        assert result.action == "delete"
        assert result.reason == "warning period expired"

    def test_exactly_at_warning_boundary_delete(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        result = detector.schedule_cleanup("feat/stale", now=NOW + 86400)
        assert result.action == "delete"

    def test_warning_timestamp_preserved(self, detector, stale_branch):
        detector.register(stale_branch)
        first = detector.schedule_cleanup("feat/stale", now=NOW)
        second = detector.schedule_cleanup("feat/stale", now=NOW + 100)
        assert second.scheduled_at == pytest.approx(first.scheduled_at)

    def test_custom_warning_period(self):
        d = StaleBranchDetector(stale_threshold_seconds=100, warning_period_seconds=10)
        r = BranchRecord(branch_name="feat/a", ticket_id="CB-1", last_activity_ts=NOW - 200, last_commit_sha="abc")
        d.register(r)
        d.schedule_cleanup("feat/a", now=NOW)
        assert d.schedule_cleanup("feat/a", now=NOW + 5).action == "warn"
        assert d.schedule_cleanup("feat/a", now=NOW + 10).action == "delete"

    def test_different_branches_independent(self, detector):
        r1 = BranchRecord(branch_name="feat/a", ticket_id="CB-1", last_activity_ts=NOW - 8 * 86400, last_commit_sha="aaa")
        r2 = BranchRecord(branch_name="feat/b", ticket_id="CB-2", last_activity_ts=NOW - 8 * 86400, last_commit_sha="bbb")
        detector.register(r1)
        detector.register(r2)
        detector.schedule_cleanup("feat/a", now=NOW)
        result_b = detector.schedule_cleanup("feat/b", now=NOW)
        assert result_b.action == "warn"
        assert result_b.reason == "initial warning"


class TestExecuteCleanup:
    def test_not_found(self, detector):
        result = detector.execute_cleanup("nonexistent", now=NOW)
        assert result["status"] == "not_found"

    def test_warning_active_blocks_deletion(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        result = detector.execute_cleanup("feat/stale", now=NOW + 100)
        assert result["status"] == "warning_active"
        assert "time_remaining" in result
        assert detector.get_branch("feat/stale") is not None

    def test_after_warning_deletes(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        result = detector.execute_cleanup("feat/stale", now=NOW + 86401)
        assert result["status"] == "deleted"
        assert result["branch_name"] == "feat/stale"
        assert result["ticket_id"] == "CB-2"
        assert detector.get_branch("feat/stale") is None

    def test_no_warning_deletes_immediately(self, detector, stale_branch):
        detector.register(stale_branch)
        result = detector.execute_cleanup("feat/stale", now=NOW)
        assert result["status"] == "deleted"
        assert detector.get_branch("feat/stale") is None

    def test_deleted_removes_warning(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        detector.execute_cleanup("feat/stale", now=NOW + 86401)
        assert "feat/stale" not in detector._warnings

    def test_time_remaining_correct(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        result = detector.execute_cleanup("feat/stale", now=NOW + 1000)
        assert result["time_remaining"] == pytest.approx(86400 - 1000, abs=1)

    def test_fresh_branch_execute_still_deletes(self, detector, fresh_branch):
        detector.register(fresh_branch)
        result = detector.execute_cleanup("feat/fresh", now=NOW)
        assert result["status"] == "deleted"

    def test_returns_last_commit_sha(self, detector, stale_branch):
        detector.register(stale_branch)
        result = detector.execute_cleanup("feat/stale", now=NOW + 86401)
        assert result["last_commit_sha"] == "def456"


class TestUpdateActivity:
    def test_updates_timestamp_and_sha(self, detector, stale_branch):
        detector.register(stale_branch)
        new_ts = NOW + 1000
        detector.update_activity("feat/stale", new_ts, "newsha123")
        r = detector.get_branch("feat/stale")
        assert r.last_activity_ts == pytest.approx(new_ts)
        assert r.last_commit_sha == "newsha123"

    def test_cancels_warning(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        assert "feat/stale" in detector._warnings
        detector.update_activity("feat/stale", NOW, "newsha")
        assert "feat/stale" not in detector._warnings

    def test_after_update_not_stale(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        detector.update_activity("feat/stale", NOW, "newsha")
        stale = detector.detect_stale(now=NOW)
        assert len(stale) == 0

    def test_update_nonexistent_noop(self, detector):
        detector.update_activity("nonexistent", NOW, "abc")
        assert detector.get_branch("nonexistent") is None

    def test_preserves_ticket_id_and_metadata(self, detector):
        r = BranchRecord(branch_name="feat/a", ticket_id="CB-1", last_activity_ts=NOW - 8 * 86400, last_commit_sha="old", metadata={"k": "v"})
        detector.register(r)
        detector.update_activity("feat/a", NOW, "new")
        updated = detector.get_branch("feat/a")
        assert updated.ticket_id == "CB-1"
        assert updated.metadata == {"k": "v"}

    def test_schedule_after_update_requires_fresh_stale_check(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.update_activity("feat/stale", NOW, "newsha")
        result = detector.schedule_cleanup("feat/stale", now=NOW)
        assert result.action == "none"


class TestSummary:
    def test_empty(self, detector):
        s = detector.summary()
        assert s["total_branches"] == 0
        assert s["pending_warnings"] == 0

    def test_counts(self, detector, fresh_branch, stale_branch):
        detector.register(fresh_branch)
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        s = detector.summary()
        assert s["total_branches"] == 2
        assert s["pending_warnings"] == 1

    def test_after_delete(self, detector, stale_branch):
        detector.register(stale_branch)
        detector.schedule_cleanup("feat/stale", now=NOW)
        detector.execute_cleanup("feat/stale", now=NOW + 86401)
        s = detector.summary()
        assert s["total_branches"] == 0
        assert s["pending_warnings"] == 0


class TestDefaults:
    def test_default_thresholds(self):
        d = StaleBranchDetector()
        assert d.stale_threshold_seconds == 7 * 86400
        assert d.warning_period_seconds == 86400
