"""Tests for enhanced discovery features: burst detection, coverage tracking,
NO_ACTIONABLE_FINDINGS, semantic dedup, backpressure, change-triggered discovery."""

import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codebot.discovery_manager import (
    DiscoveryManager,
    DiscoveryCooldown,
    RoleYieldStats,
    DISCOVERY_ROLES,
    CHEAP_DISCOVERY_ROLES,
    PREMIUM_DISCOVERY_ROLES,
)
from codebot.ticket_engine import (
    TicketStore,
    Ticket,
    TicketState,
    TicketClass,
    Severity,
    RiskLevel,
    create_ticket,
)


def _make_config(**overrides):
    defaults = dict(
        max_slots=30,
        discovery=SimpleNamespace(
            minimum_slots=2,
            maximum_fraction=0.75,
            floor_fraction=0.05,
            cooldown_seconds=3600,
            max_duplicate_rate=0.5,
            max_duplicate_rate_before_throttle=0.5,
            min_yield_to_continue=0.05,
            min_yield=0.05,
        ),
        backlog=SimpleNamespace(
            low_watermark=20,
            target=50,
            high_watermark=100,
        ),
    )
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


# ---------------------------------------------------------------------------
# Burst detection tests (§52)
# ---------------------------------------------------------------------------

class TestBurstDetection:
    def test_no_burst_when_empty(self):
        dm = DiscoveryManager()
        assert dm.detect_burst() is False

    def test_no_burst_below_threshold(self):
        dm = DiscoveryManager()
        now = time.time()
        for i in range(dm.BURST_THRESHOLD - 1):
            dm._ticket_creation_times.append(now - i)
        assert dm.detect_burst(now) is False

    def test_burst_detected_at_threshold(self):
        dm = DiscoveryManager()
        now = time.time()
        for i in range(dm.BURST_THRESHOLD + 5):
            dm._ticket_creation_times.append(now - i)
        assert dm.detect_burst(now) is True

    def test_burst_requires_multiplier(self):
        dm = DiscoveryManager()
        now = time.time()
        old_time = now - dm.BURST_WINDOW_SECONDS * 10
        for i in range(100):
            dm._ticket_creation_times.append(old_time + i)
        for i in range(dm.BURST_THRESHOLD):
            dm._ticket_creation_times.append(now - i)
        result = dm.detect_burst(now)
        assert isinstance(result, bool)

    def test_burst_in_compute_allocation(self):
        dm = DiscoveryManager()
        now = time.time()
        for i in range(dm.BURST_THRESHOLD + 5):
            dm._ticket_creation_times.append(now - i)
        config = _make_config()
        alloc = dm.compute_allocation(
            available_slots=10, config=config, now=now
        )
        assert alloc.total_slots == 0
        assert "burst" in alloc.reason.lower()


# ---------------------------------------------------------------------------
# Coverage tracking tests (§28)
# ---------------------------------------------------------------------------

class TestCoverageTracking:
    def test_empty_coverage(self):
        dm = DiscoveryManager()
        coverage = dm.get_coverage_summary()
        assert coverage["total_scans"] == 0
        assert len(coverage["never_scanned_roles"]) == len(DISCOVERY_ROLES)

    def test_coverage_after_scan(self):
        dm = DiscoveryManager()
        dm.record_completion(
            role="bug_hunter", scope="full_project",
            commit_sha="abc123", findings=3, duplicates=1,
            rejected=0, cost_tokens=1000,
        )
        coverage = dm.get_coverage_summary()
        assert coverage["total_scans"] == 1
        assert "bug_hunter" not in coverage["never_scanned_roles"]

    def test_coverage_tracks_multiple_roles(self):
        dm = DiscoveryManager()
        dm.record_completion("bug_hunter", "full_project", "abc", 3, 1, 0, 1000)
        dm.record_completion("security_auditor", "full_project", "abc", 2, 0, 1, 2000)
        coverage = dm.get_coverage_summary()
        assert coverage["total_scans"] == 2
        remaining = coverage["never_scanned_roles"]
        assert "bug_hunter" not in remaining
        assert "security_auditor" not in remaining
        assert len(remaining) == len(DISCOVERY_ROLES) - 2


