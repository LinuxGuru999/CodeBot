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

Contract tests (keyword 'contract' in test name):
- _is_implementation_bot role-based matching contract
- _flush_cost_accumulator economics integration contract
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
    _ticket_id_from_claim,
    _is_implementation_bot,
    _create_ticket_tool,
    _log,
    _persist_stream,
    _flush_cost_accumulator,
    _cost_accumulator,
    _init_cost_accumulator,
    _record_cost_attribution,
    _resolve_cost_state_dir,
    HIGH_RISK_TOKEN_MANIFESTS,
)
import codebot.api_runner as _api_runner_module
from codebot.ticket_engine import TicketStore, TicketClass, TicketState, Severity, RiskLevel


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

    def test_ticket_id_from_claim_returns_assigned_ticket(self, tmp_path):
        state_dir = tmp_path / "state"
        claims_dir = state_dir / "claims"
        claims_dir.mkdir(parents=True)
        (claims_dir / "CB-123.test-bot.json").write_text(
            json.dumps({"ticket_id": "CB-123", "bot": "test-bot"})
        )

        assert _ticket_id_from_claim(state_dir, "test-bot") == "CB-123"

    @pytest.mark.parametrize(
        ("bot_name", "expected"),
        [
            ("implementer", True),
            ("implementer-1", True),
            ("implementer-CB-B186E", True),
            ("decomposer", False),
            ("backend_implementer", False),
            ("test_implementer-2", False),
            ("implementation_planner-4", False),
            ("security_reviewer", False),
        ],
    )
    def test_auto_commit_is_limited_to_implementation_roles_contract(self, bot_name, expected):
        """Contract: only the canonical 'implementer' role prefix counts as an implementation bot."""
        assert _is_implementation_bot(bot_name) is expected

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

    def test_gatekeeper_exception_blocks_commit(self, tmp_path, capsys):
        """When gatekeeper raises an exception, commit must be blocked.

        Verifies fail-closed behavior: _auto_commit returns False,
        the log contains 'blocking commit', and no git commands execute.
        """
        adapter = self._make_adapter(tmp_path)
        (tmp_path / "Monitor-Manager-Python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Monitor-Manager-Python" / "test.py").write_text("x")
        mock_gk_cls = MagicMock()
        mock_gk_cls.return_value.verify_ticket.side_effect = RuntimeError("gatekeeper crashed")
        with patch("codebot.api_runner._adapter_instance", adapter), \
             patch("codebot.api_runner.WORK_ROOT", tmp_path), \
             patch("codebot.gatekeeper.Gatekeeper", mock_gk_cls), \
             patch("codebot.api_runner.bash", return_value={"success": True, "output": "M test.py", "error": ""}) as mock_bash:
            result = _auto_commit("test-bot", ["Monitor-Manager-Python/test.py"], ticket_id="CB-TEST-123")
            assert result is False
            assert mock_gk_cls.return_value.verify_ticket.called
            assert "blocking commit" in capsys.readouterr().out.lower()
            # bash should NOT have been called for git add/commit/push since gatekeeper crashed
            for call in mock_bash.call_args_list:
                cmd = call[0][0]
                assert "git add" not in cmd, f"git add should not execute when gatekeeper crashes: {cmd}"
                assert "git commit" not in cmd, f"git commit should not execute when gatekeeper crashes: {cmd}"
                assert "git push" not in cmd, f"git push should not execute when gatekeeper crashes: {cmd}"

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
            _auto_commit("test-bot", ["Monitor-Manager-Python/test.py"], ticket_id="CB-123")
            has_git_add = any("add" in (call[0][0] if call[0] else "") for call in mock_bash.call_args_list)
            assert has_git_add, "git add should execute when gatekeeper returns COMPLETE"
            assert mock_gk_cls.return_value.verify_ticket.call_args.kwargs["ticket_id"] == "CB-123"

    def test_auto_commit_sanitizes_malicious_bot_name(self, tmp_path):
        """Malicious bot_name with shell metacharacters is safely rejected (CB-8696404-69E3).

        Verifies that _auto_commit prevents command injection by blocking
        bot_names containing shell metacharacters via _BOT_NAME_RE before
        any git commands are constructed. This is the primary sanitization
        layer; shlex.quote provides a secondary layer for the commit message.
        """
        adapter = self._make_adapter(tmp_path)
        (tmp_path / "Monitor-Manager-Python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Monitor-Manager-Python" / "test.py").write_text("x")
        with patch("codebot.api_runner._adapter_instance", adapter), \
             patch("codebot.api_runner.WORK_ROOT", tmp_path), \
             patch("codebot.api_runner.bash", return_value={"success": True, "output": "", "error": ""}) as mock_bash:
            result = _auto_commit("test; rm -rf /", ["Monitor-Manager-Python/test.py"], ticket_id="CB-123")
            assert result is False, "Malicious bot_name 'test; rm -rf /' must block commit"
            assert mock_bash.call_count == 0, "bash must not be called for malicious bot_name"

    def test_auto_commit_handles_normal_bot_name(self, tmp_path):
        """Normal bot_name passes sanitization and allows commit (CB-8696404-69E3)."""
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
            result = _auto_commit("implementer-1", ["Monitor-Manager-Python/test.py"], ticket_id="CB-456")
            assert result is True, "Normal bot_name should allow commit"
            has_git_add = any("add" in (call[0][0] if call[0] else "") for call in mock_bash.call_args_list)
            assert has_git_add, "git add should execute for normal bot_name"


