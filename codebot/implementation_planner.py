#!/usr/bin/env python3
"""Implementation Planner — Manages implementation plans for tickets.

Purpose
-------
Provides a storage mechanism for implementation plans associated with tickets.
Plans are required for medium+ risk tickets before they can enter the IMPLEMENTING state.

Why
---
To enforce the "Planning Prerequisite" invariant defined in the roadmap.
Agents generate plans during the PLANNING state, which are then validated
before allowing transition to IMPLEMENTING.

Invariants
----------
- Plans are stored as JSON files in .codebot/state/plans/
- Plan filename matches ticket ID: <ticket_id>.plan.json
- Atomic writes via tmp+replace
"""

import json
import time
from pathlib import Path
from typing import Any, Optional


class PlanStore:
    def __init__(self, state_dir: Path) -> None:
        self._plans_dir = state_dir / "plans"
        self._plans_dir.mkdir(parents=True, exist_ok=True)

    def _plan_path(self, ticket_id: str) -> Path:
        return self._plans_dir / f"{ticket_id}.plan.json"

    def save(self, ticket_id: str, plan: dict[str, Any]) -> None:
        """Save an implementation plan for a ticket."""
        path = self._plan_path(ticket_id)
        payload = {
            "ticket_id": ticket_id,
            "created_at": time.time(),
            "updated_at": time.time(),
            "plan": plan,
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(path)

    def load(self, ticket_id: str) -> Optional[dict[str, Any]]:
        """Load an implementation plan for a ticket."""
        path = self._plan_path(ticket_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return data.get("plan")
        except (json.JSONDecodeError, OSError):
            return None

    def exists(self, ticket_id: str) -> bool:
        """Check if a plan exists for a ticket."""
        return self._plan_path(ticket_id).exists()

    def delete(self, ticket_id: str) -> None:
        """Delete an implementation plan for a ticket."""
        path = self._plan_path(ticket_id)
        if path.exists():
            path.unlink()
