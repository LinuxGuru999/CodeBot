import sys
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.monitor_adapter import MonitorAdapter, MONITOR_ROOT
from codebot.codebot_adapter import CodeBotAdapter
from codebot.project_adapter import (
    ProjectAdapter,
    ProjectPaths,
    ProjectTestConfig,
    DependencyPolicy,
    AutonomyConfig,
    ComponentDef,
)


class TestProjectName:
    def test_returns_monitor(self):
        assert MonitorAdapter().project_name() == "monitor"

    def test_is_string(self):
        assert isinstance(MonitorAdapter().project_name(), str)

    def test_differs_from_codebot(self, tmp_path):
        assert MonitorAdapter().project_name() != CodeBotAdapter(root=tmp_path).project_name()

    def test_consistent(self):
        assert MonitorAdapter().project_name() == MonitorAdapter().project_name()


class TestPaths:
    def test_returns_project_paths(self):
        assert isinstance(MonitorAdapter().paths(), ProjectPaths)

    def test_repository_root_is_monitor_root(self):
        assert MonitorAdapter().paths().repository_root == MONITOR_ROOT

    def test_monitor_root_is_home_work(self):
        assert MONITOR_ROOT == Path.home() / "Work"

    def test_state_dir(self):
        p = MonitorAdapter().paths()
        assert p.state_dir == MONITOR_ROOT / "bots" / "state"

    def test_logs_dir(self):
        p = MonitorAdapter().paths()
        assert p.logs_dir == MONITOR_ROOT / "bots" / "logs"

    def test_docs_dir(self):
        p = MonitorAdapter().paths()
        assert p.docs_dir == MONITOR_ROOT / "docs"

    def test_issues_dir(self):
        p = MonitorAdapter().paths()
        assert p.issues_dir == MONITOR_ROOT / "docs" / "issues"

    def test_adr_dir(self):
        p = MonitorAdapter().paths()
        assert p.adr_dir == MONITOR_ROOT / "docs" / "adr"

    def test_modules_docs_dir(self):
        p = MonitorAdapter().paths()
        assert p.modules_docs_dir == MONITOR_ROOT / "docs" / "modules"

    def test_queue_file(self):
        p = MonitorAdapter().paths()
        assert p.queue_file == MONITOR_ROOT / "bots" / "QUEUE.md"

    def test_api_contract(self):
        p = MonitorAdapter().paths()
        assert p.api_contract == MONITOR_ROOT / "API_CONTRACT.md"

    def test_entrypoint(self):
        p = MonitorAdapter().paths()
        assert p.entrypoint == MONITOR_ROOT / "ENTRYPOINT.md"

    def test_context_map(self):
        p = MonitorAdapter().paths()
        assert p.context_map == MONITOR_ROOT / "CONTEXT-MAP.md"

    def test_bugs_file(self):
        p = MonitorAdapter().paths()
        assert p.bugs_file == MONITOR_ROOT / "BUGS.md"

    def test_features_file(self):
        p = MonitorAdapter().paths()
        assert p.features_file == MONITOR_ROOT / "FEATURES.md"

    def test_roadmap_file(self):
        p = MonitorAdapter().paths()
        assert p.roadmap_file == MONITOR_ROOT / "ROADMAP.md"

    def test_constitution_file(self):
        p = MonitorAdapter().paths()
        assert p.constitution_file == MONITOR_ROOT / ".codebot" / "constitution.md"

    def test_project_config(self):
        p = MonitorAdapter().paths()
        assert p.project_config == MONITOR_ROOT / ".codebot" / "project.yaml"

    def test_all_fields_are_paths(self):
        p = MonitorAdapter().paths()
        for field in p.__dataclass_fields__:
            assert isinstance(getattr(p, field), Path)

    def test_paths_frozen(self):
        p = MonitorAdapter().paths()
        with pytest.raises(Exception):
            p.state_dir = Path("/tmp/other")  # type: ignore[misc]

    def test_idempotent(self):
        assert MonitorAdapter().paths() == MonitorAdapter().paths()

    def test_state_dir_differs_from_codebot(self, tmp_path):
        m = MonitorAdapter().paths().state_dir
        c = CodeBotAdapter(root=tmp_path).paths().state_dir
        assert m != c
        assert str(m).endswith("bots/state")
        assert str(c).endswith(".codebot/state")

    def test_queue_file_differs_from_codebot(self, tmp_path):
        m = MonitorAdapter().paths().queue_file
        c = CodeBotAdapter(root=tmp_path).paths().queue_file
        assert m != c
        assert m.name == "QUEUE.md"
        assert m.parent.name == "bots"
        assert c.parent.name == "docs"

    def test_api_contract_differs_from_codebot(self, tmp_path):
        m = MonitorAdapter().paths().api_contract
        c = CodeBotAdapter(root=tmp_path).paths().api_contract
        assert m != c