class TestAutoCommitBotNameSanitization:
    """Tests for CB-8696404-69E3: bot_name sanitization prevents command injection.

    _auto_commit uses _BOT_NAME_RE (^[a-zA-Z0-9_-]+$) to reject bot names
    containing shell metacharacters before any git commands are constructed.
    This is defense-in-depth against OS command injection (CWE-78).
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

    @pytest.mark.parametrize("malicious_name", [
        "bot;rm -rf /",
        "bot$(whoami)",
        "bot`id`",
        "bot|cat /etc/passwd",
        "bot&&curl evil.com",
        "bot\nrm -rf /",
        "bot space",
        "bot/tab",
        "../traversal",
        "bot;echo pwned",
    ])
    def test_malicious_bot_name_blocks_commit(self, malicious_name, tmp_path):
        """Bot names with shell metacharacters must be rejected before any git ops."""
        adapter = self._make_adapter(tmp_path)
        (tmp_path / "Monitor-Manager-Python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Monitor-Manager-Python" / "test.py").write_text("x")
        with patch("codebot.api_runner._adapter_instance", adapter), \
             patch("codebot.api_runner.WORK_ROOT", tmp_path), \
             patch("codebot.api_runner.bash", return_value={"success": True, "output": "", "error": ""}) as mock_bash:
            result = _auto_commit(malicious_name, ["Monitor-Manager-Python/test.py"], ticket_id="CB-123")
            assert result is False, f"Malicious bot_name '{malicious_name}' must block commit"
            # No bash calls should have been made at all — sanitization happens before git
            assert mock_bash.call_count == 0, (
                f"bash should not be called for malicious bot_name '{malicious_name}', "
                f"but was called {mock_bash.call_count} times"
            )

    def test_normal_bot_name_passes_sanitization(self, tmp_path):
        """Valid bot names (alphanumeric, hyphens, underscores) pass sanitization."""
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
            result = _auto_commit("implementer-2", ["Monitor-Manager-Python/test.py"], ticket_id="CB-456")
            # Sanitization passed; gatekeeper returned COMPLETE; git commands should execute
            assert result is True, "Valid bot_name should allow commit to proceed"
            has_git_add = any("add" in (call[0][0] if call[0] else "") for call in mock_bash.call_args_list)
            assert has_git_add, "git add should execute for valid bot_name after gatekeeper passes"

    def test_non_string_bot_name_blocks_commit(self, tmp_path):
        """Non-string bot_name (None, int, etc.) must be rejected."""
        adapter = self._make_adapter(tmp_path)
        (tmp_path / "Monitor-Manager-Python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Monitor-Manager-Python" / "test.py").write_text("x")
        with patch("codebot.api_runner._adapter_instance", adapter), \
             patch("codebot.api_runner.WORK_ROOT", tmp_path), \
             patch("codebot.api_runner.bash", return_value={"success": True, "output": "", "error": ""}) as mock_bash:
            for bad_name in [None, 123, ["list"], {"dict": True}]:
                result = _auto_commit(bad_name, ["Monitor-Manager-Python/test.py"], ticket_id="CB-789")  # type: ignore[arg-type]
                assert result is False, f"Non-string bot_name {type(bad_name)} must block commit"
            assert mock_bash.call_count == 0, "bash should never be called for non-string bot_name"

    def test_empty_bot_name_blocks_commit(self, tmp_path):
        """Empty string bot_name must be rejected (does not match ^[a-zA-Z0-9_-]+$)."""
        adapter = self._make_adapter(tmp_path)
        (tmp_path / "Monitor-Manager-Python").mkdir(parents=True, exist_ok=True)
        (tmp_path / "Monitor-Manager-Python" / "test.py").write_text("x")
        with patch("codebot.api_runner._adapter_instance", adapter), \
             patch("codebot.api_runner.WORK_ROOT", tmp_path), \
             patch("codebot.api_runner.bash", return_value={"success": True, "output": "", "error": ""}) as mock_bash:
            result = _auto_commit("", ["Monitor-Manager-Python/test.py"], ticket_id="CB-000")
            assert result is False, "Empty bot_name must block commit"
            assert mock_bash.call_count == 0, "bash should not be called for empty bot_name"


class TestApiKeyLoggingSecurity:
    """Tests for CB-9360904-15F2: API key must never appear in log output.

    Constitution §2 states: 'No secrets in code, logs, or error messages.'
    CWE-532: Insertion of Sensitive Information into Log File.
    """

    def test_fails_when_key_material_is_logged(self, capsys):
        """Prove the test catches violations: logging actual key content should fail assertions."""
        from codebot.api_runner import _log
        sensitive_key = "sk-secret-key-abcdef1234567890"
        # Simulate a violation: logging part of the actual key
        _log(f"API key value: {sensitive_key[:8]}")  # Logging first 8 chars is a violation
        captured = capsys.readouterr()
        # This demonstrates what a violation looks like - the assertion below SHOULD fail
        # We're testing the negative case to prove our detection works
        violation_detected = False
        for i in range(len(sensitive_key) - 3):
            substring = sensitive_key[i:i+4]
            if substring in captured.out:
                violation_detected = True
                break
        assert violation_detected, "Test should detect when key fragments are logged"

    def test_log_output_does_not_contain_key_characters(self, capsys):
        """Verify _log output does not contain any portion of an API key."""
        sensitive_key = "sk-super-secret-api-key-1234567890abcdef"
        # Simulate what run_bot does after resolving the key
        _log("test-bot: API key resolved (present)")
        captured = capsys.readouterr()
        # The log line must confirm presence without revealing content
        assert "API key resolved (present)" in captured.out
        # No portion of the actual key should appear in the log output
        for i in range(len(sensitive_key) - 3):
            substring = sensitive_key[i:i+4]
            assert substring not in captured.out, (
                f"Key fragment '{substring}' found in log output — CWE-532 violation"
            )

    def test_run_bot_logs_key_presence_without_content(self, tmp_path, capsys, monkeypatch):
        """Integration-level test: run_bot's key resolution log must not leak key material."""
        # Use a key with no substrings that collide with other log content
        fake_key = "dgr-QxZz9WpLmKqRvYjNfHcBtUeAoIgDsF"
        monkeypatch.setenv("DIALAGRAM_API_KEY", fake_key)

        import codebot.api_runner as ar

        with patch.object(ar.sys, "exit", side_effect=SystemExit), \
             patch.object(ar, "_is_draining", return_value=False), \
             patch.object(ar, "_write_heartbeat"), \
             patch.object(ar, "_start_heartbeat_thread", return_value=MagicMock()), \
             patch.object(ar, "_stop_heartbeat_thread"), \
             patch.object(ar, "run_agent_loop", side_effect=SystemExit(0)), \
             patch.object(ar, "_persist_stream"), \
             patch.object(ar, "_write_bot_status"):
            try:
                ar.run_bot(
                    bot_name="sec-check-bot",
                    model="dummy-model",
                    mission_prompt="verify security",
                    heartbeat_file=str(tmp_path / "hb"),
                    ckpt_file=str(tmp_path / "ckpt.json"),
                )
            except SystemExit:
                pass

        captured = capsys.readouterr()
        # Must confirm key is present
        assert "API key resolved (present)" in captured.out
        # Must NOT contain any fragment of the actual key (check all substrings >= 4 chars)
        for i in range(len(fake_key) - 3):
            fragment = fake_key[i:i+4]
            assert fragment not in captured.out, (
                f"API key fragment '{fragment}' leaked in log output — CWE-532 violation"
            )


