#!/usr/bin/env python3
"""Deterministic escalation rules and specialist routing.

Purpose
-------
Defines which specialist reviewers are required based on ticket properties
and diff characteristics. Resolves specialist demand when multiple sources
(deterministic triggers + primary ESCALATE) compete for limited slots.

Invariants
----------
- stdlib-only (re, pathlib)
- Role names are resolved via SPECIALIST_ROLES map, never constructed dynamically
- Keywords alone never trigger escalation; structural signals are primary
- Specialist cap is enforced deterministically with suppression recording
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SPECIALIST_ROLES: dict[str, str] = {
    "security": "security_reviewer",
    "concurrency": "concurrency_reviewer",
    "architecture": "architecture_reviewer",
    "performance": "performance_reviewer",
    "data_integrity": "data_integrity_reviewer",
}

VALID_SPECIALIST_TYPES: frozenset[str] = frozenset(SPECIALIST_ROLES.keys())

MAX_SPECIALISTS_DEFAULT = 2

PRIORITY_CRITICAL_DETERMINISTIC = 0
PRIORITY_PRIMARY_EVIDENCE = 1
PRIORITY_STANDARD_DETERMINISTIC = 2

_SECURITY_PATH_PATTERNS: tuple[str, ...] = (
    "auth", "credential", "secret", "password", "token_validat",
    "session", "login", "signup", "oauth", "jwt",
)

_CONCURRENCY_PATH_PATTERNS: tuple[str, ...] = (
    "lock", "claim", "scheduler", "dispatch", "mutex", "semaphore",
)

_DATA_INTEGRITY_PATH_PATTERNS: tuple[str, ...] = (
    "migration", "schema", "model",
)

_ARCHITECTURE_PATH_PATTERNS: tuple[str, ...] = (
    "core/", "api/", "interface", "abstraction",
)

_PERFORMANCE_PATH_PATTERNS: tuple[str, ...] = (
    "hot_path", "fastpath", "cache",
)

_SECURITY_DIFF_ADDS: tuple[str, ...] = (
    "subprocess", "os.system", "os.popen", "shell=True",
    "eval(", "exec(", "__import__",
)

_CONCURRENCY_DIFF_ADDS: tuple[str, ...] = (
    "threading.Lock", "fcntl.flock", "asyncio.Lock",
    "multiprocessing", "RLock", "Semaphore",
)

_DATA_INTEGRITY_DIFF_CONTAINS: tuple[str, ...] = (
    "DROP TABLE", "DELETE FROM", "TRUNCATE",
    "ALTER TABLE", "drop_column", "remove_field",
)


@dataclass
class SpecialistDemand:
    origin: str
    reason: str
    priority: int = PRIORITY_STANDARD_DETERMINISTIC


@dataclass
class SpecialistResolution:
    required: dict[str, SpecialistDemand]
    suppressed: dict[str, dict[str, str]]


def _file_matches_patterns(filepath: str, patterns: tuple[str, ...]) -> bool:
    lower = filepath.lower()
    return any(p in lower for p in patterns)


def _diff_contains_adds(changed_lines: list[str], adds: tuple[str, ...]) -> bool:
    for line in changed_lines:
        stripped = line.lstrip("+").strip()
        if not stripped or stripped.startswith("#"):
            continue
        for pattern in adds:
            if pattern in stripped:
                return True
    return False


def get_deterministic_specialists(
    ticket: Any,
    changed_lines: list[str] | None = None,
) -> dict[str, SpecialistDemand]:
    demands: dict[str, SpecialistDemand] = {}
    affected = list(getattr(ticket, "affected_modules", []) or [])
    risk = str(getattr(getattr(ticket, "risk", "medium"), "value", getattr(ticket, "risk", "medium")).upper())
    security_impact = str(getattr(ticket, "security_impact", "none")).lower()
    lines = changed_lines or []

    for fpath in affected:
        if _file_matches_patterns(fpath, _SECURITY_PATH_PATTERNS):
            demands["security"] = SpecialistDemand(
                origin="deterministic",
                reason=f"security-sensitive path modified: {fpath}",
                priority=PRIORITY_CRITICAL_DETERMINISTIC,
            )
            break

    if _diff_contains_adds(lines, _SECURITY_DIFF_ADDS):
        demands["security"] = SpecialistDemand(
            origin="deterministic",
            reason="shell execution or dynamic eval added in diff",
            priority=PRIORITY_CRITICAL_DETERMINISTIC,
        )

    if security_impact in ("high", "critical"):
        demands.setdefault("security", SpecialistDemand(
            origin="deterministic",
            reason=f"ticket security_impact={security_impact}",
            priority=PRIORITY_CRITICAL_DETERMINISTIC,
        ))

    for fpath in affected:
        if _file_matches_patterns(fpath, _CONCURRENCY_PATH_PATTERNS):
            demands["concurrency"] = SpecialistDemand(
                origin="deterministic",
                reason=f"concurrency-sensitive path modified: {fpath}",
                priority=PRIORITY_STANDARD_DETERMINISTIC,
            )
            break

    if _diff_contains_adds(lines, _CONCURRENCY_DIFF_ADDS):
        demands["concurrency"] = SpecialistDemand(
            origin="deterministic",
            reason="threading/locking primitives added in diff",
            priority=PRIORITY_STANDARD_DETERMINISTIC,
        )

    for fpath in affected:
        if _file_matches_patterns(fpath, _DATA_INTEGRITY_PATH_PATTERNS):
            demands["data_integrity"] = SpecialistDemand(
                origin="deterministic",
                reason=f"data/schema path modified: {fpath}",
                priority=PRIORITY_CRITICAL_DETERMINISTIC,
            )
            break

    if _diff_contains_adds(lines, _DATA_INTEGRITY_DIFF_CONTAINS):
        demands["data_integrity"] = SpecialistDemand(
            origin="deterministic",
            reason="destructive data operation detected in diff",
            priority=PRIORITY_CRITICAL_DETERMINISTIC,
        )

    if risk == "CRITICAL":
        cross_module_count = sum(
            1 for fpath in affected
            if len(Path(fpath).parts) > 1
        )
        if cross_module_count >= 5:
            demands["architecture"] = SpecialistDemand(
                origin="deterministic",
                reason=f"CRITICAL risk with {cross_module_count} cross-module changes",
                priority=PRIORITY_STANDARD_DETERMINISTIC,
            )
        for fpath in affected:
            if _file_matches_patterns(fpath, _ARCHITECTURE_PATH_PATTERNS):
                demands.setdefault("architecture", SpecialistDemand(
                    origin="deterministic",
                    reason=f"core/api path modified at CRITICAL risk: {fpath}",
                    priority=PRIORITY_STANDARD_DETERMINISTIC,
                ))
                break

    for fpath in affected:
        if _file_matches_patterns(fpath, _PERFORMANCE_PATH_PATTERNS):
            demands["performance"] = SpecialistDemand(
                origin="deterministic",
                reason=f"performance-sensitive path modified: {fpath}",
                priority=PRIORITY_STANDARD_DETERMINISTIC,
            )
            break

    return demands


def resolve_specialist_cap(
    deterministic_demands: dict[str, SpecialistDemand],
    primary_escalation_type: str = "",
    primary_escalation_reason: str = "",
    max_specialists: int = MAX_SPECIALISTS_DEFAULT,
) -> SpecialistResolution:
    all_demands: list[tuple[str, SpecialistDemand]] = []

    for stype, demand in deterministic_demands.items():
        all_demands.append((stype, demand))

    if primary_escalation_type and primary_escalation_type in VALID_SPECIALIST_TYPES:
        already_present = any(s == primary_escalation_type for s, _ in all_demands)
        if not already_present:
            all_demands.append((primary_escalation_type, SpecialistDemand(
                origin="primary",
                reason=primary_escalation_reason or "primary reviewer escalation",
                priority=PRIORITY_PRIMARY_EVIDENCE,
            )))

    all_demands.sort(key=lambda x: x[1].priority)

    required: dict[str, SpecialistDemand] = {}
    suppressed: dict[str, dict[str, str]] = {}

    for stype, demand in all_demands:
        if len(required) < max_specialists:
            required[stype] = demand
        else:
            suppressed[stype] = {
                "reason": "specialist_cap",
                "origin": demand.origin,
            }

    return SpecialistResolution(required=required, suppressed=suppressed)


def validate_escalation(review: dict[str, Any]) -> tuple[bool, str]:
    verdict = str(review.get("verdict", review.get("decision", ""))).upper()
    if verdict != "ESCALATE":
        return True, ""

    specialist_type = str(review.get("specialist_type", "")).strip()
    if not specialist_type:
        return False, "missing specialist_type"
    if specialist_type not in VALID_SPECIALIST_TYPES:
        return False, f"invalid specialist_type: {specialist_type}"

    reason = str(review.get("specialist_reason", "")).strip()
    if not reason:
        return False, "missing specialist_reason"

    evidence = review.get("specialist_evidence", "")
    if not evidence:
        return False, "missing specialist_evidence"

    question = str(review.get("specialist_question", "")).strip()
    if not question:
        return False, "missing specialist_question"

    return True, ""


def specialist_role_for_type(specialist_type: str) -> str | None:
    return SPECIALIST_ROLES.get(specialist_type)
