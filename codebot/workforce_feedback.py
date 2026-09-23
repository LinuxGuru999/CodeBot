"""Persist bounded worker-duration observations for workforce planning."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from codebot.pipeline_state import PipelineState


@dataclass(frozen=True, slots=True)
class FlowSnapshot:
    review_per_worker_per_second: float = 0.0
    verification_per_worker_per_second: float = 0.0
    implementation_per_worker_per_second: float = 0.0


@dataclass(frozen=True, slots=True)
class WorkforceTarget:
    implementation: int = 0
    review: int = 0
    verification: int = 0
    rework: int = 0
    decomposition: int = 0
    planning: int = 0
    triage: int = 0
    discovery: int = 0

    @property
    def total(self) -> int:
        return (
            self.implementation
            + self.review
            + self.verification
            + self.rework
            + self.decomposition
            + self.planning
            + self.triage
            + self.discovery
        )


@dataclass(frozen=True, slots=True)
class WorkerDuration:
    """One completed worker duration attributed to a pipeline stage."""
    stage: str
    duration_seconds: float


@dataclass(frozen=True, slots=True)
class WorkforceStatus:
    """One operator-readable result of a workforce allocation decision."""
    timestamp: float
    max_slots: int
    active_workers: int
    pipeline: PipelineState
    flow: FlowSnapshot
    target: WorkforceTarget


def persist_workforce_status(path: Path, status: WorkforceStatus) -> None:
    """Atomically persist a workforce allocation decision for operators."""
    _ = path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "timestamp": status.timestamp,
        "capacity": {
            "active_workers": status.active_workers,
            "max_slots": status.max_slots,
        },
        "pipeline": {
            "ready": status.pipeline.ready_count,
            "decompose": status.pipeline.decompose_count,
            "planning": status.pipeline.planning_count,
            "implementation": status.pipeline.implementing_count + status.pipeline.implementation_ready_count,
            "implementation_ready": status.pipeline.implementation_ready_count,
            "implementing": status.pipeline.implementing_count,
            "reviewing": status.pipeline.reviewing_count,
            "verifying": status.pipeline.verifying_count,
            "rework": status.pipeline.rework_count,
        },
        "flow": {
            "implementation_per_worker_per_second": status.flow.implementation_per_worker_per_second,
            "review_per_worker_per_second": status.flow.review_per_worker_per_second,
            "verification_per_worker_per_second": status.flow.verification_per_worker_per_second,
        },
        "target": {
            "implementation": status.target.implementation,
            "review": status.target.review,
            "verification": status.target.verification,
            "rework": status.target.rework,
            "decomposition": status.target.decomposition,
            "planning": status.target.planning,
            "triage": status.target.triage,
            "discovery": status.target.discovery,
            "total": status.target.total,
        },
    }
    temporary = path.with_suffix(".tmp")
    _ = temporary.write_text(json.dumps(payload), encoding="utf-8")
    _ = temporary.replace(path)


@dataclass(slots=True)
class WorkforceFlowHistory:  # noqa: MUTABLE_OK
    """Bounded mutable sample store retained across orchestrator restarts."""
    samples: list[WorkerDuration] = field(default_factory=list)
    max_samples: int = 200

    def record(self, stage: str, duration_seconds: float) -> None:
        """Record one positive worker duration for a known pipeline stage."""
        if duration_seconds <= 0:
            return
        self.samples.append(WorkerDuration(stage, duration_seconds))
        if len(self.samples) > self.max_samples:
            self.samples = self.samples[-self.max_samples:]

    def snapshot(self) -> FlowSnapshot:
        """Convert observed durations into per-worker service rates."""
        return FlowSnapshot(
            implementation_per_worker_per_second=self._rate("implementation"),
            review_per_worker_per_second=self._rate("review"),
            verification_per_worker_per_second=self._rate("verification"),
        )

    def save(self, path: Path) -> None:
        """Atomically persist the bounded sample history."""
        path.parent.mkdir(parents=True, exist_ok=True)
        encoded = [
            {"stage": sample.stage, "duration_seconds": sample.duration_seconds}
            for sample in self.samples
        ]
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(encoded), encoding="utf-8")
        temporary.replace(path)

    @classmethod
    def load(cls, path: Path) -> WorkforceFlowHistory:
        """Load valid samples from disk, treating missing or corrupt history as empty."""
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return cls()
        if not isinstance(payload, list):
            return cls()
        samples = [
            WorkerDuration(item["stage"], float(item["duration_seconds"]))
            for item in payload
            if isinstance(item, dict)
            and isinstance(item.get("stage"), str)
            and isinstance(item.get("duration_seconds"), (int, float))
            and item["duration_seconds"] > 0
        ]
        return cls(samples=samples[-200:])

    def _rate(self, stage: str) -> float:
        durations = [sample.duration_seconds for sample in self.samples if sample.stage == stage]
        if not durations:
            return 0.0
        return len(durations) / sum(durations)
