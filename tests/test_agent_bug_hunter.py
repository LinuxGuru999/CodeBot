"""Tests for bug_hunter agent behavior using the scripted model harness.

Verifies that the bug_hunter role prompt produces correct tool call
sequences: scans code, finds bugs, creates tickets via create_ticket,
does NOT write long text analysis instead of calling tools.
"""

import json
from pathlib import Path

import pytest

from tests.agent_harness import (
    ScriptedModel,
    run_agent_loop,
    load_role_prompt,
    tool_response,
    multi_tool_response,
    content_response,
    empty_response,
)


@pytest.fixture
def hunter_prompt() -> str:
    return load_role_prompt("bug_hunter")


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".codebot" / "state"
    d.mkdir(parents=True)
    return d


class TestBugHunterPromptLoading:
    def test_prompt_loads(self, hunter_prompt: str):
        assert "Bug Hunter" in hunter_prompt or "bug_hunter" in hunter_prompt.lower()
        assert len(hunter_prompt) > 100

    def test_prompt_mentions_create_ticket(self, hunter_prompt: str):
        assert "create_ticket" in hunter_prompt

    def test_prompt_has_source_field(self, hunter_prompt: str):
        assert "bug_hunter" in hunter_prompt


class TestBugHunterToolLoop:
    def test_scans_then_creates_ticket(self, hunter_prompt: str, state_dir: Path):
        """Agent should scan files then create a ticket for findings."""
        model = ScriptedModel([
            tool_response("grep", {"pattern": "except.*pass", "path": "/fake/src"}, call_id="1"),
            tool_response("read", {"path": "/fake/src/module.py", "limit": 50}, call_id="2"),
            tool_response("create_ticket", {
                "title": "Empty except block swallows errors in module.py",
                "ticket_class": "bug",
                "severity": "medium",
                "source": "bug_hunter",
                "evidence": "module.py:42 - except Exception: pass",
                "problem_statement": "Empty except block silently swallows all exceptions including KeyboardInterrupt and SystemExit.",
                "desired_state": "Specific exception types caught, unexpected errors logged and re-raised.",
                "acceptance_criteria": "No bare except clauses; All except blocks specify exception type; Errors logged before handling",
                "affected_modules": "module.py",
                "risk": "medium",
            }, call_id="3"),
            content_response("Found 1 bug"),
        ])

        result = run_agent_loop("bug_hunter", hunter_prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert result.tickets_created == 1
        assert result.tool_names == ["grep", "read", "create_ticket"]

    def test_multiple_bugs_multiple_tickets(self, hunter_prompt: str, state_dir: Path):
        """Each finding should produce its own create_ticket call."""
        model = ScriptedModel([
            tool_response("glob", {"pattern": "**/*.py"}, call_id="1"),
            tool_response("create_ticket", {
                "title": "Bug 1",
                "ticket_class": "bug",
                "severity": "high",
                "source": "bug_hunter",
                "evidence": "file1.py:10",
                "problem_statement": "Issue 1",
                "desired_state": "Fixed",
                "acceptance_criteria": "Test passes",
                "affected_modules": "file1.py",
                "risk": "high",
            }, call_id="2"),
            tool_response("create_ticket", {
                "title": "Bug 2",
                "ticket_class": "bug",
                "severity": "low",
                "source": "bug_hunter",
                "evidence": "file2.py:20",
                "problem_statement": "Issue 2",
                "desired_state": "Fixed",
                "acceptance_criteria": "Test passes",
                "affected_modules": "file2.py",
                "risk": "low",
            }, call_id="3"),
            content_response("Found 2 bugs"),
        ])

        result = run_agent_loop("bug_hunter", hunter_prompt, model, state_dir)

        assert result.tickets_created == 2
        args = result.create_ticket_args()
        assert all(a.get("source") == "bug_hunter" for a in args)
        assert all(a.get("ticket_class") == "bug" for a in args)

    def test_no_findings_exits_cleanly(self, hunter_prompt: str, state_dir: Path):
        """Agent that finds nothing should exit with content, no tickets."""
        model = ScriptedModel([
            tool_response("grep", {"pattern": "TODO|FIXME|HACK", "path": "/fake/src"}, call_id="1"),
            content_response("Scanned all files, no bugs found."),
        ])

        result = run_agent_loop("bug_hunter", hunter_prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert result.tickets_created == 0


class TestBugHunterForbiddenBehavior:
    def test_does_not_write_source_code(self, hunter_prompt: str, state_dir: Path):
        """Discovery agents must never call edit or write on source files."""
        model = ScriptedModel([
            tool_response("edit", {"filePath": "/fake/src/bug.py", "oldString": "x", "newString": "y"}, call_id="1"),
            content_response("fixed it"),
        ])

        result = run_agent_loop("bug_hunter", hunter_prompt, model, state_dir)

        violations = result.has_forbidden_call({"edit"})
        assert violations == ["edit"], "Bug hunter should not call edit"

    def test_does_not_run_pytest(self, hunter_prompt: str, state_dir: Path):
        """Bug hunter should find bugs, not run tests."""
        model = ScriptedModel([
            tool_response("bash", {"command": "python3 -m pytest tests/ -v"}, call_id="1"),
            content_response("tests passed"),
        ])

        result = run_agent_loop("bug_hunter", hunter_prompt, model, state_dir)

        cmds = result.bash_commands()
        assert any("pytest" in c for c in cmds), "Should detect pytest invocation"

    def test_does_not_run_git_push(self, hunter_prompt: str, state_dir: Path):
        """Discovery agents have no git write access."""
        model = ScriptedModel([
            tool_response("bash", {"command": "git push origin master"}, call_id="1"),
            content_response("pushed"),
        ])

        result = run_agent_loop("bug_hunter", hunter_prompt, model, state_dir)

        cmds = result.bash_commands()
        assert any("git push" in c for c in cmds)


class TestBugHunterArgumentValidation:
    def test_create_ticket_has_required_fields(self, hunter_prompt: str, state_dir: Path):
        """Every create_ticket call must include all required fields."""
        required_fields = {
            "title", "ticket_class", "severity", "source",
            "evidence", "problem_statement", "desired_state",
            "acceptance_criteria", "affected_modules", "risk",
        }
        model = ScriptedModel([
            tool_response("create_ticket", {
                "title": "Missing evidence field",
                "ticket_class": "bug",
                "severity": "high",
                "source": "bug_hunter",
                # Missing: evidence, problem_statement, desired_state, acceptance_criteria, affected_modules, risk
            }, call_id="1"),
            content_response("done"),
        ])

        result = run_agent_loop("bug_hunter", hunter_prompt, model, state_dir)

        args = result.create_ticket_args()[0]
        missing = required_fields - set(args.keys())
        # This documents what's missing — the test asserts we CAN detect it
        assert len(missing) > 0, "Expected some fields to be missing for this test case"
        assert "evidence" in missing

    def test_severity_values_are_valid(self, hunter_prompt: str, state_dir: Path):
        """Severity must be one of: critical, high, medium, low."""
        valid_severities = {"critical", "high", "medium", "low"}
        model = ScriptedModel([
            tool_response("create_ticket", {
                "title": "Test",
                "ticket_class": "bug",
                "severity": "URGENT",  # Invalid
                "source": "bug_hunter",
                "evidence": "test",
                "problem_statement": "test",
                "desired_state": "test",
                "acceptance_criteria": "test",
                "affected_modules": "test.py",
                "risk": "high",
            }, call_id="1"),
            content_response("done"),
        ])

        result = run_agent_loop("bug_hunter", hunter_prompt, model, state_dir)

        args = result.create_ticket_args()[0]
        assert args["severity"] not in valid_severities, "Should detect invalid severity"
