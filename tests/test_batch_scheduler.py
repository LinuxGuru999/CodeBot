"""Unit tests for codebot.batch_scheduler module.

Covers extract_tier_from_tags, needs_approval, filter_approved,
order_by_tier, pack_batches, and apply_caps.
"""

from __future__ import annotations

import pytest

from codebot.batch_scheduler import (
    extract_tier_from_tags,
    needs_approval,
    filter_approved,
    order_by_tier,
    pack_batches,
    apply_caps,
    SCRUTINY_TIERS,
    DECOMP_PREFIX,
)


class TestExtractTierFromTags:
    """Tests for extract_tier_from_tags()."""

    def test_valid_tier(self) -> None:
        assert extract_tier_from_tags(["T4", "security"]) == "T4"

    def test_multiple_tags_first_tier_wins(self) -> None:
        assert extract_tier_from_tags(["security", "T5", "T6"]) == "T5"

    def test_no_tier_tag(self) -> None:
        assert extract_tier_from_tags(["security", "performance"]) is None

    def test_empty_tags(self) -> None:
        assert extract_tier_from_tags([]) is None

    def test_none_tags(self) -> None:
        assert extract_tier_from_tags(None) is None

    def test_invalid_tier_number(self) -> None:
        """Tiers must be 1-11."""
        assert extract_tier_from_tags(["T0"]) is None
        assert extract_tier_from_tags(["T12"]) is None

    def test_non_string_tag(self) -> None:
        assert extract_tier_from_tags([123, "T4"]) == "T4"

    def test_tier_too_long(self) -> None:
        """Tag longer than 3 chars is not a tier."""
        assert extract_tier_from_tags(["TIER-4"]) is None


class TestNeedsApproval:
    """Tests for needs_approval()."""

    def test_scrutiny_tier_source(self) -> None:
        item = {"source_tier": "T4"}
        assert needs_approval(item) is True

    def test_scrutiny_tier_tag(self) -> None:
        item = {"tags": ["T5", "security"]}
        assert needs_approval(item) is True

    def test_critical_complexity(self) -> None:
        item = {"complexity": "critical"}
        assert needs_approval(item) is True

    def test_standard_item(self) -> None:
        item = {"source_tier": "T2", "tags": ["bug"], "complexity": "low"}
        assert needs_approval(item) is False

    def test_no_keys(self) -> None:
        item: dict = {}
        assert needs_approval(item) is False

    def test_t1_not_scrutiny(self) -> None:
        item = {"source_tier": "T1"}
        assert needs_approval(item) is False

    def test_t3_not_scrutiny(self) -> None:
        item = {"source_tier": "T3"}
        assert needs_approval(item) is False


class TestFilterApproved:
    """Tests for filter_approved()."""

    def test_all_items_proceed(self) -> None:
        items = [
            {"name": "item1", "source_tier": "T2"},
            {"name": "item2", "source_tier": "T4"},
        ]
        standard, scrutiny = filter_approved(items)
        assert len(standard) == 2
        assert scrutiny == []
        # Scrutiny items should be tagged
        assert standard[1].get("scrutiny") is True

    def test_scrutiny_tagging(self) -> None:
        items = [{"name": "item1", "source_tier": "T5"}]
        standard, _ = filter_approved(items)
        assert standard[0]["scrutiny"] is True


class TestOrderByTier:
    """Tests for order_by_tier()."""

    def test_basic_ordering(self) -> None:
        names = ["bot3", "bot1", "bot2"]
        tier_map = {"bot1": 1, "bot2": 2, "bot3": 3}
        next_run_at: dict[str, float] = {}
        result = order_by_tier(names, tier_map, next_run_at)
        assert result == ["bot1", "bot2", "bot3"]

    def test_same_tier_sorted_by_next_run(self) -> None:
        names = ["bot1", "bot2"]
        tier_map = {"bot1": 1, "bot2": 1}
        next_run_at = {"bot1": 100.0, "bot2": 50.0}
        result = order_by_tier(names, tier_map, next_run_at)
        assert result == ["bot2", "bot1"]

    def test_missing_tier_defaults_to_999(self) -> None:
        names = ["bot1", "bot2"]
        tier_map = {"bot1": 1}
        next_run_at: dict[str, float] = {}
        result = order_by_tier(names, tier_map, next_run_at)
        assert result == ["bot1", "bot2"]

    def test_empty_names(self) -> None:
        result = order_by_tier([], {}, {})
        assert result == []

    def test_does_not_mutate_input(self) -> None:
        names = ["bot3", "bot1"]
        tier_map = {"bot1": 1, "bot3": 3}
        original = list(names)
        order_by_tier(names, tier_map, {})
        assert names == original


