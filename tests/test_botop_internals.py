#!/usr/bin/env python3
"""Tests for codebot/botop.py internal helpers.

Covers:
- _parse_checkpoint_text (JSON, Python dict, trailing garbage, empty)
- _collect_agents (bucket classification, heartbeat age)
- _collect_ticket_throughput (rework_rate, avg_age_h, oldest_h)
- cmd_status (exit 0, output format)
- cmd_health (aggregation without exceptions)
"""

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch

import pytest

# Import internals from botop
sys.path.insert(0, str(Path(__file__).parent.parent / "codebot"))
from botop import (
    _parse_checkpoint_text,
    _collect_agents,
    _collect_ticket_throughput,
    _implementation_claims,
    cmd_status,
    cmd_health,
    _find_state_dir,
    _find_logs_dir,
)


# ---------------------------------------------------------------------------
# _parse_checkpoint_text tests
# ---------------------------------------------------------------------------

def test_parse_checkpoint_valid_json():
    raw = '{"processed_ids": ["CB-123"], "updated_at": 1716120000.0}'
    result = _parse_checkpoint_text(raw)
    assert result is not None
    assert result["processed_ids"] == ["CB-123"]
    assert result["updated_at"] == 1716120000.0


def test_parse_checkpoint_python_dict():
    raw = "{'processed_ids': ['CB-456'], 'updated_at': 1716120100.0}"
    result = _parse_checkpoint_text(raw)
    assert result is not None
    assert result["processed_ids"] == ["CB-456"]


def test_parse_checkpoint_trailing_garbage():
    raw = '{"key": "value"} extra garbage text'
    result = _parse_checkpoint_text(raw)
    assert result is not None
    assert result["key"] == "value"


def test_parse_checkpoint_empty_input():
    assert _parse_checkpoint_text("") is None
    assert _parse_checkpoint_text("   ") is None
    assert _parse_checkpoint_text("\n\t") is None


def test_parse_checkpoint_invalid_no_braces():
    raw = "not a json or dict at all"
    assert _parse_checkpoint_text(raw) is None


def test_parse_checkpoint_nested_json():
    raw = '{"outer": {"inner": {"deep": "value"}}, "list": [1, 2, 3]}'
    result = _parse_checkpoint_text(raw)
    assert result is not None
    assert result["outer"]["inner"]["deep"] == "value"
    assert result["list"] == [1, 2, 3]


# ---------------------------------------------------------------------------
# _collect_agents tests
# ---------------------------------------------------------------------------

def test_collect_agents_empty_state_dir(tmp_path):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=tmp_path / "logs"):
            agents = _collect_agents(tmp_path)
            assert agents == []


def test_collect_agents_bucket_classification_running(tmp_path):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    now = time.time()
    # Create heartbeat file with recent timestamp (<120s)
    hb_path = state_dir / "test_agent.heartbeat"
    hb_path.write_text(str(now - 30))  # 30 seconds ago
    
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._find_all_api_pids', return_value={}):
                agents = _collect_agents(tmp_path)
                assert len(agents) == 1
                assert agents[0]["name"] == "test_agent"
                assert agents[0]["bucket"] == "RUNNING"
                assert agents[0]["hb_age"] < 120


def test_collect_agents_bucket_classification_stale(tmp_path):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    now = time.time()
    # Create heartbeat file with stale timestamp (120s <= age < 600s)
    hb_path = state_dir / "stale_agent.heartbeat"
    hb_path.write_text(str(now - 300))  # 5 minutes ago
    
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._find_all_api_pids', return_value={}):
                agents = _collect_agents(tmp_path)
                assert len(agents) == 1
                assert agents[0]["name"] == "stale_agent"
                assert agents[0]["bucket"] == "STALE"


def test_collect_agents_bucket_classification_dead(tmp_path):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    now = time.time()
    # Create heartbeat file with dead timestamp (>=600s)
    hb_path = state_dir / "dead_agent.heartbeat"
    hb_path.write_text(str(now - 700))  # ~12 minutes ago
    
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._find_all_api_pids', return_value={}):
                agents = _collect_agents(tmp_path)
                assert len(agents) == 1
                assert agents[0]["name"] == "dead_agent"
                assert agents[0]["bucket"] == "DEAD"


def test_collect_agents_paused_bucket(tmp_path):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    now = time.time()
    # Create heartbeat and paused file
    hb_path = state_dir / "paused_agent.heartbeat"
    hb_path.write_text(str(now - 30))  # Recent heartbeat
    paused_path = state_dir / "paused_agent.paused"
    paused_path.write_text(str(now))
    
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._find_all_api_pids', return_value={}):
                agents = _collect_agents(tmp_path)
                assert len(agents) == 1
                assert agents[0]["name"] == "paused_agent"
                assert agents[0]["bucket"] == "PAUSED"
                assert agents[0]["paused"] is True


