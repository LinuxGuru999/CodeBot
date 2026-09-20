"""Backward-compatibility shim for file locking.

This module re-exports locking primitives from codebot.locks to maintain
API compatibility with existing callers. All new code should import directly
from codebot.locks.
"""

from __future__ import annotations

from codebot.locks import LOCK_EX, LOCK_NB, LOCK_SH, LOCK_UN, flock

__all__ = ["LOCK_SH", "LOCK_EX", "LOCK_UN", "LOCK_NB", "flock"]
