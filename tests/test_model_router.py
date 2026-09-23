"""Tests for codebot.model_router module.

Covers ProviderHealth, FallbackChain, and ModelRouter classes with focus on:
- Health tracking transitions and thresholds
- Failover chain behavior with unhealthy providers
- Cheapest selection with and without pricing data
- Stats persistence and corruption handling
- API key resolution from environment variables
"""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from codebot.model_router import (
    FallbackChain,
    ModelRouter,
    ProviderConfig,
    ProviderHealth,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sample_providers():
    """Create a list of sample provider configs for testing."""
    return [
        ProviderConfig(
            name="provider_a",
            api_url="https://a.example.com/v1",
            api_key_env="PROVIDER_A_KEY",
            model_aliases={"canonical": "model-a-v1"},
            cost_per_1k_tokens={"model-a-v1": 0.001},
            is_openai_compatible=True,
        ),
        ProviderConfig(
            name="provider_b",
            api_url="https://b.example.com/v1",
            api_key_env="PROVIDER_B_KEY",
            model_aliases={"canonical": "model-b-v2"},
            cost_per_1k_tokens={"model-b-v2": 0.005},
            is_openai_compatible=True,
        ),
        ProviderConfig(
            name="provider_c",
            api_url="https://c.example.com/v1",
            api_key_env="PROVIDER_C_KEY",
            model_aliases={},
            cost_per_1k_tokens={},
            is_openai_compatible=False,
        ),
    ]


@pytest.fixture
def fallback_chain(sample_providers):
    """Create a FallbackChain with sample providers."""
    return FallbackChain(sample_providers)


@pytest.fixture
def router(tmp_path, sample_providers):
    """Create a ModelRouter with temp state dir."""
    state_dir = str(tmp_path / "state")
    return ModelRouter(providers=sample_providers, state_dir=state_dir)


# ===========================================================================
# ProviderHealth Tests
# ===========================================================================

class TestProviderHealth:
    """Tests for ProviderHealth record_success/failure/reset transitions."""

    def test_initial_state(self):
        health = ProviderHealth("test_provider")
        assert health.provider == "test_provider"
        assert health.total_calls == 0
        assert health.failures == 0
        assert health.consecutive_failures == 0

    def test_record_success_resets_consecutive_failures(self):
        health = ProviderHealth("p")
        health.record_failure()
        health.record_failure()
        assert health.consecutive_failures == 2
        health.record_success()
        assert health.consecutive_failures == 0
        assert health.total_calls == 3
        assert health.failures == 2

    def test_record_failure_increments_all_counters(self):
        health = ProviderHealth("p")
        health.record_failure()
        assert health.total_calls == 1
        assert health.failures == 1
        assert health.consecutive_failures == 1

    def test_reset_clears_all_metrics(self):
        health = ProviderHealth("p")
        health.record_success()
        health.record_failure()
        health.record_failure()
        health.reset()
        assert health.total_calls == 0
        assert health.failures == 0
        assert health.consecutive_failures == 0

    def test_success_rate_no_calls_returns_one(self):
        health = ProviderHealth("p")
        assert health.success_rate == 1.0

    def test_success_rate_calculation(self):
        health = ProviderHealth("p")
        for _ in range(7):
            health.record_success()
        for _ in range(3):
            health.record_failure()
        assert health.success_rate == pytest.approx(0.7)

    def test_is_healthy_default_true(self):
        health = ProviderHealth("p")
        assert health.is_healthy is True

    def test_is_healthy_false_after_3_consecutive_failures(self):
        health = ProviderHealth("p")
        health.record_failure()
        health.record_failure()
        assert health.is_healthy is True
        health.record_failure()
        assert health.is_healthy is False

    def test_is_healthy_recovers_after_success(self):
        health = ProviderHealth("p")
        for _ in range(3):
            health.record_failure()
        assert health.is_healthy is False
        health.record_success()
        assert health.is_healthy is True

    def test_is_healthy_false_low_success_rate_with_enough_calls(self):
        health = ProviderHealth("p")
        # 4 successes + 6 failures = 10 calls, 40% success rate
        for _ in range(4):
            health.record_success()
        for _ in range(6):
            health.record_failure()
        assert health.total_calls == 10
        assert health.success_rate < 0.5
        # consecutive_failures is 6 >= 3, so already unhealthy
        # Reset consecutive to test the rate-based check specifically
        health.consecutive_failures = 0
        assert health.is_healthy is False

    def test_is_healthy_true_below_10_calls_even_with_low_rate(self):
        health = ProviderHealth("p")
        # 1 success + 8 failures = 9 calls, ~11% success rate but < 10 calls
        health.record_success()
        for _ in range(8):
            health.record_failure()
        assert health.total_calls == 9
        health.consecutive_failures = 0  # Remove consecutive trigger
        assert health.is_healthy is True

    def test_is_healthy_true_at_exactly_50_percent(self):
        health = ProviderHealth("p")
        for _ in range(5):
            health.record_success()
        for _ in range(5):
            health.record_failure()
        health.consecutive_failures = 0
        assert health.total_calls == 10
        assert health.success_rate == 0.5
        assert health.is_healthy is True  # < 0.5 is unhealthy, exactly 0.5 is ok


# ===========================================================================
# FallbackChain Tests
# ===========================================================================

class TestFallbackChain:
    """Tests for FallbackChain healthy_providers filtering and model resolution."""

    def test_healthy_providers_returns_all_when_healthy(self, fallback_chain):
        healthy = fallback_chain.healthy_providers()
        assert len(healthy) == 3
        assert [p.name for p in healthy] == ["provider_a", "provider_b", "provider_c"]

    def test_healthy_providers_filters_unhealthy(self, fallback_chain):
        # Make provider_a unhealthy
        for _ in range(3):
            fallback_chain._health["provider_a"].record_failure()
        healthy = fallback_chain.healthy_providers()
        assert len(healthy) == 2
        assert healthy[0].name == "provider_b"
        assert healthy[1].name == "provider_c"

    def test_healthy_providers_empty_when_all_unhealthy(self, fallback_chain):
        for name in ["provider_a", "provider_b", "provider_c"]:
            for _ in range(3):
                fallback_chain._health[name].record_failure()
        assert fallback_chain.healthy_providers() == []

    def test_get_provider_found(self, fallback_chain):
        p = fallback_chain.get_provider("provider_b")
        assert p is not None
        assert p.name == "provider_b"

    def test_get_provider_not_found(self, fallback_chain):
        assert fallback_chain.get_provider("nonexistent") is None

    def test_resolve_model_with_alias(self, fallback_chain):
        result = fallback_chain.resolve_model("provider_a", "canonical")
        assert result == "model-a-v1"

    def test_resolve_model_without_alias_returns_original(self, fallback_chain):
        result = fallback_chain.resolve_model("provider_a", "unknown-model")
        assert result == "unknown-model"

    def test_resolve_model_unknown_provider_returns_original(self, fallback_chain):
        result = fallback_chain.resolve_model("nonexistent", "some-model")
        assert result == "some-model"

    def test_select_cheapest_picks_lowest_cost(self, fallback_chain):
        # Both provider_a and provider_b have aliases for "canonical"
        # provider_a: model-a-v1 at 0.001, provider_b: model-b-v2 at 0.005
        result = fallback_chain.select_cheapest("canonical")
        assert result is not None
        assert result.name == "provider_a"

    def test_select_cheapest_returns_none_when_all_unhealthy(self, fallback_chain):
        for name in ["provider_a", "provider_b", "provider_c"]:
            for _ in range(3):
                fallback_chain._health[name].record_failure()
        assert fallback_chain.select_cheapest("canonical") is None

    def test_select_cheapest_missing_pricing_uses_sentinel(self, fallback_chain):
        # provider_c has no pricing for any model, gets sentinel 999999.0
        # Request a model that only provider_c doesn't have pricing for
        # but provider_a does via alias
        result = fallback_chain.select_cheapest("canonical")
        assert result is not None
        assert result.name == "provider_a"  # 0.001 < 999999.0

    def test_select_cheapest_all_missing_pricing_returns_first(self, fallback_chain):
        # Use a model none have pricing for - all get 999999.0 sentinel
        # First one encountered wins due to strict < comparison
        result = fallback_chain.select_cheapest("no-pricing-model")
        assert result is not None
        assert result.name == "provider_a"  # First with equal sentinel cost

    def test_select_cheapest_skips_unhealthy(self, fallback_chain):
        # Make cheapest (provider_a) unhealthy
        for _ in range(3):
            fallback_chain._health["provider_a"].record_failure()
        result = fallback_chain.select_cheapest("canonical")
        assert result is not None
        assert result.name == "provider_b"  # Next cheapest healthy


# ===========================================================================
# ModelRouter Tests
# ===========================================================================

class TestModelRouter:
    """Tests for ModelRouter failover, persistence, and API key resolution."""

    def test_init_with_custom_providers(self, tmp_path, sample_providers):
        router = ModelRouter(
            providers=sample_providers,
            state_dir=str(tmp_path / "state"),
        )
        assert len(router.chain.providers) == 3

    def test_init_default_chain(self, tmp_path):
        router = ModelRouter(state_dir=str(tmp_path / "state"))
        assert len(router.chain.providers) > 0

    def test_get_active_provider_preferred_healthy(self, router):
        result = router.get_active_provider("provider_b")
        assert result is not None
        assert result.name == "provider_b"

    def test_get_active_provider_failover_to_healthy(self, router):
        # Make preferred unhealthy
        for _ in range(3):
            router.chain._health["provider_a"].record_failure()
        result = router.get_active_provider("provider_a")
        assert result is not None
        assert result.name == "provider_b"  # First healthy in chain

    def test_get_active_provider_all_unhealthy_returns_first(self, router):
        for name in ["provider_a", "provider_b", "provider_c"]:
            for _ in range(3):
                router.chain._health[name].record_failure()
        result = router.get_active_provider("provider_a")
        assert result is not None
        assert result.name == "provider_a"  # Last resort

    def test_get_active_provider_unknown_preferred_falls_through(self, router):
        result = router.get_active_provider("nonexistent")
        assert result is not None
        assert result.name == "provider_a"  # First healthy

    def test_get_api_url_known_provider(self, router):
        assert router.get_api_url("provider_a") == "https://a.example.com/v1"

    def test_get_api_url_unknown_returns_first(self, router):
        assert router.get_api_url("nonexistent") == "https://a.example.com/v1"

    def test_get_api_url_empty_chain(self, tmp_path):
        router = ModelRouter(providers=[], state_dir=str(tmp_path / "state"))
        assert router.get_api_url("any") is None

    def test_get_api_key_from_env(self, router):
        with patch.dict(os.environ, {"PROVIDER_A_KEY": "secret-key-123"}):
            assert router.get_api_key("provider_a") == "secret-key-123"

    def test_get_api_key_strips_whitespace(self, router):
        with patch.dict(os.environ, {"PROVIDER_A_KEY": "  key-with-spaces  "}):
            assert router.get_api_key("provider_a") == "key-with-spaces"

    def test_get_api_key_empty_string_returns_none(self, router):
        with patch.dict(os.environ, {"PROVIDER_A_KEY": ""}):
            assert router.get_api_key("provider_a") is None

    def test_get_api_key_whitespace_only_returns_none(self, router):
        with patch.dict(os.environ, {"PROVIDER_A_KEY": "   "}):
            assert router.get_api_key("provider_a") is None

    def test_get_api_key_missing_env_returns_none(self, router):
        with patch.dict(os.environ, {}, clear=True):
            assert router.get_api_key("provider_a") is None

    def test_get_api_key_unknown_provider_returns_none(self, router):
        assert router.get_api_key("nonexistent") is None

    def test_record_success_updates_health(self, router):
        router.record_success("provider_a", "model-x")
        stats = router.get_stats("provider_a")
        assert stats["total_calls"] == 1
        assert stats["failures"] == 0

    def test_record_failure_updates_health(self, router):
        router.record_failure("provider_a", "model-x")
        stats = router.get_stats("provider_a")
        assert stats["total_calls"] == 1
        assert stats["failures"] == 1

    def test_record_success_unknown_provider_noop(self, router):
        # Should not raise
        router.record_success("nonexistent", "model-x")

    def test_record_failure_unknown_provider_noop(self, router):
        router.record_failure("nonexistent", "model-x")

    def test_get_stats_unknown_provider(self, router):
        stats = router.get_stats("nonexistent")
        assert stats["total_calls"] == 0
        assert stats["success_rate"] == 1.0

    def test_get_stats_known_provider(self, router):
        router.record_success("provider_a", "m")
        router.record_failure("provider_a", "m")
        stats = router.get_stats("provider_a")
        assert stats["total_calls"] == 2
        assert stats["failures"] == 1
        assert stats["success_rate"] == pytest.approx(0.5)
        assert "is_healthy" in stats
        assert "consecutive_failures" in stats

    def test_select_cheapest_provider_delegates_to_chain(self, router):
        result = router.select_cheapest_provider("canonical")
        assert result is not None
        assert result.name == "provider_a"

    def test_is_openai_compatible_known(self, router):
        assert router.is_openai_compatible("provider_a") is True
        assert router.is_openai_compatible("provider_c") is False

    def test_is_openai_compatible_unknown_defaults_true(self, router):
        assert router.is_openai_compatible("nonexistent") is True

    def test_resolve_model_delegates(self, router):
        assert router.resolve_model("provider_a", "canonical") == "model-a-v1"

    def test_get_model_aliases_known(self, router):
        aliases = router.get_model_aliases("provider_a")
        assert aliases == {"canonical": "model-a-v1"}

    def test_get_model_aliases_unknown(self, router):
        assert router.get_model_aliases("nonexistent") == {}


# ===========================================================================
# Persistence Tests (_load_stats / _save_stats)
# ===========================================================================

class TestPersistence:
    """Tests for stats loading, saving, and corruption handling."""

    def test_save_and_load_stats(self, tmp_path, sample_providers):
        state_dir = str(tmp_path / "state")
        router1 = ModelRouter(providers=sample_providers, state_dir=state_dir)
        router1.record_success("provider_a", "m")
        router1.record_failure("provider_b", "m")
        router1.record_failure("provider_b", "m")

        # Create new router that should load persisted stats
        router2 = ModelRouter(providers=sample_providers, state_dir=state_dir)
        stats_a = router2.get_stats("provider_a")
        stats_b = router2.get_stats("provider_b")
        assert stats_a["total_calls"] == 1
        assert stats_a["failures"] == 0
        assert stats_b["total_calls"] == 2
        assert stats_b["failures"] == 2
        assert stats_b["consecutive_failures"] == 2

    def test_load_stats_corrupt_json(self, tmp_path, sample_providers):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        stats_file = state_dir / "model_router_stats.json"
        stats_file.write_text("{corrupt json!!!", encoding="utf-8")

        # Should not raise, starts fresh
        router = ModelRouter(
            providers=sample_providers,
            state_dir=str(state_dir),
        )
        stats = router.get_stats("provider_a")
        assert stats["total_calls"] == 0

    def test_load_stats_nonexistent_file(self, tmp_path, sample_providers):
        state_dir = str(tmp_path / "state_new")
        # Should not raise
        router = ModelRouter(providers=sample_providers, state_dir=state_dir)
        assert router.get_stats("provider_a")["total_calls"] == 0

    def test_load_stats_ignores_unknown_providers(self, tmp_path, sample_providers):
        state_dir = tmp_path / "state"
        state_dir.mkdir(parents=True)
        stats_file = state_dir / "model_router_stats.json"
        data = {
            "provider_a": {"total_calls": 5, "failures": 1, "consecutive_failures": 0},
            "ghost_provider": {"total_calls": 99, "failures": 99, "consecutive_failures": 99},
        }
        stats_file.write_text(json.dumps(data), encoding="utf-8")

        router = ModelRouter(providers=sample_providers, state_dir=str(state_dir))
        assert router.get_stats("provider_a")["total_calls"] == 5
        assert router.get_stats("ghost_provider")["total_calls"] == 0

    def test_save_stats_atomic_write(self, tmp_path, sample_providers):
        state_dir = str(tmp_path / "state")
        router = ModelRouter(providers=sample_providers, state_dir=state_dir)
        router.record_success("provider_a", "m")

        stats_file = Path(state_dir) / "model_router_stats.json"
        tmp_file = stats_file.with_suffix(".tmp")

        # After save, tmp file should not exist (replaced atomically)
        assert stats_file.exists()
        assert not tmp_file.exists()

        # Verify content is valid JSON
        data = json.loads(stats_file.read_text(encoding="utf-8"))
        assert data["provider_a"]["total_calls"] == 1

    def test_save_stats_creates_parent_dirs(self, tmp_path, sample_providers):
        deep_dir = str(tmp_path / "a" / "b" / "c" / "state")
        router = ModelRouter(providers=sample_providers, state_dir=deep_dir)
        router.record_success("provider_a", "m")
        stats_file = Path(deep_dir) / "model_router_stats.json"
        assert stats_file.exists()

    def test_record_success_persists(self, tmp_path, sample_providers):
        state_dir = str(tmp_path / "state")
        router = ModelRouter(providers=sample_providers, state_dir=state_dir)
        router.record_success("provider_a", "m")

        # Reload and verify
        router2 = ModelRouter(providers=sample_providers, state_dir=state_dir)
        assert router2.get_stats("provider_a")["total_calls"] == 1

    def test_record_failure_persists(self, tmp_path, sample_providers):
        state_dir = str(tmp_path / "state")
        router = ModelRouter(providers=sample_providers, state_dir=state_dir)
        router.record_failure("provider_b", "m")

        router2 = ModelRouter(providers=sample_providers, state_dir=state_dir)
        stats = router2.get_stats("provider_b")
        assert stats["failures"] == 1
        assert stats["consecutive_failures"] == 1


# ===========================================================================
# Integration: Failover Chain
# ===========================================================================

class TestFailoverIntegration:
    """End-to-end failover scenarios."""

    def test_full_failover_chain(self, router):
        # Start with provider_a preferred
        assert router.get_active_provider("provider_a").name == "provider_a"

        # Degrade provider_a
        for _ in range(3):
            router.record_failure("provider_a", "m")
        assert router.get_active_provider("provider_a").name == "provider_b"

        # Degrade provider_b
        for _ in range(3):
            router.record_failure("provider_b", "m")
        assert router.get_active_provider("provider_a").name == "provider_c"

        # Degrade provider_c - all unhealthy, last resort returns first
        for _ in range(3):
            router.record_failure("provider_c", "m")
        assert router.get_active_provider("provider_a").name == "provider_a"

    def test_recovery_restores_preferred(self, router):
        for _ in range(3):
            router.record_failure("provider_a", "m")
        assert router.get_active_provider("provider_a").name == "provider_b"

        # Recover provider_a
        router.record_success("provider_a", "m")
        assert router.get_active_provider("provider_a").name == "provider_a"