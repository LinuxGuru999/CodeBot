"""Tests for CB-3498264-B932/CB-2667961-7277: orchestrator.py path configuration encapsulation.

Acceptance criteria:
1. Removal of `global` keyword for path variables in set_project_adapter
2. Paths accessed via adapter or config object (PathConfig), not mutable module globals
3. No module-level mutable path globals remain
"""

import ast
import dataclasses
import inspect
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import codebot.orchestrator as orch
import codebot.state_manager as sm
from codebot.state_manager import get_paths, PathConfig


@pytest.fixture(autouse=True)
def _reset_state_manager_paths():
    """Save and restore state_manager._paths after each test to avoid cross-test pollution."""
    original = sm._paths
    yield
    sm._paths = original
    sm._adapter_instance = None


class TestNoGlobalPathMutation:
    """AC1: set_project_adapter must NOT use `global` for path variables."""

    def test_set_project_adapter_no_global_path_vars(self):
        """The `global` statement in set_project_adapter must not list path variables."""
        source = inspect.getsource(orch.set_project_adapter)
        tree = ast.parse(textwrap.dedent(source))
        path_var_names = {
            "BOTS_DIR", "STATE_DIR", "LOGS_DIR", "BACKUP_DIR",
            "ALIGNMENT_EVENTS_DIR", "DRAIN_FILE", "UPDATE_LOCK", "RESTART_FILE",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.Global):
                overlap = path_var_names.intersection(node.names)
                assert not overlap, (
                    f"set_project_adapter still uses `global` for path variables: {overlap}"
                )

    def test_module_level_no_global_path_vars_in_set_adapter(self):
        """AST-level check on the full module: set_project_adapter has no global path vars."""
        module_path = Path(orch.__file__)
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        path_var_names = {
            "BOTS_DIR", "STATE_DIR", "LOGS_DIR", "BACKUP_DIR",
            "ALIGNMENT_EVENTS_DIR", "DRAIN_FILE", "UPDATE_LOCK", "RESTART_FILE",
        }
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "set_project_adapter":
                for child in ast.walk(node):
                    if isinstance(child, ast.Global):
                        overlap = path_var_names.intersection(child.names)
                        assert not overlap, (
                            f"set_project_adapter uses `global` for: {overlap}"
                        )


class TestPathConfigObject:
    """AC2: Paths must be accessed via a config object (PathConfig) from get_paths()."""

    def test_path_config_class_exists(self):
        """A PathConfig dataclass (or similar) must exist in orchestrator."""
        assert hasattr(orch, "PathConfig"), "orchestrator must define a PathConfig class"

    def test_path_config_has_required_fields(self):
        """PathConfig must expose all required path attributes."""
        pc = orch.PathConfig
        required = {
            "bots_dir", "state_dir", "logs_dir", "backup_dir",
            "alignment_events_dir", "drain_file", "update_lock", "restart_file",
        }
        if dataclasses.is_dataclass(pc):
            field_names = {f.name for f in dataclasses.fields(pc)}
        else:
            field_names = set(getattr(pc, "__annotations__", {}).keys())
        missing = required - field_names
        assert not missing, f"PathConfig missing fields: {missing}"

    def test_paths_available_via_get_paths(self):
        """Paths must be accessible via get_paths() returning a PathConfig instance."""
        p = get_paths()
        assert isinstance(p, PathConfig), (
            f"get_paths() must return a PathConfig instance, got {type(p)}"
        )

    def test_no_module_level_mutable_paths_global(self):
        """orchestrator module must NOT have a _paths module-level mutable global."""
        assert not hasattr(orch, "_paths"), (
            "orchestrator must NOT have a _paths module-level variable; "
            "use get_paths() or __getattr__ for dynamic resolution"
        )

    def test_set_project_adapter_returns_path_config(self, tmp_path):
        """set_project_adapter must return a PathConfig updated via state_manager."""
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "state" / "backup").mkdir(parents=True, exist_ok=True)
        (tmp_path / "state" / "alignment_events").mkdir(parents=True, exist_ok=True)

        # adapter.paths() must return object with repository_root, state_dir, logs_dir
        mock_paths = MagicMock()
        mock_paths.repository_root = tmp_path / "repo"
        mock_paths.state_dir = tmp_path / "state"
        mock_paths.logs_dir = tmp_path / "logs"

        mock_adapter = MagicMock()
        mock_adapter.paths.return_value = mock_paths

        result = orch.set_project_adapter(mock_adapter)
        assert isinstance(result, PathConfig)
        assert result.state_dir == tmp_path / "state"
        assert result.logs_dir == tmp_path / "logs"
        assert result.bots_dir == tmp_path / "repo"

        # Verify get_paths() now returns the updated paths
        current = get_paths()
        assert current.state_dir == tmp_path / "state"
        assert current.logs_dir == tmp_path / "logs"

    def test_is_draining_uses_dynamic_paths(self, tmp_path):
        """is_draining() must work with dynamically resolved paths."""
        tmp_path.mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
        (tmp_path / "backup").mkdir(parents=True, exist_ok=True)
        (tmp_path / "alignment_events").mkdir(parents=True, exist_ok=True)

        mock_paths = MagicMock()
        mock_paths.repository_root = tmp_path / "repo"
        mock_paths.state_dir = tmp_path
        mock_paths.logs_dir = tmp_path / "logs"

        mock_adapter = MagicMock()
        mock_adapter.paths.return_value = mock_paths
        orch.set_project_adapter(mock_adapter)

        # The drain_file is derived from state_dir + ".drain"
        assert orch.is_draining() is False
        actual_drain = get_paths().drain_file
        actual_drain.touch()
        assert orch.is_draining() is True


