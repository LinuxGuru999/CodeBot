#!/usr/bin/env python3
"""Freeze detection for CodeBot agents.

Purpose
-------
Detects frozen/stuck agents using four complementary signals beyond the
existing heartbeat and log-stall checks. Each signal tracks a different
failure mode so agents that appear alive but make no progress are caught
early.

Why
---
The existing is_stuck() relies on heartbeat age and log mtime. These miss:
- Agents that update heartbeats but loop on the same task forever
- Agents that call read/grep in identical repeating patterns
- Agents that produce zero meaningful output over extended periods
- Models whose response time degrades but stays under static timeout

Invariants
----------
- stdlib-only (dataclasses, time, collections, json, logging, pathlib)
- Pure state tracking: observe() records, is_frozen() queries, no I/O side effects
- Thread-safe via single-writer pattern (only orchestrator main loop calls observe)
- Bounded memory: history windows are capped per agent
"""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger("freeze_detector")

MAX_TASK_HISTORY = 10
MAX_RESPONSE_TIMES = 20
ITERATION_STALL_SECONDS = 120
LOOP_REPEAT_THRESHOLD = 3
PROGRESS_ZERO_WINDOW_SECONDS = 300
ADAPTIVE_TIMEOUT_MULTIPLIER = 3.0
MIN_SAMPLES_FOR_ADAPTIVE = 5


@dataclass
class AgentObservation:
    iteration: int = 0
    current_task: str = ""
    updated_at: float = 0.0
    files_touched: tuple[str, ...] = ()
    progress_actions: int = 0


@dataclass
class AgentState:
    observations: deque[AgentObservation] = field(default_factory=lambda: deque(maxlen=MAX_TASK_HISTORY))
    response_times: deque[float] = field(default_factory=lambda: deque(maxlen=MAX_RESPONSE_TIMES))
    last_iteration_change: float = 0.0
    last_iteration: int = -1
    last_progress_time: float = 0.0
    total_progress: int = 0
    first_seen: float = field(default_factory=time.time)


