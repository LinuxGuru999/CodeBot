#!/usr/bin/env python3
"""Performance tests for botop._collect_agents (CB-7249E).

Acceptance Criteria:
  - No per-agent subprocess spawns in _collect_agents
  - tasklog reads bounded to <100KB per file
  - status check completes in <500ms with 26 agents and 1MB tasklogs
"""

import os
import sys
import time
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Add project root to path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from codebot.botop import (
    _collect_agents,
    _find_all_api_pids,
    _tasklog_tail_line_count,
    _read_tasklog_bounded,
    _TASKLOG_TAIL_BYTES,
)


class TestBatchPidLookup:
    """Test that _collect_agents uses batch PID lookup, not per-agent spawns."""

    def test_no_per_agent_subprocess_spawns(self, tmp_path):
        """Verify _collect_agents does NOT call _find_agent_pid per agent."""
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        # Create 26 agent heartbeat files
        num_agents = 26
        for i in range(num_agents):
            agent_name = f"agent_{i:02d}"
            (state_dir / f"{agent_name}.heartbeat").write_text(str(time.time()))
            # Create small tasklog
            (logs_dir / f"{agent_name}.tasklog").write_text("ok\n")

        # Mock _find_all_api_pids to return empty map (no running agents)
        with patch("codebot.botop._find_all_api_pids", return_value={}) as mock_batch:
            with patch("codebot.botop.subprocess.run") as mock_run:
                with patch("codebot.botop.subprocess.check_output") as mock_check:
                    # Ensure no pgrep/ps calls happen inside _collect_agents loop
                    mock_run.return_value = MagicMock(stdout="", stderr="", returncode=1)
                    mock_check.return_value = ""

                    agents = _collect_agents(tmp_path)

                    # Verify batch lookup was called exactly once
                    assert mock_batch.call_count == 1, "_find_all_api_pids should be called once"

                    # Verify NO per-agent subprocess calls occurred
                    # _find_agent_pid would call subprocess.run or check_output per agent
                    assert mock_run.call_count == 0, "No per-agent subprocess.run calls expected"
                    assert mock_check.call_count == 0, "No per-agent subprocess.check_output calls expected"

                    assert len(agents) == num_agents

    def test_pid_map_lookup_used(self, tmp_path):
        """Verify agents get PID from pid_map.get(name), not individual lookup."""
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        agent_name = "test_agent"
        (state_dir / f"{agent_name}.heartbeat").write_text(str(time.time()))
        (logs_dir / f"{agent_name}.tasklog").write_text("ok\n")

        expected_pid = 12345
        pid_map = {agent_name: expected_pid}

        with patch("codebot.botop._find_all_api_pids", return_value=pid_map):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["name"] == agent_name
        assert agents[0]["pid"] == expected_pid
        assert agents[0]["running"] is True


class TestBoundedTasklogReads:
    """Test that tasklog reads are bounded to <100KB."""

    def test_tasklog_tail_line_count_bounded(self, tmp_path):
        """Verify _tasklog_tail_line_count uses 64KB binary tail-read."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True)
        tasklog_path = logs_dir / "agent.tasklog"

        # Create a 2MB tasklog file
        large_content = "x" * (2 * 1024 * 1024) + "\nend\n"
        tasklog_path.write_text(large_content)

        # Should complete quickly and not read entire file
        start = time.time()
        line_count = _tasklog_tail_line_count(tasklog_path)
        elapsed = time.time() - start

        assert line_count is not None
        assert elapsed < 0.1, "Bounded read should be fast"

    def test_read_tasklog_bounded_caps_at_64kb(self, tmp_path):
        """Verify _read_tasklog_bounded reads at most 64KB."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True)
        tasklog_path = logs_dir / "agent.tasklog"

        # Create content larger than 64KB
        prefix = "A" * 70000  # 70KB of 'A'
        suffix = "END_MARKER"
        full_content = prefix + suffix
        tasklog_path.write_text(full_content)

        result = _read_tasklog_bounded(tasklog_path, max_bytes=65536)

        assert result is not None
        # Should contain the end marker but not the full prefix
        assert "END_MARKER" in result
        assert len(result.encode("utf-8")) <= 65536 + 100  # Small margin for decode
        # The beginning of result should be truncated (not start with many 'A's)
        assert len(result) < len(full_content)

    def test_read_tasklog_bounded_small_file(self, tmp_path):
        """Verify bounded reader works for small files."""
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir(parents=True)
        tasklog_path = logs_dir / "agent.tasklog"

        tasklog_path.write_text("line1\nline2\n")

        result = _read_tasklog_bounded(tasklog_path)
        assert result == "line1\nline2\n"

    def test_read_tasklog_bounded_missing_file(self, tmp_path):
        """Verify bounded reader returns None for missing file."""
        result = _read_tasklog_bounded(tmp_path / "nonexistent.tasklog")
        assert result is None


