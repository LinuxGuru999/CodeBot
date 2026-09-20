"""Tests for migration_006_pin_setuptools."""
import os
import tempfile
from pathlib import Path

import pytest

from codebot.migrations.migration_006_pin_setuptools import (
    forward,
    rollback,
    default_pyproject_path,
)

PYPROJECT_TEMPLATE = """[build-system]
requires = ["{spec}"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "test-project"
version = "0.1.0"
"""


def _write_pyproject(tmp_path: Path, spec: str) -> Path:
    """Write a temporary pyproject.toml with the given setuptools spec."""
    content = PYPROJECT_TEMPLATE.format(spec=spec)
    target = tmp_path / "pyproject.toml"
    target.write_text(content, encoding="utf-8")
    return target


class TestMigration006Forward:
    def test_pins_unbounded_spec(self, tmp_path: Path):
        """Forward migration should pin unbounded setuptools>=68.0."""
        target = _write_pyproject(tmp_path, "setuptools>=68.0")
        result = forward(target)
        
        assert result["status"] == "migrated"
        assert result["replacements"] == 1
        
        content = target.read_text(encoding="utf-8")
        assert "setuptools>=68.0,<70.0" in content
        assert "setuptools>=68.0\"" not in content  # Ensure old unbounded is gone

    def test_idempotent_when_already_pinned(self, tmp_path: Path):
        """Forward migration should skip if already pinned."""
        target = _write_pyproject(tmp_path, "setuptools>=68.0,<70.0")
        result = forward(target)
        
        assert result["status"] == "skipped"
        
        content = target.read_text(encoding="utf-8")
        assert "setuptools>=68.0,<70.0" in content

    def test_skips_if_spec_not_found(self, tmp_path: Path):
        """Forward migration should skip if setuptools spec is missing."""
        target = _write_pyproject(tmp_path, "setuptools==69.0")
        result = forward(target)
        
        assert result["status"] == "skipped"


class TestMigration006Rollback:
    def test_reverts_pinned_spec(self, tmp_path: Path):
        """Rollback migration should revert pinned setuptools to unbounded."""
        target = _write_pyproject(tmp_path, "setuptools>=68.0,<70.0")
        result = rollback(target)
        
        assert result["status"] == "restored"
        assert result["replacements"] == 1
        
        content = target.read_text(encoding="utf-8")
        assert "setuptools>=68.0\"" in content
        assert "setuptools>=68.0,<70.0" not in content

    def test_idempotent_when_already_unbounded(self, tmp_path: Path):
        """Rollback migration should skip if already unbounded."""
        target = _write_pyproject(tmp_path, "setuptools>=68.0")
        result = rollback(target)
        
        assert result["status"] == "skipped"

    def test_skips_if_pinned_spec_not_found(self, tmp_path: Path):
        """Rollback migration should skip if pinned spec is missing."""
        target = _write_pyproject(tmp_path, "setuptools==69.0")
        result = rollback(target)
        
        assert result["status"] == "skipped"


class TestMigration006RoundTrip:
    def test_forward_then_rollback(self, tmp_path: Path):
        """Verify forward then rollback restores original state."""
        original_spec = "setuptools>=68.0"
        target = _write_pyproject(tmp_path, original_spec)
        original_content = target.read_text(encoding="utf-8")
        
        # Forward
        fwd_result = forward(target)
        assert fwd_result["status"] == "migrated"
        
        # Rollback
        rbk_result = rollback(target)
        assert rbk_result["status"] == "restored"
        
        # Verify restoration
        final_content = target.read_text(encoding="utf-8")
        assert original_content == final_content