#!/usr/bin/env python3
"""Adversarial review and gatekeeping core types.

Purpose
-------
Defines the structured vocabulary for evidence-driven adversarial review and
independent final gatekeeping: finding severities, checklist outcomes, review
phases, gate decisions, risk classes, and the machine-readable artifacts that
carry them. Every REVIEWING and VERIFYING decision flows through these types so
that completion is earned by objective evidence, not reviewer agreement.

Why
---
The legacy review path produced near-zero rejection rates because verdicts were
free-form strings ("APPROVE"/"REWORK") with no enforced severity, no required
evidence, and no completion-blocking semantics. A reviewer could approve with
"looks correct" and the gatekeeper had no structured findings to challenge.
These types make evidence mandatory and make high-severity findings impossible
to ignore.

Invariants
----------
- stdlib-only (json, enum, dataclasses, time, pathlib)
- Severities are totally ordered; BLOCKER > CRITICAL > MAJOR > MINOR > NIT > INFO
- Completion is blocked while any unresolved finding at or above the configured
  blocking threshold remains (default: BLOCKER, CRITICAL, MAJOR)
- ChecklistResult.UNKNOWN never behaves like PASS
- All types serialize to/from plain JSON dicts for persistence in STATE_DIR
- No I/O and no side effects in this module — pure data definitions
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class FindingSeverity(str, Enum):
    """Severity of a review finding, ordered from most to least severe.

    BLOCKER and CRITICAL always block completion. MAJOR blocks by default
    (configurable). MINOR/NIT/INFO never block but must be recorded.
    """

    BLOCKER = "BLOCKER"
    CRITICAL = "CRITICAL"
    MAJOR = "MAJOR"
    MINOR = "MINOR"
    NIT = "NIT"
    INFO = "INFO"


# Total ordering: lower index = more severe. Used for threshold comparison.
_SEVERITY_ORDER: dict[FindingSeverity, int] = {
    FindingSeverity.BLOCKER: 0,
    FindingSeverity.CRITICAL: 1,
    FindingSeverity.MAJOR: 2,
    FindingSeverity.MINOR: 3,
    FindingSeverity.NIT: 4,
    FindingSeverity.INFO: 5,
}

# Default severities that block completion. Configurable via ReviewConfig.
DEFAULT_BLOCKING_SEVERITIES: frozenset[FindingSeverity] = frozenset({
    FindingSeverity.BLOCKER,
    FindingSeverity.CRITICAL,
    FindingSeverity.MAJOR,
})


def severity_rank(sev: FindingSeverity) -> int:
    """Return the ordering rank of a severity (0 = most severe)."""
    return _SEVERITY_ORDER.get(sev, len(_SEVERITY_ORDER))


def severity_at_least(sev: FindingSeverity, threshold: FindingSeverity) -> bool:
    """True if `sev` is at least as severe as `threshold`.

    e.g. severity_at_least(CRITICAL, MAJOR) -> True because CRITICAL > MAJOR.
    """
    return severity_rank(sev) <= severity_rank(threshold)


def parse_severity(raw: str) -> FindingSeverity:
    """Parse a severity string, tolerating case and legacy values.

    Unknown values degrade to INFO rather than raising, so a malformed review
    artifact can never crash the pipeline. Legacy lowercase severities from the
    old reviewer JSON schema (critical/high/medium/low) are mapped upward.
    """
    if not raw:
        return FindingSeverity.INFO
    norm = raw.strip().upper()
    try:
        return FindingSeverity(norm)
    except ValueError:
        pass
    legacy = {
        "CRITICAL": FindingSeverity.CRITICAL,
        "HIGH": FindingSeverity.MAJOR,
        "MEDIUM": FindingSeverity.MINOR,
        "LOW": FindingSeverity.NIT,
    }
    return legacy.get(norm, FindingSeverity.INFO)


class ChecklistResult(str, Enum):
    """Outcome of a single mandatory review checklist item.

    UNKNOWN must never be treated as PASS. Critical unknowns should trigger
    rework or further investigation, not silent approval.
    """

    PASS = "PASS"
    FAIL = "FAIL"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    UNKNOWN = "UNKNOWN"


# Checklist items that every substantive implementation must be evaluated
# against. Reviewers mark each PASS / FAIL / NOT_APPLICABLE / UNKNOWN.
MANDATORY_CHECKLIST_ITEMS: tuple[str, ...] = (
    "requirement_satisfied",
    "acceptance_criteria_satisfied",
    "existing_behavior_preserved",
    "relevant_tests_pass",
    "new_behavior_has_tests",
    "error_paths_tested",
    "boundary_conditions_considered",
    "security_implications_considered",
    "performance_implications_considered",
    "concurrency_implications_considered",
    "architecture_consistent",
    "no_unnecessary_scope_expansion",
    "no_dead_code_introduced",
    "logging_error_handling_appropriate",
    "documentation_updated_when_needed",
    "dependency_changes_justified",
    "no_obvious_regressions",
)


class ReviewPhase(str, Enum):
    """Phase within the REVIEWING/VERIFYING lifecycle.

    Adversarial review and final gate are phases, not separate ticket states,
    so the existing IMPLEMENTING->REVIEWING->VERIFYING->COMPLETE pipeline is
    preserved. Phase is carried as ticket metadata (scratchpad / review files).
    """

    INDEPENDENT_REVIEW = "INDEPENDENT_REVIEW"
    ADVERSARIAL_REVIEW = "ADVERSARIAL_REVIEW"
    VERIFYING = "VERIFYING"
    FINAL_GATE = "FINAL_GATE"


class GateDecision(str, Enum):
    """Final gatekeeper verdict."""

    COMPLETE = "COMPLETE"
    REWORK = "REWORK"
    BLOCKED = "BLOCKED"
    NEEDS_HUMAN = "NEEDS_HUMAN"


class RiskClass(str, Enum):
    """Change-risk classification assigned before final verification."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