class TestCollectAgentsPerformance:
    """Integration performance tests for _collect_agents."""

    def test_status_check_under_500ms_with_26_agents_and_1mb_tasklogs(self, tmp_path):
        """Acceptance criterion: status check <500ms with 26 agents and 1MB tasklogs."""
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        num_agents = 26
        tasklog_size = 1024 * 1024  # 1MB

        # Create 26 agents with 1MB tasklogs each
        for i in range(num_agents):
            agent_name = f"agent_{i:02d}"
            (state_dir / f"{agent_name}.heartbeat").write_text(str(time.time()))
            # Write 1MB tasklog efficiently
            tasklog_path = logs_dir / f"{agent_name}.tasklog"
            with open(tasklog_path, "w") as f:
                # Write in chunks to avoid memory issues
                chunk = "x" * 65536
                for _ in range(16):  # 16 * 64KB = 1MB
                    f.write(chunk)
                f.write("\nend\n")

        # Mock PID lookup to avoid actual process scanning
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            with patch("codebot.botop.subprocess.run") as mock_run:
                with patch("codebot.botop.subprocess.check_output") as mock_check:
                    mock_run.return_value = MagicMock(stdout="", stderr="", returncode=1)
                    mock_check.return_value = ""

                    start = time.time()
                    agents = _collect_agents(tmp_path)
                    elapsed_ms = (time.time() - start) * 1000

                    assert len(agents) == num_agents
                    assert elapsed_ms < 500, f"Expected <500ms, got {elapsed_ms:.0f}ms"
                    print(f"Performance OK: {num_agents} agents with 1MB tasklogs in {elapsed_ms:.0f}ms")


class TestTasklogTailLineCountExhaustive:
    def test_empty_file_returns_zero(self, tmp_path):
        p = tmp_path / 'empty.tasklog'
        p.write_bytes(b'')
        assert _tasklog_tail_line_count(p) == 0

    def test_small_file_counts_all_newlines(self, tmp_path):
        p = tmp_path / 'small.tasklog'
        p.write_bytes(b'alpha\nbeta\ngamma')
        assert _tasklog_tail_line_count(p) == 2

    def test_exact_window_reads_from_start(self, tmp_path):
        p = tmp_path / 'exact.tasklog'
        data = b'\n' + b'x' * (_TASKLOG_TAIL_BYTES - 1)
        assert len(data) == _TASKLOG_TAIL_BYTES
        p.write_bytes(data)
        assert _tasklog_tail_line_count(p) == 1

    def test_one_byte_over_window_excludes_first_byte(self, tmp_path):
        p = tmp_path / 'over.tasklog'
        data = b'\n' + b'x' * _TASKLOG_TAIL_BYTES
        assert len(data) == _TASKLOG_TAIL_BYTES + 1
        p.write_bytes(data)
        assert _tasklog_tail_line_count(p) == 0

    def test_boundary_newline_at_offset_is_included(self, tmp_path):
        p = tmp_path / 'boundary.tasklog'
        data = b'\n' + b'\n' + b'x' * (_TASKLOG_TAIL_BYTES - 1)
        assert len(data) == _TASKLOG_TAIL_BYTES + 1
        p.write_bytes(data)
        assert _tasklog_tail_line_count(p) == 1

    def test_counts_raw_byte_newlines_without_decoding(self, tmp_path):
        p = tmp_path / 'binary.tasklog'
        p.write_bytes(b'\xff\xfe\nabc\n\x00')
        assert _tasklog_tail_line_count(p) == 2

    def test_missing_file_returns_none(self, tmp_path):
        assert _tasklog_tail_line_count(tmp_path / 'missing.tasklog') is None

    def test_unreadable_path_returns_none(self, tmp_path):
        d = tmp_path / 'directory.tasklog'
        d.mkdir()
        assert _tasklog_tail_line_count(d) is None


