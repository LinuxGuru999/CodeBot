#!/usr/bin/env python3
"""Configuration for adversarial review and independent gatekeeping.

Purpose
-------
Centralizes every tunable of the review/gatekeeping pipeline: severity
blocking rules, risk-based reviewer routing, blind review, multi-reviewer
consensus, random post-completion audits, cross-model preference, rework-loop
protection, and deterministic-first verification requirements. Defaults encode
the project's strictest safe behavior; operators relax them via
.codebot/review.yaml.

Why
---
The review system must be rigorous without being wasteful, and its strictness
must be a deliberate, auditable choice rather than a hard-coded accident.
Keeping these knobs in one typed, stdlib-only module means the dispatcher,
gatekeeper, and reviewers all read a single source of truth, and that changes
to CodeBot's own review policy are concentrated in one file (which itself
receives elevated scrutiny per the gate-tampering rules).

Invariants
----------
- stdlib-only (dataclasses, pathlib, json, os)
- Every field has a safe default; a missing or corrupt config file degrades to
  defaults rather than raising
- Blocking severities default to BLOCKER + CRITICAL + MAJOR
- UNKNOWN checklist results never count as PASS (enforced in review_types)
- Risk-class reviewer counts are monotonic: higher risk never uses fewer
  reviewers than lower risk
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codebot.review_types import FindingSeverity, RiskClass, DEFAULT_BLOCKING_SEVERITIES

_CODEBOT_PKG_DIR = Path(__file__).parent
_PROJECT_ROOT = _CODEBOT_PKG_DIR.parent
_DEFAULT_REVIEW_CONFIG_PATH = _PROJECT_ROOT / ".codebot" / "review.yaml"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (ValueError, TypeError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class ReviewConfig:
    """All tunables for the adversarial review and gatekeeping pipeline.

    Reviewer counts are keyed by RiskClass.value ("LOW"/"MEDIUM"/"HIGH"/
    "CRITICAL"). The dispatcher reads these to decide how many independent
    reviewers a ticket needs. A single valid BLOCKER/CRITICAL finding blocks
    completion regardless of how many reviewers approved, so these counts
    control depth of inspection, not a voting quorum.
    """

    # --- Severity blocking -------------------------------------------------
    # Severities that block completion while unresolved. Default: the three
    # highest non-nit severities. Operators may drop MAJOR to be more lenient,
    # but BLOCKER and CRITICAL should never be removed.
    blocking_severities: frozenset[FindingSeverity] = DEFAULT_BLOCKING_SEVERITIES

    # --- Review behavior ---------------------------------------------------
    blind_review_enabled: bool = True
    adversarial_review_enabled: bool = True
    multi_reviewer_enabled: bool = True
    cross_model_preferred: bool = True
    require_tests_for_behavior_changes: bool = True

    # --- Risk-based reviewer routing ---------------------------------------
    # Number of independent reviewers required per risk class. Monotonic by
    # construction: CRITICAL >= HIGH >= MEDIUM >= LOW.
    reviewers_per_risk: dict[str, int] = field(default_factory=lambda: {
        RiskClass.LOW.value: 1,
        RiskClass.MEDIUM.value: 1,
        RiskClass.HIGH.value: 2,
        RiskClass.CRITICAL.value: 2,
    })

    # Specialized reviewer roles automatically added for HIGH/CRITICAL risk.
    # Keys are RiskClass.value; values are role basenames present in
    # codebot/roles/. Empty list means no specialized routing for that class.
    specialized_reviewers_per_risk: dict[str, list[str]] = field(default_factory=lambda: {
        RiskClass.LOW.value: [],
        RiskClass.MEDIUM.value: [],
        RiskClass.HIGH.value: ["security_reviewer"],
        RiskClass.CRITICAL.value: ["security_reviewer", "architecture_reviewer"],
    })

    # --- Post-completion audits --------------------------------------------
    # Percentage of completed tickets re-reviewed by an independent auditor.
    # Sampling is risk-weighted: higher-risk completions are more likely to be
    # audited. 0 disables audits.
    random_audit_percentage: int = 10

    # --- Rework-loop protection --------------------------------------------
    # After this many rework cycles on the same ticket, escalate to an
    # alternate/senior reviewer or NEEDS_HUMAN instead of looping. Never
    # force-complete.
    max_rework_cycles: int = 3
    # After this many repetitions of the same finding across rework cycles,
    # escalate.
    repeated_finding_threshold: int = 2

    # --- Gatekeeper evidence requirements ----------------------------------
    require_test_evidence: bool = True
    require_build_success: bool = True
    require_regression_check: bool = True
    require_checklist_complete: bool = True

    # --- Deterministic-first verification ----------------------------------
    # Run cheap deterministic gates (build/tests/lint/type check) before
    # spending model tokens on reasoning. Almost always True.
    deterministic_gates_first: bool = True

    # --- Review cost guards ------------------------------------------------
    # Skip expensive adversarial passes for trivially low-risk changes (e.g.
    # documentation-only or formatting-only diffs). The risk classifier must
    # agree the change is LOW risk and touch only exempt paths.
    skip_adversarial_for_trivial: bool = True
    trivial_path_patterns: tuple[str, ...] = (
        "docs/", "README", ".md", "CHANGELOG",
    )

    def reviewers_for_risk(self, risk: RiskClass) -> int:
        """Number of independent reviewers required for a risk class."""
        return max(1, int(self.reviewers_per_risk.get(risk.value, 1)))

    def specialized_reviewers_for_risk(self, risk: RiskClass) -> list[str]:
        """Specialized reviewer role basenames for a risk class."""
        return list(self.specialized_reviewers_per_risk.get(risk.value, []))

    def is_trivial_path_set(self, changed_files: list[str]) -> bool:
        """True if every changed file matches a trivial path pattern."""
        if not changed_files:
            return False
        for f in changed_files:
            if not any(pat in f for pat in self.trivial_path_patterns):
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "blocking_severities": sorted(s.value for s in self.blocking_severities),
            "blind_review_enabled": self.blind_review_enabled,
            "adversarial_review_enabled": self.adversarial_review_enabled,
            "multi_reviewer_enabled": self.multi_reviewer_enabled,
            "cross_model_preferred": self.cross_model_preferred,
            "require_tests_for_behavior_changes": self.require_tests_for_behavior_changes,
            "reviewers_per_risk": dict(self.reviewers_per_risk),
            "specialized_reviewers_per_risk": dict(self.specialized_reviewers_per_risk),
            "random_audit_percentage": self.random_audit_percentage,
            "max_rework_cycles": self.max_rework_cycles,
            "repeated_finding_threshold": self.repeated_finding_threshold,
            "require_test_evidence": self.require_test_evidence,
            "require_build_success": self.require_build_success,
            "require_regression_check": self.require_regression_check,
            "require_checklist_complete": self.require_checklist_complete,
            "deterministic_gates_first": self.deterministic_gates_first,
            "skip_adversarial_for_trivial": self.skip_adversarial_for_trivial,
            "trivial_path_patterns": list(self.trivial_path_patterns),
        }


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Minimal two-level YAML parser (top-level keys + one nesting level).

    Mirrors quality_gate._parse_simple_yaml but supports nested maps (two
    spaces) in addition to lists, which review.yaml needs for
    reviewers_per_risk and specialized_reviewers_per_risk.
    """
    result: dict[str, Any] = {}
    current_key = ""
    for line in text.splitlines():
        if not line.strip() or line.strip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        stripped = line.strip()
        if indent == 0:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if val:
                result[key] = _coerce_scalar(val)
                current_key = ""
            else:
                result[key] = {}
                current_key = key
        elif current_key and indent >= 2:
            if stripped.startswith("- "):
                if not isinstance(result[current_key], list):
                    result[current_key] = []
                result[current_key].append(_coerce_scalar(stripped[2:].strip()))
            elif ":" in stripped:
                if not isinstance(result[current_key], dict):
                    result[current_key] = {}
                k, _, v = stripped.partition(":")
                result[current_key][k.strip()] = _coerce_scalar(v.strip())
    return result