class TestTestConfig:
    def test_returns_config(self):
        assert isinstance(MonitorAdapter().test_config(), ProjectTestConfig)

    def test_framework(self):
        assert MonitorAdapter().test_config().framework == "pytest"

    def test_test_command(self):
        assert MonitorAdapter().test_config().test_command == "python3 -m pytest -q"

    def test_test_directories(self):
        dirs = MonitorAdapter().test_config().test_directories
        assert "Monitor-Manager-Python/tests/" in dirs
        assert "Monitor-Client-Python/tests/" in dirs
        assert "lib-common/tests/" in dirs
        assert "bots/tests/" in dirs
        assert len(dirs) == 4

    def test_coverage_tool(self):
        assert MonitorAdapter().test_config().coverage_tool == "pytest-cov"

    def test_lint_tool(self):
        assert MonitorAdapter().test_config().lint_tool == "none"

    def test_type_checker(self):
        assert MonitorAdapter().test_config().type_checker == "mypy"

    def test_type_check_command(self):
        assert MonitorAdapter().test_config().type_check_command == "mypy --config-file mypy.ini"

    def test_frozen(self):
        cfg = MonitorAdapter().test_config()
        with pytest.raises(Exception):
            cfg.framework = "jest"  # type: ignore[misc]

    def test_differs_from_codebot_directories(self, tmp_path):
        m_dirs = MonitorAdapter().test_config().test_directories
        c_dirs = CodeBotAdapter(root=tmp_path).test_config().test_directories
        assert m_dirs != c_dirs
        assert len(m_dirs) > len(c_dirs)


class TestDependencyPolicy:
    def test_returns_policy(self):
        assert isinstance(MonitorAdapter().dependency_policy(), DependencyPolicy)

    def test_policy_stdlib_only(self):
        assert MonitorAdapter().dependency_policy().policy == "stdlib-only"

    def test_allowed_contains_argon2(self):
        allowed = MonitorAdapter().dependency_policy().allowed_third_party
        names = [e["name"] for e in allowed]
        assert "argon2-cffi" in names

    def test_allowed_contains_pytest(self):
        allowed = MonitorAdapter().dependency_policy().allowed_third_party
        names = [e["name"] for e in allowed]
        assert "pytest" in names

    def test_entries_have_keys(self):
        for entry in MonitorAdapter().dependency_policy().allowed_third_party:
            assert "name" in entry
            assert "scope" in entry
            assert "justification" in entry

    def test_dependency_files(self):
        files = MonitorAdapter().dependency_policy().dependency_files
        assert "Monitor-Manager-Python/pyproject.toml" in files
        assert "Monitor-Client-Python/pyproject.toml" in files
        assert "lib-common/pyproject.toml" in files
        assert len(files) == 3

    def test_differs_from_codebot_files(self, tmp_path):
        m_files = MonitorAdapter().dependency_policy().dependency_files
        c_files = CodeBotAdapter(root=tmp_path).dependency_policy().dependency_files
        assert m_files != c_files


class TestAutonomyConfig:
    def test_returns_config(self):
        assert isinstance(MonitorAdapter().autonomy_config(), AutonomyConfig)

    def test_level_is_2(self):
        assert MonitorAdapter().autonomy_config().level == 2

    def test_human_approval_required(self):
        required = MonitorAdapter().autonomy_config().human_approval_required_for
        assert "authentication_architecture" in required
        assert "cryptography" in required
        assert "secrets" in required
        assert "constitution_changes" in required
        assert len(required) == 8

    def test_autonomous_allowed(self):
        allowed = MonitorAdapter().autonomy_config().autonomous_allowed_for
        assert "documentation_corrections" in allowed
        assert "test_additions" in allowed
        assert "lint_fixes" in allowed
        assert "known_bug_fixes" in allowed
        assert len(allowed) == 6

    def test_human_approval_non_empty_where_codebot_empty(self, tmp_path):
        m = MonitorAdapter().autonomy_config().human_approval_required_for
        c = CodeBotAdapter(root=tmp_path).autonomy_config().human_approval_required_for
        assert len(m) > len(c)
        assert len(c) == 0

    def test_level_same_as_codebot(self, tmp_path):
        assert MonitorAdapter().autonomy_config().level == CodeBotAdapter(root=tmp_path).autonomy_config().level


