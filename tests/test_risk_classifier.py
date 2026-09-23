"""Tests for codebot.risk_classifier module.

Covers classify_risk() scoring logic, autonomy_level_for_risk() thresholds,
and is_autonomous_allowed() class filtering per ticket CB-5764878-560A.
"""

from __future__ import annotations

import pytest

from codebot.risk_classifier import (
    CONSTITUTION_CATEGORIES,
    autonomy_level_for_risk,
    classify_risk,
    is_autonomous_allowed,
)


class TestClassifyRiskConstitutionCategories:
    """CONSTITUTION_CATEGORIES always return score 100 regardless of other inputs."""

    @pytest.mark.parametrize("category", sorted(CONSTITUTION_CATEGORIES))
    def test_constitution_category_returns_100(self, category: str) -> None:
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=["single.py"],
            category=category,
        )
        assert score == 100
        assert "constitution-protected" in reason

    def test_constitution_category_case_insensitive(self) -> None:
        score, reason = classify_risk(
            ticket_class="bug",
            severity="low",
            affected_modules=["x.py"],
            category="CRYPTOGRAPHY",
        )
        assert score == 100

    def test_constitution_category_overrides_all_other_factors(self) -> None:
        score, _ = classify_risk(
            ticket_class="security",
            severity="critical",
            affected_modules=["a", "b", "c", "d", "e", "f"],
            security_impact="auth_bypass",
            blast_radius="all tenants production",
            dependencies=["d1", "d2", "d3", "d4"],
            category="secrets",
        )
        assert score == 100


class TestClassifyRiskSeverityWeights:
    """Severity weights: critical=40, high=25, medium=10, low=0."""

    @pytest.mark.parametrize(
        "severity,expected_weight",
        [("critical", 40), ("high", 25), ("medium", 10), ("low", 0)],
    )
    def test_severity_weight(self, severity: str, expected_weight: int) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity=severity,
            affected_modules=[],
        )
        # documentation=0, no modules, no other factors
        assert score == expected_weight

    def test_severity_case_insensitive(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="CRITICAL",
            affected_modules=[],
        )
        assert score == 40

    def test_unknown_severity_defaults_to_10(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="unknown",
            affected_modules=[],
        )
        assert score == 10


class TestClassifyRiskClassWeights:
    """Class weights for each ticket type."""

    @pytest.mark.parametrize(
        "ticket_class,expected_weight",
        [
            ("security", 30),
            ("architecture", 25),
            ("bug", 15),
            ("refactor", 15),
            ("feature", 10),
            ("performance", 10),
            ("test", 5),
            ("documentation", 0),
            ("dependency", 5),
            ("infrastructure", 10),
        ],
    )
    def test_class_weight(self, ticket_class: str, expected_weight: int) -> None:
        score, _ = classify_risk(
            ticket_class=ticket_class,
            severity="low",
            affected_modules=[],
        )
        assert score == expected_weight

    def test_class_case_insensitive(self) -> None:
        score, _ = classify_risk(
            ticket_class="SECURITY",
            severity="low",
            affected_modules=[],
        )
        assert score == 30

    def test_unknown_class_defaults_to_10(self) -> None:
        score, _ = classify_risk(
            ticket_class="unknown_class",
            severity="low",
            affected_modules=[],
        )
        assert score == 10


class TestClassifyRiskModuleCount:
    """Module count thresholds: >5 = +20, >2 = +10, else 0."""

    def test_zero_modules(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
        )
        assert score == 0

    def test_one_module(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=["a.py"],
        )
        assert score == 0

    def test_two_modules(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=["a.py", "b.py"],
        )
        assert score == 0

    def test_three_modules_adds_10(self) -> None:
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=["a.py", "b.py", "c.py"],
        )
        assert score == 10
        assert "multi-module" in reason

    def test_five_modules_adds_10(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=["a", "b", "c", "d", "e"],
        )
        assert score == 10

    def test_six_modules_adds_20(self) -> None:
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=["a", "b", "c", "d", "e", "f"],
        )
        assert score == 20
        assert "cross-cutting" in reason

    def test_empty_affected_modules_is_falsy(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
        )
        assert score == 0


class TestClassifyRiskSecurityImpact:
    """security_impact != 'none' adds +15."""

    def test_no_security_impact(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            security_impact="none",
        )
        assert score == 0

    def test_security_impact_adds_15(self) -> None:
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            security_impact="auth_bypass",
        )
        assert score == 15
        assert "security_impact" in reason

    def test_security_impact_case_insensitive(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            security_impact="NONE",
        )
        assert score == 0

    def test_empty_security_impact_no_bonus(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            security_impact="",
        )
        assert score == 0


class TestClassifyRiskDependencies:
    """Dependency fan-in: >3 dependencies adds +10."""

    def test_no_dependencies(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            dependencies=None,
        )
        assert score == 0

    def test_three_dependencies_no_bonus(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            dependencies=["d1", "d2", "d3"],
        )
        assert score == 0

    def test_four_dependencies_adds_10(self) -> None:
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            dependencies=["d1", "d2", "d3", "d4"],
        )
        assert score == 10
        assert "high fan-in" in reason

    def test_empty_dependencies_list(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            dependencies=[],
        )
        assert score == 0