class TestCollectAgentsBoundedTailPaths:
    def test_collect_agents_counts_only_tail_window(self, tmp_path):
        state_dir = tmp_path / '.codebot' / 'state'
        logs_dir = tmp_path / '.codebot' / 'logs'
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = 'tail-agent'
        (state_dir / f'{name}.heartbeat').write_text(str(time.time()))

        tail = b'a\nb\nc\n'
        excluded = b'\n' * 10 + b'x' * (1000 - 10)
        filler = b'x' * (_TASKLOG_TAIL_BYTES - len(tail))
        data = excluded + filler + tail
        assert len(data) == _TASKLOG_TAIL_BYTES + 1000
        (logs_dir / f'{name}.tasklog').write_bytes(data)

        with patch('codebot.botop._find_all_api_pids', return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]['tasklog_lines'] == 3
        assert agents[0]['tasklog_age'] is not None

    def test_collect_agents_missing_tasklog_returns_none(self, tmp_path):
        state_dir = tmp_path / '.codebot' / 'state'
        logs_dir = tmp_path / '.codebot' / 'logs'
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = 'no-tasklog-agent'
        (state_dir / f'{name}.heartbeat').write_text(str(time.time()))

        with patch('codebot.botop._find_all_api_pids', return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]['tasklog_lines'] is None
        assert agents[0]['tasklog_age'] is None

    def test_collect_agents_log_stat_oserror_is_swallowed(self, tmp_path, monkeypatch):
        state_dir = tmp_path / '.codebot' / 'state'
        logs_dir = tmp_path / '.codebot' / 'logs'
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = 'oserr-agent'
        (state_dir / f'{name}.heartbeat').write_text(str(time.time()))
        (logs_dir / f'{name}.tasklog').write_bytes(b'one\ntwo\n')

        orig_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            if self.name.endswith('.log'):
                raise OSError('simulated log stat failure')
            return orig_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, 'stat', fake_stat, raising=False)

        with patch('codebot.botop._find_all_api_pids', return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]['log_age'] is None
        assert agents[0]['log_size'] is None
        assert agents[0]['tasklog_lines'] == 2

    def test_collect_agents_unexpected_stat_error_fails_open(self, tmp_path, monkeypatch):
        state_dir = tmp_path / '.codebot' / 'state'
        logs_dir = tmp_path / '.codebot' / 'logs'
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = 'runtimeerr-agent'
        (state_dir / f'{name}.heartbeat').write_text(str(time.time()))
        (logs_dir / f'{name}.tasklog').write_bytes(b'x\n')

        orig_stat = Path.stat

        def fake_stat(self, *args, **kwargs):
            if self.name.endswith('.log'):
                raise RuntimeError('simulated unexpected stat failure')
            return orig_stat(self, *args, **kwargs)

        monkeypatch.setattr(Path, 'stat', fake_stat, raising=False)

        with patch('codebot.botop._find_all_api_pids', return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]['log_age'] is None
        assert agents[0]['tasklog_lines'] is None


