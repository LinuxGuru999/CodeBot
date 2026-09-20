"""Migration 007: unify file locking on the shared codebot.locks primitive.

WHY: ticket CB-5603529-75D5 — checkpoint_manager.py carried a private
fcntl/no-op fallback and process_manager.py carried a private
fcntl/msvcrt/no-op fallback. Both duplicates drifted from the canonical
codebot.locks.flock (byte-range vs whole-file semantics on Windows,
missing LOCK_SH/LOCK_NB handling), risking race conditions on Windows.
This migration routes both modules through codebot.locks and documents
residual platform limits in codebot.locks itself.

Reversibility: forward() backs up original bytes before editing;
rollback() restores the exact backups (byte-identical revert).
Atomicity: edits are written via tmp-file + os.replace (all-or-nothing).
Idempotency: forward() no-ops when already migrated; rollback() no-ops
when backups are absent and files already match pre-migration state.
"""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

_MIGRATIONS_DIR = Path(__file__).resolve().parent
_PROJECT_ROOT = _MIGRATIONS_DIR.parents[1]

# WHY: tests invoke forward()/rollback() against fixture trees, so every
# path is overridable. Production callers pass no args (project files).
def _default_paths() -> tuple[str, str, str]:
    return (str(_PROJECT_ROOT / "codebot" / "checkpoint_manager.py"),
            str(_PROJECT_ROOT / "codebot" / "process_manager.py"),
            str(_PROJECT_ROOT / "codebot" / "locks.py"))

CHECKPOINT_MANAGER, PROCESS_MANAGER, LOCKS_MODULE = _default_paths()

# WHY: deterministic per-file backups so rollback restores exact bytes.
BACKUP_SUFFIX = ".migration007.bak"
TMP_SUFFIX = ".migration007.tmp"

# --- Forward edit payloads -------------------------------------------------

# WHY: checkpoint_manager only needs the shared import; its _state_write_lock
# body already calls fcntl.flock(fd, LOCK_EX/LOCK_UN) which keeps working
# because `fcntl` becomes a shim around the shared flock.
_CM_OLD_IMPORT = """try:
    import fcntl
    LOCK_EX = fcntl.LOCK_EX
    LOCK_UN = fcntl.LOCK_UN
except ImportError:
    LOCK_EX = 2  # fcntl.LOCK_EX value on Linux; used for exclusive state-file locking
    LOCK_UN = 8  # fcntl.LOCK_UN value on Linux; used to release the flock
    def _noop_flock(fd, operation):
        \"\"\"No-op flock on platforms without fcntl (e.g. Windows).\"\"\"
        pass
    fcntl = type('fcntl', (), {'flock': _noop_flock})()"""

_CM_NEW_IMPORT = """try:
    from codebot.locks import LOCK_EX, LOCK_UN, flock as _shared_flock
    fcntl = type("fcntl", (), {"flock": staticmethod(_shared_flock)})()
except ImportError:  # pragma: no cover - locks module must exist
    try:
        import fcntl  # type: ignore[no-redef]
        LOCK_EX = fcntl.LOCK_EX
        LOCK_UN = fcntl.LOCK_UN
    except ImportError:
        LOCK_EX = 2  # fcntl.LOCK_EX value on Linux; used for exclusive state-file locking
        LOCK_UN = 8  # fcntl.LOCK_UN value on Linux; used to release the flock
        def _noop_flock(fd: int, operation: int) -> None:
            \"\"\"No-op flock on platforms without fcntl (e.g. Windows).\"\"\"
            pass
        fcntl = type('fcntl', (), {'flock': _noop_flock})()  # type: ignore[assignment]"""

_CM_OLD_INVARIANT = """    Invariants:
        - Uses ``fcntl.flock`` on Unix; no-ops on unsupported platforms.
        - Lock file is created with ``a+`` so it persists across calls."""

