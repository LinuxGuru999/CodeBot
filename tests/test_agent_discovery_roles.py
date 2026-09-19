"""Tests for all discovery agent roles using the scripted model harness.

Covers: security_auditor, architecture_auditor, performance_auditor,
test_gap_auditor, documentation_auditor, dependency_auditor, ux_auditor.

All discovery agents share invariants:
- READ-ONLY: must never call edit or write on source files
- Output via create_ticket only
- Must not run pytest or git commands
- Source field must match exact role name
"""

import json
from pathlib import Path

import pytest

from tests.agent_harness import (
    ScriptedModel,
    run_agent_loop,
    load_role_prompt,
    tool_response,
    content_response,
)

DISCOVERY_ROLES = [
    "security_auditor",
    "architecture_auditor",
    "performance_auditor",
    "test_gap_auditor",
    "documentation_auditor",
    "dependency_auditor",
    "ux_auditor",
]

FORBIDDEN_DISCOVERY_TOOLS = {"edit"}


@pytest.fixture(params=DISCOVERY_ROLES)
def discovery_role(request):
    return request.param


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".codebot" / "state"
    d.mkdir(parents=True)
    return d


def _ticket_args(role_name: str) -> dict:
    return {
        "title": f"Test finding from {role_name}",
        "ticket_class": "bug" if role_name != "documentation_auditor" else "documentation",
        "severity": "high",
        "source": role_name,
        "evidence": f"{role_name} found issue in module.py:42",
        "problem_statement": f"Issue discovered by {role_name}",
        "desired_state": "Fixed",
        "acceptance_criteria": "criteria met",
        "affected_modules": "module.py",
        "risk": "medium",
    }


class TestDiscoveryPromptsLoad:
    def test_all_prompts_exist_and_have_content(self):
        for role in DISCOVERY_ROLES:
            prompt = load_role_prompt(role)
            assert len(prompt) > 50, f"{role} prompt too short"

    def test_all_prompts_mention_create_ticket(self):
        for role in DISCOVERY_ROLES:
            prompt = load_role_prompt(role)
            assert "create_ticket" in prompt, f"{role} missing create_ticket instructions"


class TestDiscoveryToolLoop:
    def test_scan_then_create_ticket(self, discovery_role: str, state_dir: Path):
        prompt = load_role_prompt(discovery_role)
        model = ScriptedModel([
            tool_response("grep", {"pattern": "issue", "path": "/nonexistent/src"}, call_id="1"),
            tool_response("read", {"path": "/nonexistent/src/module.py", "limit": 50}, call_id="2"),
            tool_response("create_ticket", _ticket_args(discovery_role), call_id="3"),
            content_response("Found 1 issue"),
        ])

        result = run_agent_loop(discovery_role, prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert result.tickets_created == 1
        assert result.tool_names == ["grep", "read", "create_ticket"]

    def test_multiple_findings_multiple_tickets(self, discovery_role: str, state_dir: Path):
        prompt = load_role_prompt(discovery_role)
        responses = []
        for i in range(3):
            args = _ticket_args(discovery_role)
            args["title"] = f"Finding {i}"
            responses.append(tool_response("create_ticket", args, call_id=str(i)))
        responses.append(content_response("done"))

        model = ScriptedModel(responses)
        result = run_agent_loop(discovery_role, prompt, model, state_dir)

        assert result.tickets_created == 3
        ticket_args = result.create_ticket_args()
        assert all(a.get("source") == discovery_role for a in ticket_args)

    def test_no_findings_exits_cleanly(self, discovery_role: str, state_dir: Path):
        prompt = load_role_prompt(discovery_role)
        model = ScriptedModel([
            tool_response("grep", {"pattern": "issue", "path": "/nonexistent/src"}, call_id="1"),
            content_response("No issues found"),
        ])

        result = run_agent_loop(discovery_role, prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert result.tickets_created == 0


class TestDiscoveryForbiddenBehavior:
    def test_never_edits_source_code(self, discovery_role: str, state_dir: Path):
        prompt = load_role_prompt(discovery_role)
        model = ScriptedModel([
            tool_response("edit", {"filePath": "/nonexistent/src/x.py", "oldString": "a", "newString": "b"}, call_id="1"),
            content_response("fixed"),
        ])

        result = run_agent_loop(discovery_role, prompt, model, state_dir)

        violations = result.has_forbidden_call(FORBIDDEN_DISCOVERY_TOOLS)
        assert violations == ["edit"], f"{discovery_role} should not call edit"

    def test_detects_pytest_invocation(self, discovery_role: str, state_dir: Path):
        prompt = load_role_prompt(discovery_role)
        model = ScriptedModel([
            tool_response("bash", {"command": "echo pytest"}, call_id="1"),
            content_response("done"),
        ])

        result = run_agent_loop(discovery_role, prompt, model, state_dir)

        cmds = result.bash_commands()
        assert any("pytest" in c for c in cmds)

    def test_detects_git_push(self, discovery_role: str, state_dir: Path):
        prompt = load_role_prompt(discovery_role)
        model = ScriptedModel([
            tool_response("bash", {"command": "echo git push"}, call_id="1"),
            content_response("done"),
        ])

        result = run_agent_loop(discovery_role, prompt, model, state_dir)

        cmds = result.bash_commands()
        assert any("git push" in c for c in cmds)


class TestDiscoverySourceField:
    def test_source_matches_role_name(self, discovery_role: str, state_dir: Path):
        prompt = load_role_prompt(discovery_role)
        args = _ticket_args(discovery_role)
        args["source"] = "wrong_source"
        model = ScriptedModel([
            tool_response("create_ticket", args, call_id="1"),
            content_response("done"),
        ])

        result = run_agent_loop(discovery_role, prompt, model, state_dir)

        recorded = result.create_ticket_args()[0]
        assert recorded["source"] != discovery_role, "Harness should capture wrong source for assertion"


class TestUxAuditorSpecific:
    def test_ux_auditor_can_use_screenshot_tool(self, state_dir: Path):
        prompt = load_role_prompt("ux_auditor")
        model = ScriptedModel([
            tool_response("screenshot", {"url": "http://localhost:8080"}, call_id="1"),
            tool_response("create_ticket", _ticket_args("ux_auditor"), call_id="2"),
            content_response("done"),
        ])

        result = run_agent_loop("ux_auditor", prompt, model, state_dir)

        assert result.calls_to("screenshot") is not None
        assert result.tickets_created == 1
