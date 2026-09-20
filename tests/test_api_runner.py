"""Tests for codebot/api_runner.py core execution and safety functions.

Covers:
- _parse_tool_args
- _execute_tool
- _is_draining
- _write_heartbeat
- _write_checkpoint
- _token_gate_allows
- _extract_provider_usage
- _write_bot_status

Ticket: CB-9812194-42DA
"""
import json
import os
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

# Ensure codebot is importable
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.api_runner import (
    _parse_tool_args,
    _execute_tool,
    _is_draining,
    _write_heartbeat,
    _write_checkpoint,
    _token_gate_allows,
    _extract_provider_usage,
    _write_bot_status,
    _auto_commit,
    _log,
    HIGH_RISK_TOKEN_MANIFESTS,
)


class TestParseToolArgs:
    """Tests for _parse_tool_args JSON parsing."""

    def test_valid_json_dict(self):
        raw = '{"path": "/tmp/foo", "limit": 10}'
        result = _parse_tool_args(raw)
        assert result == {"path": "/tmp/foo", "limit": 10}

    def test_already_dict(self):
        raw = {"command": "ls -la"}
        result = _parse_tool_args(raw)
        assert result == {"command": "ls -la"}

    def test_empty_string(self):
        assert _parse_tool_args("") == {}

    def test_none_input(self):
        assert _parse_tool_args(None) == {}

    def test_invalid_json(self):
        assert _parse_tool_args("not json at all") == {}

    def test_json_array_returns_empty(self):
        # Arrays are not valid tool args dicts
        assert _parse_tool_args('[1, 2, 3]') == {}

    def test_whitespace_only(self):
        assert _parse_tool_args("   \n\t  ") == {}

    def test_nested_dict(self):
        raw = '{"filter": {"status": "active", "tags": ["a","b"]}}'
        result = _parse_tool_args(raw)
        assert result["filter"]["status"] == "active"


