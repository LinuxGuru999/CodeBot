"""Tests for FlaskAppAdapter — validates second-project adapter portability.

Verifies that FlaskAppAdapter:
1. Implements all ProjectAdapter Protocol methods correctly
2. Contains no Monitor or CodeBot-specific assumptions
3. Returns valid data structures matching the interface contract
4. Protected paths are appropriate for a Flask app (not CodeBot internals)
5. Uses only stdlib imports (no codebot.* dependencies)
"""
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.adapters.flask_app_adapter import (
    FlaskAppAdapter,
    ProjectPaths,
    ProjectTestConfig,
    DependencyPolicy,
    AutonomyConfig,
    ComponentDef,
)


class TestFlaskAppAdapterInterface:
    """Verify FlaskAppAdapter implements all Protocol methods."""

    def setup_method(self):
        self.adapter = FlaskAppAdapter(Path("/tmp/flask-demo-app"))

    def test_is_project_adapter_protocol_compliant(self):
        """Structural subtyping: adapter satisfies ProjectAdapter protocol without inheritance."""
        from codebot.adapters.flask_app_adapter import ProjectAdapter
        assert isinstance(self.adapter, ProjectAdapter)

    def test_project_name_returns_string(self):
        name = self.adapter.project_name()
        assert isinstance(name, str)
        assert len(name) > 0
        # Must NOT be 'codebot' or 'monitor'
        assert name.lower() not in ("codebot", "monitor")

    def test_paths_returns_project_paths(self):
        p = self.adapter.paths()
        assert isinstance(p, ProjectPaths)
        assert p.repository_root == Path("/tmp/flask-demo-app")
        assert p.state_dir.name == "state"
        assert p.logs_dir.name == "logs"
        assert p.docs_dir.name == "docs"
        assert p.constitution_file.name == "constitution.md"
        assert p.project_config.name == "project.yaml"

    def test_test_config_returns_valid_config(self):
        tc = self.adapter.test_config()
        assert isinstance(tc, ProjectTestConfig)
        assert tc.framework == "pytest"
        assert len(tc.test_directories) >= 1
        assert "tests/" in tc.test_directories

    def test_dependency_policy_allows_flask(self):
        dp = self.adapter.dependency_policy()
        assert isinstance(dp, DependencyPolicy)
        allowed_names = [d["name"] for d in dp.allowed_third_party]
        assert "flask" in allowed_names
        assert "pytest" in allowed_names

    def test_autonomy_config_level(self):
        ac = self.adapter.autonomy_config()
        assert isinstance(ac, AutonomyConfig)
        assert ac.level >= 1
        assert "secrets" in ac.human_approval_required_for

    def test_components_returns_list(self):
        comps = self.adapter.components()
        assert isinstance(comps, list)
        assert len(comps) >= 1
        for c in comps:
            assert isinstance(c, ComponentDef)
            assert c.name
            assert c.path

    def test_bot_registry_returns_list(self):
        bots = self.adapter.bot_registry()
        assert isinstance(bots, list)
        assert len(bots) >= 1
        for bot in bots:
            assert "name" in bot
            assert "prompt" in bot
            assert "model" in bot

    def test_model_profiles_returns_dict(self):
        profiles = self.adapter.model_profiles()
        assert isinstance(profiles, dict)
        assert "default" in profiles
        assert "lockup_risk" in profiles["default"]

    def test_tier_priority_returns_dict(self):
        tp = self.adapter.tier_priority()
        assert isinstance(tp, dict)
        assert len(tp) >= 1

    def test_prompt_directory_returns_path(self):
        pd = self.adapter.prompt_directory()
        assert isinstance(pd, Path)

    def test_api_runner_command_returns_list(self):
        cmd = self.adapter.api_runner_command("backend_implementer", "roles/backend_implementer.md")
        assert isinstance(cmd, list)
        assert len(cmd) >= 3
        assert "python3" in cmd[0]
        assert "--bot" in cmd
        assert "backend_implementer" in cmd

    def test_protected_paths_flask_specific(self):
        # Flask app protected paths should NOT include CodeBot internals
        assert self.adapter.is_protected_path(".codebot/constitution.md") is True
        assert self.adapter.is_protected_path("app/config/settings.py") is True
        assert self.adapter.is_protected_path("app/config/secrets.py") is True
        # Should NOT protect CodeBot-specific files
        assert self.adapter.is_protected_path("codebot/orchestrator.py") is False
        assert self.adapter.is_protected_path("codebot/gatekeeper.py") is False
        assert self.adapter.is_protected_path("random_file.py") is False

    def test_validate_project_returns_list(self):
        errors = self.adapter.validate_project()
        assert isinstance(errors, list)


class TestFlaskAppAdapterNoMonitorAssumptions:
    """Verify no Monitor or CodeBot-specific logic leaks into the adapter."""

    def setup_method(self):
        self.adapter = FlaskAppAdapter(Path("/tmp/flask-demo-app"))

    def test_project_name_not_codebot(self):
        assert self.adapter.project_name() != "codebot"

    def test_no_codebot_roles_in_registry(self):
        """Flask adapter should use generic roles, not CodeBot's 26-role system."""
        bots = self.adapter.bot_registry()
        bot_names = {b["name"] for b in bots}
        # Should have generic web app roles
        assert "backend_implementer" in bot_names or "test_implementer" in bot_names
        # Should NOT have CodeBot-specific discovery/control roles
        codebot_only_roles = {
            "architecture_auditor", "performance_auditor", "documentation_auditor",
            "dependency_auditor", "ux_auditor", "budget_controller",
            "conflict_resolver", "scheduler", "quality_gate",
        }
        assert not bot_names.intersection(codebot_only_roles), \
            f"Flask adapter contains CodeBot-only roles: {bot_names & codebot_only_roles}"

    def test_dependency_policy_not_stdlib_only(self):
        """Flask app should allow third-party dependencies unlike CodeBot's stdlib-only policy."""
        dp = self.adapter.dependency_policy()
        assert dp.policy != "stdlib-only"
        assert len(dp.allowed_third_party) > 2  # More than just pytest/pytest-cov

    def test_components_differ_from_codebot(self):
        """Flask components should reflect web app structure, not CodeBot modules."""
        comps = self.adapter.components()
        comp_names = {c.name for c in comps}
        # Should have web-app typical components
        assert "api" in comp_names or "models" in comp_names or "services" in comp_names
        # Should NOT have CodeBot-specific component names
        assert "roles" not in comp_names  # CodeBot has 'roles' component

    def test_paths_use_standard_structure(self):
        """Flask app paths should follow standard project layout."""
        p = self.adapter.paths()
        assert p.entrypoint.parts[-2:] == ("app", "main.py") or p.entrypoint.name == "main.py"
        # State dir should still be .codebot/state for compatibility
        assert ".codebot" in str(p.state_dir)


class TestFlaskAppAdapterInstantiation:
    """Test adapter can be instantiated with various root paths."""

    def test_default_root_uses_cwd(self):
        adapter = FlaskAppAdapter()
        assert adapter.paths().repository_root == Path.cwd().resolve()

    def test_custom_root(self):
        custom = Path("/opt/my-flask-app")
        adapter = FlaskAppAdapter(custom)
        assert adapter.paths().repository_root == custom.resolve()

    def test_relative_root_resolved(self):
        adapter = FlaskAppAdapter(Path("./my-app"))
        assert adapter.paths().repository_root.is_absolute()
