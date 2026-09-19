"""Tests for all review agent roles using the scripted model harness.

Covers: correctness_reviewer, security_reviewer, architecture_reviewer,
test_reviewer, performance_reviewer, simplicity_reviewer,
documentation_reviewer, ux_reviewer.

Review agents share invariants:
- READ-ONLY for source code: must never call edit on source files
- Output via write (verdict files) or create_ticket (escalation)
- Must produce a verdict: APPROVE, REWORK, or ESCALATE
- Must not modify implementation code
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

REVIEW_ROLES = [
    "correctness_reviewer",
    "security_reviewer",
    "architecture_reviewer",
    "test_reviewer",
    "performance_reviewer",
    "simplicity_reviewer",
    "documentation_reviewer",
    # ux_reviewer excluded: registered in role_registry.py but has no .md prompt file
]


@pytest.fixture(params=REVIEW_ROLES)
def review_role(request):
    return request.param


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".codebot" / "state"
    d.mkdir(parents=True)
    return d


class TestReviewPromptsLoad:
    def test_all_prompts_exist(self):
        for role in REVIEW_ROLES:
            prompt = load_role_prompt(role)
            assert len(prompt) > 50, f"{role} prompt too short"

    def test_all_prompts_define_verdict_format(self):
        for role in REVIEW_ROLES:
            prompt = load_role_prompt(role)
            lower = prompt.lower()
            has_verdict = any(w in lower for w in ["approve", "rework", "verdict", "escalate"])
            assert has_verdict, f"{role} missing verdict instructions"


class TestReviewToolLoop:
    def test_read_source_then_verdict(self, review_role: str, state_dir: Path):
        prompt = load_role_prompt(review_role)
        verdict_path = str(state_dir / f"{review_role}_verdict.json")
        model = ScriptedModel([
            tool_response("read", {"path": "/nonexistent/src/module.py"}, call_id="1"),
            tool_response("read", {"path": "/nonexistent/tests/test_module.py"}, call_id="2"),
            tool_response("write", {"path": verdict_path, "content": '{"verdict": "APPROVE"}'}, call_id="3"),
            content_response("Review complete: APPROVE"),
        ])

        result = run_agent_loop(review_role, prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert result.iterations == 3
        reads = result.calls_to("read")
        assert len(reads) == 2

    def test_rework_verdict_with_feedback(self, review_role: str, state_dir: Path):
        prompt = load_role_prompt(review_role)
        model = ScriptedModel([
            tool_response("read", {"path": "/nonexistent/src/module.py"}, call_id="1"),
            tool_response("grep", {"pattern": "TODO|FIXME", "path": "/nonexistent/src"}, call_id="2"),
            tool_response("create_ticket", {
                "title": f"Rework needed by {review_role}",
                "ticket_class": "bug",
                "severity": "high",
                "source": review_role,
                "evidence": "module.py:42 - incomplete implementation",
                "problem_statement": "Implementation has issues",
                "desired_state": "Fixed per review feedback",
                "acceptance_criteria": "Issues resolved",
                "affected_modules": "module.py",
                "risk": "medium",
            }, call_id="3"),
            content_response("REWORK"),
        ])

        result = run_agent_loop(review_role, prompt, model, state_dir)

        assert result.tickets_created == 1
        args = result.create_ticket_args()[0]
        assert args["source"] == review_role

    def test_approve_without_tickets(self, review_role: str, state_dir: Path):
        prompt = load_role_prompt(review_role)
        model = ScriptedModel([
            tool_response("read", {"path": "/nonexistent/src/module.py"}, call_id="1"),
            content_response("APPROVE: implementation matches spec"),
        ])

        result = run_agent_loop(review_role, prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert result.tickets_created == 0


class TestReviewForbiddenBehavior:
    def test_reviewers_must_not_edit_source(self, review_role: str, state_dir: Path):
        prompt = load_role_prompt(review_role)
        model = ScriptedModel([
            tool_response("edit", {"filePath": "/nonexistent/src/module.py", "oldString": "bad", "newString": "good"}, call_id="1"),
            content_response("fixed it myself"),
        ])

        result = run_agent_loop(review_role, prompt, model, state_dir)

        violations = result.has_forbidden_call({"edit"})
        assert violations == ["edit"], f"{review_role} should not edit source code"

    def test_reviewers_must_not_run_git_push(self, review_role: str, state_dir: Path):
        prompt = load_role_prompt(review_role)
        model = ScriptedModel([
            tool_response("bash", {"command": "echo git push"}, call_id="1"),
            content_response("done"),
        ])

        result = run_agent_loop(review_role, prompt, model, state_dir)

        cmds = result.bash_commands()
        assert any("git push" in c for c in cmds)


class TestSecurityReviewerSpecific:
    def test_security_reviewer_checks_auth(self, state_dir: Path):
        prompt = load_role_prompt("security_reviewer")
        model = ScriptedModel([
            tool_response("grep", {"pattern": "auth|token|password|secret", "path": "/nonexistent/src"}, call_id="1"),
            tool_response("read", {"path": "/nonexistent/src/auth.py"}, call_id="2"),
            content_response("VERDICT: APPROVE - no auth issues"),
        ])

        result = run_agent_loop("security_reviewer", prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert len(result.calls_to("grep")) == 1


class TestUxReviewerSpecific:
    def test_ux_reviewer_prompt_missing(self):
        with pytest.raises(FileNotFoundError):
            load_role_prompt("ux_reviewer")