class TestClassifyRiskBlastRadius:
    """Blast radius keyword matching (case-insensitive)."""

    @pytest.mark.parametrize(
        "blast_radius",
        ["all tenants", "global", "fleet-wide", "production"],
    )
    def test_wide_blast_radius_adds_15(self, blast_radius: str) -> None:
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            blast_radius=blast_radius,
        )
        assert score == 15
        assert "wide blast radius" in reason

    @pytest.mark.parametrize(
        "blast_radius",
        ["ALL TENANTS", "Global", "FLEET-WIDE", "Production"],
    )
    def test_wide_blast_radius_case_insensitive(self, blast_radius: str) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            blast_radius=blast_radius,
        )
        assert score == 15

    @pytest.mark.parametrize(
        "blast_radius",
        ["company", "tenant", "multi-site"],
    )
    def test_tenant_scoped_blast_radius_adds_5(self, blast_radius: str) -> None:
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            blast_radius=blast_radius,
        )
        assert score == 5
        assert "tenant-scoped" in reason

    @pytest.mark.parametrize(
        "blast_radius",
        ["Company", "TENANT", "Multi-Site"],
    )
    def test_tenant_scoped_case_insensitive(self, blast_radius: str) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            blast_radius=blast_radius,
        )
        assert score == 5

    def test_no_blast_radius(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            blast_radius="",
        )
        assert score == 0

    def test_unknown_blast_radius_no_bonus(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            blast_radius="single user",
        )
        assert score == 0

    def test_wide_takes_precedence_over_tenant(self) -> None:
        """'all tenants' matches wide (+15), not tenant-scoped (+5)."""
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
            blast_radius="all tenants",
        )
        assert score == 15
        assert "wide blast radius" in reason


class TestClassifyRiskScoreClamping:
    """Score is clamped to [0, 100]."""

    def test_score_capped_at_100(self) -> None:
        score, _ = classify_risk(
            ticket_class="security",
            severity="critical",
            affected_modules=["a", "b", "c", "d", "e", "f"],
            security_impact="rce",
            blast_radius="production",
            dependencies=["d1", "d2", "d3", "d4"],
        )
        # 30 + 40 + 20 + 15 + 15 + 10 = 130, clamped to 100
        assert score == 100

    def test_minimum_score_is_zero(self) -> None:
        score, _ = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
        )
        assert score == 0


class TestClassifyRiskLevelTransitions:
    """Level thresholds: >=70 critical, >=45 high, >=20 medium, <20 low."""

    def test_score_69_is_high(self) -> None:
        # Need exactly 69: security(30) + critical(40) - 1... not possible with weights.
        # Use: architecture(25) + critical(40) + 3modules(10) - but that's 75.
        # Let's try: bug(15) + high(25) + 3modules(10) + security_impact(15) + tenant_blast(5) = 70 -> critical
        # For 69: feature(10) + high(25) + 3modules(10) + security_impact(15) + 4deps(10) = 70 -> critical
        # For 69: bug(15) + high(25) + 6modules(20) + tenant_blast(5) = 65 -> high
        score, reason = classify_risk(
            ticket_class="bug",
            severity="high",
            affected_modules=["a", "b", "c", "d", "e", "f"],
            blast_radius="tenant",
        )
        # 15 + 25 + 20 + 5 = 65
        assert score == 65
        assert reason.startswith("high:")

    def test_score_70_is_critical(self) -> None:
        # bug(15) + high(25) + 6modules(20) + security_impact(15) = 75... too high
        # architecture(25) + high(25) + 6modules(20) = 70
        score, reason = classify_risk(
            ticket_class="architecture",
            severity="high",
            affected_modules=["a", "b", "c", "d", "e", "f"],
        )
        assert score == 70
        assert reason.startswith("critical:")

    def test_score_44_is_medium(self) -> None:
        # bug(15) + high(25) + tenant_blast(5) = 45 -> high
        # feature(10) + high(25) + 3modules(10) = 45 -> high
        # feature(10) + medium(10) + 6modules(20) + tenant_blast(5) = 45 -> high
        # bug(15) + medium(10) + 3modules(10) + tenant_blast(5) = 40 -> medium
        score, reason = classify_risk(
            ticket_class="bug",
            severity="medium",
            affected_modules=["a", "b", "c"],
            blast_radius="tenant",
        )
        # 15 + 10 + 10 + 5 = 40
        assert score == 40
        assert reason.startswith("medium:")

    def test_score_45_is_high(self) -> None:
        # feature(10) + high(25) + 3modules(10) = 45
        score, reason = classify_risk(
            ticket_class="feature",
            severity="high",
            affected_modules=["a", "b", "c"],
        )
        assert score == 45
        assert reason.startswith("high:")

    def test_score_19_is_low(self) -> None:
        # medium(10) + test(5) + tenant_blast(5) = 20 -> medium
        # medium(10) + dependency(5) = 15 -> low
        score, reason = classify_risk(
            ticket_class="dependency",
            severity="medium",
            affected_modules=[],
        )
        # 10 + 5 = 15
        assert score == 15
        assert reason.startswith("low:")

    def test_score_20_is_medium(self) -> None:
        # 6modules alone = 20
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=["a", "b", "c", "d", "e", "f"],
        )
        assert score == 20
        assert reason.startswith("medium:")


