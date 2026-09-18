"""Tests for cost_tracker.py."""
import json
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from cost_tracker import CostTracker, TicketCost

class TestRecordPhaseCost:
    def test_record_creates_entry(self, tmp_path):
        ct = CostTracker(tmp_path)
        entry = ct.record_phase_cost("CB-1", "worker-1", "qwen-3.8-max", 1000, 500, "implementation")
        assert entry.ticket_id == "CB-1"
        assert entry.total_tokens == 1500
        assert entry.phase == "implementation"

    def test_invalid_ticket_id_raises(self, tmp_path):
        ct = CostTracker(tmp_path)
        with pytest.raises(ValueError, match="invalid ticket_id"):
            ct.record_phase_cost("INVALID-1", "w", "m", 0, 0, "p")

    def test_negative_tokens_clamped(self, tmp_path):
        ct = CostTracker(tmp_path)
        entry = ct.record_phase_cost("CB-1", "w", "m", -100, -50, "p")
        assert entry.prompt_tokens == 0
        assert entry.completion_tokens == 0

class TestGetTicketTotal:
    def test_aggregates_multiple_phases(self, tmp_path):
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "planning")
        ct.record_phase_cost("CB-1", "w2", "m2", 200, 100, "implementation")
        total = ct.get_ticket_total("CB-1")
        assert total["total_tokens"] == 450
        assert total["prompt_tokens"] == 300
        assert total["completion_tokens"] == 150
        assert "planning" in total["by_phase"]
        assert "implementation" in total["by_phase"]

    def test_empty_ticket(self, tmp_path):
        ct = CostTracker(tmp_path)
        total = ct.get_ticket_total("CB-missing")
        assert total["total_tokens"] == 0

class TestBuildSummary:
    def test_summary_structure(self, tmp_path):
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "planning")
        ct.record_phase_cost("CB-2", "w2", "m2", 200, 100, "implementation")
        summary = ct.build_summary()
        assert "tickets" in summary
        assert "fleet_totals" in summary
        assert summary["fleet_totals"]["ticket_count"] == 2
        assert summary["fleet_totals"]["total_tokens"] == 450

    def test_summary_persisted(self, tmp_path):
        ct = CostTracker(tmp_path)
        ct.record_phase_cost("CB-1", "w1", "m1", 100, 50, "p")
        ct.build_summary()
        assert (tmp_path / "ticket_cost_summary.json").exists()

    def test_empty_summary(self, tmp_path):
        ct = CostTracker(tmp_path)
        summary = ct.build_summary()
        assert summary["tickets"] == {}