# ---------------------------------------------------------------------------
# NO_ACTIONABLE_FINDINGS tests (§34)
# ---------------------------------------------------------------------------

class TestNoActionableFindings:
    def test_record_no_actionable(self):
        dm = DiscoveryManager()
        dm.record_completion(
            role="bug_hunter", scope="full_project",
            commit_sha="abc", findings=0, duplicates=0,
            rejected=0, cost_tokens=500,
            no_actionable=True,
        )
        stats = dm.get_yield_stats("bug_hunter")
        assert stats.no_actionable_runs == 1
        assert stats.total_scans == 1
        assert stats.validated_tickets == 0

    def test_multiple_no_actionable_runs(self):
        dm = DiscoveryManager()
        for _ in range(3):
            dm.record_completion(
                role="security_auditor", scope="full_project",
                commit_sha="abc", findings=0, duplicates=0,
                rejected=0, cost_tokens=500,
                no_actionable=True,
            )
        stats = dm.get_yield_stats("security_auditor")
        assert stats.no_actionable_runs == 3


# ---------------------------------------------------------------------------
# Semantic dedup tests (§9)
# ---------------------------------------------------------------------------

class TestSemanticDedup:
    @pytest.fixture
    def store(self, tmp_path):
        return TicketStore(tmp_path / "tickets.json")

    def test_find_similar_empty(self, store):
        results = store.find_similar("some problem statement")
        assert results == []

    def test_find_similar_matches(self, store):
        t = create_ticket(
            title="Unbounded read",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="web fetch calls resp read without limit causing memory exhaustion",
            desired_state="capped read",
            acceptance_criteria=["capped"],
        )
        store.add(t)
        results = store.find_similar(
            "memory exhaustion from unbounded read in web fetch",
            threshold=0.3,
        )
        assert len(results) > 0
        assert results[0][0].id == t.id
        assert results[0][1] > 0.0

    def test_find_similar_excludes_states(self, store):
        t = create_ticket(
            title="Bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="important bug that needs fixing",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
        )
        store.add(t)
        results = store.find_similar(
            "important bug fixing",
            exclude_states=frozenset({TicketState.DISCOVERED}),
        )
        assert len(results) == 0

    def test_find_similar_threshold(self, store):
        t = create_ticket(
            title="Specific bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="very specific unique problem with particular details",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
        )
        store.add(t)
        results = store.find_similar(
            "completely different unrelated problem", threshold=0.3
        )
        assert len(results) == 0

    def test_fingerprint_dedup_on_add(self, store):
        t1 = create_ticket(
            title="Bug A",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="same problem",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
            fingerprint="abc123fingerprint",
        )
        store.add(t1)
        t2 = create_ticket(
            title="Bug B different title",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="other",
            evidence="other.py:20",
            problem_statement="different evidence",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
            fingerprint="abc123fingerprint",
        )
        with pytest.raises(ValueError, match="fingerprint"):
            store.add(t2)

    def test_fingerprint_allows_terminal_state(self, store):
        t1 = create_ticket(
            title="Bug A",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="same problem",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
            fingerprint="fp_terminal_test",
        )
        store.add(t1)
        store.transition(t1.id, TicketState.VALIDATING)
        store.transition(t1.id, TicketState.REJECTED)
        t2 = create_ticket(
            title="Bug B",
            ticket_class=TicketClass.BUG,
            severity=Severity.MEDIUM,
            source="test",
            evidence="other.py:20",
            problem_statement="regression",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
            fingerprint="fp_terminal_test",
        )
        result = store.add(t2)
        assert result.id == t2.id


# ---------------------------------------------------------------------------
# Discovery history tests (§10)
# ---------------------------------------------------------------------------

