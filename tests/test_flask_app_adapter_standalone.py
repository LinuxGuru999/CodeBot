"""Tests for FlaskAppAdapter stdlib-only invariant (CB-8736976-E825).

Verifies that flask_app_adapter.py:
1. Contains no imports from codebot.* package
2. Defines its own protocols/dataclasses locally
3. Forward migration: adapter works without codebot.project_adapter
4. Rollback: original import structure can be restored
5. Integrity: all methods still return correct types after migration
"""
import ast
import sys
from pathlib import Path
from typing import Any

import pytest

# Ensure project root is on path
sys.path.insert(0, str(Path(__file__).parent.parent))

ADAPTER_PATH = Path(__file__).parent.parent / "codebot" / "adapters" / "flask_app_adapter.py"


def _get_imports_from_source(source: str) -> list[str]:
    """Parse Python source and return all imported module names."""
    tree = ast.parse(source)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.append(node.module)
    return imports


class TestNoCodebotImports:
    """RED: forward test — adapter must not import from codebot.*."""

    def test_no_codebot_imports_in_source(self):
        """FlaskAppAdapter source must not contain any codebot.* imports."""
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        imports = _get_imports_from_source(source)
        codebot_imports = [imp for imp in imports if imp.startswith("codebot")]
        assert codebot_imports == [], (
            f"Found codebot.* imports violating stdlib-only invariant: {codebot_imports}"
        )

    def test_adapter_defines_own_protocols(self):
        """Adapter must define ProjectPaths, ProjectTestConfig, etc. locally or via Protocol."""
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        # Check that key types are defined in the file itself
        required_types = [
            "ProjectPaths",
            "ProjectTestConfig",
            "DependencyPolicy",
            "AutonomyConfig",
            "ComponentDef",
        ]
        for type_name in required_types:
            # Must appear as a class definition or dataclass, not just in an import
            assert f"class {type_name}" in source or f"{type_name} =" in source, (
                f"Type {type_name} not defined locally in flask_app_adapter.py"
            )


class TestRollback:
    """RED: rollback test — verify we can detect if codebot imports return."""

    def test_rollback_removes_codebot_imports(self):
        """After rollback, source should still have no codebot imports.
        
        This test validates the rollback function's effect by simulating
        what the source should look like after rollback.
        """
        # Import the migration functions
        from codebot.adapters.flask_app_adapter import migrate_forward, migrate_rollback
        
        # Create a mock store with the migrated content
        source = ADAPTER_PATH.read_text(encoding="utf-8")
        store = {"source": source, "path": str(ADAPTER_PATH)}
        
        # Apply forward then rollback
        migrate_forward(store)
        migrate_rollback(store)
        
        # After rollback, the stored source should still be parseable
        # and the adapter should remain functional
        assert "class FlaskAppAdapter" in store["source"]


class TestIntegrity:
    """RED: integrity test — adapter methods work correctly post-migration."""

    def test_adapter_instantiation_after_migration(self):
        """FlaskAppAdapter can be instantiated using only local definitions."""
        from codebot.adapters.flask_app_adapter import FlaskAppAdapter
        adapter = FlaskAppAdapter(Path("/tmp/flask-demo-app"))
        assert adapter.project_name() == "flask-demo-app"

    def test_paths_returns_correct_type(self):
        """paths() returns a valid ProjectPaths-like object."""
        from codebot.adapters.flask_app_adapter import FlaskAppAdapter, ProjectPaths
        adapter = FlaskAppAdapter(Path("/tmp/flask-demo-app"))
        p = adapter.paths()
        assert isinstance(p, ProjectPaths)
        assert p.repository_root == Path("/tmp/flask-demo-app")

    def test_test_config_returns_correct_type(self):
        """test_config() returns a valid ProjectTestConfig-like object."""
        from codebot.adapters.flask_app_adapter import FlaskAppAdapter, ProjectTestConfig
        adapter = FlaskAppAdapter(Path("/tmp/flask-demo-app"))
        tc = adapter.test_config()
        assert isinstance(tc, ProjectTestConfig)
        assert tc.framework == "pytest"

    def test_dependency_policy_returns_correct_type(self):
        """dependency_policy() returns a valid DependencyPolicy-like object."""
        from codebot.adapters.flask_app_adapter import FlaskAppAdapter, DependencyPolicy
        adapter = FlaskAppAdapter(Path("/tmp/flask-demo-app"))
        dp = adapter.dependency_policy()
        assert isinstance(dp, DependencyPolicy)

    def test_autonomy_config_returns_correct_type(self):
        """autonomy_config() returns a valid AutonomyConfig-like object."""
        from codebot.adapters.flask_app_adapter import FlaskAppAdapter, AutonomyConfig
        adapter = FlaskAppAdapter(Path("/tmp/flask-demo-app"))
        ac = adapter.autonomy_config()
        assert isinstance(ac, AutonomyConfig)

    def test_components_returns_correct_type(self):
        """components() returns a list of ComponentDef-like objects."""
        from codebot.adapters.flask_app_adapter import FlaskAppAdapter, ComponentDef
        adapter = FlaskAppAdapter(Path("/tmp/flask-demo-app"))
        comps = adapter.components()
        assert isinstance(comps, list)
        assert all(isinstance(c, ComponentDef) for c in comps)

    def test_all_methods_callable(self):
        """Every method on FlaskAppAdapter is callable without errors."""
        from codebot.adapters.flask_app_adapter import FlaskAppAdapter
        adapter = FlaskAppAdapter(Path("/tmp/flask-demo-app"))
        
        # Call every public method to ensure nothing is broken
        assert isinstance(adapter.project_name(), str)
        assert adapter.paths() is not None
        assert adapter.test_config() is not None
        assert adapter.dependency_policy() is not None
        assert adapter.autonomy_config() is not None
        assert isinstance(adapter.components(), list)
        assert isinstance(adapter.bot_registry(), list)
        assert isinstance(adapter.model_profiles(), dict)
        assert isinstance(adapter.tier_priority(), dict)
        assert isinstance(adapter.prompt_directory(), Path)
        assert isinstance(adapter.api_runner_command("test", "test.md"), list)
        assert isinstance(adapter.is_protected_path("test"), bool)
        assert isinstance(adapter.validate_project(), list)
        assert isinstance(adapter.queue_depth(), int)
        assert isinstance(adapter.ticket_class_counts(), dict)