class TestComponents:
    def test_returns_list(self):
        assert isinstance(MonitorAdapter().components(), list)
        assert len(MonitorAdapter().components()) == 5

    def test_all_are_component_defs(self):
        for c in MonitorAdapter().components():
            assert isinstance(c, ComponentDef)

    def test_names(self):
        names = {c.name for c in MonitorAdapter().components()}
        assert names == {"manager", "client", "lib-common", "botnet", "frontend"}

    def test_manager(self):
        m = next(c for c in MonitorAdapter().components() if c.name == "manager")
        assert m.path == "Monitor-Manager-Python/"
        assert m.component_type == "backend"
        assert m.language == "python"

    def test_client(self):
        c = next(x for x in MonitorAdapter().components() if x.name == "client")
        assert c.path == "Monitor-Client-Python/"
        assert c.component_type == "agent"

    def test_lib_common(self):
        lc = next(x for x in MonitorAdapter().components() if x.name == "lib-common")
        assert lc.path == "lib-common/"
        assert lc.component_type == "shared_kernel"

    def test_botnet(self):
        b = next(x for x in MonitorAdapter().components() if x.name == "botnet")
        assert b.path == "bots/"
        assert b.component_type == "autonomous_improvement"

    def test_frontend(self):
        f = next(x for x in MonitorAdapter().components() if x.name == "frontend")
        assert f.path == "Monitor-Manager-Python/static_manager/"
        assert f.component_type == "frontend"
        assert f.language == "javascript"

    def test_differs_from_codebot(self, tmp_path):
        m_names = {c.name for c in MonitorAdapter().components()}
        c_names = {c.name for c in CodeBotAdapter(root=tmp_path).components()}
        assert m_names != c_names


class TestBotRegistry:
    def test_returns_list(self):
        assert isinstance(MonitorAdapter().bot_registry(), list)

    def test_length(self):
        assert len(MonitorAdapter().bot_registry()) == 26

    def test_each_has_required_keys(self):
        for entry in MonitorAdapter().bot_registry():
            for key in ("name", "prompt", "interval", "model", "tier"):
                assert key in entry, f"missing {key} in {entry.get('name')}"

    def test_names_unique(self):
        names = [e["name"] for e in MonitorAdapter().bot_registry()]
        assert len(names) == len(set(names))

    def test_prompt_ends_with_md(self):
        for entry in MonitorAdapter().bot_registry():
            assert entry["prompt"].endswith(".md")

    def test_specific_bots_exist(self):
        names = {e["name"] for e in MonitorAdapter().bot_registry()}
        assert "issues" in names
        assert "security_auditor" in names
        assert "worker-1" in names
        assert "release" in names
        assert "prompt_opt" in names

    def test_release_disabled(self):
        reg = {e["name"]: e for e in MonitorAdapter().bot_registry()}
        assert reg["release"]["enabled"] is False

    def test_interval_values(self):
        reg = {e["name"]: e for e in MonitorAdapter().bot_registry()}
        assert reg["issues"]["interval"] == 300
        assert reg["dependency"]["interval"] == 3600
        assert reg["release"]["interval"] == 3600
        assert reg["doc_sync"]["interval"] == 900

    def test_model_values(self):
        reg = {e["name"]: e for e in MonitorAdapter().bot_registry()}
        assert reg["issues"]["model"] == "xiaomi-mimo-2.5"
        assert reg["security_auditor"]["model"] == "qwen-3.8-max"

    def test_tier_values(self):
        for entry in MonitorAdapter().bot_registry():
            assert entry["tier"] in (1, 2, 3)

    def test_differs_from_codebot_registry(self, tmp_path):
        m_names = {e["name"] for e in MonitorAdapter().bot_registry()}
        c_names = {e["name"] for e in CodeBotAdapter(root=tmp_path).bot_registry()}
        assert m_names != c_names
        assert "issues" in m_names
        assert "issues" not in c_names
        assert "bug_hunter" in c_names
        assert "bug_hunter" not in m_names


