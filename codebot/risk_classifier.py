"""Deterministic risk scoring and autonomy gatekeeper.

Provides functions to calculate a numeric risk score (0-100) for tickets
and determine whether automated processing is permitted based on that score,
ticket class, and project autonomy configuration.
"""

from __future__ import annotations

import re
from typing import Any, Optional

# Categories considered protected by organizational constitution/policy.
# Changes touching these areas generally require explicit human sign-off
# regardless of calculated risk score.
CONSTITUTION_CATEGORIES = {
    "cryptography",
    "secrets",
    "auth",
    "pii",
    "compliance",
}

_SEVERITY_WEIGHTS = {
    "critical": 40,
    "high": 25,
    "medium": 10,
    "low": 0,
}
_DEFAULT_SEVERITY_WEIGHT = 10

_CLASS_WEIGHTS = {
    "security": 30,
    "architecture": 25,
    "bug": 15,
    "refactor": 15,
    "feature": 10,
    "performance": 10,
    "infrastructure": 10,
    "test": 5,
    "dependency": 5,
    "documentation": 0,
}
_DEFAULT_CLASS_WEIGHT = 10

_WIDE_BLAST_KEYWORDS = {
    "all tenants",
    "global",
    "fleet-wide",
    "production",
}
_TENANT_BLAST_KEYWORDS = {
    "company",
    "tenant",
    "multi-site",
}


def classify_risk(
    *,
    ticket_class: str,
    severity: str,
    affected_modules: list[str],
    category: Optional[str] = None,
    security_impact: str = "none",
    blast_radius: str = "",
    dependencies: Optional[list[Any]] = None,
) -> tuple[int, str]:
    """Calculate deterministic risk score and reasoning string.

    Args:
        ticket_class: Type of work (e.g., 'bug', 'feature').
        severity: Urgency/severity label ('critical', 'high', etc.).
        affected_modules: List of module paths touched.
        category: Optional classification tag (checked against CONSTITUTION_CATEGORIES).
        security_impact: Description of potential security implication.
        blast_radius: Scope description keyword.
        dependencies: List of dependent components/modules.

    Returns:
        Tuple of (score [0-100], reason_string).
    """
    reasons: list[str] = []
    score = 0

    # Normalize inputs for consistent matching
    norm_category = (category or "").lower()
    norm_severity = severity.lower()
    norm_class = ticket_class.lower()
    norm_sec_impact = security_impact.lower().strip()
    norm_blast = blast_radius.lower().strip()

    # 1. Constitution Override
    if norm_category in {c.lower() for c in CONSTITUTION_CATEGORIES}:
        return 100, f"constitution-protected ({norm_category})"

    # 2. Severity Weight
    sev_weight = _SEVERITY_WEIGHTS.get(norm_severity, _DEFAULT_SEVERITY_WEIGHT)
    score += sev_weight
    if sev_weight > 0:
        reasons.append(f"severity={norm_severity}")

    # 3. Class Weight
    cls_weight = _CLASS_WEIGHTS.get(norm_class, _DEFAULT_CLASS_WEIGHT)
    score += cls_weight
    if cls_weight > 0:
        reasons.append(f"class={norm_class}")

    # 4. Module Count Thresholds
    mod_count = len(affected_modules)
    if mod_count > 5:
        score += 20
        reasons.append("cross-cutting")
    elif mod_count > 2:
        score += 10
        reasons.append("multi-module")

    # 5. Security Impact Bonus
    if norm_sec_impact and norm_sec_impact != "none":
        score += 15
        reasons.append("security_impact")

    # 6. Dependency Fan-in
    dep_count = len(dependencies) if dependencies else 0
    if dep_count > 3:
        score += 10
        reasons.append("high fan-in")

    # 7. Blast Radius Matching
    # Wide takes precedence over tenant-scoped in simple keyword match order
    matched_wide = any(kw in norm_blast for kw in _WIDE_BLAST_KEYWORDS)
    if matched_wide:
        score += 15
        reasons.append("wide blast radius")
    else:
        matched_tenant = any(kw in norm_blast for kw in _TENANT_BLAST_KEYWORDS)
        if matched_tenant:
            score += 5
            reasons.append("tenant-scoped")

    # Clamp final score
    final_score = max(0, min(100, score))

    # Determine primary level descriptor for reason prefix
    if final_score >= 70:
        level_desc = "critical"
    elif final_score >= 45:
        level_desc = "high"
    elif final_score >= 20:
        level_desc = "medium"
    else:
        level_desc = "low"

    if not reasons:
        detail = "baseline risk"
    else:
        detail = "; ".join(reasons)

    reason_str = f"{level_desc}: {detail}"
    return final_score, reason_str