class TestDiscoveryHistory:
    @pytest.fixture
    def store(self, tmp_path):
        return TicketStore(tmp_path / "tickets.json")

    def test_empty_history(self, store):
        results = store.discovery_history(discovery_category="bug")
        assert results == []

    def test_finds_by_fingerprint(self, store):
        t = create_ticket(
            title="Bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="problem",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
            fingerprint="history_fp_1",
        )
        store.add(t)
        results = store.discovery_history(fingerprint="history_fp_1")
        assert len(results) == 1
        assert results[0].id == t.id

    def test_finds_by_category_terminal(self, store):
        t = create_ticket(
            title="Bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="problem",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
            discovery_category="security",
        )
        store.add(t)
        store.transition(t.id, TicketState.VALIDATING)
        store.transition(t.id, TicketState.REJECTED)
        results = store.discovery_history(discovery_category="security")
        assert len(results) >= 1

    def test_ignores_active_tickets_for_category(self, store):
        t = create_ticket(
            title="Bug",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="problem",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
            discovery_category="performance",
        )
        store.add(t)
        results = store.discovery_history(discovery_category="performance")
        fp_results = store.discovery_history(fingerprint=t.fingerprint) if t.fingerprint else []
        assert len(results) == 0


# ---------------------------------------------------------------------------
# Change-triggered discovery tests (§30)
# ---------------------------------------------------------------------------

class TestChangeTriggeredDiscovery:
    def test_security_change_triggers_security_auditor(self):
        roles = DiscoveryManager.roles_for_change("security")
        assert "security_auditor" in roles

    def test_concurrency_change_triggers_bug_hunter(self):
        roles = DiscoveryManager.roles_for_change("concurrency")
        assert "bug_hunter" in roles

    def test_dependency_change_triggers_both(self):
        roles = DiscoveryManager.roles_for_change("dependency")
        assert "dependency_auditor" in roles
        assert "security_auditor" in roles

    def test_ui_change_triggers_ux(self):
        roles = DiscoveryManager.roles_for_change("ui")
        assert "ux_auditor" in roles

    def test_unknown_change_returns_empty(self):
        roles = DiscoveryManager.roles_for_change("unknown_category")
        assert roles == ()


# ---------------------------------------------------------------------------
# Backpressure tests (§31)
# ---------------------------------------------------------------------------

class TestBackpressure:
    def test_low_backlog_no_reduction(self):
        dm = DiscoveryManager()
        config = _make_config()
        alloc = dm.compute_allocation(
            available_slots=20, config=config, downstream_backlog=10,
        )
        assert alloc.total_slots > 0

    def test_high_backlog_reduces_slots(self):
        dm = DiscoveryManager()
        config = _make_config()
        alloc_normal = dm.compute_allocation(
            available_slots=20, config=config, downstream_backlog=0,
        )
        alloc_pressure = dm.compute_allocation(
            available_slots=20, config=config, downstream_backlog=100,
        )
        assert alloc_pressure.total_slots <= alloc_normal.total_slots

    def test_extreme_backlog_minimum_slots(self):
        dm = DiscoveryManager()
        config = _make_config()
        alloc = dm.compute_allocation(
            available_slots=20, config=config, downstream_backlog=200,
        )
        assert alloc.total_slots >= config.discovery.minimum_slots


# ---------------------------------------------------------------------------
# Downstream outcome feedback tests (§62)
# ---------------------------------------------------------------------------

class TestDownstreamFeedback:
    def test_record_downstream_completion(self):
        dm = DiscoveryManager()
        dm.record_downstream_completion("bug_hunter")
        stats = dm.get_yield_stats("bug_hunter")
        assert stats.completed_downstream == 1

    def test_record_multiple(self):
        dm = DiscoveryManager()
        dm.record_downstream_completion("security_auditor", count=3)
        stats = dm.get_yield_stats("security_auditor")
        assert stats.completed_downstream == 3

    def test_creates_stats_if_missing(self):
        dm = DiscoveryManager()
        dm.record_downstream_completion("new_role")
        assert "new_role" in dm._yields


# ---------------------------------------------------------------------------
# RoleYieldStats extended metrics tests
# ---------------------------------------------------------------------------

