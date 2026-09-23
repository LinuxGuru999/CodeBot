#!/usr/bin/env python3
"""Structured discovery finding representation.

Purpose
-------
Defines the v1 structured finding schema that every discovery role must
produce before a ticket may be created. A finding separates OBSERVATION
(what objectively exists) from INTERPRETATION (why the agent believes it
is problematic), IMPACT (what could happen), and CONFIDENCE (how certain
the agent is).

Why
---
Discovery agents previously emitted tickets directly from free-form
`create_ticket` calls with a single evidence string. That made duplicate
detection, stale-evidence rechecks, and hallucinated-reference detection
unreliable. This module gives discovery a typed contract: findings are
validated, safely repaired when trivially malformed, fingerprinted for
deduplication, and serialized into the findings log before any ticket is
created from them.

Invariants
----------
- stdlib-only (dataclasses, enum, hashlib, json, time, uuid)
- Findings are immutable once created; repair() returns a new instance
- validate() raises FindingValidationError for unrepairable problems
- Severity (how bad) and priority (how soon) are distinct axes
- Evidence items always carry an observation; repair() never fabricates
  interpretation or impact
- Category vocabulary mirrors ticket_engine.TicketClass exactly
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1.0"

MAX_TITLE_LENGTH = 200
MAX_EVIDENCE_ITEMS = 50
MAX_EXCERPT_LENGTH = 2000


class Confidence(str, Enum):
    """How certain the discovering agent is that the finding is real."""
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Atomicity(str, Enum):
    """Whether a finding is a single work unit or needs decomposition."""
    ATOMIC = "atomic"
    COMPOUND = "compound"
    UNKNOWN = "unknown"


class ScopeEstimate(str, Enum):
    SMALL = "small"
    MEDIUM = "medium"
    LARGE = "large"
    UNKNOWN = "unknown"


class DuplicateRelation(str, Enum):
    """Relationship between a finding and an existing ticket/finding."""
    EXACT_DUPLICATE = "exact_duplicate"
    OVERLAPPING = "overlapping"
    RELATED = "related"
    SUPERSEDED = "superseded"
    DISTINCT = "distinct"


class FindingRelation(str, Enum):
    """Root-cause relationship between findings (§11 of discovery spec)."""
    POSSIBLY_CAUSED_BY = "possibly_caused_by"
    CAUSES = "causes"
    RELATED_TO = "related_to"
    DUPLICATES = "duplicates"
    SUPERSEDES = "supersedes"
    BLOCKED_BY = "blocked_by"


# Mirrors ticket_engine.TicketClass values exactly. Kept local so this
# module imports without pulling in the heavyweight ticket store.
FINDING_CATEGORIES: frozenset[str] = frozenset({
    "bug", "feature", "security", "performance", "documentation",
    "test", "refactor", "dependency", "architecture", "infrastructure",
})

SEVERITY_VALUES: frozenset[str] = frozenset({"critical", "high", "medium", "low"})

# Evidence kinds that qualify as concrete proof (support HIGH confidence).
CONCRETE_EVIDENCE_KINDS: frozenset[str] = frozenset({
    "failing_test", "runtime_behavior", "benchmark", "dependency_metadata",
    "static_analysis", "insecure_pattern",
})

EVIDENCE_KINDS: frozenset[str] = frozenset({
    "code_reference", "failing_test", "missing_test", "static_analysis",
    "dependency_metadata", "runtime_behavior", "benchmark", "unsafe_path",
    "state_transition", "dead_code", "api_inconsistency", "doc_disagreement",
    "architectural_duplication", "insecure_pattern", "configuration",
    "ux_inconsistency", "other",
})

_STOPWORDS: frozenset[str] = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "could", "did",
    "do", "does", "for", "from", "has", "have", "in", "is", "it", "its",
    "may", "might", "no", "not", "of", "on", "or", "should", "so", "than",
    "that", "the", "their", "then", "there", "these", "this", "to", "was",
    "were", "when", "which", "while", "will", "with", "would",
    "via", "allows", "allow", "causes", "causing", "caused", "problem",
})

_WORD_RE = re.compile(r"[a-z0-9_]+")


class FindingValidationError(ValueError):
    """Raised when a finding cannot be validated or repaired."""


def generate_finding_id(prefix: str = "DF") -> str:
    """Generate a globally unique finding ID (mirrors generate_ticket_id)."""
    return f"{prefix}-{uuid.uuid4().hex[:12].upper()}"


def normalize_text(text: str) -> frozenset[str]:
    """Lowercase word set with stopwords removed, for similarity checks."""
    words = _WORD_RE.findall(text.lower())
    return frozenset(w for w in words if w not in _STOPWORDS and len(w) >= 2)


@dataclass(frozen=True)
class EvidenceItem:
    """One piece of evidence, separating observation from interpretation.

    observation    — what objectively exists (REQUIRED, non-empty)
    interpretation — why the agent believes it is problematic
    impact         — what could happen because of it
    """
    observation: str
    interpretation: str = ""
    impact: str = ""
    file_path: str = ""
    line_number: int = 0
    symbol: str = ""
    excerpt: str = ""
    kind: str = "code_reference"

    def __post_init__(self) -> None:
        if len(self.excerpt) > MAX_EXCERPT_LENGTH:
            object.__setattr__(self, "excerpt", self.excerpt[:MAX_EXCERPT_LENGTH])

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "EvidenceItem":
        try:
            line = int(data.get("line_number", 0) or 0)
        except (TypeError, ValueError):
            line = 0
        kind = str(data.get("kind", "") or "").strip().lower()
        if kind not in EVIDENCE_KINDS:
            kind = "code_reference"
        return cls(
            observation=str(data.get("observation", "") or "").strip(),
            interpretation=str(data.get("interpretation", "") or "").strip(),
            impact=str(data.get("impact", "") or "").strip(),
            file_path=str(data.get("file_path", "") or "").strip(),
            line_number=max(0, line),
            symbol=str(data.get("symbol", "") or "").strip(),
            excerpt=str(data.get("excerpt", "") or "")[:MAX_EXCERPT_LENGTH],
            kind=kind,
        )

    def has_concrete_proof(self) -> bool:
        return self.kind in CONCRETE_EVIDENCE_KINDS

    def is_valid_path(self) -> bool:
        """Paths must be relative and contain no traversal components."""
        if not self.file_path:
            return True  # path optional; observation text may suffice
        if self.file_path.startswith("/") or self.file_path.startswith("\\"):
            return False
        if ".." in self.file_path.split("/") or ".." in self.file_path.split("\\"):
            return False
        return True


@dataclass(frozen=True)
class DuplicateCandidate:
    """A possibly-duplicate existing ticket or finding."""
    target_id: str
    relation: str = DuplicateRelation.RELATED.value
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DuplicateCandidate":
        relation = str(data.get("relation", "") or "").strip().lower()
        valid = {r.value for r in DuplicateRelation}
        if relation not in valid:
            relation = DuplicateRelation.RELATED.value
        return cls(
            target_id=str(data.get("target_id", "") or "").strip(),
            relation=relation,
            note=str(data.get("note", "") or "").strip(),
        )


@dataclass(frozen=True)
class FindingRelationship:
    """A root-cause relationship to another finding or ticket."""
    relation: str
    target_id: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FindingRelationship":
        relation = str(data.get("relation", "") or "").strip().lower()
        valid = {r.value for r in FindingRelation}
        if relation not in valid:
            relation = FindingRelation.RELATED_TO.value
        return cls(
            relation=relation,
            target_id=str(data.get("target_id", "") or "").strip(),
            note=str(data.get("note", "") or "").strip(),
        )


@dataclass(frozen=True)
class DiscoveryFinding:
    """Structured discovery output contract (§2-3 of discovery spec)."""
    finding_id: str
    discovery_role: str
    discovery_category: str
    title: str
    problem_statement: str
    severity: str
    priority: str
    confidence: str
    atomicity: str
    repository: str
    repository_revision: str
    evidence: list[EvidenceItem]
    acceptance_outcome: str
    observed_behavior: str = ""
    expected_behavior: str = ""
    scope_estimate: str = ScopeEstimate.UNKNOWN.value
    affected_components: list[str] = field(default_factory=list)
    affected_files: list[str] = field(default_factory=list)
    affected_symbols: list[str] = field(default_factory=list)
    discovery_method: str = ""
    related_tickets: list[str] = field(default_factory=list)
    duplicate_candidates: list[DuplicateCandidate] = field(default_factory=list)
    relationships: list[FindingRelationship] = field(default_factory=list)
    created_at: float = 0.0
    schema_version: str = SCHEMA_VERSION

    # -- identity / dedup -------------------------------------------------

    def normalized_problem_statement(self) -> str:
        """Canonical word-sorted form used for semantic dedup."""
        return " ".join(sorted(normalize_text(self.problem_statement)))

    def fingerprint(self) -> str:
        """Stable content fingerprint for duplicate detection.

        Combines category, normalized problem statement, and affected
        files. Two findings about the same problem in the same place
        converge on the same fingerprint even when worded differently.
        """
        files = sorted(set(f.strip() for f in self.affected_files if f.strip()))
        canonical = (
            f"{self.discovery_category}:"
            f"{self.normalized_problem_statement()}:"
            f"{','.join(files)}"
        )
        return hashlib.sha256(canonical.encode()).hexdigest()[:16]

    def confidence_is_supported(self) -> bool:
        """HIGH confidence requires at least one concrete evidence kind."""
        if self.confidence != Confidence.HIGH.value:
            return True
        return any(e.has_concrete_proof() for e in self.evidence)

    # -- validation -------------------------------------------------------

    def validate(self) -> None:
        """Raise FindingValidationError if structurally invalid.

        Callers should prefer from_agent_output(), which repairs trivial
        problems before validating.
        """
        if not self.title or not self.title.strip():
            raise FindingValidationError("title is required")
        if len(self.title) > MAX_TITLE_LENGTH:
            raise FindingValidationError(
                f"title exceeds {MAX_TITLE_LENGTH} chars"
            )
        if not self.problem_statement.strip():
            raise FindingValidationError("problem_statement is required")
        if not self.discovery_role.strip():
            raise FindingValidationError("discovery_role is required")
        if self.discovery_category not in FINDING_CATEGORIES:
            raise FindingValidationError(
                f"unknown discovery_category: {self.discovery_category!r}"
            )
        if self.severity not in SEVERITY_VALUES:
            raise FindingValidationError(f"invalid severity: {self.severity!r}")
        if self.priority not in SEVERITY_VALUES:
            raise FindingValidationError(f"invalid priority: {self.priority!r}")
        if self.confidence not in {c.value for c in Confidence}:
            raise FindingValidationError(f"invalid confidence: {self.confidence!r}")
        if self.atomicity not in {a.value for a in Atomicity}:
            raise FindingValidationError(f"invalid atomicity: {self.atomicity!r}")
        if not self.evidence:
            raise FindingValidationError(
                "at least one evidence item is required"
            )
        for item in self.evidence:
            if not item.observation and not item.excerpt:
                raise FindingValidationError(
                    "evidence item needs observation or excerpt"
                )
            if not item.is_valid_path():
                raise FindingValidationError(
                    f"evidence path unsafe or absolute: {item.file_path!r}"
                )
        if not self.acceptance_outcome.strip():
            raise FindingValidationError(
                "acceptance_outcome is required (observable resolution)"
            )

    # -- serialization ----------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "schema_version": self.schema_version,
            "finding_id": self.finding_id,
            "discovery_role": self.discovery_role,
            "discovery_category": self.discovery_category,
            "title": self.title,
            "problem_statement": self.problem_statement,
            "severity": self.severity,
            "priority": self.priority,
            "confidence": self.confidence,
            "atomicity": self.atomicity,
            "scope_estimate": self.scope_estimate,
            "repository": self.repository,
            "repository_revision": self.repository_revision,
            "observed_behavior": self.observed_behavior,
            "expected_behavior": self.expected_behavior,
            "affected_components": list(self.affected_components),
            "affected_files": list(self.affected_files),
            "affected_symbols": list(self.affected_symbols),
            "discovery_method": self.discovery_method,
            "related_tickets": list(self.related_tickets),
            "acceptance_outcome": self.acceptance_outcome,
            "created_at": self.created_at,
            "evidence": [e.to_dict() for e in self.evidence],
            "duplicate_candidates": [c.to_dict() for c in self.duplicate_candidates],
            "relationships": [r.to_dict() for r in self.relationships],
            "fingerprint": self.fingerprint(),
        }
        return d

    def to_json(self, *, pretty: bool = False) -> str:
        if pretty:
            return json.dumps(self.to_dict(), indent=2)
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DiscoveryFinding":
        evidence = [
            EvidenceItem.from_dict(e)
            for e in data.get("evidence", [])
            if isinstance(e, dict)
        ]
        dupes = [
            DuplicateCandidate.from_dict(c)
            for c in data.get("duplicate_candidates", [])
            if isinstance(c, dict)
        ]
        rels = [
            FindingRelationship.from_dict(r)
            for r in data.get("relationships", [])
            if isinstance(r, dict)
        ]

        def _str_list(key: str) -> list[str]:
            raw = data.get(key, [])
            if isinstance(raw, str):
                raw = [s for s in raw.split(",")]
            if not isinstance(raw, list):
                raw = []
            return [str(x).strip() for x in raw if str(x).strip()]

        try:
            created_at = float(data.get("created_at", 0.0) or 0.0)
        except (TypeError, ValueError):
            created_at = 0.0

        scope = str(data.get("scope_estimate", "") or "").strip().lower()
        if scope not in {s.value for s in ScopeEstimate}:
            scope = ScopeEstimate.UNKNOWN.value

        return cls(
            finding_id=str(data.get("finding_id", "") or generate_finding_id()),
            discovery_role=str(data.get("discovery_role", "") or "").strip(),
            discovery_category=str(
                data.get("discovery_category", "") or ""
            ).strip().lower(),
            title=str(data.get("title", "") or "").strip(),
            problem_statement=str(
                data.get("problem_statement", "") or ""
            ).strip(),
            severity=str(data.get("severity", "") or "").strip().lower(),
            priority=str(
                data.get("priority", "") or data.get("severity", "") or ""
            ).strip().lower(),
            confidence=str(data.get("confidence", "") or "").strip().lower(),
            atomicity=str(data.get("atomicity", "") or "").strip().lower()
            or Atomicity.UNKNOWN.value,
            scope_estimate=scope,
            repository=str(data.get("repository", "") or "").strip(),
            repository_revision=str(
                data.get("repository_revision", "") or ""
            ).strip(),
            observed_behavior=str(
                data.get("observed_behavior", "") or ""
            ).strip(),
            expected_behavior=str(
                data.get("expected_behavior", "") or ""
            ).strip(),
            affected_components=_str_list("affected_components"),
            affected_files=_str_list("affected_files"),
            affected_symbols=_str_list("affected_symbols"),
            discovery_method=str(
                data.get("discovery_method", "") or ""
            ).strip(),
            related_tickets=_str_list("related_tickets"),
            acceptance_outcome=str(
                data.get("acceptance_outcome", "") or ""
            ).strip(),
            evidence=evidence,
            duplicate_candidates=dupes,
            relationships=rels,
            created_at=created_at,
            schema_version=str(data.get("schema_version", SCHEMA_VERSION)),
        )

    @classmethod
    def from_json(cls, raw: str) -> "DiscoveryFinding":
        return cls.from_dict(json.loads(raw))


def _repair_raw(data: dict[str, Any]) -> dict[str, Any]:
    """Apply trivial, safe repairs to raw agent output before parsing.

    Repairs are limited to: coercion of case/whitespace, enum fallbacks,
    title truncation, and list coercion from comma-separated strings.
    Nothing is fabricated — missing required fields stay missing so
    validate() rejects them.
    """
    d = dict(data)

    title = str(d.get("title", "") or "").strip()
    if len(title) > MAX_TITLE_LENGTH:
        title = title[:MAX_TITLE_LENGTH]
    d["title"] = title

    if not d.get("discovery_category") and d.get("ticket_class"):
        d["discovery_category"] = str(d.get("ticket_class", "") or "").strip().lower()

    for key in ("severity", "priority", "confidence", "atomicity",
                "scope_estimate", "discovery_category"):
        if key in d:
            d[key] = str(d[key] or "").strip().lower()

    if d.get("severity") not in SEVERITY_VALUES:
        d["severity"] = "medium"
    if not d.get("priority"):
        d["priority"] = d.get("severity")
    if d.get("confidence") not in {c.value for c in Confidence}:
        d["confidence"] = Confidence.MEDIUM.value
    if d.get("atomicity") not in {a.value for a in Atomicity}:
        d["atomicity"] = Atomicity.UNKNOWN.value

    # Accept legacy flat evidence string as a single code_reference item.
    evidence = d.get("evidence")
    if isinstance(evidence, str) and evidence.strip():
        d["evidence"] = [{"observation": evidence.strip(),
                          "kind": "code_reference"}]
    elif isinstance(evidence, list):
        d["evidence"] = [
            {"observation": str(e).strip(), "kind": "code_reference"}
            if isinstance(e, str) else e
            for e in evidence
            if e
        ][:MAX_EVIDENCE_ITEMS]

    for key in ("affected_components", "affected_files",
                "affected_symbols", "related_tickets"):
        val = d.get(key)
        if isinstance(val, str):
            d[key] = [s.strip() for s in val.split(",") if s.strip()]

    if not d.get("acceptance_outcome") and d.get("desired_state"):
        d["acceptance_outcome"] = str(d.get("desired_state", "")).strip()

    return d


def finding_from_agent_output(
    data: dict[str, Any],
    *,
    role: str,
    repository: str = "",
    repository_revision: str = "",
) -> DiscoveryFinding:
    """Parse, repair, and validate raw discovery agent output.

    This is the ONLY sanctioned path from model output to finding object
    (§36: never assume model output is structurally valid).

    Raises:
        FindingValidationError: when required fields are missing/invalid
            and cannot be safely repaired.
    """
    if not isinstance(data, dict):
        raise FindingValidationError("finding payload must be a JSON object")
    repaired = _repair_raw(data)
    repaired.setdefault("discovery_role", role)
    if not repaired.get("discovery_role"):
        repaired["discovery_role"] = role
    repaired.setdefault("repository", repository)
    repaired.setdefault("repository_revision", repository_revision)
    repaired.setdefault("finding_id", generate_finding_id())
    repaired.setdefault("created_at", time.time())
    finding = DiscoveryFinding.from_dict(repaired)
    finding.validate()
    return finding
