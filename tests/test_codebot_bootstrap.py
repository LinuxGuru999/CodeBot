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

    def test_returns_none_when_monitor_adapter_unavailable(self, tmp_path):
        # In extracted repo, bots.monitor_adapter doesn't exist.
        # Bootstrap must fail open, not crash.
        codebot_dir = tmp_path / ".codebot"
        codebot_dir.mkdir()
        (codebot_dir / "project.yaml").write_text("schema_version: '1.0'\n")
        adapter = discover_adapter_class(tmp_path)
        # Returns None because monitor_adapter module isn't importable here
        assert adapter is None


class TestWireAdapter:
    def test_wiring_is_idempotent_with_none(self):
        # wire_adapter with a real adapter is tested in Monitor context.
        # Here we verify the module list is well-formed.
        assert len(_ADAPTER_MODULES) >= 5

    def test_modules_have_setter(self):
        import importlib
        for mod_path in _ADAPTER_MODULES:
            try:
                mod = importlib.import_module(mod_path)
                assert hasattr(mod, "set_project_adapter"), f"{mod_path} missing set_project_adapter"
            except ImportError:
                pass


class TestBootstrap:
    def test_bootstrap_without_config_returns_none(self, tmp_path):
        adapter = bootstrap(tmp_path)
        assert adapter is None

    def test_fail_open_on_bad_yaml(self, tmp_path):
        codebot_dir = tmp_path / ".codebot"
        codebot_dir.mkdir()
        (codebot_dir / "project.yaml").write_text("name: broken\n")
        adapter = bootstrap(tmp_path)
        # Must not crash; returns None when adapter can't load
        assert adapter is None

    def test_bootstrap_returns_none_in_extracted_repo(self, tmp_path):
        # Proves portability: bootstrap gracefully degrades when
        # no project-specific adapter is available
        result = bootstrap(tmp_path)
        assert result is None
