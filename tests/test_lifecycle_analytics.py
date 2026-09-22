"""Tests for lifecycle analytics: deterministic aggregation of lifecycle_events.jsonl."""

import json
from pathlib import Path

from codebot.review_metrics import (
    build_lifecycle_report,
    lifecycle_report_json,
    load_lifecycle_events,
)


def _write_events(tmp_path: Path, events: list[dict]) -> Path:
    """Write events to lifecycle_events.jsonl and return the state dir."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "lifecycle_events.jsonl"
    lines = [json.dumps(e) for e in events]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return state_dir


def _event(ticket_id: str, from_state: str, to_state: str, ts: float, queue_age: float = 10.0) -> dict:
    return {
        "ticket_id": ticket_id,
        "from_state": from_state,
        "to_state": to_state,
        "timestamp": ts,
        "attempts": 0,
        "rework_count": 0,
        "queue_age_seconds": queue_age,
        "actor": "test",
        "revision": ts,
    }


class TestLoadLifecycleEvents:
    def test_empty_returns_empty_list(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        events = load_lifecycle_events(state_dir)
        assert events == []

    def test_missing_file_returns_empty_list(self, tmp_path):
        events = load_lifecycle_events(tmp_path / "nonexistent")
        assert events == []

    def test_valid_events_loaded(self, tmp_path):
        events_in = [
            _event("CB-1", "DISCOVERED", "VALIDATING", 100.0),
            _event("CB-1", "VALIDATING", "TRIAGED", 200.0),
        ]
        state_dir = _write_events(tmp_path, events_in)
        events = load_lifecycle_events(state_dir)
        assert len(events) == 2
        assert events[0]["ticket_id"] == "CB-1"
        assert events[0]["timestamp"] == 100.0
        assert events[1]["timestamp"] == 200.0

    def test_malformed_lines_skipped(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        path = state_dir / "lifecycle_events.jsonl"
        lines = [
            json.dumps(_event("CB-1", "DISCOVERED", "VALIDATING", 100.0)),
            "not-json{{{",
            json.dumps({"ticket_id": "CB-2"}),  # missing required fields
            "",
            json.dumps(_event("CB-2", "VALIDATING", "TRIAGED", 300.0)),
            json.dumps({"ticket_id": 123, "from_state": "A", "to_state": "B", "timestamp": 1.0}),  # wrong type
        ]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        events = load_lifecycle_events(state_dir)
        assert len(events) == 2
        assert events[0]["ticket_id"] == "CB-1"
        assert events[1]["ticket_id"] == "CB-2"


class TestBuildLifecycleReport:
    def test_empty_events(self):
        report = build_lifecycle_report([])
        assert report["total_transitions"] == 0
        assert report["active_tickets"] == 0
        assert report["terminal_tickets"] == 0
        assert report["avg_cycle_time_seconds"] == 0.0
        assert report["rework_rate"] == 0.0
        assert report["rework_count"] == 0
        assert report["per_stage"] == {}
        assert report["cycle_time_seconds"] == {}

    def test_stage_enter_counts_and_dwell(self):
        events = [
            _event("CB-1", "DISCOVERED", "VALIDATING", 100.0, queue_age=10.0),
            _event("CB-2", "DISCOVERED", "VALIDATING", 110.0, queue_age=30.0),
            _event("CB-1", "VALIDATING", "TRIAGED", 200.0, queue_age=20.0),
        ]
        report = build_lifecycle_report(events)
        assert report["per_stage"]["VALIDATING"]["enter_count"] == 2
        assert report["per_stage"]["VALIDATING"]["avg_queue_age_seconds"] == 20.0  # (10+30)/2
        assert report["per_stage"]["TRIAGED"]["enter_count"] == 1
        assert report["per_stage"]["TRIAGED"]["avg_queue_age_seconds"] == 20.0

    def test_active_vs_terminal_tickets(self):
        events = [
            _event("CB-1", "DISCOVERED", "VALIDATING", 100.0),
            _event("CB-1", "VALIDATING", "TRIAGED", 200.0),
            _event("CB-1", "TRIAGED", "READY", 300.0),
            _event("CB-1", "READY", "IMPLEMENTING", 400.0),
            _event("CB-1", "IMPLEMENTING", "REVIEWING", 500.0),
            _event("CB-1", "REVIEWING", "VERIFYING", 600.0),
            _event("CB-1", "VERIFYING", "COMPLETE", 700.0),
            _event("CB-2", "DISCOVERED", "VALIDATING", 100.0),
            _event("CB-3", "DISCOVERED", "REJECTED", 100.0),
        ]
        report = build_lifecycle_report(events)
        assert report["active_tickets"] == 1   # CB-2
        assert report["terminal_tickets"] == 2  # CB-1 (COMPLETE), CB-3 (REJECTED)

    def test_cycle_time_for_complete_tickets(self):
        events = [
            _event("CB-1", "DISCOVERED", "VALIDATING", 100.0),
            _event("CB-1", "VALIDATING", "COMPLETE", 400.0),
        ]
        report = build_lifecycle_report(events)
        assert report["cycle_time_seconds"] == {"CB-1": 300.0}
        assert report["avg_cycle_time_seconds"] == 300.0

    def test_rework_rate_and_count(self):
        events = [
            _event("CB-1", "DISCOVERED", "VALIDATING", 100.0),
            _event("CB-1", "VALIDATING", "REWORK", 200.0),
            _event("CB-1", "REWORK", "IMPLEMENTING", 300.0),
            _event("CB-1", "IMPLEMENTING", "REWORK", 400.0),
        ]
        report = build_lifecycle_report(events)
        assert report["rework_count"] == 2
        assert report["rework_rate"] == 0.5

    def test_input_not_mutated(self):
        events = [
            _event("CB-1", "DISCOVERED", "VALIDATING", 100.0),
            _event("CB-1", "VALIDATING", "TRIAGED", 200.0),
        ]
        original = [dict(e) for e in events]
        build_lifecycle_report(events)
        assert events == original


class TestLifecycleReportJson:
    def test_json_output_matches_report(self, tmp_path):
        events_in = [
            _event("CB-1", "DISCOVERED", "VALIDATING", 100.0),
            _event("CB-1", "VALIDATING", "COMPLETE", 200.0),
        ]
        state_dir = _write_events(tmp_path, events_in)
        output = lifecycle_report_json(state_dir)
        parsed = json.loads(output)
        assert parsed["total_transitions"] == 2
        assert parsed["terminal_tickets"] == 1
        assert parsed["cycle_time_seconds"] == {"CB-1": 100.0}

    def test_empty_state_dir_returns_valid_json(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        output = lifecycle_report_json(state_dir)
        parsed = json.loads(output)
        assert parsed["total_transitions"] == 0
