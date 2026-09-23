#!/usr/bin/env python3
"""RL Failure Taxonomy — structured attribution for rework/review failures.

Purpose
-------
Defines failure_origin (11), failure_type (11), confidence levels, evidence
types, validators, and attribution extraction from reviewer feedback.

Invariants
----------
- stdlib-only.
- All validators normalize and map unknown to safe default.
- extract_attribution_from_reviewer_feedback picks highest-confidence
  explicit attribution among feedback items, falls back to keyword heuristics.
"""

from __future__ import annotations

import re
from typing import Any

FAILURE_ORIGINS: frozenset[str] = frozenset(
    {
        "DISCOVERY_ERROR",
        "GOAL_ERROR",
        "TRIAGE_ERROR",
        "DECOMPOSITION_ERROR",
        "PLANNING_ERROR",
        "IMPLEMENTATION_ERROR",
        "REVIEW_ERROR",
        "ENVIRONMENT_ERROR",
        "DEPENDENCY_CHANGE",
        "MODEL_FAILURE",
        "UNKNOWN",
    }
)

FAILURE_TYPES: frozenset[str] = frozenset(
    {
        "WRONG_ASSUMPTION",
        "MISSING_EDGE_CASE",
        "BAD_SCOPE",
        "DUPLICATE_WORK",
        "INCORRECT_CODE",
        "TEST_FAILURE",
        "SECURITY_REGRESSION",
        "PERFORMANCE_REGRESSION",
        "CONCURRENCY_BUG",
        "API_MISMATCH",
        "BUILD_FAILURE",
    }
)

CONFIDENCE_LEVELS: frozenset[str] = frozenset({"high", "medium", "low"})

EVIDENCE_TYPES: frozenset[str] = frozenset(
    {"DETERMINISTIC", "MODEL_JUDGMENT", "USER_FEEDBACK", "RUNTIME_OBSERVATION"}
)


def validate_origin(value: str | None) -> str:
    if not value or not isinstance(value, str):
        return "UNKNOWN"
    v = value.strip().upper()
    if v in FAILURE_ORIGINS:
        return v
    return "UNKNOWN"


def validate_type(value: str | None) -> str:
    if not value or not isinstance(value, str):
        return "UNKNOWN"
    v = value.strip().upper()
    if v in FAILURE_TYPES:
        return v
    return "UNKNOWN"


def validate_confidence(value: str | None) -> str:
    if not value or not isinstance(value, str):
        return "low"
    v = value.strip().lower()
    if v in CONFIDENCE_LEVELS:
        return v
    return "low"


def validate_evidence_type(value: str | None) -> str:
    if not value or not isinstance(value, str):
        return "MODEL_JUDGMENT"
    v = value.strip().upper()
    if v in EVIDENCE_TYPES:
        return v
    return "MODEL_JUDGMENT"


# Keyword heuristics for inferring attribution from free-text descriptions.
_ORIGIN_KEYWORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\bplan\b"), "PLANNING_ERROR"),
    (re.compile(r"concurrent|circular import|lazy.?load"), "PLANNING_ERROR"),
    (re.compile(r"\bvulnerab|sql inject|xss|csrf|auth\b"), "IMPLEMENTATION_ERROR"),
    (re.compile(r"\bdiscover|\btriage\b"), "DISCOVERY_ERROR"),
]

_TYPE_KEYWORDS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"sql inject|vulnerab|security|credential leak"), "SECURITY_REGRESSION"),
    (re.compile(r"edge case|empty input|empty list"), "MISSING_EDGE_CASE"),
    (re.compile(r"test fail"), "TEST_FAILURE"),
    (re.compile(r"build fail|compile error"), "BUILD_FAILURE"),
    (re.compile(r"api mismatch|wrong signature"), "API_MISMATCH"),
    (re.compile(r"concurren|race|deadlock"), "CONCURRENCY_BUG"),
    (re.compile(r"performance|latency|slow"), "PERFORMANCE_REGRESSION"),
    (re.compile(r"scope"), "BAD_SCOPE"),
]

_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _infer_from_text(text: str) -> dict[str, str]:
    lower = text.lower()
    origin = "UNKNOWN"
    ftype = "UNKNOWN"
    # Need to test original text too (patterns already lower via case-insensitive but compiled without flag,
    # so feed lower-case and use lowercase patterns)
    for pat, val in _ORIGIN_KEYWORDS:
        if pat.search(lower):
            origin = val
            break
    for pat, val in _TYPE_KEYWORDS:
        if pat.search(lower):
            ftype = val
            break
    # If still unknown origin but type suggests implementation, set implementation
    if origin == "UNKNOWN" and ftype in ("SECURITY_REGRESSION", "MISSING_EDGE_CASE", "INCORRECT_CODE"):
        origin = "IMPLEMENTATION_ERROR"
    return {"failure_origin": origin, "failure_type": ftype}


def extract_attribution_from_reviewer_feedback(
    feedback: list[dict[str, Any]] | None,
) -> dict[str, str]:
    if not feedback:
        return {"failure_origin": "UNKNOWN", "failure_type": "UNKNOWN", "confidence": "low", "evidence_type": "MODEL_JUDGMENT"}

    # First, collect candidates with explicit fields and score by confidence rank.
    candidates: list[tuple[int, dict[str, str]]] = []
    for item in feedback:
        if not isinstance(item, dict):
            continue
        has_explicit = any(k in item for k in ("failure_origin", "failure_type", "confidence", "evidence_type"))
        if has_explicit:
            origin = validate_origin(item.get("failure_origin", ""))
            ftype = validate_type(item.get("failure_type", ""))
            conf = validate_confidence(item.get("confidence", "low"))
            ev = validate_evidence_type(item.get("evidence_type", "MODEL_JUDGMENT"))
            # If explicit but resulted in UNKNOWN, treat as not truly explicit unless user intended UNKNOWN
            # Keep as is; rank by confidence
            rank = _CONFIDENCE_RANK.get(conf, 0)
            candidates.append((rank, {"failure_origin": origin, "failure_type": ftype, "confidence": conf, "evidence_type": ev}))

    if candidates:
        # Pick highest confidence; tie-break by first occurrence (stable max)
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    # Fall back to keyword heuristics on description fields
    best: dict[str, str] | None = None
    for item in feedback:
        if not isinstance(item, dict):
            continue
        desc = str(item.get("description", "") or "")
        if not desc.strip():
            continue
        inferred = _infer_from_text(desc)
        # confidence low, evidence MODEL_JUDGMENT when heuristic
        result = {
            "failure_origin": validate_origin(inferred["failure_origin"]),
            "failure_type": validate_type(inferred["failure_type"]),
            "confidence": "low",
            "evidence_type": "MODEL_JUDGMENT",
        }
        # Prefer inferred with more specific origin/type over UNKNOWN
        if best is None:
            best = result
        else:
            # Prefer one that has a real origin/type
            if best["failure_origin"] == "UNKNOWN" and result["failure_origin"] != "UNKNOWN":
                best = result
            elif best["failure_type"] == "UNKNOWN" and result["failure_type"] != "UNKNOWN":
                # Only upgrade type if origin not already better
                best = {**best, "failure_type": result["failure_type"]}

    if best is not None:
        # Ensure defaults not UNKNOWN/UNKNOWN become at least something sensible
        if best["failure_origin"] == "UNKNOWN" and best["failure_type"] != "UNKNOWN":
            best["failure_origin"] = "IMPLEMENTATION_ERROR"
        return best

    return {"failure_origin": "UNKNOWN", "failure_type": "UNKNOWN", "confidence": "low", "evidence_type": "MODEL_JUDGMENT"}
