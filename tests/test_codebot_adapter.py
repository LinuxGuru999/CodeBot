"""Tests for codebot/codebot_adapter.py — CodeBot self-hosting adapter.

Covers __init__, project_name, paths, test_config, dependency_policy,
autonomy_config, components, bot_registry, model_profiles, tier_priority,
prompt_directory, api_runner_command, is_protected_path, validate_project,
and integration with orchestrator/api_runner expectations.
"""
import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.codebot_adapter import CodeBotAdapter
from codebot.project_adapter import (
    ProjectAdapter,
    ProjectPaths,
    ProjectTestConfig,
    DependencyPolicy,
    AutonomyConfig,
    ComponentDef,
)


# ---------------------------------------------------------------------------
# __init__
# ---------------------------------------------------------------------------

class TestInit:
    def test_explicit_root_is_resolved(self, tmp_path):
        adapter = CodeBotAdapter(root=tmp_path)
        assert adapter._root == tmp_path.resolve()
        assert isinstance(adapter._root, Path)

    def test_explicit_root_relative_is_resolved(self, tmp_path):
        # Relative path should be resolved to absolute
        sub = tmp_path / "my_root"
        sub.mkdir()
        adapter = CodeBotAdapter(root=sub)
        assert adapter._root.is_absolute()

    def test_default_root_uses_package_parent(self):
        adapter = CodeBotAdapter()
        expected = Path(__file__).parent.parent.resolve()
        # Default root is Path(__file__).parent.parent resolved
        # __file__ here is codebot_adapter.py ; parent.parent is repo root
        # Our CodeBotAdapter with no arg should match that
        assert adapter._root == expected

    def test_default_root_is_absolute(self):
        adapter = CodeBotAdapter()
        assert adapter._root.is_absolute()

    def test_explicit_root_none_uses_default(self):
        a1 = CodeBotAdapter(root=None)
        a2 = CodeBotAdapter()
        assert a1._root == a2._root

    def test_two_adapters_independent_roots(self, tmp_path):
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.mkdir()
        b.mkdir()
        adapter_a = CodeBotAdapter(root=a)
        adapter_b = CodeBotAdapter(root=b)
        assert adapter_a._root != adapter_b._root
        assert adapter_a.paths().repository_root != adapter_b.paths().repository_root

    def test_root_symlink_resolved(self, tmp_path):
        real = tmp_path / "real"
        real.mkdir()
        link = tmp_path / "link"
        try:
            link.symlink_to(real)
        except OSError:
            pytest.skip("symlink not supported")
        adapter = CodeBotAdapter(root=link)
        assert adapter._root == real.resolve()


# ---------------------------------------------------------------------------
# project_name
# ---------------------------------------------------------------------------

class TestProjectName:
    def test_returns_codebot(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).project_name() == "codebot"

    def test_returns_string(self):
        assert isinstance(CodeBotAdapter().project_name(), str)

    def test_consistent_across_instances(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).project_name() == CodeBotAdapter(root=tmp_path).project_name()


# ---------------------------------------------------------------------------
# paths
# ---------------------------------------------------------------------------

