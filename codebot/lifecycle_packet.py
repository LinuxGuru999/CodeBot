from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any


class LifecyclePacketStore:
    def __init__(self, state_dir: Path | str) -> None:
        self._directory = Path(state_dir) / "lifecycle_packets"
        self._directory.mkdir(parents=True, exist_ok=True)

    def _path(self, ticket_id: str) -> Path:
        safe_id = ticket_id.replace("/", "_").replace("\\", "_")
        return self._directory / f"{safe_id}.json"

    def load(self, ticket_id: str) -> dict[str, Any] | None:
        try:
            return json.loads(self._path(ticket_id).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def record_implementation_approval(self, ticket_id: str, role: str, revision: float) -> list[str]:
        packet = self.load(ticket_id) or {
            "ticket_id": ticket_id,
            "created_at": time.time(),
            "stages": [],
            "evidence": {},
        }
        approvals = packet.get("implementation_approvals")
        if not isinstance(approvals, list):
            approvals = []
        cleaned = [str(role_name) for role_name in approvals if isinstance(role_name, str) and role_name]
        if role and role not in cleaned:
            cleaned.append(role)
        packet["implementation_approvals"] = cleaned
        packet["implementation_revision"] = revision
        self._save(ticket_id, packet)
        return cleaned

    def implementation_approvals(self, ticket_id: str) -> list[str]:
        packet = self.load(ticket_id)
        if packet is None:
            return []
        approvals = packet.get("implementation_approvals", [])
        return [role for role in approvals if isinstance(role, str) and role]

    def record_transition(
        self, ticket: Any, from_state: str, actor: str = "",
    ) -> None:
        packet = self.load(ticket.id) or {
            "ticket_id": ticket.id,
            "created_at": time.time(),
            "stages": [],
            "evidence": {},
        }
        packet["revision"] = ticket.updated_at
        packet["risk"] = getattr(ticket.risk, "value", ticket.risk)
        packet["affected_modules"] = list(ticket.affected_modules)
        packet["acceptance_criteria"] = list(ticket.acceptance_criteria)
        to_state = getattr(ticket.state, "value", ticket.state)
        if to_state in ("REWORK", "PLANNING"):
            packet["implementation_approvals"] = []
        packet["stages"].append({
            "from": from_state,
            "to": to_state,
            "at": ticket.updated_at,
            "actor": actor,
        })
        if actor:
            participants = packet.setdefault("participants", [])
            if actor not in participants:
                participants.append(actor)
        self._save(ticket.id, packet)

    def record_participants(self, ticket_id: str, actors: list[str]) -> None:
        packet = self.load(ticket_id) or {
            "ticket_id": ticket_id,
            "created_at": time.time(),
            "stages": [],
            "evidence": {},
        }
        participants = packet.setdefault("participants", [])
        for actor in actors:
            if actor and actor not in participants:
                participants.append(actor)
        self._save(ticket_id, packet)

    def participants(self, ticket_id: str) -> list[str]:
        packet = self.load(ticket_id)
        if packet is None:
            return []
        return [actor for actor in packet.get("participants", []) if isinstance(actor, str) and actor]

    def mark_completion_rewards_recorded(self, ticket_id: str) -> bool:
        packet = self.load(ticket_id)
        if packet is None or packet.get("completion_rewards_recorded"):
            return False
        packet["completion_rewards_recorded"] = True
        packet["completion_rewards_recorded_at"] = time.time()
        self._save(ticket_id, packet)
        return True

    def append_evidence(
        self, ticket_id: str, revision: float, stage: str, evidence: dict[str, Any],
    ) -> bool:
        packet = self.load(ticket_id)
        if packet is None or packet.get("revision") != revision:
            return False
        packet["evidence"][stage] = {
            "revision": revision,
            "recorded_at": time.time(),
            "data": evidence,
        }
        self._save(ticket_id, packet)
        return True

    def _save(self, ticket_id: str, packet: dict[str, Any]) -> None:
        path = self._path(ticket_id)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(packet, indent=2, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
