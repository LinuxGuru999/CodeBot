"""Tests for codebot/discovery_manager.py — adaptive discovery role allocation."""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

# Ensure project root is on sys.path for direct imports
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import codebot.discovery_manager as dm


# ---------------------------------------------------------------------------
# DISCOVERY_ROLES tuple completeness tests
# ---------------------------------------------------------------------------

class TestDiscoveryRoles:
    def test_contains_all_8_roles(self):
        expected = {
            "bug_hunter",
            "security_auditor",
            "test_gap_auditor",
            "architecture_auditor",
            "performance_auditor",
            "documentation_auditor",
            "dependency_auditor",
            "ux_auditor",
        }
        assert set(dm.DISCOVERY_ROLES) == expected

    def test_is_tuple(self):
        assert isinstance(dm.DISCOVERY_ROLES, tuple)

    def test_length_is_8(self):
        assert len(dm.DISCOVERY_ROLES) == 8


# ---------------------------------------------------------------------------
# CHEAP_DISCOVERY_ROLES and PREMIUM_DISCOVERY_ROLES membership tests
# ---------------------------------------------------------------------------

class TestRoleCostClassification:
    def test_cheap_roles_membership(self):
        assert "test_gap_auditor" in dm.CHEAP_DISCOVERY_ROLES
        assert "documentation_auditor" in dm.CHEAP_DISCOVERY_ROLES
        assert "dependency_auditor" in dm.CHEAP_DISCOVERY_ROLES
        assert "bug_hunter" not in dm.CHEAP_DISCOVERY_ROLES
        assert "security_auditor" not in dm.CHEAP_DISCOVERY_ROLES

    def test_premium_roles_membership(self):
        assert "security_auditor" in dm.PREMIUM_DISCOVERY_ROLES
        assert "architecture_auditor" in dm.PREMIUM_DISCOVERY_ROLES
        assert "bug_hunter" not in dm.PREMIUM_DISCOVERY_ROLES
        assert "test_gap_auditor" not in dm.PREMIUM_DISCOVERY_ROLES

    def test_cheap_and_premium_are_frozensets(self):
        assert isinstance(dm.CHEAP_DISCOVERY_ROLES, frozenset)
        assert isinstance(dm.PREMIUM_DISCOVERY_ROLES, frozenset)

    def test_no_overlap_between_cheap_and_premium(self):
        overlap = dm.CHEAP_DISCOVERY_ROLES & dm.PREMIUM_DISCOVERY_ROLES
        assert overlap == frozenset()


# ---------------------------------------------------------------------------
# DiscoveryCooldown tests
# ---------------------------------------------------------------------------

class TestDiscoveryCooldown:
    def _make_cooldown(self, **overrides):
        defaults = dict(
            role="bug_hunter",
            scope="full_project",
            commit_sha="abc123",
            completed_at=1000.0,
            findings_count=5,
            duplicate_count=1,
        )
        defaults.update(overrides)
        return dm.DiscoveryCooldown(**defaults)

    def test_is_expired_true_when_past_cooldown(self):
        cd = self._make_cooldown(completed_at=1000.0)
        assert cd.is_expired(now=2000.0, cooldown_seconds=500) is True

    def test_is_expired_false_when_within_cooldown(self):
        cd = self._make_cooldown(completed_at=1000.0)
        assert cd.is_expired(now=1200.0, cooldown_seconds=500) is False

    def test_is_stale_commit_true_when_different(self):
        cd = self._make_cooldown(commit_sha="abc")
        assert cd.is_stale_commit("def") is True

    def test_is_stale_commit_false_when_same(self):
        cd = self._make_cooldown(commit_sha="abc")
        assert cd.is_stale_commit("abc") is False

    def test_frozen_dataclass(self):
        cd = self._make_cooldown()
        with pytest.raises(AttributeError):
            cd.role = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RoleYieldStats tests
# ---------------------------------------------------------------------------

