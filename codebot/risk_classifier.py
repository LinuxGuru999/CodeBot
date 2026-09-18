#!/usr/bin/env python3
"""Risk classification scoring for CodeBot tickets.

Purpose
-------
Computes a numeric risk score for each ticket based on its class, severity,
affected modules, security impact, blast radius, and dependency count. The
score drives autonomy level decisions: low-risk tickets proceed autonomously,
high-risk tickets require human approval.

Why
---
CODEBOT-ROADMAP.md §13-14 require risk-based autonomy levels. Without
quantified risk, every ticket either needs human approval (too slow) or
proceeds autonomously (too dangerous). A deterministic scoring function
ensures consistent risk assessment regardless of which agent triages.

Invariants
----------
- stdlib-only
- Score range: 0-100
- Scoring is deterministic: same inputs always produce same output
- No I/O, no side effects — pure computation
- Constitution-protected categories always score >= 70 (require human approval)
"""

from __future__ import annotations


# Categories that always require human approval per constitution §8
CONSTITUTION_CATEGORIES = frozenset({
    "authentication_architecture",
    "authorization_boundaries",
    "cryptography",
    "destructive_migrations",
    "secrets",
    "security_policy_relaxation",
    "constitution_changes",
    "major_architecture_changes",
})

SEVERITY_WEIGHTS: dict[str, int] = {
    "critical": 40,
    "high": 25,
    "medium": 10,
    "low": 0,
}

CLASS_WEIGHTS: dict[str, int] = {
    "security": 30,
    "architecture": 25,
    "bug": 15,
    "refactor": 15,
    "feature": 10,
    "performance": 10,
    "test": 5,
    "documentation": 0,
    "dependency": 5,
    "infrastructure": 10,
}


def classify_risk(
    ticket_class: str,
    severity: str,
    affected_modules: list[str],
    security_impact: str = "none",
    blast_radius: str = "",
    dependencies: list[str] | None = None,
    category: str = "",
) -> tuple[int, str]:
    score = 0
    reasons: list[str] = []

    if category and category.lower() in CONSTITUTION_CATEGORIES:
        return 100, "constitution-protected: requires human approval"

    sev_weight = SEVERITY_WEIGHTS.get(severity.lower(), 10)
    score += sev_weight
    if sev_weight >= 25:
        reasons.append(f"severity={severity}")

    cls_weight = CLASS_WEIGHTS.get(ticket_class.lower(), 10)
    score += cls_weight
    if cls_weight >= 25:
        reasons.append(f"class={ticket_class}")

    module_count = len(affected_modules) if affected_modules else 0
    if module_count > 5:
        score += 20
        reasons.append(f"cross-cutting: {module_count} modules")
    elif module_count > 2:
        score += 10
        reasons.append(f"multi-module: {module_count} modules")

    if security_impact and security_impact.lower() != "none":
        score += 15
        reasons.append(f"security_impact={security_impact}")

    dep_count = len(dependencies) if dependencies else 0
    if dep_count > 3:
        score += 10
        reasons.append(f"high fan-in: {dep_count} dependencies")

    if blast_radius:
        br_lower = blast_radius.lower()
        if any(kw in br_lower for kw in ("all tenants", "global", "fleet-wide", "production")):
            score += 15
            reasons.append("wide blast radius")
        elif any(kw in br_lower for kw in ("company", "tenant", "multi-site")):
            score += 5
            reasons.append("tenant-scoped blast radius")

    score = max(0, min(100, score))

    if score >= 70:
        level = "critical"
    elif score >= 45:
        level = "high"
    elif score >= 20:
        level = "medium"
    else:
        level = "low"

    reason_str = "; ".join(reasons) if reasons else "baseline risk"
    return score, f"{level}: {reason_str}"


def autonomy_level_for_risk(score: int, project_autonomy_level: int = 2) -> tuple[bool, str]:
    if score >= 70:
        return False, "human_required: risk score >= 70"
    if score >= 45 and project_autonomy_level < 3:
        return False, "human_required: medium-high risk at autonomy level < 3"
    if score < 20 and project_autonomy_level >= 2:
        return True, "autonomous: low risk"
    if project_autonomy_level >= 3:
        return True, "autonomous: project allows level 3+"
    return False, "human_required: insufficient autonomy level for risk"


def is_autonomous_allowed(ticket_class: str, category: str = "", project_autonomy_level: int = 2) -> bool:
    if category and category.lower() in CONSTITUTION_CATEGORIES:
        return False
    autonomous_classes = frozenset({
        "documentation", "test", "lint", "refactor",
    })
    if ticket_class.lower() in autonomous_classes and project_autonomy_level >= 2:
        return True
    return project_autonomy_level >= 4
