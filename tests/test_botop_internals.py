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
from unittest.mock import patch, MagicMock

import pytest

# Import the module under test
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.botop import (
    _parse_checkpoint_text,
    _collect_agents,
    _collect_ticket_throughput,
    cmd_status,
    cmd_health,
    _find_state_dir,
    _find_logs_dir,
)


# ---------------------------------------------------------------------------
# _parse_checkpoint_text tests
# ---------------------------------------------------------------------------

class TestParseCheckpointText:
    def test_valid_json(self):
        raw = '{"processed_ids": ["CB-123"], "updated_at": 1716120000.0}'
        result = _parse_checkpoint_text(raw)
        assert result is not None
        assert result["processed_ids"] == ["CB-123"]
        assert result["updated_at"] == 1716120000.0

    def test_python_dict_literal(self):
        raw = "{'processed_ids': ['CB-456'], 'updated_at': 1716120100.0}"
        result = _parse_checkpoint_text(raw)
        assert result is not None
        assert result["processed_ids"] == ["CB-456"]
        assert result["updated_at"] == 1716120100.0

    def test_trailing_garbage_json(self):
        raw = '{"key": "value"},\nextra garbage here'
        result = _parse_checkpoint_text(raw)
        assert result is not None
        assert result["key"] == "value"

    def test_empty_input(self):
        assert _parse_checkpoint_text("") is None
        assert _parse_checkpoint_text("   ") is None
        assert _parse_checkpoint_text("\n\t") is None

    def test_non_dict_json(self):
        # JSON array should return None
        raw = '["item1", "item2"]'
        result = _parse_checkpoint_text(raw)
        assert result is None

    def test_brace_balanced_extraction(self):
        raw = "prefix noise {\"extracted\": true} suffix noise"
        result = _parse_checkpoint_text(raw)
        assert result is not None
        assert result["extracted"] is True

    def test_invalid_input_returns_none(self):
        raw = "not json or dict at all {{{"
        result = _parse_checkpoint_text(raw)
        assert result is None


# ---------------------------------------------------------------------------
# _collect_ticket_throughput tests
# ---------------------------------------------------------------------------

class TestCollectTicketThroughput:
    def test_empty_tickets(self):
        result = _collect_ticket_throughput([])
        assert result["total"] == 0
        assert result["rework_rate"] == 0
        assert result["avg_age_h"] is None

    def test_single_ticket_no_rework(self):
        now = time.time()
        tickets = [{
            "id": "CB-001",
            "state": "COMPLETE",
            "severity": "high",
            "ticket_class": "bug",
            "created_at": now - 3600,  # 1 hour ago
            "rework_count": 0,
        }]
        result = _collect_ticket_throughput(tickets)
        assert result["total"] == 1
        assert result["rework_rate"] == 0.0
        assert result["avg_age_h"] is not None
        assert 0.9 < result["avg_age_h"] < 1.1  # ~1 hour
        assert result["oldest_h"] is not None
        assert 0.9 < result["oldest_h"] < 1.1

    def test_multiple_tickets_with_rework(self):
        now = time.time()
        tickets = [
            {"id": "CB-001", "state": "COMPLETE", "created_at": now - 7200, "rework_count": 0},
            {"id": "CB-002", "state": "REWORK", "created_at": now - 3600, "rework_count": 2},
            {"id": "CB-003", "state": "IMPLEMENTING", "created_at": now - 1800, "rework_count": 1},
        ]
        result = _collect_ticket_throughput(tickets)
        assert result["total"] == 3
        # 2 out of 3 have rework > 0
        assert result["rework_rate"] == pytest.approx(2/3, rel=0.01)
        assert result["avg_age_h"] is not None
        # avg age: (2h + 1h + 0.5h) / 3 = 1.166h
        assert 1.0 < result["avg_age_h"] < 1.3

    def test_throughput_24h_and_7d(self):
        now = time.time()
        tickets = [
            {"id": "CB-001", "created_at": now - 3600},  # 1h ago -> within 24h and 7d
            {"id": "CB-002", "created_at": now - 86400 * 2},  # 2 days ago -> within 7d only
            {"id": "CB-003", "created_at": now - 86400 * 10},  # 10 days ago -> outside both
        ]
        result = _collect_ticket_throughput(tickets)
        assert result["throughput_24h"] == 1
        assert result["throughput_7d"] == 2

    def test_by_state_by_severity_by_class(self):
        tickets = [
            {"state": "COMPLETE", "severity": "high", "ticket_class": "bug"},
            {"state": "COMPLETE", "severity": "medium", "ticket_class": "feature"},
            {"state": "REWORK", "severity": "critical", "ticket_class": "security"},
        ]
        result = _collect_ticket_throughput(tickets)
        assert result["by_state"] == {"COMPLETE": 2, "REWORK": 1}
        assert result["by_severity"] == {"high": 1, "medium": 1, "critical": 1}
        assert result["by_class"] == {"bug": 1, "feature": 1, "security": 1}


# ---------------------------------------------------------------------------
# _collect_agents tests
# ---------------------------------------------------------------------------

