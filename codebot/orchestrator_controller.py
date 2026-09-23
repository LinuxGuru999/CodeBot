"""OrchestratorController & ModelRouter — Encapsulated state for orchestration.

This module extracts global mutable state from orchestrator.py into explicit
classes to enable safe concurrent execution, testability, and clear ownership.

Classes:
    ModelRouter: Encapsulates model rotation state and logic.
    OrchestratorController: Holds registry, worker_pool, adapter, and router.
"""
from __future__ import annotations

import logging
from typing import Any

from codebot.dispatch_service import rotate_model_on_error
from codebot.role_registry import RoleCategory, roles_by_category
from codebot.worker_scaler import load_bot_registry
from codebot.ticket_engine import QueueManager

logger = logging.getLogger(__name__)


class ModelRouter:
    """Encapsulates model rotation state and selection logic.

    Wraps dispatch_service.rotate_model_on_error but maintains independent
    rotation state per instance, enabling isolated testing and concurrent use.
    """

    def __init__(self) -> None:
        self._rotation_count: int = 0

    @property
    def rotation_count(self) -> int:
        """Number of rotations performed by this router instance."""
        return self._rotation_count

    def rotate_model(self, bot: Any, bots: dict[str, Any] | None = None) -> str:
        """Rotate model for a bot on error. Delegates to dispatch_service.

        Args:
            bot: BotState instance whose model should be rotated.
            bots: Optional dict of all bots (for context-aware rotation).

        Returns:
            The new model name assigned to the bot.
        """
        new_model = rotate_model_on_error(bot, bots)
        self._rotation_count += 1
        logger.debug("ModelRouter rotation #%d for %s -> %s",
                     self._rotation_count, getattr(bot.config, 'name', '?'), new_model)
        return new_model


def _build_worker_pool(registry: list | None = None) -> frozenset:
    """Build worker pool from bot registry using role_registry.

    Args:
        registry: Pre-loaded bot registry list. If None, loads fresh.

    Returns:
        Frozenset of bot names belonging to implementation roles.
    """
    try:
        if registry is None:
            registry = load_bot_registry()
        impl_names = frozenset(r.name for r in roles_by_category(RoleCategory.IMPLEMENTATION))
        return frozenset(
            c.name for c in registry
            if c.name in impl_names or any(c.name.startswith(f"{r}-") for r in impl_names)
        )
    except Exception:
        return frozenset({"worker-1", "worker-2"})


class OrchestratorController:
    """Central controller encapsulating orchestrator runtime state.

    Replaces module-level WORKER_POOL, BOT_REGISTRY, and _adapter globals.
    Each instance owns its own registry, worker pool, adapter, and model router,
    enabling isolated testing and safe concurrent orchestration.

    Attributes:
        registry: List of bot configurations from the registry.
        worker_pool: Frozenset of implementation-role bot names.
        router: ModelRouter instance for model rotation.
        adapter: Project adapter instance (may be None until bootstrap).
        queue_manager: QueueManager instance for queue depth queries.
    """

    def __init__(self, adapter: Any = None, registry: list | None = None) -> None:
        self._registry = registry if registry is not None else load_bot_registry(adapter)
        self._worker_pool = _build_worker_pool(self._registry)
        self._router = ModelRouter()
        self._adapter = adapter
        self._queue_manager: QueueManager | None = None
        logger.debug("OrchestratorController created: %d bots, %d workers",
                     len(self._registry), len(self._worker_pool))

    @property
    def registry(self) -> list:
        """Bot registry list (read-only)."""
        return self._registry

    @property
    def worker_pool(self) -> frozenset:
        """Frozenset of implementation-role bot names (read-only)."""
        return self._worker_pool

    @property
    def router(self) -> ModelRouter:
        """ModelRouter instance for model rotation (read-only)."""
        return self._router

    @property
    def adapter(self) -> Any:
        """Project adapter instance."""
        return self._adapter

    @adapter.setter
    def adapter(self, value: Any) -> None:
        self._adapter = value

    @property
    def queue_manager(self) -> QueueManager | None:
        """QueueManager instance for queue depth and ticket class queries.

        Lazily initialized from the adapter's state directory on first access.
        Returns None if adapter is not set or state directory is unavailable.
        """
        if self._queue_manager is None and self._adapter is not None:
            try:
                from codebot.state_manager import get_paths
                paths = get_paths()
                self._queue_manager = QueueManager.from_state_dir(paths.state_dir)
            except Exception:
                logger.debug("Failed to initialize QueueManager", exc_info=True)
                return None
        return self._queue_manager

    def get_queue_depth(self) -> int:
        """Get actionable queue depth via QueueManager adapter.

        Returns 0 if QueueManager is unavailable.
        """
        qm = self.queue_manager
        if qm is not None:
            return qm.actionable_queue_depth()
        return 0

    def get_ticket_classes(self) -> list[str]:
        """Get ticket classes for IMPLEMENT state tickets via QueueManager.

        Returns empty list if QueueManager is unavailable.
        """
        qm = self.queue_manager
        if qm is not None:
            return qm.ticket_classes()
        return []
