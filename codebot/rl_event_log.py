#!/usr/bin/env python3
"""RL Event Log — append-only factual event ingestion.

Purpose
-------
Append-only JSONL writer for RL factual events. Schema v2 includes
causal identifiers (attempt_id, transition_id, policy_*, schema versions).
Atomic O_APPEND writes, never raises to caller, dead-letters unknown types.

Why
---
Facts vs interpretations are separated. Events record what happened; credit
assignment derives what it means. This file owns ingestion.

Invariants
----------
- stdlib-only.
- All writes use os.open(O_APPEND) for atomic concurrent writers.
- Never raises to caller; logs warning on error.
- Unknown event types go to rl_dead_letters.jsonl.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 2

ALLOWED_EVENT_TYPES = frozenset(
    {
        "DISCOVERY_CREATED",
        "GOAL_DECISION",
        "GOAL_REASSESSMENT",
        "TRIAGE_DECISION",
        "DECOMP_STARTED",
        "DECOMP_COMPLETED",
        "PLAN_STARTED",
        "PLAN_COMPLETED",
        "IMPLEMENT_STARTED",
        "IMPLEMENT_COMPLETED",
        "IMPLEMENT_FAILED",
        "REVIEW_STARTED",
        "REVIEW_APPROVED",
        "REVIEW_REWORK",
        "REVIEW_REJECTED",
        "TICKET_COMPLETE",
        "TICKET_DEFERRED",
        "TICKET_RESOLVED",
        "TICKET_SUPERSEDED",
        "TICKET_CANCELLED",
        "TICKET_DUPLICATE",
        "TICKET_NOT_ACTIONABLE",
        "REGRESSION_DISCOVERED",
        "TICKET_REOPENED",
        "USER_ACCEPTED",
        "USER_REJECTED",
        "RUNTIME_FAILURE",
        "MODEL_FAILURE",
        "MODEL_SELECTED",
        "COST_RECORDED",
        "TICKET_TERMINAL",
        "DISCOVERY_DOWNSTREAM",
    }
)

_adapter_instance: object | None = None
_DEFAULT_STATE_DIR = Path(__file__).parent.parent / ".codebot" / "state"


def set_project_adapter(adapter: object | None) -> None:
    global _adapter_instance
    _adapter_instance = adapter


def get_adapter() -> object | None:
    return _adapter_instance


def _resolve_state_dir(state_dir: Path | str | None) -> Path:
    if state_dir is not None:
        return Path(state_dir)
    if _adapter_instance is not None:
        try:
            p = _adapter_instance.paths()  # type: ignore[union-attr]
            return Path(p.state_dir)
        except Exception:
            pass
    return _DEFAULT_STATE_DIR


def append_event(
    event_type: str,
    *,
    state_dir: Path | str | None = None,
    project_id: str = "",
    ticket_id: str = "",
    parent_ticket_id: str = "",
    stage: str = "",
    actor_type: str = "",
    actor_role: str = "",
    agent_id: str = "",
    model: str = "",
    model_provider: str = "",
    repository_revision: str = "",
    goal_revision: int = 0,
    discovery_generation: int = 0,
    input_features: dict[str, Any] | None = None,
    decision: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
    context: dict[str, Any] | None = None,
    attempt_id: str = "",
    transition_id: str = "",
    policy_name: str = "",
    policy_version: str = "",
    feature_schema_version: int = 0,
    reward_schema_version: int = 0,
    event_id: str = "",
    timestamp: float | None = None,
) -> None:
    try:
        if event_type not in ALLOWED_EVENT_TYPES:
            logger.warning("rl_event_log: unknown event_type %r", event_type)
            try:
                resolved_dl = _resolve_state_dir(state_dir)
                dead_letter_path = resolved_dl / "rl_dead_letters.jsonl"
                dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
                dl_record = {
                    "event_type": event_type,
                    "timestamp": time.time(),
                    "ticket_id": ticket_id,
                }
                fd_dl = os.open(str(dead_letter_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
                try:
                    os.write(fd_dl, (json.dumps(dl_record) + "\n").encode("utf-8"))
                finally:
                    os.close(fd_dl)
            except Exception:
                pass
            return

        resolved = _resolve_state_dir(state_dir)
        resolved.mkdir(parents=True, exist_ok=True)
        event_path = resolved / "rl_events.jsonl"

        eid = event_id if event_id else str(uuid.uuid4())
        ts = float(timestamp) if timestamp is not None else time.time()

        record: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "event_id": eid,
            "timestamp": ts,
            "project_id": project_id,
            "ticket_id": ticket_id,
            "parent_ticket_id": parent_ticket_id,
            "event_type": event_type,
            "stage": stage,
            "actor_type": actor_type,
            "actor_role": actor_role,
            "agent_id": agent_id,
            "model": model,
            "model_provider": model_provider,
            "repository_revision": repository_revision,
            "goal_revision": goal_revision,
            "discovery_generation": discovery_generation,
            "input_features": input_features if input_features is not None else {},
            "decision": decision if decision is not None else {},
            "execution": execution if execution is not None else {},
            "context": context if context is not None else {},
            "attempt_id": attempt_id,
            "transition_id": transition_id,
            "policy_name": policy_name,
            "policy_version": policy_version,
            "feature_schema_version": feature_schema_version,
            "reward_schema_version": reward_schema_version,
        }

        line = json.dumps(record, sort_keys=False) + "\n"
        fd = os.open(str(event_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
        try:
            os.write(fd, line.encode("utf-8"))
        finally:
            os.close(fd)
    except Exception as e:
        logger.warning("rl_event_log.append_event failed: %s: %s", type(e).__name__, e)


def read_events(
    state_dir: Path | str | None = None,
    *,
    event_type: str | None = None,
    ticket_id: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    try:
        resolved = _resolve_state_dir(state_dir)
        event_path = resolved / "rl_events.jsonl"
        if not event_path.exists():
            return []
        events: list[dict[str, Any]] = []
        try:
            text = event_path.read_text(encoding="utf-8")
        except OSError:
            return []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(rec, dict):
                continue
            if rec.get("schema_version") != SCHEMA_VERSION:
                continue
            if event_type is not None and rec.get("event_type") != event_type:
                continue
            if ticket_id is not None and rec.get("ticket_id") != ticket_id:
                continue
            events.append(rec)
        if limit is not None:
            try:
                lim = int(limit)
                if lim >= 0:
                    events = events[:lim]
            except (TypeError, ValueError):
                pass
        return events
    except Exception as e:
        logger.warning("rl_event_log.read_events failed: %s: %s", type(e).__name__, e)
        return []
