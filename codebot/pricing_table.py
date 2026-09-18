"""Model pricing table for monetary cost calculation.

Purpose
-------
Converts token counts to USD using per-model provider pricing. Supports
daily, weekly, and monthly aggregation windows with configurable budget alerts.

Why
---
The existing cost_tracker records tokens but not dollars. The economics engine
needs monetary values for budgeting decisions. This module provides the
pricing lookup and conversion layer without introducing third-party dependencies.

Invariants
----------
- stdlib-only (no external dependencies)
- Pricing is expressed as USD per 1M tokens (industry standard)
- Unknown models return zero cost (fail-open, never crash)
- All public functions have type hints on parameters and returns
- No I/O in this module; pure computation only
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ModelPricing:
    """Per-model pricing in USD per 1M tokens."""
    prompt_usd_per_1m: float
    completion_usd_per_1m: float
    provider: str


# Default pricing table. Values are approximate as of mid-2026.
# Keys are normalized model identifiers (lowercase, hyphenated).
DEFAULT_PRICING: dict[str, ModelPricing] = {
    # OpenAI
    "gpt-4o": ModelPricing(prompt_usd_per_1m=2.50, completion_usd_per_1m=10.00, provider="openai"),
    "gpt-4o-mini": ModelPricing(prompt_usd_per_1m=0.15, completion_usd_per_1m=0.60, provider="openai"),
    "gpt-4.1": ModelPricing(prompt_usd_per_1m=2.00, completion_usd_per_1m=8.00, provider="openai"),
    "gpt-4.1-mini": ModelPricing(prompt_usd_per_1m=0.40, completion_usd_per_1m=1.60, provider="openai"),
    "o3": ModelPricing(prompt_usd_per_1m=10.00, completion_usd_per_1m=40.00, provider="openai"),
    "o3-mini": ModelPricing(prompt_usd_per_1m=1.10, completion_usd_per_1m=4.40, provider="openai"),
    "o4-mini": ModelPricing(prompt_usd_per_1m=1.10, completion_usd_per_1m=4.40, provider="openai"),

    # Anthropic
    "claude-sonnet-4-20250514": ModelPricing(prompt_usd_per_1m=3.00, completion_usd_per_1m=15.00, provider="anthropic"),
    "claude-opus-4-20250514": ModelPricing(prompt_usd_per_1m=15.00, completion_usd_per_1m=75.00, provider="anthropic"),
    "claude-3-5-haiku-20241022": ModelPricing(prompt_usd_per_1m=0.80, completion_usd_per_1m=4.00, provider="anthropic"),
    "claude-sonnet-4": ModelPricing(prompt_usd_per_1m=3.00, completion_usd_per_1m=15.00, provider="anthropic"),
    "claude-opus-4": ModelPricing(prompt_usd_per_1m=15.00, completion_usd_per_1m=75.00, provider="anthropic"),

    # Qwen / Alibaba
    "qwen-3.8-max": ModelPricing(prompt_usd_per_1m=1.20, completion_usd_per_1m=4.80, provider="alibaba"),
    "qwen-3.8-plus": ModelPricing(prompt_usd_per_1m=0.40, completion_usd_per_1m=1.20, provider="alibaba"),
    "qwen-max": ModelPricing(prompt_usd_per_1m=1.60, completion_usd_per_1m=6.40, provider="alibaba"),

    # Dialagram (placeholder for custom provider)
    "dialagram-default": ModelPricing(prompt_usd_per_1m=1.00, completion_usd_per_1m=3.00, provider="dialagram"),
}


def _normalize_model(model: str) -> str:
    """Normalize a model identifier for pricing lookup."""
    return model.strip().lower().replace(" ", "-")


def get_pricing(model: str, pricing_table: dict[str, ModelPricing] | None = None) -> ModelPricing | None:
    """Look up pricing for a model. Returns None if unknown.

    Args:
        model: Model identifier string.
        pricing_table: Optional override table. Defaults to DEFAULT_PRICING.

    Returns:
        ModelPricing if found, None otherwise.
    """
    table = pricing_table if pricing_table is not None else DEFAULT_PRICING
    normalized = _normalize_model(model)
    return table.get(normalized)


def calculate_cost_usd(
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    pricing_table: dict[str, ModelPricing] | None = None,
) -> float:
    """Calculate monetary cost in USD for a single API call.

    Args:
        model: Model identifier string.
        prompt_tokens: Number of input tokens consumed.
        completion_tokens: Number of output tokens generated.
        pricing_table: Optional override pricing table.

    Returns:
        Cost in USD. Returns 0.0 for unknown models (fail-open).
    """
    pricing = get_pricing(model, pricing_table)
    if pricing is None:
        return 0.0
    prompt_cost = max(0, prompt_tokens) * pricing.prompt_usd_per_1m / 1_000_000.0
    completion_cost = max(0, completion_tokens) * pricing.completion_usd_per_1m / 1_000_000.0
    return round(prompt_cost + completion_cost, 8)


def calculate_ticket_cost_usd(
    ticket_totals: dict[str, Any],
    model: str,
    pricing_table: dict[str, ModelPricing] | None = None,
) -> float:
    """Calculate total USD cost for a ticket from its aggregated token totals.

    Args:
        ticket_totals: Dict with 'prompt_tokens' and 'completion_tokens' keys
                       (as returned by CostTracker.get_ticket_total).
        model: Primary model used for the ticket (for pricing lookup).
        pricing_table: Optional override pricing table.

    Returns:
        Total cost in USD.
    """
    prompt = int(ticket_totals.get("prompt_tokens", 0))
    completion = int(ticket_totals.get("completion_tokens", 0))
    return calculate_cost_usd(model, prompt, completion, pricing_table)


def list_providers(pricing_table: dict[str, ModelPricing] | None = None) -> list[str]:
    """Return sorted list of unique provider names in the pricing table.

    Args:
        pricing_table: Optional override pricing table.

    Returns:
        Sorted list of provider name strings.
    """
    table = pricing_table if pricing_table is not None else DEFAULT_PRICING
    providers = {p.provider for p in table.values()}
    return sorted(providers)


def list_models_for_provider(
    provider: str,
    pricing_table: dict[str, ModelPricing] | None = None,
) -> list[str]:
    """Return sorted list of model names for a given provider.

    Args:
        provider: Provider name to filter by.
        pricing_table: Optional override pricing table.

    Returns:
        Sorted list of model identifier strings.
    """
    table = pricing_table if pricing_table is not None else DEFAULT_PRICING
    return sorted(k for k, v in table.items() if v.provider == provider)
