"""Migration 005: pin pytest dev dependency to pytest>=7.0,<8.0.

WHY: ticket CB-8794332-FD88 — pyproject.toml declared an unbounded
``pytest>=7.0`` dev dependency, allowing automatic upgrades to future
major versions with breaking changes (supply-chain risk). This migration
bounds the spec to ``>=7.0,<8.0`` so only compatible releases install.

Reversibility: forward() backs up the original bytes before editing;
rollback() restores the exact backup (byte-identical revert).
Atomicity: edits are written via tmp-file + os.replace (all-or-nothing).
Idempotency: forward() no-ops when already pinned; rollback() no-ops
when the file already matches the backup.
"""
from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path

PINNED_SPEC = "pytest>=7.0,<8.0"
BARE_SPEC = "pytest>=7.0"

# WHY: match only the bare quoted spec; the closing-quote anchor ensures an
# already-pinned "pytest>=7.0,<8.0" (followed by ",") never matches.
_BARE_RE = re.compile(r"""(['"])pytest>=7\.0\1""")
_PINNED_RE = re.compile(r"""(['"])pytest>=7\.0,<8\.0\1""")


def default_pyproject_path() -> Path:
    """Return the project-root pyproject.toml for this migration."""
    return Path(__file__).resolve().parents[2] / "pyproject.toml"


def backup_path_for(pyproject: str | Path | None = None) -> Path:
    """Return the deterministic backup path for a pyproject.toml file."""
    target = Path(pyproject) if pyproject is not None else default_pyproject_path()
    return target.parent / (target.name + ".migration005.bak")


def verify_backup(pyproject: str | Path | None = None) -> bool:
    """Return True iff a parseable backup exists for the given file."""
    backup = backup_path_for(pyproject)
    if not backup.is_file():
        return False
    try:
        data = tomllib.loads(backup.read_text(encoding="utf-8"))
        dev = data["project"]["optional-dependencies"]["dev"]
        return any("pytest" in spec for spec in dev)
    except (OSError, ValueError, KeyError):
        return False


def _resolve(pyproject: str | Path | None) -> Path:
    return Path(pyproject) if pyproject is not None else default_pyproject_path()


def forward(pyproject: str | Path | None = None) -> dict:
    """Pin bare pytest>=7.0 to pytest>=7.0,<8.0 (idempotent, atomic)."""
    target = _resolve(pyproject)
    text = target.read_text(encoding="utf-8")
    if PINNED_SPEC in text and not _BARE_RE.search(text):
        return {"status": "skipped", "path": str(target)}
    if not _BARE_RE.search(text):
        return {"status": "skipped", "path": str(target)}
    # WHY: backup before any destructive write so rollback restores bytes.
    backup = backup_path_for(target)
    if not backup.is_file():
        backup.write_text(text, encoding="utf-8")
    new_text, count = _BARE_RE.subn(lambda m: f"{m.group(1)}{PINNED_SPEC}{m.group(1)}", text)
    if count == 0:
        return {"status": "skipped", "path": str(target)}
    # WHY: validate TOML before swapping so a bad edit never lands.
    data = tomllib.loads(new_text)
    assert data["project"]["optional-dependencies"]["dev"] == [PINNED_SPEC]
    tmp = target.parent / (target.name + ".migration005.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)
    if not verify_backup(target):
        raise RuntimeError("Backup verification failed after forward migration.")
    return {"status": "migrated", "path": str(target), "replacements": count}


def rollback(pyproject: str | Path | None = None) -> dict:
    """Restore the pre-migration pyproject.toml bytes (idempotent)."""
    target = _resolve(pyproject)
    backup = backup_path_for(target)
    if backup.is_file():
        original = backup.read_text(encoding="utf-8")
        if target.is_file() and target.read_text(encoding="utf-8") == original:
            return {"status": "skipped", "path": str(target)}
        tmp = target.parent / (target.name + ".migration005.tmp")
        tmp.write_text(original, encoding="utf-8")
        os.replace(tmp, target)
        return {"status": "restored", "path": str(target)}
    # WHY: no backup (e.g. hand-pinned file) — still offer a safe reverse
    # via the inverse regex so rollback never hard-fails on partial state.
    if not target.is_file():
        return {"status": "skipped", "path": str(target)}
    text = target.read_text(encoding="utf-8")
    new_text, count = _PINNED_RE.subn(lambda m: f"{m.group(1)}{BARE_SPEC}{m.group(1)}", text)
    if count == 0:
        return {"status": "skipped", "path": str(target)}
    tomllib.loads(new_text)  # validate before swapping
    tmp = target.parent / (target.name + ".migration005.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)
    return {"status": "restored", "path": str(target), "replacements": count}
