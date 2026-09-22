"""Tests for CB-E05F413D3E8A1DC08515A73567E10CD8 — bounded tasklog read.

Verifies:
- tasklog reads bounded to <100KB per file (window=65536)
- _collect_agents completes in <500ms with 26 agents x 1MB tasklogs
- Binary tail-read capped at 65536 bytes: seek to max(0, size-65536), read tail, decode errors=ignore, count newlines
- Files under cap keep exact counts
"""
from __future__ import annotations

import time
from pathlib import Path

import pytest

from codebot.botop import _collect_agents

WINDOW = 65536  # per ticket spec: capped at 65536 (<100KB)


@pytest.fixture
def agent_env(tmp_path: Path):
    """Create minimal project with state and logs dirs."""
    state_dir = tmp_path / ".codebot" / "state"
    logs_dir = tmp_path / ".codebot" / "logs"
    state_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    return tmp_path, state_dir, logs_dir


def _create_agent(state_dir: Path, logs_dir: Path, name: str, tasklog_size: int = 0):
    """Create heartbeat + optional tasklog."""
    (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
    if tasklog_size > 0:
        line = b"X" * 79 + b"\n"  # 80 bytes per line
        lines_needed = tasklog_size // 80
        remainder = tasklog_size - (lines_needed * 80)
        data = line * lines_needed
        if remainder > 0:
            data += b"Y" * (remainder - 1) + b"\n"
        (logs_dir / f"{name}.tasklog").write_bytes(data)


def _tail_newline_count(path: Path, window: int = WINDOW) -> int:
    """Compute expected count using same tail logic as implementation."""
    size = path.stat().st_size
    offset = max(0, size - window)
    with open(path, "rb") as f:
        f.seek(offset)
        data = f.read(window)
    return data.decode("utf-8", errors="ignore").count("\n")


def test_tasklog_reads_bounded_to_100kb(agent_env):
    """Large 1MB tasklog must be bounded to WINDOW bytes, not full file."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-large"
    size_1mb = 1024 * 1024
    _create_agent(state_dir, logs_dir, name, tasklog_size=size_1mb)

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    agent = agents[0]

    tasklog_path = logs_dir / f"{name}.tasklog"
    total_lines = tasklog_path.read_bytes().count(b"\n")
    expected_tail = _tail_newline_count(tasklog_path, WINDOW)

    # bounded: not equal to total
    assert agent["tasklog_lines"] != total_lines
    # exact tail count
    assert agent["tasklog_lines"] == expected_tail
    # window is <100KB
    assert WINDOW < 100 * 1024
    # also verify per-file read was capped
    assert expected_tail < total_lines


def test_tasklog_under_cap_exact_count(agent_env):
    """File < WINDOW returns exact newline count."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-small"
    size_small = 5000  # 5KB << WINDOW
    _create_agent(state_dir, logs_dir, name, tasklog_size=size_small)

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    agent = agents[0]

    tasklog_path = logs_dir / f"{name}.tasklog"
    expected = tasklog_path.read_bytes().count(b"\n")
    assert agent["tasklog_lines"] == expected


def test_tasklog_exact_window_size(agent_env):
    """File exactly WINDOW bytes returns exact count (offset 0)."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-exact"
    _create_agent(state_dir, logs_dir, name, tasklog_size=WINDOW)

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    tasklog_path = logs_dir / f"{name}.tasklog"
    expected = tasklog_path.read_bytes().count(b"\n")
    assert agents[0]["tasklog_lines"] == expected


def test_empty_tasklog_zero(agent_env):
    """Empty file returns 0 lines without error."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-empty"
    (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
    (logs_dir / f"{name}.tasklog").write_bytes(b"")

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    assert agents[0]["tasklog_lines"] == 0


def test_missing_tasklog_none(agent_env):
    """No tasklog file returns None."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-no-tasklog"
    (state_dir / f"{name}.heartbeat").write_text(str(time.time()))

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    assert agents[0]["tasklog_lines"] is None


def test_truncated_utf8_decode_safe(agent_env):
    """Truncated UTF-8 at seek point must not crash; errors=ignore."""
    project_root, state_dir, logs_dir = agent_env
    name = "agent-utf8"
    (state_dir / f"{name}.heartbeat").write_text(str(time.time()))
    # Build file >WINDOW with multibyte chars near boundary
    # WINDOW -10 bytes of ascii, then a 4-byte utf8 char split by offset
    prefix_size = WINDOW + 100
    # Use 😀 (4 bytes f0 9f 98 80) repeated to create split scenario
    payload = b"a\n" * 1000  # some lines
    # pad to exceed WINDOW
    payload = payload + b"X" * (prefix_size - len(payload) - 10) + "😀😀😀".encode() + b"\nEND\n"
    # Ensure >WINDOW
    assert len(payload) > WINDOW
    (logs_dir / f"{name}.tasklog").write_bytes(payload)

    agents = _collect_agents(project_root)
    assert len(agents) == 1
    # Must not raise and must return an int count
    assert isinstance(agents[0]["tasklog_lines"], int)
    # Verify count matches decode(..., errors=ignore) behavior
    expected = _tail_newline_count(logs_dir / f"{name}.tasklog", WINDOW)
    assert agents[0]["tasklog_lines"] == expected


def test_collect_agents_under_500ms_with_26x1MB(agent_env):
    """26 agents x 1MB tasklogs must complete in <500ms."""
    project_root, state_dir, logs_dir = agent_env
    size_1mb = 1024 * 1024
    for i in range(26):
        _create_agent(state_dir, logs_dir, f"agent-{i:02d}", tasklog_size=size_1mb)

    start = time.perf_counter()
    agents = _collect_agents(project_root)
    elapsed_ms = (time.perf_counter() - start) * 1000

    assert len(agents) == 26
    assert elapsed_ms < 500, f"_collect_agents took {elapsed_ms:.1f}ms; must be <500ms"


def test_read_is_binary_tail_not_text(agent_env):
    """Verify implementation uses binary tail, not read_text()."""
    # Inspect botop.py source for compliance
    import pathlib
    src = pathlib.Path("codebot/botop.py").read_text(encoding="utf-8", errors="ignore")
    # Ensure the old unbounded read_text path is not present for tasklogs in _collect_agents
    # The file should contain our bounded logic markers
    assert "65536" in src
    assert "seek" in src
    assert "errors=\"ignore\"" in src or "errors='ignore'" in src
    # Should use binary mode and count newlines after decode
    assert "open(tasklog_path, \"rb\")" in src or "open(tasklog_path, 'rb')" in src