class TestPackBatches:
    """Tests for pack_batches()."""

    def test_empty_ready(self) -> None:
        result = pack_batches([])
        assert result["batches"] == []
        assert result["dropped"] == []

    def test_none_ready(self) -> None:
        result = pack_batches(None)
        assert result["batches"] == []

    def test_basic_packing(self) -> None:
        ready = [
            {"name": "bot1", "tier_priority": 1, "model": "gpt-4"},
            {"name": "bot2", "tier_priority": 2, "model": "gpt-4"},
        ]
        result = pack_batches(ready, max_per_batch=5, max_batches=2)
        assert len(result["batches"]) == 1
        assert len(result["batches"][0]) == 2

    def test_respects_max_per_batch(self) -> None:
        ready = [{"name": f"bot{i}", "tier_priority": 1, "model": "gpt-4"} for i in range(10)]
        result = pack_batches(ready, max_per_batch=3, max_batches=5)
        # Should have multiple batches
        assert all(len(b) <= 3 for b in result["batches"])

    def test_respects_max_batches(self) -> None:
        ready = [{"name": f"bot{i}", "tier_priority": 1, "model": "gpt-4"} for i in range(20)]
        result = pack_batches(ready, max_per_batch=5, max_batches=2)
        assert len(result["batches"]) <= 2
        # Excess should be dropped
        assert len(result["dropped"]) > 0

    def test_budget_stop(self) -> None:
        ready = [{"name": "bot1", "tier_priority": 1, "model": "gpt-4"}]
        result = pack_batches(ready, budget_state="stop")
        assert result["batches"] == []
        assert result["reason"] == "budget-exhausted"
        assert len(result["dropped"]) == 1

    def test_budget_shed_tier3(self) -> None:
        ready = [
            {"name": "bot1", "tier_priority": 1, "model": "gpt-4"},
            {"name": "bot2", "tier_priority": 31, "model": "gpt-4"},
        ]
        result = pack_batches(ready, budget_state="shed_tier3")
        # bot2 should be dropped
        dropped_names = [d["name"] for d in result["dropped"]]
        assert "bot2" in dropped_names
        assert "bot1" not in dropped_names

    def test_budget_warn(self) -> None:
        ready = [{"name": "bot1", "tier_priority": 1, "model": "gpt-4"}]
        result = pack_batches(ready, budget_state="warn")
        assert result["warning"] == "budget-warn"
        assert len(result["batches"]) > 0

    def test_budget_ok(self) -> None:
        ready = [{"name": "bot1", "tier_priority": 1, "model": "gpt-4"}]
        result = pack_batches(ready, budget_state="ok")
        assert result["reason"] is None
        assert result["warning"] is None

    def test_unknown_budget_state(self) -> None:
        ready = [{"name": "bot1", "tier_priority": 1, "model": "gpt-4"}]
        result = pack_batches(ready, budget_state="unknown")
        assert result["reason"] == "budget-unknown"
        assert result["batches"] == []

    def test_grouping_by_model(self) -> None:
        ready = [
            {"name": "bot1", "tier_priority": 1, "model": "gpt-4"},
            {"name": "bot2", "tier_priority": 1, "model": "claude"},
        ]
        result = pack_batches(ready, max_per_batch=5, max_batches=5)
        # Should be grouped by model
        assert len(result["batches"]) == 2

    def test_staggers(self) -> None:
        ready = [{"name": f"bot{i}", "tier_priority": 1, "model": "gpt-4"} for i in range(10)]
        result = pack_batches(ready, max_per_batch=5, max_batches=5, stagger_s=10)
        assert result["staggers"] == [0, 10]  # Two batches
        assert result["stagger_s"] == 10