class TestRoleYieldStats:
    def test_yield_rate_zero_scans(self):
        stats = dm.RoleYieldStats(role="bug_hunter")
        assert stats.yield_rate == 0.0

    def test_yield_rate_computed_correctly(self):
        stats = dm.RoleYieldStats(role="bug_hunter", total_scans=10, validated_tickets=3)
        assert stats.yield_rate == pytest.approx(0.3)

    def test_duplicate_rate_zero_scans(self):
        stats = dm.RoleYieldStats(role="bug_hunter")
        assert stats.duplicate_rate == 0.0

    def test_duplicate_rate_computed(self):
        stats = dm.RoleYieldStats(role="bug_hunter", total_scans=10, duplicates=7)
        assert stats.duplicate_rate == pytest.approx(0.7)

    def test_cost_per_ticket_no_validated(self):
        stats = dm.RoleYieldStats(role="bug_hunter", total_cost_tokens=1000)
        assert stats.cost_per_ticket == float("inf")

    def test_cost_per_ticket_computed(self):
        stats = dm.RoleYieldStats(role="bug_hunter", validated_tickets=5, total_cost_tokens=1000)
        assert stats.cost_per_ticket == 200.0

    def test_record_scan_updates_fields(self):
        stats = dm.RoleYieldStats(role="bug_hunter")
        now = 12345.0
        stats.record_scan(validated=2, duplicates=1, rejected=0, cost_tokens=500, now=now)
        assert stats.total_scans == 1
        assert stats.validated_tickets == 2
        assert stats.duplicates == 1
        assert stats.rejected == 0
        assert stats.total_cost_tokens == 500
        assert stats.last_scan_at == now

    def test_to_dict_structure(self):
        stats = dm.RoleYieldStats(role="tester", total_scans=5, validated_tickets=2)
        d = stats.to_dict()
        assert d["role"] == "tester"
        assert d["total_scans"] == 5
        assert d["validated_tickets"] == 2
        assert "yield_rate" in d
        assert "duplicate_rate" in d
        assert "cost_per_ticket" in d


# ---------------------------------------------------------------------------
# DiscoveryAllocation tests
# ---------------------------------------------------------------------------

class TestDiscoveryAllocation:
    def test_to_list_includes_nonzero_allocations(self):
        alloc = dm.DiscoveryAllocation(
            allocations={"bug_hunter": 3, "security_auditor": 0, "test_gap_auditor": 2},
            total_slots=5,
            reason="test",
        )
        result = alloc.to_list()
        roles = [r["role"] for r in result]
        assert "bug_hunter" in roles
        assert "test_gap_auditor" in roles
        assert "security_auditor" not in roles

    def test_to_list_cost_class_assignment(self):
        alloc = dm.DiscoveryAllocation(
            allocations={"test_gap_auditor": 1, "security_auditor": 1, "bug_hunter": 1},
            total_slots=3,
            reason="test",
        )
        result = {r["role"]: r["cost_class"] for r in alloc.to_list()}
        assert result["test_gap_auditor"] == "cheap"
        assert result["security_auditor"] == "premium"
        assert result["bug_hunter"] == "standard"

    def test_is_saturated_default_false(self):
        alloc = dm.DiscoveryAllocation(allocations={}, total_slots=0, reason="x")
        assert alloc.is_saturated is False


# ---------------------------------------------------------------------------
# DiscoveryManager — cooldown tracking per (role, scope, commit SHA)
# ---------------------------------------------------------------------------

def _make_config(**overrides):
    discovery = SimpleNamespace(
        maximum_fraction=0.5,
        minimum_slots=2,
        max_duplicate_rate_before_throttle=0.5,
        min_yield_to_continue=0.05,
        cooldown_seconds=3600,
    )
    config = SimpleNamespace(max_slots=30, discovery=discovery)
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