# ---------------------------------------------------------------------------
# _collect_ticket_throughput tests
# ---------------------------------------------------------------------------

def test_collect_ticket_throughput_empty():
    result = _collect_ticket_throughput([])
    assert result["total"] == 0
    assert result["avg_age_h"] is None
    assert result["oldest_h"] is None
    assert result["rework_rate"] == 0


def test_collect_ticket_throughput_single_ticket():
    now = time.time()
    tickets = [
        {
            "id": "CB-001",
            "state": "IMPLEMENTING",
            "severity": "medium",
            "ticket_class": "bug",
            "created_at": now - 3600,  # 1 hour ago
            "rework_count": 0,
        }
    ]
    result = _collect_ticket_throughput(tickets)
    assert result["total"] == 1
    assert result["avg_age_h"] == 1.0
    assert result["oldest_h"] == 1.0
    assert result["rework_rate"] == 0.0


def test_collect_ticket_throughput_rework_rate():
    now = time.time()
    tickets = [
        {"id": "CB-001", "state": "COMPLETE", "created_at": now - 7200, "rework_count": 0},
        {"id": "CB-002", "state": "COMPLETE", "created_at": now - 7200, "rework_count": 1},
        {"id": "CB-003", "state": "COMPLETE", "created_at": now - 7200, "rework_count": 2},
        {"id": "CB-004", "state": "COMPLETE", "created_at": now - 7200, "rework_count": 0},
    ]
    result = _collect_ticket_throughput(tickets)
    assert result["total"] == 4
    # 2 out of 4 have rework_count > 0
    assert result["rework_rate"] == 0.5


def test_collect_ticket_throughput_avg_age_multiple():
    now = time.time()
    tickets = [
        {"id": "CB-001", "state": "READY", "created_at": now - 3600},  # 1h
        {"id": "CB-002", "state": "READY", "created_at": now - 7200},  # 2h
    ]
    result = _collect_ticket_throughput(tickets)
    assert result["total"] == 2
    assert result["avg_age_h"] == 1.5
    assert result["oldest_h"] == 2.0
    assert result["newest_h"] == 1.0


def test_collect_ticket_throughput_throughput_24h_7d():
    now = time.time()
    tickets = [
        {"id": "CB-001", "state": "DISCOVERED", "created_at": now - 3600},      # 1h ago (24h)
        {"id": "CB-002", "state": "DISCOVERED", "created_at": now - 86400},     # 24h ago (not in 24h, in 7d)
        {"id": "CB-003", "state": "DISCOVERED", "created_at": now - 604800},    # 7d ago (not in 7d)
        {"id": "CB-004", "state": "DISCOVERED", "created_at": now - 100},       # 100s ago (24h)
    ]
    result = _collect_ticket_throughput(tickets)
    # CB-001 and CB-004 are within 24h
    assert result["throughput_24h"] == 2
    # CB-001, CB-002, CB-004 are within 7d (CB-003 is exactly 7d, boundary case)
    assert result["throughput_7d"] >= 2


def test_implementation_claims_excludes_non_implementers():
    agents = [{"name": "backend_implementer-2", "current_task": "tool:write executing"}]
    claims = [
        {"worker": "backend_implementer-2", "ticket_id": "CB-IMPLEMENT"},
        {"worker": "implementation_planner", "ticket_id": "CB-PLAN"},
        {"worker": "security_reviewer", "ticket_id": "CB-REVIEW"},
    ]

    assert _implementation_claims(agents, claims) == [{
        "ticket_id": "CB-IMPLEMENT",
        "worker": "backend_implementer-2",
        "task": "tool:write executing",
    }]


# ---------------------------------------------------------------------------
# cmd_status tests
# ---------------------------------------------------------------------------

def test_cmd_status_exit_code_zero(tmp_path, capsys):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._collect_orchestrator_info', return_value={"pid": None, "alive": False}):
                exit_code = cmd_status(tmp_path, verbose=False, json_out=False)
                assert exit_code == 0


def test_cmd_status_json_output(tmp_path, capsys):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._collect_orchestrator_info', return_value={"pid": None, "alive": False}):
                exit_code = cmd_status(tmp_path, verbose=False, json_out=True)
                assert exit_code == 0
                captured = capsys.readouterr()
                output = json.loads(captured.out)
                assert "project" in output
                assert "state_dir" in output
                assert "agents" in output


# ---------------------------------------------------------------------------
# cmd_health tests
# ---------------------------------------------------------------------------

