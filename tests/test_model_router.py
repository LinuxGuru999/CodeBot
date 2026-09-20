"""Tests for model_router.py — multi-provider routing with automatic fallback.

Covers: ProviderConfig, FallbackChain, ModelRouter, cost-based selection,
success-rate tracking, and automatic provider failover.
"""
import json
import time
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.model_router import (
    ProviderConfig,
    ProviderHealth,
    FallbackChain,
    ModelRouter,
    DEFAULT_PROVIDER_CHAIN,
    build_default_chain,
)


class TestProviderConfig:
    def test_basic_creation(self):
        pc = ProviderConfig(
            name="dialagram",
            api_url="https://dialagram.me/router/v1/chat/completions",
            api_key_env="DIALAGRAM_API_KEY",
        )
        assert pc.name == "dialagram"
        assert "dialagram.me" in pc.api_url

    def test_model_aliases_default_empty(self):
        pc = ProviderConfig(
            name="test",
            api_url="https://test.example.com/v1/chat/completions",
            api_key_env="TEST_KEY",
        )
        assert pc.model_aliases == {}

    def test_model_aliases_custom(self):
        aliases = {"gpt-4o": "gpt-4", "claude-sonnet-4": "claude-3.5-sonnet"}
        pc = ProviderConfig(
            name="openai",
            api_url="https://api.openai.com/v1/chat/completions",
            api_key_env="OPENAI_API_KEY",
            model_aliases=aliases,
        )
        assert pc.model_aliases == aliases

    def test_cost_per_1k_tokens_default_empty(self):
        pc = ProviderConfig(name="test", api_url="https://x.com", api_key_env="K")
        assert pc.cost_per_1k_tokens == {}

    def test_cost_per_1k_tokens_custom(self):
        costs = {"gpt-4o": 0.005, "gpt-4o-mini": 0.00015}
        pc = ProviderConfig(
            name="openai",
            api_url="https://api.openai.com/v1/chat/completions",
            api_key_env="OPENAI_API_KEY",
            cost_per_1k_tokens=costs,
        )
        assert pc.cost_per_1k_tokens == costs

    def test_is_openai_compatible_default_true(self):
        pc = ProviderConfig(name="openai", api_url="https://api.openai.com/v1/chat/completions", api_key_env="K")
        assert pc.is_openai_compatible is True

    def test_is_openai_compatible_anthropic(self):
        pc = ProviderConfig(
            name="anthropic",
            api_url="https://api.anthropic.com/v1/messages",
            api_key_env="ANTHROPIC_API_KEY",
            is_openai_compatible=False,
        )
        assert pc.is_openai_compatible is False


class TestProviderHealth:
    def test_new_provider_is_healthy(self):
        h = ProviderHealth(provider="dialagram")
        assert h.is_healthy is True
        assert h.success_rate == 1.0

    def test_recording_success(self):
        h = ProviderHealth(provider="dialagram")
        for _ in range(5):
            h.record_success()
        assert h.total_calls == 5
        assert h.failures == 0
        assert h.success_rate == 1.0

    def test_recording_failure(self):
        h = ProviderHealth(provider="dialagram")
        for _ in range(5):
            h.record_failure()
        assert h.total_calls == 5
        assert h.failures == 5
        assert h.consecutive_failures == 5

    def test_unhealthy_after_consecutive_failures(self):
        h = ProviderHealth(provider="dialagram")
        for _ in range(3):
            h.record_failure()
        assert h.is_healthy is False

    def test_unhealthy_after_low_success_rate(self):
        h = ProviderHealth(provider="dialagram")
        for _ in range(6):
            h.record_failure()
        for _ in range(4):
            h.record_success()
        # 6 failures + 4 successes = 10 total, 4/10 = 40% < 50%
        # consecutive_failures reset to 0 by successes, so only low rate triggers
        assert h.total_calls == 10
        assert h.is_healthy is False

    def test_unhealthy_boundary_exactly_9_calls_not_enough(self):
        """Less than 10 total calls should NOT trigger the low-success-rate check."""
        h = ProviderHealth(provider="dialagram")
        for _ in range(5):
            h.record_failure()
        for _ in range(4):
            h.record_success()
        # 5+4=9 total calls, success_rate=44% but <10 calls so still healthy
        assert h.total_calls == 9
        assert h.is_healthy is True

    def test_healthy_exactly_50_percent(self):
        """Exactly 50% success rate with 10+ calls should still be healthy."""
        h = ProviderHealth(provider="dialagram")
        for _ in range(10):
            if _ % 2 == 0:
                h.record_failure()
            else:
                h.record_success()
        # 5 failures + 5 successes = 10 total, 50% == 50% (not <)
        assert h.total_calls == 10
        assert h.is_healthy is True

    def test_success_rate_zero_when_no_calls(self):
        """Success rate returns 1.0 (healthy) when no calls recorded."""
        h = ProviderHealth(provider="dialagram")
        assert h.total_calls == 0
        assert h.success_rate == 1.0

    def test_healthy_with_mixed_results_above_threshold(self):
        h = ProviderHealth(provider="dialagram")
        for _ in range(2):
            h.record_failure()
        for _ in range(8):
            h.record_success()
        # 8/10 = 80% >= 50%
        assert h.is_healthy is True

    def test_success_resets_consecutive_failures(self):
        h = ProviderHealth(provider="dialagram")
        h.record_failure()
        h.record_failure()
        assert h.consecutive_failures == 2
        h.record_success()
        assert h.consecutive_failures == 0

    def test_health_reset(self):
        h = ProviderHealth(provider="dialagram")
        for _ in range(5):
            h.record_failure()
        assert h.is_healthy is False
        h.reset()
        assert h.total_calls == 0
        assert h.failures == 0
        assert h.consecutive_failures == 0
        assert h.is_healthy is True


