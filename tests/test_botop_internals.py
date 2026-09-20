"""Tests for codebot/botop.py internal helpers.

Covers:
- _parse_checkpoint_text: valid JSON, Python dict, trailing garbage, empty input, brace-balanced extraction
- _collect_agents: bucket classification with mock state directory
- _collect_ticket_throughput: rework_rate, avg_age_h, oldest_h computations
- cmd_status: exits 0 with expected output format
- cmd_health: aggregates all subsystems without exceptions
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot.botop import (
    _collect_agents,
    _collect_ticket_throughput,
    _parse_checkpoint_text,
    cmd_health,
    cmd_status,
)


# ---------------------------------------------------------------------------
# _parse_checkpoint_text
# ---------------------------------------------------------------------------

class TestParseCheckpointText:
    """At least 5 input variants per acceptance criteria."""

    def test_valid_json_dict(self):
        raw = '{"bot": "test", "updated_at": 1700000000.0}'
        result = _parse_checkpoint_text(raw)
        assert isinstance(result, dict)
        assert result["bot"] == "test"
        assert result["updated_at"] == 1700000000.0

    def test_python_single_quoted_dict(self):
        raw = "{'bot': 'agent1', 'reason': 'working'}"
        result = _parse_checkpoint_text(raw)
        assert isinstance(result, dict)
        assert result["bot"] == "agent1"
        assert result["reason"] == "working"

    def test_trailing_garbage_after_json(self):
        raw = '{"key": "value"} some trailing garbage text'
        result = _parse_checkpoint_text(raw)
        assert isinstance(result, dict)
        assert result["key"] == "value"

    def test_empty_input_returns_none(self):
        assert _parse_checkpoint_text("") is None
        assert _parse_checkpoint_text("   ") is None
        assert _parse_checkpoint_text("\n\t") is None

    def test_brace_balanced_extraction_from_noise(self):
        raw = 'noise before {"extracted": true} noise after'
        result = _parse_checkpoint_text(raw)
        assert isinstance(result, dict)
        assert result["extracted"] is True

    def test_invalid_returns_none(self):
        assert _parse_checkpoint_text("not a dict at all") is None
        assert _parse_checkpoint_text("[1,2,3]") is None  # list, not dict

    def test_null_byte_stripped_python_dict(self):
        raw = "\x00{'clean': True}\x00"
        result = _parse_checkpoint_text(raw)
        assert isinstance(result, dict)
        assert result["clean"] is True


# ---------------------------------------------------------------------------
# _collect_agents — bucket classification
# ---------------------------------------------------------------------------

class TestCollectAgents:
    """Verify correct bucket classifications with mock state directory."""

    def _setup_state_dir(self, tmp_path: Path) -> Path:
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / ".codebot" / "logs"
        logs_dir.mkdir(parents=True)
        return state_dir

    @patch("codebot.botop._find_all_api_pids", return_value={})
    @patch("codebot.botop._find_agent_pid", return_value=None)
    def test_running_bucket_fresh_heartbeat(self, mock_pid, mock_pids, tmp_path):
        state_dir = self._setup_state_dir(tmp_path)
        now = time.time()
        (state_dir / "agent_alpha.heartbeat").write_text(str(now))

        agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["name"] == "agent_alpha"
        assert agents[0]["bucket"] == "RUNNING"

    @patch("codebot.botop._find_all_api_pids", return_value={})
    @patch("codebot.botop._find_agent_pid", return_value=None)
    def test_stale_bucket_old_heartbeat(self, mock_pid, mock_pids, tmp_path):
        state_dir = self._setup_state_dir(tmp_path)
        old_ts = time.time() - 300  # 5 min ago → STALE (120 < age < 600)
        (state_dir / "agent_beta.heartbeat").write_text(str(old_ts))

        agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "STALE"

    @patch("codebot.botop._find_all_api_pids", return_value={})
    @patch("codebot.botop._find_agent_pid", return_value=None)
    def test_dead_bucket_very_old_heartbeat(self, mock_pid, mock_pids, tmp_path):
        state_dir = self._setup_state_dir(tmp_path)
        old_ts = time.time() - 900  # 15 min ago → DEAD (>600)
        (state_dir / "agent_gamma.heartbeat").write_text(str(old_ts))

        agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "DEAD"

    @patch("codebot.botop._find_all_api_pids", return_value={})
    @patch("codebot.botop._find_agent_pid", return_value=None)
    def test_paused_bucket(self, mock_pid, mock_pids, tmp_path):
        state_dir = self._setup_state_dir(tmp_path)
        now = time.time()
        (state_dir / "agent_delta.heartbeat").write_text(str(now))
        (state_dir / "agent_delta.paused").write_text(str(now))

        agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "PAUSED"

    @patch("codebot.botop._find_all_api_pids", return_value={})
    @patch("codebot.botop._find_agent_pid", return_value=None)
    def test_unknown_bucket_no_heartbeat(self, mock_pid, mock_pids, tmp_path):
        state_dir = self._setup_state_dir(tmp_path)
        # Only status file, no heartbeat
        (state_dir / "agent_epsilon.status.json").write_text(json.dumps({}))

        agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "UNKNOWN"

    @patch("codebot.botop._find_all_api_pids", return_value={})
    @patch("codebot.botop._find_agent_pid", return_value=None)
    def test_no_agents_returns_empty_list(self, mock_pid, mock_pids, tmp_path):
        self._setup_state_dir(tmp_path)
        agents = _collect_agents(tmp_path)
        assert agents == []


# ---------------------------------------------------------------------------
# _collect_ticket_throughput
# ---------------------------------------------------------------------------

class TestCollectTicketThroughput:
    """Verify correct averages and rework rate computation."""

    def test_empty_tickets(self):
        result = _collect_ticket_throughput([])
        assert result["total"] == 0
        assert result["avg_age_h"] is None
        assert result["oldest_h"] is None
        assert result["rework_rate"] == 0

    def test_rework_rate_computation(self):
        now = time.time()
        tickets = [
            {"state": "COMPLETE", "severity": "medium", "ticket_class": "bug",
             "created_at": now - 3600, "rework_count": 0},
            {"state": "REWORK", "severity": "high", "ticket_class": "bug",
             "created_at": now - 7200, "rework_count": 2},
            {"state": "IMPLEMENTING", "severity": "low", "ticket_class": "feature",
             "created_at": now - 1800, "rework_count": 0},
            {"state": "COMPLETE", "severity": "medium", "ticket_class": "test",
             "created_at": now - 900, "rework_count": 1},
        ]
        result = _collect_ticket_throughput(tickets)
        assert result["total"] == 4
        # 2 out of 4 have rework_count > 0
        assert result["rework_rate"] == 0.5

    def test_avg_age_and_oldest(self):
        now = time.time()
        tickets = [
            {"state": "A", "severity": "low", "ticket_class": "x",
             "created_at": now - 3600, "rework_count": 0},   # 1h
            {"state": "B", "severity": "low", "ticket_class": "x",
             "created_at": now - 7200, "rework_count": 0},   # 2h
            {"state": "C", "severity": "low", "ticket_class": "x",
             "created_at": now - 1800, "rework_count": 0},   # 0.5h
        ]
        result = _collect_ticket_throughput(tickets)
        # avg = (1 + 2 + 0.5) / 3 = 1.166... hours
        assert result["avg_age_h"] is not None
        assert abs(result["avg_age_h"] - 1.17) < 0.05
        # oldest = 2h
        assert result["oldest_h"] is not None
        assert abs(result["oldest_h"] - 2.0) < 0.05
        # newest = 0.5h
        assert result["newest_h"] is not None
        assert abs(result["newest_h"] - 0.5) < 0.05

    def test_by_state_severity_class_counts(self):
        now = time.time()
        tickets = [
            {"state": "READY", "severity": "critical", "ticket_class": "bug", "created_at": now, "rework_count": 0},
            {"state": "READY", "severity": "high", "ticket_class": "bug", "created_at": now, "rework_count": 0},
            {"state": "DONE", "severity": "low", "ticket_class": "feature", "created_at": now, "rework_count": 0},
        ]
        result = _collect_ticket_throughput(tickets)
        assert result["by_state"]["READY"] == 2
        assert result["by_state"]["DONE"] == 1
        assert result["by_severity"]["critical"] == 1
        assert result["by_class"]["bug"] == 2
        assert result["by_class"]["feature"] == 1


# ---------------------------------------------------------------------------
# cmd_status — exits 0 with expected output
# ---------------------------------------------------------------------------

class TestCmdStatus:
    def test_exits_zero_with_no_agents(self, tmp_path, capsys):
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / ".codebot" / "logs"
        logs_dir.mkdir(parents=True)

        rc = cmd_status(tmp_path)
        assert rc == 0
        captured = capsys.readouterr()
        assert "CodeBot status" in captured.out

    def test_json_output_mode(self, tmp_path, capsys):
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / ".codebot" / "logs"
        logs_dir.mkdir(parents=True)

        rc = cmd_status(tmp_path, json_out=True)
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "agents" in data
        assert "orchestrator" in data
        assert "timestamp" in data


# ---------------------------------------------------------------------------
# cmd_health — aggregates without exceptions
# ---------------------------------------------------------------------------

class TestCmdHealth:
    def test_exits_zero_no_exceptions(self, tmp_path, capsys):
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / ".codebot" / "logs"
        logs_dir.mkdir(parents=True)

        rc = cmd_health(tmp_path)
        assert rc == 0
        captured = capsys.readouterr()
        assert "Diagnostics" in captured.out

    def test_json_output_mode(self, tmp_path, capsys):
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / ".codebot" / "logs"
        logs_dir.mkdir(parents=True)

        rc = cmd_health(tmp_path, json_out=True)
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "orchestrator" in data
        assert "agents_summary" in data
        assert "tickets" in data
