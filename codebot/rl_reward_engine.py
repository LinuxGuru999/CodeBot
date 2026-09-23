#!/usr/bin/env python3
"""RL Reward Engine — multi-dimensional fitness evaluation.

Purpose
-------
Computes 9 reward dimensions for TicketOutcome. Provides RewardVector,
per-dimension computers, vector aggregation, confidence, and
update_outcome_reward.

Invariants
----------
- stdlib-only.
- Each dimension in [0,1].
- 9 dimensions: usefulness, correctness, first_pass_quality, stability,
  cost_efficiency, speed, implementation_efficiency, human_override, goal_value.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from codebot.rl_outcome_aggregator import TicketOutcome

REWARD_SCHEMA_VERSION = 1

DIMENSIONS = (
    "usefulness",
    "correctness",
    "first_pass_quality",
    "stability",
    "cost_efficiency",
    "speed",
    "implementation_efficiency",
    "human_override",
    "goal_value",
)

OBSERVATION_WINDOWS = {
    "immediate": 3600.0,
    "24h": 24 * 3600.0,
    "72h": 72 * 3600.0,
    "7d": 7 * 24 * 3600.0,
}

def _clamp(v: float) -> float:
    return max(0.0, min(1.0, float(v)))

@dataclass
class RewardVector:
    usefulness: float = 0.0
    correctness: float = 0.0
    first_pass_quality: float = 0.0
    stability: float = 0.0
    cost_efficiency: float = 0.0
    speed: float = 0.0
    implementation_efficiency: float = 0.0
    human_override: float = 0.0
    goal_value: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return {k: getattr(self, k) for k in DIMENSIONS}

    def scalar(self, weights: dict[str, float] | None = None) -> float:
        if not weights:
            # uniform average over 9 dims
            vals = [getattr(self, d) for d in DIMENSIONS]
            return sum(vals) / len(DIMENSIONS) if DIMENSIONS else 0.0
        total_w = sum(float(w) for w in weights.values())
        if total_w == 0:
            return 0.0
        s = sum(float(getattr(self, k, 0.0)) * float(w) for k, w in weights.items())
        return _clamp(s / total_w)

def compute_usefulness(outcome: TicketOutcome) -> float:
    status = str(getattr(outcome, "status", "") or "")
    goal_decision = str(getattr(outcome, "goal_decision", "") or "")
    if status == "COMPLETE":
        return 1.0
    if status == "RESOLVED":
        return 0.9
    if status == "DEFERRED":
        return 0.1
    if status in ("CANCELLED", "SUPERSEDED"):
        return 0.0
    # IN_PROGRESS or empty etc.
    if not goal_decision:
        return 0.0
    if goal_decision == "NOW":
        return 0.3
    if goal_decision == "LATER":
        return 0.05
    return 0.0

def compute_correctness(outcome: TicketOutcome) -> float:
    status = str(getattr(outcome, "status", "") or "")
    if status not in ("COMPLETE", "RESOLVED"):
        return 0.0
    signals = getattr(outcome, "quality_signals", None) or {}
    if not signals:
        return 0.7
    score = 0.0
    checks = 0
    if "tests_passed" in signals:
        score += 1.0 if signals["tests_passed"] else 0.0
        checks += 1
    if "build_passed" in signals:
        score += 1.0 if signals["build_passed"] else 0.0
        checks += 1
    if "review_approved" in signals:
        score += 1.0 if signals["review_approved"] else 0.0
        checks += 1
    if "static_checks_passed" in signals:
        score += 1.0 if signals["static_checks_passed"] else 0.0
        checks += 1
    if checks == 0:
        return 0.7
    return _clamp(score / checks)

def compute_first_pass_quality(outcome: TicketOutcome) -> float:
    status = str(getattr(outcome, "status", "") or "")
    if status not in ("COMPLETE", "RESOLVED"):
        return 0.0
    rc = int(getattr(outcome, "rework_count", 0) or 0)
    attempts = int(getattr(outcome, "attempts", 1) or 1)
    if rc == 0 and attempts <= 1:
        return 1.0
    penalty = (rc * 0.2) + max(0, (attempts - 1) * 0.15)
    return _clamp(1.0 - penalty)

def compute_stability(outcome: TicketOutcome) -> float:
    status = str(getattr(outcome, "status", "") or "")
    if status not in ("COMPLETE", "RESOLVED"):
        return 0.5
    regressions = int(getattr(outcome, "post_completion_regressions", 0) or 0)
    reopens = int(getattr(outcome, "reopen_count", 0) or 0)
    penalty = (regressions * 0.3) + (reopens * 0.2)
    return _clamp(1.0 - penalty)

def compute_cost_efficiency(outcome: TicketOutcome) -> float:
    cost = float(getattr(outcome, "total_cost", 0.0) or 0.0)
    if cost == 0.0:
        return 0.5
    # reference $5
    if cost <= 5.0:
        return 1.0
    # linear decay: 5->1.0, 10->0.5, larger->approaches 0
    return _clamp(5.0 / cost)

def compute_speed(outcome: TicketOutcome) -> float:
    t = float(getattr(outcome, "completion_time", 0.0) or 0.0)
    if t == 0.0:
        return 0.5
    if t <= 1800.0:
        return 1.0
    return _clamp(1800.0 / t)

def compute_implementation_efficiency(outcome: TicketOutcome) -> float:
    attempts = int(getattr(outcome, "attempts", 1) or 1)
    rc = int(getattr(outcome, "rework_count", 0) or 0)
    if attempts <= 1 and rc == 0:
        return 1.0
    penalty = (rc * 0.2) + max(0, (attempts - 1) * 0.15)
    return _clamp(1.0 - penalty)

def compute_human_override(outcome: TicketOutcome) -> float:
    overrides = int(getattr(outcome, "human_overrides", 0) or 0)
    if overrides == 0:
        return 1.0
    return _clamp(1.0 - (overrides * 0.25))

def compute_goal_value(outcome: TicketOutcome) -> float:
    status = str(getattr(outcome, "status", "") or "")
    if status not in ("COMPLETE", "RESOLVED"):
        return 0.0
    score = 0.3
    severity_weights = {"critical": 0.3, "high": 0.25, "medium": 0.15, "low": 0.05}
    sev = str(getattr(outcome, "severity", "medium") or "medium").lower()
    score += severity_weights.get(sev, 0.1)
    if str(getattr(outcome, "goal_decision", "") or "") == "NOW":
        score += 0.2
    if bool(getattr(outcome, "user_requested", False)):
        score += 0.2
    return _clamp(score)

def compute_reward_vector(outcome: TicketOutcome, now: float | None = None) -> tuple[RewardVector, float]:
    if now is None:
        now = time.time()
    v = RewardVector(
        usefulness=compute_usefulness(outcome),
        correctness=compute_correctness(outcome),
        first_pass_quality=compute_first_pass_quality(outcome),
        stability=compute_stability(outcome),
        cost_efficiency=compute_cost_efficiency(outcome),
        speed=compute_speed(outcome),
        implementation_efficiency=compute_implementation_efficiency(outcome),
        human_override=compute_human_override(outcome),
        goal_value=compute_goal_value(outcome),
    )
    c = _compute_confidence(outcome, now)
    return v, c

def _compute_confidence(outcome: TicketOutcome, now: float) -> float:
    status = str(getattr(outcome, "status", "") or "")
    if status not in ("COMPLETE", "RESOLVED"):
        return 0.3
    confidence = 0.3
    completed_at = float(getattr(outcome, "completed_at", 0) or 0)
    if completed_at > 0:
        age = now - completed_at
        if age >= OBSERVATION_WINDOWS["immediate"]:
            confidence += 0.05
        if age >= OBSERVATION_WINDOWS["24h"]:
            confidence += 0.1
        if age >= OBSERVATION_WINDOWS["72h"]:
            confidence += 0.1
        if age >= OBSERVATION_WINDOWS["7d"]:
            confidence += 0.1
    exposure = getattr(outcome, "exposure_signals", None) or {}
    if isinstance(exposure, dict):
        if exposure.get("tests_executed"):
            confidence += 0.1
        if exposure.get("discovery_scanned"):
            confidence += 0.05
        if exposure.get("user_accepted"):
            confidence += 0.1
    if int(getattr(outcome, "post_completion_regressions", 0) or 0) > 0:
        confidence *= 0.8
    return _clamp(confidence)

def update_outcome_reward(outcome: TicketOutcome, now: float | None = None) -> TicketOutcome:
    v, c = compute_reward_vector(outcome, now=now)
    outcome.reward_vector = v.to_dict()
    outcome.reward_confidence = c
    return outcome
