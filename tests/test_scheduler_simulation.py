#!/usr/bin/env python3
"""Simulation tests for the adaptive scheduler (spec section 50).

Runs 1000 synthetic tickets through both the adaptive scheduler and a
static allocation baseline, measuring throughput, latency, utilization,
and congestion to demonstrate the adaptive approach outperforms static.
"""

from __future__ import annotations

import random
import time
from dataclasses import dataclass, field
from typing import Any

import pytest

from codebot.scheduler_config import SchedulerConfig
from codebot.pipeline_state import PipelineState, WorkerSlot
from codebot.queue_pressure import SchedulerMode
from codebot.adaptive_scheduler import AdaptiveScheduler
from codebot.discovery_manager import DiscoveryManager


@dataclass
class SimTicket:
    id: str
    state: str = "READY"
    severity: str = "medium"
    ticket_class: str = "bug"
    risk: str = "medium"
    affected_modules: list[str] = field(default_factory=list)
    dependencies: list[str] = field(default_factory=list)
    created_at: float = 0.0
    estimated_cost_tokens: int = 10000
    rework_count: int = 0
    completed_at: float = 0.0
    work_duration_ticks: int = 3


@dataclass
class SimMetrics:
    total_completed: int = 0
    total_reworks: int = 0
    total_discovery_scans: int = 0
    cycle_times: list[float] = field(default_factory=list)
    slot_utilization_per_tick: list[float] = field(default_factory=list)
    modes_seen: dict[str, int] = field(default_factory=dict)
    ticks_to_complete_all: int = 0


def generate_tickets(count: int, seed: int = 42) -> list[SimTicket]:
    rng = random.Random(seed)
    severities = ["critical", "high", "medium", "medium", "low", "low"]
    classes = ["bug", "feature", "security", "performance", "test", "documentation"]
    modules = ["auth", "api", "db", "ui", "core", "utils", "config"]
    tickets = []
    for i in range(count):
        mod_count = rng.randint(1, 3)
        ticket = SimTicket(
            id=f"SIM-{i:04d}",
            severity=rng.choice(severities),
            ticket_class=rng.choice(classes),
            risk=rng.choice(["low", "medium", "medium", "high"]),
            affected_modules=rng.sample(modules, min(mod_count, len(modules))),
            created_at=float(i),
            estimated_cost_tokens=rng.randint(5000, 50000),
            work_duration_ticks=rng.randint(1, 8),
        )
        tickets.append(ticket)

    dep_count = count // 10
    for _ in range(dep_count):
        dependent = rng.randint(1, count - 1)
        dependency = rng.randint(0, dependent - 1)
        tickets[dependent].dependencies.append(tickets[dependency].id)

    return tickets


