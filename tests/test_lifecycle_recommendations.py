"""Tests for Task 7: guarded rework/escaped-defect feedback recommendations."""

import json
from pathlib import Path

from codebot.review_metrics import build_lifecycle_recommendations


def _write_events(state_dir: Path, events: list[dict]) -> None:
    path = state_dir / "lifecycle_events.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


def _event(ticket_id: str, from_state: str, to_state: str, ts: float) -> dict:
    return {
        "ticket_id": ticket_id, "from_state": from_state, "to_state": to_state,
        "timestamp": ts, "attempts": 0, "rework_count": 0,
        "queue_age_seconds": 10.0, "actor": "test", "revision": ts,
    }


class TestLifecycleRecommendations:
    def test_empty_events_no_recommendations(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        recs = build_lifecycle_recommendations(state_dir)
        assert recs == []

    def test_few_events_no_recommendations(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events = [_event(f"CB-{i}", "DISCOVERED", "VALIDATING", 100.0 + i) for i in range(10)]
        _write_events(state_dir, events)
        recs = build_lifecycle_recommendations(state_dir)
        assert recs == []

    def test_high_rework_rate_generates_advisory(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events = []
        for i in range(30):
            tid = f"CB-{i:03d}"
            events.append(_event(tid, "DISCOVERED", "VALIDATING", 100.0 + i * 10))
            events.append(_event(tid, "VALIDATING", "REWORK", 200.0 + i * 10))
        _write_events(state_dir, events)
        recs = build_lifecycle_recommendations(state_dir)
        rework_recs = [r for r in recs if r["type"] == "rework_rate"]
        assert len(rework_recs) == 1
        assert rework_recs[0]["action"] == "advisory"
        assert rework_recs[0]["severity"] == "warning"
        assert "Rework rate" in rework_recs[0]["message"]

    def test_low_rework_rate_no_advisory(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events = []
        for i in range(30):
            tid = f"CB-{i:03d}"
            events.append(_event(tid, "DISCOVERED", "VALIDATING", 100.0 + i * 10))
            events.append(_event(tid, "VALIDATING", "TRIAGED", 200.0 + i * 10))
        _write_events(state_dir, events)
        recs = build_lifecycle_recommendations(state_dir)
        rework_recs = [r for r in recs if r["type"] == "rework_rate"]
        assert len(rework_recs) == 0

    def test_escaped_defects_generates_advisory(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events = [_event(f"CB-{i}", "DISCOVERED", "VALIDATING", 100.0 + i) for i in range(25)]
        _write_events(state_dir, events)
        escaped_path = state_dir / "escaped_defects.jsonl"
        lines = [json.dumps({"ticket_id": f"CB-{i}", "defect_type": "logic_error"}) for i in range(6)]
        escaped_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        recs = build_lifecycle_recommendations(state_dir)
        escaped_recs = [r for r in recs if r["type"] == "escaped_defects"]
        assert len(escaped_recs) == 1
        assert escaped_recs[0]["severity"] == "critical"

    def test_recommendations_never_disable_checks(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events = []
        for i in range(30):
            tid = f"CB-{i:03d}"
            events.append(_event(tid, "DISCOVERED", "VALIDATING", 100.0 + i * 10))
            events.append(_event(tid, "VALIDATING", "REWORK", 200.0 + i * 10))
        _write_events(state_dir, events)
        recs = build_lifecycle_recommendations(state_dir)
        for rec in recs:
            assert rec["action"] == "advisory"
            assert "disable" not in rec.get("message", "").lower()
            assert "bypass" not in rec.get("message", "").lower()
            assert "skip" not in rec.get("message", "").lower()

    def test_recommendations_are_bounded(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        events = []
        for i in range(100):
            tid = f"CB-{i:03d}"
            events.append(_event(tid, "DISCOVERED", "VALIDATING", 100.0 + i))
            events.append(_event(tid, "VALIDATING", "REWORK", 200.0 + i))
        _write_events(state_dir, events)
        escaped_path = state_dir / "escaped_defects.jsonl"
        lines = [json.dumps({"ticket_id": f"CB-{i}", "defect_type": "bug"}) for i in range(10)]
        escaped_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        recs = build_lifecycle_recommendations(state_dir)
        assert len(recs) <= 10