class TestFallbackChain:
    def test_creation_with_providers(self):
        chain = FallbackChain([
            ProviderConfig(name="p1", api_url="https://p1.com", api_key_env="K1"),
            ProviderConfig(name="p2", api_url="https://p2.com", api_key_env="K2"),
        ])
        assert len(chain.providers) == 2

    def test_get_healthy_providers_in_order(self):
        p1 = ProviderConfig(name="p1", api_url="https://p1.com", api_key_env="K1")
        p2 = ProviderConfig(name="p2", api_url="https://p2.com", api_key_env="K2")
        chain = FallbackChain([p1, p2])
        healthy = chain.healthy_providers()
        assert len(healthy) == 2
        assert healthy[0].name == "p1"
        assert healthy[1].name == "p2"

    def test_unhealthy_provider_excluded(self):
        p1 = ProviderConfig(name="p1", api_url="https://p1.com", api_key_env="K1")
        p2 = ProviderConfig(name="p2", api_url="https://p2.com", api_key_env="K2")
        chain = FallbackChain([p1, p2])
        # Make p1 unhealthy
        for _ in range(3):
            chain._health["p1"].record_failure()
        healthy = chain.healthy_providers()
        assert len(healthy) == 1
        assert healthy[0].name == "p2"

    def test_get_provider_by_name(self):
        p1 = ProviderConfig(name="p1", api_url="https://p1.com", api_key_env="K1")
        chain = FallbackChain([p1])
        assert chain.get_provider("p1") is p1
        assert chain.get_provider("nonexistent") is None

    def test_resolve_model_name_with_alias(self):
        p1 = ProviderConfig(
            name="openai",
            api_url="https://api.openai.com",
            api_key_env="K",
            model_aliases={"xiaomi-mimo-2.5": "gpt-4o"},
        )
        chain = FallbackChain([p1])
        resolved = chain.resolve_model("openai", "xiaomi-mimo-2.5")
        assert resolved == "gpt-4o"

    def test_resolve_model_name_without_alias(self):
        p1 = ProviderConfig(
            name="dialagram",
            api_url="https://dialagram.me",
            api_key_env="K",
        )
        chain = FallbackChain([p1])
        resolved = chain.resolve_model("dialagram", "xiaomi-mimo-2.5")
        assert resolved == "xiaomi-mimo-2.5"

    def test_select_cheapest_provider(self):
        p1 = ProviderConfig(
            name="expensive",
            api_url="https://e.com",
            api_key_env="K1",
            cost_per_1k_tokens={"model-a": 0.05},
        )
        p2 = ProviderConfig(
            name="cheap",
            api_url="https://c.com",
            api_key_env="K2",
            cost_per_1k_tokens={"model-a": 0.001},
        )
        chain = FallbackChain([p1, p2])
        best = chain.select_cheapest("model-a")
        assert best is not None
        assert best.name == "cheap"

    def test_select_cheapest_ignores_unhealthy(self):
        p1 = ProviderConfig(
            name="cheap_unhealthy",
            api_url="https://c.com",
            api_key_env="K1",
            cost_per_1k_tokens={"model-a": 0.001},
        )
        p2 = ProviderConfig(
            name="expensive_healthy",
            api_url="https://e.com",
            api_key_env="K2",
            cost_per_1k_tokens={"model-a": 0.05},
        )
        chain = FallbackChain([p1, p2])
        for _ in range(3):
            chain._health["cheap_unhealthy"].record_failure()
        best = chain.select_cheapest("model-a")
        assert best is not None
        assert best.name == "expensive_healthy"

    def test_select_cheapest_returns_none_if_all_unhealthy(self):
        p1 = ProviderConfig(name="p1", api_url="https://p1.com", api_key_env="K1")
        chain = FallbackChain([p1])
        for _ in range(3):
            chain._health["p1"].record_failure()
        best = chain.select_cheapest("model-a")
        assert best is None