class TestModelProfiles:
    def test_returns_dict(self):
        assert isinstance(MonitorAdapter().model_profiles(), dict)

    def test_count(self):
        assert len(MonitorAdapter().model_profiles()) == 12

    def test_thinking_profiles_high_risk(self):
        profiles = MonitorAdapter().model_profiles()
        assert profiles["qwen-3.8-max-thinking"]["lockup_risk"] == "high"
        assert profiles["qwen-3.7-max-thinking"]["lockup_risk"] == "high"

    def test_low_risk_profile(self):
        profiles = MonitorAdapter().model_profiles()
        assert profiles["xiaomi-mimo-2.5"]["lockup_risk"] == "low"

    def test_all_have_required_keys(self):
        for name, profile in MonitorAdapter().model_profiles().items():
            assert "lockup_risk" in profile, f"{name} missing lockup_risk"
            assert "heartbeat_multiplier" in profile
            assert "log_stall_seconds" in profile
            assert "restart_cooldown" in profile

    def test_multiplier_values(self):
        profiles = MonitorAdapter().model_profiles()
        assert profiles["xiaomi-mimo-2.5"]["heartbeat_multiplier"] == 1.3
        assert profiles["qwen-3.8-max-thinking"]["heartbeat_multiplier"] == 2.8

    def test_differs_from_codebot(self, tmp_path):
        m = MonitorAdapter().model_profiles()
        c = CodeBotAdapter(root=tmp_path).model_profiles()
        assert set(m.keys()) != set(c.keys())
        assert "default" in c
        assert "default" not in m
        assert "xiaomi-mimo-2.5" in m


class TestTierPriority:
    def test_returns_dict(self):
        assert isinstance(MonitorAdapter().tier_priority(), dict)

    def test_critical_bots_priority_11(self):
        tp = MonitorAdapter().tier_priority()
        assert tp["issues"] == 11
        assert tp["build"] == 11

    def test_worker_priorities(self):
        tp = MonitorAdapter().tier_priority()
        assert tp["worker-1"] == 12
        assert tp["worker-5"] == 13

    def test_disabled_bots_99(self):
        tp = MonitorAdapter().tier_priority()
        assert tp["github_issues"] == 99
        assert tp["release"] == 99

    def test_tier3_bots_31(self):
        tp = MonitorAdapter().tier_priority()
        assert tp["doc_sync"] == 31
        assert tp["dependency"] == 31

    def test_all_values_are_int(self):
        for v in MonitorAdapter().tier_priority().values():
            assert isinstance(v, int)

    def test_differs_from_codebot(self, tmp_path):
        m = MonitorAdapter().tier_priority()
        c = CodeBotAdapter(root=tmp_path).tier_priority()
        assert "issues" in m
        assert "issues" not in c
        assert "bug_hunter" in c
        assert "bug_hunter" not in m


class TestPromptDirectory:
    def test_returns_path(self):
        assert isinstance(MonitorAdapter().prompt_directory(), Path)

    def test_equals_monitor_root_bots(self):
        assert MonitorAdapter().prompt_directory() == MONITOR_ROOT / "bots"

    def test_differs_from_codebot(self, tmp_path):
        m = MonitorAdapter().prompt_directory()
        c = CodeBotAdapter(root=tmp_path).prompt_directory()
        assert m != c
        assert m.name == "bots"
        assert c.name == "roles"


class TestApiRunnerCommand:
    def test_returns_list(self):
        cmd = MonitorAdapter().api_runner_command("issues", "ISSUES_BOT.md")
        assert isinstance(cmd, list)

    def test_contains_python(self):
        cmd = MonitorAdapter().api_runner_command("issues", "ISSUES_BOT.md")
        assert cmd[0] == "python3"
        assert str(MONITOR_ROOT / "bots" / "api_runner.py") in cmd

    def test_contains_bot(self):
        cmd = MonitorAdapter().api_runner_command("my_bot", "MY_BOT.md")
        assert "--bot" in cmd
        assert "my_bot" in cmd

    def test_contains_prompt(self):
        cmd = MonitorAdapter().api_runner_command("issues", "ISSUES_BOT.md")
        assert "--prompt" in cmd
        idx = cmd.index("--prompt")
        assert cmd[idx + 1] == str(MONITOR_ROOT / "bots" / "ISSUES_BOT.md")

    def test_uses_prompt_directory(self):
        adapter = MonitorAdapter()
        cmd = adapter.api_runner_command("worker-1", "WORKER_BOT.md")
        prompt_arg = cmd[cmd.index("--prompt") + 1]
        assert prompt_arg == str(adapter.prompt_directory() / "WORKER_BOT.md")

    def test_differs_from_codebot(self, tmp_path):
        m_cmd = MonitorAdapter().api_runner_command("issues", "ISSUES_BOT.md")
        c_cmd = CodeBotAdapter(root=tmp_path).api_runner_command("bug_hunter", "bug_hunter.md")
        assert m_cmd != c_cmd
        assert "codebot.api_runner" in c_cmd
        assert str(MONITOR_ROOT / "bots" / "api_runner.py") in m_cmd


