"""TDD RED: migration 005 — pin pytest dev dependency with rollback + integrity.

WHY: ticket CB-8794332-FD88 requires pytest>=7.0 (unbounded) to be pinned
to pytest>=7.0,<8.0. Every migration must prove forward AND reverse paths,
idempotency, atomicity, and backup integrity before implementation exists.
"""
from __future__ import annotations

import hashlib
import re
import tomllib
from pathlib import Path

import pytest

from codebot.migrations import migration_005_pin_pytest as mig


UNPINNED_TOML = """[build-system]
requires = ["setuptools>=68.0"]
build-backend = "setuptools.backends._legacy:_Backend"

[project]
name = "codebot"
version = "0.2.0"
dependencies = []

[project.optional-dependencies]
dev = ["pytest>=7.0"]
"""

PINNED_SPEC = "pytest>=7.0,<8.0"


def _write_tmp_pyproject(tmp_path: Path, content: str) -> Path:
    p = tmp_path / "pyproject.toml"
    p.write_text(content, encoding="utf-8")
    return p


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_migrate_forward_pins_unbounded_spec(tmp_path: Path) -> None:
    """Forward migration pins bare pytest>=7.0 to bounded range."""
    p = _write_tmp_pyproject(tmp_path, UNPINNED_TOML)
    mig.forward(p)
    text = p.read_text(encoding="utf-8")
    assert PINNED_SPEC in text
    assert '"pytest>=7.0"' not in text and "'pytest>=7.0'" not in text.replace(PINNED_SPEC, "")
    # Result must still be valid TOML with pinned dev dep.
    data = tomllib.loads(text)
    assert data["project"]["optional-dependencies"]["dev"] == [PINNED_SPEC]


def test_migrate_rollback_restores_unbounded_spec(tmp_path: Path) -> None:
    """Rollback reverts pinned spec to original unbounded form (reversibility)."""
    p = _write_tmp_pyproject(tmp_path, UNPINNED_TOML)
    before = p.read_text(encoding="utf-8")
    mig.forward(p)
    assert PINNED_SPEC in p.read_text(encoding="utf-8")
    mig.rollback(p)
    after = p.read_text(encoding="utf-8")
    assert PINNED_SPEC not in after
    assert "pytest>=7.0" in after
    # Rollback of a backup-restored file must equal the original bytes.
    assert after == before


def test_migrate_forward_is_idempotent(tmp_path: Path) -> None:
    """Running forward twice equals running once (safe retry)."""
    p = _write_tmp_pyproject(tmp_path, UNPINNED_TOML)
    mig.forward(p)
    first = p.read_text(encoding="utf-8")
    first_sum = _checksum(p)
    mig.forward(p)  # must not raise, must not duplicate pin
    second = p.read_text(encoding="utf-8")
    assert first == second
    assert _checksum(p) == first_sum
    assert second.count(PINNED_SPEC) == 1


def test_migrate_rollback_is_idempotent(tmp_path: Path) -> None:
    """Running rollback twice is safe."""
    p = _write_tmp_pyproject(tmp_path, UNPINNED_TOML)
    mig.forward(p)
    mig.rollback(p)
    first = p.read_text(encoding="utf-8")
    mig.rollback(p)  # must not raise
    assert p.read_text(encoding="utf-8") == first


def test_migrate_preserves_other_content_and_comments(tmp_path: Path) -> None:
    """Migration must not reformat unrelated sections (zero-downtime compat)."""
    content = "# comment: keep me\n" + UNPINNED_TOML + '\n[tool.pytest.ini_options]\ntestpaths = ["tests"]\n'
    p = _write_tmp_pyproject(tmp_path, content)
    mig.forward(p)
    text = p.read_text(encoding="utf-8")
    assert "# comment: keep me" in text
    assert 'testpaths = ["tests"]' in text
    assert 'name = "codebot"' in text


def test_migrate_creates_verified_backup(tmp_path: Path) -> None:
    """Forward must leave a verified backup; checksum proves integrity."""
    p = _write_tmp_pyproject(tmp_path, UNPINNED_TOML)
    before_sum = _checksum(p)
    mig.forward(p)
    backup = mig.backup_path_for(p)
    assert backup.exists(), "backup file must exist after forward"
    assert _checksum(backup) == before_sum
    assert mig.verify_backup(p) is True


def test_migrate_forward_skips_when_already_pinned(tmp_path: Path) -> None:
    """Already-pinned file is a no-op (handles partial/complete migration)."""
    pinned = UNPINNED_TOML.replace('"pytest>=7.0"', f'"{PINNED_SPEC}"')
    p = _write_tmp_pyproject(tmp_path, pinned)
    before = p.read_text(encoding="utf-8")
    result = mig.forward(p)
    assert p.read_text(encoding="utf-8") == before
    assert result.get("status") == "skipped"


def test_current_pyproject_is_pinned() -> None:
    """Acceptance criteria: real pyproject.toml pins pytest with upper bound."""
    root = Path(__file__).resolve().parents[2] / "pyproject.toml"
    text = root.read_text(encoding="utf-8")
    data = tomllib.loads(text)
    dev = data["project"]["optional-dependencies"]["dev"]
    assert any(re.match(r"pytest\s*>=.*<\d+", spec) for spec in dev), f"dev deps lack upper bound: {dev}"
    assert PINNED_SPEC in dev
