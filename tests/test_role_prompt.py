#!/usr/bin/env python3
"""Tests for codebot/role_prompt.py — prompt assembly, template loading, legacy mapping."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from codebot.role_prompt import (
    LEGACY_ROLE_MAP,
    assemble_prompt,
    build_project_context,
    format_ticket_context,
    load_role_template,
    resolve_role_name,
)


# ---------------------------------------------------------------------------
# resolve_role_name
# ---------------------------------------------------------------------------

class TestResolveRoleName:
    def test_legacy_name_mapped(self) -> None:
        assert resolve_role_name("issues") == "bug_hunter"

    def test_legacy_name_bug_triage(self) -> None:
        assert resolve_role_name("bug_triage") == "ticket_triager"

    def test_worker_names_mapped(self) -> None:
        assert resolve_role_name("worker-1") == "general_implementer"
        assert resolve_role_name("worker-4") == "backend_implementer"
        assert resolve_role_name("worker-7") == "frontend_implementer"
        assert resolve_role_name("worker-8") == "test_implementer"
        assert resolve_role_name("worker-9") == "migration_implementer"
        assert resolve_role_name("worker-10") == "documentation_implementer"

    def test_unknown_name_returns_itself(self) -> None:
        assert resolve_role_name("unknown_role") == "unknown_role"

    def test_canonical_role_name_passes_through(self) -> None:
        """A name that is already a canonical role name (and not in LEGACY_ROLE_MAP) passes through."""
        assert resolve_role_name("bug_hunter") == "bug_hunter"

    def test_all_legacy_roles_resolve(self) -> None:
        """Every key in LEGACY_ROLE_MAP resolves to a non-empty string."""
        for legacy, canonical in LEGACY_ROLE_MAP.items():
            result = resolve_role_name(legacy)
            assert result == canonical, f"Legacy '{legacy}' should map to '{canonical}', got '{result}'"

    def test_empty_string(self) -> None:
        assert resolve_role_name("") == ""

    def test_length_of_legacy_map(self) -> None:
        assert len(LEGACY_ROLE_MAP) > 20, "LEGACY_ROLE_MAP should have at least 20 entries"


# ---------------------------------------------------------------------------
# load_role_template
# ---------------------------------------------------------------------------

class TestLoadRoleTemplate:
    def test_existing_role_returns_nonempty(self) -> None:
        """A role that likely has a .md file should return non-empty content."""
        result = load_role_template("general_implementer")
        assert isinstance(result, str)
        assert len(result) > 50

    def test_nonexistent_role_returns_minimal_prompt(self) -> None:
        result = load_role_template("nonexistent_role_xyz_abc")
        assert "# Role: nonexistent_role_xyz_abc" in result
        assert "nonexistent_role_xyz_abc" in result

    def test_missing_role_still_has_role_header(self) -> None:
        result = load_role_template("definitely_missing_role")
        assert result.startswith("# Role:")

    def test_path_traversal_attempt_sanitized(self) -> None:
        """Slashes in role names should be replaced with underscores."""
        result = load_role_template("../../../etc/passwd")
        # Should not crash; should return minimal prompt
        assert isinstance(result, str)

    def test_backslash_sanitized(self) -> None:
        result = load_role_template("role\\with\\backslash")
        assert isinstance(result, str)

    def test_empty_role_name(self) -> None:
        result = load_role_template("")
        # Empty name: _ROLES_DIR / ".md" won't exist -> minimal prompt
        assert isinstance(result, str)


class TestRolePromptCache:
    def test_template_cache_returns_same_without_reread(self, tmp_path: Path) -> None:
        from codebot import role_prompt as rp_mod
        rp_mod.clear_role_prompt_cache()
        first = load_role_template("general_implementer")
        assert len(first) > 50
        assert "general_implementer" in rp_mod._template_cache
        second = load_role_template("general_implementer")
        assert second == first

    def test_template_cache_invalidates_on_mtime(self, tmp_path: Path) -> None:
        from codebot import role_prompt as rp_mod
        rp_mod.clear_role_prompt_cache()
        rp_mod._template_cache["general_implementer"] = (0.0, "STALE")
        result = load_role_template("general_implementer")
        assert result != "STALE"
        assert len(result) > 50

    def test_context_cache_per_adapter(self) -> None:
        from codebot import role_prompt as rp_mod
        from unittest.mock import MagicMock as _MM
        rp_mod.clear_role_prompt_cache()
        adapter = _MM()
        adapter.project_name.return_value = "P"
        adapter.paths.return_value = _MM(
            repository_root="/r", state_dir="/r/s",
            logs_dir="/r/l", docs_dir="/r/d",
            constitution_file="/r/c",
        )
        first = build_project_context(adapter)
        calls_after_first = adapter.paths.call_count
        second = build_project_context(adapter)
        assert second == first
        assert adapter.paths.call_count == calls_after_first

    def test_clear_cache(self) -> None:
        from codebot import role_prompt as rp_mod
        rp_mod._template_cache["x"] = (1.0, "y")
        rp_mod.clear_role_prompt_cache()
        assert rp_mod._template_cache == {}
        assert rp_mod._context_cache == {}


# ---------------------------------------------------------------------------
# build_project_context
# ---------------------------------------------------------------------------

class TestBuildProjectContext:
    def test_none_adapter_returns_empty(self) -> None:
        assert build_project_context(None) == ""

    def test_none_adapter_not_empty_string(self) -> None:
        result = build_project_context(None)
        assert result == ""

    def test_adapter_with_project_name(self) -> None:
        adapter = MagicMock()
        adapter.project_name.return_value = "TestProject"
        adapter.paths.return_value = MagicMock(
            repository_root="/repo",
            state_dir="/repo/state",
            logs_dir="/repo/logs",
            docs_dir="/repo/docs",
            constitution_file="/repo/constitution.md",
        )
        result = build_project_context(adapter)
        assert "## Project Context" in result
        assert "TestProject" in result

    def test_adapter_with_components(self) -> None:
        adapter = MagicMock()
        adapter.project_name.return_value = "P"
        adapter.paths.return_value = MagicMock(
            repository_root="/r", state_dir="/r/s",
            logs_dir="/r/l", docs_dir="/r/d",
            constitution_file="/r/c",
        )
        comp = MagicMock()
        comp.name = "core"
        comp.path = "/r/core"
        comp.component_type = "backend"
        comp.language = "python"
        comp.description = "Core module"
        adapter.components.return_value = [comp]
        result = build_project_context(adapter)
        assert "Components" in result
        assert "core" in result

    def test_adapter_with_test_config(self) -> None:
        adapter = MagicMock()
        adapter.project_name.return_value = "P"
        adapter.paths.return_value = MagicMock(
            repository_root="/r", state_dir="/r/s",
            logs_dir="/r/l", docs_dir="/r/d",
            constitution_file="/r/c",
        )
        tc = MagicMock()
        tc.framework = "pytest"
        tc.test_command = "pytest -q"
        tc.test_directories = ["tests/"]
        adapter.test_config.return_value = tc
        result = build_project_context(adapter)
        assert "Testing" in result
        assert "pytest" in result

    def test_adapter_with_dependency_policy(self) -> None:
        adapter = MagicMock()
        adapter.project_name.return_value = "P"
        adapter.paths.return_value = MagicMock(
            repository_root="/r", state_dir="/r/s",
            logs_dir="/r/l", docs_dir="/r/d",
            constitution_file="/r/c",
        )
        dp = MagicMock()
        dp.policy = "stdlib-only"
        dp.allowed_third_party = [{"name": "pytest"}]
        adapter.dependency_policy.return_value = dp
        result = build_project_context(adapter)
        assert "Dependencies" in result
        assert "stdlib-only" in result

    def test_adapter_with_autonomy_config(self) -> None:
        adapter = MagicMock()
        adapter.project_name.return_value = "P"
        adapter.paths.return_value = MagicMock(
            repository_root="/r", state_dir="/r/s",
            logs_dir="/r/l", docs_dir="/r/d",
            constitution_file="/r/c",
        )
        ac = MagicMock()
        ac.level = 3
        ac.human_approval_required_for = ["secrets"]
        ac.autonomous_allowed_for = ["test_additions"]
        adapter.autonomy_config.return_value = ac
        result = build_project_context(adapter)
        assert "Autonomy" in result
        assert "Level" in result

    def test_adapter_with_exception_during_paths(self) -> None:
        adapter = MagicMock()
        adapter.project_name.return_value = "P"
        adapter.paths.side_effect = RuntimeError("boom")
        result = build_project_context(adapter)
        assert "## Project Context" in result
        assert "P" in result

    def test_critical_rules_present(self) -> None:
        adapter = MagicMock()
        adapter.project_name.return_value = "P"
        adapter.paths.return_value = MagicMock(
            repository_root="/r", state_dir="/r/s",
            logs_dir="/r/l", docs_dir="/r/d",
            constitution_file="/r/c",
        )
        result = build_project_context(adapter)
        assert "Critical Rules" in result
        assert "constitution.md" in result
        assert "heartbeat" in result


# ---------------------------------------------------------------------------
# assemble_prompt
# ---------------------------------------------------------------------------

class TestAssemblePrompt:
    def test_basic_assembly(self) -> None:
        result = assemble_prompt("general_implementer")
        assert isinstance(result, str)
        assert len(result) > 0

    def test_with_ticket_context(self) -> None:
        result = assemble_prompt("bug_hunter", ticket_context="Fix the bug")
        assert "Fix the bug" in result
        assert "Current Ticket" in result

    def test_with_extra_instructions(self) -> None:
        result = assemble_prompt("bug_hunter", extra_instructions="Be careful")
        assert "Be careful" in result
        assert "Additional Instructions" in result

    def test_with_all_parts(self) -> None:
        result = assemble_prompt(
            "bug_hunter",
            ticket_context="Bug info",
            extra_instructions="Extra info",
        )
        assert "Bug info" in result
        assert "Extra info" in result
        assert "Current Ticket" in result
        assert "Additional Instructions" in result

    def test_no_ticket_no_extra(self) -> None:
        result = assemble_prompt("bug_hunter")
        assert "Current Ticket" not in result
        assert "Additional Instructions" not in result

    def test_missing_role_uses_minimal(self) -> None:
        result = assemble_prompt("nonexistent_role_xyz")
        assert "nonexistent_role_xyz" in result

    def test_with_adapter(self) -> None:
        adapter = MagicMock()
        adapter.project_name.return_value = "MyProject"
        adapter.paths.return_value = MagicMock(
            repository_root="/repo", state_dir="/repo/state",
            logs_dir="/repo/logs", docs_dir="/repo/docs",
            constitution_file="/repo/constitution.md",
        )
        result = assemble_prompt("bug_hunter", adapter=adapter)
        assert "MyProject" in result
        assert "Project Context" in result


# ---------------------------------------------------------------------------
# format_ticket_context
# ---------------------------------------------------------------------------

class TestFormatTicketContext:
    def _make_ticket(self, **overrides: Any) -> MagicMock:
        ticket = MagicMock()
        ticket.id = overrides.get("id", "CB-123456-ABCD")
        ticket.title = overrides.get("title", "Test ticket")
        ticket.ticket_class = overrides.get("ticket_class", "bug")
        ticket.severity = overrides.get("severity", "high")
        ticket.problem_statement = overrides.get("problem_statement", "A problem")
        ticket.desired_state = overrides.get("desired_state", "Fixed state")
        ticket.acceptance_criteria = overrides.get("acceptance_criteria", ["Criterion 1", "Criterion 2"])
        ticket.affected_modules = overrides.get("affected_modules", ["codebot/foo.py"])
        ticket.evidence = overrides.get("evidence", "some evidence")
        return ticket

    def test_basic_fields_present(self) -> None:
        ticket = self._make_ticket()
        result = format_ticket_context(ticket)
        assert "CB-123456-ABCD" in result
        assert "Test ticket" in result
        assert "A problem" in result
        assert "Fixed state" in result

    def test_acceptance_criteria_listed(self) -> None:
        ticket = self._make_ticket(acceptance_criteria=["AC1", "AC2", "AC3"])
        result = format_ticket_context(ticket)
        assert "AC1" in result
        assert "AC2" in result
        assert "AC3" in result

    def test_affected_modules_present(self) -> None:
        ticket = self._make_ticket(affected_modules=["codebot/foo.py", "codebot/bar.py"])
        result = format_ticket_context(ticket)
        assert "foo.py" in result
        assert "bar.py" in result

    def test_evidence_truncated_to_500(self) -> None:
        long_evidence = "x" * 1000
        ticket = self._make_ticket(evidence=long_evidence)
        result = format_ticket_context(ticket)
        # Evidence should be truncated to 500 chars in the code
        assert len(result) < len(long_evidence) + 500  # not the full 1000

    def test_empty_acceptance_criteria(self) -> None:
        ticket = self._make_ticket(acceptance_criteria=[])
        result = format_ticket_context(ticket)
        assert "Acceptance Criteria" not in result

    def test_empty_affected_modules(self) -> None:
        ticket = self._make_ticket(affected_modules=[])
        result = format_ticket_context(ticket)
        assert "Affected Modules" not in result

    def test_ticket_class_enum_value(self) -> None:
        """When ticket_class is an enum, .value is used."""
        ticket = self._make_ticket()
        ticket.ticket_class = MagicMock(value="security")
        result = format_ticket_context(ticket)
        assert "security" in result

    def test_ticket_class_plain_string(self) -> None:
        """When ticket_class is a plain string, it's used directly."""
        ticket = self._make_ticket(ticket_class="test")
        result = format_ticket_context(ticket)
        assert "test" in result

    def test_severity_enum_value(self) -> None:
        ticket = self._make_ticket()
        ticket.severity = MagicMock(value="critical")
        result = format_ticket_context(ticket)
        assert "critical" in result

    def test_severity_plain_string(self) -> None:
        ticket = self._make_ticket(severity="medium")
        result = format_ticket_context(ticket)
        assert "medium" in result

    def test_missing_evidence(self) -> None:
        ticket = self._make_ticket(evidence="")
        result = format_ticket_context(ticket)
        assert isinstance(result, str)