class TestCollectAgents:
    def test_empty_state_dir(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            state_dir = project_root / ".codebot" / "state"
            state_dir.mkdir(parents=True)
            logs_dir = project_root / ".codebot" / "logs"
            logs_dir.mkdir(parents=True)
            
            agents = _collect_agents(project_root)
            assert agents == []

    def test_agent_with_heartbeat_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            state_dir = project_root / ".codebot" / "state"
            state_dir.mkdir(parents=True)
            logs_dir = project_root / ".codebot" / "logs"
            logs_dir.mkdir(parents=True)
            
            now = time.time()
            # Create heartbeat file (age < 120s -> RUNNING)
            hb_path = state_dir / "test_agent.heartbeat"
            hb_path.write_text(str(now - 30))
            
            # Create status file
            status_path = state_dir / "test_agent.status.json"
            status_path.write_text(json.dumps({"current_task": "testing", "iteration": 5}))
            
            # Create state file
            state_path = state_dir / "test_agent.state.json"
            state_path.write_text(json.dumps({"status": "active", "restart_count": 0}))
            
            agents = _collect_agents(project_root)
            assert len(agents) == 1
            agent = agents[0]
            assert agent["name"] == "test_agent"
            assert agent["bucket"] == "RUNNING"
            assert agent["running"] is False  # no actual PID
            assert agent["hb_age"] is not None
            assert agent["hb_age"] < 60
            assert agent["current_task"] == "testing"
            assert agent["iteration"] == 5

    def test_agent_stale_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            state_dir = project_root / ".codebot" / "state"
            state_dir.mkdir(parents=True)
            logs_dir = project_root / ".codebot" / "logs"
            logs_dir.mkdir(parents=True)
            
            now = time.time()
            # Create heartbeat file (age 300s -> STALE, between 120 and 600)
            hb_path = state_dir / "stale_agent.heartbeat"
            hb_path.write_text(str(now - 300))
            
            agents = _collect_agents(project_root)
            assert len(agents) == 1
            assert agents[0]["bucket"] == "STALE"

    def test_agent_dead_heartbeat(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            state_dir = project_root / ".codebot" / "state"
            state_dir.mkdir(parents=True)
            logs_dir = project_root / ".codebot" / "logs"
            logs_dir.mkdir(parents=True)
            
            now = time.time()
            # Create heartbeat file (age > 600s -> DEAD)
            hb_path = state_dir / "dead_agent.heartbeat"
            hb_path.write_text(str(now - 700))
            
            agents = _collect_agents(project_root)
            assert len(agents) == 1
            assert agents[0]["bucket"] == "DEAD"

    def test_agent_paused(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            state_dir = project_root / ".codebot" / "state"
            state_dir.mkdir(parents=True)
            logs_dir = project_root / ".codebot" / "logs"
            logs_dir.mkdir(parents=True)
            
            now = time.time()
            hb_path = state_dir / "paused_agent.heartbeat"
            hb_path.write_text(str(now - 30))
            paused_path = state_dir / "paused_agent.paused"
            paused_path.write_text(str(now))
            
            agents = _collect_agents(project_root)
            assert len(agents) == 1
            assert agents[0]["bucket"] == "PAUSED"
            assert agents[0]["paused"] is True


# ---------------------------------------------------------------------------
# cmd_status tests
# ---------------------------------------------------------------------------

class TestCmdStatus:
    def test_cmd_status_exits_zero(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            state_dir = project_root / ".codebot" / "state"
            state_dir.mkdir(parents=True)
            logs_dir = project_root / ".codebot" / "logs"
            logs_dir.mkdir(parents=True)
            
            # Capture stdout
            import io
            from contextlib import redirect_stdout
            
            f = io.StringIO()
            with redirect_stdout(f):
                exit_code = cmd_status(project_root, verbose=False, json_out=False)
            
            assert exit_code == 0
            output = f.getvalue()
            assert "CodeBot status" in output
            assert "No agents found" in output or "Agent" in output

    def test_cmd_status_json_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            state_dir = project_root / ".codebot" / "state"
            state_dir.mkdir(parents=True)
            logs_dir = project_root / ".codebot" / "logs"
            logs_dir.mkdir(parents=True)
            
            import io
            from contextlib import redirect_stdout
            import json as json_module
            
            f = io.StringIO()
            with redirect_stdout(f):
                exit_code = cmd_status(project_root, json_out=True)
            
            assert exit_code == 0
            output = f.getvalue()
            payload = json_module.loads(output)
            assert "project" in payload
            assert "agents" in payload
            assert "orchestrator" in payload


# ---------------------------------------------------------------------------
# cmd_health tests
# ---------------------------------------------------------------------------

class TestCmdHealth:
    def test_cmd_health_exits_zero_no_exceptions(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            state_dir = project_root / ".codebot" / "state"
            state_dir.mkdir(parents=True)
            logs_dir = project_root / ".codebot" / "logs"
            logs_dir.mkdir(parents=True)
            
            import io
            from contextlib import redirect_stdout
            
            f = io.StringIO()
            with redirect_stdout(f):
                exit_code = cmd_health(project_root, json_out=False)
            
            assert exit_code == 0
            output = f.getvalue()
            assert "CodeBot Diagnostics" in output
            assert "Orchestrator" in output
            assert "Agents" in output
            assert "Tickets" in output

    def test_cmd_health_json_output(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            project_root = Path(tmpdir)
            state_dir = project_root / ".codebot" / "state"
            state_dir.mkdir(parents=True)
            logs_dir = project_root / ".codebot" / "logs"
            logs_dir.mkdir(parents=True)
            
            import io
            from contextlib import redirect_stdout
            import json as json_module
            
            f = io.StringIO()
            with redirect_stdout(f):
                exit_code = cmd_health(project_root, json_out=True)
            
            assert exit_code == 0
            output = f.getvalue()
            payload = json_module.loads(output)
            assert "project" in payload
            assert "orchestrator" in payload
            assert "agents_summary" in payload
            assert "tickets" in payload