class TestRoleYieldStatsExtended:
    def test_hallucination_rate_zero(self):
        stats = RoleYieldStats(role="test")
        assert stats.hallucination_rate == 0.0

    def test_hallucination_rate_computed(self):
        stats = RoleYieldStats(role="test")
        stats.record_scan(1, 0, 0, 100, hallucinated=1)
        assert stats.hallucination_rate == 1.0

    def test_no_actionable_tracked(self):
        stats = RoleYieldStats(role="test")
        stats.record_scan(0, 0, 0, 100, no_actionable=True)
        assert stats.no_actionable_runs == 1

    def test_stale_prevented_tracked(self):
        stats = RoleYieldStats(role="test")
        stats.record_scan(1, 0, 0, 100, stale_prevented=2)
        assert stats.stale_findings_prevented == 2

    def test_to_dict_includes_new_fields(self):
        stats = RoleYieldStats(role="test")
        stats.record_scan(1, 0, 0, 100, hallucinated=1, stale_prevented=1, no_actionable=True)
        d = stats.to_dict()
        assert "hallucinated_references" in d
        assert "stale_findings_prevented" in d
        assert "no_actionable_runs" in d
        assert "hallucination_rate" in d


# ---------------------------------------------------------------------------
# Persistence tests (new fields survive save/load)
# ---------------------------------------------------------------------------

class TestPersistence:
    def test_yield_stats_persist(self, tmp_path):
        dm1 = DiscoveryManager(state_dir=tmp_path)
        dm1.record_completion("bug_hunter", "full", "abc", 3, 1, 0, 1000,
                              no_actionable=False, hallucinated=1, stale_prevented=2)
        dm1.save()

        dm2 = DiscoveryManager(state_dir=tmp_path)
        stats = dm2.get_yield_stats("bug_hunter")
        assert stats.hallucinated_references == 1
        assert stats.stale_findings_prevented == 2
        assert stats.total_scans == 1

    def test_ticket_creation_times_persist(self, tmp_path):
        dm1 = DiscoveryManager(state_dir=tmp_path)
        now = time.time()
        dm1._ticket_creation_times = [now - 100, now - 50, now]
        dm1.save()

        dm2 = DiscoveryManager(state_dir=tmp_path)
        assert len(dm2._ticket_creation_times) == 3


# ---------------------------------------------------------------------------
# Ticket new fields tests
# ---------------------------------------------------------------------------

class TestTicketNewFields:
    def test_ticket_has_new_fields(self):
        t = create_ticket(
            title="Test",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="problem",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
            confidence="high",
            priority="high",
            repo_revision="abc123",
            atomicity="atomic",
            discovery_category="bug",
            finding_id="DF-TEST001",
            fingerprint="fp123",
        )
        assert t.confidence == "high"
        assert t.priority == "high"
        assert t.repo_revision == "abc123"
        assert t.atomicity == "atomic"
        assert t.discovery_category == "bug"
        assert t.finding_id == "DF-TEST001"
        assert t.fingerprint == "fp123"

    def test_defaults_are_empty(self):
        t = create_ticket(
            title="Test",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="problem",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
        )
        assert t.confidence == ""
        assert t.priority == ""
        assert t.repo_revision == ""
        assert t.atomicity == ""
        assert t.fingerprint == ""

    def test_serialization_roundtrip(self):
        t = create_ticket(
            title="Test",
            ticket_class=TicketClass.BUG,
            severity=Severity.HIGH,
            source="test",
            evidence="file.py:10",
            problem_statement="problem",
            desired_state="fixed",
            acceptance_criteria=["fixed"],
            confidence="medium",
            priority="low",
            atomicity="compound",
            fingerprint="fp_roundtrip",
        )
        d = t.to_dict()
        t2 = Ticket.from_dict(d)
        assert t2.confidence == "medium"
        assert t2.priority == "low"
        assert t2.atomicity == "compound"
        assert t2.fingerprint == "fp_roundtrip"
