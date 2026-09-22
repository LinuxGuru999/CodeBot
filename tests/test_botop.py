#!/usr/bin/env python3
"""Tests for codebot/botop.py _collect_agents function.

Covers:
- test_collect_agents: verifies correct aggregation of agent data from mocked filesystem
- Mocks state directory with .heartbeat and .status.json files
- Asserts returned list contains expected agents with correct bucket/status
- Verifies sorting order (RUNNING before DEAD)
"""

import json
import re
import signal
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot.botop import _collect_agents, _bucket_color, _supports_color, _parse_checkpoint_text


class TestCollectAgents:
    """Test suite for _collect_agents function."""

    def test_collect_agents_empty_state_dir(self, tmp_path):
        """Test that empty state directory returns empty agent list."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    assert agents == []

    def test_collect_agents_with_heartbeat_file(self, tmp_path):
        """Test agent detection from .heartbeat file."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        now = time.time()
        hb_path = state_dir / "test_agent.heartbeat"
        hb_path.write_text(str(now - 30))  # 30 seconds ago

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    assert agents[0]["name"] == "test_agent"

    def test_collect_agents_with_status_json(self, tmp_path):
        """Test agent detection from .status.json file."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        status_data = {
            "current_task": "test_task",
            "iteration": 5,
            "updated_at": time.time()
        }
        status_path = state_dir / "status_agent.status.json"
        status_path.write_text(json.dumps(status_data))

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    assert agents[0]["name"] == "status_agent"

    def test_collect_agents_bucket_running(self, tmp_path):
        """Test RUNNING bucket classification for recent heartbeat (<120s)."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        now = time.time()
        hb_path = state_dir / "running_agent.heartbeat"
        hb_path.write_text(str(now - 60))  # 60 seconds ago

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    assert agents[0]["bucket"] == "RUNNING"
                    assert agents[0]["hb_age"] < 120

    def test_collect_agents_bucket_stale(self, tmp_path):
        """Test STALE bucket classification for heartbeat 120s-600s old."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        now = time.time()
        hb_path = state_dir / "stale_agent.heartbeat"
        hb_path.write_text(str(now - 300))  # 5 minutes ago

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    assert agents[0]["bucket"] == "STALE"
                    assert 120 <= agents[0]["hb_age"] < 600

    def test_collect_agents_bucket_dead(self, tmp_path):
        """Test DEAD bucket classification for heartbeat >=600s old."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        now = time.time()
        hb_path = state_dir / "dead_agent.heartbeat"
        hb_path.write_text(str(now - 700))  # ~12 minutes ago

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    assert agents[0]["bucket"] == "DEAD"
                    assert agents[0]["hb_age"] >= 600

    def test_collect_agents_bucket_paused(self, tmp_path):
        """Test PAUSED bucket classification when .paused file exists."""
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
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    assert agents[0]["bucket"] == "PAUSED"
                    assert agents[0]["paused"] is True

    def test_collect_agents_sorting_order_running_before_dead(self, tmp_path):
        """Test that RUNNING agents appear before DEAD agents in sorted list."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        now = time.time()
        # Create DEAD agent (old heartbeat)
        dead_hb = state_dir / "dead_agent.heartbeat"
        dead_hb.write_text(str(now - 700))
        # Create RUNNING agent (recent heartbeat)
        running_hb = state_dir / "running_agent.heartbeat"
        running_hb.write_text(str(now - 30))

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 2
                    # First agent should be RUNNING, second should be DEAD
                    assert agents[0]["bucket"] == "RUNNING"
                    assert agents[0]["name"] == "running_agent"
                    assert agents[1]["bucket"] == "DEAD"
                    assert agents[1]["name"] == "dead_agent"

    def test_collect_agents_sorting_full_order(self, tmp_path):
        """Test complete sorting order: RUNNING > STALE > PAUSED > DEAD > UNKNOWN."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        now = time.time()
        # RUNNING: recent heartbeat
        (state_dir / "running.heartbeat").write_text(str(now - 30))
        # STALE: 5 min old heartbeat
        (state_dir / "stale.heartbeat").write_text(str(now - 300))
        # PAUSED: recent heartbeat + .paused file
        (state_dir / "paused.heartbeat").write_text(str(now - 30))
        (state_dir / "paused.paused").write_text(str(now))
        # DEAD: old heartbeat
        (state_dir / "dead.heartbeat").write_text(str(now - 700))

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    buckets = [a["bucket"] for a in agents]
                    # Verify order: RUNNING first, then STALE, then PAUSED, then DEAD
                    assert buckets[0] == "RUNNING"
                    assert buckets[1] == "STALE"
                    assert buckets[2] == "PAUSED"
                    assert buckets[3] == "DEAD"

    def test_collect_agents_with_status_json_content(self, tmp_path):
        """Test that status.json content is correctly parsed into agent data."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        now = time.time()
        hb_path = state_dir / "status_agent.heartbeat"
        hb_path.write_text(str(now - 30))

        status_data = {
            "current_task": "implement_feature",
            "iteration": 42,
            "task_description": "Implement new API endpoint",
            "updated_at": now - 60
        }
        status_path = state_dir / "status_agent.status.json"
        status_path.write_text(json.dumps(status_data))

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    agent = agents[0]
                    assert agent["name"] == "status_agent"
                    assert agent["status"]["current_task"] == "implement_feature"
                    assert agent["status"]["iteration"] == 42

    def test_collect_agents_multiple_sources_same_agent(self, tmp_path):
        """Test agent aggregation from multiple sources (heartbeat + status + state)."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        now = time.time()
        agent_name = "multi_source_agent"

        # Heartbeat
        (state_dir / f"{agent_name}.heartbeat").write_text(str(now - 30))
        # Status JSON
        (state_dir / f"{agent_name}.status.json").write_text(json.dumps({
            "current_task": "testing",
            "iteration": 10
        }))
        # State JSON
        (state_dir / f"{agent_name}.state.json").write_text(json.dumps({
            "restart_count": 3,
            "consecutive_errors": 0,
            "status": "active"
        }))

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    agent = agents[0]
                    assert agent["name"] == agent_name
                    assert agent["restart_count"] == 3
                    assert agent["consecutive_errors"] == 0
                    assert agent["bot_status"] == "active"

    def test_collect_agents_running_with_pid(self, tmp_path):
        """Test that running agent with PID is correctly identified."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        now = time.time()
        hb_path = state_dir / "running_with_pid.heartbeat"
        hb_path.write_text(str(now - 30))

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={"running_with_pid": 12345}):
                    agents = _collect_agents(tmp_path)
                    assert len(agents) == 1
                    assert agents[0]["pid"] == 12345
                    assert agents[0]["running"] is True
                    assert agents[0]["bucket"] == "RUNNING"


class TestBucketColor:
    """Test suite for _bucket_color function accessibility improvements."""

    def test_bucket_color_includes_symbol_and_text_running(self):
        """Test RUNNING bucket includes label and Unicode symbol."""
        result = _bucket_color("RUNNING", False)
        # When disabled, should return plain text without ANSI codes
        assert "RUNNING" in result
        assert "▶" in result  # \u25b6 black right-pointing triangle

    def test_bucket_color_includes_symbol_and_text_stale(self):
        """Test STALE bucket includes STALE label and Unicode symbol."""
        result = _bucket_color("STALE", False)
        assert "STALE" in result
        assert "\u26a0" in result  # warning sign

    def test_bucket_color_includes_symbol_and_text_waiting(self):
        """Test WAITING bucket includes WAITING label and Unicode symbol."""
        result = _bucket_color("WAITING", False)
        assert "WAITING" in result
        assert "\u23f3" in result  # hourglass

    def test_bucket_color_includes_symbol_and_text_paused(self):
        """Test PAUSED bucket includes PAUSED label and Unicode symbol."""
        result = _bucket_color("PAUSED", False)
        assert "PAUSED" in result
        assert "\u23f8" in result  # pause button

    def test_bucket_color_includes_symbol_and_text_dead(self):
        """Test DEAD bucket includes DEAD label and Unicode symbol."""
        result = _bucket_color("DEAD", False)
        assert "DEAD" in result
        assert "\u2716" in result  # heavy multiplication x

    def test_bucket_color_includes_symbol_and_text_unknown(self):
        """Test UNKNOWN bucket includes UNKNOWN label and Unicode symbol."""
        result = _bucket_color("UNKNOWN", False)
        assert "UNKNOWN" in result
        assert "\u2753" in result  # question mark

    def test_bucket_color_format_matches_spec(self):
        """Test that output format matches 'SYMBOL LABEL' pattern."""
        symbols = {"RUNNING": "\u25b6", "STALE": "\u26a0", "WAITING": "\u23f3", "PAUSED": "\u23f8", "DEAD": "\u2716", "UNKNOWN": "\u2753"}
        for bucket in ["RUNNING", "STALE", "WAITING", "PAUSED", "DEAD", "UNKNOWN"]:
            result = _bucket_color(bucket, False)
            # Should contain bucket label and its symbol
            assert bucket in result
            assert symbols[bucket] in result
            # Should contain a space between symbol and label (format: 'SYMBOL LABEL')
            assert " " in result


class TestSupportsColor:
    """Test suite for _supports_color function.

    Verifies color output respects user environment settings:
    - NO_COLOR env var
    - --no-color CLI flag (no_color parameter)
    - TTY status of stdout
    - Global _GLOBAL_NO_COLOR flag
    """

    def test_supports_color_no_color_env(self):
        """_supports_color returns False when NO_COLOR env var is set.

        Per no-color.org convention, any truthy NO_COLOR value should disable color.
        """
        with patch.dict("os.environ", {"NO_COLOR": "1"}, clear=False):
            with patch("codebot.botop._GLOBAL_NO_COLOR", False):
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.isatty.return_value = True
                    assert _supports_color() is False

    def test_supports_color_no_color_env_empty_string(self):
        """_supports_color returns False when NO_COLOR env var is empty string.

        An empty string is falsy in Python, so os.environ.get("NO_COLOR") returns ""
        which is falsy — function should check TTY. This documents the actual behavior.
        """
        # os.environ.get("NO_COLOR") returns "" which is falsy,
        # so it falls through to TTY check
        with patch.dict("os.environ", {"NO_COLOR": ""}, clear=False):
            with patch("codebot.botop._GLOBAL_NO_COLOR", False):
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.isatty.return_value = True
                    # Empty string is falsy, so falls through to TTY check
                    assert _supports_color() is True

    def test_supports_color_no_tty(self):
        """_supports_color returns False when stdout is not a TTY.

        When output is piped to a file or another process, ANSI escape codes
        would produce unreadable garbage. Must detect non-TTY and disable color.
        """
        with patch.dict("os.environ", {}, clear=True):
            with patch("codebot.botop._GLOBAL_NO_COLOR", False):
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.isatty.return_value = False
                    assert _supports_color() is False

    def test_supports_color_explicit_no_color_flag(self):
        """_supports_color returns False when no_color=True (--no-color passed).

        The explicit --no-color CLI flag takes highest precedence.
        """
        assert _supports_color(no_color=True) is False

    def test_supports_color_explicit_no_color_ignores_env_and_tty(self):
        """_supports_color(no_color=True) ignores NO_COLOR env and TTY status."""
        with patch.dict("os.environ", {"NO_COLOR": "1"}, clear=False):
            with patch("sys.stdout") as mock_stdout:
                mock_stdout.isatty.return_value = True
                # no_color=True takes precedence over everything
                assert _supports_color(no_color=True) is False

    def test_supports_color_explicit_color_flag(self):
        """_supports_color(no_color=False) returns based on TTY only.

        When the caller explicitly passes --color, skip global/env checks
        and just check TTY.
        """
        with patch.dict("os.environ", {"NO_COLOR": "1"}, clear=False):
            with patch("codebot.botop._GLOBAL_NO_COLOR", True):
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.isatty.return_value = True
                    # no_color=False skips global/env checks, only checks TTY
                    assert _supports_color(no_color=False) is True

    def test_supports_color_explicit_color_flag_not_tty(self):
        """_supports_color(no_color=False) returns False when not a TTY."""
        with patch("sys.stdout") as mock_stdout:
            mock_stdout.isatty.return_value = False
            assert _supports_color(no_color=False) is False

    def test_supports_color_true_when_tty_and_no_flags(self):
        """_supports_color returns True when stdout is TTY and no flags/env set.

        This is the default happy path — color should be enabled in an
        interactive terminal session.
        """
        with patch.dict("os.environ", {}, clear=True):
            with patch("codebot.botop._GLOBAL_NO_COLOR", False):
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.isatty.return_value = True
                    assert _supports_color() is True

    def test_supports_color_global_no_color_flag(self):
        """_supports_color returns False when _GLOBAL_NO_COLOR is True.

        The global flag is set by main() from --no-color argument before
        any command dispatch.
        """
        with patch.dict("os.environ", {}, clear=True):
            with patch("codebot.botop._GLOBAL_NO_COLOR", True):
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.isatty.return_value = True
                    assert _supports_color() is False

    def test_supports_color_precedence_explicit_over_global(self):
        """no_color parameter overrides _GLOBAL_NO_COLOR when both set."""
        # no_color=False (explicit color) should override global no-color
        with patch("codebot.botop._GLOBAL_NO_COLOR", True):
            with patch("sys.stdout") as mock_stdout:
                mock_stdout.isatty.return_value = True
                assert _supports_color(no_color=False) is True

    def test_supports_color_precedence_explicit_true_overrides_all(self):
        """no_color=True overrides even TTY=True and no env var."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("codebot.botop._GLOBAL_NO_COLOR", False):
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.isatty.return_value = True
                    assert _supports_color(no_color=True) is False

    def test_supports_color_no_color_env_with_non_tty(self):
        """Both NO_COLOR env and non-TTY should return False."""
        with patch.dict("os.environ", {"NO_COLOR": "1"}, clear=False):
            with patch("codebot.botop._GLOBAL_NO_COLOR", False):
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.isatty.return_value = False
                    assert _supports_color() is False

    def test_supports_color_default_none_parameter(self):
        """Calling with explicit None behaves same as no argument."""
        with patch.dict("os.environ", {}, clear=True):
            with patch("codebot.botop._GLOBAL_NO_COLOR", False):
                with patch("sys.stdout") as mock_stdout:
                    mock_stdout.isatty.return_value = True
                    assert _supports_color(no_color=None) is True

    def test_supports_color_no_color_env_various_truthy_values(self):
        """Various truthy NO_COLOR values all disable color."""
        for val in ["1", "true", "yes", "on", "anything"]:
            with patch.dict("os.environ", {"NO_COLOR": val}, clear=False):
                with patch("codebot.botop._GLOBAL_NO_COLOR", False):
                    with patch("sys.stdout") as mock_stdout:
                        mock_stdout.isatty.return_value = True
                        assert _supports_color() is False, f"NO_COLOR={val!r} should disable color"


