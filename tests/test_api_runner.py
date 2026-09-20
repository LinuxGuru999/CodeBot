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
    _ticket_id_from_claim,
    _is_implementation_bot,
    _create_ticket_tool,
    _log,
    HIGH_RISK_TOKEN_MANIFESTS,
)
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
            ("backend_implementer", True),
            ("test_implementer-2", True),
            ("decomposer", False),
            ("implementation_planner-4", False),
            ("security_reviewer", False),
        ],
    )
    def test_auto_commit_is_limited_to_implementation_roles(self, bot_name, expected):
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
            _auto_commit("test-bot", ["Monitor-Manager-Python/test.py"], ticket_id="CB-123")
            has_git_add = any("add" in (call[0][0] if call[0] else "") for call in mock_bash.call_args_list)
            assert has_git_add, "git add should execute when gatekeeper returns COMPLETE"
            assert mock_gk_cls.return_value.verify_ticket.call_args.kwargs["ticket_id"] == "CB-123"


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
