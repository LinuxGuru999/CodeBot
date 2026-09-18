"""Tests for project_adapter.py + monitor_adapter.py."""
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from project_adapter import ProjectAdapter, ProjectPaths, ProjectTestConfig, DependencyPolicy, AutonomyConfig, ComponentDef
from monitor_adapter import MonitorAdapter


class TestMonitorAdapterInterface:
    def setup_method(self):
        self.adapter = MonitorAdapter()

    def test_project_name(self):
        assert self.adapter.project_name() == "monitor"

    def test_paths_returns_project_paths(self):
        p = self.adapter.paths()
        assert p.state_dir.name == "state"
        assert p.logs_dir.name == "logs"
        assert p.docs_dir.name == "docs"

    def test_test_config(self):
        tc = self.adapter.test_config()
        assert tc.framework == "pytest"
        assert len(tc.test_directories) >= 3

    def test_dependency_policy(self):
        dp = self.adapter.dependency_policy()
        assert dp.policy == "stdlib-only"
        assert any(d["name"] == "argon2-cffi" for d in dp.allowed_third_party)

    def test_autonomy_config(self):
        ac = self.adapter.autonomy_config()
        assert ac.level == 2
        assert "authentication_architecture" in ac.human_approval_required_for

    def test_components(self):
        comps = self.adapter.components()
        assert len(comps) >= 4
        names = [c.name for c in comps]
        assert "manager" in names
        assert "client" in names
        assert "lib-common" in names
        assert "botnet" in names

    def test_bot_registry(self):
        bots = self.adapter.bot_registry()
        assert len(bots) >= 16
        names = [b["name"] for b in bots]
        assert "issues" in names
        assert "worker-1" in names

    def test_model_profiles(self):
        profiles = self.adapter.model_profiles()
        assert len(profiles) >= 10
        assert "qwen-3.8-max" in profiles
        assert "xiaomi-mimo-2.5" in profiles

    def test_tier_priority(self):
        tp = self.adapter.tier_priority()
        assert "issues" in tp
        assert tp["issues"] < tp["doc_sync"]

    def test_prompt_directory_exists(self):
        pd = self.adapter.prompt_directory()
        assert isinstance(pd, Path)

    def test_api_runner_command(self):
        cmd = self.adapter.api_runner_command("test-bot", "TEST_BOT.md")
        assert "python3" in cmd[0]
        assert "--bot" in cmd
        assert "test-bot" in cmd

    def test_protected_paths(self):
        assert self.adapter.is_protected_path(".codebot/constitution.md") is True
        assert self.adapter.is_protected_path("AGENTS.md") is True
        assert self.adapter.is_protected_path("random_file.py") is False

    def test_validate_project(self):
        errors = self.adapter.validate_project()
        assert isinstance(errors, list)


class TestProjectAdapterABC:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            ProjectAdapter()
