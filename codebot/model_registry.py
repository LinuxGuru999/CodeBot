"""Centralized model configuration registry.

Purpose
-------
Single source of truth for all model rotation, fallback, and tier
configuration.  Every other module (ticket_dispatcher, model_manager,
orchestrator_scheduler, etc.) imports from here rather than defining its
own copy.

Why
---
Previously the same model lists were duplicated across at least three
modules with subtle inconsistencies (e.g. ``MODEL_TIER_EXPENSIVE`` was
a populated frozenset in ticket_dispatcher but empty in model_manager).
Shotgun-surgery meant adding a model required touching multiple files.

Invariants
----------
- This module is a leaf node: it MUST NOT import from any other
  ``codebot.*`` module to avoid circular dependencies.
- ``WORKER_MODEL_CYCLE`` and ``WORKER_FALLBACK_CYCLE`` must have the
  same length so index-based pairing works correctly.
- Every model in ``WORKER_MODEL_CYCLE`` should have an entry in
  ``MODEL_FALLBACKS``; if missing, callers fall back to
  ``DEFAULT_FALLBACK_MODEL``.
"""

from __future__ import annotations

# Default fallback when a model has no explicit entry in MODEL_FALLBACKS
DEFAULT_FALLBACK_MODEL: str = "xiaomi-mimo-2.5"

# Primary rotation order for worker model assignment
WORKER_MODEL_CYCLE: tuple[str, ...] = (
    "qwen-3.5-plus",
    "qwen-3.6-plus",
    "qwen-3.7-plus",
    "qwen-3.7-plus-thinking",
    "qwen-3.6-max-preview",
    "qwen-3.6-max-preview-thinking",
    "qwen-3.8-omni-flash",
    "qwen-3.8-omni-flash-thinking",
    "qwen-3.5-omni-plus",
    "qwen-3.8-max",
    "qwen-3.7-max",
    "qwen-3.5-plus-thinking",
    "qwen-3.6-plus-thinking",
    "qwen-3.7-max-thinking",
    "qwen-3.8-max-thinking",
    "xiaomi-mimo-2.5",
    "meta-muse-spark-1.2",
    "meta-muse-spark-1.3",
)

# Fallback model paired by index with WORKER_MODEL_CYCLE
WORKER_FALLBACK_CYCLE: tuple[str, ...] = (
    "qwen-3.7-plus",
    "qwen-3.7-plus",
    "qwen-3.6-plus",
    "qwen-3.6-plus",
    "qwen-3.7-max",
    "qwen-3.7-max",
    "qwen-3.7-plus",
    "qwen-3.7-plus",
    "qwen-3.6-plus",
    "qwen-3.7-plus",
    "qwen-3.6-plus",
    "qwen-3.7-max",
    "qwen-3.7-max",
    "qwen-3.7-plus",
    "qwen-3.7-plus",
    "qwen-3.5-plus",
    "qwen-3.6-plus",
    "qwen-3.5-plus",
)

# Per-model explicit fallback overrides
MODEL_FALLBACKS: dict[str, str] = {
    "qwen-3.8-max": "qwen-3.7-plus",
    "qwen-3.8-max-thinking": "qwen-3.7-max-thinking",
    "qwen-3.7-max": "qwen-3.6-plus",
    "qwen-3.7-max-thinking": "qwen-3.7-plus",
    "qwen-3.7-plus": "xiaomi-mimo-2.5",
    "qwen-3.6-plus": "xiaomi-mimo-2.5",
    "qwen-3.6-plus-thinking": "qwen-3.7-max-thinking",
    "qwen-3.5-plus": "xiaomi-mimo-2.5",
    "qwen-3.5-plus-thinking": "qwen-3.7-max-thinking",
    "meta-muse-spark-1.3": "qwen-3.6-plus",
    "meta-muse-spark-1.2": "qwen-3.5-plus",
    "xiaomi-mimo-2.5": "qwen-3.5-plus",
}

# Models that cost significantly more per token
MODEL_TIER_EXPENSIVE: frozenset[str] = frozenset({
    "qwen-3.8-max",
    "qwen-3.8-max-thinking",
    "qwen-3.7-max",
    "qwen-3.7-max-thinking",
})

# All known models (derived from cycle to keep single source of truth)
ALL_MODELS: tuple[str, ...] = WORKER_MODEL_CYCLE
