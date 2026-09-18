"""Tests for risk_classifier.py."""
import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from codebot.risk_classifier import classify_risk, autonomy_level_for_risk, is_autonomous_allowed, CONSTITUTION_CATEGORIES

class TestClassifyRisk:
    def test_low_risk_documentation(self):
        score, reason = classify_risk("documentation", "low", ["docs/readme.md"])
        assert score < 20

    def test_critical_security_high_score(self):
        score, _ = classify_risk("security", "critical", ["lib/auth.py", "lib/router.py", "lib/store.py"])
        assert score >= 70

    def test_deterministic(self):
        args = ("bug", "medium", ["lib/router.py"], "none", "single tenant", [], "")
        assert classify_risk(*args) == classify_risk(*args)

    def test_constitution_category_always_100(self):
        for cat in CONSTITUTION_CATEGORIES:
            score, reason = classify_risk("feature", "low", [], category=cat)
            assert score == 100
            assert "constitution" in reason

    def test_score_clamped(self):
        score, _ = classify_risk("security", "critical", ["a","b","c","d","e","f"], security_impact="rce", blast_radius="global production", dependencies=["1","2","3","4"])
        assert 0 <= score <= 100

class TestAutonomyLevel:
    def test_high_risk_requires_human(self):
        allowed, _ = autonomy_level_for_risk(80, project_autonomy_level=2)
        assert not allowed

    def test_low_risk_autonomous_at_level_2(self):
        allowed, _ = autonomy_level_for_risk(10, project_autonomy_level=2)
        assert allowed

class TestIsAutonomousAllowed:
    def test_constitution_never_autonomous(self):
        for cat in CONSTITUTION_CATEGORIES:
            assert not is_autonomous_allowed("feature", category=cat)

    def test_documentation_autonomous_at_level_2(self):
        assert is_autonomous_allowed("documentation", project_autonomy_level=2)