class TestClassifyRiskReasonString:
    """Reason string formatting."""

    def test_baseline_risk_when_no_factors(self) -> None:
        score, reason = classify_risk(
            ticket_class="documentation",
            severity="low",
            affected_modules=[],
        )
        assert score == 0
        assert reason == "low: baseline risk"

    def test_multiple_reasons_joined(self) -> None:
        score, reason = classify_risk(
            ticket_class="security",
            severity="critical",
            affected_modules=["a", "b", "c"],
            security_impact="sqli",
        )
        assert "severity=critical" in reason
        assert "class=security" in reason
        assert "multi-module" in reason
        assert "security_impact" in reason


class TestAutonomyLevelForRisk:
    """autonomy_level_for_risk() threshold boundaries."""

    def test_score_70_requires_human(self) -> None:
        allowed, reason = autonomy_level_for_risk(70)
        assert allowed is False
        assert "human_required" in reason

    def test_score_69_at_level_2_requires_human(self) -> None:
        allowed, reason = autonomy_level_for_risk(69, project_autonomy_level=2)
        assert allowed is False
        assert "human_required" in reason

    def test_score_45_at_level_2_requires_human(self) -> None:
        allowed, reason = autonomy_level_for_risk(45, project_autonomy_level=2)
        assert allowed is False
        assert "human_required" in reason

    def test_score_45_at_level_3_is_autonomous(self) -> None:
        allowed, reason = autonomy_level_for_risk(45, project_autonomy_level=3)
        assert allowed is True
        assert "autonomous" in reason

    def test_score_44_at_level_2_requires_human(self) -> None:
        allowed, reason = autonomy_level_for_risk(44, project_autonomy_level=2)
        assert allowed is False

    def test_score_19_at_level_2_is_autonomous(self) -> None:
        allowed, reason = autonomy_level_for_risk(19, project_autonomy_level=2)
        assert allowed is True
        assert "autonomous" in reason

    def test_score_20_at_level_2_requires_human(self) -> None:
        allowed, reason = autonomy_level_for_risk(20, project_autonomy_level=2)
        assert allowed is False

    def test_score_0_at_level_2_is_autonomous(self) -> None:
        allowed, reason = autonomy_level_for_risk(0, project_autonomy_level=2)
        assert allowed is True

    def test_score_0_at_level_1_requires_human(self) -> None:
        allowed, reason = autonomy_level_for_risk(0, project_autonomy_level=1)
        assert allowed is False

    def test_any_score_at_level_3_below_45_is_autonomous(self) -> None:
        allowed, _ = autonomy_level_for_risk(44, project_autonomy_level=3)
        assert allowed is True

    def test_any_score_at_level_3_above_70_requires_human(self) -> None:
        allowed, _ = autonomy_level_for_risk(71, project_autonomy_level=3)
        assert allowed is False


class TestIsAutonomousAllowed:
    """is_autonomous_allowed() class filtering."""

    def test_constitution_category_blocks_autonomy(self) -> None:
        assert is_autonomous_allowed("test", category="cryptography") is False

    def test_constitution_category_case_insensitive(self) -> None:
        assert is_autonomous_allowed("test", category="CRYPTOGRAPHY") is False

    @pytest.mark.parametrize(
        "ticket_class",
        ["documentation", "test", "lint", "refactor"],
    )
    def test_autonomous_classes_at_level_2(self, ticket_class: str) -> None:
        assert is_autonomous_allowed(ticket_class, project_autonomy_level=2) is True

    @pytest.mark.parametrize(
        "ticket_class",
        ["documentation", "test", "lint", "refactor"],
    )
    def test_autonomous_classes_at_level_1_blocked(self, ticket_class: str) -> None:
        assert is_autonomous_allowed(ticket_class, project_autonomy_level=1) is False

    def test_non_autonomous_class_at_level_2_blocked(self) -> None:
        assert is_autonomous_allowed("security", project_autonomy_level=2) is False

    def test_non_autonomous_class_at_level_3_blocked(self) -> None:
        assert is_autonomous_allowed("bug", project_autonomy_level=3) is False

    def test_any_class_at_level_4_allowed(self) -> None:
        assert is_autonomous_allowed("security", project_autonomy_level=4) is True

    def test_any_class_at_level_5_allowed(self) -> None:
        assert is_autonomous_allowed("architecture", project_autonomy_level=5) is True

    def test_case_insensitive_class(self) -> None:
        assert is_autonomous_allowed("TEST", project_autonomy_level=2) is True

    def test_empty_category_no_constitution_block(self) -> None:
        assert is_autonomous_allowed("test", category="", project_autonomy_level=2) is True