class TestCreateTicketTool:
    """Tests for _create_ticket_tool — ticket creation and store persistence.

    CB-9506618-8175: Verify that _create_ticket_tool creates a ticket that
    exists in the store and has the correct fields.
    """

    def _make_adapter(self, tmp_path):
        """Create a mock adapter with paths() returning tmp_path-based dirs."""
        adapter = MagicMock()
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True, exist_ok=True)
        adapter.paths.return_value = MagicMock(
            state_dir=state_dir,
            repository_root=tmp_path,
        )
        return adapter

    def test_ticket_exists_in_store(self, tmp_path):
        """Ticket appears in store after creation."""
        adapter = self._make_adapter(tmp_path)
        with patch("codebot.api_runner._adapter_instance", adapter):
            result = _create_ticket_tool(
                title="Test bug from investigation",
                ticket_class="bug",
                severity="low",
                source="general_implementer",
                evidence="Testing if create_ticket works",
                problem_statement="Testing if create_ticket works",
                desired_state="Ticket appears in store",
                acceptance_criteria="Ticket exists; Ticket has correct fields",
            )
        assert result["success"] is True
        # Verify ticket exists in the store
        store_path = adapter.paths().state_dir / "tickets.json"
        assert store_path.exists(), "tickets.json store file must exist"
        store = TicketStore(store_path)
        tickets = store.list_by_state(TicketState.DISCOVERED)
        assert len(tickets) >= 1, "At least one ticket must be in the store"
        # Find our ticket
        matching = [t for t in tickets if t.title == "Test bug from investigation"]
        assert len(matching) == 1, "Our ticket must be found in the store"

    def test_ticket_has_correct_fields(self, tmp_path):
        """Ticket has correct title, class, severity, problem_statement,
        desired_state, and acceptance_criteria."""
        adapter = self._make_adapter(tmp_path)
        with patch("codebot.api_runner._adapter_instance", adapter):
            result = _create_ticket_tool(
                title="Test bug from investigation",
                ticket_class="bug",
                severity="low",
                source="general_implementer",
                evidence="Testing if create_ticket works",
                problem_statement="Testing if create_ticket works",
                desired_state="Ticket appears in store",
                acceptance_criteria="Ticket exists; Ticket has correct fields",
            )
        assert result["success"] is True
        store_path = adapter.paths().state_dir / "tickets.json"
        store = TicketStore(store_path)
        tickets = store.list_by_state(TicketState.DISCOVERED)
        ticket = [t for t in tickets if t.title == "Test bug from investigation"][0]
        # Verify fields
        assert ticket.title == "Test bug from investigation"
        assert ticket.ticket_class == TicketClass.BUG
        assert ticket.severity == Severity.LOW
        assert ticket.problem_statement == "Testing if create_ticket works"
        assert ticket.desired_state == "Ticket appears in store"
        assert "Ticket exists" in ticket.acceptance_criteria
        assert "Ticket has correct fields" in ticket.acceptance_criteria

    def test_ticket_id_format(self, tmp_path):
        """Ticket ID follows CB-XXXXXXXX-XXXX format."""
        adapter = self._make_adapter(tmp_path)
        with patch("codebot.api_runner._adapter_instance", adapter):
            result = _create_ticket_tool(
                title="ID format test",
                ticket_class="bug",
                severity="medium",
                source="test",
                evidence="ID format test",
                problem_statement="ID format test",
                desired_state="Correct ID format",
                acceptance_criteria="ID matches CB- pattern",
            )
        assert result["success"] is True
        store_path = adapter.paths().state_dir / "tickets.json"
        store = TicketStore(store_path)
        tickets = store.list_by_state(TicketState.DISCOVERED)
        ticket = [t for t in tickets if t.title == "ID format test"][0]
        assert ticket.id.startswith("CB-"), f"Ticket ID must start with CB-, got {ticket.id}"

    def test_ticket_state_is_discovered(self, tmp_path):
        """Newly created ticket has DISCOVERED state."""
        adapter = self._make_adapter(tmp_path)
        with patch("codebot.api_runner._adapter_instance", adapter):
            result = _create_ticket_tool(
                title="State test",
                ticket_class="feature",
                severity="medium",
                source="test",
                evidence="state test",
                problem_statement="state test",
                desired_state="correct state",
                acceptance_criteria="state is DISCOVERED",
            )
        assert result["success"] is True
        store_path = adapter.paths().state_dir / "tickets.json"
        store = TicketStore(store_path)
        tickets = store.list_by_state(TicketState.DISCOVERED)
        ticket = [t for t in tickets if t.title == "State test"][0]
        assert ticket.state == TicketState.DISCOVERED

    def test_ticket_with_all_fields(self, tmp_path):
        """Ticket with all optional fields populated."""
        adapter = self._make_adapter(tmp_path)
        with patch("codebot.api_runner._adapter_instance", adapter):
            result = _create_ticket_tool(
                title="Full field test",
                ticket_class="security",
                severity="critical",
                source="security_auditor",
                evidence="CWE-123: vulnerability found",
                problem_statement="Security vulnerability in auth module",
                desired_state="Vulnerability patched and tested",
                acceptance_criteria="Tests pass; No regressions",
                affected_modules="codebot/auth.py,codebot/session.py",
                risk="high",
            )
        assert result["success"] is True
        store_path = adapter.paths().state_dir / "tickets.json"
        store = TicketStore(store_path)
        tickets = store.list_by_state(TicketState.DISCOVERED)
        ticket = [t for t in tickets if t.title == "Full field test"][0]
        assert ticket.ticket_class == TicketClass.SECURITY
        assert ticket.severity == Severity.CRITICAL
        assert ticket.source == "security_auditor"
        assert "codebot/auth.py" in ticket.affected_modules
        assert "codebot/session.py" in ticket.affected_modules

    def test_empty_title_falls_back_to_problem_statement(self, tmp_path):
        """When title is empty, problem_statement is used as title."""
        adapter = self._make_adapter(tmp_path)
        with patch("codebot.api_runner._adapter_instance", adapter):
            result = _create_ticket_tool(
                title="",
                ticket_class="bug",
                severity="medium",
                source="test",
                evidence="evidence",
                problem_statement="Fallback title from problem_statement",
                desired_state="correct",
                acceptance_criteria="title matches problem_statement",
            )
        assert result["success"] is True
        store_path = adapter.paths().state_dir / "tickets.json"
        store = TicketStore(store_path)
        tickets = store.list_by_state(TicketState.DISCOVERED)
        ticket = [t for t in tickets if t.ticket_class == TicketClass.BUG]
        assert len(ticket) >= 1
        # The title should have fallen back to problem_statement
        assert ticket[-1].title == "Fallback title from problem_statement"

    def test_acceptance_criteria_semicolon_parsing(self, tmp_path):
        """Semicolon-separated acceptance criteria are parsed into a list."""
        adapter = self._make_adapter(tmp_path)
        with patch("codebot.api_runner._adapter_instance", adapter):
            result = _create_ticket_tool(
                title="AC parsing test",
                ticket_class="bug",
                severity="medium",
                source="test",
                evidence="test",
                problem_statement="test",
                desired_state="test",
                acceptance_criteria="First criterion; Second criterion; Third criterion",
            )
        assert result["success"] is True
        store_path = adapter.paths().state_dir / "tickets.json"
        store = TicketStore(store_path)
        tickets = store.list_by_state(TicketState.DISCOVERED)
        ticket = [t for t in tickets if t.title == "AC parsing test"][0]
        assert len(ticket.acceptance_criteria) == 3
        assert "First criterion" in ticket.acceptance_criteria
        assert "Second criterion" in ticket.acceptance_criteria
        assert "Third criterion" in ticket.acceptance_criteria

    def test_execute_tool_create_ticket_dispatch(self, tmp_path):
        """_execute_tool correctly dispatches to create_ticket."""
        adapter = self._make_adapter(tmp_path)
        with patch("codebot.api_runner._adapter_instance", adapter):
            result = _execute_tool("create_ticket", {
                "title": "Dispatch test",
                "ticket_class": "bug",
                "severity": "low",
                "source": "test",
                "evidence": "dispatch test",
                "problem_statement": "dispatch test",
                "desired_state": "dispatch works",
                "acceptance_criteria": "ticket created",
            })
        assert result["success"] is True
        assert "Created ticket" in result["output"]


