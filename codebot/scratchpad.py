#!/usr/bin/env python3
"""Ticket-scoped scratchpad for cross-agent context handoff.

Purpose
-------
Every agent at every lifecycle stage reads and writes the SAME scratchpad
per ticket. When an implementer hands off to a reviewer, the reviewer sees
exactly what was done. When a rate-limited bot retries, it picks up where
it left off. When REWORK loops back, the implementer sees reviewer feedback.

File layout: state/{ticket_id}.scratchpad.json

Invariants
----------
- stdlib-only (json, os, time, pathlib)
- Atomic writes via tmp + os.replace
- Max 8KB per scratchpad (prevents bloat)
- Read is fail-open (corrupt scratchpad = fresh start)
- Any agent can append; no agent can clobber another's history
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

SCRATCHPAD_VERSION = 2
MAX_SCRATCHPAD_BYTES = 8 * 1024


@dataclass
class AgentRecord:
    agent: str = ""
    stage: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0
    completed_steps: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    summary: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ScratchpadState:
    ticket_id: str = ""
    current_agent: str = ""
    current_stage: str = ""
    phase: str = "init"
    completed_steps: list[str] = field(default_factory=list)
    remaining_steps: list[str] = field(default_factory=list)
    files_changed: list[str] = field(default_factory=list)
    last_tool_call: str = ""
    last_tool_result_summary: str = ""
    error_message: str = ""
    context_summary: str = ""
    iteration: int = 0
    agent_history: list[dict] = field(default_factory=list)
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

    def start_agent(self, agent_name: str, stage: str) -> None:
        self.current_agent = agent_name
        self.current_stage = stage
        self.phase = "running"
        self.iteration = 0
        self.last_tool_call = ""
        self.last_tool_result_summary = ""
        self.error_message = ""
        self.updated_at = time.time()

    def finish_agent(self, summary: str = "") -> None:
        rec = AgentRecord(
            agent=self.current_agent,
            stage=self.current_stage,
            started_at=self.started_at,
            finished_at=time.time(),
            completed_steps=list(self.completed_steps),
            files_changed=list(self.files_changed),
            summary=summary[:500],
            error=self.error_message,
        )
        self.agent_history.append(rec.to_dict())
        self.current_agent = ""
        self.current_stage = ""
        self.phase = "idle"
        self.updated_at = time.time()

    def truncate_for_size(self) -> None:
        serialized = self.to_json()
        while len(serialized.encode("utf-8")) > MAX_SCRATCHPAD_BYTES:
            if self.agent_history and len(self.agent_history) > 3:
                self.agent_history = self.agent_history[-3:]
            elif self.context_summary and len(self.context_summary) > 100:
                self.context_summary = self.context_summary[:len(self.context_summary) // 2]
            elif self.completed_steps and len(self.completed_steps) > 3:
                self.completed_steps = self.completed_steps[-3:]
            elif self.remaining_steps and len(self.remaining_steps) > 3:
                self.remaining_steps = self.remaining_steps[:3]
            else:
                break
            serialized = self.to_json()


def _scratchpad_path(state_dir: Path, ticket_id: str) -> Path:
    return state_dir / f"{ticket_id}.scratchpad.json"


def load_scratchpad(state_dir: Path, ticket_id: str) -> ScratchpadState:
    path = _scratchpad_path(state_dir, ticket_id)
    if not path.exists():
        return ScratchpadState(ticket_id=ticket_id, started_at=time.time(), updated_at=time.time())
    try:
        raw = path.read_text(encoding="utf-8")
        state = ScratchpadState.from_json(raw)
        if state.version != SCRATCHPAD_VERSION:
            state.ticket_id = ticket_id
            state.version = SCRATCHPAD_VERSION
        return state
    except (json.JSONDecodeError, KeyError, OSError, TypeError):
        return ScratchpadState(ticket_id=ticket_id, started_at=time.time(), updated_at=time.time())


def save_scratchpad(state_dir: Path, state: ScratchpadState) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    state.updated_at = time.time()
    state.truncate_for_size()
    path = _scratchpad_path(state_dir, state.ticket_id)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(state.to_json(), encoding="utf-8")
    os.replace(str(tmp), str(path))


def clear_scratchpad(state_dir: Path, ticket_id: str) -> None:
    path = _scratchpad_path(state_dir, ticket_id)
    try:
        path.unlink(missing_ok=True)
    except OSError:
        pass


def create_handoff_note(state: ScratchpadState) -> str:
    lines = [
        "=== TICKET SCRATCHPAD HANDOFF ===",
        f"Ticket: {state.ticket_id}",
    ]
    if state.current_agent:
        lines.append(f"Current agent: {state.current_agent}")
        if state.current_stage:
            lines.append(f"Current stage: {state.current_stage}")
    if state.agent_history:
        lines.append(f"Agent chain: {' → '.join(h.get('agent', '?') for h in state.agent_history[-5:])}")
        last = state.agent_history[-1]
        if last.get("summary"):
            lines.append(f"Last agent ({last.get('agent')}): {last['summary'][:200]}")
        if last.get("files_changed"):
            lines.append(f"Files changed: {', '.join(last['files_changed'][:10])}")
    if state.completed_steps:
        lines.append(f"Completed: {'; '.join(state.completed_steps[-5:])}")
    if state.remaining_steps:
        lines.append(f"Remaining: {'; '.join(state.remaining_steps[:5])}")
    if state.files_changed:
        lines.append(f"This run files: {', '.join(state.files_changed[-10:])}")
    if state.error_message:
        lines.append(f"Last error: {state.error_message}")
    if state.context_summary:
        lines.append(f"Context: {state.context_summary[:500]}")
    lines.append("=================================")
    return "\n".join(lines)
