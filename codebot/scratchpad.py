#!/usr/bin/env python3
"""Structured scratchpad for agent session persistence and handoff.

Purpose
-------
Provides a JSON-based scratchpad that agents use to persist intermediate
state between tool calls, survive timeouts, and hand off work to other
workers when they fail or hit rate limits.

Why
---
The existing `_write_scratchpad` in api_runner.py writes freeform markdown
lines. For reliable handoff between workers, we need structured JSON with
schema validation, atomic writes, and explicit fields for resume state.

Invariants
----------
- stdlib-only (json, os, time, pathlib)
- Atomic writes via tmp + os.replace
- Max 8KB per scratchpad (prevents bloat)
- Schema versioned for forward compatibility
- Read is fail-open (corrupt scratchpad = fresh start)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

SCRATCHPAD_VERSION = 1
MAX_SCRATCHPAD_BYTES = 8 * 1024


@dataclass
class ScratchpadState:
    ticket_id: str = ""
    agent_name: str = ""
    phase: str = "init"
    completed_steps: list[str] = field(default_factory=list)
    remaining_steps: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    last_tool_call: str = ""
    last_tool_result_summary: str = ""
    error_message: str = ""
    context_summary: str = ""
    iteration: int = 0
    started_at: float = 0.0
    updated_at: float = 0.0
    version: int = SCRATCHPAD_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ScratchpadState:
        valid_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in valid_fields}
        return cls(**filtered)

    @classmethod
    def from_json(cls, raw: str) -> ScratchpadState:
        return cls.from_dict(json.loads(raw))

    def mark_step_complete(self, step: str) -> None:
        if step not in self.completed_steps:
            self.completed_steps.append(step)
        if step in self.remaining_steps:
            self.remaining_steps.remove(step)
        self.updated_at = time.time()

    def mark_error(self, message: str) -> None:
        self.error_message = message[:500]
        self.phase = "error"
        self.updated_at = time.time()

    def set_phase(self, phase: str) -> None:
        self.phase = phase
        self.updated_at = time.time()

    def truncate_for_size(self) -> None:
        serialized = self.to_json()
        while len(serialized.encode("utf-8")) > MAX_SCRATCHPAD_BYTES:
            if self.context_summary and len(self.context_summary) > 100:
                self.context_summary = self.context_summary[:len(self.context_summary) // 2]
            elif self.completed_steps and len(self.completed_steps) > 3:
                self.completed_steps = self.completed_steps[-3:]
            elif self.remaining_steps and len(self.remaining_steps) > 3:
                self.remaining_steps = self.remaining_steps[:3]
            else:
                break
            serialized = self.to_json()


def load_scratchpad(state_dir: Path, agent_name: str) -> ScratchpadState:
    path = state_dir / f"{agent_name}.scratchpad.json"
    if not path.exists():
        return ScratchpadState(agent_name=agent_name, started_at=time.time(), updated_at=time.time())
    try:
        raw = path.read_text(encoding="utf-8")
        state = ScratchpadState.from_json(raw)
        if state.version != SCRATCHPAD_VERSION:
            return ScratchpadState(agent_name=agent_name, started_at=time.time(), updated_at=time.time())
        return state
    except (json.JSONDecodeError, KeyError, OSError, TypeError):
        return ScratchpadState(agent_name=agent_name, started_at=time.time(), updated_at=time.time())


def save_scratchpad(state_dir: Path, state: ScratchpadState) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.time()
    state.truncate_for_size()
    path = state_dir / f"{state.agent_name}.scratchpad.json"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.to_json(), encoding="utf-8")
    os.replace(str(tmp), str(path))


def clear_scratchpad(state_dir: Path, agent_name: str) -> None:
    path = state_dir / f"{agent_name}.scratchpad.json"
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def create_handoff_note(state: ScratchpadState) -> str:
    lines = [
        f"=== HANDOFF NOTE ===",
        f"Ticket: {state.ticket_id}",
        f"Agent: {state.agent_name}",
        f"Phase: {state.phase}",
        f"Iteration: {state.iteration}",
    ]
    if state.completed_steps:
        lines.append(f"Completed: {'; '.join(state.completed_steps[-5:])}")
    if state.remaining_steps:
        lines.append(f"Remaining: {'; '.join(state.remaining_steps[:5])}")
    if state.files_changed:
        lines.append(f"Files changed: {', '.join(state.files_changed)}")
    if state.error_message:
        lines.append(f"Last error: {state.error_message}")
    if state.context_summary:
        lines.append(f"Context: {state.context_summary[:500]}")
    lines.append("====================")
    return "\n".join(lines)