# Numeric risk-score thresholds (from risk_classifier.classify_risk, 0-100)
# that map to each RiskClass. Kept here so review routing and risk_classifier
# agree on the same boundaries.
RISK_CLASS_THRESHOLDS: tuple[tuple[int, RiskClass], ...] = (
    (70, RiskClass.CRITICAL),
    (45, RiskClass.HIGH),
    (20, RiskClass.MEDIUM),
    (0, RiskClass.LOW),
)


def risk_class_for_score(score: int) -> RiskClass:
    """Map a 0-100 risk score to a RiskClass."""
    for threshold, rc in RISK_CLASS_THRESHOLDS:
        if score >= threshold:
            return rc
    return RiskClass.LOW


@dataclass
class StructuredFinding:
    """A single evidence-backed review finding.

    Vague rejections ("needs improvement", "could be cleaner") are not
    representable: location, evidence, and expected/actual behavior are
    first-class fields. Reproduction steps are required where practical.
    """

    severity: FindingSeverity
    category: str
    finding: str
    file: str = ""
    location: str = ""
    evidence: str = ""
    reproduction: str = ""
    expected: str = ""
    actual: str = ""
    recommended_fix: str = ""
    resolved: bool = False
    reviewer: str = ""
    created_at: float = field(default_factory=time.time)

    def is_blocking(self, blocking: frozenset[FindingSeverity] = DEFAULT_BLOCKING_SEVERITIES) -> bool:
        """True if this finding blocks completion at the given threshold set."""
        return (not self.resolved) and self.severity in blocking

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["severity"] = self.severity.value
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> StructuredFinding:
        data = dict(data)
        data["severity"] = parse_severity(str(data.get("severity", "")))
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class ReviewChecklist:
    """Structured results for every mandatory checklist item.

    Items not explicitly evaluated default to UNKNOWN, which never behaves as
    PASS. A reviewer that submits a checklist without filling it in cannot
    silently approve the change.
    """

    items: dict[str, ChecklistResult] = field(default_factory=dict)
    notes: dict[str, str] = field(default_factory=dict)

    def result_for(self, item: str) -> ChecklistResult:
        return self.items.get(item, ChecklistResult.UNKNOWN)

    def set(self, item: str, result: ChecklistResult, note: str = "") -> None:
        self.items[item] = result
        if note:
            self.notes[item] = note

    def unresolved_unknowns(self) -> list[str]:
        """Checklist items still marked UNKNOWN (never treated as PASS)."""
        return [k for k, v in self.items.items() if v == ChecklistResult.UNKNOWN]

    def failed_items(self) -> list[str]:
        return [k for k, v in self.items.items() if v == ChecklistResult.FAIL]

    def is_complete(self) -> bool:
        """True only if every mandatory item has an explicit non-UNKNOWN result."""
        for item in MANDATORY_CHECKLIST_ITEMS:
            if self.result_for(item) == ChecklistResult.UNKNOWN:
                return False
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": {k: v.value for k, v in self.items.items()},
            "notes": dict(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewChecklist:
        items: dict[str, ChecklistResult] = {}
        for k, v in (data.get("items") or {}).items():
            try:
                items[k] = ChecklistResult(str(v).upper())
            except ValueError:
                items[k] = ChecklistResult.UNKNOWN
        return cls(items=items, notes=dict(data.get("notes") or {}))


@dataclass
class CompletionEvidence:
    """Machine-readable evidence artifact required for a COMPLETE decision.

    Arbitrary confidence numbers do not substitute for evidence. Each field is
    populated from deterministic verification (tests, build, review findings)
    rather than from an LLM's self-reported certainty.
    """

    requirement_verified: bool = False
    acceptance_passed: int = 0
    acceptance_failed: int = 0
    acceptance_unknown: int = 0
    tests_executed: int = 0
    tests_passed: int = 0
    tests_failed: int = 0
    new_tests_added: int = 0
    finding_counts: dict[str, int] = field(default_factory=dict)
    build_verified: bool = False
    security_checked: bool = False
    regression_checked: bool = False
    deterministic_gates_passed: bool = False
    checklist_complete: bool = False
    completion_confidence: float = 0.0

    def unresolved_blocking_findings(self, blocking: frozenset[FindingSeverity] = DEFAULT_BLOCKING_SEVERITIES) -> int:
        """Count of unresolved findings at/above the blocking threshold."""
        total = 0
        for sev in blocking:
            total += int(self.finding_counts.get(sev.value, 0))
        return total

    def has_missing_evidence(self) -> bool:
        """True when evidence required for COMPLETE is absent or failing."""
        if not self.requirement_verified:
            return True
        if self.acceptance_failed > 0 or self.acceptance_unknown > 0:
            return True
        if self.tests_failed > 0:
            return True
        if not self.build_verified:
            return True
        if not self.checklist_complete:
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CompletionEvidence:
        valid_keys = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in dict(data).items() if k in valid_keys}
        return cls(**filtered)


