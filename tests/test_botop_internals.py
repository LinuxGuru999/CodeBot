#!/usr/bin/env python3
"""Tests for codebot/botop.py internal helpers.

Covers:
- _parse_checkpoint_text: valid JSON, Python dict, trailing garbage, empty input
- _collect_agents: mock state directory with heartbeat/status/state files
- _collect_ticket_throughput: rework_rate, avg_age_h, oldest_h calculations
- cmd_status: correct table output, exit 0
- cmd_health: aggregates all subsystems without exceptions
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.botop import (
    _parse_checkpoint_text,
    _collect_agents,
    _collect_ticket_throughput,
    cmd_status,
    cmd_health,
)


# =============================================================================
# _parse_checkpoint_text tests
# =============================================================================

class TestParseCheckpointText:
    def test_valid_json(self):
        """Test parsing valid JSON checkpoint."""
        raw = '{"processed_ids": ["CB-123"], "updated_at": 1716120000.0}'
        result = _parse_checkpoint_text(raw)
        assert result is not None
        assert result["processed_ids"] == ["CB-123"]
        assert result["updated_at"] == 1716120000.0

    def test_python_dict_single_quotes(self):
        """Test parsing Python dict with single quotes via ast.literal_eval."""
        raw = "{'processed_ids': ['CB-456'], 'updated_at': 1716120100.0}"
        result = _parse_checkpoint_text(raw)
        assert result is not None
        assert result["processed_ids"] == ["CB-456"]
        assert result["updated_at"] == 1716120100.0

    def test_trailing_garbage(self):
        """Test parsing JSON with trailing garbage - extracts valid block."""
        raw = '{"key": "value"}, extra garbage here'
        result = _parse_checkpoint_text(raw)
        assert result is not None
        assert result["key"] == "value"

    def test_empty_input(self):
        """Test parsing empty or whitespace-only input."""
        assert _parse_checkpoint_text("") is None
        assert _parse_checkpoint_text("   ") is None
        assert _parse_checkpoint_text("\n\t") is None

    def test_invalid_json_and_dict(self):
        """Test parsing completely invalid input returns None."""
        raw = "not json at all { broken"
        result = _parse_checkpoint_text(raw)
        assert result is None

    def test_nested_json(self):
        """Test parsing nested JSON structures."""
        raw = '{"outer": {"inner": [1, 2, 3]}, "flag": true}'
        result = _parse_checkpoint_text(raw)
        assert result is not None
        assert result["outer"]["inner"] == [1, 2, 3]
        assert result["flag"] is True

    def test_brace_balanced_extraction(self):
        """Test extracting JSON from text with surrounding content."""
        raw = "Some prefix text {\"extracted\": \"value\"} some suffix"
        result = _parse_checkpoint_text(raw)
        assert result is not None
        assert result["extracted"] == "value"


# =============================================================================
# _collect_agents tests
# =============================================================================

class TestCollectAgents:
    @pytest.fixture
    def mock_state_dir(self, tmp_path):
        """Create a mock state directory with test data."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        
        # Create heartbeat file for running agent
        hb_path = state_dir / "test_agent.heartbeat"
        hb_path.write_text(str(time.time()))
        
        # Create status file
        status_path = state_dir / "test_agent.status.json"
        status_data = {"current_task": "testing", "iteration": 5}
        status_path.write_text(json.dumps(status_data))
        
        # Create state file
        state_path = state_dir / "test_agent.state.json"
        state_data = {"restart_count": 2, "consecutive_errors": 0, "status": "active"}
        state_path.write_text(json.dumps(state_data))
        
        # Create checkpoint file
        ckpt_path = state_dir / "test_agent.checkpoint.json"
        ckpt_data = {"processed_ids": ["CB-789"], "reason": "completed"}
        ckpt_path.write_text(json.dumps(ckpt_data))
        
        # Create paused agent
        paused_path = state_dir / "paused_agent.paused"
        paused_path.write_text(str(time.time()))
        paused_hb = state_dir / "paused_agent.heartbeat"
        paused_hb.write_text(str(time.time() - 30))  # 30 seconds ago
        
        # Create stale agent (old heartbeat)
        stale_hb = state_dir / "stale_agent.heartbeat"
        stale_hb.write_text(str(time.time() - 700))  # >600 seconds = DEAD
        
        return state_dir

    def test_collect_agents_running(self, mock_state_dir, monkeypatch):
        """Test collecting agents with RUNNING status."""
        project_root = mock_state_dir.parent
        
        # Mock _find_state_dir to return our test directory
        def mock_find_state_dir(pr):
            return mock_state_dir
        
        def mock_find_logs_dir(pr):
            return mock_state_dir.parent / "logs"
        
        monkeypatch.setattr("codebot.botop._find_state_dir", mock_find_state_dir)
        monkeypatch.setattr("codebot.botop._find_logs_dir", mock_find_logs_dir)
        monkeypatch.setattr("codebot.botop._find_all_api_pids", lambda: {})
        monkeypatch.setattr("codebot.botop._find_agent_pid", lambda name: None)
        
        agents = _collect_agents(project_root)
        
        # Should find at least test_agent and paused_agent and stale_agent
        assert len(agents) >= 3
        
        # Find test_agent
        test_agent = next((a for a in agents if a["name"] == "test_agent"), None)
        assert test_agent is not None
        assert test_agent["bucket"] == "RUNNING"
        assert test_agent["iteration"] == 5
        assert test_agent["restart_count"] == 2

    def test_collect_agents_paused(self, mock_state_dir, monkeypatch):
        """Test collecting agents with PAUSED status."""
        project_root = mock_state_dir.parent
        
        def mock_find_state_dir(pr):
            return mock_state_dir
        
        def mock_find_logs_dir(pr):
            return mock_state_dir.parent / "logs"
        
        monkeypatch.setattr("codebot.botop._find_state_dir", mock_find_state_dir)
        monkeypatch.setattr("codebot.botop._find_logs_dir", mock_find_logs_dir)
        monkeypatch.setattr("codebot.botop._find_all_api_pids", lambda: {})
        monkeypatch.setattr("codebot.botop._find_agent_pid", lambda name: None)
        
        agents = _collect_agents(project_root)
        
        paused_agent = next((a for a in agents if a["name"] == "paused_agent"), None)
        assert paused_agent is not None
        assert paused_agent["bucket"] == "PAUSED"
        assert paused_agent["paused"] is True

    def test_collect_agents_stale(self, mock_state_dir, monkeypatch):
        """Test collecting agents with STALE/DEAD status based on heartbeat age."""
        project_root = mock_state_dir.parent
        
        def mock_find_state_dir(pr):
            return mock_state_dir
        
        def mock_find_logs_dir(pr):
            return mock_state_dir.parent / "logs"
        
        monkeypatch.setattr("codebot.botop._find_state_dir", mock_find_state_dir)
        monkeypatch.setattr("codebot.botop._find_logs_dir", mock_find_logs_dir)
        monkeypatch.setattr("codebot.botop._find_all_api_pids", lambda: {})
        monkeypatch.setattr("codebot.botop._find_agent_pid", lambda name: None)
        
        agents = _collect_agents(project_root)
        
        stale_agent = next((a for a in agents if a["name"] == "stale_agent"), None)
        assert stale_agent is not None
        # Heartbeat >600s old should be DEAD
        assert stale_agent["bucket"] == "DEAD"


