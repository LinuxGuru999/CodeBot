"""Tests for codebot_bootstrap.py."""
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot_bootstrap import discover_adapter_class, wire_adapter, bootstrap, _ADAPTER_MODULES


class TestDiscoverAdapter:
    def test_discovers_monitor_adapter(self):
        adapter = discover_adapter_class(Path("/home/kozuka/Work"))
        assert adapter is not None
        assert adapter.project_name() == "monitor"

    def test_missing_config_returns_none(self, tmp_path):
        adapter = discover_adapter_class(tmp_path)
        assert adapter is None


class TestWireAdapter:
    def test_wiring_is_idempotent(self):
        adapter = discover_adapter_class(Path("/home/kozuka/Work"))
        if adapter is None:
            pytest.skip("no adapter available")
        wire_adapter(adapter)
        wire_adapter(adapter)

    def test_modules_have_setter(self):
        import importlib
        for mod_path in _ADAPTER_MODULES:
            try:
                mod = importlib.import_module(mod_path)
                assert hasattr(mod, "set_project_adapter"), f"{mod_path} missing set_project_adapter"
            except ImportError:
                pass


class TestBootstrap:
    def test_bootstrap_with_project(self):
        adapter = bootstrap(Path("/home/kozuka/Work"))
        assert adapter is not None

    def test_bootstrap_without_config(self, tmp_path):
        adapter = bootstrap(tmp_path)
        assert adapter is None

    def test_fail_open_on_bad_adapter(self, tmp_path):
        codebot_dir = tmp_path / ".codebot"
        codebot_dir.mkdir()
        (codebot_dir / "project.yaml").write_text("name: broken\n")
        adapter = bootstrap(tmp_path)