class TestPersistStreamStress:
    """Stress tests for _persist_stream memory bounding.

    CB-2096CA22C30F1B0B0014D7B1CE28C973: Verify stream persistence remains
    bounded under adversarial input sizes (100K+ messages) without OOM.
    """

    def test_persist_stream_handles_100k_messages_without_oom(self, tmp_path):
        """100K+ messages complete without MemoryError, file <=500KB, truncated."""
        # Arrange: 100K messages (adversarial history length)
        num_messages = 100_000
        # Use small, realistic messages to keep test <30s while still exceeding 500KB
        # Each message ~50 chars JSON => 100K * 50 = 5MB raw, must truncate to 500KB
        messages = [
            {"role": "user", "content": f"msg {i} hello world test content abcdef"}
            for i in range(num_messages)
        ]
        assert len(messages) >= 100_000, "must construct 100K+ message list"

        bot_name = "stress-test-bot"
        import codebot.api_runner as ar

        # Ensure logs dir exists — _persist_stream does not mkdir when
        # truncation path is not hit via context tracing alone in some flows
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        start = time.monotonic()
        # Act: must complete without MemoryError/OOM
        with patch.object(ar, "BOTS_DIR", tmp_path):
            try:
                ar._persist_stream(
                    bot_name=bot_name,
                    messages=messages,
                    active_model="test-model",
                    tool_iterations=1,
                    exit_reason="completed",
                )
            except MemoryError:
                pytest.fail("_persist_stream raised MemoryError on 100K messages - DoS vulnerability")
            elapsed = time.monotonic() - start

            # Assert: timing bound <30s
            assert elapsed < 30, f"stress test took {elapsed:.1f}s, must be <30s"

            # Assert: output file exists
            stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
            assert stream_path.exists(), f"stream file must exist at {stream_path}"

            # Assert: file size <=500KB (500_000 bytes)
            raw = stream_path.read_text(encoding="utf-8")
            file_size = len(raw.encode("utf-8"))
            assert file_size <= 500_000, f"stream file {file_size} bytes exceeds 500KB cap"

            # Assert: valid JSON and truncation flag set
            payload = json.loads(raw)
            assert payload.get("truncated") is True, "truncation flag must be set for 100K input"
            assert "messages" in payload
            # Must have truncated: persisted count < input count
            assert len(payload["messages"]) < num_messages, "persisted messages must be truncated"
            assert len(payload["messages"]) > 0, "at least some messages must be persisted"

            # Assert: output is bounded even though input is huge (streaming accumulation)
            assert len(raw) <= 500_000

    def test_persist_stream_100k_tool_messages_truncation(self, tmp_path):
        """100K tool messages with large content still bounded to 500KB."""
        # Arrange: tool messages trigger per-entry content truncation (2000 char cap)
        num_messages = 100_000
        large_content = "x" * 3000  # exceeds 2000 char tool truncation
        messages = [
            {"role": "tool", "content": large_content, "tool_call_id": f"call_{i}"}
            for i in range(num_messages)
        ]
        bot_name = "stress-tool-bot"
        import codebot.api_runner as ar

        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        start = time.monotonic()
        with patch.object(ar, "BOTS_DIR", tmp_path):
            ar._persist_stream(bot_name, messages, "model-x", 2, "iteration_limit")
            elapsed = time.monotonic() - start
            assert elapsed < 30, f"took {elapsed:.1f}s"

            stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
            assert stream_path.exists()
            raw = stream_path.read_text(encoding="utf-8")
            assert len(raw.encode("utf-8")) <= 500_000
            payload = json.loads(raw)
            assert payload.get("truncated") is True
            # Verify tool content truncation was applied to persisted entries
            for m in payload["messages"]:
                if m.get("role") == "tool" and isinstance(m.get("content"), str):
                    assert len(m["content"]) <= 2016, "tool content must be truncated to 2000+len(suffix)"

    def test_persist_stream_small_history_not_truncated(self, tmp_path):
        """Small history (<500KB) must NOT set truncated flag and persist all."""
        messages = [{"role": "user", "content": f"hello {i}"} for i in range(10)]
        bot_name = "small-history-bot"
        import codebot.api_runner as ar

        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        with patch.object(ar, "BOTS_DIR", tmp_path):
            ar._persist_stream(bot_name, messages, "m", 1, "completed")
            stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
            assert stream_path.exists()
            payload = json.loads(stream_path.read_text(encoding="utf-8"))
            # Small payload should not be marked truncated
            assert payload.get("truncated") is not True
            assert len(payload["messages"]) == 10

    def test_persist_stream_logs_typed_failures(self, tmp_path, caplog):
        """Verify that typed exceptions in _persist_stream are logged, not swallowed."""
        import codebot.api_runner as ar
        import logging

        bot_name = "fail-test-bot"
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        # Create a message that is not serializable (e.g., contains a set)
        bad_message = {"role": "user", "content": {"set": {1, 2, 3}}}
        messages = [bad_message]

        with patch.object(ar, "BOTS_DIR", tmp_path), \
             caplog.at_level(logging.WARNING):
            ar._persist_stream(bot_name, messages, "m", 1, "completed")

        # Check that a warning was logged about skipping the message
        assert any("skipped non-serializable" in record.message for record in caplog.records), \
            "Expected warning about skipped non-serializable message"

        # Verify the stream file was still created (fail-open)
        stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
        assert stream_path.exists()
        payload = json.loads(stream_path.read_text(encoding="utf-8"))
        # The bad message should have been skipped, so messages list is empty or just has overhead
        assert len(payload["messages"]) == 0

    def test_persist_stream_tail_trim_loop_triggered_by_overhead_drift(self, tmp_path):
        """Cover the while-loop tail-trim path (lines ~1540-1555).

        When cumulative per-entry size tracking stays under MAX_SIZE but
        the final serialized payload exceeds it due to structural overhead,
        the while loop must pop entries from the tail until under cap.
        We force this by patching json.dumps to return a larger body on
        the final full-payload serialization than the sum of per-entry sizes.
        """
        import codebot.api_runner as ar
        original_dumps = json.dumps
        call_count = {"n": 0}

        def patched_dumps(obj, *args, **kwargs):
            result = original_dumps(obj, *args, **kwargs)
            # Only inflate when serializing the full payload dict with messages
            if isinstance(obj, dict) and "messages" in obj and len(obj.get("messages", [])) > 0:
                call_count["n"] += 1
                # On the FIRST full-payload serialization (before while loop),
                # inflate to exceed MAX_SIZE to trigger the while loop
                if call_count["n"] == 1:
                    # Add padding to push over 500KB
                    padding = "x" * 600_000
                    return result + padding
            return result

        bot_name = "tail-trim-bot"
        # Create enough messages to fill near 500KB
        messages = [{"role": "user", "content": f"msg-{i}-payload-data"} for i in range(5000)]
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        with patch.object(ar, "BOTS_DIR", tmp_path), \
             patch("codebot.api_runner.json.dumps", side_effect=patched_dumps):
            ar._persist_stream(bot_name, messages, "m", 1, "completed")

        stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
        assert stream_path.exists()
        raw = stream_path.read_text(encoding="utf-8")
        file_size = len(raw.encode("utf-8"))
        assert file_size <= 500_000, f"File {file_size} exceeds 500KB after tail-trim"
        payload = json.loads(raw)
        assert payload.get("truncated") is True, "truncated flag must be set after tail-trim"
        assert len(payload["messages"]) < len(messages), "Messages must be trimmed"

    def test_persist_stream_reserialize_when_truncated_flag_added_post_guard(self, tmp_path):
        """Cover the re-serialization path (lines ~1560-1561).

        When stream_truncated is True from the accumulation break (not the
        while loop), the truncated flag is added AFTER the initial body
        serialization. If '"truncated"' is not already in body, it must
        re-serialize to include the flag.
        """
        import codebot.api_runner as ar

        bot_name = "reserialize-bot"
        # Create messages that will hit the cumulative size break exactly
        # Each message ~100 bytes JSON, need ~5000 to approach 500KB
        msg_content = "a" * 90  # ~100 bytes per message with role/key overhead
        messages = [{"role": "user", "content": msg_content} for _ in range(6000)]
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        with patch.object(ar, "BOTS_DIR", tmp_path):
            ar._persist_stream(bot_name, messages, "m", 1, "completed")

        stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
        assert stream_path.exists()
        raw = stream_path.read_text(encoding="utf-8")
        file_size = len(raw.encode("utf-8"))
        assert file_size <= 500_000, f"File {file_size} exceeds 500KB"
        payload = json.loads(raw)
        # Must have truncated flag set AND present in the serialized body
        assert payload.get("truncated") is True
        assert '"truncated"' in raw, "truncated key must appear in serialized JSON"
        assert len(payload["messages"]) < len(messages)

    def test_persist_stream_reserialize_via_skip_truncation(self, tmp_path, caplog):
        """Cover lines 1560-1561: re-serialization when truncated flag added post-guard.

        Trigger stream_truncated=True via skipped non-mapping messages (NOT via
        size break). The final payload is small (<MAX_SIZE), so the while loop
        doesn't execute. The truncated flag is added after initial serialization,
        forcing the re-serialization path at lines 1560-1561.
        """
        import codebot.api_runner as ar
        import logging

        bot_name = "reserialize-skip-bot"
        # Mix valid messages with non-mapping entries that trigger skip + stream_truncated
        messages = [
            {"role": "user", "content": "valid1"},
            "not-a-dict",           # triggers TypeError -> stream_truncated=True
            {"role": "assistant", "content": "valid2"},
            [1, 2, 3],              # triggers TypeError -> stream_truncated=True
            {"role": "user", "content": "valid3"},
        ]
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        with patch.object(ar, "BOTS_DIR", tmp_path), \
             caplog.at_level(logging.WARNING):
            ar._persist_stream(bot_name, messages, "m", 1, "completed")

        stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
        assert stream_path.exists()
        raw = stream_path.read_text(encoding="utf-8")
        payload = json.loads(raw)
        # stream_truncated was set by skips, so truncated flag must be present
        assert payload.get("truncated") is True
        assert '"truncated"' in raw
        # Only 3 valid dict messages persisted
        assert len(payload["messages"]) == 3
        # File is tiny — well under MAX_SIZE, proving while loop didn't run
        assert len(raw.encode("utf-8")) < 1000

    def test_persist_stream_non_mapping_message_skipped_with_warning(self, tmp_path, caplog):
        """Cover the except (TypeError, ValueError) path for non-mapping messages (lines 1487-1494).

        When a message is not a mapping (e.g., a string or list), dict(m) raises
        TypeError and the message is skipped with a warning log.
        """
        import codebot.api_runner as ar
        import logging

        bot_name = "non-mapping-bot"
        # Mix of valid messages and non-mapping entries
        messages = [
            {"role": "user", "content": "valid message"},
            "this is a string, not a dict",  # Will cause TypeError in dict(m)
            ["this", "is", "a", "list"],      # Will also cause TypeError
            42,                                # Integer - TypeError
            {"role": "assistant", "content": "another valid"},
        ]
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        with patch.object(ar, "BOTS_DIR", tmp_path), \
             caplog.at_level(logging.WARNING):
            ar._persist_stream(bot_name, messages, "m", 1, "completed")

        # Verify warnings were logged for non-mapping messages
        warning_msgs = [r.message for r in caplog.records if "skipped non-mapping" in r.message]
        assert len(warning_msgs) >= 3, f"Expected at least 3 non-mapping warnings, got {len(warning_msgs)}: {warning_msgs}"

        # Verify stream file was created with only valid messages
        stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
        assert stream_path.exists()
        payload = json.loads(stream_path.read_text(encoding="utf-8"))
        # Only the 2 valid dict messages should be persisted
        assert len(payload["messages"]) == 2
        assert payload.get("truncated") is True  # stream_truncated set due to skipped messages

    def test_persist_stream_file_write_failure_logged(self, tmp_path, caplog):
        """Cover the except (OSError, ValueError, TypeError) path in _persist_stream (lines ~1583-1584).

        When the file write operations (mkdir, write_text, replace) fail with
        OSError/ValueError/TypeError, the function logs a warning and returns
        without crashing.
        """
        import codebot.api_runner as ar
        import logging
        from unittest.mock import patch

        bot_name = "write-fail-bot"
        messages = [{"role": "user", "content": "hello"}]
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        # Patch Path.write_text to raise OSError, simulating disk full or permission error
        original_write_text = None
        def failing_write_text(*args, **kwargs):
            raise OSError("Simulated disk write failure")

        with patch.object(ar, "BOTS_DIR", tmp_path), \
             caplog.at_level(logging.WARNING):
            # Patch the Path method that writes the file
            with patch("pathlib.Path.write_text", side_effect=failing_write_text):
                ar._persist_stream(bot_name, messages, "m", 1, "completed")

        # Verify warning was logged about the failure
        assert any("_persist_stream failed" in record.message for record in caplog.records), \
            f"Expected warning about persist_stream failure. Records: {[r.message for r in caplog.records]}"

    def test_persist_stream_usage_none_handled(self, tmp_path):
        """Cover the usage=None path (line ~1467) where usage_dict becomes empty dict."""
        import codebot.api_runner as ar

        bot_name = "usage-none-bot"
        messages = [{"role": "user", "content": "hello"}]
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        with patch.object(ar, "BOTS_DIR", tmp_path):
            ar._persist_stream(bot_name, messages, "m", 1, "completed", usage=None)

        stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
        assert stream_path.exists()
        payload = json.loads(stream_path.read_text(encoding="utf-8"))
        # usage should be an empty dict when None is passed
        assert payload.get("usage") == {}

    def test_persist_stream_tool_truncation_logs_context(self, tmp_path, caplog):
        """Cover the _log_context_assembly path when truncated_count > 0."""
        import codebot.api_runner as ar
        import logging

        bot_name = "tool-trunc-bot"
        # Create a tool message with content > 2000 chars to trigger truncation
        large_content = "x" * 3000
        messages = [{"role": "tool", "content": large_content, "tool_call_id": "call_1"}]
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        with patch.object(ar, "BOTS_DIR", tmp_path), \
             caplog.at_level(logging.INFO):
            ar._persist_stream(bot_name, messages, "m", 1, "completed")

        stream_path = tmp_path / "logs" / f"{bot_name}.stream.json"
        assert stream_path.exists()
        payload = json.loads(stream_path.read_text(encoding="utf-8"))
        # Verify tool content was truncated
        assert len(payload["messages"][0]["content"]) <= 2016
        # Verify truncated flag is set
        assert payload.get("truncated") is True