def test_cmd_health_no_exceptions(tmp_path, capsys):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._collect_orchestrator_info', return_value={"pid": None, "alive": False}):
                with patch('codebot.botop._collect_tickets', return_value=(None, None, [])):
                    with patch('codebot.botop._collect_claims', return_value=[]):
                        with patch('codebot.botop._collect_rl', return_value=None):
                            with patch('codebot.botop._collect_token_ledger', return_value=None):
                                with patch('codebot.botop._collect_leases', return_value=None):
                                    with patch('codebot.botop._collect_bot_metrics', return_value=None):
                                        with patch('codebot.botop._collect_gatekeeper_state', return_value=[]):
                                            with patch('codebot.botop._collect_events_state', return_value=[]):
                                                with patch('codebot.botop._collect_findings_state', return_value=[]):
                                                    with patch('codebot.botop._collect_anomalies', return_value=[]):
                                                        exit_code = cmd_health(tmp_path, json_out=False)
                                                        assert exit_code == 0


def test_cmd_health_json_output(tmp_path, capsys):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._collect_orchestrator_info', return_value={"pid": None, "alive": False}):
                with patch('codebot.botop._collect_tickets', return_value=(None, None, [])):
                    with patch('codebot.botop._collect_claims', return_value=[]):
                        with patch('codebot.botop._collect_rl', return_value=None):
                            with patch('codebot.botop._collect_token_ledger', return_value=None):
                                with patch('codebot.botop._collect_leases', return_value=None):
                                    with patch('codebot.botop._collect_bot_metrics', return_value=None):
                                        with patch('codebot.botop._collect_gatekeeper_state', return_value=[]):
                                            with patch('codebot.botop._collect_events_state', return_value=[]):
                                                with patch('codebot.botop._collect_findings_state', return_value=[]):
                                                    with patch('codebot.botop._collect_anomalies', return_value=[]):
                                                        exit_code = cmd_health(tmp_path, json_out=True)
                                                        assert exit_code == 0
                                                        captured = capsys.readouterr()
                                                        output = json.loads(captured.out)
                                                        assert "project" in output
                                                        assert "orchestrator" in output


# ---------------------------------------------------------------------------
# Drain status text marker tests (CB-5100583-4313)
# ---------------------------------------------------------------------------

def test_drain_status_text_markers_in_source():
    """Verify drain status strings include explicit [DRAIN] and [OK] markers."""
    botop_path = Path(__file__).parent.parent / "codebot" / "botop.py"
    content = botop_path.read_text()
    
    # Check drain_txt location uses [DRAIN] and [OK] markers
    assert '[DRAIN] ACTIVE' in content, "drain_txt should contain '[DRAIN] ACTIVE' marker"
    assert '[OK] clear' in content, "drain_txt should contain '[OK] clear' marker"
    
    # Check drain_badge location uses [DRAIN] and [OK] markers
    assert '[DRAIN]' in content, "drain_badge should contain '[DRAIN]' marker"
    assert '[OK]' in content, "drain_badge should contain '[OK]' marker"


def test_drain_status_readable_without_color(tmp_path, capsys):
    """Verify drain status is readable with NO_COLOR=1 (ticket CB-5100583-4313)."""
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    # Test with drain active
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._collect_orchestrator_info', return_value={"pid": 123, "alive": True, "drain": True}):
                with patch('codebot.botop._collect_agents', return_value=[]):
                    with patch.dict('os.environ', {'NO_COLOR': '1'}):
                        exit_code = cmd_status(tmp_path, verbose=False, json_out=False)
                        assert exit_code == 0
                        captured = capsys.readouterr()
                        # Verify [DRAIN] marker appears in output
                        assert '[DRAIN]' in captured.out, f"Expected '[DRAIN]' marker in output: {captured.out}"
                        assert 'ACTIVE' in captured.out, f"Expected 'ACTIVE' in output: {captured.out}"


def test_drain_status_clear_readable_without_color(tmp_path, capsys):
    """Verify drain clear status is readable with NO_COLOR=1 (ticket CB-5100583-4313)."""
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    
    # Test with drain clear
    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._collect_orchestrator_info', return_value={"pid": 123, "alive": True, "drain": False}):
                with patch('codebot.botop._collect_agents', return_value=[]):
                    with patch.dict('os.environ', {'NO_COLOR': '1'}):
                        exit_code = cmd_status(tmp_path, verbose=False, json_out=False)
                        assert exit_code == 0
                        captured = capsys.readouterr()
                        # Verify [OK] marker appears in output
                        assert '[OK]' in captured.out, f"Expected '[OK]' marker in output: {captured.out}"
                        assert 'clear' in captured.out, f"Expected 'clear' in output: {captured.out}"