class TestExtractProviderUsage:
    """Tests for _extract_provider_usage token counting."""

    def test_valid_usage(self):
        resp = {"usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}}
        result = _extract_provider_usage(resp)
        assert result["prompt_tokens"] == 100
        assert result["completion_tokens"] == 50
        assert result["total_tokens"] == 150

    def test_missing_usage_key(self):
        resp = {"choices": []}
        result = _extract_provider_usage(resp)
        assert result == {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

    def test_none_response(self):
        result = _extract_provider_usage(None)
        assert result["total_tokens"] == 0

    def test_string_token_values(self):
        resp = {"usage": {"prompt_tokens": "200", "completion_tokens": "80"}}
        result = _extract_provider_usage(resp)
        assert result["prompt_tokens"] == 200
        assert result["completion_tokens"] == 80
        # total should be computed when missing
        assert result["total_tokens"] == 280

    def test_zero_total_computed_from_parts(self):
        resp = {"usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 0}}
        result = _extract_provider_usage(resp)
        assert result["total_tokens"] == 30

    def test_non_dict_usage(self):
        resp = {"usage": "invalid"}
        result = _extract_provider_usage(resp)
        assert result["total_tokens"] == 0


class TestTokenGateAllows:
    """Tests for _token_gate_allows budget enforcement."""

    def test_standard_manifest_allowed(self):
        manifest = {"name": "worker-1", "batch_tier": "standard"}
        assert _token_gate_allows(manifest) is True

    def test_high_risk_manifest_blocked_without_high_limit(self):
        for name in HIGH_RISK_TOKEN_MANIFESTS:
            manifest = {"name": name, "batch_tier": "standard"}
            assert _token_gate_allows(manifest) is False, f"{name} should be blocked"

    def test_high_risk_manifest_allowed_with_high_limit(self):
        for name in HIGH_RISK_TOKEN_MANIFESTS:
            manifest = {"name": name, "batch_tier": "high-limit"}
            assert _token_gate_allows(manifest) is True, f"{name} should be allowed with high-limit"

    def test_missing_name_allowed(self):
        manifest = {"batch_tier": "standard"}
        assert _token_gate_allows(manifest) is True

    def test_missing_tier_allowed_for_non_high_risk(self):
        manifest = {"name": "worker-2"}
        assert _token_gate_allows(manifest) is True

    def test_missing_tier_blocks_high_risk(self):
        for name in HIGH_RISK_TOKEN_MANIFESTS:
            manifest = {"name": name}
            assert _token_gate_allows(manifest) is False


class TestIsDraining:
    """Tests for _is_draining drain detection."""

    def test_no_drain_file(self, tmp_path):
        with patch("codebot.api_runner.DRAIN_FILE", tmp_path / ".drain"):
            with patch("codebot.api_runner.BOTS_DIR", tmp_path):
                assert _is_draining() is False

    def test_global_drain_file_exists(self, tmp_path):
        drain_file = tmp_path / ".drain"
        drain_file.write_text(str(time.time()))
        with patch("codebot.api_runner.DRAIN_FILE", drain_file):
            with patch("codebot.api_runner.BOTS_DIR", tmp_path):
                assert _is_draining() is True

    def test_per_bot_drain_file_exists(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        per_bot = state_dir / ".drain_test-bot"
        per_bot.write_text(str(time.time()))
        with patch("codebot.api_runner.DRAIN_FILE", tmp_path / ".drain_nonexistent"):
            with patch("codebot.api_runner.BOTS_DIR", tmp_path):
                assert _is_draining("test-bot") is True

    def test_per_bot_drain_not_triggered_for_other_bot(self, tmp_path):
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        (state_dir / ".drain_other-bot").write_text(str(time.time()))
        with patch("codebot.api_runner.DRAIN_FILE", tmp_path / ".drain_nonexistent"):
            with patch("codebot.api_runner.BOTS_DIR", tmp_path):
                assert _is_draining("my-bot") is False


class TestWriteHeartbeat:
    """Tests for _write_heartbeat file I/O."""

    def test_writes_timestamp(self, tmp_path):
        hb = tmp_path / "test.heartbeat"
        before = time.time()
        _write_heartbeat(hb)
        after = time.time()
        content = hb.read_text().strip()
        ts = float(content)
        assert before <= ts <= after

    def test_creates_parent_dirs(self, tmp_path):
        hb = tmp_path / "nested" / "dir" / "test.heartbeat"
        _write_heartbeat(hb)
        assert hb.exists()

    def test_overwrites_existing(self, tmp_path):
        hb = tmp_path / "test.heartbeat"
        hb.write_text("old")
        _write_heartbeat(hb)
        content = hb.read_text().strip()
        assert content != "old"
        float(content)  # Should be valid float


class TestWriteCheckpoint:
    """Tests for _write_checkpoint atomic JSON writes."""

    def test_writes_valid_json(self, tmp_path):
        ckpt = tmp_path / "test.checkpoint.json"
        _write_checkpoint(ckpt, "test-bot", "completed")
        data = json.loads(ckpt.read_text())
        assert data["bot"] == "test-bot"
        assert data["reason"] == "completed"
        assert "updated_at" in data

    def test_truncates_long_reason(self, tmp_path):
        ckpt = tmp_path / "test.checkpoint.json"
        long_reason = "x" * 5000
        _write_checkpoint(ckpt, "bot", long_reason)
        raw = ckpt.read_text()
        assert len(raw.encode("utf-8")) <= 4096
        data = json.loads(raw)
        assert len(data["reason"]) <= 200

    def test_creates_parent_dirs(self, tmp_path):
        ckpt = tmp_path / "sub" / "dir" / "ckpt.json"
        _write_checkpoint(ckpt, "bot", "test")
        assert ckpt.exists()


class TestExecuteTool:
    """Tests for _execute_tool dispatch."""

    def test_unknown_tool_returns_error(self):
        result = _execute_tool("nonexistent_tool_xyz", {})
        assert result["success"] is False
        assert "unknown tool" in result["error"]

    def test_read_tool_dispatched(self, tmp_path):
        import codebot.api_tools as api_tools
        workspace = tmp_path
        with patch.object(api_tools, "WORKSPACE_ROOT", workspace):
            target = tmp_path / "hello.txt"
            target.write_text("world")
            result = _execute_tool("read", {"path": str(target)})
            assert result["success"] is True
            assert "world" in result["output"]

    def test_write_tool_dispatched(self, tmp_path):
        import codebot.api_tools as api_tools
        workspace = tmp_path
        with patch.object(api_tools, "WORKSPACE_ROOT", workspace):
            target = tmp_path / "out.txt"
            result = _execute_tool("write", {"path": str(target), "content": "data"})
            assert result["success"] is True
            assert target.read_text() == "data"

    def test_bad_args_returns_error(self):
        # 'read' requires 'path'; passing wrong args should fail gracefully
        result = _execute_tool("read", {"wrong_param": 123})
        assert result["success"] is False
        assert "bad args" in result["error"] or "required" in result["error"].lower() or "missing" in result["error"].lower()

    def test_heartbeat_injected_for_heartbeat_writes(self, tmp_path):
        hb = tmp_path / "bot.heartbeat"
        result = _execute_tool("write", {"path": str(hb), "content": "123"})
        assert result.get("_heartbeat_injected") is True
        assert result["success"] is True


class TestWriteBotStatus:
    """Tests for _write_bot_status output schema."""

    def test_writes_valid_schema(self, tmp_path):
        _write_bot_status(
            bot_name="test-bot",
            state_dir=tmp_path,
            current_task="testing",
            task_description="Running unit tests",
            files_touched=["a.py", "b.py"],
            iteration=5,
        )
        status_file = tmp_path / "test-bot.status.json"
        assert status_file.exists()
        data = json.loads(status_file.read_text())
        assert data["bot"] == "test-bot"
        assert data["current_task"] == "testing"
        assert data["iteration"] == 5
        assert "updated_at" in data
        assert "updated_at_human" in data

    def test_truncates_long_description(self, tmp_path):
        long_desc = "A" * 1000
        _write_bot_status(
            bot_name="bot",
            state_dir=tmp_path,
            current_task="work",
            task_description=long_desc,
        )
        data = json.loads((tmp_path / "bot.status.json").read_text())
        assert len(data["task_description"]) <= 500

    def test_caps_files_touched_at_10(self, tmp_path):
        many_files = [f"file_{i}.py" for i in range(20)]
        _write_bot_status(
            bot_name="bot",
            state_dir=tmp_path,
            current_task="work",
            files_touched=many_files,
        )
        data = json.loads((tmp_path / "bot.status.json").read_text())
        assert len(data["files_touched"]) == 10
        # Should keep the LAST 10
        assert data["files_touched"][0] == "file_10.py"

    def test_handles_none_files_touched(self, tmp_path):
        _write_bot_status(
            bot_name="bot",
            state_dir=tmp_path,
            current_task="idle",
            files_touched=None,
        )
        data = json.loads((tmp_path / "bot.status.json").read_text())
        assert data["files_touched"] == []


class TestAutoCommitGatekeeperFailClosed:
    """Tests for _auto_commit gatekeeper fail-closed behavior.

    CB-2195514-9B12: When the gatekeeper cannot be imported or raises an
    exception, _auto_commit MUST block the commit (fail-closed), not silently
    allow it (fail-open). The previous behavior violated Constitution §4
    (Central Quality Gate) and GAP-2.
    """

    def _make_adapter(self, tmp_path):
        """Create a mock adapter with paths() returning tmp_path-based dirs."""
        adapter = MagicMock()
        adapter.paths.return_value = MagicMock(
            state_dir=tmp_path / "state",
            quality_policy=tmp_path / "policy.yaml",
            repository_root=tmp_path,
        )
        adapter.paths.return_value.state_dir.mkdir(parents=True, exist_ok=True)
        return adapter

    def test_gatekeeper_import_error_blocks_commit(self, tmp_path):
        """When gatekeeper module cannot be imported, commit must be blocked."""
        adapter = self._make_adapter(tmp_path)
        # Create a file that would be committed
        (tmp_path / "Monitor-Manager-Python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Monitor-Manager-Python" / "test.py").write_text("x")
        # Mock git to succeed
        with patch("codebot.api_runner._adapter_instance", adapter), \
             patch("codebot.api_runner.WORK_ROOT", tmp_path), \
             patch("builtins.__import__", side_effect=ImportError("no module")), \
             patch("codebot.api_runner.bash", return_value={"success": True, "output": "M test.py", "error": ""}) as mock_bash:
            _auto_commit("test-bot", ["Monitor-Manager-Python/test.py"])
            # bash should NOT have been called for git add/commit since gatekeeper import failed
            for call in mock_bash.call_args_list:
                cmd = call[0][0]
                assert "git add" not in cmd, f"git add should not execute when gatekeeper import fails: {cmd}"

    def test_gatekeeper_exception_blocks_commit(self, tmp_path):
        """When gatekeeper raises an exception, commit must be blocked."""
        adapter = self._make_adapter(tmp_path)
        (tmp_path / "Monitor-Manager-Python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Monitor-Manager-Python" / "test.py").write_text("x")
        mock_gk_cls = MagicMock()
        mock_gk_cls.return_value.verify_ticket.side_effect = RuntimeError("gatekeeper crashed")
        with patch("codebot.api_runner._adapter_instance", adapter), \
             patch("codebot.api_runner.WORK_ROOT", tmp_path), \
             patch("codebot.gatekeeper.Gatekeeper", mock_gk_cls), \
             patch("codebot.api_runner.bash", return_value={"success": True, "output": "M test.py", "error": ""}) as mock_bash:
            _auto_commit("test-bot", ["Monitor-Manager-Python/test.py"])
            # bash should NOT have been called for git add/commit since gatekeeper crashed
            for call in mock_bash.call_args_list:
                cmd = call[0][0]
                assert "git add" not in cmd, f"git add should not execute when gatekeeper crashes: {cmd}"

    def test_gatekeeper_rework_decision_blocks_commit(self, tmp_path):
        """When gatekeeper returns REWORK decision, commit must be blocked."""
        adapter = self._make_adapter(tmp_path)
        (tmp_path / "Monitor-Manager-Python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Monitor-Manager-Python" / "test.py").write_text("x")
        mock_gk_cls = MagicMock()
        mock_gk_cls.return_value.verify_ticket.return_value = {
            "decision": "REWORK",
            "failed_gates": ["pytest"],
            "passed": False,
        }
        with patch("codebot.api_runner._adapter_instance", adapter), \
             patch("codebot.api_runner.WORK_ROOT", tmp_path), \
             patch("codebot.gatekeeper.Gatekeeper", mock_gk_cls), \
             patch("codebot.api_runner.bash", return_value={"success": True, "output": "M test.py", "error": ""}) as mock_bash:
            _auto_commit("test-bot", ["Monitor-Manager-Python/test.py"])
            # bash should NOT have been called for git add since gatekeeper returned REWORK
            for call in mock_bash.call_args_list:
                cmd = call[0][0]
                assert "git add" not in cmd, f"git add should not execute when gatekeeper returns REWORK: {cmd}"

    def test_gatekeeper_complete_decision_allows_commit(self, tmp_path):
        """When gatekeeper returns COMPLETE, commit proceeds normally."""
        adapter = self._make_adapter(tmp_path)
        (tmp_path / "Monitor-Manager-Python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Monitor-Manager-Python" / "test.py").write_text("x")
        mock_gk_cls = MagicMock()
        mock_gk_cls.return_value.verify_ticket.return_value = {
            "decision": "COMPLETE",
            "failed_gates": [],
            "passed": True,
        }
        with patch("codebot.api_runner._adapter_instance", adapter), \
             patch("codebot.api_runner.WORK_ROOT", tmp_path), \
             patch("codebot.gatekeeper.Gatekeeper", mock_gk_cls), \
             patch("codebot.api_runner.bash", return_value={"success": True, "output": "M test.py", "error": ""}) as mock_bash:
            _auto_commit("test-bot", ["Monitor-Manager-Python/test.py"])
            has_git_add = any("add" in (call[0][0] if call[0] else "") for call in mock_bash.call_args_list)
            assert has_git_add, "git add should execute when gatekeeper returns COMPLETE"