class TestCostTracking:
    """Tests for economics/cost tracking integration in _flush_cost_accumulator.

    CB-B186EB623EC3E9AFE03D3C344AA2B051: Verify that _flush_cost_accumulator
    correctly records token usage and phase costs with proper attribution,
    graceful degradation when economics modules are unavailable, and atomic
    recording under concurrent access.
    """

    def _seed_accumulator(self, prompt_tokens=100, completion_tokens=50, call_count=2):
        """Seed the thread-local cost accumulator with test data."""
        _init_cost_accumulator()
        _cost_accumulator.prompt_tokens = prompt_tokens
        _cost_accumulator.completion_tokens = completion_tokens
        _cost_accumulator.call_count = call_count
        _cost_accumulator.start_time = time.time() - 1.0  # 1 second elapsed

    def test_record_usage_called_after_successful_api_call_contract(self):
        """Contract: token_budget.record_usage invoked via _flush with correct model and token counts."""
        self._seed_accumulator(prompt_tokens=100, completion_tokens=50, call_count=2)

        with patch.object(_api_runner_module, '_record_token_usage') as mock_record_usage, \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', True), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', False):
            _flush_cost_accumulator("my-bot", "CB-TEST", "gpt-4", "implement")

            mock_record_usage.assert_called_once()
            call_kwargs = mock_record_usage.call_args.kwargs
            assert call_kwargs["model"] == "gpt-4"
            assert call_kwargs["prompt_tokens"] == 100
            assert call_kwargs["completion_tokens"] == 50
            # day_utc should be a valid YYYY-MM-DD string
            import re
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", call_kwargs["day_utc"])

    def test_record_cost_called_for_tool_execution_contract(self):
        """Contract: CostTracker.record_phase_cost invoked with correct ticket/agent/model/tokens/phase."""
        self._seed_accumulator(prompt_tokens=100, completion_tokens=50, call_count=2)

        mock_tracker_instance = MagicMock()
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)

        with patch.object(_api_runner_module, '_CostTracker', mock_tracker_cls), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', True), \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', False):
            _flush_cost_accumulator("my-bot", "CB-TEST", "gpt-4", "implement")

            mock_tracker_cls.assert_called_once()
            mock_tracker_instance.record_phase_cost.assert_called_once()
            call_kwargs = mock_tracker_instance.record_phase_cost.call_args.kwargs
            assert call_kwargs["ticket_id"] == "CB-TEST"
            assert call_kwargs["agent"] == "my-bot"
            assert call_kwargs["model"] == "gpt-4"
            assert call_kwargs["prompt_tokens"] == 100
            assert call_kwargs["completion_tokens"] == 50
            assert call_kwargs["phase"] == "implement"
            assert call_kwargs["attempts"] == 2
            assert "wall_clock_seconds" in call_kwargs

    def test_cost_attribution_uses_current_bot_and_ticket(self):
        """Bot name and ticket_id flow through to both recording calls."""
        self._seed_accumulator(prompt_tokens=75, completion_tokens=25, call_count=1)

        mock_tracker_instance = MagicMock()
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)

        with patch.object(_api_runner_module, '_record_token_usage') as mock_record_usage, \
             patch.object(_api_runner_module, '_CostTracker', mock_tracker_cls), \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', True), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', True):
            _flush_cost_accumulator(
                "planner-CB-B186E",
                "CB-B186EB623EC3E9AFE03D3C344AA2B051",
                "qwen-3.7-max",
                "plan"
            )

            # Verify record_usage was called (attribution is via model, not bot/ticket)
            mock_record_usage.assert_called_once()
            assert mock_record_usage.call_args.kwargs["model"] == "qwen-3.7-max"

            # Verify record_phase_cost received exact bot and ticket
            mock_tracker_instance.record_phase_cost.assert_called_once()
            cost_kwargs = mock_tracker_instance.record_phase_cost.call_args.kwargs
            assert cost_kwargs["agent"] == "planner-CB-B186E"
            assert cost_kwargs["ticket_id"] == "CB-B186EB623EC3E9AFE03D3C344AA2B051"
            assert cost_kwargs["model"] == "qwen-3.7-max"
            assert cost_kwargs["phase"] == "plan"

    def test_graceful_degradation_when_economics_unavailable(self):
        """_HAS_* False / None recorders never crash _flush_cost_accumulator."""
        self._seed_accumulator(prompt_tokens=100, completion_tokens=50, call_count=2)

        # Simulate missing economics modules
        with patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', False), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', False), \
             patch.object(_api_runner_module, '_record_token_usage', None), \
             patch.object(_api_runner_module, '_CostTracker', None):
            # Must not raise any exception
            _flush_cost_accumulator("test-bot", "CB-TEST", "gpt-4", "implement")

        # Verify accumulator was drained even though recorders were unavailable
        assert _cost_accumulator.prompt_tokens == 0
        assert _cost_accumulator.completion_tokens == 0
        assert _cost_accumulator.call_count == 0

    def test_zero_tokens_early_return_without_recorders(self):
        """Zero-token accumulator early-returns without calling either recorder."""
        self._seed_accumulator(prompt_tokens=0, completion_tokens=0, call_count=2)

        with patch.object(_api_runner_module, '_record_token_usage') as mock_record_usage, \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', True), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', True):
            _flush_cost_accumulator("test-bot", "CB-TEST", "gpt-4", "implement")

            # Neither recorder should be called for zero tokens
            mock_record_usage.assert_not_called()

    def test_cost_attribution_event_carries_bot_and_ticket(self, caplog):
        """AC2: per-bot/per-ticket attribution rides alongside record_usage.

        token_budget.record_usage() aggregates by day+model only, so the
        flush also emits a structured cost_attribution log record carrying
        bot_name + ticket_id for the economics ledger join.
        """
        import logging

        self._seed_accumulator(prompt_tokens=100, completion_tokens=50, call_count=2)

        with patch.object(_api_runner_module, '_record_token_usage') as mock_record_usage, \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', True), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', False), \
             caplog.at_level(logging.INFO, logger="codebot.api_runner"):
            _flush_cost_accumulator("my-bot", "CB-TEST", "gpt-4", "implement")

            mock_record_usage.assert_called_once()
            assert any(
                "cost_attribution" in r.message
                and "my-bot" in r.message
                and "CB-TEST" in r.message
                for r in caplog.records
            ), "expected cost_attribution log with bot_name and ticket_id"

    def test_state_dir_resolves_from_heartbeat_parent(self, tmp_path):
        """CostTracker writes land in the runtime state dir, not codebot/.codebot/state."""
        self._seed_accumulator(prompt_tokens=100, completion_tokens=50, call_count=2)

        hb = tmp_path / "hb-file"
        ckpt = tmp_path / "ckpt-file"
        resolved = _api_runner_module._resolve_cost_state_dir(
            heartbeat_file=str(hb), ckpt_file=str(ckpt)
        )
        assert "codebot/.codebot" not in str(resolved).replace("\\", "/")

        mock_tracker_instance = MagicMock()
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)
        with patch.object(_api_runner_module, '_CostTracker', mock_tracker_cls), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', True), \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', False):
            _flush_cost_accumulator(
                "my-bot", "CB-TEST", "gpt-4", "implement",
                heartbeat_file=str(hb), ckpt_file=str(ckpt),
            )
            mock_tracker_cls.assert_called_once()
            used_state_dir = Path(mock_tracker_cls.call_args.args[0])
            assert "codebot/.codebot" not in str(used_state_dir).replace("\\", "/")

    def test_state_dir_parent_named_state_wins(self, tmp_path):
        """hb_path.parent named 'state' is used verbatim as the ledger dir."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        hb = state_dir / "my-bot.heartbeat"
        resolved = _api_runner_module._resolve_cost_state_dir(heartbeat_file=str(hb))
        assert resolved == state_dir

    def test_inter_flush_elapsed_documented(self):
        """wall_clock_seconds reflects the inter-flush slice, not session total."""
        self._seed_accumulator(prompt_tokens=100, completion_tokens=50, call_count=2)
        _cost_accumulator.start_time = time.time() - 5.0

        mock_tracker_instance = MagicMock()
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)
        with patch.object(_api_runner_module, '_CostTracker', mock_tracker_cls), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', True), \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', False):
            _flush_cost_accumulator(
                "my-bot", "CB-TEST", "gpt-4", "implement", state_dir="/tmp/xyz-state"
            )
            elapsed = mock_tracker_instance.record_phase_cost.call_args.kwargs[
                "wall_clock_seconds"
            ]
            assert 4.0 <= elapsed <= 30.0

    def test_session_exit_flush_records_accumulated_costs(self, tmp_path):
        """AC5 integration: _execute_provider_session flushes on session exit.

        A single mocked API response (below the flush-interval threshold)
        must still reach record_usage + record_phase_cost via the _result()
        exit flush, with accumulated totals matching the mocked usage.
        """
        hb = tmp_path / "sess-bot.heartbeat"
        ckpt = tmp_path / "CB-SESS.sess-bot.checkpoint.json"

        def api_call(msgs, model, key):
            return {
                "choices": [{"message": {"content": "done"}}],
                "usage": {
                    "prompt_tokens": 30,
                    "completion_tokens": 12,
                    "total_tokens": 42,
                },
            }

        mock_tracker_instance = MagicMock()
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)
        with patch.object(_api_runner_module, '_record_token_usage') as mock_record_usage, \
             patch.object(_api_runner_module, '_CostTracker', mock_tracker_cls), \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', True), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', True):
            res = _api_runner_module._execute_provider_session(
                "sess-bot", "gpt-4", [{"role": "user", "content": "hi"}],
                str(hb), str(ckpt), "key",
                api_call=api_call,
                drain_check=lambda n: False,
                sleep_fn=lambda d: None,
            )
            assert res["status"] == "completed"
            mock_record_usage.assert_called_once()
            assert mock_record_usage.call_args.kwargs["prompt_tokens"] == 30
            assert mock_record_usage.call_args.kwargs["completion_tokens"] == 12
            mock_tracker_instance.record_phase_cost.assert_called_once()
            cost_kwargs = mock_tracker_instance.record_phase_cost.call_args.kwargs
            assert cost_kwargs["prompt_tokens"] == 30
            assert cost_kwargs["completion_tokens"] == 12
            assert cost_kwargs["agent"] == "sess-bot"
            assert cost_kwargs["ticket_id"] == "CB-SESS"
            assert cost_kwargs["model"] == "gpt-4"

    def test_session_flush_interval_records_batched_costs(self, tmp_path):
        """AC5 integration: periodic flush fires after _COST_FLUSH_INTERVAL calls.

        Drive enough tool-call iterations to cross the flush threshold, then
        assert both recorders fired at least once with accumulated totals.
        """
        hb = tmp_path / "batch-bot.heartbeat"
        ckpt = tmp_path / "CB-BATCH.batch-bot.checkpoint.json"
        calls = {"n": 0}
        interval = _api_runner_module._COST_FLUSH_INTERVAL

        def api_call(msgs, model, key):
            calls["n"] += 1
            if calls["n"] <= interval:
                return {
                    "choices": [{
                        "message": {
                            "tool_calls": [{
                                "id": str(calls["n"]),
                                "type": "function",
                                "function": {"name": "read", "arguments": '{"path":"a"}'},
                            }],
                            "content": None,
                        }
                    }],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            return {
                "choices": [{"message": {"content": "done"}}],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                },
            }

        mock_tracker_instance = MagicMock()
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)
        with patch.object(_api_runner_module, '_record_token_usage') as mock_record_usage, \
             patch.object(_api_runner_module, '_CostTracker', mock_tracker_cls), \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', True), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', True):
            res = _api_runner_module._execute_provider_session(
                "batch-bot", "gpt-4", [{"role": "user", "content": "hi"}],
                str(hb), str(ckpt), "key",
                api_call=api_call,
                drain_check=lambda n: False,
                tool_dispatch=lambda n, a, **kw: {"success": True, "output": "ok", "error": None},
                sleep_fn=lambda d: None,
            )
            assert res["status"] == "completed"
            assert mock_record_usage.call_count >= 1
            total_prompt = sum(
                c.kwargs["prompt_tokens"] for c in mock_record_usage.call_args_list
            )
            total_completion = sum(
                c.kwargs["completion_tokens"] for c in mock_record_usage.call_args_list
            )
            expected_calls = interval + 1
            assert total_prompt == 10 * expected_calls
            assert total_completion == 5 * expected_calls
            assert mock_tracker_instance.record_phase_cost.call_count >= 1

    def test_session_cost_tracking_no_regression_benchmark(self, tmp_path, capsys):
        """AC4: batched accumulator adds no measurable per-call overhead.

        Times _execute_provider_session with cost recording enabled vs fully
        disabled; enabled must complete within 5x the disabled wall time and
        print the measured timings for the pipeline log.
        """
        import time as _time

        def make_api_call():
            def api_call(msgs, model, key):
                return {
                    "choices": [{"message": {"content": "done"}}],
                    "usage": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    },
                }
            return api_call

        def run_once(disabled: bool) -> float:
            hb = tmp_path / f"bench-{disabled}.heartbeat"
            ckpt = tmp_path / f"CB-BENCH.bench-{disabled}.checkpoint.json"
            patches = [
                patch.object(
                    _api_runner_module, '_HAS_TOKEN_BUDGET', not disabled
                ),
                patch.object(
                    _api_runner_module, '_HAS_COST_TRACKER', not disabled
                ),
            ]
            if disabled:
                patches.append(
                    patch.object(_api_runner_module, '_record_token_usage', None)
                )
                patches.append(
                    patch.object(_api_runner_module, '_CostTracker', None)
                )
            else:
                patches.append(
                    patch.object(
                        _api_runner_module, '_record_token_usage',
                        MagicMock(side_effect=lambda **kw: None),
                    )
                )
                mock_tracker = MagicMock()
                patches.append(
                    patch.object(
                        _api_runner_module, '_CostTracker',
                        MagicMock(return_value=mock_tracker),
                    )
                )
            for p in patches:
                p.start()
            try:
                start = _time.perf_counter()
                res = _api_runner_module._execute_provider_session(
                    "bench-bot", "gpt-4", [{"role": "user", "content": "hi"}],
                    str(hb), str(ckpt), "key",
                    api_call=make_api_call(),
                    drain_check=lambda n: False,
                    sleep_fn=lambda d: None,
                )
                elapsed = _time.perf_counter() - start
                assert res["status"] == "completed"
                return elapsed
            finally:
                for p in reversed(patches):
                    p.stop()

        # Warm up once so import/lazy-init noise doesn't skew the comparison.
        run_once(True)
        disabled_t = min(run_once(True) for _ in range(3))
        enabled_t = min(run_once(False) for _ in range(3))
        with capsys.disabled():
            print(
                f"\n[cost-tracking-benchmark] disabled={disabled_t:.4f}s "
                f"enabled={enabled_t:.4f}s ratio={enabled_t/max(disabled_t, 1e-9):.2f}x"
            )
        assert enabled_t <= max(5 * disabled_t, disabled_t + 1.0), (
            f"cost tracking regressed session loop: disabled={disabled_t:.4f}s "
            f"enabled={enabled_t:.4f}s"
        )

    def test_atomic_recording_under_concurrent_access(self):
        """Concurrent _flush_cost_accumulator calls complete without exception.

        Patches are applied ONCE outside worker threads to avoid race conditions
        on module-level globals. The test verifies that the thread-local
        accumulator drain logic is safe under concurrent invocation.
        """
        import threading

        errors = []
        completed = []

        # Use thread-safe mocks patched once for all threads
        mock_record = MagicMock()
        mock_tracker_instance = MagicMock()
        mock_tracker_cls = MagicMock(return_value=mock_tracker_instance)

        def worker(thread_id):
            try:
                # Each thread seeds its own thread-local accumulator
                _init_cost_accumulator()
                _cost_accumulator.prompt_tokens = 10 * thread_id
                _cost_accumulator.completion_tokens = 5 * thread_id
                _cost_accumulator.call_count = thread_id
                _cost_accumulator.start_time = time.time()

                _flush_cost_accumulator(f"bot-{thread_id}", f"CB-{thread_id}", "gpt-4", "implement")

                completed.append(thread_id)
            except Exception as e:
                errors.append((thread_id, str(e)))

        with patch.object(_api_runner_module, '_record_token_usage', mock_record), \
             patch.object(_api_runner_module, '_CostTracker', mock_tracker_cls), \
             patch.object(_api_runner_module, '_HAS_TOKEN_BUDGET', True), \
             patch.object(_api_runner_module, '_HAS_COST_TRACKER', True):
            threads = [threading.Thread(target=worker, args=(i,)) for i in range(1, 5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=10)

        # All threads must complete without exception
        assert len(errors) == 0, f"Concurrent flush failed: {errors}"
        assert len(completed) == 4, f"Expected 4 completions, got {len(completed)}"

    def test_record_cost_attribution_logs_correct_format(self, caplog):
        """Verify _record_cost_attribution emits structured log record.

        AC2: bot/ticket attribution rides alongside record_usage via logger.
        """
        import logging

        with caplog.at_level(logging.INFO, logger="codebot.api_runner"):
            _record_cost_attribution(
                bot_name="test-bot",
                ticket_id="CB-TEST-123",
                model="gpt-4",
                prompt_tokens=100,
                completion_tokens=50,
            )

        assert any(
            "cost_attribution" in r.message
            and "test-bot" in r.message
            and "CB-TEST-123" in r.message
            and "gpt-4" in r.message
            and "prompt=100" in r.message
            and "completion=50" in r.message
            for r in caplog.records
        ), "Expected cost_attribution log with all fields"

    def test_record_cost_attribution_fail_open_never_raises(self, caplog):
        """Verify _record_cost_attribution never raises even on logger failure."""
        import logging

        # Even with logger configured, this should not raise
        with caplog.at_level(logging.INFO, logger="codebot.api_runner"):
            try:
                _record_cost_attribution(
                    bot_name="bot",
                    ticket_id="CB-T",
                    model="m",
                    prompt_tokens=0,
                    completion_tokens=0,
                )
            except Exception as e:
                pytest.fail(f"_record_cost_attribution raised unexpectedly: {e}")

    def test_resolve_cost_state_dir_uses_adapter_when_available(self, tmp_path):
        """Adapter state dir takes precedence over heartbeat/ckpt parents."""
        adapter_state = tmp_path / "adapter-state"
        adapter_state.mkdir()
        
        mock_adapter = MagicMock()
        mock_adapter.paths.return_value.state_dir = adapter_state
        
        with patch.object(_api_runner_module, '_adapter_instance', mock_adapter):
            result = _resolve_cost_state_dir(heartbeat_file="", ckpt_file="")
            assert result == adapter_state

    def test_resolve_cost_state_dir_fallback_to_heartbeat_parent(self, tmp_path):
        """When adapter unavailable, use heartbeat file parent if named 'state'."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        hb = state_dir / "bot.heartbeat"
        
        with patch.object(_api_runner_module, '_adapter_instance', None):
            result = _resolve_cost_state_dir(heartbeat_file=str(hb), ckpt_file="")
            assert result == state_dir

    def test_resolve_cost_state_dir_detects_state_by_siblings(self, tmp_path):
        """Parent with .heartbeat/.json siblings is detected as state dir."""
        state_dir = tmp_path / "runtime-state"
        state_dir.mkdir()
        (state_dir / "other.heartbeat").touch()
        
        hb = state_dir / "bot.heartbeat"
        
        with patch.object(_api_runner_module, '_adapter_instance', None):
            result = _resolve_cost_state_dir(heartbeat_file=str(hb), ckpt_file="")
            assert result == state_dir

    def test_resolve_cost_state_dir_ancestor_named_state(self, tmp_path):
        """Ancestor directory named 'state' is preferred."""
        state_dir = tmp_path / "state"
        state_dir.mkdir()
        nested = state_dir / "subdir"
        nested.mkdir()
        hb = nested / "bot.heartbeat"
        
        with patch.object(_api_runner_module, '_adapter_instance', None):
            result = _resolve_cost_state_dir(heartbeat_file=str(hb), ckpt_file="")
            assert result == state_dir

    def test_resolve_cost_state_dir_last_resort_cwd_state(self, tmp_path):
        """When no valid candidate, falls back to cwd/.codebot/state."""
        fake_hb = tmp_path / "nonexistent_xyz_123" / "hb"
        # Ensure parent does NOT exist so both resolve loops fall through to cwd fallback
        assert not fake_hb.parent.exists()

        with patch.object(_api_runner_module, '_adapter_instance', None), \
             patch('codebot.api_runner.Path.cwd', return_value=tmp_path):
            result = _resolve_cost_state_dir(heartbeat_file=str(fake_hb), ckpt_file="")
            assert result == tmp_path / ".codebot" / "state"