# =============================================================================
# _collect_ticket_throughput tests
# =============================================================================

class TestCollectTicketThroughput:
    def test_empty_tickets(self):
        """Test throughput calculation with no tickets."""
        result = _collect_ticket_throughput([])
        assert result["total"] == 0
        assert result["rework_rate"] == 0
        assert result["avg_age_h"] is None
        assert result["oldest_h"] is None

    def test_basic_throughput(self):
        """Test basic throughput metrics calculation."""
        now = time.time()
        tickets = [
            {"state": "COMPLETE", "created_at": now - 3600, "rework_count": 0},  # 1 hour old
            {"state": "IMPLEMENTING", "created_at": now - 7200, "rework_count": 1},  # 2 hours old, reworked
            {"state": "REVIEWING", "created_at": now - 10800, "rework_count": 0},  # 3 hours old
        ]
        
        result = _collect_ticket_throughput(tickets)
        
        assert result["total"] == 3
        assert result["by_state"]["COMPLETE"] == 1
        assert result["by_state"]["IMPLEMENTING"] == 1
        assert result["by_state"]["REVIEWING"] == 1
        assert result["rework_rate"] == pytest.approx(1/3, rel=0.01)  # 1 out of 3 reworked
        assert result["avg_age_h"] is not None
        assert result["avg_age_h"] == pytest.approx(2.0, rel=0.1)  # Average of 1, 2, 3 hours
        assert result["oldest_h"] is not None
        assert result["oldest_h"] == pytest.approx(3.0, rel=0.1)

    def test_rework_rate_calculation(self):
        """Test rework rate is calculated correctly."""
        now = time.time()
        tickets = [
            {"state": "COMPLETE", "created_at": now - 100, "rework_count": 0},
            {"state": "COMPLETE", "created_at": now - 200, "rework_count": 2},
            {"state": "COMPLETE", "created_at": now - 300, "rework_count": 0},
            {"state": "COMPLETE", "created_at": now - 400, "rework_count": 1},
        ]
        
        result = _collect_ticket_throughput(tickets)
        
        # 2 out of 4 tickets have rework_count > 0
        assert result["rework_rate"] == pytest.approx(0.5, rel=0.01)

    def test_throughput_24h_7d(self):
        """Test 24h and 7d throughput counts."""
        now = time.time()
        tickets = [
            {"state": "DISCOVERED", "created_at": now - 1000},  # Within 24h (1000s < 86400s)
            {"state": "DISCOVERED", "created_at": now - 100000},  # Within 7d but NOT 24h (100000s > 86400s, < 604800s)
            {"state": "DISCOVERED", "created_at": now - 1000000},  # Older than 7d (1000000s > 604800s)
        ]
        
        result = _collect_ticket_throughput(tickets)
        
        # Only first ticket (1000s) is within 24h (86400s)
        assert result["throughput_24h"] == 1
        # First two tickets (1000s and 100000s) are within 7d (604800s)
        assert result["throughput_7d"] == 2


