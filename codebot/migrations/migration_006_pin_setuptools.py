"""Migration 006: Pin setuptools build dependency.

WHY: ticket CB-9481010-8A0A — setuptools was specified as >=68.0 without an
upper bound in the build-system requirements. This risks breaking changes
or malicious updates affecting build reproducibility.

This migration ensures setuptools is pinned to >=68.0,<70.0.

Reversibility: forward() pins the version; rollback() removes the upper bound
(restoring >=68.0) if needed for specific legacy compatibility scenarios,
though typically the pinned state is desired.
Atomicity: edits are written via tmp-file + os.replace.
Idempotency: forward() no-ops when already pinned; rollback() no-ops when
already unpinned.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

PINNED_SPEC = "setuptools>=68.0,<70.0"
UNBOUNDED_SPEC = "setuptools>=68.0"

# Match setuptools>=68.0 without upper bound in requires list
# Handles both single and double quotes
_UNBOUNDED_RE = re.compile(r"(['\"])setuptools>=68\.0(?!,<)\1")
_PINNED_RE = re.compile(r"(['\"])setuptools>=68\.0,<70\.0\1")


def default_pyproject_path() -> Path:
    """Return the project-root pyproject.toml for this migration."""
    return Path(__file__).resolve().parents[2] / "pyproject.toml"


def _resolve(pyproject: str | Path | None) -> Path:
    return Path(pyproject) if pyproject is not None else default_pyproject_path()


def forward(pyproject: str | Path | None = None) -> dict:
    """Pin setuptools>=68.0 to setuptools>=68.0,<70.0 (idempotent, atomic)."""
    target = _resolve(pyproject)
    text = target.read_text(encoding="utf-8")
    
    # Check if already pinned
    if _PINNED_RE.search(text):
        return {"status": "skipped", "path": str(target), "reason": "already pinned"}
    
    # Check if unbounded spec exists
    if not _UNBOUNDED_RE.search(text):
        return {"status": "skipped", "path": str(target), "reason": "spec not found or already different"}
        
    new_text, count = _UNBOUNDED_RE.subn(lambda m: f"{m.group(1)}{PINNED_SPEC}{m.group(1)}", text)
    if count == 0:
        return {"status": "skipped", "path": str(target)}
        
    # Atomic write
    tmp = target.parent / (target.name + ".migration006.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)
    
    return {"status": "migrated", "path": str(target), "replacements": count}


def rollback(pyproject: str | Path | None = None) -> dict:
    """Revert setuptools pin to >=68.0 (idempotent)."""
    target = _resolve(pyproject)
    text = target.read_text(encoding="utf-8")
    
    # Check if already unbounded
    if _UNBOUNDED_RE.search(text):
        return {"status": "skipped", "path": str(target), "reason": "already unbounded"}
        
    if not _PINNED_RE.search(text):
        return {"status": "skipped", "path": str(target), "reason": "pinned spec not found"}
        
    new_text, count = _PINNED_RE.subn(lambda m: f"{m.group(1)}{UNBOUNDED_SPEC}{m.group(1)}", text)
    if count == 0:
        return {"status": "skipped", "path": str(target)}
        
    # Atomic write
    tmp = target.parent / (target.name + ".migration006.tmp")
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)
    
    return {"status": "restored", "path": str(target), "replacements": count}