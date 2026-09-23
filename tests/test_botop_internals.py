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
import codebot.botop as _codebot_botop
import sys as _sys
_sys.modules["botop"] = _codebot_botop

# Import internals via canonical path so patch("codebot.botop.*") hits the same objects
from codebot.botop import (
    _parse_checkpoint_text,
    _collect_agents,
    _collect_ticket_throughput,
    _implementation_claims,
    cmd_status,
    cmd_health,
    _collect_orchestrator_info,
    _find_state_dir,
    _find_logs_dir,
    _tasklog_tail_line_count,
    _TASKLOG_TAIL_BYTES,
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


def test_collect_orchestrator_info_reads_hidden_pid_file(tmp_path):
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    (state_dir / ".orchestrator.pid").write_text(str(os.getpid()), encoding="utf-8")

    info = _collect_orchestrator_info(tmp_path)

    assert info["pid"] == os.getpid()
    assert info["alive"] is True


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
    hb_path = state_dir / "paused_agent.heartbeat"
    hb_path.write_text(str(now - 30))
    paused_path = state_dir / "paused_agent.paused"
    paused_path.write_text(str(now))

    with patch('codebot.botop._find_state_dir', return_value=state_dir):
        with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
            with patch('codebot.botop._find_all_api_pids', return_value={}):
                with patch('codebot.botop._find_agent_pid', side_effect=AssertionError("no per-agent scan")):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    assert agents[0]["name"] == "paused_agent"
                    assert agents[0]["bucket"] == "PAUSED"
                    assert agents[0]["paused"] is True


def test_collect_agents_uses_bulk_pid_scan_only(tmp_path):
    from codebot import botop as botop_mod
    now = time.time()
    state_dir = tmp_path / ".codebot" / "state"
    state_dir.mkdir(parents=True)
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    for i in range(5):
        (state_dir / f"dead_agent_{i}.heartbeat").write_text(str(now - 3600))

    with patch.object(botop_mod, '_find_state_dir', return_value=state_dir):
        with patch.object(botop_mod, '_find_logs_dir', return_value=logs_dir):
            with patch.object(botop_mod, '_find_all_api_pids', return_value={"dead_agent_0": 1234}) as bulk:
                with patch.object(botop_mod, '_find_agent_pid', side_effect=AssertionError("per-agent fallback")) as single:
                    agents = botop_mod._collect_agents(tmp_path)
                    assert len(agents) == 5
                    assert bulk.call_count == 1
                    assert single.call_count == 0
                    by_name = {a["name"]: a for a in agents}
                    assert by_name["dead_agent_0"]["pid"] == 1234
                    assert by_name["dead_agent_0"]["running"] is True
                    for i in range(1, 5):
                        assert by_name[f"dead_agent_{i}"]["pid"] is None
                        assert by_name[f"dead_agent_{i}"]["running"] is False


def test_find_all_api_pids_parses_all_spawn_forms():
    from codebot import botop as botop_mod
    ps_output = (
        "  1001 /usr/bin/python3 -m codebot.api_runner decomposer-2 model /hb /ckpt /mission fb 0 \n"
        "  1002 /usr/bin/python3 -m codebot.api_runner --bot decomposer-3 --prompt roles/decomposer.md\n"
        "  1003 /usr/bin/python3 /repo/bots/api_runner.py reviewer-1 --bot reviewer-1\n"
    )
    with patch.object(botop_mod.subprocess, 'check_output', return_value=ps_output):
        pid_map = botop_mod._find_all_api_pids()
    assert pid_map == {"decomposer-2": 1001, "decomposer-3": 1002, "reviewer-1": 1003}


def test_find_agent_pid_matches_exact_name_only():
    from codebot import botop as botop_mod
    ps_output = "  2001 /usr/bin/python3 -m codebot.api_runner decomposer-25 model\n"
    with patch.object(botop_mod.subprocess, 'run') as run_mock:
        run_mock.return_value.stdout = ""
        run_mock.return_value.returncode = 1
        with patch.object(botop_mod.subprocess, 'check_output', return_value=ps_output):
            assert botop_mod._find_agent_pid("decomposer-2") is None


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


# ---------------------------------------------------------------------------
# _tasklog_tail_line_count tests
# ---------------------------------------------------------------------------

class TestTasklogTailLineCount:
    """Direct unit tests for _tasklog_tail_line_count covering all branches."""

    def test_tail_line_count_large_file(self, tmp_path):
        """Large file where offset > 0 and data.count(b'\\n') runs on tail window."""
        tasklog = tmp_path / "agent.tasklog"
        # Create a file larger than _TASKLOG_TAIL_BYTES (65536)
        # Write 128KB of data with known newline count in the tail
        line = b"x" * 63 + b"\n"  # 64 bytes per line
        num_lines_total = 3000  # ~192KB total
        num_lines_in_tail = 1000  # last 64KB should contain ~1000 lines
        
        with open(tasklog, "wb") as f:
            for _ in range(num_lines_total):
                f.write(line)
        
        count = _tasklog_tail_line_count(tasklog)
        assert count is not None
        # The tail window is 65536 bytes. Each line is 64 bytes.
        # 65536 / 64 = 1024 lines approximately in the tail window
        assert count == 1024, f"Expected 1024 newlines in tail, got {count}"

    def test_tail_line_count_small_file(self, tmp_path):
        """Small file where offset = 0 and full file is read."""
        tasklog = tmp_path / "agent.tasklog"
        content = b"line1\nline2\nline3\n"
        tasklog.write_bytes(content)
        
        count = _tasklog_tail_line_count(tasklog)
        assert count == 3, f"Expected 3 newlines, got {count}"

    def test_tail_line_count_empty_file(self, tmp_path):
        """Zero-byte file returns 0."""
        tasklog = tmp_path / "agent.tasklog"
        tasklog.write_bytes(b"")
        
        count = _tasklog_tail_line_count(tasklog)
        assert count == 0, f"Expected 0 newlines for empty file, got {count}"

    def test_tail_line_count_missing_file(self, tmp_path):
        """Non-existent file triggers except clause, returns None."""
        nonexistent = tmp_path / "nonexistent_dir" / "tasklog.txt"
        
        count = _tasklog_tail_line_count(nonexistent)
        assert count is None, f"Expected None for missing file, got {count}"
