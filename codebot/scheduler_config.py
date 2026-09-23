"""Adaptive Scheduler Configuration for CodeBot.

Purpose
-------
Centralizes all configurable parameters for the 30-slot adaptive concurrency
scheduler. Loaded from YAML or constructed with sensible defaults. Every
magic number in the scheduler lives here — nothing is hardcoded in the
control loop.

Why
---
The spec (§48) requires configuration over hardcoding. Different projects
have different scales, budgets, and maturity levels. A small library needs
different watermarks than a monorepo.

Invariants
----------
- stdlib-only (dataclasses, json, pathlib)
- Frozen dataclass once created — no mutation during a scheduler run
- All values validated at construction time
- YAML parsing uses a minimal stdlib parser (no PyYAML dependency)
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_CONCURRENT_AGENTS = int(os.getenv("CODEBOT_MAX_CONCURRENT", "90"))


@dataclass(frozen=True)
class BacklogConfig:
    """Ready backlog watermark targets (§7)."""
    low_watermark: int = 20
    target: int = 50
    high_watermark: int = 100

    def validate(self) -> None:
        if not (0 < self.low_watermark <= self.target <= self.high_watermark):
            raise ValueError(
                f"Invalid backlog watermarks: low={self.low_watermark} "
                f"target={self.target} high={self.high_watermark}"
            )


@dataclass(frozen=True)
class DiscoveryConfig:
    """Discovery capacity bounds and cooldowns (§8-12, §40)."""
    minimum_slots: int = 2
    maximum_fraction: float = 0.75
    floor_fraction: float = 0.05
    cooldown_seconds: int = 3600
    max_duplicate_rate: float = 0.5
    min_yield_to_continue: float = 0.05

    def validate(self) -> None:
        if not (0.0 <= self.floor_fraction <= self.maximum_fraction <= 1.0):
            raise ValueError("Discovery fractions out of range")
        if self.minimum_slots < 0:
            raise ValueError("minimum_slots must be >= 0")


@dataclass(frozen=True)
class ImplementationConfig:
    """Implementation WIP limits (§14)."""
    maximum_fraction: float = 0.60

    def validate(self) -> None:
        if not (0.0 < self.maximum_fraction <= 1.0):
            raise ValueError("Implementation max fraction out of range")


@dataclass(frozen=True)
class ReviewConfig:
    """Review capacity reservations (§19)."""
    minimum_when_pending: int = 2

    def validate(self) -> None:
        if self.minimum_when_pending < 0:
            raise ValueError("Review minimum must be >= 0")


@dataclass(frozen=True)
class VerificationConfig:
    """Verification capacity reservations (§19)."""
    minimum_when_pending: int = 2

    def validate(self) -> None:
        if self.minimum_when_pending < 0:
            raise ValueError("Verification minimum must be >= 0")


@dataclass(frozen=True)
class WorkforceConfig:
    """Flow-aware allocation bounds for engineering pipeline stages."""
    review_floor: int = 2
    verification_floor: int = 2
    decomposition_floor: int = 1
    planning_floor: int = 1
    implementation_maximum_fraction: float = 0.60
    discovery_maximum_fraction: float = 0.75
    target_drain_seconds: int = 300

    def validate(self) -> None:
        if min(
            self.review_floor,
            self.verification_floor,
            self.decomposition_floor,
            self.planning_floor,
        ) < 0:
            raise ValueError("Workforce floors must be >= 0")
        if not (0.0 <= self.implementation_maximum_fraction <= 1.0):
            raise ValueError("Workforce implementation maximum fraction out of range")
        if not (0.0 <= self.discovery_maximum_fraction <= 1.0):
            raise ValueError("Workforce discovery maximum fraction out of range")
        if self.target_drain_seconds <= 0:
            raise ValueError("Workforce target drain duration must be positive")


@dataclass(frozen=True)
class LifecycleEfficiencyConfig:
    """Opt-in controls for lifecycle efficiency optimizations.

    All controls default to off/disabled to preserve existing behavior.
    Operators enable them individually after verifying quality invariants
    hold under pilot conditions.
    """
    scoring_dispatch_enabled: bool = True
    extended_cache_enabled: bool = True
    lifecycle_telemetry_enabled: bool = True
    recommendations_enabled: bool = True
    pilot_rework_rate_threshold: float = 0.30
    pilot_audit_reopen_threshold: float = 0.20
    auto_disable_on_quality_regression: bool = True

    def validate(self) -> None:
        if not (0.0 <= self.pilot_rework_rate_threshold <= 1.0):
            raise ValueError("pilot_rework_rate_threshold must be in [0, 1]")
        if not (0.0 <= self.pilot_audit_reopen_threshold <= 1.0):
            raise ValueError("pilot_audit_reopen_threshold must be in [0, 1]")


@dataclass(frozen=True)
class WorkerConfig:
    """Worker lifecycle parameters (§25-26)."""
    heartbeat_timeout_seconds: int = 600
    lease_duration_seconds: int = 1800
    max_retries: int = 3
    stuck_multiplier: float = 2.0

    def validate(self) -> None:
        if self.heartbeat_timeout_seconds <= 0:
            raise ValueError("heartbeat_timeout must be positive")
        if self.lease_duration_seconds <= 0:
            raise ValueError("lease_duration must be positive")
        if self.max_retries < 1:
            raise ValueError("max_retries must be >= 1")


@dataclass(frozen=True)
class CostConfig:
    """Budget constraints (§28)."""
    hourly_limit_usd: float = 10.0
    daily_limit_usd: float = 100.0
    per_ticket_limit_tokens: int = 500_000

    def validate(self) -> None:
        if self.hourly_limit_usd <= 0:
            raise ValueError("hourly_limit must be positive")
        if self.daily_limit_usd <= 0:
            raise ValueError("daily_limit must be positive")


@dataclass(frozen=True)
class HysteresisConfig:
    """Anti-oscillation parameters (§36)."""
    min_allocation_duration_seconds: int = 300
    shift_threshold: float = 0.15
    rolling_window_ticks: int = 5

    def validate(self) -> None:
        if self.min_allocation_duration_seconds < 0:
            raise ValueError("min_allocation_duration must be >= 0")
        if not (0.0 < self.shift_threshold <= 1.0):
            raise ValueError("shift_threshold out of range")


@dataclass(frozen=True)
class PriorityAgingConfig:
    """Fairness vs priority aging (§27)."""
    aging_start_seconds: int = 86400
    aging_rate: float = 0.1
    max_age_bonus: float = 50.0

    def validate(self) -> None:
        if self.aging_rate < 0:
            raise ValueError("aging_rate must be >= 0")


@dataclass(frozen=True)
class IntegrationConfig:
    """Integration queue parameters (§23)."""
    max_integration_queue: int = 20
    rebase_required: bool = True


@dataclass(frozen=True)
class SchedulerConfig:
    """Top-level scheduler configuration aggregating all sub-configs."""
    max_slots: int = MAX_CONCURRENT_AGENTS
    scheduler_interval_seconds: int = 30
    backlog: BacklogConfig = field(default_factory=BacklogConfig)
    discovery: DiscoveryConfig = field(default_factory=DiscoveryConfig)
    implementation: ImplementationConfig = field(default_factory=ImplementationConfig)
    review: ReviewConfig = field(default_factory=ReviewConfig)
    verification: VerificationConfig = field(default_factory=VerificationConfig)
    workforce: WorkforceConfig = field(default_factory=WorkforceConfig)
    lifecycle_efficiency: LifecycleEfficiencyConfig = field(default_factory=LifecycleEfficiencyConfig)
    worker: WorkerConfig = field(default_factory=WorkerConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    hysteresis: HysteresisConfig = field(default_factory=HysteresisConfig)
    priority_aging: PriorityAgingConfig = field(default_factory=PriorityAgingConfig)
    integration: IntegrationConfig = field(default_factory=IntegrationConfig)

    def validate(self) -> SchedulerConfig:
        """Validate all sub-configs. Returns self for chaining."""
        if self.max_slots < 1:
            raise ValueError("max_slots must be >= 1")
        if self.scheduler_interval_seconds < 1:
            raise ValueError("scheduler_interval must be >= 1")
        self.backlog.validate()
        self.discovery.validate()
        self.implementation.validate()
        self.review.validate()
        self.verification.validate()
        self.workforce.validate()
        self.lifecycle_efficiency.validate()
        self.worker.validate()
        self.cost.validate()
        self.hysteresis.validate()
        self.priority_aging.validate()
        return self

    def to_dict(self) -> dict[str, Any]:
        """Serialize to nested dict for JSON/YAML output."""
        return {
            "max_slots": self.max_slots,
            "scheduler_interval_seconds": self.scheduler_interval_seconds,
            "backlog": {
                "low_watermark": self.backlog.low_watermark,
                "target": self.backlog.target,
                "high_watermark": self.backlog.high_watermark,
            },
            "discovery": {
                "minimum_slots": self.discovery.minimum_slots,
                "maximum_fraction": self.discovery.maximum_fraction,
                "floor_fraction": self.discovery.floor_fraction,
                "cooldown_seconds": self.discovery.cooldown_seconds,
                "max_duplicate_rate": self.discovery.max_duplicate_rate,
                "min_yield_to_continue": self.discovery.min_yield_to_continue,
            },
            "implementation": {
                "maximum_fraction": self.implementation.maximum_fraction,
            },
            "review": {
                "minimum_when_pending": self.review.minimum_when_pending,
            },
            "verification": {
                "minimum_when_pending": self.verification.minimum_when_pending,
            },
            "workforce": {
                "review_floor": self.workforce.review_floor,
                "verification_floor": self.workforce.verification_floor,
                "decomposition_floor": self.workforce.decomposition_floor,
                "planning_floor": self.workforce.planning_floor,
                "implementation_maximum_fraction": self.workforce.implementation_maximum_fraction,
                "discovery_maximum_fraction": self.workforce.discovery_maximum_fraction,
                "target_drain_seconds": self.workforce.target_drain_seconds,
            },
            "lifecycle_efficiency": {
                "scoring_dispatch_enabled": self.lifecycle_efficiency.scoring_dispatch_enabled,
                "extended_cache_enabled": self.lifecycle_efficiency.extended_cache_enabled,
                "lifecycle_telemetry_enabled": self.lifecycle_efficiency.lifecycle_telemetry_enabled,
                "recommendations_enabled": self.lifecycle_efficiency.recommendations_enabled,
                "pilot_rework_rate_threshold": self.lifecycle_efficiency.pilot_rework_rate_threshold,
                "pilot_audit_reopen_threshold": self.lifecycle_efficiency.pilot_audit_reopen_threshold,
                "auto_disable_on_quality_regression": self.lifecycle_efficiency.auto_disable_on_quality_regression,
            },
            "worker": {
                "heartbeat_timeout_seconds": self.worker.heartbeat_timeout_seconds,
                "lease_duration_seconds": self.worker.lease_duration_seconds,
                "max_retries": self.worker.max_retries,
                "stuck_multiplier": self.worker.stuck_multiplier,
            },
            "cost": {
                "hourly_limit_usd": self.cost.hourly_limit_usd,
                "daily_limit_usd": self.cost.daily_limit_usd,
                "per_ticket_limit_tokens": self.cost.per_ticket_limit_tokens,
            },
            "hysteresis": {
                "min_allocation_duration_seconds": self.hysteresis.min_allocation_duration_seconds,
                "shift_threshold": self.hysteresis.shift_threshold,
                "rolling_window_ticks": self.hysteresis.rolling_window_ticks,
            },
            "priority_aging": {
                "aging_start_seconds": self.priority_aging.aging_start_seconds,
                "aging_rate": self.priority_aging.aging_rate,
                "max_age_bonus": self.priority_aging.max_age_bonus,
            },
            "integration": {
                "max_integration_queue": self.integration.max_integration_queue,
                "rebase_required": self.integration.rebase_required,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def default(cls) -> SchedulerConfig:
        """Return validated default configuration."""
        return cls().validate()

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> SchedulerConfig:
        """Construct from nested dict (parsed YAML or JSON)."""
        d = dict(data)
        config = cls(
            max_slots=int(d.get("max_slots", MAX_CONCURRENT_AGENTS)),
            scheduler_interval_seconds=int(d.get("scheduler_interval_seconds", 30)),
            backlog=BacklogConfig(**d["backlog"]) if "backlog" in d else BacklogConfig(),
            discovery=DiscoveryConfig(**d["discovery"]) if "discovery" in d else DiscoveryConfig(),
            implementation=ImplementationConfig(**d["implementation"]) if "implementation" in d else ImplementationConfig(),
            review=ReviewConfig(**d["review"]) if "review" in d else ReviewConfig(),
            verification=VerificationConfig(**d["verification"]) if "verification" in d else VerificationConfig(),
            workforce=WorkforceConfig(**d["workforce"]) if "workforce" in d else WorkforceConfig(),
            lifecycle_efficiency=LifecycleEfficiencyConfig(**d["lifecycle_efficiency"]) if "lifecycle_efficiency" in d else LifecycleEfficiencyConfig(),
            worker=WorkerConfig(**d["worker"]) if "worker" in d else WorkerConfig(),
            cost=CostConfig(**d["cost"]) if "cost" in d else CostConfig(),
            hysteresis=HysteresisConfig(**d["hysteresis"]) if "hysteresis" in d else HysteresisConfig(),
            priority_aging=PriorityAgingConfig(**d["priority_aging"]) if "priority_aging" in d else PriorityAgingConfig(),
            integration=IntegrationConfig(**d["integration"]) if "integration" in d else IntegrationConfig(),
        )
        return config.validate()

    @classmethod
    def from_json(cls, raw: str) -> SchedulerConfig:
        return cls.from_dict(json.loads(raw))


def _parse_simple_yaml(text: str) -> dict[str, Any]:
    """Minimal YAML-subset parser for scheduler config files.

    Supports: scalars, nested dicts (indentation-based), lists (- prefix).
    Does NOT support: anchors, multi-line strings, flow style.
    This avoids a PyYAML dependency while handling typical config files.
    """
    result: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(0, result)]

    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        indent = len(line) - len(line.lstrip())

        # Pop stack to find parent at correct indentation level
        while len(stack) > 1 and stack[-1][0] >= indent:
            stack.pop()

        parent = stack[-1][1]

        if stripped.startswith("- "):
            item_text = stripped[2:].strip()
            if isinstance(parent, list):
                if ":" in item_text:
                    k, _, v = item_text.partition(":")
                    item_dict: dict[str, Any] = {k.strip(): _parse_scalar(v.strip())}
                    parent.append(item_dict)
                else:
                    parent.append(_parse_scalar(item_text))
            continue

        if ":" not in stripped:
            continue

        key, _, val = stripped.partition(":")
        key = key.strip()
        val = val.strip()

        if val:
            parent[key] = _parse_scalar(val)
        else:
            # Check if next non-empty line is a list item
            child: Any = {}
            parent[key] = child
            stack.append((indent + 2, child))

    return result


def _parse_scalar(val: str) -> Any:
    """Parse a scalar YAML value into Python type."""
    val = val.strip('"').strip("'")
    if val.lower() in ("true", "yes"):
        return True
    if val.lower() in ("false", "no"):
        return False
    if val.lower() in ("null", "none", "~"):
        return None
    try:
        return int(val)
    except ValueError:
        pass
    try:
        return float(val)
    except ValueError:
        pass
    return val


def load_scheduler_config(path: Path | None = None) -> SchedulerConfig:
    """Load scheduler config from YAML file, falling back to defaults.

    Searches standard locations if path is not provided:
    1. .codebot/scheduler.yaml
    2. .codebot/scheduler.json
    3. Default configuration
    """
    if path and path.exists():
        text = path.read_text(encoding="utf-8")
        if path.suffix == ".json":
            return SchedulerConfig.from_json(text)
        parsed = _parse_simple_yaml(text)
        scheduler_data = parsed.get("scheduler", parsed)
        return SchedulerConfig.from_dict(scheduler_data)

    # Search standard locations
    search_paths = [
        Path(".codebot/scheduler.yaml"),
        Path(".codebot/scheduler.yml"),
        Path(".codebot/scheduler.json"),
    ]
    for sp in search_paths:
        if sp.exists():
            return load_scheduler_config(sp)

    return SchedulerConfig.default()
