"""Service Registry — Canonical access point for optional services.

Purpose
-------
Provides a single, centralized location for importing optional services
with graceful fallbacks. Eliminates duplicate try/except blocks across
the codebase when accessing services that may not be installed.

Invariants
----------
- Each service has exactly one import attempt with fallback
- Services are accessed via functions, not direct module imports
- Fallbacks are no-op functions that never raise exceptions
- New services can be added without modifying existing callers
"""
from __future__ import annotations

import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# Cache for resolved service implementations
_service_cache: dict[str, Any] = {}


def _get_alignment_service_run_pipeline() -> Callable[[], None]:
    """Get run_alignment_pipeline_for_all with graceful fallback."""
    if "run_alignment_pipeline_for_all" in _service_cache:
        return _service_cache["run_alignment_pipeline_for_all"]
    
    try:
        from codebot.alignment_service import run_alignment_pipeline_for_all as real_fn
        _service_cache["run_alignment_pipeline_for_all"] = real_fn
        return real_fn
    except ModuleNotFoundError:
        def _no_op() -> None:
            pass
        _service_cache["run_alignment_pipeline_for_all"] = _no_op
        logger.debug("alignment_service not available; using no-op fallback")
        return _no_op


def _get_alignment_service_run_single_pipeline() -> Callable[[str], bool]:
    """Get run_alignment_pipeline with graceful fallback."""
    if "run_alignment_pipeline" in _service_cache:
        return _service_cache["run_alignment_pipeline"]
    
    try:
        from codebot.alignment_service import run_alignment_pipeline as real_fn
        _service_cache["run_alignment_pipeline"] = real_fn
        return real_fn
    except ModuleNotFoundError:
        def _no_op_bot(bot_name: str) -> bool:
            return False
        _service_cache["run_alignment_pipeline"] = _no_op_bot
        logger.debug("alignment_service not available; using no-op fallback")
        return _no_op_bot


# Public API: service accessors
def get_run_alignment_pipeline_for_all() -> Callable[[], None]:
    """Return the canonical alignment pipeline runner for all bots."""
    return _get_alignment_service_run_pipeline()


def get_run_alignment_pipeline() -> Callable[[str], bool]:
    """Return the canonical alignment pipeline runner for a single bot."""
    return _get_alignment_service_run_single_pipeline()
