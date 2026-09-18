"""Tests for project_adapter.py — ABC interface compliance.

Uses a minimal concrete adapter for testing. monitor_adapter.py is
intentionally excluded from the extracted CodeBot repo (it lives in
Monitor's bots/ directory).
"""
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.project_adapter import (
    ProjectAdapter,
    ProjectPaths,
    ProjectTestConfig,
    DependencyPolicy,
    AutonomyConfig,
    ComponentDef,
)


class _StubAdapter(ProjectAdapter):
    """Minimal concrete adapter for testing the ABC contract."""

    def __init__(self, root: Path) -> None:
        self._root = root

    def project_name(self) -> str:
        return "stub-project"

    def paths(self) -> ProjectPaths:
        r = self._root
        return ProjectPaths(
            repository_root=r,
            state_dir=r / "state",
            logs_dir=r / "logs",
            docs_dir=r / "docs",
            issues_dir=r / "docs" / "issues",
            adr_dir=r / "docs" / "adr",
            modules_docs_dir=r / "docs" / "modules",
            queue_file=r / "QUEUE.md",
            api_contract=r / "api" / "contract.md",
            entrypoint=r / "ENTRYPOINT.md",
            context_map=r / "CONTEXT-MAP.md",
            bugs_file=r / "bugs.md",
            features_file=r / "features.md",
            roadmap_file=r / "roadmap.md",
            constitution_file=r / ".codebot" / "constitution.md",
            project_config=r / ".codebot" / "project.yaml",
        )

    def test_config(self) -> ProjectTestConfig:
        return ProjectTestConfig(
            framework="pytest",
            test_command="python3 -m pytest -q",
            test_directories=["tests/"],
            coverage_tool="none",
            lint_tool="none",
            type_checker="none",
            type_check_command="",
        )

    def dependency_policy(self) -> DependencyPolicy:
        return DependencyPolicy(policy="stdlib-only", allowed_third_party=[], dependency_files=[])

    def autonomy_config(self) -> AutonomyConfig:
        return AutonomyConfig(level=2, human_approval_required_for=["secrets"], autonomous_allowed_for=["test_additions"])

    def components(self) -> list[ComponentDef]:
        return [ComponentDef("core", "src/", "backend", "python", "Core logic")]

    def bot_registry(self) -> list[dict]:
        return [{"name": "worker-1", "prompt": "WORKER.md", "interval": 300, "model": "default", "tier": 1}]

    def model_profiles(self) -> dict[str, dict]:
        return {"default": {"lockup_risk": "low", "heartbeat_multiplier": 1.5, "log_stall_seconds": 120, "restart_cooldown": 5}}

    def tier_priority(self) -> dict[str, int]:
        return {"worker-1": 11}

    def prompt_directory(self) -> Path:
        return self._root / "prompts"

    def api_runner_command(self, bot_name: str, prompt_file: str) -> list[str]:
        return ["python3", "-m", "bots.api_runner", "--bot", bot_name]

    def is_protected_path(self, path: str) -> bool:
        return path.startswith(".codebot/constitution.md")

    def validate_project(self) -> list[str]:
        errors = []
        if not self._root.exists():
            errors.append("root missing")
        return errors


class TestProjectAdapterABC:
    def test_cannot_instantiate_abstract(self):
        with pytest.raises(TypeError):
            ProjectAdapter()


class TestStubAdapterInterface:
    def setup_method(self):
        self.adapter = _StubAdapter(Path("/tmp/opencode/codebot-test-project"))

    def test_project_name(self):
        assert self.adapter.project_name() == "stub-project"

    def test_paths_returns_project_paths(self):
        p = self.adapter.paths()
        assert isinstance(p, ProjectPaths)
        assert p.state_dir.name == "state"
        assert p.logs_dir.name == "logs"
        assert p.docs_dir.name == "docs"
        assert p.constitution_file.name == "constitution.md"

    def test_test_config(self):
        tc = self.adapter.test_config()
        assert isinstance(tc, ProjectTestConfig)
        assert tc.framework == "pytest"
        assert len(tc.test_directories) >= 1

    def test_dependency_policy(self):
        dp = self.adapter.dependency_policy()
        assert isinstance(dp, DependencyPolicy)
        assert dp.policy == "stdlib-only"

    def test_autonomy_config(self):
        ac = self.adapter.autonomy_config()
        assert isinstance(ac, AutonomyConfig)
        assert ac.level == 2
        assert "secrets" in ac.human_approval_required_for

    def test_components(self):
        comps = self.adapter.components()
        assert len(comps) >= 1
        assert comps[0].name == "core"
        assert comps[0].language == "python"

    def test_bot_registry(self):
        bots = self.adapter.bot_registry()
        assert len(bots) >= 1
        assert bots[0]["name"] == "worker-1"

    def test_model_profiles(self):
        profiles = self.adapter.model_profiles()
        assert "default" in profiles
        assert profiles["default"]["lockup_risk"] == "low"

    def test_tier_priority(self):
        tp = self.adapter.tier_priority()
        assert "worker-1" in tp

    def test_prompt_directory_exists(self):
        pd = self.adapter.prompt_directory()
        assert isinstance(pd, Path)

    def test_api_runner_command(self):
        cmd = self.adapter.api_runner_command("test-bot", "TEST.md")
        assert "python3" in cmd[0]
        assert "--bot" in cmd
        assert "test-bot" in cmd

    def test_protected_paths(self):
        assert self.adapter.is_protected_path(".codebot/constitution.md") is True
        assert self.adapter.is_protected_path("random_file.py") is False

    def test_validate_project_existing_root(self):
        errors = self.adapter.validate_project()
        assert isinstance(errors, list)
        assert len(errors) == 0

    def test_validate_project_missing_root(self):
        bad = _StubAdapter(Path("/nonexistent/path"))
        errors = bad.validate_project()
        assert len(errors) > 0