class TestModelRouter:
    def test_creation(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        assert router is not None

    def test_default_chain_loaded(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        assert len(router.chain.providers) > 0
        names = [p.name for p in router.chain.providers]
        assert "dialagram" in names

    def test_get_api_url_for_provider(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        url = router.get_api_url("dialagram")
        assert url is not None
        assert "dialagram" in url

    def test_get_api_url_unknown_provider_returns_default(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        url = router.get_api_url("nonexistent")
        assert url is not None  # Should fall back to first provider

    def test_resolve_api_key_from_env(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        with patch.dict("os.environ", {"DIALAGRAM_API_KEY": "test-key-123"}):
            key = router.get_api_key("dialagram")
            assert key == "test-key-123"

    def test_resolve_api_key_missing_returns_none(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        with patch.dict("os.environ", {}, clear=True):
            key = router.get_api_key("dialagram")
            assert key is None

    def test_record_success(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        router.record_success("dialagram", "xiaomi-mimo-2.5")
        stats = router.get_stats("dialagram")
        assert stats["total_calls"] == 1

    def test_record_failure(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        router.record_failure("dialagram", "xiaomi-mimo-2.5")
        stats = router.get_stats("dialagram")
        assert stats["total_calls"] == 1
        assert stats["failures"] == 1

    def test_get_active_provider_initial(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        provider = router.get_active_provider("dialagram")
        assert provider is not None
        assert provider.name == "dialagram"

    def test_failover_to_next_provider(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        # Make dialagram unhealthy
        for _ in range(3):
            router.record_failure("dialagram", "xiaomi-mimo-2.5")
        # Should failover to next healthy provider
        provider = router.get_active_provider("dialagram")
        assert provider is not None
        assert provider.name != "dialagram"

    def test_failover_keeps_provider_on_success(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        router.record_success("dialagram", "xiaomi-mimo-2.5")
        provider = router.get_active_provider("dialagram")
        assert provider is not None
        assert provider.name == "dialagram"

    def test_stats_persist_across_instances(self, tmp_path):
        router1 = ModelRouter(state_dir=str(tmp_path))
        router1.record_success("dialagram", "xiaomi-mimo-2.5")
        router1.record_success("dialagram", "xiaomi-mimo-2.5")

        router2 = ModelRouter(state_dir=str(tmp_path))
        stats = router2.get_stats("dialagram")
        assert stats["total_calls"] == 2

    def test_select_cheapest_model(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        provider = router.select_cheapest_provider("xiaomi-mimo-2.5")
        # Should return some provider (dialagram is default/cheapest for this model)
        assert provider is not None

    def test_is_openai_compatible(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        assert router.is_openai_compatible("dialagram") is True

    def test_custom_providers(self, tmp_path):
        custom = [
            ProviderConfig(
                name="custom-p1",
                api_url="https://custom.api.com/v1/chat/completions",
                api_key_env="CUSTOM_KEY",
            ),
        ]
        router = ModelRouter(providers=custom, state_dir=str(tmp_path))
        assert len(router.chain.providers) == 1
        assert router.chain.providers[0].name == "custom-p1"

    def test_health_file_persisted(self, tmp_path):
        router1 = ModelRouter(state_dir=str(tmp_path))
        for _ in range(5):
            router1.record_success("dialagram", "model")
        # Create new router to verify persistence
        router2 = ModelRouter(state_dir=str(tmp_path))
        stats = router2.get_stats("dialagram")
        assert stats["total_calls"] == 5

    def test_get_model_aliases(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path))
        aliases = router.get_model_aliases("dialagram")
        # Default dialagram config should have some aliases
        assert isinstance(aliases, dict)


class TestDefaultProviderChain:
    def test_has_dialagram(self):
        names = [p.name for p in DEFAULT_PROVIDER_CHAIN]
        assert "dialagram" in names

    def test_all_providers_have_urls(self):
        for p in DEFAULT_PROVIDER_CHAIN:
            assert p.api_url, f"{p.name} missing api_url"
            assert p.api_key_env, f"{p.name} missing api_key_env"

    def test_build_default_chain(self):
        chain = build_default_chain()
        assert len(chain.providers) > 0


class TestIntegration:
    """Integration tests for ModelRouter with realistic scenarios."""

    def test_full_fallback_scenario(self, tmp_path):
        """Simulate primary provider failure and fallback to secondary."""
        p1 = ProviderConfig(
            name="primary",
            api_url="https://primary.com/v1/chat/completions",
            api_key_env="PRIMARY_KEY",
        )
        p2 = ProviderConfig(
            name="secondary",
            api_url="https://secondary.com/v1/chat/completions",
            api_key_env="SECONDARY_KEY",
        )
        router = ModelRouter(providers=[p1, p2], state_dir=str(tmp_path))

        # Initial request should use primary
        provider = router.get_active_provider("primary")
        assert provider.name == "primary"

        # Simulate failures
        for _ in range(3):
            router.record_failure("primary", "model-a")

        # Now should failover to secondary
        provider = router.get_active_provider("primary")
        assert provider.name == "secondary"

    def test_cost_based_routing(self, tmp_path):
        """Verify cheapest provider is selected for non-critical tasks."""
        p_expensive = ProviderConfig(
            name="expensive",
            api_url="https://exp.com/v1/chat/completions",
            api_key_env="E_KEY",
            cost_per_1k_tokens={"model-a": 0.10},
        )
        p_cheap = ProviderConfig(
            name="cheap",
            api_url="https://cheap.com/v1/chat/completions",
            api_key_env="C_KEY",
            cost_per_1k_tokens={"model-a": 0.002},
        )
        router = ModelRouter(providers=[p_expensive, p_cheap], state_dir=str(tmp_path))
        best = router.select_cheapest_provider("model-a")
        assert best.name == "cheap"

    def test_all_providers_down(self, tmp_path):
        """When all providers are unhealthy, should return None or first."""
        p1 = ProviderConfig(name="p1", api_url="https://p1.com", api_key_env="K1")
        router = ModelRouter(providers=[p1], state_dir=str(tmp_path))
        for _ in range(5):
            router.record_failure("p1", "model")
        # Even unhealthy, should still return the provider as last resort
        provider = router.get_active_provider("p1")
        assert provider is not None  # Last resort: use primary anyway

    def test_recording_and_querying_stats(self, tmp_path):
        """Verify stats are recorded and queryable."""
        router = ModelRouter(state_dir=str(tmp_path))

        # Record mix of successes and failures
        for _ in range(8):
            router.record_success("dialagram", "model-a")
        for _ in range(2):
            router.record_failure("dialagram", "model-a")

        stats = router.get_stats("dialagram")
        assert stats["total_calls"] == 10
        assert stats["failures"] == 2
        assert stats["success_rate"] == pytest.approx(0.8)

    def test_resolve_model_across_providers(self, tmp_path):
        """Verify model name resolution via aliases."""
        p1 = ProviderConfig(
            name="openai",
            api_url="https://api.openai.com/v1/chat/completions",
            api_key_env="OPENAI_API_KEY",
            model_aliases={"xiaomi-mimo-2.5": "gpt-4o"},
        )
        p2 = ProviderConfig(
            name="dialagram",
            api_url="https://dialagram.me/router/v1/chat/completions",
            api_key_env="DIALAGRAM_API_KEY",
            # No alias — uses model name as-is
        )
        router = ModelRouter(providers=[p1, p2], state_dir=str(tmp_path))

        resolved_openai = router.resolve_model("openai", "xiaomi-mimo-2.5")
        assert resolved_openai == "gpt-4o"

        resolved_dialagram = router.resolve_model("dialagram", "xiaomi-mimo-2.5")
        assert resolved_dialagram == "xiaomi-mimo-2.5"