_CM_NEW_INVARIANT = """    Invariants:
        - Routes through ``codebot.locks.flock`` (Unix fcntl / Windows msvcrt /
          documented no-op). See codebot.locks docstring for platform limits:
          byte-range (not whole-file) locks on Windows, LOCK_SH treated as
          LOCK_EX there, fail-open no-op with RuntimeWarning elsewhere.
        - Lock file is created with ``a+`` so it persists across calls."""

# WHY: process_manager's private _get_flock_function/_flock/LOCK block is the
# divergent duplicate; replace the header with a delegating import, keeping
# module-level LOCK_EX/LOCK_UN and _flock names for compatibility.
_PM_OLD_BLOCK = """# ---------------------------------------------------------------------------
# Cross-platform file locking
# ---------------------------------------------------------------------------

def _get_flock_function():
    \"\"\"Return a flock-like function compatible with the current platform.\"\"\"
    try:
        import fcntl
        def _unix_flock(fd, operation):
            fcntl.flock(fd, operation)
        return _unix_flock
    except ImportError:
        pass

    try:
        import msvcrt
        def _windows_flock(fd, operation):
            if operation == 2:  # LOCK_EX
                msvcrt.locking(fd, 1, 1)  # LK_LOCK = 1
            elif operation == 8:  # LOCK_UN
                msvcrt.locking(fd, 2, 1)  # LK_UNLCK = 2
        return _windows_flock
    except ImportError:
        pass

    def _noop_flock(fd, operation):
        pass
    return _noop_flock

_flock = _get_flock_function()

try:
    import fcntl
    LOCK_EX = fcntl.LOCK_EX
    LOCK_UN = fcntl.LOCK_UN
except ImportError:
    LOCK_EX = 2
    LOCK_UN = 8"""

_PM_NEW_BLOCK = """# ---------------------------------------------------------------------------
# Cross-platform file locking — single source of truth is codebot.locks.
# ---------------------------------------------------------------------------
# WHY (CB-5603529-75D5): private fcntl/msvcrt/no-op duplicates drifted from
# the canonical primitive (byte-range vs whole-file on Windows, missing
# LOCK_SH/LOCK_NB handling). Import the shared flock so every caller gets
# identical semantics; keep module-level LOCK_EX/LOCK_UN aliases plus a
# private _flock alias so existing tests/patches keep working.

try:
    from codebot.locks import LOCK_EX as _LOCK_EX_SHARED
    from codebot.locks import LOCK_UN as _LOCK_UN_SHARED
    from codebot.locks import flock as _shared_flock
    LOCK_EX = _LOCK_EX_SHARED
    LOCK_UN = _LOCK_UN_SHARED

    def _flock(fd: int, operation: int) -> None:
        \"\"\"Delegate to the shared cross-platform flock (see codebot.locks).\"\"\"
        _shared_flock(fd, operation)
except ImportError:  # pragma: no cover - locks module must exist
    def _get_flock_function():  # type: ignore[no-redef]
        \"\"\"Return a flock-like function compatible with the current platform.\"\"\"
        try:
            import fcntl
            def _unix_flock(fd, operation):
                fcntl.flock(fd, operation)
            return _unix_flock
        except ImportError:
            pass

        try:
            import msvcrt
            def _windows_flock(fd, operation):
                if operation == 2:  # LOCK_EX
                    msvcrt.locking(fd, 1, 1)  # LK_LOCK = 1
                elif operation == 8:  # LOCK_UN
                    msvcrt.locking(fd, 2, 1)  # LK_UNLCK = 2
            return _windows_flock
        except ImportError:
            pass

        def _noop_flock(fd, operation):
            pass
        return _noop_flock

    _flock = _get_flock_function()

    try:
        import fcntl
        LOCK_EX = fcntl.LOCK_EX
        LOCK_UN = fcntl.LOCK_UN
    except ImportError:
        LOCK_EX = 2
        LOCK_UN = 8"""

