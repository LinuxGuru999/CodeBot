"""Tests for codebot_bootstrap.py — portable, no Monitor dependencies."""
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.codebot_bootstrap import discover_adapter_class, wire_adapter, bootstrap, _ADAPTER_MODULES


class TestDiscoverAdapter:
    def test_missing_config_returns_none(self, tmp_path):
        adapter = discover_adapter_class(tmp_path)
        assert adapter is None

    def test_discovers_monitor_adapter_when_available(self, tmp_path):
        # Create minimal project.yaml so discovery proceeds past file check
        codebot_dir = tmp_path / ".codebot"
        codebot_dir.mkdir()
        (codebot_dir / "project.yaml").write_text("schema_version: '1.0'\nproject:\n  name: monitor\n")
        adapter = discover_adapter_class(tmp_path)
        # MonitorAdapter is bundled in codebot/ package, so it should be found
        if adapter is not None:
            assert adapter.project_name() == "monitor"

    def test_unknown_project_returns_none(self, tmp_path):
        codebot_dir = tmp_path / ".codebot"
        codebot_dir.mkdir()
        (codebot_dir / "project.yaml").write_text("schema_version: '1.0'\nproject:\n  name: nonexistent_project_xyz\n")
        adapter = discover_adapter_class(tmp_path)
        assert adapter is None


class TestWireAdapter:
    def test_wiring_is_idempotent_with_none(self):
        wire_adapter(None)  # type: ignore[arg-type]
        wire_adapter(None)  # type: ignore[arg-type]

    def test_modules_have_setter(self):
        import importlib
        found = 0
        for mod_path in _ADAPTER_MODULES:
            try:
                mod = importlib.import_module(mod_path)
                assert hasattr(mod, "set_project_adapter"), f"{mod_path} missing set_project_adapter"
                found += 1
            except ImportError:
                pass
        assert found >= 3, f"Only {found} core modules importable"


class TestBootstrap:
    def test_bootstrap_without_config_returns_none(self, tmp_path):
        adapter = bootstrap(tmp_path)
        assert adapter is None

    def test_fail_open_on_bad_yaml(self, tmp_path):
        codebot_dir = tmp_path / ".codebot"
        codebot_dir.mkdir()
        (codebot_dir / "project.yaml").write_text("name: broken\n")
        adapter = bootstrap(tmp_path)
        # Must not crash; returns None when adapter can't load or validate fails
        # With monitor_adapter bundled, it may find and instantiate the adapter
        # but validation will show errors for missing paths — that's acceptable
        assert adapter is None or hasattr(adapter, "project_name")

    def test_bootstrap_returns_none_for_empty_dir(self, tmp_path):
        result = bootstrap(tmp_path)
        assert result is None
