#!/usr/bin/env python3
"""Runtime pipeline invariants for CodeBot dispatch.

Checks structural inconsistencies without changing ticket state:
passing gates with zero review decisions, ticket-store count drift,
missing execution packets, repeated checkpoint corruption, and gatekeeper
log/metric divergence. Results are appended to
``<state>/runtime_invariants.jsonl`` for operators.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _count_gatekeeper_decisions(state_dir: Path) -> dict[str, int]:
    counts = {"COMPLETE": 0, "REWORK": 0, "DEFERRED": 0}
    path = state_dir / "gate_results.jsonl"
    if not path.exists():
        return counts
    try:
        with open(path, encoding="utf-8") as stream:
            for line in stream:
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                decision = str(record.get("decision", "")).upper()
                if decision in counts:
                    counts[decision] += 1
    except OSError:
        pass
    return counts


def check_runtime_invariants(
    state_dir: Path | str,
    store: Any | None = None,
    workforce_status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resolved = Path(state_dir)
    alerts: list[dict[str, Any]] = []

    try:
        from codebot.review_metrics import check_gatekeeper_consistency, get_gatekeeper_stats
        consistency = check_gatekeeper_consistency(resolved)
        stats = get_gatekeeper_stats(resolved)
    except (ImportError, OSError, ValueError):
        consistency = {"consistent": True, "log": {}, "metrics": {}}
        stats = {}
    if not consistency.get("consistent", True):
        alerts.append({
            "rule": "GATE_LOG_METRICS_DIVERGED",
            "severity": "warning",
            "message": f"gatekeeper log and metrics disagree: {consistency.get('log')} vs {consistency.get('metrics')}",
            "ts": time.time(),
        })

    zero_review_passes = 0
    try:
        log_path = resolved / "gate_results.jsonl"
        if log_path.exists():
            with open(log_path, encoding="utf-8") as stream:
                for line in stream:
                    try:
                        record = json.loads(line)
                    except ValueError:
                        continue
                    if record.get("passed") is True and int(record.get("review_count", 0) or 0) == 0:
                        zero_review_passes += 1
    except OSError:
        zero_review_passes = 0
    if zero_review_passes > 0:
        alerts.append({
            "rule": "PASSING_GATE_WITHOUT_REVIEWS",
            "severity": "warning",
            "message": f"{zero_review_passes} passing gate decision(s) have zero review decisions",
            "ts": time.time(),
        })

    if store is not None:
        try:
            persisted = dict(store.summary())
        except Exception:
            persisted = {}
        if workforce_status and persisted:
            pipeline = workforce_status.get("pipeline", {})
            if not pipeline and workforce_status.get("buckets"):
                pipeline = workforce_status.get("buckets", {})
            mapping = {
                "implement": "IMPLEMENT",
                "review": "REVIEW",
                "planning": "PLANNING",
                "rework": "REWORK",
                "decomp": "DECOMP",
                "goal": "GOAL",
                "triaged": "TRIAGED",
            }
            drift = {
                name: (int(pipeline.get(name, 0) or 0), int(persisted.get(state, 0) or 0))
                for name, state in mapping.items()
                if int(pipeline.get(name, 0) or 0) != int(persisted.get(state, 0) or 0)
            }
            if drift:
                alerts.append({
                    "rule": "TICKET_STATE_DRIFT",
                    "severity": "warning",
                    "message": f"workforce status and ticket store disagree: {drift}",
                    "ts": time.time(),
                })
        try:
            from codebot.ticket_engine import TicketState
            implement_ids = set()
            review_ids = set()
            for state_attr in ("IMPLEMENT", "IMPLEMENTING"):
                try:
                    st = getattr(TicketState, state_attr, None)
                    if st is not None:
                        implement_ids.update(t.id for t in store.list_by_state(st))
                except Exception:
                    pass
            for state_attr in ("REVIEW", "REVIEWING"):
                try:
                    st = getattr(TicketState, state_attr, None)
                    if st is not None:
                        review_ids.update(t.id for t in store.list_by_state(st))
                except Exception:
                    pass
        except Exception:
            implement_ids = set()
            review_ids = set()
        missing_impl = sum(
            1 for tid in implement_ids
            if not (resolved / "implementation_packets" / f"{tid}.json").exists()
        )
        missing_review = sum(
            1 for tid in review_ids
            if not (resolved / "review_packets" / f"{tid}.json").exists()
        )
        if missing_impl or missing_review:
            alerts.append({
                "rule": "PACKET_COVERAGE_GAP",
                "severity": "info",
                "message": f"missing packets: implementation={missing_impl}, review={missing_review}",
                "ts": time.time(),
            })

    quarantine = resolved / "checkpoint_quarantine"
    try:
        quarantined = len(list(quarantine.glob("*.json"))) if quarantine.exists() else 0
    except OSError:
        quarantined = 0
    if quarantined > 0:
        alerts.append({
            "rule": "CHECKPOINT_CORRUPTION",
            "severity": "info",
            "message": f"{quarantined} corrupt checkpoint(s) quarantined",
            "ts": time.time(),
        })

    complete_rate = float(stats.get("complete_rate", 0.0) or 0.0)
    total = int(stats.get("total_decisions", 0) or 0)
    if total >= 20 and complete_rate == 0.0:
        alerts.append({
            "rule": "NO_COMPLETIONS",
            "severity": "warning",
            "message": f"zero completions across {total} gatekeeper decisions",
            "ts": time.time(),
        })
    return alerts


def record_runtime_invariants(
    state_dir: Path | str,
    store: Any | None = None,
    workforce_status: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    resolved = Path(state_dir)
    alerts = check_runtime_invariants(resolved, store, workforce_status)
    if not alerts:
        return []
    path = resolved / "runtime_invariants.jsonl"
    try:
        with open(path, "a", encoding="utf-8") as stream:
            for alert in alerts:
                stream.write(json.dumps(alert) + "\n")
    except OSError:
        pass
    return alerts