def autonomy_level_for_risk(
    score: int,
    project_autonomy_level: int = 1,
) -> tuple[bool, str]:
    """Determine if autonomous execution is allowed given risk score and config level.

    Levels typically range 1 (strictest/manual) to 5 (most autonomous).
    Higher scores demand higher autonomy levels to proceed automatically.

    Args:
        score: Calculated risk score (0-100).
        project_autonomy_level: Configured autonomy setting (default 1).

    Returns:
        Tuple of (is_allowed, reason_description).
    """
    # Hard cap: Very high risk always needs human review unless extreme autonomy configured?
    # Based on tests: score>=70 blocked even at lower levels. At level 3+, still blocked?
    # Test: `test_any_score_at_level_3_above_70_requires_human` -> False.
    # It seems >=70 is ALWAYS human_required except potentially very high levels? 
    # But we don't have tests for L4/L5 blocking here specifically for score.
    # Let's stick to observed boundaries.
    
    if score >= 70:
        return False, "human_required (critical risk)"
    
    if score >= 45:
        if project_autonomy_level >= 3:
            return True, "autonomous (high risk accepted at level 3+)"
        return False, "human_required (high risk)"
        
    if score >= 20:
        if project_autonomy_level >= 3:
             return True, "autonomous (medium risk accepted at level 3+)"
        # Wait, test says 20@L2 is False. 19@L2 is True.
        # So threshold for L2 is strictly < 20.
        return False, "human_required (medium risk)"
        
    # Score < 20
    if project_autonomy_level >= 2:
        return True, "autonomous (low risk)"
    
    # Level 1 blocks everything apparently
    return False, "human_required (strict mode)"


def is_autonomous_allowed(
    ticket_class: str,
    category: Optional[str] = None,
    project_autonomy_level: int = 1,
) -> bool:
    """Check if a specific ticket class/category is whitelisted for automation.

    This acts as a secondary filter alongside numerical risk assessment.

    Args:
        ticket_class: The type of ticket.
        category: Optional policy category.
        project_autonomy_level: Current autonomy configuration.

    Returns:
        Boolean indicating permission.
    """
    norm_cat = (category or "").lower()
    norm_cls = ticket_class.lower()

    # Constitution blocks all
    if norm_cat in {c.lower() for c in CONSTITUTION_CATEGORIES}:
        return False

    # High autonomy levels bypass class restrictions
    if project_autonomy_level >= 4:
        return True

    # Define safe classes for intermediate levels
    # From tests: doc, test, lint, refactor are OK at L2.
    SAFE_CLASSES_L2 = {"documentation", "test", "lint", "refactor"}
    
    if project_autonomy_level == 2:
        return norm_cls in SAFE_CLASSES_L2
        
    if project_autonomy_level == 3:
        # Broader set? Tests imply security/bug blocked at L3 too?
        # `test_non_autonomous_class_at_level_3_blocked`: security->False, bug->False.
        # So L3 behaves same as L2 regarding class filtering?
        return norm_cls in SAFE_CLASSES_L2

    # Level 1 blocks almost everything implicitly via strictness or empty set
    return False