def _coerce_scalar(raw: str) -> Any:
    raw = raw.strip().strip('"').strip("'")
    low = raw.lower()
    if low in ("true", "yes", "on"):
        return True
    if low in ("false", "no", "off"):
        return False
    if low in ("null", "none", "~", ""):
        return None
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _parse_severity_list(raw: Any) -> frozenset[FindingSeverity] | None:
    if raw is None:
        return None
    if isinstance(raw, str):
        items = [s.strip() for s in raw.split(",") if s.strip()]
    elif isinstance(raw, list):
        items = [str(s).strip() for s in raw if str(s).strip()]
    else:
        return None
    sevs: set[FindingSeverity] = set()
    for item in items:
        try:
            sevs.add(FindingSeverity(item.upper()))
        except ValueError:
            continue
    return frozenset(sevs) if sevs else None


def load_review_config(path: Path | None = None) -> ReviewConfig:
    """Load ReviewConfig from review.yaml, degrading to defaults on any error.

    Environment overrides (CODEBOT_REVIEW_*) win over file values, which win
    over defaults, so operators can pin behavior without editing the repo.
    """
    cfg = ReviewConfig()
    target = path or _DEFAULT_REVIEW_CONFIG_PATH
    data: dict[str, Any] = {}
    if target.exists():
        try:
            data = _parse_simple_yaml(target.read_text(encoding="utf-8"))
        except OSError:
            data = {}

    review = data.get("review", {}) if isinstance(data.get("review", {}), dict) else {}
    gatekeeper = data.get("gatekeeper", {}) if isinstance(data.get("gatekeeper", {}), dict) else {}
    rework = data.get("rework", {}) if isinstance(data.get("rework", {}), dict) else {}

    def _get(section: dict[str, Any], key: str, default: Any) -> Any:
        return section.get(key, default)

    # Severity blocking
    blocking = _parse_severity_list(_get(gatekeeper, "blocking_severities", None))
    if blocking is not None:
        cfg.blocking_severities = blocking

    # Review behavior booleans
    cfg.blind_review_enabled = bool(_get(review, "blind_review_enabled", cfg.blind_review_enabled))
    cfg.adversarial_review_enabled = bool(_get(review, "adversarial_review_enabled", cfg.adversarial_review_enabled))
    cfg.multi_reviewer_enabled = bool(_get(review, "multi_reviewer_enabled", cfg.multi_reviewer_enabled))
    cfg.cross_model_preferred = bool(_get(review, "cross_model_preferred", cfg.cross_model_preferred))
    cfg.require_tests_for_behavior_changes = bool(_get(review, "require_tests_for_behavior_changes", cfg.require_tests_for_behavior_changes))

    # Risk-based routing — these live at top level in review.yaml because the
    # 2-level YAML parser cannot represent review → reviewers_per_risk → LOW.
    rpr = data.get("reviewers_per_risk") or _get(review, "reviewers_per_risk", None)
    if isinstance(rpr, dict):
        merged = dict(cfg.reviewers_per_risk)
        for rc in RiskClass:
            if rc.value in rpr:
                try:
                    merged[rc.value] = max(1, int(rpr[rc.value]))
                except (ValueError, TypeError):
                    pass
        cfg.reviewers_per_risk = merged

    spr = data.get("specialized_reviewers_per_risk") or _get(review, "specialized_reviewers_per_risk", None)
    if isinstance(spr, dict):
        merged_spec = dict(cfg.specialized_reviewers_per_risk)
        for rc in RiskClass:
            val = spr.get(rc.value)
            if isinstance(val, list):
                merged_spec[rc.value] = [str(v) for v in val]
            elif isinstance(val, str) and val:
                merged_spec[rc.value] = [s.strip() for s in val.split(",") if s.strip()]
        cfg.specialized_reviewers_per_risk = merged_spec

    # Audits
    try:
        cfg.random_audit_percentage = max(0, min(100, int(_get(review, "random_audit_percentage", cfg.random_audit_percentage))))
    except (ValueError, TypeError):
        pass

    # Rework protection
    try:
        cfg.max_rework_cycles = max(1, int(_get(rework, "max_cycles", cfg.max_rework_cycles)))
    except (ValueError, TypeError):
        pass
    try:
        cfg.repeated_finding_threshold = max(1, int(_get(rework, "repeated_finding_threshold", cfg.repeated_finding_threshold)))
    except (ValueError, TypeError):
        pass

    # Gatekeeper evidence requirements
    cfg.require_test_evidence = bool(_get(gatekeeper, "require_test_evidence", cfg.require_test_evidence))
    cfg.require_build_success = bool(_get(gatekeeper, "require_build_success", cfg.require_build_success))
    cfg.require_regression_check = bool(_get(gatekeeper, "require_regression_check", cfg.require_regression_check))
    cfg.require_checklist_complete = bool(_get(gatekeeper, "require_checklist_complete", cfg.require_checklist_complete))
    cfg.deterministic_gates_first = bool(_get(gatekeeper, "deterministic_gates_first", cfg.deterministic_gates_first))
    cfg.skip_adversarial_for_trivial = bool(_get(review, "skip_adversarial_for_trivial", cfg.skip_adversarial_for_trivial))

    # Environment overrides (highest precedence)
    cfg.blind_review_enabled = _env_bool("CODEBOT_REVIEW_BLIND", cfg.blind_review_enabled)
    cfg.adversarial_review_enabled = _env_bool("CODEBOT_REVIEW_ADVERSARIAL", cfg.adversarial_review_enabled)
    cfg.multi_reviewer_enabled = _env_bool("CODEBOT_REVIEW_MULTI", cfg.multi_reviewer_enabled)
    cfg.random_audit_percentage = _env_int("CODEBOT_REVIEW_AUDIT_PCT", cfg.random_audit_percentage)
    cfg.max_rework_cycles = _env_int("CODEBOT_REWORK_MAX_CYCLES", cfg.max_rework_cycles)

    return cfg


# Module-level singleton so the dispatcher/gatekeeper share one instance.
_config_instance: ReviewConfig | None = None


def get_review_config(path: Path | None = None) -> ReviewConfig:
    """Return the process-wide ReviewConfig, loading it on first call."""
    global _config_instance
    if _config_instance is None:
        _config_instance = load_review_config(path)
    return _config_instance


def reset_review_config() -> None:
    """Clear the cached singleton (used by tests and config reload)."""
    global _config_instance
    _config_instance = None