class TestCollectAgentsCoverageGaps:
    """Cover remaining _collect_agents branches required for function-level 100%."""

    def test_invalid_status_updated_at_is_swallowed(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = "bad-status-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (state_dir / f"{name}.status.json").write_text('{"updated_at": "not-a-number"}')
        (logs_dir / f"{name}.tasklog").write_bytes(b"line\n")

        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["status_age"] is None

    def test_mission_directory_read_failure_is_swallowed(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = "bad-mission-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (logs_dir / f"{name}.tasklog").write_bytes(b"line\n")
        (logs_dir / f"{name}.mission").mkdir()

        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["model"] == ""

    def test_mission_model_is_extracted(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = "mission-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (logs_dir / f"{name}.tasklog").write_bytes(b"line\n")
        (logs_dir / f"{name}.mission").write_text("model qwen-3.8-omni-flash-thinking\n", encoding="utf-8")

        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        # Model is truncated to 24 chars per botop.py code
        assert agents[0]["model"] == "qwen-3.8-omni-flash-thin"

    def test_mission_without_model_leaves_blank(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = "no-model-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (logs_dir / f"{name}.tasklog").write_bytes(b"line\n")
        (logs_dir / f"{name}.mission").write_text("no models here\n", encoding="utf-8")

        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["model"] == ""


class TestCollectAgentsRemainingBranches:
    """Cover remaining _collect_agents statements required for function-level 100%."""

    def test_future_heartbeat_clamps_negative_age_to_zero(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = "future-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time() + 1000.0))
        (logs_dir / f"{name}.tasklog").write_bytes(b"one\n")

        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["hb_age"] == 0
        assert agents[0]["bucket"] == "RUNNING"

    def test_missing_heartbeat_with_live_pid_marks_running(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = "status-only-agent"
        (state_dir / f"{name}.status.json").write_text('{"current_task": "working"}')
        (logs_dir / f"{name}.tasklog").write_bytes(b"one\n")

        with patch("codebot.botop._find_all_api_pids", return_value={name: 4242}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["hb_age"] is None
        assert agents[0]["running"] is True
        assert agents[0]["pid"] == 4242
        assert agents[0]["bucket"] == "RUNNING"

    def test_status_task_description_populates_current_task(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = "desc-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (state_dir / f"{name}.status.json").write_text('{"task_description": "Do the thing"}')
        (logs_dir / f"{name}.tasklog").write_bytes(b"one\n")

        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["current_task"] == "Do the thing"

    def test_checkpoint_provides_current_task_when_status_absent(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = "ckpt-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (state_dir / f"{name}.checkpoint.json").write_text('{"current_task": "checkpoint task"}')
        (logs_dir / f"{name}.tasklog").write_bytes(b"one\n")

        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["current_task"] == "checkpoint task"

    def test_log_stat_populates_log_age_and_size(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)

        name = "log-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (logs_dir / f"{name}.log").write_bytes(b"hello")
        (logs_dir / f"{name}.tasklog").write_bytes(b"one\n")

        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)

        assert len(agents) == 1
        assert agents[0]["log_size"] == 5
        assert agents[0]["log_age"] is not None


class TestCollectAgentsMicroCoverage:
    """Micro-tests to hit every statement in _collect_agents for 100% coverage."""

    def test_state_json_and_paused_discovery_both_hit(self, tmp_path):
        """Ensure both *.state.json and *.paused glob loops execute their add calls."""
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        
        # Create files that will be discovered by both globs
        (state_dir / "agent-a.state.json").write_text('{}')
        (state_dir / "agent-b.paused").write_text('')
        (logs_dir / "agent-a.tasklog").write_bytes(b'x\n')
        (logs_dir / "agent-b.tasklog").write_bytes(b'y\n')
        
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)
        
        names = [a["name"] for a in agents]
        assert "agent-a" in names
        assert "agent-b" in names

    def test_empty_names_pass_executed(self, tmp_path):
        """Hit the if not names: pass branch."""
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        # No files created
        
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)
        
        assert agents == []


class TestCollectAgentsDiscoveryAndBuckets:
    """Cover discovery paths and bucket derivation for 100% function coverage."""

    def test_state_json_discovery(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        (state_dir / "state-agent.state.json").write_text('{}')
        (logs_dir / "state-agent.tasklog").write_bytes(b'x\n')
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)
        assert any(a["name"] == "state-agent" for a in agents)

    def test_paused_file_discovery(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        (state_dir / "paused-agent.paused").write_text("")
        (logs_dir / "paused-agent.tasklog").write_bytes(b'x\n')
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)
        paused = [a for a in agents if a["name"] == "paused-agent"]
        assert len(paused) == 1
        assert paused[0]["paused"] is True
        assert paused[0]["bucket"] == "PAUSED"

    def test_empty_state_dir_returns_empty_list(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)
        assert agents == []

    def test_stale_bucket_with_live_pid_guard(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        name = "stale-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time() - 300))
        (logs_dir / f"{name}.tasklog").write_bytes(b'x\n')
        with patch("codebot.botop._find_all_api_pids", return_value={name: 9999}):
            agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "STALE"
        assert agents[0]["running"] is True

    def test_dead_bucket_with_live_pid_guard(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        name = "dead-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time() - 700))
        (logs_dir / f"{name}.tasklog").write_bytes(b'x\n')
        with patch("codebot.botop._find_all_api_pids", return_value={name: 8888}):
            agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "DEAD"
        assert agents[0]["running"] is True

    def test_unknown_bucket_with_live_pid_becomes_running(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        name = "unknown-agent"
        (state_dir / f"{name}.status.json").write_text('{}')
        (logs_dir / f"{name}.tasklog").write_bytes(b'x\n')
        with patch("codebot.botop._find_all_api_pids", return_value={name: 7777}):
            agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "RUNNING"

    def test_disabled_waiting_bucket(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        name = "waiting-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (state_dir / f"{name}.state.json").write_text('{"status": "disabled"}')
        (state_dir / f"{name}.status.json").write_text('{"current_task": "waiting"}')
        (logs_dir / f"{name}.tasklog").write_bytes(b'x\n')
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "WAITING"

    def test_tool_prefix_cur_task_merges_task_desc(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        name = "tool-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (state_dir / f"{name}.status.json").write_text(
            '{"current_task": "tool:bash", "task_description": "Run tests"}'
        )
        (logs_dir / f"{name}.tasklog").write_bytes(b'x\n')
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert "tool:bash" in agents[0]["current_task"]
        assert "Run tests" in agents[0]["current_task"]

    def test_scratch_phase_used_when_status_current_task_empty(self, tmp_path):
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        name = "scratch-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (state_dir / f"{name}.scratchpad.json").write_text('{"phase": "implementing"}')
        (logs_dir / f"{name}.tasklog").write_bytes(b'x\n')
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["current_task"] == "implementing"

    def test_state_json_status_field_populates_bot_status(self, tmp_path):
        """Ensure state.get('status', '') is executed."""
        state_dir = tmp_path / ".codebot" / "state"
        logs_dir = tmp_path / ".codebot" / "logs"
        state_dir.mkdir(parents=True)
        logs_dir.mkdir(parents=True)
        name = "status-field-agent"
        (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
        (state_dir / f"{name}.state.json").write_text('{"status": "running", "restart_count": 5}')
        (logs_dir / f"{name}.tasklog").write_bytes(b'x\n')
        with patch("codebot.botop._find_all_api_pids", return_value={}):
            agents = _collect_agents(tmp_path)
        assert len(agents) == 1
        assert agents[0]["bot_status"] == "running"
        assert agents[0]["restart_count"] == 5


def test_import_and_basic_functionality():
    assert callable(_collect_agents)
    assert callable(_find_all_api_pids)
    assert callable(_tasklog_tail_line_count)
    assert callable(_read_tasklog_bounded)
    assert _TASKLOG_TAIL_BYTES == 65536


if __name__ == '__main__':
    pytest.main([__file__, '-v'])