@dataclass
class ReviewDecision:
    """Aggregate verdict from one reviewer pass (independent or adversarial).

    The verdict string alone is insufficient for gating; consumers must inspect
    `findings`, `checklist`, and `phase`. A verdict of APPROVE that still
    carries an unresolved BLOCKER finding is invalid and must be treated as
    REWORK by the gatekeeper.
    """

    verdict: str  # "APPROVE" | "REWORK" | "ESCALATE"
    phase: ReviewPhase
    reviewer: str
    ticket_id: str = ""
    findings: list[StructuredFinding] = field(default_factory=list)
    checklist: ReviewChecklist = field(default_factory=ReviewChecklist)
    summary: str = ""
    completed_at: float = field(default_factory=time.time)

    def blocking_findings(self, blocking: frozenset[FindingSeverity] = DEFAULT_BLOCKING_SEVERITIES) -> list[StructuredFinding]:
        return [f for f in self.findings if f.is_blocking(blocking)]

    def effective_verdict(self, blocking: frozenset[FindingSeverity] = DEFAULT_BLOCKING_SEVERITIES) -> str:
        """Verdict after enforcing that blocking findings override APPROVE."""
        if self.blocking_findings(blocking):
            return "REWORK"
        if self.verdict.upper() in ("APPROVE", "REWORK", "ESCALATE"):
            return self.verdict.upper()
        return "REWORK"

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "phase": self.phase.value,
            "reviewer": self.reviewer,
            "ticket_id": self.ticket_id,
            "findings": [f.to_dict() for f in self.findings],
            "checklist": self.checklist.to_dict(),
            "summary": self.summary,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ReviewDecision:
        data = dict(data)
        try:
            phase = ReviewPhase(str(data.get("phase", ReviewPhase.INDEPENDENT_REVIEW.value)).upper())
        except ValueError:
            phase = ReviewPhase.INDEPENDENT_REVIEW
        findings = [StructuredFinding.from_dict(f) for f in (data.get("findings") or []) if isinstance(f, dict)]
        checklist = ReviewChecklist.from_dict(data.get("checklist") or {})
        return cls(
            verdict=str(data.get("verdict", "REWORK")),
            phase=phase,
            reviewer=str(data.get("reviewer", "")),
            ticket_id=str(data.get("ticket_id", "")),
            findings=findings,
            checklist=checklist,
            summary=str(data.get("summary", "")),
            completed_at=float(data.get("completed_at", time.time())),
        )


def highest_unresolved_severity(findings: list[StructuredFinding]) -> FindingSeverity | None:
    """Return the most severe unresolved finding, or None if clean."""
    unresolved = [f for f in findings if not f.resolved]
    if not unresolved:
        return None
    return min(unresolved, key=lambda f: severity_rank(f.severity)).severity