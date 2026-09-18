"""Multi-provider model routing with automatic fallback.

Purpose
-------
Provides a routing layer that selects the cheapest capable model for a task,
tracks per-provider success rates, and automatically fails over to healthy
providers when the primary fails.

Why
---
GOALS.md Goal 7 requires automatic fallback between providers (dialagram → openai → anthropic)
and cost-based model selection for non-critical tasks. Currently only dialagram is configured,
causing complete failure when that provider is unavailable.

Invariants
----------
- stdlib-only (dataclasses, json, os, time, pathlib)
- Fallback chain is deterministic: dialagram -> openai -> anthropic
- Health tracking uses consecutive failures (>=3) or low success rate (<50% with 10+ calls)
- Model aliases allow canonical names to map to provider-specific names
- No secrets logged or exposed in error messages
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


@dataclass
class ProviderConfig:
    """Configuration for a single LLM provider.
    
    Attributes:
        name: Provider identifier (e.g., "dialagram", "openai")
        api_url: Full API endpoint URL
        api_key_env: Environment variable name containing the API key
        model_aliases: Maps canonical model names to provider-specific names
        cost_per_1k_tokens: Maps model names to cost per 1K tokens (USD)
        is_openai_compatible: Whether this provider uses OpenAI-compatible API format
    """
    name: str
    api_url: str
    api_key_env: str
    model_aliases: Dict[str, str] = field(default_factory=dict)
    cost_per_1k_tokens: Dict[str, float] = field(default_factory=dict)
    is_openai_compatible: bool = True


class ProviderHealth:
    """Tracks health metrics for a single provider.
    
    A provider is considered unhealthy if:
    - It has 3+ consecutive failures, OR
    - It has 10+ total calls AND success rate < 50%
    """
    
    def __init__(self, provider: str):
        self.provider = provider
        self.total_calls = 0
        self.failures = 0
        self.consecutive_failures = 0
    
    def record_success(self) -> None:
        """Record a successful API call."""
        self.total_calls += 1
        self.consecutive_failures = 0
    
    def record_failure(self) -> None:
        """Record a failed API call."""
        self.total_calls += 1
        self.failures += 1
        self.consecutive_failures += 1
    
    def reset(self) -> None:
        """Reset all health metrics."""
        self.total_calls = 0
        self.failures = 0
        self.consecutive_failures = 0
    
    @property
    def success_rate(self) -> float:
        """Calculate success rate as a fraction (0.0 to 1.0)."""
        if self.total_calls == 0:
            return 1.0
        successes = self.total_calls - self.failures
        return successes / self.total_calls
    
    @property
    def is_healthy(self) -> bool:
        """Determine if provider is healthy based on failure patterns."""
        # Unhealthy if 3+ consecutive failures
        if self.consecutive_failures >= 3:
            return False
        # Unhealthy if 10+ calls and success rate < 50%
        if self.total_calls >= 10 and self.success_rate < 0.5:
            return False
        return True


class FallbackChain:
    """Ordered list of providers with health tracking and model resolution.
    
    Provides methods to:
    - Get healthy providers in priority order
    - Resolve canonical model names to provider-specific names
    - Select the cheapest healthy provider for a given model
    """
    
    def __init__(self, providers: List[ProviderConfig]):
        self.providers = providers
        self._health: Dict[str, ProviderHealth] = {
            p.name: ProviderHealth(p.name) for p in providers
        }
    
    def healthy_providers(self) -> List[ProviderConfig]:
        """Return providers that are currently healthy, in priority order."""
        return [p for p in self.providers if self._health[p.name].is_healthy]
    
    def get_provider(self, name: str) -> Optional[ProviderConfig]:
        """Get provider configuration by name, or None if not found."""
        for p in self.providers:
            if p.name == name:
                return p
        return None
    
    def resolve_model(self, provider_name: str, model_name: str) -> str:
        """Resolve canonical model name to provider-specific name.
        
        If the provider has an alias for this model, return the alias.
        Otherwise, return the model name as-is.
        """
        provider = self.get_provider(provider_name)
        if provider is None:
            return model_name
        return provider.model_aliases.get(model_name, model_name)
    
    def select_cheapest(self, model_name: str) -> Optional[ProviderConfig]:
        """Select the cheapest healthy provider that supports this model.
        
        Returns None if no healthy provider supports this model.
        """
        healthy = self.healthy_providers()
        if not healthy:
            return None
        
        # Find cheapest among healthy providers
        best_provider = None
        best_cost = float('inf')
        
        for provider in healthy:
            # Check if provider has pricing for this model
            resolved_model = provider.model_aliases.get(model_name, model_name)
            cost = provider.cost_per_1k_tokens.get(resolved_model)
            
            # If no pricing info, use a default high cost (prefer providers with explicit pricing)
            if cost is None:
                cost = 999999.0
            
            if cost < best_cost:
                best_cost = cost
                best_provider = provider
        
        return best_provider


def build_default_chain() -> FallbackChain:
    """Build the default fallback chain: dialagram -> openai -> anthropic."""
    return FallbackChain(list(DEFAULT_PROVIDER_CHAIN))


# Default provider chain configuration
DEFAULT_PROVIDER_CHAIN: List[ProviderConfig] = [
    ProviderConfig(
        name="dialagram",
        api_url="https://dialagram.me/router/v1/chat/completions",
        api_key_env="DIALAGRAM_API_KEY",
        model_aliases={},  # Uses canonical names as-is
        cost_per_1k_tokens={
            "xiaomi-mimo-2.5": 0.0005,
            "qwen-3.5-plus": 0.001,
            "qwen-3.6-plus": 0.0015,
            "qwen-3.7-plus": 0.002,
            "qwen-3.8-max": 0.003,
        },
        is_openai_compatible=True,
    ),
    ProviderConfig(
        name="openai",
        api_url="https://api.openai.com/v1/chat/completions",
        api_key_env="OPENAI_API_KEY",
        model_aliases={
            "xiaomi-mimo-2.5": "gpt-4o-mini",
            "qwen-3.5-plus": "gpt-4o-mini",
            "qwen-3.7-plus": "gpt-4o",
            "qwen-3.8-max": "gpt-4.1",
        },
        cost_per_1k_tokens={
            "gpt-4o-mini": 0.00015,
            "gpt-4o": 0.005,
            "gpt-4.1": 0.01,
        },
        is_openai_compatible=True,
    ),
    ProviderConfig(
        name="anthropic",
        api_url="https://api.anthropic.com/v1/messages",
        api_key_env="ANTHROPIC_API_KEY",
        model_aliases={
            "xiaomi-mimo-2.5": "claude-3-5-haiku-20241022",
            "qwen-3.7-plus": "claude-sonnet-4-20250514",
            "qwen-3.8-max": "claude-opus-4-20250514",
        },
        cost_per_1k_tokens={
            "claude-3-5-haiku-20241022": 0.00025,
            "claude-sonnet-4-20250514": 0.003,
            "claude-opus-4-20250514": 0.015,
        },
        is_openai_compatible=False,
    ),
]


class ModelRouter:
    """High-level router for multi-provider model selection and failover.
    
    Manages:
    - Provider chain with health tracking
    - Success/failure recording
    - Stats persistence across instances
    - Active provider selection with automatic failover
    """
    
    def __init__(
        self,
        providers: Optional[List[ProviderConfig]] = None,
        state_dir: str = ".codebot/state",
    ):
        """Initialize the router.
        
        Args:
            providers: Custom provider list, or None to use default chain
            state_dir: Directory for persisting health stats
        """
        if providers is None:
            self.chain = build_default_chain()
        else:
            self.chain = FallbackChain(providers)
        
        self.state_dir = state_dir
        self._stats_file = Path(state_dir) / "model_router_stats.json"
        
        # Load persisted stats if available
        self._load_stats()
    
    def _load_stats(self) -> None:
        """Load persisted health stats from disk."""
        try:
            if self._stats_file.exists():
                data = json.loads(self._stats_file.read_text(encoding="utf-8"))
                for provider_name, stats in data.items():
                    if provider_name in self.chain._health:
                        health = self.chain._health[provider_name]
                        health.total_calls = stats.get("total_calls", 0)
                        health.failures = stats.get("failures", 0)
                        health.consecutive_failures = stats.get("consecutive_failures", 0)
        except Exception:
            # Fail-open: if stats can't be loaded, start fresh
            pass
    
    def _save_stats(self) -> None:
        """Persist health stats to disk."""
        try:
            data = {}
            for provider_name, health in self.chain._health.items():
                data[provider_name] = {
                    "total_calls": health.total_calls,
                    "failures": health.failures,
                    "consecutive_failures": health.consecutive_failures,
                }
            self._stats_file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._stats_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            tmp.replace(self._stats_file)
        except Exception:
            # Fail-open: stats persistence is best-effort
            pass
    
    def get_api_url(self, provider_name: str) -> Optional[str]:
        """Get API URL for a provider, or first provider if not found."""
        provider = self.chain.get_provider(provider_name)
        if provider is None:
            # Fall back to first provider
            if self.chain.providers:
                return self.chain.providers[0].api_url
            return None
        return provider.api_url
    
    def get_api_key(self, provider_name: str) -> Optional[str]:
        """Resolve API key from environment variable for a provider."""
        provider = self.chain.get_provider(provider_name)
        if provider is None:
            return None
        key = os.environ.get(provider.api_key_env, "").strip()
        return key if key else None
    
    def record_success(self, provider_name: str, model_name: str) -> None:
        """Record a successful API call for a provider."""
        if provider_name in self.chain._health:
            self.chain._health[provider_name].record_success()
            self._save_stats()
    
    def record_failure(self, provider_name: str, model_name: str) -> None:
        """Record a failed API call for a provider."""
        if provider_name in self.chain._health:
            self.chain._health[provider_name].record_failure()
            self._save_stats()
    
    def get_stats(self, provider_name: str) -> Dict[str, Any]:
        """Get health stats for a provider."""
        if provider_name not in self.chain._health:
            return {"total_calls": 0, "failures": 0, "success_rate": 1.0}
        
        health = self.chain._health[provider_name]
        return {
            "total_calls": health.total_calls,
            "failures": health.failures,
            "success_rate": health.success_rate,
            "consecutive_failures": health.consecutive_failures,
            "is_healthy": health.is_healthy,
        }
    
    def get_active_provider(self, preferred_provider: str) -> Optional[ProviderConfig]:
        """Get the active provider, with automatic failover if preferred is unhealthy.
        
        Args:
            preferred_provider: The provider to use if healthy
        
        Returns:
            ProviderConfig for the active provider, or None if all are unhealthy
        """
        # Check if preferred provider is healthy
        if preferred_provider in self.chain._health:
            if self.chain._health[preferred_provider].is_healthy:
                provider = self.chain.get_provider(preferred_provider)
                if provider:
                    return provider
        
        # Preferred provider is unhealthy, find next healthy provider
        healthy = self.chain.healthy_providers()
        if healthy:
            return healthy[0]
        
        # All providers unhealthy - return first as last resort
        if self.chain.providers:
            return self.chain.providers[0]
        
        return None
    
    def select_cheapest_provider(self, model_name: str) -> Optional[ProviderConfig]:
        """Select the cheapest healthy provider for a given model."""
        return self.chain.select_cheapest(model_name)
    
    def is_openai_compatible(self, provider_name: str) -> bool:
        """Check if a provider uses OpenAI-compatible API format."""
        provider = self.chain.get_provider(provider_name)
        if provider is None:
            return True  # Default to True for unknown providers
        return provider.is_openai_compatible
    
    def resolve_model(self, provider_name: str, model_name: str) -> str:
        """Resolve canonical model name to provider-specific name."""
        return self.chain.resolve_model(provider_name, model_name)
    
    def get_model_aliases(self, provider_name: str) -> Dict[str, str]:
        """Get model aliases for a provider."""
        provider = self.chain.get_provider(provider_name)
        if provider is None:
            return {}
        return provider.model_aliases
