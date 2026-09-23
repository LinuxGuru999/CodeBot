#!/usr/bin/env python3
"""Model Manager — Model selection, rotation, and lockup profiles.

Purpose
-------
Manages model profiles for lockup detection and handles model rotation
on bot errors. Extracted from process_manager.py and dispatch_service.py
to maintain clear architectural boundaries.

Why
---
Model selection and rotation are cross-cutting concerns that affect both
process management (heartbeat timeouts based on model risk) and dispatch
(error recovery via model rotation). Centralizing this logic prevents
duplication and ensures consistent behavior.

Invariants
----------
- Model profiles are immutable once defined
- Rotation is deterministic: always pick first available healthy model
- Fallback model is always xiaomi-mimo-2.5 (fast, low-risk)
- No subprocess management here — only model configuration
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Model Profiles for Lockup Detection
# ---------------------------------------------------------------------------

from codebot.model_registry import (
    ALL_MODELS as ALL_MODELS_UNIFIED,
    MODEL_TIER_EXPENSIVE,
)

MODEL_TIER_CHEAP = frozenset(ALL_MODELS_UNIFIED)


@dataclass
class ModelProfile:
    """Lockup behaviour profile for a model provider."""
    lockup_risk: str          # high | medium | low
    heartbeat_multiplier: float  # effective_timeout = interval * multiplier
    log_stall_seconds: int    # seconds log must be silent before suspicious
    restart_cooldown: int     # seconds to wait before respawn after stuck
    description: str


MODEL_PROFILES: dict[str, ModelProfile] = {
    "qwen-3.8-max-thinking": ModelProfile(
        lockup_risk="high", heartbeat_multiplier=2.8, log_stall_seconds=280,
        restart_cooldown=12, description="Newest deep reasoning; longest silent thinking",
    ),
    "qwen-3.7-max-thinking": ModelProfile(
        lockup_risk="high", heartbeat_multiplier=2.5, log_stall_seconds=240,
        restart_cooldown=10, description="Deep reasoning; silent thinking ~90s normal",
    ),
    "qwen-3.6-plus-thinking": ModelProfile(
        lockup_risk="medium-high", heartbeat_multiplier=2.3, log_stall_seconds=210,
        restart_cooldown=9, description="Plus thinking; moderate-deep reasoning",
    ),
    "qwen-3.5-plus-thinking": ModelProfile(
        lockup_risk="medium-high", heartbeat_multiplier=2.2, log_stall_seconds=200,
        restart_cooldown=8, description="Older thinking; slower, can stall",
    ),
    "qwen-3.8-max": ModelProfile(
        lockup_risk="medium-high", heartbeat_multiplier=2.0, log_stall_seconds=180,
        restart_cooldown=7, description="Strong code gen; can hang on large edits",
    ),
    "qwen-3.7-max": ModelProfile(
        lockup_risk="medium", heartbeat_multiplier=1.9, log_stall_seconds=175,
        restart_cooldown=30, description="Strong code gen; balanced performance",
    ),
    "qwen-3.7-plus": ModelProfile(
        lockup_risk="medium", heartbeat_multiplier=1.9, log_stall_seconds=170,
        restart_cooldown=6, description="Balanced plus; steady",
    ),
    "qwen-3.6-plus": ModelProfile(
        lockup_risk="medium", heartbeat_multiplier=1.9, log_stall_seconds=165,
        restart_cooldown=6, description="Balanced plus; steady on integration checks",
    ),
    "qwen-3.5-plus": ModelProfile(
        lockup_risk="low-medium", heartbeat_multiplier=1.7, log_stall_seconds=140,
        restart_cooldown=4, description="Older plus; fast, lightweight",
    ),
    "qwen-3.5-omni-plus": ModelProfile(
        lockup_risk="low-medium", heartbeat_multiplier=1.7, log_stall_seconds=145,
        restart_cooldown=4, description="Omni; holistic cross-check",
    ),
    "meta-muse-spark-1.3": ModelProfile(
        lockup_risk="medium", heartbeat_multiplier=1.8, log_stall_seconds=150,
        restart_cooldown=5, description="Creative; steady but can stall on broad scans",
    ),
    "meta-muse-spark-1.2": ModelProfile(
        lockup_risk="low-medium", heartbeat_multiplier=1.8, log_stall_seconds=150,
        restart_cooldown=5, description="Writing-heavy; generally fast",
    ),
    "xiaomi-mimo-2.5": ModelProfile(
        lockup_risk="low", heartbeat_multiplier=1.5, log_stall_seconds=120,
        restart_cooldown=3, description="Balanced/fast; fails quickly if it fails",
    ),
}


ALL_MODELS = list(ALL_MODELS_UNIFIED)

from typing import NamedTuple


class ModelAssignment(NamedTuple):
    model: str
    fallback: str


TIERED_MODEL_POOLS: dict[str, tuple[str, ...]] = {
    "unified": ALL_MODELS_UNIFIED,
}

TIER_FALLBACK: dict[str, str] = {
    "unified": "qwen-3.7-plus",
}

MODEL_FALLBACKS: dict[str, str] = {
    "qwen-3.8-max": "qwen-3.7-plus",
    "qwen-3.8-max-thinking": "qwen-3.7-max-thinking",
    "qwen-3.7-max": "qwen-3.6-plus",
    "qwen-3.7-max-thinking": "qwen-3.7-plus",
    "qwen-3.7-plus": "qwen-3.6-plus",
    "qwen-3.6-plus": "qwen-3.5-plus",
    "qwen-3.6-plus-thinking": "qwen-3.7-max-thinking",
    "qwen-3.5-plus": "xiaomi-mimo-2.5",
    "qwen-3.5-plus-thinking": "qwen-3.7-max-thinking",
    "meta-muse-spark-1.3": "qwen-3.6-plus",
    "meta-muse-spark-1.2": "qwen-3.5-plus",
    "xiaomi-mimo-2.5": "qwen-3.5-plus",
}

_unified_rotation_index: int = 0


def _tier_for_role(role_name: str) -> str:
    return "unified"


def next_model_for_role(
    role_name: str,
    *,
    ticket_class: str = "",
    severity: str = "",
    risk: str = "",
) -> ModelAssignment:
    global _unified_rotation_index
    pool = ALL_MODELS_UNIFIED
    idx = _unified_rotation_index % len(pool)
    model = pool[idx]
    fallback_model = MODEL_FALLBACKS.get(model, "qwen-3.7-plus")
    _unified_rotation_index = idx + 1
    return ModelAssignment(model=model, fallback=fallback_model)


def model_profile(model: str) -> ModelProfile | None:
    """Return profile for model, or None if unknown."""
    return MODEL_PROFILES.get(model)


def effective_heartbeat_timeout(interval_seconds: int, model: str, base_timeout: int) -> int:
    """Effective heartbeat timeout after applying model profile.
    
    Args:
        interval_seconds: Base run interval for the bot
        model: Model name to look up profile
        base_timeout: Minimum timeout value
    
    Returns:
        Effective timeout in seconds
    """
    prof = model_profile(model)
    if prof:
        derived = int(interval_seconds * prof.heartbeat_multiplier)
        return max(derived, base_timeout)
    return base_timeout


def rotate_model_on_error(bot: Any, bots: dict[str, Any] | None = None) -> str:
    """Simple model rotation for error recovery.
    
    Uses model_router's provider chain to select next model.
    Falls back to round-robin through ALL_MODELS if router unavailable.
    
    Args:
        bot: BotState object with config.model attribute
        bots: Optional dict of all bots (unused but kept for API compatibility)
    
    Returns:
        The new model name assigned to the bot
    """
    try:
        from codebot.model_router import build_default_chain
        chain = build_default_chain()
        healthy = chain.healthy_providers()
        if healthy:
            # Get all models from healthy providers
            available_models = []
            for p in healthy:
                for alias in p.model_aliases.values():
                    if alias not in available_models:
                        available_models.append(alias)
            # Also include canonical names
            for m in ALL_MODELS:
                if m not in available_models:
                    available_models.append(m)
            failed_model = bot.config.model
            candidates = [m for m in available_models if m != failed_model]
            if candidates:
                new_model = candidates[0]
                old_model = bot.config.model
                bot.config.model = new_model
                bot.config.fallback_model = "xiaomi-mimo-2.5"
                logger.info(f"Model rotation for '{bot.config.name}': {old_model} -> {new_model}")
                return new_model
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Model router rotation failed: {e}, using fallback")

    # Fallback: simple rotation
    failed_model = bot.config.model
    candidates = [m for m in ALL_MODELS if m != failed_model]
    if not candidates:
        return failed_model
    new_model = candidates[0]
    old_model = bot.config.model
    bot.config.model = new_model
    bot.config.fallback_model = "xiaomi-mimo-2.5"
    logger.info(f"Model rotation for '{bot.config.name}': {old_model} -> {new_model}")
    return new_model


def get_model_tier_for_complexity(complexity: str) -> list[str]:
    """Get recommended models for a given complexity tier.
    
    Args:
        complexity: One of 'low', 'medium', 'high'
    
    Returns:
        List of recommended model names in priority order
    """
    tier_mapping = {
        "low": ["xiaomi-mimo-2.5", "qwen-3.5-plus"],
        "medium": ["qwen-3.7-plus", "qwen-3.6-plus", "qwen-3.7-max"],
        "high": ["qwen-3.8-max", "qwen-3.8-max-thinking", "qwen-3.7-max-thinking"],
    }
    return tier_mapping.get(complexity, tier_mapping["medium"])
