"""Tests for all implementation agent roles using the scripted model harness.

Covers: general_implementer, backend_implementer, frontend_implementer,
test_implementer, migration_implementer, documentation_implementer.

Implementation agents share invariants:
- CAN call read, write, edit, grep, glob, bash
- Follow claim → implement → test → commit protocol
- Must not weaken acceptance criteria
- Source field on any created tickets must match role name
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
)

IMPL_ROLES = [
    "general_implementer",
    "backend_implementer",
    "frontend_implementer",
    "test_implementer",
    "migration_implementer",
    "documentation_implementer",
]


@pytest.fixture(params=IMPL_ROLES)
def impl_role(request):
    return request.param


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".codebot" / "state"
    d.mkdir(parents=True)
    claims = d / "claims"
    claims.mkdir(exist_ok=True)
    return d


class TestImplPromptsLoad:
    def test_all_prompts_exist(self):
        for role in IMPL_ROLES:
            prompt = load_role_prompt(role)
            assert len(prompt) > 50, f"{role} prompt too short"

    def test_all_prompts_mention_claim_protocol(self):
        for role in IMPL_ROLES:
            prompt = load_role_prompt(role)
            lower = prompt.lower()
            assert "claim" in lower or "ticket" in lower, f"{role} missing claim/ticket protocol"


class TestImplToolLoop:
    def test_read_edit_write_cycle(self, impl_role: str, state_dir: Path):
        prompt = load_role_prompt(impl_role)
        model = ScriptedModel([
            tool_response("read", {"path": "/nonexistent/src/module.py"}, call_id="1"),
            tool_response("edit", {"filePath": "/nonexistent/src/module.py", "oldString": "x", "newString": "y"}, call_id="2"),
            tool_response("bash", {"command": "python3 -m pytest tests/test_module.py -q"}, call_id="3"),
            content_response("Implementation complete"),
        ])

        result = run_agent_loop(impl_role, prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert result.iterations == 3
        assert "read" in result.tool_names
        assert "edit" in result.tool_names

    def test_write_new_file(self, impl_role: str, state_dir: Path):
        prompt = load_role_prompt(impl_role)
        model = ScriptedModel([
            tool_response("write", {"filePath": "/nonexistent/src/new_module.py", "content": "def foo(): pass"}, call_id="1"),
            content_response("Created new file"),
        ])

        result = run_agent_loop(impl_role, prompt, model, state_dir)

        assert result.exit_reason == "completed"
        write_calls = result.calls_to("write")
        assert len(write_calls) >= 1

    def test_parallel_reads(self, impl_role: str, state_dir: Path):
        prompt = load_role_prompt(impl_role)
        model = ScriptedModel([
            multi_tool_response([
                ("read", {"path": "/nonexistent/src/a.py"}),
                ("read", {"path": "/nonexistent/src/b.py"}),
            ]),
            content_response("Read both files"),
        ])

        result = run_agent_loop(impl_role, prompt, model, state_dir)

        assert result.iterations == 1
        assert result.tool_call_count == 2

    def test_files_touched_tracking(self, impl_role: str, state_dir: Path):
        prompt = load_role_prompt(impl_role)
        model = ScriptedModel([
            tool_response("edit", {"filePath": "/nonexistent/src/module.py", "oldString": "a", "newString": "b"}, call_id="1"),
            tool_response("write", {"filePath": "/nonexistent/src/new.py", "content": "x"}, call_id="2"),
            content_response("done"),
        ])

        result = run_agent_loop(impl_role, prompt, model, state_dir)

        assert "/nonexistent/src/module.py" in result.files_touched
        assert "/nonexistent/src/new.py" in result.files_touched


class TestImplClaimProtocol:
    def test_claim_file_creation(self, impl_role: str, state_dir: Path):
        prompt = load_role_prompt(impl_role)
        claim_path = str(state_dir / "claims" / "CB-123.json")
        model = ScriptedModel([
            tool_response("write", {"path": claim_path, "content": '{"ticket_id":"CB-123"}'}, call_id="1"),
            tool_response("read", {"path": "/nonexistent/src/module.py"}, call_id="2"),
            content_response("claimed and done"),
        ])

        result = run_agent_loop(impl_role, prompt, model, state_dir)

        write_calls = result.calls_to("write")
        assert len(write_calls) >= 1


class TestImplForbiddenBehavior:
    def test_implementers_can_use_bash(self, impl_role: str, state_dir: Path):
        prompt = load_role_prompt(impl_role)
        model = ScriptedModel([
            tool_response("bash", {"command": "echo pytest"}, call_id="1"),
            content_response("done"),
        ])

        result = run_agent_loop(impl_role, prompt, model, state_dir)

        assert result.calls_to("bash") is not None

    def test_implementers_can_edit(self, impl_role: str, state_dir: Path):
        prompt = load_role_prompt(impl_role)
        model = ScriptedModel([
            tool_response("edit", {"filePath": "/x.py", "oldString": "a", "newString": "b"}, call_id="1"),
            content_response("edited"),
        ])

        result = run_agent_loop(impl_role, prompt, model, state_dir)

        assert len(result.calls_to("edit")) == 1


class TestTestImplementerSpecific:
    def test_test_implementer_writes_tests(self, state_dir: Path):
        prompt = load_role_prompt("test_implementer")
        model = ScriptedModel([
            tool_response("read", {"path": "/nonexistent/src/module.py"}, call_id="1"),
            tool_response("write", {"filePath": "/nonexistent/tests/test_module.py", "content": "def test_x(): assert True"}, call_id="2"),
            tool_response("bash", {"command": "echo pytest"}, call_id="3"),
            content_response("Tests written"),
        ])

        result = run_agent_loop("test_implementer", prompt, model, state_dir)

        assert result.exit_reason == "completed"
        assert result.tickets_created == 0
        assert len(result.calls_to("write")) >= 1


class TestDocumentationImplementerSpecific:
    def test_doc_implementer_writes_markdown(self, state_dir: Path):
        prompt = load_role_prompt("documentation_implementer")
        model = ScriptedModel([
            tool_response("read", {"path": "/nonexistent/docs/ARCHITECTURE.md"}, call_id="1"),
            tool_response("edit", {"filePath": "/nonexistent/docs/ARCHITECTURE.md", "oldString": "old", "newString": "new"}, call_id="2"),
            content_response("Docs updated"),
        ])

        result = run_agent_loop("documentation_implementer", prompt, model, state_dir)

        assert result.exit_reason == "completed"