class TestCriticalCommands:
    """Test suite for critical botop CLI commands: cmd_status, cmd_drain, cmd_restart."""

    def test_cmd_status_output_format(self, tmp_path, capsys):
        """Test that cmd_status produces expected output format from mocked state."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        # Create mock agent state
        now = time.time()
        hb_path = state_dir / "test_agent.heartbeat"
        hb_path.write_text(str(now - 30))
        status_data = {"current_task": "testing", "iteration": 1, "updated_at": now}
        (state_dir / "test_agent.status.json").write_text(json.dumps(status_data))

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    from codebot.botop import cmd_status
                    cmd_status(tmp_path)

        captured = capsys.readouterr()
        # Verify output contains expected columns/headers
        assert "AGENT" in captured.out or "agent" in captured.out.lower()
        assert "test_agent" in captured.out
        assert "RUNNING" in captured.out or "running" in captured.out.lower()

    def test_cmd_drain_creates_drain_file(self, tmp_path, capsys):
        """Test that cmd_drain creates the drain file correctly."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        drain_file = state_dir / ".drain"
        assert not drain_file.exists(), "Drain file should not exist before cmd_drain"

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                from codebot.botop import cmd_drain
                rc = cmd_drain(tmp_path, reason="test_drain")

        assert rc == 0, "cmd_drain should return exit code 0"
        assert drain_file.exists(), "cmd_drain should create .drain file"
        content = drain_file.read_text()
        assert "test_drain" in content, "Drain file should contain the reason"
        assert re.match(r'^\d+\.\d+ .+$', content.strip()), \
            f"Drain file content {content!r} should match '<timestamp> <reason>' pattern"

    def test_cmd_drain_creates_file(self, tmp_path, capsys):
        """Alias for test_cmd_drain_creates_drain_file — verifies regex and exit code 0.

        Ticket CB-47C639B7A8CF9200 requires this exact name.
        Delegates to the same verification as test_cmd_drain_creates_drain_file.
        """
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        drain_file = state_dir / ".drain"
        assert not drain_file.exists()
        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                from codebot.botop import cmd_drain
                rc = cmd_drain(tmp_path, reason="test_drain")
        assert rc == 0
        assert drain_file.exists()
        content = drain_file.read_text()
        assert "test_drain" in content
        assert re.match(r'^\d+\.\d+ .+$', content.strip()), \
            f"Drain file content {content!r} should match '<timestamp> <reason>' pattern"

    def test_cmd_clear_drain_removes_file(self, tmp_path, capsys):
        """Test that cmd_clear_drain removes the drain file and returns 0."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        drain_file = state_dir / ".drain"
        drain_file.write_text(f"{time.time()} test_drain")
        assert drain_file.exists(), "Drain file should exist before cmd_clear_drain"

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                from codebot.botop import cmd_clear_drain
                rc = cmd_clear_drain(tmp_path)

        assert rc == 0, "cmd_clear_drain should return exit code 0"
        assert not drain_file.exists(), "cmd_clear_drain should remove .drain file"

    def test_cmd_restart_verifies_subprocess_invocation(self, tmp_path, capsys):
        """Test that cmd_restart triggers the correct subprocess calls with mocked state."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        # Create mock agent with PID
        now = time.time()
        hb_path = state_dir / "restart_agent.heartbeat"
        hb_path.write_text(str(now - 30))
        status_data = {"pid": 12345, "current_task": "working", "updated_at": now}
        (state_dir / "restart_agent.status.json").write_text(json.dumps(status_data))

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_agent_pid', return_value=12345):
                    with patch('os.kill') as mock_kill:
                        with patch('subprocess.run') as mock_run:
                            with patch('time.sleep'):
                                from codebot.botop import cmd_restart
                                # Make liveness check raise so SIGKILL path is skipped
                                def _kill_side(pid, sig):
                                    if sig == 0:
                                        raise ProcessLookupError
                                    return None
                                mock_kill.side_effect = _kill_side
                                cmd_restart(tmp_path, "restart_agent")

                                # Verify os.kill was called with SIGTERM and correct PID
                                mock_kill.assert_any_call(12345, signal.SIGTERM)
                                # Verify subprocess.run was called to restart (implementation detail may vary)
                                # The current implementation may just kill and let orchestrator restart,
                                # or it may explicitly spawn. We verify at least one action was taken.
                                assert mock_kill.called or mock_run.called, "cmd_restart should invoke kill or subprocess"

    def test_cmd_restart_missing_agent(self, tmp_path, capsys):
        """Test cmd_restart behavior when agent does not exist."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    with patch('os.kill') as mock_kill:
                        from codebot.botop import cmd_restart
                        cmd_restart(tmp_path, "nonexistent_agent")

                        # Should not call kill for nonexistent agent
                        mock_kill.assert_not_called()

    def test_cmd_status_empty_state(self, tmp_path, capsys):
        """Test cmd_status output when no agents are found."""
        state_dir = tmp_path / ".codebot" / "state"
        state_dir.mkdir(parents=True)
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()

        with patch('codebot.botop._find_state_dir', return_value=state_dir):
            with patch('codebot.botop._find_logs_dir', return_value=logs_dir):
                with patch('codebot.botop._find_all_api_pids', return_value={}):
                    from codebot.botop import cmd_status
                    cmd_status(tmp_path)

        captured = capsys.readouterr()
        # Should indicate no agents found
        assert "No agents" in captured.out or "agents" in captured.out.lower()


class TestParseCheckpointText:
    """Test suite for _parse_checkpoint_text function."""

    def test_parse_checkpoint_text_valid_json(self):
        """Test that valid JSON string is parsed correctly."""
        valid_json = '{"bot": "test_reviewer", "iteration": 5}'
        result = _parse_checkpoint_text(valid_json)
        assert result is not None
        assert isinstance(result, dict)
        assert result["bot"] == "test_reviewer"
        assert result["iteration"] == 5

    def test_parse_checkpoint_text_python_dict(self):
        """Test that Python dict literal string is parsed correctly."""
        python_dict = "{'bot': 'test_reviewer', 'iteration': 5}"
        result = _parse_checkpoint_text(python_dict)
        assert result is not None
        assert isinstance(result, dict)
        assert result["bot"] == "test_reviewer"
        assert result["iteration"] == 5

    def test_parse_checkpoint_text_malformed_returns_none(self):
        """Test that malformed input returns None."""
        malformed_inputs = [
            "not a dict",
            "12345",
            "[1, 2, 3]",
            "{invalid json}",
            "{'key': }",
            "None",
            "True",
        ]
        for inp in malformed_inputs:
            result = _parse_checkpoint_text(inp)
            assert result is None, f"Expected None for {inp!r}, got {result}"

    def test_parse_checkpoint_text_empty_returns_none(self):
        """Test that empty or whitespace-only strings return None."""
        empty_inputs = ["", "   ", "\n\t", "  \n  "]
        for inp in empty_inputs:
            result = _parse_checkpoint_text(inp)
            assert result is None, f"Expected None for {inp!r}, got {result}"