class TestDiscoveryManagerCooldown:
    def test_is_on_cooldown_true_within_window(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        mgr.record_completion(
            role="bug_hunter", scope="full_project",
            commit_sha="abc", findings=2, duplicates=0,
            rejected=0, cost_tokens=100, now=1000.0,
        )
        assert mgr.is_on_cooldown("bug_hunter", "full_project", "abc", 3600, now=2000.0) is True

    def test_is_on_cooldown_false_after_expiry(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        mgr.record_completion(
            role="bug_hunter", scope="full_project",
            commit_sha="abc", findings=2, duplicates=0,
            rejected=0, cost_tokens=100, now=1000.0,
        )
        assert mgr.is_on_cooldown("bug_hunter", "full_project", "abc", 3600, now=5000.0) is False

    def test_is_on_cooldown_false_when_commit_changed(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        mgr.record_completion(
            role="bug_hunter", scope="full_project",
            commit_sha="abc", findings=2, duplicates=0,
            rejected=0, cost_tokens=100, now=1000.0,
        )
        assert mgr.is_on_cooldown("bug_hunter", "full_project", "def", 3600, now=1500.0) is False

    def test_is_on_cooldown_false_for_never_scanned(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        assert mgr.is_on_cooldown("bug_hunter", "full_project", "abc", 3600) is False

    def test_cooldown_index_updated_immediately_on_record_completion(self, tmp_path):
        """Verify that record_completion updates the O(1) index immediately.
        
        This ensures new cooldowns are visible to is_on_cooldown without delay.
        """
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        
        # Initially, no entry in index
        assert ("bug_hunter", "full_project") not in mgr._cooldown_index
        
        # Record a completion
        mgr.record_completion(
            role="bug_hunter", scope="full_project",
            commit_sha="abc", findings=2, duplicates=0,
            rejected=0, cost_tokens=100, now=1000.0,
        )
        
        # Index should now contain the entry pointing to the last element
        assert ("bug_hunter", "full_project") in mgr._cooldown_index
        idx = mgr._cooldown_index[("bug_hunter", "full_project")]
        assert idx == len(mgr._cooldowns) - 1  # Points to last element
        assert mgr._cooldowns[idx].commit_sha == "abc"
        
        # is_on_cooldown should immediately see the new cooldown
        assert mgr.is_on_cooldown("bug_hunter", "full_project", "abc", 3600, now=1500.0) is True
        
        # Record another completion for same (role, scope) - index should update
        mgr.record_completion(
            role="bug_hunter", scope="full_project",
            commit_sha="def", findings=3, duplicates=0,
            rejected=0, cost_tokens=150, now=2000.0,
        )
        
        # Index should point to the new last element
        idx = mgr._cooldown_index[("bug_hunter", "full_project")]
        assert idx == len(mgr._cooldowns) - 1
        assert mgr._cooldowns[idx].commit_sha == "def"
        
        # Should now see the new commit SHA
        assert mgr.is_on_cooldown("bug_hunter", "full_project", "def", 3600, now=2500.0) is True
        assert mgr.is_on_cooldown("bug_hunter", "full_project", "abc", 3600, now=2500.0) is False  # Stale commit

    def test_cooldown_index_rebuilt_after_pruning(self, tmp_path):
        """Verify that the index is correctly rebuilt after old cooldowns are pruned."""
        import time as time_module
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        now = time_module.time()
        old_time = now - 31 * 86400  # 31 days ago
        
        # Record an old cooldown
        mgr.record_completion(
            role="bug_hunter", scope="proj",
            commit_sha="old", findings=1, duplicates=0,
            rejected=0, cost_tokens=50, now=old_time,
        )
        old_idx = mgr._cooldown_index[("bug_hunter", "proj")]
        assert old_idx == 0
        
        # Record a recent cooldown (triggers pruning)
        mgr.record_completion(
            role="security_auditor", scope="proj",
            commit_sha="new", findings=1, duplicates=0,
            rejected=0, cost_tokens=50, now=now,
        )
        
        # Old cooldown should be pruned, index rebuilt
        assert len(mgr._cooldowns) == 1
        assert mgr._cooldowns[0].role == "security_auditor"
        assert ("bug_hunter", "proj") not in mgr._cooldown_index  # Pruned from index
        assert mgr._cooldown_index[("security_auditor", "proj")] == 0


# ---------------------------------------------------------------------------
# DiscoveryManager — yield stat decay over time
# ---------------------------------------------------------------------------

class TestDiscoveryManagerYieldDecay:
    def test_get_yield_stats_returns_default_for_new_role(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        stats = mgr.get_yield_stats("ux_auditor")
        assert stats.role == "ux_auditor"
        assert stats.total_scans == 0

    def test_record_completion_updates_yield(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        mgr.record_completion(
            role="bug_hunter", scope="proj", commit_sha="a",
            findings=3, duplicates=1, rejected=0, cost_tokens=200, now=1000.0,
        )
        stats = mgr.get_yield_stats("bug_hunter")
        assert stats.total_scans == 1
        assert stats.validated_tickets == 3
        assert stats.duplicates == 1

    def test_old_cooldowns_pruned_after_30_days(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        now = time.time()
        old_time = now - 31 * 86400
        mgr.record_completion(
            role="bug_hunter", scope="proj", commit_sha="old",
            findings=1, duplicates=0, rejected=0, cost_tokens=50, now=old_time,
        )
        # Record a recent completion to trigger pruning with current 'now'
        mgr.record_completion(
            role="security_auditor", scope="proj", commit_sha="new",
            findings=1, duplicates=0, rejected=0, cost_tokens=50, now=now,
        )
        # Old cooldown should be pruned; only the recent one remains
        assert len(mgr._cooldowns) == 1
        assert mgr._cooldowns[0].role == "security_auditor"


# ---------------------------------------------------------------------------
# DiscoveryManager — weighted round-robin diversity allocation
# ---------------------------------------------------------------------------

class TestDiscoveryManagerAllocation:
    def test_zero_slots_returns_empty(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        config = _make_config()
        alloc = mgr.compute_allocation(0, config)
        assert alloc.total_slots == 0
        assert alloc.allocations == {}

    def test_saturated_project_returns_empty(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        # Simulate saturation: many scans, high dup rate, low yield for all roles
        for role in dm.DISCOVERY_ROLES:
            for i in range(10):
                mgr.record_completion(
                    role=role, scope="proj", commit_sha="same",
                    findings=0, duplicates=5, rejected=0, cost_tokens=100, now=1000.0 + i,
                )
        config = _make_config()
        alloc = mgr.compute_allocation(10, config)
        assert alloc.is_saturated is True
        assert alloc.total_slots == 0

    def test_normal_allocation_distributes_slots(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        config = _make_config()
        alloc = mgr.compute_allocation(10, config)
        assert alloc.total_slots > 0
        assert sum(alloc.allocations.values()) == alloc.total_slots

    def test_allocation_respects_max_discovery_fraction(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        config = _make_config()
        config.max_slots = 20
        config.discovery.maximum_fraction = 0.5
        # max_discovery = int(20 * 0.5) = 10
        alloc = mgr.compute_allocation(100, config)
        assert alloc.total_slots <= 10

    def test_allocation_enforces_minimum_slots(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        config = _make_config()
        config.discovery.minimum_slots = 5
        alloc = mgr.compute_allocation(10, config)
        assert alloc.total_slots >= 5

    def test_high_yield_role_gets_boosted(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        # Give bug_hunter high yield
        for i in range(5):
            mgr.record_completion(
                role="bug_hunter", scope="proj", commit_sha=f"sha{i}",
                findings=5, duplicates=0, rejected=0, cost_tokens=100, now=1000.0 + i,
            )
        config = _make_config()
        alloc = mgr.compute_allocation(20, config)
        # bug_hunter should get a non-trivial share
        assert alloc.allocations.get("bug_hunter", 0) > 0

    def test_project_signals_boost_relevant_role(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        config = _make_config()
        signals = {"test_coverage": 2.0}  # Strong signal for test_gap_auditor
        alloc = mgr.compute_allocation(20, config, project_signals=signals)
        assert alloc.allocations.get("test_gap_auditor", 0) > 0


# ---------------------------------------------------------------------------
# DiscoveryManager — persistence tests
# ---------------------------------------------------------------------------

class TestDiscoveryManagerPersistence:
    def test_save_creates_file(self, tmp_path):
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        mgr.record_completion(
            role="bug_hunter", scope="proj", commit_sha="abc",
            findings=1, duplicates=0, rejected=0, cost_tokens=50, now=1000.0,
        )
        mgr.save()
        assert (tmp_path / "discovery_state.json").exists()

    def test_load_restores_state(self, tmp_path):
        mgr1 = dm.DiscoveryManager(state_dir=tmp_path)
        mgr1.record_completion(
            role="bug_hunter", scope="proj", commit_sha="abc",
            findings=3, duplicates=1, rejected=0, cost_tokens=100, now=1000.0,
        )
        mgr1.save()

        mgr2 = dm.DiscoveryManager(state_dir=tmp_path)
        stats = mgr2.get_yield_stats("bug_hunter")
        assert stats.total_scans == 1
        assert stats.validated_tickets == 3

    def test_no_state_dir_does_not_crash(self):
        mgr = dm.DiscoveryManager(state_dir=None)
        mgr.save()  # Should not raise
        mgr.record_completion(
            role="bug_hunter", scope="proj", commit_sha="abc",
            findings=1, duplicates=0, rejected=0, cost_tokens=50,
        )
        mgr.save()  # Still should not raise

    def test_corrupt_state_file_handled_gracefully(self, tmp_path):
        (tmp_path / "discovery_state.json").write_text("NOT JSON{{{", encoding="utf-8")
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        # Should initialize with empty state
        assert len(mgr._cooldowns) == 0
        assert len(mgr._yields) == 0


# ---------------------------------------------------------------------------
# Accessibility tests for yield statistics rendering
# ---------------------------------------------------------------------------

class TestYieldAccessibility:
    def test_yield_table_uses_scope_attributes(self, tmp_path):
        """Table uses th elements with scope='col'/'row'."""
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        mgr.record_completion(
            role="bug_hunter", scope="proj", commit_sha="abc",
            findings=3, duplicates=1, rejected=0, cost_tokens=100, now=1000.0,
        )
        html_output = mgr.render_yield_table_html()
        assert '<th scope="col"' in html_output
        assert '<th scope="row"' in html_output

    def test_chart_has_aria_label_insight(self, tmp_path):
        """Chart container has aria-label describing insight."""
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        mgr.record_completion(
            role="bug_hunter", scope="proj", commit_sha="abc",
            findings=3, duplicates=1, rejected=0, cost_tokens=100, now=1000.0,
        )
        chart_html = mgr.render_yield_chart_html()
        assert 'role="img"' in chart_html
        assert 'aria-label=' in chart_html
        assert 'Yield trend' in chart_html

    def test_sort_controls_focusable_announce_state(self, tmp_path):
        """Sort controls are focusable and announce state changes (button + aria-sort)."""
        mgr = dm.DiscoveryManager(state_dir=tmp_path)
        mgr.record_completion(
            role="bug_hunter", scope="proj", commit_sha="abc",
            findings=3, duplicates=1, rejected=0, cost_tokens=100, now=1000.0,
        )
        html_output = mgr.render_yield_table_html()
        # Check for button elements with tabindex and aria-sort
        assert '<button' in html_output
        assert 'tabindex="0"' in html_output
        assert 'aria-sort=' in html_output
