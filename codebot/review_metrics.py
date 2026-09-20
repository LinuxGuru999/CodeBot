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


def get_gatekeeper_stats() -> dict[str, Any]:
    state_dir = _resolve_state_dir()
    path = state_dir / "gatekeeper_metrics.jsonl"
    if not path.exists():
        return {}
    total = 0
    completes = 0
    reworks = 0
    blocked = 0
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                    total += 1
                    d = rec.get("decision", "").upper()
                    if d == "COMPLETE":
                        completes += 1
                    elif d == "REWORK":
                        reworks += 1
                    elif d == "BLOCKED":
                        blocked += 1
                except json.JSONDecodeError:
                    continue
    except OSError:
        return {}
    if total == 0:
        return {}
    return {
        "total_decisions": total,
        "complete_count": completes,
        "rework_count": reworks,
        "blocked_count": blocked,
        "complete_rate": round(completes / total, 3),
        "rework_rate": round(reworks / total, 3),
    }


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
    return alerts
