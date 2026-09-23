"""Tests for codebot.botop operator CLI."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot import botop


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def project_root(tmp_path: Path) -> Path:
    """Create a minimal project root with .codebot/state and .codebot/logs."""
    state_dir = tmp_path / ".codebot" / "state"
    logs_dir = tmp_path / ".codebot" / "logs"
    state_dir.mkdir(parents=True)
    logs_dir.mkdir(parents=True)
    return tmp_path


@pytest.fixture(autouse=True)
def reset_global_no_color():
    """Ensure _GLOBAL_NO_COLOR is reset between tests."""
    original = botop._GLOBAL_NO_COLOR
    botop._GLOBAL_NO_COLOR = False
    yield
    botop._GLOBAL_NO_COLOR = original


# ---------------------------------------------------------------------------
# _supports_color
# ---------------------------------------------------------------------------

class TestSupportsColor:
    def test_explicit_true_disables(self):
        assert botop._supports_color(no_color=True) is False

    def test_explicit_false_checks_tty(self):
        with patch.object(botop.sys.stdout, "isatty", return_value=True):
            assert botop._supports_color(no_color=False) is True
        with patch.object(botop.sys.stdout, "isatty", return_value=False):
            assert botop._supports_color(no_color=False) is False

    def test_respects_no_color_env_var(self, monkeypatch):
        monkeypatch.setenv("NO_COLOR", "1")
        assert botop._supports_color() is False

    def test_respects_global_flag(self):
        botop._GLOBAL_NO_COLOR = True
        assert botop._supports_color() is False

    def test_falls_back_to_tty(self, monkeypatch):
        monkeypatch.delenv("NO_COLOR", raising=False)
        with patch.object(botop.sys.stdout, "isatty", return_value=True):
            assert botop._supports_color() is True
        with patch.object(botop.sys.stdout, "isatty", return_value=False):
            assert botop._supports_color() is False


# ---------------------------------------------------------------------------
# _parse_checkpoint_text
# ---------------------------------------------------------------------------

class TestParseCheckpointText:
    def test_valid_json_dict(self):
        result = botop._parse_checkpoint_text('{"key": "value"}')
        assert result == {"key": "value"}

    def test_empty_string(self):
        assert botop._parse_checkpoint_text("") is None
        assert botop._parse_checkpoint_text("   ") is None

    def test_malformed_returns_none(self):
        assert botop._parse_checkpoint_text("not json or python dict at all!!!") is None

    def test_python_single_quoted_dict(self):
        result = botop._parse_checkpoint_text("{'a': 1, 'b': 2}")
        assert result == {"a": 1, "b": 2}

    def test_json_with_surrounding_garbage(self):
        raw = 'some garbage {"x": 42} more garbage'
        result = botop._parse_checkpoint_text(raw)
        assert result == {"x": 42}

    def test_json_list_not_returned(self):
        assert botop._parse_checkpoint_text("[1, 2, 3]") is None


# ---------------------------------------------------------------------------
# _read_json_safe
# ---------------------------------------------------------------------------

class TestReadJsonSafe:
    def test_reads_valid_json(self, tmp_path: Path):
        p = tmp_path / "test.json"
        p.write_text('{"hello": "world"}')
        assert botop._read_json_safe(p) == {"hello": "world"}

    def test_missing_file(self, tmp_path: Path):
        p = tmp_path / "missing.json"
        assert botop._read_json_safe(p) is None

    def test_invalid_json(self, tmp_path: Path):
        p = tmp_path / "bad.json"
        p.write_text("not json{{{")
        assert botop._read_json_safe(p) is None

    def test_empty_file(self, tmp_path: Path):
        p = tmp_path / "empty.json"
        p.write_text("")
        assert botop._read_json_safe(p) is None


# ---------------------------------------------------------------------------
# _collect_agents
# ---------------------------------------------------------------------------

class TestCollectAgents:
    @patch("codebot.botop._find_all_api_pids", return_value={})
    def test_returns_empty_when_no_agents(self, mock_pids, project_root: Path):
        agents = botop._collect_agents(project_root)
        assert agents == []

    @patch("codebot.botop._find_all_api_pids", return_value={})
    def test_discovers_agent_from_heartbeat(self, mock_pids, project_root: Path):
        state_dir = project_root / ".codebot" / "state"
        hb = state_dir / "scheduler.heartbeat"
        hb.write_text(str(time.time()))

        agents = botop._collect_agents(project_root)
        assert len(agents) == 1
        assert agents[0]["name"] == "scheduler"
        assert agents[0]["bucket"] == "RUNNING"

    @patch("codebot.botop._find_all_api_pids", return_value={})
    def test_stale_heartbeat_bucket(self, mock_pids, project_root: Path):
        state_dir = project_root / ".codebot" / "state"
        hb = state_dir / "old_bot.heartbeat"
        # heartbeat from 5 minutes ago → STALE (120 <= age < 600)
        hb.write_text(str(time.time() - 300))

        agents = botop._collect_agents(project_root)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "STALE"

    @patch("codebot.botop._find_all_api_pids", return_value={})
    def test_dead_heartbeat_bucket(self, mock_pids, project_root: Path):
        state_dir = project_root / ".codebot" / "state"
        hb = state_dir / "dead_bot.heartbeat"
        # heartbeat from 20 minutes ago → DEAD (>= 600)
        hb.write_text(str(time.time() - 1200))

        agents = botop._collect_agents(project_root)
        assert len(agents) == 1
        assert agents[0]["bucket"] == "DEAD"

    @patch("codebot.botop._find_all_api_pids", return_value={})
    def test_paused_agent(self, mock_pids, project_root: Path):
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "p_bot.heartbeat").write_text(str(time.time()))
        (state_dir / "p_bot.paused").write_text(str(time.time()))

        agents = botop._collect_agents(project_root)
        assert len(agents) == 1
        assert agents[0]["paused"] is True
        assert agents[0]["bucket"] == "PAUSED"

    @patch("codebot.botop._find_all_api_pids", return_value={"mybot": 12345})
    def test_running_pid_detected(self, mock_pids, project_root: Path):
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "mybot.heartbeat").write_text(str(time.time()))

        agents = botop._collect_agents(project_root)
        assert len(agents) == 1
        assert agents[0]["pid"] == 12345
        assert agents[0]["running"] is True


class TestCollectAgentsPartialAndBuckets:
    NOW = 1000.0

    def _collect(self, project_root: Path, pids: dict[str, int] | None = None) -> list[dict]:
        if pids is None:
            pids = {}
        with patch("codebot.botop._find_all_api_pids", return_value=pids), patch(
            "codebot.botop.time.time", return_value=self.NOW
        ):
            return botop._collect_agents(project_root)

    def test_discovers_and_buckets_all_partial_state_types(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "hb.heartbeat").write_text(str(self.NOW - 10))
        (state_dir / "status.status.json").write_text("{}")
        (state_dir / "state.state.json").write_text("{}")
        (state_dir / "paused.paused").write_text("")
        agents = {a["name"]: a for a in self._collect(project_root)}
        assert agents["hb"]["bucket"] == "RUNNING"
        assert agents["status"]["bucket"] == "UNKNOWN"
        assert agents["state"]["bucket"] == "UNKNOWN"
        assert agents["paused"]["bucket"] == "PAUSED"
        assert agents["status"]["status"] == {}
        assert agents["status"]["state"] == {}
        assert agents["status"]["scratch"] == {}
        assert agents["status"]["ckpt"] is None

    def test_corrupt_and_unexpected_types_are_normalized(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "bot.heartbeat").write_text(str(self.NOW - 10))
        (state_dir / "bot.status.json").write_text(json.dumps([1, 2]))
        (state_dir / "bot.state.json").write_text(json.dumps("nope"))
        (state_dir / "bot.checkpoint.json").write_text("not a dict")
        (state_dir / "bot.scratchpad.json").write_text(json.dumps(123))
        a = self._collect(project_root)[0]
        assert a["status"] == {}
        assert a["state"] == {}
        assert a["scratch"] == {}
        assert a["ckpt"] is None
        assert a["bucket"] == "RUNNING"

    def test_heartbeat_boundaries_and_future_clamp(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        cases = [
            ("run", 119, "RUNNING"),
            ("stale", 120, "STALE"),
            ("stale2", 599, "STALE"),
            ("dead", 600, "DEAD"),
            ("future", -100, "RUNNING"),
        ]
        for name, age, _ in cases:
            (state_dir / f"{name}.heartbeat").write_text(str(self.NOW - age))
        agents = {a["name"]: a for a in self._collect(project_root)}
        for name, _, expected in cases:
            assert agents[name]["bucket"] == expected
        assert agents["future"]["hb_age"] == 0

    def test_pid_detection_without_heartbeat_marks_running(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "ghost.status.json").write_text("{}")
        a = self._collect(project_root, pids={"ghost": 4242})[0]
        assert a["pid"] == 4242
        assert a["running"] is True
        assert a["bucket"] == "RUNNING"

    def test_no_heartbeat_no_pid_is_unknown(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "ghost.status.json").write_text("{}")
        a = self._collect(project_root)[0]
        assert a["running"] is False
        assert a["bucket"] == "UNKNOWN"

    def test_paused_overrides_live_heartbeat(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "p.heartbeat").write_text(str(self.NOW - 5))
        (state_dir / "p.paused").write_text("")
        a = self._collect(project_root)[0]
        assert a["paused"] is True
        assert a["bucket"] == "PAUSED"

    def test_waiting_bucket_from_disabled_state_and_waiting_task(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "w.heartbeat").write_text(str(self.NOW - 5))
        (state_dir / "w.state.json").write_text(json.dumps({"status": "disabled"}))
        (state_dir / "w.status.json").write_text(json.dumps({"current_task": "waiting"}))
        a = self._collect(project_root)[0]
        assert a["bucket"] == "WAITING"

    def test_status_task_description_and_updated_at(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "t.heartbeat").write_text(str(self.NOW - 5))
        (state_dir / "t.status.json").write_text(json.dumps({"task_description": "do the thing", "updated_at": self.NOW - 55}))
        a = self._collect(project_root)[0]
        assert a["current_task"] == "do the thing"
        assert a["status_age"] == pytest.approx(55.0)

    def test_tool_task_description_merge(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "tool.heartbeat").write_text(str(self.NOW - 5))
        (state_dir / "tool.status.json").write_text(json.dumps({"current_task": "tool:read", "task_description": "inspect file"}))
        a = self._collect(project_root)[0]
        assert a["current_task"] == "tool:read inspect file"

    def test_scratch_phase_and_iteration_fallback(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "s.heartbeat").write_text(str(self.NOW - 5))
        (state_dir / "s.scratchpad.json").write_text(json.dumps({"phase": "implementing", "iteration": 9}))
        a = self._collect(project_root)[0]
        assert a["current_task"] == "implementing"
        assert a["iteration"] == 9

    def test_checkpoint_reason_fallback(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "c.heartbeat").write_text(str(self.NOW - 5))
        (state_dir / "c.checkpoint.json").write_text(json.dumps({"reason": "stalled"}))
        a = self._collect(project_root)[0]
        assert a["current_task"] == "stalled"

    def test_status_iteration_overrides_scratch(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "i.heartbeat").write_text(str(self.NOW - 5))
        (state_dir / "i.status.json").write_text(json.dumps({"iteration": 3}))
        (state_dir / "i.scratchpad.json").write_text(json.dumps({"iteration": 9}))
        a = self._collect(project_root)[0]
        assert a["iteration"] == 3

    def test_log_tasklog_and_mission_metadata(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        logs_dir = project_root / ".codebot" / "logs"
        (state_dir / "meta.heartbeat").write_text(str(self.NOW - 10))
        log = logs_dir / "meta.log"
        log.write_text("hello")
        os.utime(log, (self.NOW - 30, self.NOW - 30))
        nl = chr(10)
        tasklog = logs_dir / "meta.tasklog"
        tasklog.write_text(f"a{nl}b{nl}c{nl}")
        os.utime(tasklog, (self.NOW - 40, self.NOW - 40))
        mission = logs_dir / "meta.mission"
        mission.write_text("model test.model-1" + chr(10))
        a = self._collect(project_root)[0]
        assert a["log_age"] == pytest.approx(30.0)
        assert a["log_size"] == 5
        assert a["tasklog_age"] == pytest.approx(40.0)
        assert a["tasklog_lines"] == 3
        assert a["model"] == "test.model-1"

    def test_corrupt_heartbeat_is_unknown_without_pid(self, project_root: Path) -> None:
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "bad.heartbeat").write_text("not-a-float")
        a = self._collect(project_root)[0]
        assert a["hb_ts"] is None
        assert a["hb_age"] is None
        assert a["bucket"] == "UNKNOWN"


# ---------------------------------------------------------------------------
# _collect_tickets
# ---------------------------------------------------------------------------

class TestCollectTickets:
    def test_no_tickets_file(self, project_root: Path):
        store, summary, tickets = botop._collect_tickets(project_root)
        assert store is None
        assert summary is None
        assert tickets == []

    def test_reads_tickets_json(self, project_root: Path):
        state_dir = project_root / ".codebot" / "state"
        data = {
            "tickets": [
                {"id": "T1", "state": "TRIAGED", "severity": "high"},
                {"id": "T2", "state": "COMPLETE", "severity": "low"},
            ]
        }
        (state_dir / "tickets.json").write_text(json.dumps(data))

        store, summary, tickets = botop._collect_tickets(project_root)
        assert summary is not None
        assert summary.get("TRIAGED") == 1
        assert summary.get("COMPLETE") == 1
        assert len(tickets) == 2


# ---------------------------------------------------------------------------
# cmd_status
# ---------------------------------------------------------------------------

class TestCmdStatus:
    @patch("codebot.botop._find_all_api_pids", return_value={})
    def test_cmd_status_no_agents(self, mock_pids, project_root: Path, capsys):
        rc = botop.cmd_status(project_root)
        assert rc == 0
        out = capsys.readouterr().out
        assert "No agents found" in out

    @patch("codebot.botop._find_all_api_pids", return_value={})
    def test_cmd_status_json_output(self, mock_pids, project_root: Path, capsys):
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "bot1.heartbeat").write_text(str(time.time()))

        rc = botop.cmd_status(project_root, json_out=True)
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert "agents" in data
        assert len(data["agents"]) == 1
        assert data["agents"][0]["name"] == "bot1"

    @patch("codebot.botop._find_all_api_pids", return_value={})
    def test_cmd_status_verbose(self, mock_pids, project_root: Path, capsys):
        state_dir = project_root / ".codebot" / "state"
        (state_dir / "vbot.heartbeat").write_text(str(time.time()))

        rc = botop.cmd_status(project_root, verbose=True)
        assert rc == 0
        out = capsys.readouterr().out
        assert "vbot" in out
        assert "MODEL" in out  # verbose header includes MODEL column


# ---------------------------------------------------------------------------
# cmd_drain / cmd_clear_drain
# ---------------------------------------------------------------------------

class TestCmdDrain:
    def test_cmd_drain_creates_file(self, project_root: Path, capsys):
        state_dir = project_root / ".codebot" / "state"
        drain_file = state_dir / ".drain"
        assert not drain_file.exists()

        rc = botop.cmd_drain(project_root, reason="testing")
        assert rc == 0
        assert drain_file.exists()
        content = drain_file.read_text()
        assert "testing" in content
        # Verify format: '<timestamp> <reason>'
        assert re.match(r'^\d+\.\d+ .+$', content), f"Drain file content '{content}' does not match expected format"

        out = capsys.readouterr().out
        assert "Drain set" in out

    def test_cmd_clear_drain_removes_file(self, project_root: Path, capsys):
        state_dir = project_root / ".codebot" / "state"
        drain_file = state_dir / ".drain"
        # Create a drain file manually
        drain_file.write_text(f"{time.time()} manual")
        assert drain_file.exists()

        rc = botop.cmd_clear_drain(project_root)
        assert rc == 0
        assert not drain_file.exists()
        assert "Drain cleared" in capsys.readouterr().out

    def test_cmd_clear_drain(self, project_root: Path, capsys):
        state_dir = project_root / ".codebot" / "state"
        drain_file = state_dir / ".drain"
        drain_file.write_text("123 manual")

        rc = botop.cmd_clear_drain(project_root)
        assert rc == 0
        assert not drain_file.exists()
        assert "Drain cleared" in capsys.readouterr().out

    def test_cmd_clear_drain_no_active(self, project_root: Path, capsys):
        rc = botop.cmd_clear_drain(project_root)
        assert rc == 0
        assert "No drain active" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_restart
# ---------------------------------------------------------------------------

class TestCmdRestart:
    @patch("codebot.botop._find_agent_pid", return_value=None)
    def test_restart_not_running(self, mock_pid, project_root: Path, capsys):
        rc = botop.cmd_restart(project_root, "ghost_bot")
        assert rc == 0
        assert "not running" in capsys.readouterr().out

    @patch("codebot.botop._find_agent_pid", return_value=12345)
    @patch("codebot.botop.os.kill")
    @patch("codebot.botop.time.sleep")
    def test_restart_dry_run(self, mock_sleep, mock_kill, mock_pid, project_root: Path, capsys):
        """Test that --dry-run shows preview but does not kill process."""
        rc = botop.cmd_restart(project_root, "test_bot", dry_run=True)
        assert rc == 0
        mock_kill.assert_not_called()
        out = capsys.readouterr().out
        assert "[DRY-RUN]" in out
        assert "Would restart agent 'test_bot'" in out
        assert "Undo:" in out

    @patch("codebot.botop._find_agent_pid", return_value=12345)
    @patch("codebot.botop.os.kill")
    @patch("codebot.botop.time.sleep")
    def test_restart_force_skips_prompt(self, mock_sleep, mock_kill, mock_pid, project_root: Path, capsys):
        """Test that --force skips confirmation prompt."""
        rc = botop.cmd_restart(project_root, "test_bot", force=True)
        assert rc == 0
        mock_kill.assert_called_with(12345, botop.signal.SIGTERM)
        out = capsys.readouterr().out
        assert "Sent SIGTERM" in out

    @patch("codebot.botop._find_agent_pid", return_value=12345)
    @patch("codebot.botop.os.kill")
    @patch("codebot.botop.time.sleep")
    def test_restart_with_confirmation(self, mock_sleep, mock_kill, mock_pid, project_root: Path, capsys, monkeypatch):
        """Test that restart prompts for confirmation."""
        # Simulate user typing 'y'
        monkeypatch.setattr("builtins.input", lambda _: "y")
        rc = botop.cmd_restart(project_root, "test_bot")
        assert rc == 0
        mock_kill.assert_called_with(12345, botop.signal.SIGTERM)

    @patch("codebot.botop._find_agent_pid", return_value=12345)
    @patch("codebot.botop.os.kill")
    def test_restart_cancelled_by_user(self, mock_kill, mock_pid, project_root: Path, capsys, monkeypatch):
        """Test that restart is cancelled if user says no."""
        monkeypatch.setattr("builtins.input", lambda _: "n")
        rc = botop.cmd_restart(project_root, "test_bot")
        assert rc == 0
        mock_kill.assert_not_called()
        assert "Cancelled" in capsys.readouterr().out

    @patch("codebot.botop._find_agent_pid", return_value=12345)
    @patch("codebot.botop.os.kill")
    def test_restart_non_interactive_requires_force(self, mock_kill, mock_pid, project_root: Path, capsys, monkeypatch):
        """Test that non-interactive mode (EOFError) requires --force."""
        # Simulate EOFError (no stdin)
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
        rc = botop.cmd_restart(project_root, "test_bot")
        assert rc == 1
        mock_kill.assert_not_called()
        err = capsys.readouterr().err
        assert "Interactive confirmation required" in err


class TestCmdPause:
    def test_pause_dry_run(self, project_root: Path, capsys):
        """Test that --dry-run shows preview but does not create pause file."""
        state_dir = project_root / ".codebot" / "state"
        pause_file = state_dir / "test_bot.paused"
        assert not pause_file.exists()
        
        rc = botop.cmd_pause(project_root, "test_bot", dry_run=True)
        assert rc == 0
        assert not pause_file.exists()
        out = capsys.readouterr().out
        assert "[DRY-RUN]" in out
        assert "Would pause agent 'test_bot'" in out
        assert "Undo:" in out

    def test_pause_force_skips_prompt(self, project_root: Path, capsys):
        """Test that --force skips confirmation prompt."""
        state_dir = project_root / ".codebot" / "state"
        pause_file = state_dir / "test_bot.paused"
        assert not pause_file.exists()
        
        rc = botop.cmd_pause(project_root, "test_bot", force=True)
        assert rc == 0
        assert pause_file.exists()
        out = capsys.readouterr().out
        assert "Paused test_bot" in out

    def test_pause_with_confirmation(self, project_root: Path, capsys, monkeypatch):
        """Test that pause prompts for confirmation."""
        state_dir = project_root / ".codebot" / "state"
        pause_file = state_dir / "test_bot.paused"
        monkeypatch.setattr("builtins.input", lambda _: "y")
        
        rc = botop.cmd_pause(project_root, "test_bot")
        assert rc == 0
        assert pause_file.exists()

    def test_pause_cancelled_by_user(self, project_root: Path, capsys, monkeypatch):
        """Test that pause is cancelled if user says no."""
        state_dir = project_root / ".codebot" / "state"
        pause_file = state_dir / "test_bot.paused"
        monkeypatch.setattr("builtins.input", lambda _: "n")
        
        rc = botop.cmd_pause(project_root, "test_bot")
        assert rc == 0
        assert not pause_file.exists()
        assert "Cancelled" in capsys.readouterr().out

    def test_pause_non_interactive_requires_force(self, project_root: Path, capsys, monkeypatch):
        """Test that non-interactive mode (EOFError) requires --force."""
        state_dir = project_root / ".codebot" / "state"
        pause_file = state_dir / "test_bot.paused"
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
        
        rc = botop.cmd_pause(project_root, "test_bot")
        assert rc == 1
        assert not pause_file.exists()
        err = capsys.readouterr().err
        assert "Interactive confirmation required" in err

    def test_pause_already_paused(self, project_root: Path, capsys):
        """Test that pausing an already paused agent is a no-op."""
        state_dir = project_root / ".codebot" / "state"
        pause_file = state_dir / "test_bot.paused"
        pause_file.write_text("12345")
        
        rc = botop.cmd_pause(project_root, "test_bot")
        assert rc == 0
        assert "already paused" in capsys.readouterr().out


class TestCmdDrain:
    def test_drain_dry_run(self, project_root: Path, capsys):
        """Test that --dry-run shows preview but does not create drain file."""
        state_dir = project_root / ".codebot" / "state"
        drain_file = state_dir / ".drain"
        assert not drain_file.exists()
        
        rc = botop.cmd_drain(project_root, reason="testing", dry_run=True)
        assert rc == 0
        assert not drain_file.exists()
        out = capsys.readouterr().out
        assert "[DRY-RUN]" in out
        assert "Would set drain flag" in out
        assert "Undo:" in out

    def test_drain_force_skips_prompt(self, project_root: Path, capsys):
        """Test that --force skips confirmation prompt."""
        state_dir = project_root / ".codebot" / "state"
        drain_file = state_dir / ".drain"
        assert not drain_file.exists()
        
        rc = botop.cmd_drain(project_root, reason="testing", force=True)
        assert rc == 0
        assert drain_file.exists()
        out = capsys.readouterr().out
        assert "Drain set: testing" in out

    def test_drain_with_confirmation(self, project_root: Path, capsys, monkeypatch):
        """Test that drain prompts for confirmation."""
        state_dir = project_root / ".codebot" / "state"
        drain_file = state_dir / ".drain"
        monkeypatch.setattr("builtins.input", lambda _: "y")
        
        rc = botop.cmd_drain(project_root, reason="testing")
        assert rc == 0
        assert drain_file.exists()

    def test_drain_cancelled_by_user(self, project_root: Path, capsys, monkeypatch):
        """Test that drain is cancelled if user says no."""
        state_dir = project_root / ".codebot" / "state"
        drain_file = state_dir / ".drain"
        monkeypatch.setattr("builtins.input", lambda _: "n")
        
        rc = botop.cmd_drain(project_root, reason="testing")
        assert rc == 0
        assert not drain_file.exists()
        assert "Cancelled" in capsys.readouterr().out

    def test_drain_non_interactive_requires_force(self, project_root: Path, capsys, monkeypatch):
        """Test that non-interactive mode (EOFError) requires --force."""
        state_dir = project_root / ".codebot" / "state"
        drain_file = state_dir / ".drain"
        monkeypatch.setattr("builtins.input", lambda _: (_ for _ in ()).throw(EOFError()))
        
        rc = botop.cmd_drain(project_root, reason="testing")
        assert rc == 1
        assert not drain_file.exists()
        err = capsys.readouterr().err
        assert "Interactive confirmation required" in err

    def test_drain_already_active(self, project_root: Path, capsys):
        """Test that setting drain when already active is a no-op."""
        state_dir = project_root / ".codebot" / "state"
        drain_file = state_dir / ".drain"
        drain_file.write_text("12345 manual")
        
        rc = botop.cmd_drain(project_root, reason="testing")
        assert rc == 0
        assert "Drain is already active" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# cmd_health
# ---------------------------------------------------------------------------

class TestCmdHealth:
    @patch("codebot.botop._find_all_api_pids", return_value={})
    def test_cmd_health_json(self, mock_pids, project_root: Path, capsys):
        rc = botop.cmd_health(project_root, json_out=True)
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert "project" in data
        assert "orchestrator" in data
        assert "agents_summary" in data


# ---------------------------------------------------------------------------
# Helper utilities: _strip_ansi, _visible_width, _ansi_pad
# ---------------------------------------------------------------------------

class TestAnsiHelpers:
    def test_strip_ansi(self):
        assert botop._strip_ansi("\033[31mred\033[0m") == "red"
        assert botop._strip_ansi("plain") == "plain"

    def test_visible_width(self):
        assert botop._visible_width("\033[32mhello\033[0m") == 5
        assert botop._visible_width("abc") == 3

    def test_ansi_pad_left(self):
        result = botop._ansi_pad("hi", 5, align="left")
        assert result == "hi   "
        assert len(result) == 5

    def test_ansi_pad_right(self):
        result = botop._ansi_pad("hi", 5, align="right")
        assert result == "   hi"

    def test_ansi_pad_no_truncation(self):
        result = botop._ansi_pad("toolong", 3)
        assert result == "toolong"