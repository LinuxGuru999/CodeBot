"""Tests for CB-3498264-B932: orchestrator.py path configuration encapsulation.

Acceptance criteria:
1. Removal of `global` keyword for path variables in set_project_adapter
2. Paths accessed via adapter or config object (PathConfig)
"""

import ast
import inspect
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

import codebot.orchestrator as orch


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
    """AC2: Paths must be accessed via a config object."""

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
        # Check via dataclass fields or __init__ annotations
        import dataclasses
        if dataclasses.is_dataclass(pc):
            field_names = {f.name for f in dataclasses.fields(pc)}
        else:
            field_names = set(getattr(pc, "__annotations__", {}).keys())
        missing = required - field_names
        assert not missing, f"PathConfig missing fields: {missing}"

    def test_paths_instance_exists(self):
        """A module-level _paths instance of PathConfig must exist."""
        assert hasattr(orch, "_paths"), "orchestrator must have a _paths instance"
        assert isinstance(orch._paths, orch.PathConfig)

    def test_set_project_adapter_updates_path_config(self, tmp_path):
        """set_project_adapter must update _paths without using global."""
        # Save original
        orig = orch._paths.state_dir

        # Create a mock adapter
        mock_paths = MagicMock()
        mock_paths.repository_root = tmp_path / "repo"
        mock_paths.state_dir = tmp_path / "state"
        mock_paths.logs_dir = tmp_path / "logs"
        mock_paths.state_dir.mkdir(parents=True, exist_ok=True)
        mock_paths.logs_dir.mkdir(parents=True, exist_ok=True)

        mock_adapter = MagicMock()
        mock_adapter.paths.return_value = mock_paths

        try:
            orch.set_project_adapter(mock_adapter)
            assert orch._paths.state_dir == tmp_path / "state"
            assert orch._paths.logs_dir == tmp_path / "logs"
            assert orch._paths.bots_dir == tmp_path / "repo"
            assert orch._paths.drain_file == tmp_path / "state" / ".drain"
            assert orch._paths.update_lock == tmp_path / "state" / ".update_lock"
            assert orch._paths.restart_file == tmp_path / "state" / ".restart"
        finally:
            # Restore
            orch._paths.state_dir = orig
            orch._paths.logs_dir = orig.parent / "logs"
            orch._paths.bots_dir = orig.parent.parent
            orch._paths.backup_dir = orig / "backup"
            orch._paths.alignment_events_dir = orig / "alignment_events"
            orch._paths.drain_file = orig / ".drain"
            orch._paths.update_lock = orig / ".update_lock"
            orch._paths.restart_file = orig / ".restart"
            orch._adapter_instance = None

    def test_is_draining_uses_path_config(self, tmp_path):
        """is_draining() must use _paths.drain_file."""
        drain = tmp_path / ".drain"
        orig_drain = orch._paths.drain_file
        try:
            orch._paths.drain_file = drain
            assert orch.is_draining() is False
            drain.touch()
            assert orch.is_draining() is True
        finally:
            orch._paths.drain_file = orig_drain


class TestBackwardCompatibility:
    """Module-level access to path names must still work for external code."""

    def test_module_getattr_provides_state_dir(self):
        """orch.STATE_DIR should still resolve via __getattr__ for compat."""
        val = orch.STATE_DIR
        assert isinstance(val, Path)
        assert val == orch._paths.state_dir

    def test_module_getattr_provides_bots_dir(self):
        val = orch.BOTS_DIR
        assert isinstance(val, Path)
        assert val == orch._paths.bots_dir

    def test_module_getattr_provides_logs_dir(self):
        val = orch.LOGS_DIR
        assert isinstance(val, Path)
        assert val == orch._paths.logs_dir

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
        assert orch._paths.bots_dir == _project_root
        assert orch._paths.state_dir == _project_root / ".codebot" / "state"
        assert orch._paths.logs_dir == _project_root / ".codebot" / "logs"
        assert orch._paths.backup_dir == _project_root / ".codebot" / "state" / "backup"

    def test_default_drain_file_in_state_dir(self):
        """Default drain_file lives under state_dir."""
        assert orch._paths.drain_file == orch._paths.state_dir / ".drain"

    def test_default_update_lock_in_state_dir(self):
        """Default update_lock lives under state_dir."""
        assert orch._paths.update_lock == orch._paths.state_dir / ".update_lock"

    def test_default_restart_file_in_state_dir(self):
        """Default restart_file lives under state_dir."""
        assert orch._paths.restart_file == orch._paths.state_dir / ".restart"


class TestGlobalsNotMutatedByAdapter:
    """set_project_adapter must not mutate module-level globals."""

    def test_set_adapter_preserves_module_path_attrs(self, tmp_path):
        """Calling set_project_adapter does not create new module-level attributes."""
        attrs_before = set(dir(orch))

        mock_paths = MagicMock()
        mock_paths.repository_root = tmp_path / "repo"
        mock_paths.state_dir = tmp_path / "state"
        mock_paths.logs_dir = tmp_path / "logs"
        mock_paths.state_dir.mkdir(parents=True, exist_ok=True)
        mock_paths.logs_dir.mkdir(parents=True, exist_ok=True)

        mock_adapter = MagicMock()
        mock_adapter.paths.return_value = mock_paths

        # Save originals to restore after test
        orig_paths = orch._paths
        try:
            orch.set_project_adapter(mock_adapter)
            # No new public attributes should have been added to the module
            attrs_after = set(dir(orch))
            new_attrs = attrs_after - attrs_before
            assert not new_attrs, (
                f"set_project_adapter created new module attrs: {new_attrs}"
            )
        finally:
            import codebot.orchestrator as _mod
            _mod._paths = orig_paths
            orch._adapter_instance = None

    def test_set_adapter_does_not_touch_process_manager_globals(self, tmp_path):
        """set_project_adapter must not change any global in process_manager."""
        import codebot.process_manager as pm
        pm_attrs_before = {
            name: getattr(pm, name)
            for name in dir(pm)
            if name.endswith("_DIR") or name.endswith("_FILE") or name == "BOTS_DIR"
        }

        mock_paths = MagicMock()
        mock_paths.repository_root = tmp_path / "repo"
        mock_paths.state_dir = tmp_path / "state"
        mock_paths.logs_dir = tmp_path / "logs"
        mock_paths.state_dir.mkdir(parents=True, exist_ok=True)
        mock_paths.logs_dir.mkdir(parents=True, exist_ok=True)

        mock_adapter = MagicMock()
        mock_adapter.paths.return_value = mock_paths

        orig_paths = orch._paths
        try:
            orch.set_project_adapter(mock_adapter)
            pm_attrs_after = {
                name: getattr(pm, name)
                for name in dir(pm)
                if name.endswith("_DIR") or name.endswith("_FILE") or name == "BOTS_DIR"
            }
            # Nothing in process_manager should have changed
            for name in pm_attrs_before:
                assert pm_attrs_before[name] == pm_attrs_after[name], (
                    f"set_project_adapter mutated process_manager.{name}"
                )
        finally:
            import codebot.orchestrator as _mod
            _mod._paths = orig_paths
            orch._adapter_instance = None