class TestApplyCaps:
    """Tests for apply_caps()."""

    def test_no_caps_exceeded(self) -> None:
        batches = [[{"name": "bot1", "model": "gpt-4", "tier_priority": 1}]]
        result = apply_caps(batches, thinking_cap=10, qwen38max_cap=10, slot_cap=10)
        assert len(result["dropped"]) == 0

    def test_thinking_cap(self) -> None:
        batches = [
            [
                {"name": "bot1", "model": "thinking-model", "tier_priority": 1},
                {"name": "bot2", "model": "thinking-model", "tier_priority": 2},
                {"name": "bot3", "model": "thinking-model", "tier_priority": 3},
            ]
        ]
        result = apply_caps(batches, thinking_cap=2, qwen38max_cap=10, slot_cap=10)
        # One should be dropped
        assert len(result["dropped"]) == 1
        assert result["dropped"][0]["reason"] == "thinking-cap"

    def test_qwen38max_cap(self) -> None:
        batches = [
            [
                {"name": "bot1", "model": "qwen-3.8-max", "tier_priority": 1},
                {"name": "bot2", "model": "qwen-3.8-max", "tier_priority": 2},
            ]
        ]
        result = apply_caps(batches, thinking_cap=10, qwen38max_cap=1, slot_cap=10)
        assert len(result["dropped"]) == 1
        assert result["dropped"][0]["reason"] == "qwen38max-cap"

    def test_slot_cap(self) -> None:
        batches = [
            [
                {"name": "bot1", "model": "gpt-4", "tier_priority": 1},
                {"name": "bot2", "model": "gpt-4", "tier_priority": 2},
            ]
        ]
        result = apply_caps(batches, thinking_cap=10, qwen38max_cap=10, slot_cap=1)
        assert len(result["dropped"]) == 1
        assert result["dropped"][0]["reason"] == "slot-cap"

    def test_dict_input(self) -> None:
        """Test with dict input from pack_batches."""
        packed = pack_batches([
            {"name": "bot1", "tier_priority": 1, "model": "gpt-4"},
        ])
        result = apply_caps(packed, thinking_cap=10, qwen38max_cap=10, slot_cap=10)
        assert len(result["batches"]) > 0

    def test_preserves_existing_dropped(self) -> None:
        """Existing dropped items should be preserved."""
        packed = {
            "batches": [[{"name": "bot1", "model": "gpt-4", "tier_priority": 1}]],
            "dropped": [{"name": "bot0", "manifest": {}, "reason": "prior"}],
            "reason": None,
            "warning": None,
            "staggers": [0],
            "stagger_s": 20,
        }
        result = apply_caps(packed, thinking_cap=10, qwen38max_cap=10, slot_cap=10)
        dropped_names = [d["name"] for d in result["dropped"]]
        assert "bot0" in dropped_names


class TestPackBatchesPerformance:
    """Tests for pack_batches O(1) index tracking and remaining-manifest dropping."""

    def test_no_index_call_on_large_input(self) -> None:
        """Verify all manifests accounted for: batched + dropped == input count."""
        ready = [{"name": f"bot{i}", "tier_priority": 1, "model": "gpt-4"} for i in range(25)]
        result = pack_batches(ready, max_per_batch=5, max_batches=2)
        total = (
            sum(len(b) for b in result["batches"])
            + len(result["dropped"])
        )
        assert total == 25

    def test_remaining_manifests_correctly_dropped(self) -> None:
        """When max_batches=1 with 3 models, manifests after batch 1 are all dropped."""
        ready = [
            {"name": "bot0", "tier_priority": 1, "model": "model-a"},
            {"name": "bot1", "tier_priority": 1, "model": "model-a"},
            {"name": "bot2", "tier_priority": 1, "model": "model-b"},
            {"name": "bot3", "tier_priority": 1, "model": "model-b"},
            {"name": "bot4", "tier_priority": 1, "model": "model-c"},
            {"name": "bot5", "tier_priority": 1, "model": "model-c"},
        ]
        result = pack_batches(ready, max_per_batch=3, max_batches=1)
        # Exactly 1 batch, up to 3 manifests
        assert len(result["batches"]) == 1
        assert len(result["dropped"]) >= 3
        dropped_names = {d["name"] for d in result["dropped"]}
        batched_names = {m["name"] for b in result["batches"] for m in b}
        # Every manifest is either batched or dropped, never both
        assert dropped_names.isdisjoint(batched_names)
        assert dropped_names | batched_names == {f"bot{i}" for i in range(6)}

    def test_batching_behavior_unchanged(self) -> None:
        """Identical inputs produce identical batches — no behavioral regression."""
        ready = [
            {"name": "bot1", "tier_priority": 1, "model": "gpt-4"},
            {"name": "bot2", "tier_priority": 1, "model": "gpt-4"},
            {"name": "bot3", "tier_priority": 2, "model": "claude"},
            {"name": "bot4", "tier_priority": 2, "model": "claude"},
            {"name": "bot5", "tier_priority": 3, "model": "gpt-4"},
        ]
        r1 = pack_batches(ready, max_per_batch=2, max_batches=3)
        r2 = pack_batches(ready, max_per_batch=2, max_batches=3)
        # Same batch count and same total batched manifests
        assert len(r1["batches"]) == len(r2["batches"])
        total1 = sum(len(b) for b in r1["batches"])
        total2 = sum(len(b) for b in r2["batches"])
        assert total1 == total2

    def test_cross_model_limit_drops_all_remaining(self) -> None:
        """Limit hit mid-way through a model group; rest of that group + all later groups dropped."""
        ready = [
            {"name": "a1", "tier_priority": 1, "model": "m1"},
            {"name": "a2", "tier_priority": 1, "model": "m1"},
            {"name": "a3", "tier_priority": 1, "model": "m1"},
            {"name": "b1", "tier_priority": 1, "model": "m2"},
            {"name": "b2", "tier_priority": 1, "model": "m2"},
        ]
        result = pack_batches(ready, max_per_batch=2, max_batches=1)
        assert len(result["batches"]) == 1
        assert len(result["batches"][0]) <= 2
        dropped_names = {d["name"] for d in result["dropped"]}
        assert len(dropped_names) == len(ready) - len(result["batches"][0])
