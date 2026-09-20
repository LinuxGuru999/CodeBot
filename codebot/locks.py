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

Invariants
----------
- stdlib-only (fcntl on Unix, msvcrt on Windows)
- Fail-open on unsupported platforms: locking becomes a no-op with warning
- Lock semantics match fcntl.flock where possible
- Thread-safe within a single process
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

if _IS_WINDOWS:
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
    elif _MSCRT_AVAILABLE and _IS_WINDOWS:
        # Windows path: use msvcrt.locking
        # Note: msvcrt.locking has different semantics than fcntl.flock:
        # - It locks a byte range, not the whole file
        # - We lock a large region starting at current position
        # - LOCK_SH is not supported; we treat it as LOCK_EX
        non_blocking = bool(operation & LOCK_NB)
        op = operation & ~LOCK_NB

        try:
            if op == LOCK_UN:
                # Unlock: unlock bytes from current position
                # msvcrt.LK_UNLCK unlocks previously locked region
                _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)  # type: ignore[union-attr]
            else:
                # Lock: try to lock bytes
                # Save and restore position since locking affects it
                pos = os.lseek(fd, 0, os.SEEK_CUR)
                os.lseek(fd, 0, os.SEEK_SET)
                try:
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
                    os.lseek(fd, pos, os.SEEK_SET)
        except OSError:
            raise
    else:
        # Unsupported platform: warn once and proceed without locking
        if not hasattr(flock, '_warned'):
            warnings.warn(
                "File locking not available on this platform. "
                "Concurrent access to state files may cause corruption.",
                RuntimeWarning,
                stacklevel=2
            )
            flock._warned = True  # type: ignore[attr-defined]
        # No-op: allow operation to proceed without actual locking
