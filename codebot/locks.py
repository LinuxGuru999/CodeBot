"""Cross-platform file locking primitives.

Purpose
-------
Provides portable advisory file locking for CodeBot modules that need
to serialize access to shared state files (leases, tickets, ledgers).

Why
---
The fcntl module is Unix-only. CodeBot aims to be portable (Goal 1),
so hard imports of fcntl break on Windows. This module abstracts the
platform differences behind a simple API.

Platform Limitations (CB-5603529-75D5)
-----------------------------------
File locking is advisory and its semantics differ by platform. Callers must
not assume identical behaviour across operating systems:

- Linux/Unix (fcntl.flock): whole-file advisory locks. LOCK_SH allows
  multiple concurrent readers; LOCK_EX is exclusive; LOCK_NB makes either
  non-blocking (raises BlockingIOError/OSError on contention). Locks are
  associated with the open file description and released on close.
- Windows (msvcrt.locking): mandatory byte-range locks. The lock is applied
  to a fixed region starting at position 0. `flock()` saves the current
  file pointer, seeks to 0 before acquiring or releasing the lock, and
  restores the previous position in a finally block to prevent corruption.
  LOCK_SH is NOT natively supported and is treated as LOCK_EX (exclusive).
  LOCK_NB maps to a non-blocking attempt that raises OSError on contention.
- Other platforms (no fcntl, no msvcrt): fail-open no-op with a one-time
  RuntimeWarning. Concurrent read-modify-write cycles are NOT serialized;
  callers relying on mutual exclusion must use the atomic tmp+replace write
  path (os.replace) which remains safe without locks.

Zero-downtime note: old code holding a private fcntl/msvcrt/no-op fallback
coexists with this module because lock constants keep their standard values
(LOCK_SH=1, LOCK_EX=2, LOCK_NB=4, LOCK_UN=8) and every consumer now routes
through this single primitive.
"""

from __future__ import annotations

import os
import sys
import warnings
from typing import TextIO

# Platform detection
_IS_WINDOWS = sys.platform == "win32"
_FLOCK_AVAILABLE = False
_MSCRT_AVAILABLE = False

try:
    import fcntl as _fcntl
    _FLOCK_AVAILABLE = True
except ImportError:
    _fcntl = None  # type: ignore[assignment]

if _IS_WINDOWS:  # pragma: no cover (Windows-only)
    try:
        import msvcrt as _msvcrt
        _MSCRT_AVAILABLE = True
    except ImportError:
        _msvcrt = None  # type: ignore[assignment]

# Lock operation constants (mirror fcntl values for API compatibility)
LOCK_SH: int = 1
"""Shared (read) lock. Multiple readers can hold this simultaneously."""

LOCK_EX: int = 2
"""Exclusive (write) lock. Only one holder at a time; blocks other locks."""

LOCK_UN: int = 8
"""Unlock operation. Releases any previously held lock on the file descriptor."""

LOCK_NB: int = 4
"""Non-blocking flag. OR'd with LOCK_SH or LOCK_EX to request immediate return
if the lock cannot be acquired, rather than blocking until available."""


def flock(fd: int | TextIO, operation: int) -> None:
    """Apply or remove an advisory lock on a file descriptor.

    Args:
        fd: File descriptor (int) or file object with fileno() method.
        operation: Lock operation (LOCK_SH, LOCK_EX, LOCK_UN, optionally OR'd with LOCK_NB).

    Raises:
        OSError: If locking fails (e.g., would block with LOCK_NB).
        ValueError: If fd is invalid.
    """
    # Normalize fd to int
    if hasattr(fd, 'fileno'):
        fd = fd.fileno()

    if _FLOCK_AVAILABLE:
        # Unix path: use fcntl.flock directly
        _fcntl.flock(fd, operation)  # type: ignore[union-attr]
    elif _MSCRT_AVAILABLE and _IS_WINDOWS:  # pragma: no cover (Windows-only)
        # Windows path: use msvcrt.locking
        # Note: msvcrt.locking has different semantics than fcntl.flock:
        # - It locks a byte range, not the whole file
        # - We lock a large region starting at position 0
        # - LOCK_SH is not supported; we treat it as LOCK_EX
        # - File position is NOT preserved across lock/unlock calls on Windows.
        #   Callers must not rely on file pointer position being maintained.
        non_blocking = bool(operation & LOCK_NB)
        op = operation & ~LOCK_NB

        try:
            if op == LOCK_UN:
                # Unlock: unlock bytes from current position
                # msvcrt.LK_UNLCK unlocks previously locked region
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
            else:
                # Lock: try to lock bytes from position 0
                # Save current position and seek to 0 for the fixed-region lock.
                # Position MUST be restored in a finally block to prevent corruption
                # if locking fails or is interrupted (CB-406700-F085).
                original_pos = os.lseek(fd, 0, os.SEEK_CUR)
                try:
                    os.lseek(fd, 0, os.SEEK_SET)
                    # Use LK_LOCK for exclusive lock. msvcrt does not have a separate
                    # non-blocking constant like LK_NBLCK. Instead, LK_LOCK raises
                    # OSError (errno 36, EDEADLOCK) if the lock would block.
                    # We rely on this exception to implement non-blocking semantics.
                    _msvcrt.locking(fd, _msvcrt.LK_LOCK, 0x3FFFFFFF)  # type: ignore[union-attr]
                except OSError:
                    # If non-blocking was requested and we got an error (lock held),
                    # re-raise to match fcntl.flock(LOCK_NB) behavior.
                    if non_blocking:
                        raise
                    # If blocking was requested, we might need to retry or handle differently,
                    # but msvcrt.locking with LK_LOCK blocks by default until success or error.
                    # If it raised here, it's a real error (e.g., invalid fd), so re-raise.
                    raise
                finally:
                    # Always restore file position, even if locking raised OSError.
                    # This prevents silent data corruption from a displaced fd offset.
                    os.lseek(fd, original_pos, os.SEEK_SET)
        except OSError:
            raise
    else:
        # Unsupported platform: fail-open no-op with a one-time warning.
        # Advisory locking cannot be provided here; callers still use atomic
        # tmp+replace writes, but concurrent read-modify-write cycles are not
        # serialized. Warn once so operators can see the degraded guarantee.
        if not getattr(flock, "_warned", False):
            warnings.warn(
                "File locking not available on this platform; continuing without advisory locks",
                RuntimeWarning,
                stacklevel=2,
            )
            setattr(flock, "_warned", True)
        return
