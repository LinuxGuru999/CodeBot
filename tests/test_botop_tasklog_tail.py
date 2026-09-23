"""Tests for CB-3B012: Bounded tasklog read in _collect_agents.

Verifies:
- Large tasklogs (>64KB) only read last 64KB for line counting
- Small tasklogs (<64KB) return exact line count
- Performance: 26 agents x 1MB tasklogs completes <500ms
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from codebot.botop import _collect_agents


@pytest.fixture
def agent_env(tmp_path: Path):
    """Create a minimal project structure with state and logs dirs."""
    state_dir = tmp_path / ".codebot" / "state"
    logs_dir = tmp_path / ".codebot" / "logs"
    state_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    return tmp_path, state_dir, logs_dir


def _create_agent(state_dir: Path, logs_dir: Path, name: str, tasklog_size: int = 0):
    """Create heartbeat + optional tasklog for an agent."""
    (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
    if tasklog_size > 0:
        # Create a tasklog with known line density (~80 bytes per line)
        line = b"X" * 79 + b"\n"  # 80 bytes per line
        lines_needed = tasklog_size // 80
        remainder = tasklog_size - (lines_needed * 80)
        data = line * lines_needed
        if remainder > 0:
            data += b"Y" * (remainder - 1) + b"\n"
        (logs_dir / f"{name}.tasklog").write_bytes(data)


def _count_newlines_in_file(path: Path) -> int:
    """Count actual newlines in a file for verification."""
    with open(path, "rb") as f:
        return f.read().count(b"\n")


def test_tasklog_tail_capped_at_100kb(agent_env):
    """Large 1MB tasklog should only count newlines in last 64KB (<100KB cap)."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-large"
    size_1mb = 1024 * 1024
    _create_agent(state_dir, logs_dir, name, tasklog_size=size_1mb)

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    agent = agents[0]

    tasklog_path = logs_dir / f"{name}.tasklog"
    # Count actual total newlines in the full file
    total_lines = _count_newlines_in_file(tasklog_path)
    # Count actual newlines in last 64KB using raw byte counting per spec
    with open(tasklog_path, "rb") as f:
        f.seek(max(0, size_1mb - 65536))
        tail_data = f.read(65536)
    expected_tail_lines = tail_data.count(b"\n")

    # The count must NOT equal total lines (proving we didn't read all)
    assert agent["tasklog_lines"] != total_lines, (
        f"Expected tail approximation, got full file count {agent['tasklog_lines']}"
    )
    # Should match tail window count
    assert agent["tasklog_lines"] == expected_tail_lines, (
        f"Expected {expected_tail_lines} tail lines, got {agent['tasklog_lines']}"
    )


def test_small_tasklog_exact_count(agent_env):
    """File <64KB returns exact newline count (<100KB cap, under window)."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-small"
    size_small = 5000  # 5KB
    _create_agent(state_dir, logs_dir, name, tasklog_size=size_small)

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    agent = agents[0]

    tasklog_path = logs_dir / f"{name}.tasklog"
    expected_lines = _count_newlines_in_file(tasklog_path)
    assert agent["tasklog_lines"] == expected_lines, (
        f"Expected exact {expected_lines} lines for small file, got {agent['tasklog_lines']}"
    )


def test_collect_agents_under_500ms_with_26x1MB(agent_env, monkeypatch):
    """26 agents x 1MB tasklogs must complete in <500ms."""
    from codebot import botop

    monkeypatch.setattr(botop, "_find_all_api_pids", lambda: {})

    project_root, state_dir, logs_dir = agent_env
    size_1mb = 1024 * 1024
    for i in range(26):
        _create_agent(state_dir, logs_dir, f"agent-{i:02d}", tasklog_size=size_1mb)

    start = time.perf_counter()
    agents = _collect_agents(project_root)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(agents) == 26
    assert elapsed_ms < 500, (
        f"_collect_agents took {elapsed_ms:.1f}ms for 26x1MB tasklogs; must be <500ms"
    )


def test_empty_tasklog_zero_lines(agent_env):
    """Empty tasklog file returns 0 lines without error."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-empty"
    _create_agent(state_dir, logs_dir, name, tasklog_size=0)
    # Create empty tasklog explicitly
    (logs_dir / f"{name}.tasklog").write_bytes(b"")

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    assert agents[0]["tasklog_lines"] == 0


def test_missing_tasklog_returns_none(agent_env):
    """Agent without tasklog returns tasklog_lines=None."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-no-tasklog"
    _create_agent(state_dir, logs_dir, name, tasklog_size=0)
    # Don't create tasklog file at all

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    assert agents[0]["tasklog_lines"] is None
