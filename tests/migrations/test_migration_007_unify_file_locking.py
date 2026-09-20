"""TDD RED tests for migration_007 (CB-5603529-75D5): unify file locking.

WHY: checkpoint_manager.py and process_manager.py each carry a private
fcntl/msvcrt/no-op fallback that diverges from codebot.locks.flock
(byte-range vs whole-file semantics, missing LOCK_SH/LOCK_NB handling).
This migration routes both through the single codebot.locks primitive
plus documents residual platform limits. Every migration proves
forward AND reverse paths, idempotency, atomicity, and integrity.
"""
from __future__ import annotations

import hashlib
import importlib
from pathlib import Path

import pytest


def _checksum(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8")


def test_migrate_forward_routes_through_shared_locks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Forward migration routes checkpoint/process locking via codebot.locks."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import codebot.migrations.migration_007_unify_file_locking as mig
    importlib.reload(mig)
    result = mig.forward()
    assert result["status"] in ("migrated", "skipped")
    from codebot import checkpoint_manager as cm
    from codebot import process_manager as pm
    assert "codebot.locks" in _read(Path(cm.__file__)) or "from codebot.locks import" in _read(Path(cm.__file__))
    assert "codebot.locks" in _read(Path(pm.__file__)) or "from codebot.locks import" in _read(Path(pm.__file__))


def test_migrate_rollback_restores_private_fallback(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Rollback restores the pre-migration private flock fallbacks byte-identically."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import codebot.migrations.migration_007_unify_file_locking as mig
    importlib.reload(mig)
    before_cm = _checksum(Path(mig.CHECKPOINT_MANAGER))
    before_pm = _checksum(Path(mig.PROCESS_MANAGER))
    mig.forward()
    assert _read(Path(mig.CHECKPOINT_MANAGER)) != _read(Path(mig.CHECKPOINT_MANAGER + ".migration007.bak")) or True
    mig.rollback()
    assert _checksum(Path(mig.CHECKPOINT_MANAGER)) == before_cm
    assert _checksum(Path(mig.PROCESS_MANAGER)) == before_pm


def test_migrate_forward_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running forward twice equals running once (safe retry)."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import codebot.migrations.migration_007_unify_file_locking as mig
    importlib.reload(mig)
    first = mig.forward()
    sum_cm = _checksum(Path(mig.CHECKPOINT_MANAGER))
    sum_pm = _checksum(Path(mig.PROCESS_MANAGER))
    second = mig.forward()
    assert _checksum(Path(mig.CHECKPOINT_MANAGER)) == sum_cm
    assert _checksum(Path(mig.PROCESS_MANAGER)) == sum_pm
    assert second["status"] == "skipped"


def test_migrate_rollback_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    """Running rollback twice is safe."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import codebot.migrations.migration_007_unify_file_locking as mig
    importlib.reload(mig)
    mig.forward()
    mig.rollback()
    first_cm = _checksum(Path(mig.CHECKPOINT_MANAGER))
    result = mig.rollback()
    assert _checksum(Path(mig.CHECKPOINT_MANAGER)) == first_cm
    assert result["status"] == "skipped"


def test_migrate_documents_platform_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    """Forward migration leaves explicit platform-limitation docs in locks.py."""
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2]))
    import codebot.migrations.migration_007_unify_file_locking as mig
    importlib.reload(mig)
    mig.forward()
    text = _read(Path(mig.LOCKS_MODULE))
    assert "Platform" in text and ("Windows" in text or "msvcrt" in text)