class TestIsProtectedPath:
    def test_constitution_protected(self):
        assert MonitorAdapter().is_protected_path(".codebot/constitution.md") is True

    def test_agents_protected(self):
        assert MonitorAdapter().is_protected_path("AGENTS.md") is True

    def test_store_protected(self):
        assert MonitorAdapter().is_protected_path("lib-common/lib_common/store.py") is True

    def test_store_subpath(self):
        assert MonitorAdapter().is_protected_path("lib-common/lib_common/store.py.extra") is True

    def test_unprotected(self):
        adapter = MonitorAdapter()
        assert adapter.is_protected_path("codebot/orchestrator.py") is False
        assert adapter.is_protected_path("tests/test_foo.py") is False
        assert adapter.is_protected_path("random_file.py") is False
        assert adapter.is_protected_path("") is False

    def test_returns_bool(self):
        assert isinstance(MonitorAdapter().is_protected_path("x"), bool)

    def test_differs_from_codebot(self, tmp_path):
        m = MonitorAdapter()
        c = CodeBotAdapter(root=tmp_path)
        assert m.is_protected_path("AGENTS.md") is True
        assert c.is_protected_path("AGENTS.md") is False
        assert c.is_protected_path("codebot/orchestrator.py") is True
        assert m.is_protected_path("codebot/orchestrator.py") is False


class TestValidateProject:
    def test_returns_list(self):
        assert isinstance(MonitorAdapter().validate_project(), list)

    def test_each_error_is_string(self):
        for e in MonitorAdapter().validate_project():
            assert isinstance(e, str)

    def test_missing_paths_reported_when_not_exists(self):
        with patch.object(Path, "exists", return_value=False):
            errors = MonitorAdapter().validate_project()
            assert any("repository root missing" in e for e in errors)
            assert any("project config missing" in e for e in errors)
            assert any("constitution missing" in e for e in errors)
            assert any("component path missing" in e for e in errors)

    def test_all_exist_no_errors(self):
        with patch.object(Path, "exists", return_value=True):
            errors = MonitorAdapter().validate_project()
            assert errors == []

    def test_partial_missing_only_some_errors(self):
        original_exists = Path.exists

        def fake_exists(self):
            if str(self).endswith("constitution.md"):
                return False
            return True

        with patch.object(Path, "exists", fake_exists):
            errors = MonitorAdapter().validate_project()
            assert any("constitution missing" in e for e in errors)
            assert not any("repository root missing" in e for e in errors)


