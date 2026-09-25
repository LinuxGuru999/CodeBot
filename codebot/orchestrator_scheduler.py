"""Scheduler V2 coordinator for orchestrator.

Manages the V2 scheduler instance and provides integration hooks
for the main orchestrator loop.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from codebot.process_manager import BotState
from codebot.scheduler_config import MAX_CONCURRENT_AGENTS, ROLE_CAPS
from codebot.state_manager import get_paths

logger = logging.getLogger("orchestrator.scheduler")

_v2_scheduler: Any = None


def get_scheduler(state_dir: Path | None = None) -> Any | None:
    """Get or create the V2 scheduler instance (singleton)."""
    global _v2_scheduler
    if _v2_scheduler is not None:
        return _v2_scheduler
    from codebot.scheduler_v2.dispatcher import Scheduler as V2Scheduler
    try:
        from codebot.model_registry import WORKER_MODEL_CYCLE
        model_pool = list(dict.fromkeys(WORKER_MODEL_CYCLE))
    except ImportError:
        model_pool = ["qwen-3.5-plus"]

    _v2_scheduler = V2Scheduler(
        max_slots=MAX_CONCURRENT_AGENTS,
        state_dir=state_dir or get_paths().state_dir,
        model_pool=model_pool,
        stagger_seconds=5.0,
        role_caps=ROLE_CAPS,
    )
    return _v2_scheduler


def count_active() -> int:
    """Return count of active tasks in V2 scheduler gate."""
    if _v2_scheduler is not None:
        try:
            return _v2_scheduler.gate.count_active()
        except Exception:
            pass
    return 0


def check_all_bots_with_scheduler(bots: dict[str, BotState], check_all_bots_fn: Any) -> None:
    """Ensure scheduler is initialized, then run check_all_bots."""
    get_scheduler()
    check_all_bots_fn(bots)