class TestBackwardCompatibility:
    """Module-level access to path names must still work for external code."""

    def test_module_getattr_provides_state_dir(self):
        """orch.STATE_DIR should still resolve via __getattr__ for compat."""
        val = orch.STATE_DIR
        assert isinstance(val, Path)
        expected = get_paths().state_dir
        assert val == expected

    def test_module_getattr_provides_bots_dir(self):
        val = orch.BOTS_DIR
        assert isinstance(val, Path)
        expected = get_paths().bots_dir
        assert val == expected

    def test_module_getattr_provides_logs_dir(self):
        val = orch.LOGS_DIR
        assert isinstance(val, Path)
        expected = get_paths().logs_dir
        assert val == expected

    def test_module_getattr_provides_all_path_names(self):
        """All legacy path names resolve via __getattr__."""
        path_names = [
            "BOTS_DIR", "STATE_DIR", "LOGS_DIR", "BACKUP_DIR",
            "ALIGNMENT_EVENTS_DIR", "DRAIN_FILE", "UPDATE_LOCK", "RESTART_FILE",
        ]
        for name in path_names:
            val = getattr(orch, name)
            assert val is not None, f"orch.{name} resolved to None"


class TestDefaultImportTimeBehavior:
    """AC: Default import-time behavior preserved when set_project_adapter never called."""

    def test_default_paths_derived_from_project_root(self):
        """Without calling set_project_adapter, paths default to project-root."""
        from codebot.state_manager import _project_root
        p = get_paths()
        assert p.bots_dir == _project_root
        assert p.state_dir == _project_root / ".codebot" / "state"
        assert p.logs_dir == _project_root / ".codebot" / "logs"
        assert p.backup_dir == _project_root / ".codebot" / "state" / "backup"

    def test_default_drain_file_in_state_dir(self):
        """Default drain_file lives under state_dir."""
        p = get_paths()
        assert p.drain_file == p.state_dir / ".drain"

    def test_default_update_lock_in_state_dir(self):
        """Default update_lock lives under state_dir."""
        p = get_paths()
        assert p.update_lock == p.state_dir / ".update_lock"

    def test_default_restart_file_in_state_dir(self):
        """Default restart_file lives under state_dir."""
        p = get_paths()
        assert p.restart_file == p.state_dir / ".restart"


