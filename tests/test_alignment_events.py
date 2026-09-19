"""Tests for codebot/alignment_events.py — event creation, serialization, persistence."""
import json
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

import codebot.alignment_events as ae


@pytest.fixture
def dirs(tmp_path):
    state = tmp_path / "state"
    logs = tmp_path / "logs"
    state.mkdir(parents=True)
    logs.mkdir(parents=True)
    ae.set_dirs(state, logs)
    yield {"state": state, "logs": logs}
    ae._STATE_DIR = Path(".codebot/state")
    ae._LOGS_DIR = Path("logs")
    ae._ALIGNMENT_EVENTS_DIR = Path(".codebot/state/alignment_events")


class TestWriteAlignmentEvent:
    def test_creates_event_file(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        p = dirs["state"] / "alignment_events" / "bot-a.exit.json"
        assert p.exists()
        data = json.loads(p.read_text())
        assert data["bot"] == "bot-a"
        assert data["exit_code"] == 0
        assert data["exit_reason"] == "clean"

    def test_event_has_required_fields(self, dirs):
        ae.write_alignment_event("bot-a", 1, "error", started_at=time.time() - 100)
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        for field in ("bot", "exit_code", "exit_reason", "exit_time", "exit_time_human",
                      "run_duration", "started_at", "log_path", "stream_path", "checkpoint_path",
                      "heartbeat_age_at_exit", "log_bytes_at_exit", "processed", "processed_at", "version"):
            assert field in data, f"missing {field}"

    def test_processed_false_initially(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["processed"] is False
        assert data["processed_at"] is None

    def test_version_is_1(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["version"] == 1

    def test_run_duration_computed(self, dirs):
        started = time.time() - 42
        ae.write_alignment_event("bot-a", 0, "clean", started_at=started)
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["run_duration"] == pytest.approx(42, abs=1)
        assert data["started_at"] == pytest.approx(started, abs=0.1)

    def test_run_duration_none_when_no_start(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean", started_at=None)
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["run_duration"] is None

    def test_started_from_state_file(self, dirs):
        start_ts = time.time() - 50
        (dirs["state"] / "bot-a.state.json").write_text(json.dumps({"started": start_ts}))
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["started_at"] == pytest.approx(start_ts, abs=0.1)

    def test_started_from_state_file_invalid_ignored(self, dirs):
        (dirs["state"] / "bot-a.state.json").write_text(json.dumps({"started": "not-a-number"}))
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["started_at"] is None

    def test_heartbeat_age_captured(self, dirs):
        now = time.time()
        (dirs["state"] / "bot-a.heartbeat").write_text(str(now - 5))
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["heartbeat_age_at_exit"] is not None
        assert data["heartbeat_age_at_exit"] == pytest.approx(5, abs=1.5)

    def test_heartbeat_iso_format_parsed(self, dirs):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        (dirs["state"] / "bot-a.heartbeat").write_text(now)
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["heartbeat_age_at_exit"] is not None

    def test_heartbeat_missing(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["heartbeat_age_at_exit"] is None

    def test_log_bytes_captured(self, dirs):
        (dirs["logs"] / "bot-a.log").write_text("x" * 1000)
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["log_bytes_at_exit"] == 1000

    def test_log_bytes_none_when_missing(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["log_bytes_at_exit"] is None

    def test_exit_code_none(self, dirs):
        ae.write_alignment_event("bot-a", None, "stuck")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["exit_code"] is None
        assert data["exit_reason"] == "stuck"

    def test_atomic_write_no_tmp_leak(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        assert not (dirs["state"] / "alignment_events" / "bot-a.exit.json.tmp").exists()

    def test_never_raises(self, dirs):
        with patch.object(Path, "write_text", side_effect=OSError("disk full")):
            ae.write_alignment_event("bot-a", 0, "clean")

    def test_overwrites_previous_event(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        ae.write_alignment_event("bot-a", 1, "error")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["exit_code"] == 1
        assert data["exit_reason"] == "error"

    def test_log_and_stream_paths(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert data["log_path"] == "logs/bot-a.log"
        assert data["stream_path"] == "logs/bot-a.stream.json"

    def test_exit_time_human_format(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        data = json.loads((dirs["state"] / "alignment_events" / "bot-a.exit.json").read_text())
        assert "T" in data["exit_time_human"]
        assert data["exit_time_human"].endswith("Z")


class TestReadHeartbeat:
    def test_returns_float(self, dirs):
        (dirs["state"] / "bot-a.heartbeat").write_text(str(time.time()))
        assert ae._read_heartbeat("bot-a") > 0

    def test_missing_returns_zero(self, dirs):
        assert ae._read_heartbeat("nonexistent") == pytest.approx(0.0)

    def test_invalid_returns_zero(self, dirs):
        (dirs["state"] / "bot-a.heartbeat").write_text("not_a_time_at_all_xyz")
        assert ae._read_heartbeat("bot-a") == pytest.approx(0.0)

    def test_iso_future_returns_zero(self, dirs):
        from datetime import datetime, timezone, timedelta
        future = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
        (dirs["state"] / "bot-a.heartbeat").write_text(future)
        assert ae._read_heartbeat("bot-a") == pytest.approx(0.0)


class TestListPendingEvents:
    def test_empty_when_no_events(self, dirs):
        assert ae.list_pending_events() == []

    def test_lists_unprocessed(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        ae.write_alignment_event("bot-b", 1, "error")
        pending = ae.list_pending_events()
        assert len(pending) == 2

    def test_excludes_processed(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        p = dirs["state"] / "alignment_events" / "bot-a.exit.json"
        data = json.loads(p.read_text())
        ae.mark_event_processed(p, data, 0.9, 1.0, "aligned")
        pending = ae.list_pending_events()
        assert len([x for x in pending if x[1]["bot"] == "bot-a"]) == 0

    def test_returns_tuples(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        pending = ae.list_pending_events()
        assert isinstance(pending[0], tuple)
        assert isinstance(pending[0][0], Path)
        assert isinstance(pending[0][1], dict)

    def test_corrupt_file_skipped(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        (dirs["state"] / "alignment_events" / "bad.exit.json").write_text("not json")
        pending = ae.list_pending_events()
        assert len(pending) == 1

    def test_creates_dir_if_missing(self, dirs):
        import shutil
        shutil.rmtree(dirs["state"] / "alignment_events")
        result = ae.list_pending_events()
        assert result == []
        assert (dirs["state"] / "alignment_events").exists()


class TestMarkEventProcessed:
    def test_marks_processed(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        p = dirs["state"] / "alignment_events" / "bot-a.exit.json"
        data = json.loads(p.read_text())
        ae.mark_event_processed(p, data, 0.85, 0.5, "aligned")
        updated = json.loads(p.read_text())
        assert updated["processed"] is True
        assert updated["processed_at"] is not None
        assert updated["score"] == pytest.approx(0.85)
        assert updated["reward"] == pytest.approx(0.5)
        assert updated["verdict"] == "aligned"

    def test_processed_at_is_timestamp(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        p = dirs["state"] / "alignment_events" / "bot-a.exit.json"
        data = json.loads(p.read_text())
        before = time.time()
        ae.mark_event_processed(p, data, 1.0, 1.0, "aligned")
        after = time.time()
        updated = json.loads(p.read_text())
        assert before <= updated["processed_at"] <= after

    def test_atomic_write(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        p = dirs["state"] / "alignment_events" / "bot-a.exit.json"
        data = json.loads(p.read_text())
        ae.mark_event_processed(p, data, 0.5, 0.5, "misaligned")
        assert not (dirs["state"] / "alignment_events" / "bot-a.exit.json.tmp").exists()

    def test_never_raises(self, dirs):
        ae.write_alignment_event("bot-a", 0, "clean")
        p = dirs["state"] / "alignment_events" / "bot-a.exit.json"
        data = json.loads(p.read_text())
        with patch.object(Path, "write_text", side_effect=OSError("fail")):
            ae.mark_event_processed(p, data, 0.5, 0.5, "aligned")

    def test_verdict_values(self, dirs):
        for verdict in ("aligned", "misaligned", "unknown"):
            ae.write_alignment_event(f"bot-{verdict}", 0, "clean")
            p = dirs["state"] / "alignment_events" / f"bot-{verdict}.exit.json"
            data = json.loads(p.read_text())
            ae.mark_event_processed(p, data, 0.5, 0.5, verdict)
            updated = json.loads(p.read_text())
            assert updated["verdict"] == verdict


class TestCollectReviewerFeedback:
    def test_no_tickets_file_returns_empty(self, dirs):
        result = ae.collect_reviewer_feedback_for_trigger("bot-a")
        assert result == []

    def test_with_feedback(self, dirs):
        from codebot.ticket_engine import TicketStore, TicketClass, Severity, RiskLevel, create_ticket, TicketState
        store_path = dirs["state"] / "tickets.json"
        store = TicketStore(store_path)
        t = create_ticket("feedback task", TicketClass.BUG, Severity.MEDIUM, "test", "ev", "problem", "desired", ["ac"], risk=RiskLevel.LOW)
        store.add(t)
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.TRIAGED)
        store.transition(t.id, TicketState.READY)
        store.transition(t.id, TicketState.IMPLEMENTING)
        store.transition(t.id, TicketState.REWORK, reviewer_feedback=[{"reviewer": "rev", "file": "a.py", "description": "bad", "recommendation": "fix it"}])
        store.transition(t.id, TicketState.IMPLEMENTING)
        store.transition(t.id, TicketState.REWORK, reviewer_feedback=[{"reviewer": "rev2", "file": "b.py", "description": "worse", "recommendation": "rewrite"}])
        store.transition(t.id, TicketState.IMPLEMENTING)
        store.transition(t.id, TicketState.REWORK, reviewer_feedback=[{"reviewer": "rev3", "file": "c.py", "description": "worst", "recommendation": "redo"}])
        t_final = store.get(t.id)
        t_final_assigned = t_final  # type: ignore
        # manually assign bot to trigger feedback collection (Ticket has assigned_agent field)
        import dataclasses
        updated_ticket = dataclasses.replace(t_final, assigned_agent="bot-a")  # type: ignore
        store._tickets[t_final.id] = updated_ticket  # type: ignore
        store._save()  # type: ignore
        result = ae.collect_reviewer_feedback_for_trigger("bot-a")
        assert len(result) >= 1
        assert result[0]["ticket_id"] == t.id

    def test_below_rework_threshold_returns_empty(self, dirs):
        from codebot.ticket_engine import TicketStore, TicketClass, Severity, RiskLevel, create_ticket
        store_path = dirs["state"] / "tickets.json"
        store = TicketStore(store_path)
        t = create_ticket("low rework", TicketClass.BUG, Severity.MEDIUM, "test", "ev2", "prob", "des", ["ac"], risk=RiskLevel.LOW)
        store.add(t)
        import dataclasses
        t2 = dataclasses.replace(store.get(t.id), assigned_agent="bot-a", rework_count=1, reviewer_feedback=[{"reviewer": "r", "file": "f", "description": "d", "recommendation": "rec"}])  # type: ignore
        store._tickets[t.id] = t2  # type: ignore
        store._save()  # type: ignore
        result = ae.collect_reviewer_feedback_for_trigger("bot-a")
        assert result == []

    def test_corrupt_tickets_handled(self, dirs):
        (dirs["state"] / "tickets.json").write_text("not json")
        result = ae.collect_reviewer_feedback_for_trigger("bot-a")
        assert result == []


class TestSetDirs:
    def test_set_dirs_creates_alignment_events(self, tmp_path):
        state = tmp_path / "s"
        logs = tmp_path / "l"
        ae.set_dirs(state, logs)
        assert (state / "alignment_events").exists()
        ae._STATE_DIR = Path(".codebot/state")
        ae._LOGS_DIR = Path("logs")
        ae._ALIGNMENT_EVENTS_DIR = Path(".codebot/state/alignment_events")

    def test_ensure_dirs(self, dirs):
        import shutil
        shutil.rmtree(dirs["state"] / "alignment_events")
        ae._ensure_dirs()
        assert (dirs["state"] / "alignment_events").exists()


class TestStartedAtFor:
    def test_returns_none_when_no_file(self, dirs):
        assert ae._started_at_for("nope") is None

    def test_returns_float(self, dirs):
        ts = time.time()
        (dirs["state"] / "bot-x.state.json").write_text(json.dumps({"started": ts}))
        assert ae._started_at_for("bot-x") == pytest.approx(ts)

    def test_corrupt_returns_none(self, dirs):
        (dirs["state"] / "bot-x.state.json").write_text("bad json")
        assert ae._started_at_for("bot-x") is None

    def test_zero_returns_none(self, dirs):
        (dirs["state"] / "bot-x.state.json").write_text(json.dumps({"started": 0}))
        assert ae._started_at_for("bot-x") is None

    def test_negative_returns_none(self, dirs):
        (dirs["state"] / "bot-x.state.json").write_text(json.dumps({"started": -5}))
        assert ae._started_at_for("bot-x") is None


class TestLogBytes:
    def test_returns_size(self, dirs):
        (dirs["logs"] / "bot-a.log").write_text("hello")
        assert ae._log_bytes("bot-a") == 5

    def test_missing_returns_none(self, dirs):
        assert ae._log_bytes("missing") is None
