"""ProcessSupervisor — abstraction for process replacement (self-restart).

Purpose
-------
Define an abstract base class for process self-restart so that the direct
os.execv() call can be isolated from business logic. This enables:
- Unit testing without side effects (mock the interface)
- Portability to environments where execv behaves differently or is unavailable

Architecture
------------
``ProcessSupervisor`` is an ABC with a single ``restart()`` abstract method.
``UnixProcessSupervisor`` is the concrete implementation using ``os.execv``.
"""
from __future__ import annotations

import logging
import os
import sys
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)


class ProcessSupervisor(ABC):
    """Abstract base class defining the interface for process self-restart."""

    @abstractmethod
    def restart(self) -> None:
        """Replace the current process with a new instance of itself.

        This method should not return on success (os.execv replaces the
        process). On failure, it may raise an exception.
        """
        ...


class UnixProcessSupervisor(ProcessSupervisor):
    """Unix implementation using os.execv for process replacement."""

    def restart(self) -> None:
        """Restart the current process by replacing it via os.execv.

        This method does not return on success. On failure, raises OSError.
        """
        python = sys.executable
        args = [python] + sys.argv
        logger.info(f"Executing self-restart: {' '.join(args)}")
        os.execv(python, args)

    # Backward-compatible alias for existing callers
    restart_self = restart


# ---------------------------------------------------------------------------
# Backward-compatible aliases
# ---------------------------------------------------------------------------
DefaultProcessSupervisor = UnixProcessSupervisor  # deprecated: use UnixProcessSupervisor