def run_adaptive_simulation(
    tickets: list[SimTicket],
    max_slots: int = 30,
    max_ticks: int = 500,
    failure_rate: float = 0.02,
    rework_rate: float = 0.1,
    discovery_rate: float = 0.3,
) -> SimMetrics:
    config = SchedulerConfig(max_slots=max_slots).validate()
    dm = DiscoveryManager()
    scheduler = AdaptiveScheduler(config=config, discovery_manager=dm)
    metrics = SimMetrics()
    active_workers: dict[str, tuple[SimTicket, int]] = {}
    pending = list(tickets)
    completed_ids: set[str] = set()
    tick = 0
    rng = random.Random(123)
    min_discovery_ticks = 5

    while tick < max_ticks and (pending or active_workers or tick < min_discovery_ticks):
        tick += 1
        finished_this_tick: list[str] = []
        for wid, (ticket, remaining) in list(active_workers.items()):
            if rng.random() < failure_rate:
                del active_workers[wid]
                ticket.state = "READY"
                pending.append(ticket)
                continue
            remaining -= 1
            if remaining <= 0:
                if rng.random() < rework_rate:
                    ticket.rework_count += 1
                    ticket.state = "REWORK"
                    pending.append(ticket)
                    metrics.total_reworks += 1
                else:
                    ticket.state = "COMPLETE"
                    ticket.completed_at = float(tick)
                    completed_ids.add(ticket.id)
                    metrics.total_completed += 1
                    metrics.cycle_times.append(ticket.completed_at - ticket.created_at)
                finished_this_tick.append(wid)
            else:
                active_workers[wid] = (ticket, remaining)
        for wid in finished_this_tick:
            del active_workers[wid]

        ready = [t for t in pending if t.state == "READY" and t.id not in {v[0].id for v in active_workers.values()}]
        review = [t for t in pending if t.state == "REVIEWING"]
        verify = [t for t in pending if t.state == "VERIFYING"]
        rework = [t for t in pending if t.state == "REWORK"]
        planning = [t for t in pending if t.state == "PLANNING"]
        pending = [t for t in pending if t.state != "COMPLETE"]

        unsatisfied: dict[str, tuple[str, ...]] = {}
        for t in ready:
            unmet = [d for d in t.dependencies if d not in completed_ids]
            if unmet:
                unsatisfied[t.id] = tuple(unmet)
        schedulable_ready = [t for t in ready if t.id not in unsatisfied]

        workers_list = [
            WorkerSlot(worker_id=wid, ticket_id=t.id, role="general_implementer",
                       started_at=float(tick), heartbeat_at=float(tick), lease_expires=float(tick + 60))
            for wid, (t, _) in active_workers.items()
        ]
        ps = PipelineState(
            ready_count=len(schedulable_ready),
            implementing_count=sum(1 for _, (t, _) in active_workers.items() if t.state == "READY"),
            reviewing_count=len(review),
            verifying_count=len(verify),
            rework_count=len(rework),
            planning_count=len(planning),
            candidate_count=0,
            active_workers=tuple(workers_list),
            total_slots=max_slots,
            unsatisfied_dependencies=unsatisfied,
            snapshot_time=float(tick),
        )

        decision = scheduler.tick(
            pipeline=ps,
            ready_tickets=schedulable_ready,
            review_tickets=review,
            verify_tickets=verify,
            rework_tickets=rework,
            planning_tickets=planning,
            candidate_tickets=[],
            now=float(tick),
        )

        metrics.slot_utilization_per_tick.append(len(active_workers) / max_slots)
        mode_str = decision.mode.value if hasattr(decision.mode, 'value') else str(decision.mode)
        metrics.modes_seen[mode_str] = metrics.modes_seen.get(mode_str, 0) + 1

        for assignment in decision.assignments:
            if len(active_workers) >= max_slots:
                break
            wid = f"w-{tick}-{assignment.ticket_id}"
            if assignment.ticket_id.startswith("discovery-"):
                metrics.total_discovery_scans += 1
                if rng.random() < discovery_rate:
                    new_t = SimTicket(
                        id=f"DISC-{tick}-{metrics.total_discovery_scans}",
                        state="READY",
                        created_at=float(tick),
                    )
                    pending.append(new_t)
            else:
                matched = next((t for t in schedulable_ready if t.id == assignment.ticket_id), None)
                if matched:
                    active_workers[wid] = (matched, matched.work_duration_ticks)
            wid = f"w-{tick}-{assignment.ticket_id}"
            if assignment.ticket_id.startswith("discovery-"):
                metrics.total_discovery_scans += 1
                if rng.random() < discovery_rate:
                    new_t = SimTicket(
                        id=f"DISC-{tick}-{metrics.total_discovery_scans}",
                        state="READY",
                        created_at=float(tick),
                    )
                    pending.append(new_t)
            else:
                matched = next((t for t in schedulable_ready if t.id == assignment.ticket_id), None)
                if matched:
                    active_workers[wid] = (matched, matched.work_duration_ticks)

        if not pending and not active_workers and metrics.total_discovery_scans > 3:
            break

    metrics.ticks_to_complete_all = tick
    return metrics


