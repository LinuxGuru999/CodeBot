#!/usr/bin/env python3
"""Deterministic review completion gate.

Purpose
-------
Replaces LLM-based gatekeeper reasoning for review completion decisions.
Evaluates whether a ticket can proceed to COMPLETE based on primary review,
specialist reviews, blocking findings, and deterministic gates.

ESCALATE is not a terminal failure. It means specialists must approve.
Once all required specialists approve, the gate passes regardless of
whether primary said APPROVE or ESCALATE.

Invariants
----------
- stdlib-only
- No LLM calls
- Legacy (unversioned) reviews are never completion-authoritative
- Reviews must match current implementation_attempt_id + implementation_revision
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

MAX_REVIEW_RETRIES = 2


def is_review_current(review: dict[str, Any], ticket: Any) -> bool:
    if review.get("is_legacy"):
        return False
    impl_attempt = int(review.get("implementation_attempt_id", 0))
    impl_revision = str(review.get("implementation_revision", ""))
    ticket_attempt = int(getattr(ticket, "attempts", 0))
    ticket_revision = str(getattr(ticket, "repo_revision", ""))
    if not impl_revision:
        return False
    if ticket_attempt > 0 and impl_attempt != ticket_attempt:
        return False
    if not ticket_revision:
        return impl_attempt == ticket_attempt if ticket_attempt > 0 else bool(impl_attempt)
    if impl_revision == ticket_revision:
        return True
    return False


def get_current_primary_review(
    ticket_id: str,
    state_dir: Path | str,
    ticket: Any,
) -> dict[str, Any] | None:
    from codebot.review_store import load_ticket_verdicts
    verdicts = load_ticket_verdicts(state_dir, ticket_id)
    attempt = int(getattr(ticket, "attempts", 0))
    attempt_verdicts = [
        v for v in verdicts
        if int(v.get("implementation_attempt_id", 0)) == attempt
        and not v.get("is_legacy")
    ]
    for v in attempt_verdicts:
        reviewer = str(v.get("reviewer", ""))
        base = reviewer.split("-", 1)[0]
        if base in ("reviewer", "primary_reviewer"):
            return v
    for v in verdicts:
        reviewer = str(v.get("reviewer", ""))
        base = reviewer.split("-", 1)[0]
        if base in ("reviewer", "primary_reviewer") and not v.get("is_legacy"):
            return v
    return None


def get_current_specialist_review(
    ticket_id: str,
    specialist_type: str,
    state_dir: Path | str,
    ticket: Any,
) -> dict[str, Any] | None:
    from codebot.escalation_rules import specialist_role_for_type
    from codebot.review_store import load_ticket_verdicts
    role_name = specialist_role_for_type(specialist_type)
    if not role_name:
        return None
    verdicts = load_ticket_verdicts(state_dir, ticket_id)
    attempt = int(getattr(ticket, "attempts", 0))
    for v in verdicts:
        if v.get("is_legacy"):
            continue
        if int(v.get("implementation_attempt_id", 0)) != attempt:
            continue
        reviewer = str(v.get("reviewer", ""))
        base = reviewer.split("-", 1)[0]
        if base == role_name:
            return v
    return None


def has_blocking_findings(
    ticket_id: str,
    state_dir: Path | str,
    ticket: Any,
) -> bool:
    from codebot.review_store import load_ticket_verdicts
    from codebot.review_types import ReviewDecision
    try:
        from codebot.review_config import get_review_config
        cfg = get_review_config()
        blocking = cfg.blocking_severities
    except Exception:
        from codebot.review_types import DEFAULT_BLOCKING_SEVERITIES
        blocking = DEFAULT_BLOCKING_SEVERITIES
    verdicts = load_ticket_verdicts(state_dir, ticket_id)
    for v in verdicts:
        if v.get("is_legacy"):
            continue
        try:
            rd = ReviewDecision.from_dict(v)
        except (ValueError, KeyError):
            continue
        if rd.blocking_findings(blocking):
            return True
    return False


def determine_rework_target(review: dict[str, Any]) -> str:
    origin = str(review.get("failure_origin", "")).upper()
    explicit_target = str(review.get("rework_target", "")).upper()
    if explicit_target in ("IMPLEMENT", "PLANNING", "DECOMP"):
        return explicit_target
    if origin == "PLANNING_ERROR":
        return "PLANNING"
    if origin == "DECOMPOSITION_ERROR":
        return "DECOMP"
    return "IMPLEMENT"


def determine_required_specialists(
    ticket_id: str,
    state_dir: Path | str,
    ticket: Any,
    primary: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from codebot.escalation_rules import (
        get_deterministic_specialists,
        resolve_specialist_cap,
    )

    try:
        from codebot.review_learning_registry import get_policy_mode, is_observation_enabled
        from codebot.rl_review_policy import load_policy, predict_specialist_scores, get_thresholds as _get_thr
        from codebot.rl_review_features import extract_features, feature_vector_hash
        from codebot.rl_review_episode import build_episode
        from codebot.rl_review_shadow import persist_shadow

        if primary is None:
            primary = get_current_primary_review(ticket_id, state_dir, ticket)
        deterministic = get_deterministic_specialists(ticket)
        obs = is_observation_enabled(state_dir)
        mode = get_policy_mode(state_dir)

        if obs and mode in ("SHADOW", "ASSIST"):
            try:
                pol = load_policy(state_dir)
                if pol.get("status") == "OK" and pol.get("weights"):
                    ep = build_episode(ticket, state_dir=state_dir, specialist_results={}, completion_result="")
                    feats = extract_features(ep, decision_ts=float(getattr(ticket, "updated_at", 0) or 0))
                    scores = predict_specialist_scores(feats, pol["weights"])
                    try:
                        persist_shadow(
                            ep.review_cycle_id or str(getattr(ticket, "current_review_cycle_id", "") or ticket_id),
                            str(pol.get("policy_version", "unknown")),
                            scores,
                            state_dir,
                            features_snapshot=feats,
                            actual_deterministic_specialists=sorted(deterministic.keys()),
                        )
                    except Exception:
                        pass
                    if mode == "ASSIST":
                        tmp_demands = dict(deterministic)
                        for spec, score in scores.items():
                            if spec in tmp_demands:
                                continue
                            try:
                                thr = _get_thr(pol.get("weights", {}), spec, str(getattr(getattr(ticket, "risk", None), "value", "medium")))
                            except Exception:
                                thr = 0.30 if spec in ("security", "data_integrity", "concurrency") else 0.50
                            if float(score) >= float(thr):
                                from codebot.escalation_rules import SpecialistDemand
                                tmp_demands[spec] = SpecialistDemand(origin="rl_assist", reason=f"shadow score {score:.2f} >= {thr:.2f}", priority=2)
                        try:
                            decision = str((primary or {}).get("verdict", (primary or {}).get("decision", ""))).upper()
                            if decision == "ESCALATE":
                                st = str((primary or {}).get("specialist_type", ""))
                                sr = str((primary or {}).get("specialist_reason", ""))
                                ares = resolve_specialist_cap(tmp_demands, primary_escalation_type=st, primary_escalation_reason=sr)
                            else:
                                ares = resolve_specialist_cap(tmp_demands)
                            return dict(ares.required), dict(ares.suppressed)
                        except Exception:
                            pass
            except Exception:
                pass
    except Exception:
        pass

    from codebot.escalation_rules import (
        get_deterministic_specialists as _gds,
        resolve_specialist_cap as _rsc,
    )

    deterministic = _gds(ticket)
    if primary is None:
        primary = get_current_primary_review(ticket_id, state_dir, ticket)
    if primary is None:
        return dict(resolve_specialist_cap(deterministic).required), dict(resolve_specialist_cap(deterministic).suppressed)
    decision = str(primary.get("verdict", primary.get("decision", ""))).upper()
    if decision == "APPROVE":
        res = _rsc(deterministic)
    elif decision == "ESCALATE":
        st = str(primary.get("specialist_type", ""))
        sr = str(primary.get("specialist_reason", ""))
        res = _rsc(deterministic, primary_escalation_type=st, primary_escalation_reason=sr)
    else:
        res = _rsc(deterministic)
    return dict(res.required), dict(res.suppressed)


def review_completion_gate(
    ticket_id: str,
    state_dir: Path | str,
    ticket: Any,
) -> tuple[bool, str, str | None]:
    from codebot.escalation_rules import (
        get_deterministic_specialists,
        resolve_specialist_cap,
        validate_escalation,
    )
    primary = get_current_primary_review(ticket_id, state_dir, ticket)
    if primary is None:
        return False, "primary_review_missing", None
    if not is_review_current(primary, ticket):
        return False, "primary_review_stale", None
    valid, err = validate_escalation(primary)
    if not valid:
        logger.warning("ticket %s: malformed escalation: %s", ticket_id, err)
        return False, f"malformed_escalation:{err}", None
    decision = str(primary.get("verdict", primary.get("decision", ""))).upper()
    if decision == "REWORK":
        target = determine_rework_target(primary)
        return False, "primary_review_rework", target
    try:
        required, _suppressed = determine_required_specialists(ticket_id, state_dir, ticket, primary)
        resolution_required = required
    except Exception:
        deterministic_demands = get_deterministic_specialists(ticket)
        if decision == "APPROVE":
            resolution_required = dict(resolve_specialist_cap(deterministic_demands).required)
        elif decision == "ESCALATE":
            st = str(primary.get("specialist_type", ""))
            sr = str(primary.get("specialist_reason", ""))
            resolution_required = dict(resolve_specialist_cap(deterministic_demands, primary_escalation_type=st, primary_escalation_reason=sr).required)
        else:
            return False, "primary_review_invalid_decision", None
        if decision not in ("APPROVE", "ESCALATE"):
            return False, "primary_review_invalid_decision", None
    else:
        if decision not in ("APPROVE", "ESCALATE"):
            return False, "primary_review_invalid_decision", None
        resolution = type("R", (), {"required": resolution_required})()
    for specialist_type in (resolution_required if "resolution_required" in locals() else resolution.required):
        specialist = get_current_specialist_review(
            ticket_id, specialist_type, state_dir, ticket,
        )
        if specialist is None:
            return False, f"specialist_{specialist_type}_pending", None
        if not is_review_current(specialist, ticket):
            return False, f"specialist_{specialist_type}_stale", None
        s_decision = str(specialist.get("verdict", specialist.get("decision", ""))).upper()
        if s_decision == "REWORK":
            target = determine_rework_target(specialist)
            return False, f"specialist_{specialist_type}_rework", target
    if has_blocking_findings(ticket_id, state_dir, ticket):
        return False, "blocking_findings_present", None
    return True, "all_checks_passed", None