class TestInterfaceConsistency:
    def test_both_are_project_adapter_subclasses(self, tmp_path):
        assert isinstance(MonitorAdapter(), ProjectAdapter)
        assert isinstance(CodeBotAdapter(root=tmp_path), ProjectAdapter)

    def test_both_have_no_remaining_abstracts(self, tmp_path):
        assert len(getattr(MonitorAdapter, "__abstractmethods__", set())) == 0
        assert len(getattr(CodeBotAdapter, "__abstractmethods__", set())) == 0

    def test_both_implement_same_methods(self, tmp_path):
        expected = {
            "project_name", "paths", "test_config", "dependency_policy",
            "autonomy_config", "components", "bot_registry", "model_profiles",
            "tier_priority", "prompt_directory", "api_runner_command",
            "is_protected_path", "validate_project",
        }
        for adapter in (MonitorAdapter(), CodeBotAdapter(root=tmp_path)):
            for m in expected:
                assert hasattr(adapter, m)
                assert callable(getattr(adapter, m))

    def test_both_paths_return_project_paths(self, tmp_path):
        assert isinstance(MonitorAdapter().paths(), ProjectPaths)
        assert isinstance(CodeBotAdapter(root=tmp_path).paths(), ProjectPaths)

    def test_both_test_config_same_type(self, tmp_path):
        assert isinstance(MonitorAdapter().test_config(), ProjectTestConfig)
        assert isinstance(CodeBotAdapter(root=tmp_path).test_config(), ProjectTestConfig)

    def test_both_dependency_policy_same_type(self, tmp_path):
        assert isinstance(MonitorAdapter().dependency_policy(), DependencyPolicy)
        assert isinstance(CodeBotAdapter(root=tmp_path).dependency_policy(), DependencyPolicy)

    def test_both_autonomy_config_same_type(self, tmp_path):
        assert isinstance(MonitorAdapter().autonomy_config(), AutonomyConfig)
        assert isinstance(CodeBotAdapter(root=tmp_path).autonomy_config(), AutonomyConfig)

    def test_both_bot_registry_list_of_dicts(self, tmp_path):
        for adapter in (MonitorAdapter(), CodeBotAdapter(root=tmp_path)):
            reg = adapter.bot_registry()
            assert isinstance(reg, list)
            for entry in reg:
                assert isinstance(entry, dict)
                assert "name" in entry
                assert "model" in entry
                assert "tier" in entry

    def test_both_model_profiles_dict(self, tmp_path):
        assert isinstance(MonitorAdapter().model_profiles(), dict)
        assert isinstance(CodeBotAdapter(root=tmp_path).model_profiles(), dict)

    def test_both_tier_priority_dict_str_int(self, tmp_path):
        for adapter in (MonitorAdapter(), CodeBotAdapter(root=tmp_path)):
            tp = adapter.tier_priority()
            assert isinstance(tp, dict)
            for k, v in tp.items():
                assert isinstance(k, str)
                assert isinstance(v, int)

    def test_both_prompt_directory_path(self, tmp_path):
        assert isinstance(MonitorAdapter().prompt_directory(), Path)
        assert isinstance(CodeBotAdapter(root=tmp_path).prompt_directory(), Path)

    def test_both_api_runner_command_structure(self, tmp_path):
        for adapter, bot, prompt in [
            (MonitorAdapter(), "issues", "ISSUES_BOT.md"),
            (CodeBotAdapter(root=tmp_path), "bug_hunter", "bug_hunter.md"),
        ]:
            cmd = adapter.api_runner_command(bot, prompt)
            assert isinstance(cmd, list)
            assert "python3" in cmd[0]
            assert "--bot" in cmd
            assert bot in cmd
            assert "--prompt" in cmd

    def test_both_is_protected_path_bool(self, tmp_path):
        for adapter in (MonitorAdapter(), CodeBotAdapter(root=tmp_path)):
            assert isinstance(adapter.is_protected_path("any/path.py"), bool)

    def test_both_validate_project_list(self, tmp_path):
        assert isinstance(MonitorAdapter().validate_project(), list)
        assert isinstance(CodeBotAdapter(root=tmp_path).validate_project(), list)

    def test_orchestrator_accepts_both(self, tmp_path):
        from codebot import orchestrator
        try:
            orchestrator.set_project_adapter(MonitorAdapter())
            assert orchestrator.STATE_DIR == MONITOR_ROOT / "bots" / "state"
            assert orchestrator.LOGS_DIR == MONITOR_ROOT / "bots" / "logs"
            orchestrator.set_project_adapter(CodeBotAdapter(root=tmp_path))
            assert orchestrator.STATE_DIR == tmp_path.resolve() / ".codebot" / "state"
        finally:
            orchestrator.set_project_adapter(CodeBotAdapter())

    def test_api_runner_accepts_both(self, tmp_path):
        from codebot import api_runner
        try:
            api_runner.set_project_adapter(MonitorAdapter())
            assert api_runner.DRAIN_FILE == MONITOR_ROOT / "bots" / "state" / ".drain"
            assert api_runner.WORK_ROOT == MONITOR_ROOT
            api_runner.set_project_adapter(CodeBotAdapter(root=tmp_path))
            assert api_runner.DRAIN_FILE == tmp_path.resolve() / ".codebot" / "state" / ".drain"
        finally:
            api_runner.set_project_adapter(CodeBotAdapter())

    def test_components_all_component_def(self, tmp_path):
        for adapter in (MonitorAdapter(), CodeBotAdapter(root=tmp_path)):
            for c in adapter.components():
                assert isinstance(c, ComponentDef)
                assert isinstance(c.name, str)
                assert isinstance(c.path, str)