class TestGlobalsNotMutatedByAdapter:
    """set_project_adapter must not create or mutate module-level attributes."""

    def test_set_adapter_preserves_module_attrs(self, tmp_path):
        """Calling set_project_adapter does not create new module-level attributes."""
        attrs_before = set(dir(orch))

        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        mock_paths = MagicMock()
        mock_paths.repository_root = tmp_path / "repo"
        mock_paths.state_dir = tmp_path / "state"
        mock_paths.logs_dir = tmp_path / "logs"

        mock_adapter = MagicMock()
        mock_adapter.paths.return_value = mock_paths

        orch.set_project_adapter(mock_adapter)
        # No new public attributes should have been added to the module
        attrs_after = set(dir(orch))
        new_attrs = attrs_after - attrs_before
        assert not new_attrs, (
            f"set_project_adapter created new module attrs: {new_attrs}"
        )

    def test_set_project_adapter_does_not_mutate_module_globals(self, tmp_path):
        """Calling set_project_adapter must not create module-level BOTS_DIR attribute.
        
        Verifies that orchestrator.BOTS_DIR is proxied via __getattr__, not directly
        assigned to the module __dict__. This proves dependency injection works
        without global mutation.
        """
        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        mock_paths = MagicMock()
        mock_paths.repository_root = tmp_path / "repo"
        mock_paths.state_dir = tmp_path / "state"
        mock_paths.logs_dir = tmp_path / "logs"

        mock_adapter = MagicMock()
        mock_adapter.paths.return_value = mock_paths

        orch.set_project_adapter(mock_adapter)

        # BOTS_DIR should be accessible via __getattr__
        assert orch.BOTS_DIR == tmp_path / "repo"
        # But BOTS_DIR must NOT be in the module's __dict__ (proves __getattr__ proxy)
        assert "BOTS_DIR" not in orch.__dict__, (
            "BOTS_DIR should not be in orchestrator.__dict__; "
            "it must be resolved via __getattr__ proxy"
        )

    def test_set_adapter_does_not_touch_process_manager_globals(self, tmp_path):
        """set_project_adapter must not change any global in process_manager."""
        import codebot.process_manager as pm
        pm_attrs_before = {
            name: getattr(pm, name)
            for name in dir(pm)
            if name.endswith("_DIR") or name.endswith("_FILE") or name == "BOTS_DIR"
        }

        (tmp_path / "state").mkdir(parents=True, exist_ok=True)
        (tmp_path / "logs").mkdir(parents=True, exist_ok=True)

        mock_paths = MagicMock()
        mock_paths.repository_root = tmp_path / "repo"
        mock_paths.state_dir = tmp_path / "state"
        mock_paths.logs_dir = tmp_path / "logs"

        mock_adapter = MagicMock()
        mock_adapter.paths.return_value = mock_paths

        orch.set_project_adapter(mock_adapter)
        pm_attrs_after = {
            name: getattr(pm, name)
            for name in dir(pm)
            if name.endswith("_DIR") or name.endswith("_FILE") or name == "BOTS_DIR"
        }
        for name in pm_attrs_before:
            assert pm_attrs_before[name] == pm_attrs_after[name], (
                f"set_project_adapter mutated process_manager.{name}"
            )


class TestOrchestratorCodeChange:
    """Verify the orchestrator.py code itself is free of module-level _paths mutation."""

    def test_set_project_adapter_has_no_mod_paths_assignment(self):
        """set_project_adapter must not assign to _mod._paths or module._paths."""
        source = inspect.getsource(orch.set_project_adapter)
        assert "_mod._paths" not in source, (
            "set_project_adapter still assigns to _mod._paths"
        )
        assert "orchestrator._paths" not in source, (
            "set_project_adapter still references orchestrator._paths"
        )

    def test_no_module_level_paths_assignment(self):
        """orchestrator.py must not have a module-level _paths = assignment."""
        module_path = Path(orch.__file__)
        source = module_path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in ast.iter_child_nodes(tree):
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == "_paths":
                        pytest.fail(
                            "orchestrator.py still has a module-level _paths assignment"
                        )

    def test_orchestrator_module_has_no__paths_attribute(self):
        """The orchestrator module must not expose a _paths attribute."""
        assert "_paths" not in orch.__dict__, (
            "orchestrator.__dict__ contains '_paths'; must use get_paths() instead"
        )
