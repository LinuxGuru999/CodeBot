"""Agent lifecycle state machine and abstractions for deterministic scheduling."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol
from typing import TypedDict

try:
    from codebot.api_runner import _MODEL_PROFILES as _API_MODEL_PROFILES
except Exception:  # pragma: no cover - defensive fallback for missing api_runner
    _API_MODEL_PROFILES: dict[str, dict[str, Any]] = {}

DEFAULT_STALE_TIMEOUT = 120


class AgentState(str, Enum):
    CREATED = "CREATED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    ZOMBIE = "ZOMBIE"
    DEAD = "DEAD"


_VALID_TRANSITIONS: dict[AgentState, set[AgentState]] = {
    AgentState.CREATED: {AgentState.STARTING, AgentState.ZOMBIE},
    AgentState.STARTING: {AgentState.RUNNING, AgentState.ZOMBIE},
    AgentState.RUNNING: {AgentState.ZOMBIE},
    AgentState.ZOMBIE: {AgentState.DEAD},
    AgentState.DEAD: set(),
}


class InvalidTransitionError(ValueError):
    def __init__(self, from_state: AgentState, to_state: AgentState) -> None:
        valid = _VALID_TRANSITIONS[from_state]
        allowed = ", ".join(s.value for s in valid) if valid else "(none - terminal state)"
        super().__init__(
            f"Invalid transition {from_state.value} -> {to_state.value}. "
            f"Allowed from {from_state.value}: {allowed}"
        )
        self.from_state = from_state
        self.to_state = to_state


class Clock(Protocol):
    def now(self) -> float: ...


class RealClock:
    def now(self) -> float:
        return time.time()


class FakeClock:
    def __init__(self, initial: float = 0.0) -> None:
        self._time = initial

    def now(self) -> float:
        return self._time

    def advance(self, delta: float) -> None:
        if delta < 0:
            raise ValueError("Cannot advance clock by negative amount")
        self._time += delta

    def set(self, value: float) -> None:
        self._time = value


class ProcessHandle(TypedDict):
    pid: int
    cmd: list[str]
    process: Any


class ProcessSpawner(Protocol):
    def spawn(self, cmd: list[str], env: dict[str, str] | None = None) -> ProcessHandle: ...

    def is_alive(self, handle: ProcessHandle) -> bool: ...


class RealProcessSpawner:
    def spawn(self, cmd: list[str], env: dict[str, str] | None = None) -> ProcessHandle:
        import os
        import subprocess

        proc_env = os.environ.copy()
        if env:
            proc_env.update(env)
        process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=proc_env)
        return ProcessHandle(pid=process.pid, cmd=cmd, process=process)

    def is_alive(self, handle: ProcessHandle) -> bool:
        process = handle["process"]
        return process.poll() is None


class SpawnerMode(str, Enum):
    SUCCESS = "success"
    CRASH = "crash"
    TIMEOUT = "timeout"


class FakeProcess:
    def __init__(self, pid: int, alive: bool = True, timeout: bool = False) -> None:
        self.pid = pid
        self.alive = alive
        self.timeout = timeout
        self._returncode: int | None = 0 if alive else 1

    def poll(self) -> int | None:
        if self.timeout:
            return None
        return self._returncode

    def wait(self, timeout: float | None = None) -> int:
        if self.timeout:
            return 0
        return self._returncode if self._returncode is not None else 0

    def terminate(self) -> None:
        self.timeout = False
        self.alive = False
        self._returncode = 1

    def kill(self) -> None:
        self.timeout = False
        self.alive = False
        self._returncode = 9


class FakeProcessSpawner:
    def __init__(self, mode: SpawnerMode = SpawnerMode.SUCCESS, delay: float = 0.0) -> None:
        self.mode = mode
        self.delay = delay
        self.spawned: list[dict[str, Any]] = []
        self._next_pid = 10000

    def spawn(self, cmd: list[str], env: dict[str, str] | None = None) -> ProcessHandle:
        pid = self._next_pid
        self._next_pid += 1
        record: dict[str, Any] = {"pid": pid, "cmd": list(cmd), "env": dict(env) if env else None, "mode": self.mode, "delay": self.delay}
        self.spawned.append(record)
        if self.mode == SpawnerMode.SUCCESS:
            return ProcessHandle(pid=pid, cmd=cmd, process=FakeProcess(pid, True))
        elif self.mode == SpawnerMode.CRASH:
            return ProcessHandle(pid=pid, cmd=cmd, process=FakeProcess(pid, False))
        elif self.mode == SpawnerMode.TIMEOUT:
            return ProcessHandle(pid=pid, cmd=cmd, process=FakeProcess(pid, True, timeout=True))
        else:
            raise ValueError(f"Unknown SpawnerMode: {self.mode}")

    def is_alive(self, handle: ProcessHandle) -> bool:
        proc = handle.get("process")
        if isinstance(proc, FakeProcess):
            return proc.alive and proc.poll() is None
        if isinstance(proc, dict):
            return bool(proc.get("alive", True))
        return True


@dataclass
class AgentRecord:
    agent_id: str
    ticket_id: str
    role: str
    model: str
    pid: int
    state: AgentState
    created_at: float
    scheduled_at: float = 0.0
    started_at: float = 0.0
    last_heartbeat: float = 0.0
    exit_at: float = 0.0
    exit_reason: str = ""
    trace: list[tuple[AgentState, float]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.trace:
            self.trace.append((self.state, self.created_at))

    def transition(self, new_state: AgentState, timestamp: float | None = None) -> "AgentRecord":
        if new_state not in _VALID_TRANSITIONS[self.state]:
            raise InvalidTransitionError(self.state, new_state)
        ts = timestamp if timestamp is not None else time.time()
        new_trace = list(self.trace) + [(new_state, ts)]
        return AgentRecord(
            agent_id=self.agent_id,
            ticket_id=self.ticket_id,
            role=self.role,
            model=self.model,
            pid=self.pid,
            state=new_state,
            created_at=self.created_at,
            scheduled_at=self.scheduled_at,
            started_at=ts if new_state == AgentState.STARTING else self.started_at,
            last_heartbeat=self.last_heartbeat,
            exit_at=ts if new_state == AgentState.DEAD else self.exit_at,
            exit_reason=self.exit_reason,
            trace=new_trace,
        )

    def with_heartbeat(self, timestamp: float | None = None) -> "AgentRecord":
        ts = timestamp if timestamp is not None else time.time()
        return AgentRecord(
            agent_id=self.agent_id,
            ticket_id=self.ticket_id,
            role=self.role,
            model=self.model,
            pid=self.pid,
            state=self.state,
            created_at=self.created_at,
            scheduled_at=self.scheduled_at,
            started_at=self.started_at,
            last_heartbeat=ts,
            exit_at=self.exit_at,
            exit_reason=self.exit_reason,
            trace=list(self.trace),
        )

    def with_exit(self, reason: str, timestamp: float | None = None) -> "AgentRecord":
        ts = timestamp if timestamp is not None else time.time()
        if self.state == AgentState.DEAD:
            raise InvalidTransitionError(self.state, AgentState.DEAD)
        # Valid exit path is always through ZOMBIE -> DEAD unless already ZOMBIE
        if self.state == AgentState.ZOMBIE:
            new_trace = list(self.trace) + [(AgentState.DEAD, ts)]
            return AgentRecord(
                agent_id=self.agent_id,
                ticket_id=self.ticket_id,
                role=self.role,
                model=self.model,
                pid=self.pid,
                state=AgentState.DEAD,
                created_at=self.created_at,
                scheduled_at=self.scheduled_at,
                started_at=self.started_at,
                last_heartbeat=self.last_heartbeat,
                exit_at=ts,
                exit_reason=reason,
                trace=new_trace,
            )
        if self.state not in (AgentState.CREATED, AgentState.STARTING, AgentState.RUNNING):  # pragma: no cover - guarded by earlier ZOMBIE check
            raise InvalidTransitionError(self.state, AgentState.DEAD)
        new_trace = list(self.trace) + [(AgentState.ZOMBIE, ts), (AgentState.DEAD, ts)]
        return AgentRecord(
            agent_id=self.agent_id,
            ticket_id=self.ticket_id,
            role=self.role,
            model=self.model,
            pid=self.pid,
            state=AgentState.DEAD,
            created_at=self.created_at,
            scheduled_at=self.scheduled_at,
            started_at=self.started_at,
            last_heartbeat=self.last_heartbeat,
            exit_at=ts,
            exit_reason=reason,
            trace=new_trace,
        )


def create_agent_record(
    ticket_id: str,
    role: str,
    model: str,
    clock: Clock | None = None,
) -> AgentRecord:
    clk = clock or RealClock()
    now = clk.now()
    return AgentRecord(
        agent_id=str(uuid.uuid4()),
        ticket_id=ticket_id,
        role=role,
        model=model,
        pid=0,
        state=AgentState.CREATED,
        created_at=now,
        scheduled_at=now,
    )


MODEL_TIMEOUTS: dict[str, dict[str, Any]] = {
    k: {"heartbeat_multiplier": v.get("heartbeat_multiplier", 1.0), "log_stall_seconds": v.get("log_stall_seconds", 120), "restart_cooldown": v.get("restart_cooldown", 3)}
    for k, v in _API_MODEL_PROFILES.items()
}


def get_stale_timeout(model: str) -> int:
    profile = _API_MODEL_PROFILES.get(model)
    if not profile:
        return DEFAULT_STALE_TIMEOUT
    multiplier = profile.get("heartbeat_multiplier", 1.0)
    base = 120
    return int(base * multiplier)


def is_agent_stale(record: AgentRecord, clock: Clock | None = None) -> bool:
    clk = clock or RealClock()
    if record.state not in (AgentState.STARTING, AgentState.RUNNING):
        return False
    timeout = get_stale_timeout(record.model)
    last_hb = record.last_heartbeat
    if last_hb <= 0:
        return False
    return (clk.now() - last_hb) > timeout


def agent_state_from_string(value: str) -> AgentState | None:
    try:
        return AgentState(value)
    except ValueError:
        return None


_HEARTBEAT_INTERVAL = 30.0
_HEARTBEAT_STALE_MULTIPLIER = 2.0


def heartbeat_interval_for(role: str | None = None) -> float:
    return _HEARTBEAT_INTERVAL


def stale_timeout_for(model: str | None = None) -> float:
    if model is None or not model:
        return DEFAULT_STALE_TIMEOUT
    return float(get_stale_timeout(model))
