#!/usr/bin/env python3
"""Throughput metrics collector for the adaptive scheduler.

Purpose
-------
Aggregates all scheduler telemetry required by spec §30-31: slot utilization,
tickets completed per hour/day, mean cycle time, review queue age, discovery
yield, cost per ticket, first-pass review rate, rework rate, human intervention
rate, and productive (not just raw) slot utilization.

Why
---
The scheduler's decisions are only as good as the feedback signal. Without
metrics, there's no way to know if the 30-slot pool is actually producing
verified engineering work or just burning tokens on duplicate discovery.
Spec §31 explicitly distinguishes PRODUCTIVE_SLOT_UTILIZATION from raw
utilization — a worker doing redundant audits doesn't count.

Invariants
----------
- stdlib-only (dataclasses, time, json, pathlib)
- All metrics computed from event history; no external dependencies
- Atomic writes via tmp->replace pattern
- Bounded history to prevent unbounded memory growth
- Pure computation functions; I/O isolated to load/save
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SchedulerMetrics:
    """Point-in-time snapshot of all scheduler metrics (§30)."""
    # Slot metrics
    slots_total: int = 30
    slots_active: int = 0
    slots_idle: int = 0
    slots_by_role: dict[str, int] = field(default_factory=dict)

    # Queue depths
    tickets_ready: int = 0
    tickets_implementing: int = 0
    tickets_reviewing: int = 0
    tickets_verifying: int = 0
    tickets_rework: int = 0
    tickets_blocked: int = 0
    discovery_candidates: int = 0
    validated_candidates: int = 0
    duplicate_candidates: int = 0

    # Throughput
    tickets_completed_per_hour: float = 0.0
    tickets_completed_per_day: float = 0.0
    mean_ticket_cycle_time_seconds: float = 0.0

    # Quality
    review_queue_age_seconds: float = 0.0
    first_pass_review_rate: float = 0.0
    rework_rate: float = 0.0
    human_intervention_rate: float = 0.0

    # Discovery
    discovery_yield_rate: float = 0.0

    # Cost
    cost_per_hour_usd: float = 0.0
    cost_per_ticket_usd: float = 0.0

    # Utilization (§31)
    slot_utilization: float = 0.0
    productive_slot_utilization: float = 0.0

    # Economics (budget health)
    budget_state: str = "ok"
    budget_exhausted: bool = False
    budget_warning: bool = False
    hourly_spend_usd: float = 0.0
    daily_spend_usd: float = 0.0
    hourly_limit_usd: float = 10.0
    daily_limit_usd: float = 100.0
    hourly_burn_rate: float = 0.0
    daily_burn_rate: float = 0.0
    remaining_hourly_usd: float = 10.0
    remaining_daily_usd: float = 100.0

    # Mode
    scheduler_mode: str = "BALANCED"

    timestamp: float = field(default_factory=time.time)

    def summary(self) -> dict[str, Any]:
        return {
            "slots": {
                "total": self.slots_total,
                "active": self.slots_active,
                "idle": self.slots_idle,
                "by_role": self.slots_by_role,
                "utilization": round(self.slot_utilization, 4),
                "productive_utilization": round(self.productive_slot_utilization, 4),
            },
            "queues": {
                "ready": self.tickets_ready,
                "implementing": self.tickets_implementing,
                "reviewing": self.tickets_reviewing,
                "verifying": self.tickets_verifying,
                "rework": self.tickets_rework,
                "blocked": self.tickets_blocked,
                "candidates": self.discovery_candidates,
            },
            "throughput": {
                "per_hour": round(self.tickets_completed_per_hour, 2),
                "per_day": round(self.tickets_completed_per_day, 2),
                "mean_cycle_time_s": round(self.mean_ticket_cycle_time_seconds, 1),
            },
            "quality": {
                "first_pass_rate": round(self.first_pass_review_rate, 4),
                "rework_rate": round(self.rework_rate, 4),
                "human_intervention_rate": round(self.human_intervention_rate, 4),
                "review_queue_age_s": round(self.review_queue_age_seconds, 1),
            },
            "discovery": {
                "yield_rate": round(self.discovery_yield_rate, 4),
                "validated": self.validated_candidates,
                "duplicates": self.duplicate_candidates,
            },
            "cost": {
                "per_hour_usd": round(self.cost_per_hour_usd, 4),
                "per_ticket_usd": round(self.cost_per_ticket_usd, 4),
            },
            "economics": {
                "budget_state": self.budget_state,
                "budget_exhausted": self.budget_exhausted,
                "budget_warning": self.budget_warning,
                "hourly_spend_usd": round(self.hourly_spend_usd, 4),
                "daily_spend_usd": round(self.daily_spend_usd, 4),
                "hourly_limit_usd": round(self.hourly_limit_usd, 4),
                "daily_limit_usd": round(self.daily_limit_usd, 4),
                "hourly_burn_rate": round(self.hourly_burn_rate, 4),
                "daily_burn_rate": round(self.daily_burn_rate, 4),
                "remaining_hourly_usd": round(self.remaining_hourly_usd, 4),
                "remaining_daily_usd": round(self.remaining_daily_usd, 4),
            },
            "mode": self.scheduler_mode,
            "timestamp": self.timestamp,
        }


@dataclass
class CompletionRecord:
    """Tracks a single ticket completion for throughput calculation."""
    ticket_id: str
    completed_at: float
    created_at: float
    was_rework: bool = False
    was_productive: bool = True
    cost_tokens: int = 0


@dataclass
class MetricsAccumulator:
    """Mutable accumulator that builds SchedulerMetrics over time.

    Feed events into this; call build() to get a frozen snapshot.
    """
    completions: list[CompletionRecord] = field(default_factory=list)
    total_reviews: int = 0
    first_pass_reviews: int = 0
    total_reworks: int = 0
    human_interventions: int = 0
    total_discovery_scans: int = 0
    validated_discoveries: int = 0
    duplicate_discoveries: int = 0
    total_cost_tokens: int = 0
    hourly_cost_usd: float = 0.0
    max_history: int = 5000

    def record_completion(
        self,
        ticket_id: str,
        completed_at: float,
        created_at: float,
        was_rework: bool = False,
        was_productive: bool = True,
        cost_tokens: int = 0,
    ) -> None:
        self.completions.append(CompletionRecord(
            ticket_id=ticket_id,
            completed_at=completed_at,
            created_at=created_at,
            was_rework=was_rework,
            was_productive=was_productive,
            cost_tokens=cost_tokens,
        ))
        self.total_cost_tokens += cost_tokens
        if was_rework:
            self.total_reworks += 1
        self._prune()

    def record_review(self, passed_first: bool) -> None:
        self.total_reviews += 1
        if passed_first:
            self.first_pass_reviews += 1

    def record_discovery(self, validated: int, duplicates: int) -> None:
        self.total_discovery_scans += 1
        self.validated_discoveries += validated
        self.duplicate_discoveries += duplicates

    def record_human_intervention(self) -> None:
        self.human_interventions += 1

    def set_hourly_cost(self, usd: float) -> None:
        self.hourly_cost_usd = usd

    def _prune(self) -> None:
        if len(self.completions) > self.max_history:
            self.completions = self.completions[-self.max_history:]

    def build(
        self,
        pipeline_state: Any,
        scheduler_mode: str = "BALANCED",
        now: float | None = None,
        cost_config: Any | None = None,
    ) -> SchedulerMetrics:
        """Compute a frozen metrics snapshot from accumulated data.

        Args:
            pipeline_state: PipelineState (or compatible) with budget fields.
            scheduler_mode: Current scheduler mode string.
            now: Optional timestamp override.
            cost_config: Optional CostConfig (or SchedulerConfig) with hourly/daily USD limits.
        """
        now = now or time.time()
        ps = pipeline_state

        # Throughput calculations
        hour_ago = now - 3600
        day_ago = now - 86400
        recent_hour = [c for c in self.completions if c.completed_at > hour_ago]
        recent_day = [c for c in self.completions if c.completed_at > day_ago]

        completed_hour = len(recent_hour)
        completed_day = len(recent_day)

        # Mean cycle time (last 100 completions)
        cycle_times = [
            c.completed_at - c.created_at
            for c in self.completions[-100:]
            if c.completed_at > c.created_at
        ]
        mean_cycle = sum(cycle_times) / len(cycle_times) if cycle_times else 0.0

        # Quality rates
        first_pass = (
            self.first_pass_reviews / self.total_reviews
            if self.total_reviews > 0 else 0.0
        )
        total_completed = len(self.completions)
        rework_rate = (
            self.total_reworks / total_completed
            if total_completed > 0 else 0.0
        )
        human_rate = (
            self.human_interventions / total_completed
            if total_completed > 0 else 0.0
        )

        # Discovery yield
        total_findings = self.validated_discoveries + self.duplicate_discoveries
        discovery_yield = (
            self.validated_discoveries / total_findings
            if total_findings > 0 else 0.0
        )

        # Utilization (§31)
        active = ps.active_count if hasattr(ps, 'active_count') else 0
        total = ps.total_slots if hasattr(ps, 'total_slots') else 30
        raw_util = active / total if total > 0 else 0.0

        # Productive utilization: exclude non-productive workers
        productive_active = sum(
            1 for c in recent_hour if c.was_productive
        ) if recent_hour else 0
        productive_util = (
            productive_active / max(completed_hour, 1)
            if completed_hour > 0
            else raw_util * 0.9  # Estimate when no completions yet
        )
        productive_util = min(productive_util, 1.0)

        # Cost per ticket
        cost_per_ticket = (
            self.hourly_cost_usd / completed_hour
            if completed_hour > 0 else 0.0
        )

        # Review queue age
        review_age = 0.0
        if hasattr(ps, 'reviewing_count') and ps.reviewing_count > 0:
            # Approximate: use oldest active review worker
            review_workers = [
                w for w in (ps.active_workers if hasattr(ps, 'active_workers') else [])
                if 'review' in getattr(w, 'role', '').lower()
            ]
            if review_workers:
                oldest = min(w.started_at for w in review_workers)
                review_age = now - oldest

        slots_by_role = ps.active_by_role() if hasattr(ps, 'active_by_role') else {}

        # --- Economics / budget health ---
        # Extract spend from pipeline state
        hourly_spend = getattr(ps, 'hourly_spend_usd', 0.0)
        daily_spend = getattr(ps, 'daily_spend_usd', 0.0)
        budget_exhausted = getattr(ps, 'budget_exhausted', False)
        budget_warning = getattr(ps, 'budget_warning', False)

        # Extract limits from cost_config (CostConfig or SchedulerConfig)
        hourly_limit = 10.0
        daily_limit = 100.0
        if cost_config is not None:
            if hasattr(cost_config, 'cost'):
                # It's a SchedulerConfig — access nested CostConfig
                hourly_limit = getattr(cost_config.cost, 'hourly_limit_usd', hourly_limit)
                daily_limit = getattr(cost_config.cost, 'daily_limit_usd', daily_limit)
            else:
                hourly_limit = getattr(cost_config, 'hourly_limit_usd', hourly_limit)
                daily_limit = getattr(cost_config, 'daily_limit_usd', daily_limit)

        # Compute burn rates (guard divide-by-zero)
        hourly_burn_rate = (hourly_spend / hourly_limit) if hourly_limit > 0 else 0.0
        daily_burn_rate = (daily_spend / daily_limit) if daily_limit > 0 else 0.0

        # Remaining budget (clamp to non-negative)
        remaining_hourly = max(0.0, hourly_limit - hourly_spend)
        remaining_daily = max(0.0, daily_limit - daily_spend)

        # Budget state: derive from burn rate if not already set by token_budget
        if budget_exhausted:
            budget_state = "stop"
        elif budget_warning or daily_burn_rate >= 0.8:
            if daily_burn_rate >= 0.9:
                budget_state = "shed_tier3"
            else:
                budget_state = "warn"
        else:
            budget_state = "ok"

        return SchedulerMetrics(
            slots_total=total,
            slots_active=active,
            slots_idle=max(0, total - active),
            slots_by_role=slots_by_role,
            tickets_ready=getattr(ps, 'ready_count', 0),
            tickets_implementing=getattr(ps, 'implementing_count', 0),
            tickets_reviewing=getattr(ps, 'reviewing_count', 0),
            tickets_verifying=getattr(ps, 'verifying_count', 0),
            tickets_rework=getattr(ps, 'rework_count', 0),
            tickets_blocked=getattr(ps, 'blocked_count', 0),
            discovery_candidates=getattr(ps, 'candidate_count', 0),
            validated_candidates=self.validated_discoveries,
            duplicate_candidates=self.duplicate_discoveries,
            tickets_completed_per_hour=float(completed_hour),
            tickets_completed_per_day=float(completed_day),
            mean_ticket_cycle_time_seconds=mean_cycle,
            review_queue_age_seconds=review_age,
            first_pass_review_rate=first_pass,
            rework_rate=rework_rate,
            human_intervention_rate=human_rate,
            discovery_yield_rate=discovery_yield,
            cost_per_hour_usd=self.hourly_cost_usd,
            cost_per_ticket_usd=cost_per_ticket,
            slot_utilization=round(raw_util, 4),
            productive_slot_utilization=round(productive_util, 4),
            budget_state=budget_state,
            budget_exhausted=budget_exhausted,
            budget_warning=budget_warning,
            hourly_spend_usd=hourly_spend,
            daily_spend_usd=daily_spend,
            hourly_limit_usd=hourly_limit,
            daily_limit_usd=daily_limit,
            hourly_burn_rate=round(hourly_burn_rate, 4),
            daily_burn_rate=round(daily_burn_rate, 4),
            remaining_hourly_usd=round(remaining_hourly, 4),
            remaining_daily_usd=round(remaining_daily, 4),
            scheduler_mode=scheduler_mode,
            timestamp=now,
        )

    def save(self, path: Path) -> None:
        """Persist accumulator state for cross-restart continuity."""
        data = {
            "completions": [
                {
                    "ticket_id": c.ticket_id,
                    "completed_at": c.completed_at,
                    "created_at": c.created_at,
                    "was_rework": c.was_rework,
                    "was_productive": c.was_productive,
                    "cost_tokens": c.cost_tokens,
                }
                for c in self.completions[-self.max_history:]
            ],
            "total_reviews": self.total_reviews,
            "first_pass_reviews": self.first_pass_reviews,
            "total_reworks": self.total_reworks,
            "human_interventions": self.human_interventions,
            "total_discovery_scans": self.total_discovery_scans,
            "validated_discoveries": self.validated_discoveries,
            "duplicate_discoveries": self.duplicate_discoveries,
            "total_cost_tokens": self.total_cost_tokens,
            "hourly_cost_usd": self.hourly_cost_usd,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> MetricsAccumulator:
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            acc = cls(
                total_reviews=int(data.get("total_reviews", 0)),
                first_pass_reviews=int(data.get("first_pass_reviews", 0)),
                total_reworks=int(data.get("total_reworks", 0)),
                human_interventions=int(data.get("human_interventions", 0)),
                total_discovery_scans=int(data.get("total_discovery_scans", 0)),
                validated_discoveries=int(data.get("validated_discoveries", 0)),
                duplicate_discoveries=int(data.get("duplicate_discoveries", 0)),
                total_cost_tokens=int(data.get("total_cost_tokens", 0)),
                hourly_cost_usd=float(data.get("hourly_cost_usd", 0.0)),
            )
            for entry in data.get("completions", []):
                acc.completions.append(CompletionRecord(
                    ticket_id=entry["ticket_id"],
                    completed_at=float(entry["completed_at"]),
                    created_at=float(entry["created_at"]),
                    was_rework=bool(entry.get("was_rework", False)),
                    was_productive=bool(entry.get("was_productive", True)),
                    cost_tokens=int(entry.get("cost_tokens", 0)),
                ))
            return acc
        except Exception:
            return cls()