# WHY: explicit platform-limitation docs satisfy the ticket's second
# acceptance criterion (identical behavior OR documented limits).
_LOCKS_OLD_INVARIANTS = """Invariants
----------
- stdlib-only (fcntl on Unix, msvcrt on Windows)
- Fail-open on unsupported platforms: locking becomes a no-op with warning
- Lock semantics match fcntl.flock where possible
- Thread-safe within a single process
\"\"\""""

_LOCKS_NEW_INVARIANTS = """Platform Limitations (CB-5603529-75D5)
-----------------------------------
File locking is advisory and its semantics differ by platform. Callers must
not assume identical behaviour across operating systems:

- Linux/Unix (fcntl.flock): whole-file advisory locks. LOCK_SH allows
  multiple concurrent readers; LOCK_EX is exclusive; LOCK_NB makes either
  non-blocking (raises BlockingIOError/OSError on contention). Locks are
  associated with the open file description and released on close.
- Windows (msvcrt.locking): mandatory byte-range locks. Only the first byte
  (``LK_LOCK``/``LK_UNLCK`` over 1 byte) is locked as a proxy for the whole
  file, so a non-cooperating reader that ignores locking can still read.
  LOCK_SH is NOT natively supported and is treated as LOCK_EX (exclusive).
  LOCK_NB maps to a non-blocking attempt that raises OSError on contention.
  Lock/unlock regions must match exactly or unlock silently fails.
- Other platforms (no fcntl, no msvcrt): fail-open no-op with a one-time
  RuntimeWarning. Concurrent read-modify-write cycles are NOT serialized;
  callers relying on mutual exclusion must use the atomic tmp+replace write
  path (os.replace) which remains safe without locks.

Zero-downtime note: old code holding a private fcntl/msvcrt/no-op fallback
coexists with this module because lock constants keep their standard values
(LOCK_SH=1, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8) and every consumer now routes
through this single primitive.
\"\"\""""


def _backup_path(target: str | Path) -> Path:
    return Path(str(target) + BACKUP_SUFFIX)


def _checksum(path: str | Path) -> str:
    import hashlib as _hl
    return _hl.sha256(Path(path).read_bytes()).hexdigest()


def _verify_backup(target: str | Path) -> bool:
    """Return True iff a non-empty backup exists for target. WHY: integrity gate."""
    bak = _backup_path(target)
    try:
        return bak.is_file() and bak.stat().st_size > 0
    except OSError:
        return False


def _atomic_replace(target: Path, new_text: str) -> None:
    """Write new_text via tmp-file + os.replace. WHY: all-or-nothing swap."""
    tmp = Path(str(target) + TMP_SUFFIX)
    tmp.write_text(new_text, encoding="utf-8")
    os.replace(tmp, target)


def _apply_edits(text: str, edits: list[tuple[str, str]]) -> tuple[str, int]:
    """Apply (old, new) replacements; return (result, applied_count)."""
    applied = 0
    for old, new in edits:
        if old in text:
            text = text.replace(old, new, 1)
            applied += 1
    return text, applied


def _already_migrated_cm(text: str) -> bool:
    return "from codebot.locks import" in text and "codebot.locks.flock" in text


def _already_migrated_pm(text: str) -> bool:
    return "single source of truth is codebot.locks" in text


def _already_documented_locks(text: str) -> bool:
    return "Platform Limitations (CB-5603529-75D5)" in text


