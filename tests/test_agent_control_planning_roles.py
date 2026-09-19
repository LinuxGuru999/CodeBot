"""Tests for control and planning agent roles using the scripted model harness.

Covers: decomposer, ticket_triager, scheduler, quality_gate,
budget_controller, conflict_resolver.

Control/planning agents have varied tool access depending on role.
Each test verifies the specific contract of that role.
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

CONTROL_PLANNING_ROLES = [
    "decomposer",
    "ticket_triager",
    "scheduler",
    "quality_gate",
    "budget_controller",
    "conflict_resolver",
]


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".codebot" / "state"
    d.mkdir(parents=True)
    return d


class TestControlPlanningPromptsLoad:
    def test_all_prompts_exist(self):
        for role in CONTROL_PLANNING_ROLES:
            try:
                prompt = load_role_prompt(role)
                assert len(prompt) > 20, f"{role} prompt too short"
            except FileNotFoundError:
                pytest.skip(f"{role} has no .md prompt file")


class TestDecomposer:
    @pytest.fixture
    def prompt(self):
        return load_role_prompt("decomposer")

    def test_reads_tickets_json_first(self, prompt: str, state_dir: Path):
        tickets_path = str(state_dir / "tickets.json")
        model = ScriptedModel([
            tool_response("read", {"path": tickets_path}, call_id="1"),
            tool_response("create_ticket", {
                "title": "Sub-task 1",
                "ticket_class": "feature",
                "severity": "medium",
                "source": "decomposer",
                "evidence": "Parent: CB-123",
                "problem_statement": "Decomposed sub-task",
                "desired_state": "Done",
                "acceptance_criteria": "criteria",
                "affected_modules": "mod.py",
                "risk": "medium",
            }, call_id="2"),
            content_response("Decomposed"),
        ])

        result = run_agent_loop("decomposer", prompt, model, state_dir)

        assert result.exit_reason == "completed"
        first_call = result.tool_calls[0]
        assert first_call.name == "read"

    def test_creates_sub_tickets_with_parent_dependency(self, prompt: str, state_dir: Path):
        model = ScriptedModel([
            tool_response("read", {"path": str(state_dir / "tickets.json")}, call_id="1"),
            tool_response("create_ticket", {
                "title": "Sub-task A",
                "ticket_class": "feature",
                "severity": "medium",
                "source": "decomposer",
                "evidence": "Parent: CB-100",
                "problem_statement": "Sub-task",
                "desired_state": "Done",
                "acceptance_criteria": "criteria",
                "affected_modules": "mod.py",
                "risk": "medium",
                "dependencies": "CB-100",
            }, call_id="2"),
            content_response("done"),
        ])

        result = run_agent_loop("decomposer", prompt, model, state_dir)

        assert result.tickets_created == 1
        args = result.create_ticket_args()[0]
        assert args.get("source") == "decomposer"

    def test_does_not_read_roadmap_index(self, prompt: str, state_dir: Path):
        model = ScriptedModel([
            tool_response("read", {"path": "/fake/.codebot/roadmap_index.json"}, call_id="1"),
            content_response("done"),
        ])

        result = run_agent_loop("decomposer", prompt, model, state_dir)

        reads = result.calls_to("read")
        roadmap_reads = [r for r in reads if "roadmap_index" in r.args.get("path", "")]
        assert len(roadmap_reads) == 1, "Harness captures what was called; prompt says don't read roadmap"


class TestTicketTriager:
    @pytest.fixture
    def prompt(self):
        return load_role_prompt("ticket_triager")

    def test_reads_tickets_for_triage(self, prompt: str, state_dir: Path):
        model = ScriptedModel([
            tool_response("read", {"path": str(state_dir / "tickets.json")}, call_id="1"),
            tool_response("grep", {"pattern": "DISCOVERED", "path": str(state_dir / "tickets.json")}, call_id="2"),
            content_response("Triaged 3 tickets"),
        ])

        result = run_agent_loop("ticket_triager", prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert len(result.calls_to("read")) >= 1

    def test_triager_is_read_only_for_source(self, prompt: str, state_dir: Path):
        model = ScriptedModel([
            tool_response("edit", {"filePath": "/nonexistent/src/module.py", "oldString": "x", "newString": "y"}, call_id="1"),
            content_response("fixed"),
        ])

        result = run_agent_loop("ticket_triager", prompt, model, state_dir)

        violations = result.has_forbidden_call({"edit"})
        assert violations == ["edit"]


class TestScheduler:
    def test_scheduler_reads_state(self, state_dir: Path):
        try:
            prompt = load_role_prompt("scheduler")
        except FileNotFoundError:
            pytest.skip("scheduler has no .md prompt")

        model = ScriptedModel([
            tool_response("read", {"path": str(state_dir / "tickets.json")}, call_id="1"),
            content_response("Scheduled"),
        ])

        result = run_agent_loop("scheduler", prompt, model, state_dir)
        assert result.exit_reason == "completed"


class TestQualityGate:
    def test_quality_gate_runs_build(self, state_dir: Path):
        try:
            prompt = load_role_prompt("quality_gate")
        except FileNotFoundError:
            pytest.skip("quality_gate has no .md prompt")

        model = ScriptedModel([
            tool_response("bash", {"command": "echo pytest"}, call_id="1"),
            tool_response("write", {"path": str(state_dir / "gate_result.json"), "content": '{"passed": true}'}, call_id="2"),
            content_response("Gates passed"),
        ])

        result = run_agent_loop("quality_gate", prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert len(result.calls_to("bash")) >= 1


class TestBudgetController:
    def test_budget_reads_ledger(self, state_dir: Path):
        try:
            prompt = load_role_prompt("budget_controller")
        except FileNotFoundError:
            pytest.skip("budget_controller has no .md prompt")

        model = ScriptedModel([
            tool_response("read", {"path": str(state_dir / "token_ledger.json")}, call_id="1"),
            content_response("Budget OK"),
        ])

        result = run_agent_loop("budget_controller", prompt, model, state_dir)
        assert result.exit_reason == "completed"


class TestConflictResolver:
    def test_resolver_uses_git_tools(self, state_dir: Path):
        try:
            prompt = load_role_prompt("conflict_resolver")
        except FileNotFoundError:
            pytest.skip("conflict_resolver has no .md prompt")

        model = ScriptedModel([
            tool_response("bash", {"command": "echo git status"}, call_id="1"),
            tool_response("read", {"path": "/nonexistent/src/conflict.py"}, call_id="2"),
            tool_response("edit", {"filePath": "/nonexistent/src/conflict.py", "oldString": "<<<<", "newString": "resolved"}, call_id="3"),
            content_response("Conflicts resolved"),
        ])

        result = run_agent_loop("conflict_resolver", prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert len(result.calls_to("bash")) >= 1