class TestPaths:
    def test_returns_project_paths_dataclass(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path).paths(), ProjectPaths)

    def test_repository_root_equals_root(self, tmp_path):
        adapter = CodeBotAdapter(root=tmp_path)
        assert adapter.paths().repository_root == tmp_path.resolve()

    def test_state_dir(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.state_dir == tmp_path.resolve() / ".codebot" / "state"

    def test_logs_dir(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.logs_dir == tmp_path.resolve() / ".codebot" / "logs"

    def test_docs_dir(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.docs_dir == tmp_path.resolve() / "docs"

    def test_issues_dir(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.issues_dir == tmp_path.resolve() / "docs" / "issues"

    def test_adr_dir(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.adr_dir == tmp_path.resolve() / "docs" / "adr"

    def test_modules_docs_dir(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.modules_docs_dir == tmp_path.resolve() / "docs" / "modules"

    def test_queue_file(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.queue_file == tmp_path.resolve() / "docs" / "QUEUE.md"

    def test_api_contract(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.api_contract == tmp_path.resolve() / ".codebot" / "api" / "contract.md"

    def test_entrypoint(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.entrypoint == tmp_path.resolve() / "codebot" / "__main__.py"

    def test_context_map(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.context_map == tmp_path.resolve() / ".codebot" / "CONTEXT-MAP.md"

    def test_bugs_file(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.bugs_file == tmp_path.resolve() / "docs" / "bugs.md"

    def test_features_file(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.features_file == tmp_path.resolve() / "docs" / "features.md"

    def test_roadmap_file(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.roadmap_file == tmp_path.resolve() / "docs" / "GOALS.md"

    def test_constitution_file(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.constitution_file == tmp_path.resolve() / ".codebot" / "constitution.md"

    def test_project_config(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        assert p.project_config == tmp_path.resolve() / ".codebot" / "project.yaml"

    def test_paths_are_all_path_instances(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        for field in p.__dataclass_fields__:
            assert isinstance(getattr(p, field), Path), f"{field} should be Path"

    def test_paths_frozen(self, tmp_path):
        p = CodeBotAdapter(root=tmp_path).paths()
        with pytest.raises(Exception):
            p.state_dir = Path("/tmp/other")  # type: ignore[misc]

    def test_paths_consistent_with_tmp_path_fixture(self, tmp_path):
        # Using tmp_path ensures we don't touch real .codebot/state
        adapter = CodeBotAdapter(root=tmp_path)
        p = adapter.paths()
        # Simulate what orchestrator expects after set_project_adapter
        assert p.state_dir.parent.name == ".codebot"
        assert p.logs_dir.parent.name == ".codebot"
        assert p.state_dir.name == "state"
        assert p.logs_dir.name == "logs"

    def test_paths_match_orchestrator_expectations(self, tmp_path):
        """Verify paths align with orchestrator.set_project_adapter mapping."""
        adapter = CodeBotAdapter(root=tmp_path)
        p = adapter.paths()
        # orchestrator does: BOTS_DIR = p.repository_root
        #                    STATE_DIR = p.state_dir
        #                    LOGS_DIR = p.logs_dir
        #                    BACKUP_DIR = p.state_dir / "backup"
        #                    ALIGNMENT_EVENTS_DIR = p.state_dir / "alignment_events"
        #                    DRAIN_FILE = p.state_dir / ".drain"
        assert (p.state_dir / "backup").name == "backup"
        assert (p.state_dir / "alignment_events").name == "alignment_events"
        assert (p.state_dir / ".drain").name == ".drain"
        assert p.repository_root == tmp_path.resolve()

    def test_paths_match_api_runner_expectations(self, tmp_path):
        """Verify paths align with api_runner.set_project_adapter mapping."""
        adapter = CodeBotAdapter(root=tmp_path)
        p = adapter.paths()
        # api_runner does: DRAIN_FILE = p.state_dir / ".drain"
        #                  WORK_ROOT = p.repository_root
        assert p.state_dir / ".drain" == tmp_path.resolve() / ".codebot" / "state" / ".drain"
        assert p.repository_root == tmp_path.resolve()

    def test_paths_idempotent(self, tmp_path):
        adapter = CodeBotAdapter(root=tmp_path)
        assert adapter.paths() == adapter.paths()

    def test_different_roots_produce_different_paths(self, tmp_path):
        # Use two sibling directories under tmp_path rather than bare /tmp
        a = tmp_path / "proj_a"
        b = tmp_path / "proj_b"
        a.mkdir()
        b.mkdir()
        pa = CodeBotAdapter(root=a).paths()
        pb = CodeBotAdapter(root=b).paths()
        assert pa.state_dir != pb.state_dir
        assert pa.repository_root != pb.repository_root


# ---------------------------------------------------------------------------
# test_config
# ---------------------------------------------------------------------------

class TestTestConfig:
    def test_returns_project_test_config(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path).test_config(), ProjectTestConfig)

    def test_framework_is_pytest(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).test_config().framework == "pytest"

    def test_test_command(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).test_config().test_command == "python3 -m pytest -q"

    def test_test_directories(self, tmp_path):
        cfg = CodeBotAdapter(root=tmp_path).test_config()
        assert cfg.test_directories == ["tests/"]

    def test_coverage_tool(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).test_config().coverage_tool == "pytest-cov"

    def test_lint_tool(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).test_config().lint_tool == "none"

    def test_type_checker(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).test_config().type_checker == "none"

    def test_type_check_command_empty(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).test_config().type_check_command == ""

    def test_frozen(self, tmp_path):
        cfg = CodeBotAdapter(root=tmp_path).test_config()
        with pytest.raises(Exception):
            cfg.framework = "jest"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# dependency_policy
# ---------------------------------------------------------------------------

class TestDependencyPolicy:
    def test_returns_dependency_policy(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path).dependency_policy(), DependencyPolicy)

    def test_policy_stdlib_only(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).dependency_policy().policy == "stdlib-only"

    def test_allowed_third_party_contains_pytest(self, tmp_path):
        allowed = CodeBotAdapter(root=tmp_path).dependency_policy().allowed_third_party
        names = [e["name"] for e in allowed]
        assert "pytest" in names

    def test_allowed_third_party_contains_pytest_cov(self, tmp_path):
        allowed = CodeBotAdapter(root=tmp_path).dependency_policy().allowed_third_party
        names = [e["name"] for e in allowed]
        assert "pytest-cov" in names

    def test_allowed_entries_have_required_keys(self, tmp_path):
        for entry in CodeBotAdapter(root=tmp_path).dependency_policy().allowed_third_party:
            assert "name" in entry
            assert "scope" in entry
            assert "justification" in entry

    def test_dependency_files(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).dependency_policy().dependency_files == ["pyproject.toml"]


# ---------------------------------------------------------------------------
# autonomy_config
# ---------------------------------------------------------------------------

class TestAutonomyConfig:
    def test_returns_autonomy_config(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path).autonomy_config(), AutonomyConfig)

    def test_level_is_2(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).autonomy_config().level == 2

    def test_human_approval_empty(self, tmp_path):
        # CodeBot self-hosting has level 2 with empty human_approval_required_for (lower than monitor)
        assert CodeBotAdapter(root=tmp_path).autonomy_config().human_approval_required_for == []

    def test_autonomous_allowed_contains_expected(self, tmp_path):
        allowed = CodeBotAdapter(root=tmp_path).autonomy_config().autonomous_allowed_for
        assert "documentation_corrections" in allowed
        assert "test_additions" in allowed
        assert "safe_refactors" in allowed
        assert "known_bug_fixes" in allowed

    def test_level_lower_than_monitor_is_self_hosting_constraint(self, tmp_path):
        # Invariant: self-improvement autonomy level is 2
        assert CodeBotAdapter(root=tmp_path).autonomy_config().level == 2


# ---------------------------------------------------------------------------
# components
# ---------------------------------------------------------------------------

class TestComponents:
    def test_returns_list(self, tmp_path):
        comps = CodeBotAdapter(root=tmp_path).components()
        assert isinstance(comps, list)
        assert len(comps) == 4

    def test_all_are_component_defs(self, tmp_path):
        for c in CodeBotAdapter(root=tmp_path).components():
            assert isinstance(c, ComponentDef)

    def test_component_names(self, tmp_path):
        names = {c.name for c in CodeBotAdapter(root=tmp_path).components()}
        assert names == {"core", "tests", "docs", "roles"}

    def test_core_component(self, tmp_path):
        core = next(c for c in CodeBotAdapter(root=tmp_path).components() if c.name == "core")
        assert core.path == "codebot/"
        assert core.component_type == "backend"
        assert core.language == "python"

    def test_tests_component(self, tmp_path):
        t = next(c for c in CodeBotAdapter(root=tmp_path).components() if c.name == "tests")
        assert t.path == "tests/"
        assert t.component_type == "test"

    def test_roles_component(self, tmp_path):
        r = next(c for c in CodeBotAdapter(root=tmp_path).components() if c.name == "roles")
        assert r.path == "codebot/roles/"
        assert r.component_type == "configuration"


# ---------------------------------------------------------------------------
# bot_registry
# ---------------------------------------------------------------------------

class TestBotRegistry:
    def test_returns_list(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path).bot_registry(), list)

    def test_length_matches_all_roles(self, tmp_path):
        from codebot.role_registry import ALL_ROLES
        assert len(CodeBotAdapter(root=tmp_path).bot_registry()) == len(ALL_ROLES)

    def test_each_entry_has_required_keys(self, tmp_path):
        for entry in CodeBotAdapter(root=tmp_path).bot_registry():
            for key in ("name", "prompt", "interval", "model", "fallback_model", "tier", "enabled", "max_restarts", "clean_exit_wait", "runner_mode", "category"):
                assert key in entry, f"missing key {key} in {entry.get('name')}"

    def test_names_match_roles(self, tmp_path):
        from codebot.role_registry import ALL_ROLES
        role_names = {r.name for r in ALL_ROLES}
        registry_names = {e["name"] for e in CodeBotAdapter(root=tmp_path).bot_registry()}
        assert registry_names == role_names

    def test_prompt_path_format(self, tmp_path):
        for entry in CodeBotAdapter(root=tmp_path).bot_registry():
            assert entry["prompt"].startswith("codebot/roles/")
            assert entry["prompt"].endswith(".md")

    def test_interval_by_category(self, tmp_path):
        # discovery 1800, planning 3600, implementation 300, review 600, control 900
        registry = {e["name"]: e for e in CodeBotAdapter(root=tmp_path).bot_registry()}
        # Check that implementation roles have interval 300
        from codebot.role_registry import ROLE_REGISTRY
        for name, entry in registry.items():
            role = ROLE_REGISTRY[name]
            cat = role.category.value
            if cat == "implementation":
                assert entry["interval"] == 300, f"{name} should have interval 300"
            elif cat == "discovery":
                assert entry["interval"] == 1800
            elif cat == "planning":
                assert entry["interval"] == 3600
            elif cat == "review":
                assert entry["interval"] == 600
            elif cat == "control":
                assert entry["interval"] == 900

    def test_thinking_roles_use_thinking_models(self, tmp_path):
        thinking_roles = {"security_auditor", "architecture_auditor", "security_reviewer", "architecture_reviewer", "correctness_reviewer", "ux_reviewer"}
        thinking_models = {"qwen-3.8-max-thinking", "qwen-3.7-max-thinking", "qwen-3.6-plus-thinking", "qwen-3.5-plus-thinking"}
        for entry in CodeBotAdapter(root=tmp_path).bot_registry():
            if entry["name"] in thinking_roles:
                assert entry["model"] in thinking_models, f"{entry['name']} should use thinking model"

    def test_feature_hunter_model_override(self, tmp_path):
        registry = {e["name"]: e for e in CodeBotAdapter(root=tmp_path).bot_registry()}
        assert registry["feature_hunter"]["model"] == "xiaomi-mimo-2.5"

    def test_bug_hunter_model_override(self, tmp_path):
        registry = {e["name"]: e for e in CodeBotAdapter(root=tmp_path).bot_registry()}
        assert registry["bug_hunter"]["model"] == "qwen-3.8-max"

    def test_tier_values(self, tmp_path):
        for entry in CodeBotAdapter(root=tmp_path).bot_registry():
            assert entry["tier"] in (1, 2)
            # discovery + implementation => tier 1, others tier 2
            cat = entry["category"]
            if cat in ("discovery", "implementation"):
                assert entry["tier"] == 1
            else:
                assert entry["tier"] == 2

    def test_all_enabled(self, tmp_path):
        for entry in CodeBotAdapter(root=tmp_path).bot_registry():
            assert entry["enabled"] is True

    def test_fallback_model_present(self, tmp_path):
        for entry in CodeBotAdapter(root=tmp_path).bot_registry():
            assert isinstance(entry["fallback_model"], str)
            assert len(entry["fallback_model"]) > 0

    def test_runner_mode_api(self, tmp_path):
        for entry in CodeBotAdapter(root=tmp_path).bot_registry():
            assert entry["runner_mode"] == "api"


# ---------------------------------------------------------------------------
# model_profiles
# ---------------------------------------------------------------------------

class TestModelProfiles:
    def test_returns_dict(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path).model_profiles(), dict)

    def test_default_profile_exists(self, tmp_path):
        profiles = CodeBotAdapter(root=tmp_path).model_profiles()
        assert "default" in profiles

    def test_default_profile_fields(self, tmp_path):
        d = CodeBotAdapter(root=tmp_path).model_profiles()["default"]
        assert d["lockup_risk"] == "low"
        assert d["heartbeat_multiplier"] == 1.5
        assert d["log_stall_seconds"] == 120
        assert d["restart_cooldown"] == 5


# ---------------------------------------------------------------------------
# tier_priority
# ---------------------------------------------------------------------------

class TestTierPriority:
    def test_returns_dict(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path).tier_priority(), dict)

    def test_all_roles_present(self, tmp_path):
        from codebot.role_registry import ALL_ROLES
        tp = CodeBotAdapter(root=tmp_path).tier_priority()
        for role in ALL_ROLES:
            assert role.name in tp, f"{role.name} missing from tier_priority"

    def test_discovery_priority_11(self, tmp_path):
        tp = CodeBotAdapter(root=tmp_path).tier_priority()
        from codebot.role_registry import ROLE_REGISTRY
        for name, prio in tp.items():
            role = ROLE_REGISTRY[name]
            if role.category.value == "discovery":
                assert prio == 11

    def test_implementation_priority_13(self, tmp_path):
        tp = CodeBotAdapter(root=tmp_path).tier_priority()
        from codebot.role_registry import ROLE_REGISTRY
        for name, prio in tp.items():
            role = ROLE_REGISTRY[name]
            if role.category.value == "implementation":
                assert prio == 13

    def test_review_priority_21(self, tmp_path):
        tp = CodeBotAdapter(root=tmp_path).tier_priority()
        from codebot.role_registry import ROLE_REGISTRY
        for name, prio in tp.items():
            role = ROLE_REGISTRY[name]
            if role.category.value == "review":
                assert prio == 21

    def test_control_priority_31(self, tmp_path):
        tp = CodeBotAdapter(root=tmp_path).tier_priority()
        from codebot.role_registry import ROLE_REGISTRY
        for name, prio in tp.items():
            role = ROLE_REGISTRY[name]
            if role.category.value == "control":
                assert prio == 31


# ---------------------------------------------------------------------------
# prompt_directory
# ---------------------------------------------------------------------------

class TestPromptDirectory:
    def test_returns_path(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path).prompt_directory(), Path)

    def test_equals_root_codebot_roles(self, tmp_path):
        adapter = CodeBotAdapter(root=tmp_path)
        assert adapter.prompt_directory() == tmp_path.resolve() / "codebot" / "roles"

    def test_with_tmp_path(self, tmp_path):
        # tmp_path use — no filesystem assertions beyond path construction
        adapter = CodeBotAdapter(root=tmp_path)
        pd = adapter.prompt_directory()
        assert pd.name == "roles"
        assert pd.parent.name == "codebot"


# ---------------------------------------------------------------------------
# api_runner_command
# ---------------------------------------------------------------------------

class TestApiRunnerCommand:
    def test_returns_list(self, tmp_path):
        cmd = CodeBotAdapter(root=tmp_path).api_runner_command("general_implementer", "general_implementer.md")
        assert isinstance(cmd, list)
        assert len(cmd) >= 4

    def test_contains_python_and_api_runner(self, tmp_path):
        cmd = CodeBotAdapter(root=tmp_path).api_runner_command("bot", "prompt.md")
        assert cmd[0] == "python3"
        assert "-m" in cmd
        assert "codebot.api_runner" in cmd

    def test_contains_bot_name(self, tmp_path):
        cmd = CodeBotAdapter(root=tmp_path).api_runner_command("my_bot", "my_prompt.md")
        assert "--bot" in cmd
        idx = cmd.index("--bot")
        assert cmd[idx + 1] == "my_bot"

    def test_contains_prompt_path(self, tmp_path):
        adapter = CodeBotAdapter(root=tmp_path)
        cmd = adapter.api_runner_command("bot", "test_prompt.md")
        assert "--prompt" in cmd
        idx = cmd.index("--prompt")
        assert cmd[idx + 1] == str(tmp_path.resolve() / "codebot" / "roles" / "test_prompt.md")

    def test_prompt_directory_used(self, tmp_path):
        adapter = CodeBotAdapter(root=tmp_path)
        cmd = adapter.api_runner_command("worker-1", "WORKER.md")
        prompt_arg = cmd[cmd.index("--prompt") + 1]
        assert prompt_arg.startswith(str(adapter.prompt_directory()))

    def test_different_bots(self, tmp_path):
        adapter = CodeBotAdapter(root=tmp_path)
        cmd1 = adapter.api_runner_command("bot_a", "a.md")
        cmd2 = adapter.api_runner_command("bot_b", "b.md")
        assert cmd1 != cmd2
        assert "bot_a" in cmd1
        assert "bot_b" in cmd2


# ---------------------------------------------------------------------------
# is_protected_path
# ---------------------------------------------------------------------------

class TestIsProtectedPath:
    def test_constitution_protected(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).is_protected_path(".codebot/constitution.md") is True

    def test_orchestrator_protected(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).is_protected_path("codebot/orchestrator.py") is True

    def test_gatekeeper_protected(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).is_protected_path("codebot/gatekeeper.py") is True

    def test_tool_policy_protected(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).is_protected_path("codebot/tool_policy.py") is True

    def test_quality_gate_protected(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).is_protected_path("codebot/quality_gate.py") is True

    def test_prompt_gateway_protected(self, tmp_path):
        assert CodeBotAdapter(root=tmp_path).is_protected_path("codebot/prompt_gateway.py") is True

    def test_subpath_of_protected(self, tmp_path):
        # startswith check: "codebot/orchestrator.py" should protect subpaths like with suffix? Not subpath but prefix
        # Actually is_protected returns any(path.startswith(p) or path==p) — so exact and prefix
        adapter = CodeBotAdapter(root=tmp_path)
        # An exact match is protected; a random file is not
        assert adapter.is_protected_path("codebot/orchestrator.py") is True
        assert adapter.is_protected_path("codebot/orchestrator.py.bak") is True  # startswith prefix still true

    def test_unprotected_paths(self, tmp_path):
        adapter = CodeBotAdapter(root=tmp_path)
        assert adapter.is_protected_path("codebot/api_runner.py") is False
        assert adapter.is_protected_path("tests/test_foo.py") is False
        assert adapter.is_protected_path("docs/GOALS.md") is False
        assert adapter.is_protected_path("random_file.py") is False
        assert adapter.is_protected_path("") is False

    def test_returns_bool(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path).is_protected_path("x"), bool)


# ---------------------------------------------------------------------------
# validate_project
# ---------------------------------------------------------------------------

class TestValidateProject:
    def test_empty_tmp_path_returns_errors(self, tmp_path):
        errors = CodeBotAdapter(root=tmp_path).validate_project()
        assert isinstance(errors, list)
        assert len(errors) >= 1

    def test_missing_project_config(self, tmp_path):
        errors = CodeBotAdapter(root=tmp_path).validate_project()
        assert any("project config missing" in e for e in errors)

    def test_missing_constitution(self, tmp_path):
        errors = CodeBotAdapter(root=tmp_path).validate_project()
        assert any("constitution missing" in e for e in errors)

    def test_missing_codebot_package(self, tmp_path):
        errors = CodeBotAdapter(root=tmp_path).validate_project()
        assert any("codebot/ package directory missing" in e for e in errors)

    def test_valid_repo_returns_no_errors(self):
        # Use the real repo root (default adapter) — it should validate cleanly
        adapter = CodeBotAdapter()
        errors = adapter.validate_project()
        assert errors == [], f"expected no errors for real repo, got: {errors}"

    def test_with_minimal_valid_structure(self, tmp_path):
        # Create minimal expected structure
        (tmp_path / ".codebot").mkdir()
        (tmp_path / ".codebot" / "project.yaml").write_text("name: codebot\n")
        (tmp_path / ".codebot" / "constitution.md").write_text("# Constitution\n")
        (tmp_path / "codebot").mkdir()
        (tmp_path / "codebot" / "__init__.py").write_text("")
        (tmp_path / "tests").mkdir()
        (tmp_path / "codebot" / "roles").mkdir()
        # Need >=20 role prompts
        for i in range(20):
            (tmp_path / "codebot" / "roles" / f"role_{i}.md").write_text(f"# Role {i}\n")
        errors = CodeBotAdapter(root=tmp_path).validate_project()
        assert errors == [], f"expected no errors after minimal setup, got: {errors}"

    def test_role_count_insufficient(self, tmp_path):
        (tmp_path / ".codebot").mkdir()
        (tmp_path / ".codebot" / "project.yaml").write_text("name: codebot\n")
        (tmp_path / ".codebot" / "constitution.md").write_text("# Constitution\n")
        (tmp_path / "codebot").mkdir()
        (tmp_path / "codebot" / "__init__.py").write_text("")
        (tmp_path / "tests").mkdir()
        (tmp_path / "codebot" / "roles").mkdir()
        for i in range(5):
            (tmp_path / "codebot" / "roles" / f"role_{i}.md").write_text(f"# Role {i}\n")
        errors = CodeBotAdapter(root=tmp_path).validate_project()
        assert any("role prompts" in e for e in errors)

    def test_returns_list_of_strings(self, tmp_path):
        errors = CodeBotAdapter(root=tmp_path).validate_project()
        for e in errors:
            assert isinstance(e, str)


# ---------------------------------------------------------------------------
# ProjectAdapter ABC compliance
# ---------------------------------------------------------------------------

class TestInterfaceCompliance:
    def test_is_project_adapter_subclass(self, tmp_path):
        assert isinstance(CodeBotAdapter(root=tmp_path), ProjectAdapter)

    def test_implements_all_abstract_methods(self):
        abstract = getattr(CodeBotAdapter, "__abstractmethods__", set())
        assert len(abstract) == 0, f"CodeBotAdapter still has abstract methods: {abstract}"
        adapter = CodeBotAdapter(root=Path("/tmp"))
        for method in ("project_name", "paths", "test_config", "dependency_policy", "autonomy_config", "components", "bot_registry", "model_profiles", "tier_priority", "prompt_directory", "api_runner_command", "is_protected_path", "validate_project"):
            assert hasattr(adapter, method)
            assert callable(getattr(adapter, method))

    def test_orchestrator_integration_set_adapter(self, tmp_path):
        """Adapters paths must be consumable by orchestrator.set_project_adapter."""
        adapter = CodeBotAdapter(root=tmp_path)
        from codebot import orchestrator
        # Save originals
        orig_state = orchestrator.STATE_DIR
        orig_logs = orchestrator.LOGS_DIR
        try:
            orchestrator.set_project_adapter(adapter)
            assert orchestrator.STATE_DIR == tmp_path.resolve() / ".codebot" / "state"
            assert orchestrator.LOGS_DIR == tmp_path.resolve() / ".codebot" / "logs"
            assert orchestrator.BOTS_DIR == tmp_path.resolve()
            assert orchestrator.ALIGNMENT_EVENTS_DIR == tmp_path.resolve() / ".codebot" / "state" / "alignment_events"
            assert orchestrator.DRAIN_FILE == tmp_path.resolve() / ".codebot" / "state" / ".drain"
        finally:
            # Restore via default adapter to avoid polluting other tests
            orchestrator.set_project_adapter(CodeBotAdapter())

    def test_api_runner_integration_set_adapter(self, tmp_path):
        """Adapter paths must be consumable by api_runner.set_project_adapter."""
        adapter = CodeBotAdapter(root=tmp_path)
        from codebot import api_runner
        try:
            api_runner.set_project_adapter(adapter)
            assert api_runner.DRAIN_FILE == tmp_path.resolve() / ".codebot" / "state" / ".drain"
            assert api_runner.WORK_ROOT == tmp_path.resolve()
        finally:
            api_runner.set_project_adapter(CodeBotAdapter())