def forward(checkpoint_manager: str | Path | None = None, process_manager: str | Path | None = None, locks_module: str | Path | None = None) -> dict:
    """Route checkpoint/process locking via codebot.locks (idempotent, atomic)."""
    defaults = _default_paths()
    cm_p = str(checkpoint_manager) if checkpoint_manager is not None else defaults[0]
    pm_p = str(process_manager) if process_manager is not None else defaults[1]
    lk_p = str(locks_module) if locks_module is not None else defaults[2]
    paths = [cm_p, pm_p, lk_p]
    for p in paths:
        if not Path(p).is_file():
            return {"status": "skipped", "reason": f"missing file: {p}"}
    cm_text = Path(cm_p).read_text(encoding="utf-8")
    pm_text = Path(pm_p).read_text(encoding="utf-8")
    lk_text = Path(lk_p).read_text(encoding="utf-8")
    if _already_migrated_cm(cm_text) and _already_migrated_pm(pm_text) and _already_documented_locks(lk_text):
        return {"status": "skipped", "reason": "already migrated"}
    # WHY: backup before any destructive write so rollback restores bytes.
    for p in paths:
        bak = _backup_path(p)
        if not bak.is_file():
            bak.write_text(Path(p).read_text(encoding="utf-8"), encoding="utf-8")
    # Re-read after backup (no-op) then compute new contents.
    cm_text = Path(cm_p).read_text(encoding="utf-8")
    pm_text = Path(pm_p).read_text(encoding="utf-8")
    lk_text = Path(lk_p).read_text(encoding="utf-8")
    new_cm, cm_n = _apply_edits(cm_text, [(_CM_OLD_IMPORT, _CM_NEW_IMPORT), (_CM_OLD_INVARIANT, _CM_NEW_INVARIANT)])
    new_pm, pm_n = _apply_edits(pm_text, [(_PM_OLD_BLOCK, _PM_NEW_BLOCK)])
    new_lk, lk_n = _apply_edits(lk_text, [(_LOCKS_OLD_INVARIANTS, _LOCKS_NEW_INVARIANTS)])
    # WHY: compile-check before swapping so a bad edit never lands.
    for label, content in (("checkpoint_manager", new_cm), ("process_manager", new_pm), ("locks", new_lk)):
        try:
            compile(content, f"<migration007:{label}>", "exec")
        except SyntaxError as exc:
            raise RuntimeError(f"Migration 007 generated invalid {label}.py: {exc}") from exc
    if cm_n:
        _atomic_replace(Path(cm_p), new_cm)
    if pm_n:
        _atomic_replace(Path(pm_p), new_pm)
    if lk_n:
        _atomic_replace(Path(lk_p), new_lk)
    for p in paths:
        if not _verify_backup(p):
            raise RuntimeError(f"Backup verification failed for {p} after forward migration.")
    total = cm_n + pm_n + lk_n
    if total == 0:
        return {"status": "skipped", "reason": "no matching blocks; already migrated or diverged"}
    return {"status": "migrated", "edits": {"checkpoint_manager": cm_n, "process_manager": pm_n, "locks": lk_n}}


def rollback(checkpoint_manager: str | Path | None = None, process_manager: str | Path | None = None, locks_module: str | Path | None = None) -> dict:
    """Restore pre-migration bytes from backups (idempotent)."""
    defaults = _default_paths()
    cm_p = str(checkpoint_manager) if checkpoint_manager is not None else defaults[0]
    pm_p = str(process_manager) if process_manager is not None else defaults[1]
    lk_p = str(locks_module) if locks_module is not None else defaults[2]
    paths = [cm_p, pm_p, lk_p]
    restored: dict[str, str] = {}
    for p in paths:
        bak = _backup_path(p)
        target = Path(p)
        if not bak.is_file():
            continue  # WHY: no backup (e.g. partial run) — safe no-op, never hard-fail.
        original = bak.read_text(encoding="utf-8")
        if target.is_file() and target.read_text(encoding="utf-8") == original:
            continue
        try:
            compile(original, f"<migration007-rollback:{Path(p).name}>", "exec")
        except SyntaxError as exc:
            raise RuntimeError(f"Backup for {p} is not valid Python: {exc}") from exc
        _atomic_replace(target, original)
        restored[Path(p).name] = _checksum(target)
    if not restored:
        return {"status": "skipped", "reason": "already at pre-migration state or no backups"}
    return {"status": "restored", "checksums": restored}
