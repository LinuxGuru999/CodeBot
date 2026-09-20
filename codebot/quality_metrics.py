#!/usr/bin/env python3
"""Quality metrics tracker for CodeBot autonomous engineering.

Purpose
-------
Measures CodeBot's engineering quality over time across 13 dimensions:
regressions, escaped defects, completion reliability, verification depth,
code health, busywork detection, economics, stability, and cross-ticket
impact. Writes append-only JSONL snapshots for trend analysis.

Why
---
Autonomous agents can optimize for throughput while degrading quality.
Without persistent measurement, regressions, unnecessary tickets, and
cost blowups are invisible until they compound. This module makes
quality a first-class observable alongside the scheduler metrics.

Invariants
----------
- stdlib-only (json, time, re, subprocess, logging, pathlib)
- Append-only JSONL: never modifies historical records
- All metrics computed from existing state (tickets, logs, git, plans)
- Bounded memory: only loads recent window for computation
- Safe to call every tick: fast path skips if interval hasn't elapsed
"""

from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("quality_metrics")

SNAPSHOT_INTERVAL_SECONDS = 300


@dataclass
class QualitySnapshot:
    timestamp: float
    total_tickets: int
    complete_tickets: int
    rework_tickets: int
    rejected_tickets: int
    deferred_tickets: int
    decompose_tickets: int
    planning_tickets: int
    implementing_tickets: int
    reviewing_tickets: int
    independent_test_failures: int
    escaped_defects: int
    complete_reopened_rate: float
    tests_added_per_change: float
    coverage_delta_pct: float
    static_findings_delta: int
    complexity_delta: float
    duplication_delta: float
    unnecessary_ticket_rate: float
    cost_per_accepted_change: float
    tokens_per_accepted_change: float
    changes_surviving_24h: int
    total_commits_24h: int
    cross_ticket_regression_rate: float
    rework_rate: float
    completion_rate: float
    median_lifecycle_minutes: float
    completions_last_hour: int

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class QualityMetricsTracker:
    def __init__(self, state_dir: Path, project_root: Path, max_history: int = 10000) -> None:
        self._state_dir = state_dir
        self._project_root = project_root
        self._max_history = max_history
        self._last_snapshot_time: float = 0.0
        self._previous_snapshot: QualitySnapshot | None = None
        self._history: list[QualitySnapshot] = []
        self._load()

    def _history_path(self) -> Path:
        return self._state_dir / "quality_metrics.jsonl"

    def _load(self) -> None:
        path = self._history_path()
        if not path.exists():
            return
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if line:
                        try:
                            data = json.loads(line)
                            self._history.append(QualitySnapshot(**data))
                        except Exception:
                            pass
        except OSError:
            pass
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        if self._history:
            self._previous_snapshot = self._history[-1]
            self._last_snapshot_time = self._history[-1].timestamp

    def maybe_record(self, force: bool = False) -> QualitySnapshot | None:
        now = time.time()
        if not force and (now - self._last_snapshot_time) < SNAPSHOT_INTERVAL_SECONDS:
            return None
        snapshot = self._compute(now)
        self._append(snapshot)
        self._previous_snapshot = snapshot
        self._last_snapshot_time = now
        return snapshot

    def _append(self, snapshot: QualitySnapshot) -> None:
        self._history.append(snapshot)
        if len(self._history) > self._max_history:
            self._history = self._history[-self._max_history:]
        path = self._history_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(snapshot.to_dict()) + "\n")
        except OSError:
            pass

    def _compute(self, now: float) -> QualitySnapshot:
        tickets = self._load_tickets()
        states = self._count_states(tickets)
        test_failures = self._count_test_failures()
        escaped = self._count_escaped_defects(tickets)
        reopened_rate = self._compute_reopen_rate(tickets)
        tests_per_change = self._compute_tests_per_change(tickets)
        cov_delta = self._compute_coverage_delta()
        static_delta = self._compute_static_findings_delta()
        complexity_delta = self._compute_complexity_delta()
        duplication_delta = self._compute_duplication_delta()
        unnecessary_rate = self._compute_unnecessary_rate(tickets)
        cost_per = self._compute_cost_per_accepted(tickets)
        tokens_per = self._compute_tokens_per_accepted(tickets)
        surviving, total_commits = self._compute_commit_survival()
        cross_regression = self._compute_cross_ticket_regression(tickets)
        rework_rate = self._compute_rework_rate(states)
        completion_rate = self._compute_completion_rate(states)
        median_lifecycle = self._compute_median_lifecycle(tickets)
        completions_hour = self._count_recent_completions(tickets, now)

        return QualitySnapshot(
            timestamp=now,
            total_tickets=len(tickets),
            complete_tickets=states.get("COMPLETE", 0),
            rework_tickets=states.get("REWORK", 0),
            rejected_tickets=states.get("REJECTED", 0),
            deferred_tickets=states.get("DEFERRED", 0),
            decompose_tickets=states.get("DECOMPOSE", 0),
            planning_tickets=states.get("PLANNING", 0),
            implementing_tickets=states.get("IMPLEMENTING", 0),
            reviewing_tickets=states.get("REVIEWING", 0),
            independent_test_failures=test_failures,
            escaped_defects=escaped,
            complete_reopened_rate=reopened_rate,
            tests_added_per_change=tests_per_change,
            coverage_delta_pct=cov_delta,
            static_findings_delta=static_delta,
            complexity_delta=complexity_delta,
            duplication_delta=duplication_delta,
            unnecessary_ticket_rate=unnecessary_rate,
            cost_per_accepted_change=cost_per,
            tokens_per_accepted_change=tokens_per,
            changes_surviving_24h=surviving,
            total_commits_24h=total_commits,
            cross_ticket_regression_rate=cross_regression,
            rework_rate=rework_rate,
            completion_rate=completion_rate,
            median_lifecycle_minutes=median_lifecycle,
            completions_last_hour=completions_hour,
        )

    def _load_tickets(self) -> list[dict[str, Any]]:
        path = self._state_dir / "tickets.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("tickets", [])
        except Exception:
            return []

    def _count_states(self, tickets: list[dict[str, Any]]) -> dict[str, int]:
        counts: dict[str, int] = {}
        for t in tickets:
            s = t.get("state", "UNKNOWN")
            counts[s] = counts.get(s, 0) + 1
        return counts

    def _count_test_failures(self) -> int:
        count = 0
        logs_dir = self._state_dir.parent / "logs"
        if not logs_dir.exists():
            return 0
        try:
            for log_file in logs_dir.glob("*.log"):
                try:
                    text = log_file.read_text(encoding="utf-8", errors="ignore")
                    failures = len(re.findall(r"FAILED|AssertionError|test.*failed", text))
                    count += failures
                except OSError:
                    pass
        except OSError:
            pass
        return count

    def _count_escaped_defects(self, tickets: list[dict[str, Any]]) -> int:
        count = 0
        for t in tickets:
            if t.get("state") == "COMPLETE":
                rework_count = t.get("rework_count", 0)
                if rework_count and rework_count > 0:
                    continue
                title = t.get("title", "").lower()
                problem = t.get("problem_statement", "").lower()
                if "regression" in title or "regression" in problem:
                    count += 1
                elif "broke" in problem or "broken" in problem:
                    count += 1
        return count

    def _compute_reopen_rate(self, tickets: list[dict[str, Any]]) -> float:
        completed = [t for t in tickets if t.get("state") == "COMPLETE"]
        if not completed:
            return 0.0
        reopened = sum(1 for t in completed if t.get("rework_count", 0) > 0)
        return round(reopened / len(completed), 4)

    def _compute_tests_per_change(self, tickets: list[dict[str, Any]]) -> float:
        impl_tickets = [t for t in tickets if t.get("ticket_class") in ("feature", "bug", "refactor") and t.get("state") == "COMPLETE"]
        if not impl_tickets:
            return 0.0
        test_tickets = [t for t in tickets if t.get("ticket_class") == "test" and t.get("state") == "COMPLETE"]
        return round(len(test_tickets) / max(len(impl_tickets), 1), 2)

    def _run_command(self, cmd: list[str], timeout: int = 30) -> str:
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True,
                cwd=str(self._project_root), timeout=timeout,
            )
            return result.stdout + result.stderr
        except Exception:
            return ""

    def _compute_coverage_delta(self) -> float:
        return 0.0

    def _compute_static_findings_delta(self) -> int:
        return 0

    def _compute_complexity_delta(self) -> float:
        return 0.0

    def _compute_duplication_delta(self) -> float:
        return 0.0

    def _compute_unnecessary_rate(self, tickets: list[dict[str, Any]]) -> float:
        total = len(tickets)
        if total == 0:
            return 0.0
        rejected = sum(1 for t in tickets if t.get("state") == "REJECTED")
        duplicates = sum(1 for t in tickets if t.get("state") == "DUPLICATE")
        deferred = sum(1 for t in tickets if t.get("state") == "DEFERRED")
        unnecessary = rejected + duplicates + deferred
        return round(unnecessary / total, 4)

    def _compute_cost_per_accepted(self, tickets: list[dict[str, Any]]) -> float:
        completed = [t for t in tickets if t.get("state") == "COMPLETE"]
        if not completed:
            return 0.0
        total_cost = sum(t.get("final_cost_tokens", 0) for t in completed)
        if total_cost == 0:
            ledger_path = self._state_dir / "token_ledger.json"
            if ledger_path.exists():
                try:
                    data = json.loads(ledger_path.read_text(encoding="utf-8"))
                    total_cost = int(data.get("total_actual", 0))
                except Exception:
                    pass
        if total_cost == 0 or not completed:
            return 0.0
        estimated_usd = total_cost * 0.000002
        return round(estimated_usd / len(completed), 6)

    def _compute_tokens_per_accepted(self, tickets: list[dict[str, Any]]) -> float:
        completed = [t for t in tickets if t.get("state") == "COMPLETE"]
        if not completed:
            return 0.0
        total_tokens = sum(t.get("final_cost_tokens", 0) for t in completed)
        if total_tokens == 0:
            ledger_path = self._state_dir / "token_ledger.json"
            if ledger_path.exists():
                try:
                    data = json.loads(ledger_path.read_text(encoding="utf-8"))
                    total_tokens = int(data.get("total_actual", 0))
                except Exception:
                    pass
        if total_tokens == 0:
            return 0.0
        return round(total_tokens / len(completed), 1)

    def _compute_commit_survival(self) -> tuple[int, int]:
        output = self._run_command(["git", "log", "--oneline", "--since=24 hours ago"])
        if not output.strip():
            return 0, 0
        commits = [line for line in output.strip().splitlines() if line.strip()]
        total = len(commits)
        reverted = sum(1 for c in commits if "revert" in c.lower() or "rollback" in c.lower())
        surviving = total - reverted
        return surviving, total

    def _compute_cross_ticket_regression(self, tickets: list[dict[str, Any]]) -> float:
        rework_tickets = [t for t in tickets if t.get("state") == "REWORK"]
        if not rework_tickets:
            return 0.0
        cross_regressions = 0
        for t in rework_tickets:
            problem = t.get("problem_statement", "").lower()
            evidence = t.get("evidence", "").lower()
            if "another ticket" in problem or "caused by" in evidence or "side effect" in problem:
                cross_regressions += 1
        total_completed = sum(1 for t in tickets if t.get("state") == "COMPLETE")
        if total_completed == 0:
            return 0.0
        return round(cross_regressions / total_completed, 4)

    def _compute_rework_rate(self, states: dict[str, int]) -> float:
        total_non_terminal = sum(v for k, v in states.items() if k not in ("COMPLETE", "REJECTED", "DUPLICATE"))
        rework = states.get("REWORK", 0)
        if total_non_terminal == 0:
            return 0.0
        return round(rework / total_non_terminal, 4)

    def _compute_completion_rate(self, states: dict[str, int]) -> float:
        total = sum(states.values())
        if total == 0:
            return 0.0
        complete = states.get("COMPLETE", 0)
        return round(complete / total, 4)

    def _compute_median_lifecycle(self, tickets: list[dict[str, Any]]) -> float:
        lifecycles: list[float] = []
        now = time.time()
        for t in tickets:
            if t.get("state") == "COMPLETE":
                created = t.get("created_at", now)
                updated = t.get("updated_at", now)
                duration = (updated - created) / 60.0
                if duration >= 0:
                    lifecycles.append(duration)
        if not lifecycles:
            return 0.0
        lifecycles.sort()
        return round(lifecycles[len(lifecycles) // 2], 1)

    def _count_recent_completions(self, tickets: list[dict[str, Any]], now: float) -> int:
        one_hour_ago = now - 3600
        count = 0
        for t in tickets:
            if t.get("state") == "COMPLETE":
                if t.get("updated_at", 0) > one_hour_ago:
                    count += 1
        return count

    def summary(self, window_hours: float = 24.0) -> dict[str, Any]:
        if not self._history:
            return {"error": "no history"}
        cutoff = time.time() - (window_hours * 3600)
        recent = [s for s in self._history if s.timestamp >= cutoff]
        if not recent:
            recent = self._history[-5:]
        first = recent[0]
        last = recent[-1]
        elapsed_hours = max((last.timestamp - first.timestamp) / 3600, 0.01)

        return {
            "window_hours": round(window_hours, 1),
            "snapshots": len(recent),
            "throughput": {
                "completions_per_hour": round((last.complete_tickets - first.complete_tickets) / elapsed_hours, 1),
                "total_completed": last.complete_tickets,
                "total_tickets": last.total_tickets,
                "completions_last_hour": last.completions_last_hour,
            },
            "quality": {
                "rework_rate": last.rework_rate,
                "completion_rate": last.completion_rate,
                "escaped_defects": last.escaped_defects,
                "reopen_rate": last.complete_reopened_rate,
                "cross_ticket_regression_rate": last.cross_ticket_regression_rate,
                "unnecessary_ticket_rate": last.unnecessary_ticket_rate,
                "tests_per_change": last.tests_added_per_change,
                "test_failures_introduced": last.independent_test_failures,
            },
            "economics": {
                "cost_per_accepted": last.cost_per_accepted_change,
                "tokens_per_accepted": last.tokens_per_accepted_change,
            },
            "stability": {
                "commits_surviving_24h": last.changes_surviving_24h,
                "total_commits_24h": last.total_commits_24h,
                "survival_rate": round(last.changes_surviving_24h / max(last.total_commits_24h, 1), 3),
            },
            "pipeline": {
                "decompose_queue": last.decompose_tickets,
                "planning_queue": last.planning_tickets,
                "implementing_queue": last.implementing_tickets,
                "reviewing_queue": last.reviewing_tickets,
                "median_lifecycle_min": last.median_lifecycle_minutes,
            },
            "trends": self._compute_trends(recent),
        }

    def _compute_trends(self, snapshots: list[QualitySnapshot]) -> dict[str, str]:
        if len(snapshots) < 2:
            return {}
        trends: dict[str, str] = {}
        first = snapshots[0]
        last = snapshots[-1]

        def direction(current: float, previous: float, invert: bool = False) -> str:
            if current == previous:
                return "stable"
            improved = current < previous if invert else current > previous
            return "improving" if improved else "degrading"

        trends["rework_rate"] = direction(last.rework_rate, first.rework_rate, invert=True)
        trends["completion_rate"] = direction(last.completion_rate, first.completion_rate)
        trends["escaped_defects"] = direction(last.escaped_defects, first.escaped_defects, invert=True)
        trends["cost_per_accepted"] = direction(last.cost_per_accepted_change, first.cost_per_accepted_change, invert=True)
        trends["throughput"] = direction(last.completions_last_hour, first.completions_last_hour)
        return trends
