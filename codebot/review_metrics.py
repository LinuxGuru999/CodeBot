#!/usr/bin/env python3
"""Review and gatekeeper calibration metrics.

Purpose
-------
Tracks reviewer behavior, gatekeeper decisions, and quality health alerts
so that the review system's own quality is measurable and auditable. Metrics
are append-only JSONL for provenance and can be queried for dashboards.

Why
---
Without metrics, there is no way to detect reviewers that rubber-stamp,
gatekeepers that always agree, or post-completion defect patterns.
Metrics make the review system itself accountable.

Invariants
----------
- stdlib-only (json, time, pathlib, os)
- Append-only; never modify or delete existing entries
- All metrics are per-reviewer or per-gatekeeper, keyed by name
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CODEBOT_PKG_DIR = Path(__file__).parent
_PROJECT_ROOT = _CODEBOT_PKG_DIR.parent
_DEFAULT_STATE_DIR = _PROJECT_ROOT / ".codebot" / "state"


def _resolve_state_dir() -> Path:
    env = os.environ.get("CODEBOT_STATE_DIR") or os.environ.get("CODEBOT_PROJECT_ROOT")
    if env:
        p = Path(env)
        if not p.name == "state":
            p = p / ".codebot" / "state"
        if p.exists():
            return p
    return _DEFAULT_STATE_DIR


def record_reviewer_metric(
    reviewer: str,
    ticket_id: str,
    verdict: str,
    findings_count: int = 0,
    blocking_count: int = 0,
    duration_s: float = 0.0,
    token_cost: int = 0,
    model: str = "",
) -> None:
    state_dir = _resolve_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "reviewer_metrics.jsonl"
    record = {
        "ts": time.time(),
        "reviewer": reviewer,
        "ticket_id": ticket_id,
        "verdict": verdict,
        "findings_count": findings_count,
        "blocking_count": blocking_count,
        "duration_s": round(duration_s, 2),
        "token_cost": token_cost,
        "model": model,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def record_gatekeeper_metric(
    ticket_id: str,
    decision: str,
    gates_passed: bool,
    blocking_findings: int,
    review_count: int,
    rework_count: int = 0,
) -> None:
    state_dir = _resolve_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "gatekeeper_metrics.jsonl"
    record = {
        "ts": time.time(),
        "ticket_id": ticket_id,
        "decision": decision,
        "gates_passed": gates_passed,
        "blocking_findings": blocking_findings,
        "review_count": review_count,
        "rework_count": rework_count,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def record_escaped_defect(
    ticket_id: str,
    defect_type: str,
    discovered_by: str,
    original_reviewers: list[str] | None = None,
) -> None:
    state_dir = _resolve_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "escaped_defects.jsonl"
    record = {
        "ts": time.time(),
        "ticket_id": ticket_id,
        "defect_type": defect_type,
        "discovered_by": discovered_by,
        "original_reviewers": original_reviewers or [],
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def get_reviewer_stats(reviewer: str) -> dict[str, Any]:
    state_dir = _resolve_state_dir()
    path = state_dir / "reviewer_metrics.jsonl"
    if not path.exists():
        return {}
    total = 0
    approvals = 0
    reworks = 0
    total_findings = 0
    total_blocking = 0
    total_duration = 0.0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    if rec.get("reviewer") != reviewer:
                        continue
                    total += 1
                    v = rec.get("verdict", "").upper()
                    if v == "APPROVE":
                        approvals += 1
                    elif v in ("REWORK", "ESCALATE"):
                        reworks += 1
                    total_findings += rec.get("findings_count", 0)
                    total_blocking += rec.get("blocking_count", 0)
                    total_duration += rec.get("duration_s", 0.0)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return {}
    if total == 0:
        return {}
    return {
        "tickets_reviewed": total,
        "approval_rate": round(approvals / total, 3),
        "rejection_rate": round(reworks / total, 3),
        "findings_per_review": round(total_findings / total, 2),
        "blocking_findings": total_blocking,
        "avg_duration_s": round(total_duration / total, 1),
    }


def get_review_speed_quality_summary(state_dir: Path | None = None) -> dict[str, Any]:
    resolved = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    path = resolved / "reviewer_metrics.jsonl"
    if not path.exists():
        return {"reviews": 0, "average_duration_s": 0.0, "p95_duration_s": 0.0, "rework_rate": 0.0}
    durations: list[float] = []
    reworks = 0
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                durations.append(float(record.get("duration_s", 0.0)))
                if str(record.get("verdict", "")).upper() in ("REWORK", "ESCALATE"):
                    reworks += 1
    except OSError:
        return {"reviews": 0, "average_duration_s": 0.0, "p95_duration_s": 0.0, "rework_rate": 0.0}
    if not durations:
        return {"reviews": 0, "average_duration_s": 0.0, "p95_duration_s": 0.0, "rework_rate": 0.0}
    ordered = sorted(durations)
    percentile_index = min(len(ordered) - 1, max(0, (len(ordered) * 95 + 99) // 100 - 1))
    return {
        "reviews": len(durations),
        "average_duration_s": round(sum(durations) / len(durations), 2),
        "p95_duration_s": ordered[percentile_index],
        "rework_rate": round(reworks / len(durations), 3),
    }


def _count_gatekeeper_log(state_dir: Path) -> dict[str, int]:
    path = state_dir / "gate_results.jsonl"
    counts = {"total": 0, "complete": 0, "rework": 0, "blocked": 0, "fail": 0}
    if not path.exists():
        return counts
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                decision = str(rec.get("decision", "")).upper()
                if decision not in ("COMPLETE", "REWORK", "BLOCKED", "FAIL"):
                    continue
                counts["total"] += 1
                if decision == "COMPLETE":
                    counts["complete"] += 1
                elif decision == "REWORK":
                    counts["rework"] += 1
                elif decision == "FAIL":
                    counts["fail"] += 1
                else:
                    counts["blocked"] += 1
    except OSError:
        return {"total": 0, "complete": 0, "rework": 0, "blocked": 0, "fail": 0}
    return counts


def _count_gatekeeper_metrics(state_dir: Path) -> dict[str, int]:
    path = state_dir / "gatekeeper_metrics.jsonl"
    counts = {"total": 0, "complete": 0, "rework": 0, "blocked": 0, "fail": 0}
    if not path.exists():
        return counts
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                decision = str(rec.get("decision", "")).upper()
                if decision not in ("COMPLETE", "REWORK", "BLOCKED", "FAIL"):
                    continue
                counts["total"] += 1
                if decision == "COMPLETE":
                    counts["complete"] += 1
                elif decision == "REWORK":
                    counts["rework"] += 1
                elif decision == "FAIL":
                    counts["fail"] += 1
                else:
                    counts["blocked"] += 1
    except OSError:
        return {"total": 0, "complete": 0, "rework": 0, "blocked": 0, "fail": 0}
    return counts


def check_gatekeeper_consistency(state_dir: Path | None = None) -> dict[str, Any]:
    resolved = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    logged = _count_gatekeeper_log(resolved)
    measured = _count_gatekeeper_metrics(resolved)
    consistent = logged == measured
    return {
        "consistent": consistent,
        "log": logged,
        "metrics": measured,
    }


def get_gatekeeper_stats(state_dir: Path | None = None) -> dict[str, Any]:
    resolved = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    counts = _count_gatekeeper_log(resolved)
    total = counts["total"]
    if total == 0:
        legacy = _count_gatekeeper_metrics(resolved)
        total = legacy["total"]
        if total == 0:
            return {}
        return {
            "total_decisions": total,
            "complete_count": legacy["complete"],
            "rework_count": legacy["rework"],
            "blocked_count": legacy["blocked"],
            "fail_count": legacy["fail"],
            "complete_rate": round(legacy["complete"] / total, 3),
            "rework_rate": round(legacy["rework"] / total, 3),
            "fail_rate": round(legacy["fail"] / total, 3),
            "source": "gatekeeper_metrics.jsonl",
            "metrics_log_consistent": check_gatekeeper_consistency(resolved)["consistent"],
        }
    return {
        "total_decisions": total,
        "complete_count": counts["complete"],
        "rework_count": counts["rework"],
        "blocked_count": counts["blocked"],
        "fail_count": counts["fail"],
        "complete_rate": round(counts["complete"] / total, 3),
        "rework_rate": round(counts["rework"] / total, 3),
        "fail_rate": round(counts["fail"] / total, 3),
        "source": "gate_results.jsonl",
        "metrics_log_consistent": check_gatekeeper_consistency(resolved)["consistent"],
    }


# ---------------------------------------------------------------------------
# Post-completion audit sampling
# ---------------------------------------------------------------------------

def select_tickets_for_audit(
    completed_ticket_ids: list[str],
    audit_percentage: int,
    risk_scores: dict[str, int] | None = None,
) -> list[str]:
    """Select tickets for post-completion audit via risk-weighted sampling.

    Higher-risk completions are more likely to be audited. Returns the
    subset of ticket IDs that should be re-reviewed.
    """
    import random

    if not completed_ticket_ids or audit_percentage <= 0:
        return []

    target_count = max(1, len(completed_ticket_ids) * audit_percentage // 100)
    target_count = min(target_count, len(completed_ticket_ids))

    if risk_scores is None:
        return random.sample(completed_ticket_ids, target_count)

    weights = [float(risk_scores.get(tid, 50)) + 1.0 for tid in completed_ticket_ids]
    selected: list[str] = []
    remaining = list(completed_ticket_ids)
    remaining_weights = list(weights)

    for _ in range(target_count):
        if not remaining:
            break
        total_w = sum(remaining_weights)
        r = random.uniform(0, total_w)
        cumulative = 0.0
        chosen_idx = len(remaining) - 1
        for i, w in enumerate(remaining_weights):
            cumulative += w
            if r <= cumulative:
                chosen_idx = i
                break
        selected.append(remaining[chosen_idx])
        remaining.pop(chosen_idx)
        remaining_weights.pop(chosen_idx)

    return selected


def record_audit_result(
    ticket_id: str,
    auditor: str,
    verdict: str,
    findings_count: int = 0,
    blocking_count: int = 0,
    reopened: bool = False,
) -> None:
    """Record the outcome of a post-completion audit."""
    state_dir = _resolve_state_dir()
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / "audit_results.jsonl"
    record = {
        "ts": time.time(),
        "ticket_id": ticket_id,
        "auditor": auditor,
        "verdict": verdict,
        "findings_count": findings_count,
        "blocking_count": blocking_count,
        "reopened": reopened,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record) + "\n")


def get_audit_stats() -> dict[str, Any]:
    """Aggregate post-completion audit statistics."""
    state_dir = _resolve_state_dir()
    path = state_dir / "audit_results.jsonl"
    if not path.exists():
        return {}
    total = 0
    reopened_count = 0
    total_findings = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    total += 1
                    if rec.get("reopened"):
                        reopened_count += 1
                    total_findings += rec.get("findings_count", 0)
                except json.JSONDecodeError:
                    continue
    except OSError:
        return {}
    if total == 0:
        return {}
    return {
        "total_audits": total,
        "reopened_count": reopened_count,
        "reopen_rate": round(reopened_count / total, 3),
        "findings_per_audit": round(total_findings / total, 2),
    }


# ---------------------------------------------------------------------------
# Quality health alerts
# ---------------------------------------------------------------------------

def check_quality_health() -> list[dict[str, str]]:
    alerts: list[dict[str, str]] = []
    gk = get_gatekeeper_stats()
    if gk:
        total = gk.get("total_decisions", 0)
        if total >= 50 and gk.get("complete_rate", 0) >= 0.98:
            alerts.append({
                "rule": "GK_HIGH_APPROVAL",
                "severity": "warning",
                "message": f"Gatekeeper complete_rate={gk['complete_rate']:.1%} over {total} decisions — possible rubber-stamping",
            })
        if total >= 50 and gk.get("rework_rate", 0) >= 0.90:
            alerts.append({
                "rule": "GK_HIGH_REWORK",
                "severity": "info",
                "message": f"Gatekeeper rework_rate={gk['rework_rate']:.1%} over {total} decisions — possibly too strict",
            })
    escaped_path = _resolve_state_dir() / "escaped_defects.jsonl"
    if escaped_path.exists():
        count = 0
        try:
            with open(escaped_path, encoding="utf-8") as f:
                count = sum(1 for line in f if line.strip())
        except OSError:
            pass
        if count > 10:
            alerts.append({
                "rule": "HIGH_ESCAPED_DEFECTS",
                "severity": "critical",
                "message": f"{count} escaped defects recorded — review system may be failing",
            })
    audit = get_audit_stats()
    if audit:
        reopen_rate = audit.get("reopen_rate", 0)
        total_audits = audit.get("total_audits", 0)
        if total_audits >= 20 and reopen_rate >= 0.15:
            alerts.append({
                "rule": "HIGH_AUDIT_REOPEN_RATE",
                "severity": "critical",
                "message": f"Post-completion audit reopen_rate={reopen_rate:.1%} over {total_audits} audits — false completions likely",
            })
    return alerts


_LIFECYCLE_REQUIRED_KEYS = (
    "ticket_id",
    "from_state",
    "to_state",
    "timestamp",
    "attempts",
    "rework_count",
    "queue_age_seconds",
    "actor",
    "revision",
)

_LIFECYCLE_TERMINAL_STATES = frozenset({"COMPLETE", "REJECTED", "DUPLICATE"})


def _is_lifecycle_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def load_lifecycle_events(state_dir: Path | None = None) -> list[dict[str, Any]]:
    try:
        resolved = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    except Exception:
        return []
    path = resolved / "lifecycle_events.jsonl"
    try:
        if not path.exists():
            return []
    except Exception:
        return []
    events: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                except Exception:
                    continue
                if not isinstance(rec, dict):
                    continue
                if any(k not in rec for k in _LIFECYCLE_REQUIRED_KEYS):
                    continue
                if not isinstance(rec.get("ticket_id"), str):
                    continue
                if not isinstance(rec.get("from_state"), str):
                    continue
                if not isinstance(rec.get("to_state"), str):
                    continue
                if not isinstance(rec.get("actor"), str):
                    continue
                if not _is_lifecycle_number(rec.get("timestamp")):
                    continue
                if not _is_lifecycle_number(rec.get("attempts")):
                    continue
                if not _is_lifecycle_number(rec.get("rework_count")):
                    continue
                if not _is_lifecycle_number(rec.get("queue_age_seconds")):
                    continue
                if not _is_lifecycle_number(rec.get("revision")):
                    continue
                events.append(rec)
    except OSError:
        return []
    except Exception:
        return []
    return events


def build_lifecycle_report(events: list[dict[str, Any]], now: float | None = None) -> dict[str, Any]:
    _ = now
    total = len(events)
    if total == 0:
        return {
            "total_transitions": 0,
            "per_stage": {},
            "active_tickets": 0,
            "terminal_tickets": 0,
            "cycle_time_seconds": {},
            "avg_cycle_time_seconds": 0.0,
            "rework_rate": 0.0,
            "rework_count": 0,
        }
    stage_counts: dict[str, int] = {}
    stage_queue_totals: dict[str, float] = {}
    for e in events:
        stage = e.get("to_state")
        if not isinstance(stage, str):
            continue
        stage_counts[stage] = stage_counts.get(stage, 0) + 1
        q = e.get("queue_age_seconds")
        if _is_lifecycle_number(q) and q is not None:
            stage_queue_totals[stage] = stage_queue_totals.get(stage, 0.0) + float(q)
    per_stage: dict[str, dict[str, Any]] = {}
    for stage, count in stage_counts.items():
        queue_total = stage_queue_totals.get(stage, 0.0)
        avg_q = round(queue_total / count, 3) if count else 0.0
        per_stage[stage] = {
            "enter_count": count,
            "avg_queue_age_seconds": avg_q,
        }
    last_state: dict[str, str] = {}
    first_ts: dict[str, float] = {}
    last_ts: dict[str, float] = {}
    has_complete: set[str] = set()
    for e in events:
        tid = e.get("ticket_id")
        to_state = e.get("to_state")
        ts = e.get("timestamp")
        if not isinstance(tid, str) or not isinstance(to_state, str):
            continue
        if not _is_lifecycle_number(ts) or ts is None:
            continue
        ts_f = float(ts)
        if tid not in first_ts:
            first_ts[tid] = ts_f
        last_ts[tid] = ts_f
        last_state[tid] = to_state
        if to_state == "COMPLETE":
            has_complete.add(tid)
    active = sum(1 for s in last_state.values() if s not in _LIFECYCLE_TERMINAL_STATES)
    terminal = sum(1 for s in last_state.values() if s in _LIFECYCLE_TERMINAL_STATES)
    cycle_time_seconds: dict[str, float] = {}
    for tid in sorted(has_complete):
        if tid in first_ts and tid in last_ts:
            cycle_time_seconds[tid] = float(last_ts[tid]) - float(first_ts[tid])
    if cycle_time_seconds:
        avg_cycle = round(sum(cycle_time_seconds.values()) / len(cycle_time_seconds), 3)
    else:
        avg_cycle = 0.0
    rework_count = sum(1 for e in events if e.get("to_state") == "REWORK")
    rework_rate = round(rework_count / total, 4) if total else 0.0
    return {
        "total_transitions": total,
        "per_stage": per_stage,
        "active_tickets": active,
        "terminal_tickets": terminal,
        "cycle_time_seconds": cycle_time_seconds,
        "avg_cycle_time_seconds": avg_cycle,
        "rework_rate": rework_rate,
        "rework_count": rework_count,
    }


_TERMINAL_STATES = frozenset({"COMPLETE", "REJECTED", "DUPLICATE", "RESOLVED", "SUPERSEDED", "CANCELLED", "NEVER", "LATER"})


def build_lifecycle_report_from_store(store: Any | None = None) -> dict[str, Any]:
    try:
        if store is None:
            from codebot.ticket_dispatcher import get_ticket_store
            store = get_ticket_store()
        if store is None:
            return {}
        tickets = store.list_all() if hasattr(store, "list_all") else []
    except Exception:
        return {}
    if not tickets:
        return {}
    per_stage: dict[str, dict[str, Any]] = {}
    active = 0
    terminal = 0
    total_rework = 0
    cycle_times: dict[str, float] = {}
    for t in tickets:
        state_val = getattr(t, "state", None)
        sv = state_val.value if state_val is not None and hasattr(state_val, "value") else str(state_val) if state_val else "UNKNOWN"
        entry = per_stage.get(sv, {"enter_count": 0, "avg_queue_age_seconds": 0.0})
        entry["enter_count"] += 1
        per_stage[sv] = entry
        rework = getattr(t, "rework_count", 0) or 0
        total_rework += rework
        if sv in _TERMINAL_STATES:
            terminal += 1
            created = getattr(t, "created_at", 0.0) or 0.0
            updated = getattr(t, "updated_at", 0.0) or 0.0
            if created and updated and updated > created:
                cycle_times[getattr(t, "id", "")] = updated - created
        else:
            active += 1
    total_transitions = sum(e["enter_count"] for e in per_stage.values()) + total_rework
    avg_cycle = round(sum(cycle_times.values()) / len(cycle_times), 3) if cycle_times else 0.0
    rework_rate = round(total_rework / total_transitions, 4) if total_transitions else 0.0
    return {
        "total_transitions": total_transitions,
        "per_stage": per_stage,
        "active_tickets": active,
        "terminal_tickets": terminal,
        "cycle_time_seconds": cycle_times,
        "avg_cycle_time_seconds": avg_cycle,
        "rework_rate": rework_rate,
        "rework_count": total_rework,
        "source": "ticket_store",
    }


def lifecycle_report_json(state_dir: Path | None = None, now: float | None = None) -> str:
    store_report = build_lifecycle_report_from_store()
    if store_report:
        return json.dumps(store_report, indent=2)
    events = load_lifecycle_events(state_dir)
    report = build_lifecycle_report(events, now=now)
    return json.dumps(report, indent=2)


_REWORK_RECOMMENDATION_MIN_EVENTS = 20
_REWORK_RECOMMENDATION_MIN_RATE = 0.25
_ESCAPED_DEFECT_ALERT_THRESHOLD = 5


def build_lifecycle_recommendations(state_dir: Path | None = None) -> list[dict[str, Any]]:
    """Build advisory recommendations from lifecycle and quality metrics.

    Recommendations are bounded, explainable, and never disable checks
    or change ticket state. They are purely advisory for operator review.
    """
    recommendations: list[dict[str, Any]] = []
    try:
        resolved = Path(state_dir) if state_dir is not None else _resolve_state_dir()
    except Exception:
        return []

    events = load_lifecycle_events(resolved)
    if len(events) < _REWORK_RECOMMENDATION_MIN_EVENTS:
        return []

    report = build_lifecycle_report(events)
    rework_rate = report.get("rework_rate", 0.0)
    rework_count = report.get("rework_count", 0)

    if rework_rate >= _REWORK_RECOMMENDATION_MIN_RATE and rework_count >= 5:
        recommendations.append({
            "type": "rework_rate",
            "severity": "warning",
            "message": (
                f"Rework rate {rework_rate:.1%} ({rework_count} events) exceeds "
                f"{_REWORK_RECOMMENDATION_MIN_RATE:.0%} threshold over {len(events)} transitions. "
                "Consider reviewing planning quality or implementer prompt calibration."
            ),
            "metric": {"rework_rate": rework_rate, "rework_count": rework_count, "total_events": len(events)},
            "action": "advisory",
        })

    per_stage = report.get("per_stage", {})
    for stage, data in per_stage.items():
        avg_dwell = data.get("avg_queue_age_seconds", 0.0)
        enter_count = data.get("enter_count", 0)
        if avg_dwell > 3600 and enter_count >= 5:
            recommendations.append({
                "type": "stage_bottleneck",
                "severity": "info",
                "message": (
                    f"Stage {stage} has avg dwell {avg_dwell:.0f}s across {enter_count} entries. "
                    "Consider increasing capacity or reviewing stage efficiency."
                ),
                "metric": {"stage": stage, "avg_dwell_seconds": avg_dwell, "enter_count": enter_count},
                "action": "advisory",
            })

    escaped_path = resolved / "escaped_defects.jsonl"
    if escaped_path.exists():
        escaped_count = 0
        try:
            with open(escaped_path, encoding="utf-8") as f:
                escaped_count = sum(1 for line in f if line.strip())
        except OSError:
            pass
        if escaped_count >= _ESCAPED_DEFECT_ALERT_THRESHOLD:
            recommendations.append({
                "type": "escaped_defects",
                "severity": "critical",
                "message": (
                    f"{escaped_count} escaped defects recorded. "
                    "Review system may be failing to catch defects before completion."
                ),
                "metric": {"escaped_defect_count": escaped_count},
                "action": "advisory",
            })

    audit = get_audit_stats()
    if audit:
        reopen_rate = audit.get("reopen_rate", 0)
        total_audits = audit.get("total_audits", 0)
        if total_audits >= 10 and reopen_rate >= 0.10:
            recommendations.append({
                "type": "audit_reopen_rate",
                "severity": "warning",
                "message": (
                    f"Post-completion audit reopen rate {reopen_rate:.1%} over {total_audits} audits. "
                    "Gatekeeper may be passing tickets that fail subsequent review."
                ),
                "metric": {"reopen_rate": reopen_rate, "total_audits": total_audits},
                "action": "advisory",
            })

    return recommendations
