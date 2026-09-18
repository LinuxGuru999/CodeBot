"""Tests for pricing_table.py monetary cost calculation."""
import pytest
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.pricing_table import (
    ModelPricing,
    DEFAULT_PRICING,
    get_pricing,
    calculate_cost_usd,
    calculate_ticket_cost_usd,
    list_providers,
    list_models_for_provider,
)


class TestGetPricing:
    def test_known_model_returns_pricing(self):
        result = get_pricing("gpt-4o")
        assert result is not None
        assert result.prompt_usd_per_1m == 2.50
        assert result.completion_usd_per_1m == 10.00
        assert result.provider == "openai"

    def test_unknown_model_returns_none(self):
        result = get_pricing("nonexistent-model-xyz")
        assert result is None

    def test_case_insensitive_lookup(self):
        result = get_pricing("GPT-4O")
        assert result is not None
        assert result.provider == "openai"

    def test_whitespace_stripped(self):
        result = get_pricing("  gpt-4o  ")
        assert result is not None

    def test_custom_pricing_table(self):
        custom = {"my-model": ModelPricing(prompt_usd_per_1m=1.0, completion_usd_per_1m=2.0, provider="custom")}
        result = get_pricing("my-model", pricing_table=custom)
        assert result is not None
        assert result.provider == "custom"

    def test_custom_table_does_not_include_defaults(self):
        custom = {"my-model": ModelPricing(prompt_usd_per_1m=1.0, completion_usd_per_1m=2.0, provider="custom")}
        result = get_pricing("gpt-4o", pricing_table=custom)
        assert result is None


class TestCalculateCostUsd:
    def test_basic_calculation(self):
        # gpt-4o: $2.50/1M prompt, $10.00/1M completion
        cost = calculate_cost_usd("gpt-4o", 1_000_000, 1_000_000)
        assert cost == pytest.approx(12.50, rel=1e-6)

    def test_zero_tokens(self):
        cost = calculate_cost_usd("gpt-4o", 0, 0)
        assert cost == 0.0

    def test_partial_million_tokens(self):
        # 500k prompt tokens at $2.50/1M = $1.25
        cost = calculate_cost_usd("gpt-4o", 500_000, 0)
        assert cost == pytest.approx(1.25, rel=1e-6)

    def test_unknown_model_returns_zero(self):
        cost = calculate_cost_usd("unknown-model", 1000, 500)
        assert cost == 0.0

    def test_negative_tokens_clamped_to_zero(self):
        cost = calculate_cost_usd("gpt-4o", -100, -50)
        assert cost == 0.0

    def test_qwen_model_pricing(self):
        cost = calculate_cost_usd("qwen-3.8-max", 1_000_000, 1_000_000)
        assert cost == pytest.approx(6.00, rel=1e-6)

    def test_anthropic_model_pricing(self):
        cost = calculate_cost_usd("claude-sonnet-4", 1_000_000, 1_000_000)
        assert cost == pytest.approx(18.00, rel=1e-6)


class TestCalculateTicketCostUsd:
    def test_ticket_cost_from_totals(self):
        totals = {"prompt_tokens": 2_000_000, "completion_tokens": 1_000_000}
        cost = calculate_ticket_cost_usd(totals, "gpt-4o")
        # 2M * $2.50/1M + 1M * $10.00/1M = $5.00 + $10.00 = $15.00
        assert cost == pytest.approx(15.00, rel=1e-6)

    def test_missing_keys_default_to_zero(self):
        totals = {}
        cost = calculate_ticket_cost_usd(totals, "gpt-4o")
        assert cost == 0.0

    def test_unknown_model_returns_zero(self):
        totals = {"prompt_tokens": 1000, "completion_tokens": 500}
        cost = calculate_ticket_cost_usd(totals, "nonexistent")
        assert cost == 0.0


class TestListProviders:
    def test_returns_sorted_unique_providers(self):
        providers = list_providers()
        assert isinstance(providers, list)
        assert len(providers) > 0
        assert providers == sorted(providers)
        assert "openai" in providers
        assert "anthropic" in providers

    def test_no_duplicates(self):
        providers = list_providers()
        assert len(providers) == len(set(providers))

    def test_custom_table(self):
        custom = {
            "m1": ModelPricing(1.0, 2.0, "zeta"),
            "m2": ModelPricing(1.0, 2.0, "alpha"),
        }
        providers = list_providers(custom)
        assert providers == ["alpha", "zeta"]


class TestListModelsForProvider:
    def test_openai_models(self):
        models = list_models_for_provider("openai")
        assert "gpt-4o" in models
        assert models == sorted(models)

    def test_nonexistent_provider_returns_empty(self):
        models = list_models_for_provider("nonexistent")
        assert models == []

    def test_custom_table(self):
        custom = {
            "m1": ModelPricing(1.0, 2.0, "prov-a"),
            "m2": ModelPricing(1.0, 2.0, "prov-b"),
        }
        models = list_models_for_provider("prov-a", custom)
        assert models == ["m1"]


class TestDefaultPricingIntegrity:
    def test_all_entries_have_positive_prices(self):
        for model, pricing in DEFAULT_PRICING.items():
            assert pricing.prompt_usd_per_1m >= 0, f"{model} has negative prompt price"
            assert pricing.completion_usd_per_1m >= 0, f"{model} has negative completion price"

    def test_all_entries_have_provider(self):
        for model, pricing in DEFAULT_PRICING.items():
            assert pricing.provider, f"{model} missing provider"

    def test_model_pricing_is_frozen(self):
        entry = DEFAULT_PRICING["gpt-4o"]
        with pytest.raises(AttributeError):
            entry.prompt_usd_per_1m = 999.0  # type: ignore[misc]