# =============================================================================
# cmd_status tests
# =============================================================================

class TestCmdStatus:
    def test_cmd_status_exits_zero(self, tmp_path, monkeypatch, capsys):
        """Test cmd_status returns exit code 0."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        
        logs_dir = tmp_path / ".codebot" / "logs"
        logs_dir.mkdir(parents=True)
        
        project_root = tmp_path
        
        def mock_find_state_dir(pr):
            return state_dir
        
        def mock_find_logs_dir(pr):
            return logs_dir
        
        monkeypatch.setattr("codebot.botop._find_state_dir", mock_find_state_dir)
        monkeypatch.setattr("codebot.botop._find_logs_dir", mock_find_logs_dir)
        monkeypatch.setattr("codebot.botop._collect_orchestrator_info", lambda pr: {"pid": None, "alive": False})
        monkeypatch.setattr("codebot.botop._collect_agents", lambda pr: [])
        
        exit_code = cmd_status(project_root)
        
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "CodeBot status" in captured.out or "No agents found" in captured.out

    def test_cmd_status_json_output(self, tmp_path, monkeypatch, capsys):
        """Test cmd_status with JSON output."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        
        logs_dir = tmp_path / ".codebot" / "logs"
        logs_dir.mkdir(parents=True)
        
        project_root = tmp_path
        
        def mock_find_state_dir(pr):
            return state_dir
        
        def mock_find_logs_dir(pr):
            return logs_dir
        
        monkeypatch.setattr("codebot.botop._find_state_dir", mock_find_state_dir)
        monkeypatch.setattr("codebot.botop._find_logs_dir", mock_find_logs_dir)
        monkeypatch.setattr("codebot.botop._collect_orchestrator_info", lambda pr: {"pid": None, "alive": False})
        monkeypatch.setattr("codebot.botop._collect_agents", lambda pr: [])
        
        exit_code = cmd_status(project_root, json_out=True)
        
        assert exit_code == 0
        captured = capsys.readouterr()
        # Should be valid JSON
        output = json.loads(captured.out)
        assert "project" in output
        assert "agents" in output


# =============================================================================
# cmd_health tests
# =============================================================================

class TestCmdHealth:
    def test_cmd_health_no_exceptions(self, tmp_path, monkeypatch, capsys):
        """Test cmd_health aggregates all subsystems without raising exceptions."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        
        logs_dir = tmp_path / ".codebot" / "logs"
        logs_dir.mkdir(parents=True)
        
        project_root = tmp_path
        
        # Write minimal state files
        (state_dir / "tickets.json").write_text(json.dumps({"tickets": []}))
        
        def mock_find_state_dir(pr):
            return state_dir
        
        def mock_find_logs_dir(pr):
            return logs_dir
        
        monkeypatch.setattr("codebot.botop._find_state_dir", mock_find_state_dir)
        monkeypatch.setattr("codebot.botop._find_logs_dir", mock_find_logs_dir)
        monkeypatch.setattr("codebot.botop._find_project_name", lambda pr: "test_project")
        
        # Should not raise any exceptions
        exit_code = cmd_health(project_root)
        
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "CodeBot Diagnostics" in captured.out

    def test_cmd_health_json_output(self, tmp_path, monkeypatch, capsys):
        """Test cmd_health with JSON output."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        
        logs_dir = tmp_path / ".codebot" / "logs"
        logs_dir.mkdir(parents=True)
        
        project_root = tmp_path
        
        (state_dir / "tickets.json").write_text(json.dumps({"tickets": []}))
        
        def mock_find_state_dir(pr):
            return state_dir
        
        def mock_find_logs_dir(pr):
            return logs_dir
        
        monkeypatch.setattr("codebot.botop._find_state_dir", mock_find_state_dir)
        monkeypatch.setattr("codebot.botop._find_logs_dir", mock_find_logs_dir)
        monkeypatch.setattr("codebot.botop._find_project_name", lambda pr: "test_project")
        
        exit_code = cmd_health(project_root, json_out=True)
        
        assert exit_code == 0
        captured = capsys.readouterr()
        # Should be valid JSON
        output = json.loads(captured.out)
        assert "project" in output
        assert "orchestrator" in output
        assert "agents_summary" in output