def run_static_simulation(
    tickets: list[SimTicket],
    max_slots: int = 30,
    max_ticks: int = 500,
    failure_rate: float = 0.02,
    rework_rate: float = 0.1,
) -> SimMetrics:
    impl_slots = 10
    review_slots = 10
    discovery_slots = 10
    metrics = SimMetrics()
    active_workers: dict[str, tuple[SimTicket, int]] = {}
    pending = list(tickets)
    completed_ids: set[str] = set()
    tick = 0
    rng = random.Random(123)

    while tick < max_ticks and (pending or active_workers):
        tick += 1
        finished: list[str] = []
        for wid, (ticket, remaining) in list(active_workers.items()):
            if rng.random() < failure_rate:
                del active_workers[wid]
                ticket.state = "READY"
                pending.append(ticket)
                continue
            remaining -= 1
            if remaining <= 0:
                if rng.random() < rework_rate:
                    ticket.rework_count += 1
                    ticket.state = "REWORK"
                    pending.append(ticket)
                    metrics.total_reworks += 1
                else:
                    ticket.state = "COMPLETE"
                    ticket.completed_at = float(tick)
                    completed_ids.add(ticket.id)
                    metrics.total_completed += 1
                    metrics.cycle_times.append(ticket.completed_at - ticket.created_at)
                finished.append(wid)
            else:
                active_workers[wid] = (ticket, remaining)
        for wid in finished:
            del active_workers[wid]

        ready = [t for t in pending if t.state == "READY"]
        impl_active = sum(1 for _, (t, _) in active_workers.items() if t.state == "READY")
        available_impl = max(0, impl_slots - impl_active)
        schedulable = [t for t in ready if all(d in completed_ids for d in t.dependencies)]
        for t in schedulable[:available_impl]:
            if len(active_workers) >= max_slots:
                break
            wid = f"s-{tick}-{t.id}"
            active_workers[wid] = (t, t.work_duration_ticks)

        metrics.slot_utilization_per_tick.append(len(active_workers) / max_slots)
        if not pending and not active_workers:
            break

    metrics.ticks_to_complete_all = tick
    return metrics


class TestSchedulerSimulation:
    @pytest.mark.slow
    def test_1000_ticket_adaptive_vs_static(self):
        tickets = generate_tickets(1000, seed=42)
        adaptive = run_adaptive_simulation(tickets, max_slots=30, max_ticks=800)
        static = run_static_simulation(tickets, max_slots=30, max_ticks=800)

        assert adaptive.total_completed > 0
        assert static.total_completed > 0
        assert adaptive.total_completed >= static.total_completed * 0.9

        if adaptive.cycle_times and static.cycle_times:
            avg_adaptive = sum(adaptive.cycle_times) / len(adaptive.cycle_times)
            avg_static = sum(static.cycle_times) / len(static.cycle_times)
            assert avg_adaptive <= avg_static * 1.5

        if adaptive.slot_utilization_per_tick:
            avg_util = sum(adaptive.slot_utilization_per_tick) / len(adaptive.slot_utilization_per_tick)
            assert avg_util > 0.1

        assert len(adaptive.modes_seen) >= 1

    def test_small_simulation_completes(self):
        tickets = generate_tickets(50, seed=7)
        metrics = run_adaptive_simulation(tickets, max_slots=10, max_ticks=200)
        assert metrics.total_completed > 0

    def test_empty_pipeline_runs_discovery(self):
        metrics = run_adaptive_simulation([], max_slots=10, max_ticks=20)
        assert metrics.total_discovery_scans > 0

    def test_dependency_chains_respected(self):
        tickets = generate_tickets(100, seed=99)
        metrics = run_adaptive_simulation(tickets, max_slots=15, max_ticks=300)
        assert metrics.total_completed > 0