class FreezeDetector:
    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir
        self._agents: dict[str, AgentState] = {}
        self._load()

    def _state_path(self) -> Path | None:
        if self._state_dir is None:
            return None
        return self._state_dir / "freeze_detector_state.json"

    def _load(self) -> None:
        path = self._state_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for name, state_data in data.get("agents", {}).items():
                agent = AgentState()
                agent.last_iteration = state_data.get("last_iteration", -1)
                agent.last_iteration_change = state_data.get("last_iteration_change", 0.0)
                agent.total_progress = state_data.get("total_progress", 0)
                agent.last_progress_time = state_data.get("last_progress_time", 0.0)
                agent.first_seen = state_data.get("first_seen", time.time())
                for rt in state_data.get("response_times", []):
                    agent.response_times.append(rt)
                self._agents[name] = agent
        except Exception:
            pass

    def save(self) -> None:
        path = self._state_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {"agents": {}}
        for name, agent in self._agents.items():
            data["agents"][name] = {
                "last_iteration": agent.last_iteration,
                "last_iteration_change": agent.last_iteration_change,
                "total_progress": agent.total_progress,
                "last_progress_time": agent.last_progress_time,
                "first_seen": agent.first_seen,
                "response_times": list(agent.response_times),
            }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _get_or_create(self, bot_name: str) -> AgentState:
        if bot_name not in self._agents:
            self._agents[bot_name] = AgentState()
        return self._agents[bot_name]

    def observe(
        self,
        bot_name: str,
        iteration: int,
        current_task: str,
        updated_at: float,
        files_touched: list[str] | None = None,
        progress_actions: int = 0,
        now: float | None = None,
    ) -> None:
        now = now or time.time()
        agent = self._get_or_create(bot_name)

        obs = AgentObservation(
            iteration=iteration,
            current_task=current_task,
            updated_at=updated_at or now,
            files_touched=tuple(files_touched or []),
            progress_actions=progress_actions,
        )
        agent.observations.append(obs)

        if iteration != agent.last_iteration:
            if agent.last_iteration >= 0 and agent.last_iteration_change > 0:
                elapsed = now - agent.last_iteration_change
                agent.response_times.append(elapsed)
            agent.last_iteration = iteration
            agent.last_iteration_change = now

        if progress_actions > 0:
            agent.total_progress += progress_actions
            agent.last_progress_time = now

    @dataclass(frozen=True)
    class FreezeReport:
        frozen: bool
        reason: str
        details: dict[str, Any]

    def is_frozen(self, bot_name: str, now: float | None = None) -> FreezeReport:
        now = now or time.time()
        agent = self._agents.get(bot_name)
        if agent is None:
            return self.FreezeReport(False, "", {})

        stall = self._check_iteration_stall(agent, now)
        if stall.frozen:
            return stall

        loop = self._check_loop_detection(agent)
        if loop.frozen:
            return loop

        progress = self._check_progress_rate(agent, now)
        if progress.frozen:
            return progress

        timeout = self._check_adaptive_timeout(agent, now)
        if timeout.frozen:
            return timeout

        return self.FreezeReport(False, "", {})

    def _check_iteration_stall(self, agent: AgentState, now: float) -> FreezeReport:
        if agent.last_iteration_change == 0.0:
            return self.FreezeReport(False, "", {})
        stalled_seconds = now - agent.last_iteration_change
        if stalled_seconds > ITERATION_STALL_SECONDS and agent.last_iteration >= 0:
            return self.FreezeReport(
                True,
                "iteration_stall",
                {"stalled_seconds": stalled_seconds, "iteration": agent.last_iteration},
            )
        return self.FreezeReport(False, "", {})

    def _check_loop_detection(self, agent: AgentState) -> FreezeReport:
        if len(agent.observations) < LOOP_REPEAT_THRESHOLD * 2:
            return self.FreezeReport(False, "", {})

        recent = list(agent.observations)[-LOOP_REPEAT_THRESHOLD * 2:]
        tasks = [obs.current_task for obs in recent]

        for window_size in range(1, len(tasks) // LOOP_REPEAT_THRESHOLD + 1):
            pattern = tasks[-window_size:]
            repeat_count = 0
            for i in range(len(tasks) - window_size, -1, -window_size):
                segment = tasks[i:i + window_size]
                if segment == pattern:
                    repeat_count += 1
                else:
                    break
            if repeat_count >= LOOP_REPEAT_THRESHOLD:
                return self.FreezeReport(
                    True,
                    "task_loop",
                    {"pattern": pattern, "repeats": repeat_count},
                )
        return self.FreezeReport(False, "", {})

    def _check_progress_rate(self, agent: AgentState, now: float) -> FreezeReport:
        if agent.first_seen == 0.0 or now - agent.first_seen < PROGRESS_ZERO_WINDOW_SECONDS:
            return self.FreezeReport(False, "", {})
        if agent.last_progress_time == 0.0:
            zero_duration = now - agent.first_seen
        else:
            zero_duration = now - agent.last_progress_time
        if zero_duration > PROGRESS_ZERO_WINDOW_SECONDS:
            return self.FreezeReport(
                True,
                "zero_progress",
                {"zero_seconds": zero_duration, "total_progress": agent.total_progress},
            )
        return self.FreezeReport(False, "", {})

    def _check_adaptive_timeout(self, agent: AgentState, now: float) -> FreezeReport:
        if len(agent.response_times) < MIN_SAMPLES_FOR_ADAPTIVE:
            return self.FreezeReport(False, "", {})
        avg_response = sum(agent.response_times) / len(agent.response_times)
        threshold = avg_response * ADAPTIVE_TIMEOUT_MULTIPLIER
        if agent.last_iteration_change > 0:
            current_elapsed = now - agent.last_iteration_change
            if current_elapsed > threshold:
                return self.FreezeReport(
                    True,
                    "adaptive_timeout",
                    {
                        "current_elapsed": current_elapsed,
                        "avg_response": avg_response,
                        "threshold": threshold,
                        "samples": len(agent.response_times),
                    },
                )
        return self.FreezeReport(False, "", {})

    def remove_agent(self, bot_name: str) -> None:
        self._agents.pop(bot_name, None)
