"""Bucket dispatcher: maps actionable WorkItems to worker requests.

Extracted from dispatcher.py per ADR-007 §16 bounded-context decomposition.
Sole mapper of WorkItem → worker request; only Scheduler.run_once() calls tick().
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .dispatch_gate import DispatchGate, DispatchResult
from .lifecycle import Clock, RealClock

import logging

logger = logging.getLogger(__name__)

from codebot.roles import (
    DECOMPOSER_ROLE_NAMES,
    IMPLEMENTER_ROLE_NAMES,
    PLANNING_ROLE_NAMES,
    REVIEWER_ROLE_NAMES,
)

try:
    from codebot.ticket_engine import TicketState
except ImportError:
    TicketState = None  # type: ignore

try:
    from codebot.ticket_dispatcher import TICKET_CLASS_TO_IMPLEMENTER
except ImportError:
    _FALLBACK_CLASS_TO_IMPLEMENTER: dict[str, str] = {
        "bug": "implementer", "feature": "implementer", "refactor": "implementer",
        "security": "implementer", "performance": "implementer", "architecture": "implementer",
        "test": "implementer", "documentation": "implementer", "dependency": "implementer",
        "infrastructure": "implementer",
    }
    TICKET_CLASS_TO_IMPLEMENTER = _FALLBACK_CLASS_TO_IMPLEMENTER

TICKET_CLASS_TO_ROLES: dict[str, str] = TICKET_CLASS_TO_IMPLEMENTER

BUCKET_ORDER: list[tuple[str, list[str]]] = [
    ("USER", ["REQUESTED"]),
    ("TRIAGE", ["DISCOVERED"]),
    ("GOAL", ["TRIAGED"]),
    ("DECOMP", ["DECOMP"]),
    ("PLAN", ["PLANNING"]),
    ("IMPLEMENT", ["IMPLEMENT"]),
    ("REVIEW", ["REVIEW"]),
    ("REWORK", ["REWORK"]),
    ("VERIFY", ["VERIFY"]),
]

BUCKET_TO_ROLE_SETS: dict[str, frozenset[str]] = {
    "USER": frozenset({"user_agent"}),
    "TRIAGE": frozenset({"ticket_triager"}),
    "GOAL": frozenset({"goal_aligner"}),
    "DECOMP": DECOMPOSER_ROLE_NAMES,
    "PLAN": PLANNING_ROLE_NAMES,
    "IMPLEMENT": IMPLEMENTER_ROLE_NAMES,
    "REVIEW": REVIEWER_ROLE_NAMES,
    "REWORK": IMPLEMENTER_ROLE_NAMES,
    "VERIFY": frozenset({"verifier"}),
}


class NoModelsAvailableError(RuntimeError):
    pass


@dataclass
class BucketSnapshot:
    name: str
    actionable_count: int
    claimed_count: int
    blocked_count: int
    active_count: int


class BucketDispatcher:
    def __init__(
        self,
        gate: DispatchGate,
        model_selector: Any | None = None,
        clock: Clock | None = None,
        bucket_order: list[tuple[str, list[str]]] | None = None,
        bucket_weights: dict[str, int] | None = None,
        role_caps: dict[str, int] | None = None,
        discovery_watermark: int = 1000,
    ) -> None:
        self._gate = gate
        self._model_selector = model_selector
        self._clock = clock or RealClock()
        self._bucket_order = bucket_order or BUCKET_ORDER
        self._bucket_weights = bucket_weights or {}
        self._role_caps = role_caps or {}
        self._discovery_watermark = discovery_watermark
        self._next_bucket_index = 0
        self._lock = threading.Lock()
        self._file_claims: dict[str, str] = {}

    def _has_plan_for(self, ticket: Any) -> bool:
        sdir = getattr(self._gate, "_state_dir", None)
        if sdir is None:
            return True
        try:
            from codebot.implementation_planner import PlanStore
            return PlanStore(sdir).exists(getattr(ticket, "id", ""))
        except Exception:
            return True

    def _filtered_for_plan_gate(self, tickets: list[Any]) -> list[Any]:
        try:
            from codebot.ticket_engine import MIN_RISK_FOR_PLANNING, TicketState as _TS, _RISK_ORDER
            threshold = _RISK_ORDER.get(MIN_RISK_FOR_PLANNING.value, 1)
        except Exception:
            return tickets
        filtered: list[Any] = []
        for t in tickets:
            try:
                st = getattr(t, "state", None)
                sval = st.value if hasattr(st, "value") else str(st) if st else ""
                if sval == _TS.REWORK.value:
                    filtered.append(t)
                    continue
                risk = getattr(t, "risk", None)
                risk_val = risk.value if hasattr(risk, "value") else str(risk) if risk else "medium"
                order = _RISK_ORDER.get(str(risk_val).lower(), 1)
                if order >= threshold and not self._has_plan_for(t):
                    continue
            except Exception:
                pass
            filtered.append(t)
        return filtered

    def _roles_for_ticket(self, ticket: Any) -> list[str]:
        state_val = ""
        try:
            state = getattr(ticket, "state", None)
            state_val = state.value if hasattr(state, "value") else str(state) if state else ""
        except Exception:
            state_val = ""
        if state_val == "REVIEW":
            try:
                from codebot.ticket_dispatcher import reviewer_roles_for_ticket
                roles = list(reviewer_roles_for_ticket(ticket))
                return roles if roles else ["reviewer"]
            except Exception:
                return ["reviewer"]
        return [self._role_for_ticket(ticket)]

    def _ensure_packet(self, ticket: Any, bucket_name: str) -> None:
        pass

    def _snapshot_buckets(self, store: Any) -> dict[str, list[Any]]:
        result: dict[str, list[Any]] = {name: [] for name, _ in self._bucket_order}
        if store is None or TicketState is None:
            return result
        for bucket_name, state_names in self._bucket_order:
            for state_name in state_names:
                try:
                    if not hasattr(TicketState, state_name):
                        continue
                    state_enum = TicketState[state_name]
                    tickets = store.list_by_state(state_enum)
                    for t in tickets:
                        if getattr(t, "state", None) and getattr(t.state, "value", "") == "BLOCKED":
                            continue
                        result[bucket_name].append(t)
                except Exception:
                    continue
        return result

    def _count_active_per_bucket(self, bucket_snapshots: dict[str, list[Any]], active_tickets: set[str]) -> dict[str, int]:
        if not bucket_snapshots:
            return {}
        if not active_tickets:
            return {name: 0 for name, _ in self._bucket_order}
        active = {name: 0 for name, _ in self._bucket_order}
        for bucket_name, tickets in bucket_snapshots.items():
            for tid in (getattr(t, "id", "") for t in tickets):
                if tid in active_tickets:
                    active[bucket_name] += 1
        return active

    def tick(self, store: Any) -> int:
        if store is None:
            return 0
        snapshots = self._snapshot_buckets(store)
        active_ticket_ids: set[str] = set()
        for _, claim, _ in self._gate._active.values():
            active_ticket_ids.add(claim.ticket_id)
        for bname in ("IMPLEMENT", "REWORK"):
            if snapshots.get(bname):
                snapshots[bname] = self._filtered_for_plan_gate(snapshots[bname])
        impl_count = len(snapshots.get("IMPLEMENT", [])) + len(snapshots.get("REWORK", []))
        dynamic_weights = dict(self._bucket_weights)
        if impl_count >= 10:
            dynamic_weights["IMPLEMENT"] = dynamic_weights.get("IMPLEMENT", 1) * 3
            dynamic_weights["REWORK"] = dynamic_weights.get("REWORK", 1) * 3
        weighted_order: list[str] = []
        for bucket_name, _ in self._bucket_order:
            weight = dynamic_weights.get(bucket_name, 1)
            for _ in range(weight):
                weighted_order.append(bucket_name)
        dispatched = 0
        max_iterations = len(snapshots) * 20
        max_slots = self._gate.concurrency.max_slots
        stage_weight = {"REVIEW": 8, "IMPLEMENT": 6, "REWORK": 6, "PLAN": 5, "DECOMP": 4, "GOAL": 2, "TRIAGE": 3, "VERIFY": 4}
        weighted_backlogs = {b: len(snapshots.get(b, [])) * stage_weight.get(b, 1) for b, _ in self._bucket_order}
        total_weighted = sum(weighted_backlogs.values()) or 1
        for _ in range(max_iterations):
            if self._gate.concurrency.free_slots <= 0:
                break
            bucket_name = None
            for offset in range(len(weighted_order)):
                with self._lock:
                    candidate_idx = (self._next_bucket_index + offset) % len(weighted_order)
                    candidate = weighted_order[candidate_idx]
                tickets = snapshots.get(candidate, [])
                if not tickets:
                    continue
                role_set = BUCKET_TO_ROLE_SETS.get(candidate)
                if role_set:
                    active_in_bucket = sum(
                        1 for _, claim, _ in self._gate._active.values()
                        if claim.role in role_set
                    )
                    cap = self._gate.concurrency.max_slots
                    if self._role_caps and candidate.lower() in self._role_caps:
                        cap = min(cap, self._role_caps[candidate.lower()])
                    if active_in_bucket >= cap:
                        continue
                bucket_name = candidate
                with self._lock:
                    self._next_bucket_index = (candidate_idx + 1) % len(weighted_order)
                break
            if bucket_name is None:
                break
            tickets = snapshots.get(bucket_name, [])
            if not tickets:
                continue
            ticket = self._select_ticket(tickets)
            if ticket is None:
                continue
            tid = getattr(ticket, "id", "")
            roles = self._roles_for_ticket(ticket)
            snapshots[bucket_name] = [t for t in snapshots[bucket_name] if getattr(t, "id", "") != tid]
            if bucket_name in ("IMPLEMENT", "REWORK"):
                ticket_files = set(getattr(ticket, "affected_modules", []) or [])
                with self._lock:
                    for f in ticket_files:
                        owner_tid = self._file_claims.get(f)
                        if owner_tid and owner_tid != tid:
                            snapshots[bucket_name] = [t for t in snapshots[bucket_name] if getattr(t, "id", "") != tid]
                            continue
            last_result = None
            self._ensure_packet(ticket, bucket_name)
            dispatched_for_ticket = 0
            for role in roles:
                if self._gate.concurrency.free_slots <= 0:
                    break
                role_prompt = Path(__file__).resolve().parent.parent / "roles" / f"{role}.md"
                if not role_prompt.exists():
                    continue
                model = ""
                if self._model_selector:
                    try:
                        model = self._model_selector.next_model(role)
                    except NoModelsAvailableError:
                        continue
                agent_id = str(uuid.uuid4())
                result = self._gate.try_dispatch(ticket_id=tid, agent_id=agent_id, role=role, model=model)
                if result.success:
                    dispatched += 1
                    dispatched_for_ticket += 1
                    active_ticket_ids.add(tid)
                    if bucket_name in ("IMPLEMENT", "REWORK"):
                        ticket_files = set(getattr(ticket, "affected_modules", []) or [])
                        with self._lock:
                            for f in ticket_files:
                                self._file_claims[f] = tid
                    if bucket_name != "REVIEW":
                        break
                elif result.reason in ("ALREADY_CLAIMED", "NO_CAPACITY"):
                    last_result = result
                    if result.reason == "NO_CAPACITY" or bucket_name != "REVIEW":
                        break
                last_result = result
            if bucket_name == "REVIEW" and dispatched_for_ticket == 0:
                continue
            if dispatched_for_ticket == 0 and last_result is not None and last_result.reason == "NO_CAPACITY":
                break
        return dispatched

    def _select_ticket(self, tickets: list[Any]) -> Any | None:
        if not tickets:
            return None
        def goal_priority_tier(t: Any) -> int:
            tc = getattr(t, "ticket_class", None)
            tc_val = tc.value if hasattr(tc, "value") else str(tc) if tc else ""
            sev = getattr(t, "severity", None)
            sev_val = sev.value if hasattr(sev, "value") else str(sev) if sev else ""
            if tc_val == "security" and sev_val == "critical":
                return -1
            if tc_val == "test":
                return 0
            if tc_val == "documentation":
                return 1
            return 2
        def sort_key(t: Any) -> tuple[int, int, float, str]:
            tier = goal_priority_tier(t)
            prio = getattr(t, "priority", "") or ""
            prio_map = {"high": 0, "medium": 1, "low": 2, "": 3}
            prio_val = prio_map.get(str(prio).lower(), 3)
            age = float(getattr(t, "created_at", 0) or 0)
            tid = str(getattr(t, "id", ""))
            return (tier, prio_val, age, tid)
        return min(tickets, key=sort_key)

    def _role_for_ticket(self, ticket: Any) -> str:
        state_val = ""
        try:
            state = getattr(ticket, "state", None)
            state_val = state.value if hasattr(state, "value") else str(state) if state else ""
        except Exception:
            state_val = ""
        if state_val == "REQUESTED":
            return "user_agent"
        if state_val == "DISCOVERED":
            return "ticket_triager"
        if state_val == "TRIAGED":
            return "goal_aligner"
        if state_val in ("GOAL", "DECOMP"):
            return "decomposer"
        if state_val == "PLANNING":
            return "planner"
        if state_val in ("IMPLEMENT", "REWORK"):
            return "implementer"
        if state_val == "REVIEW":
            return "reviewer"
        if state_val == "VERIFY":
            return "verifier"
        tc = getattr(ticket, "ticket_class", None)
        tc_val = tc.value if hasattr(tc, "value") else str(tc) if tc else "feature"
        return TICKET_CLASS_TO_IMPLEMENTER.get(tc_val, "implementer")

    def _release_file_claims(self, ticket_id: str) -> None:
        with self._lock:
            stale = [f for f, tid in self._file_claims.items() if tid == ticket_id]
            for f in stale:
                del self._file_claims[f]

    def _is_ticket_active(self, ticket_id: str) -> bool:
        for agent_id, (_, claim, _) in self._gate._active.items():
            if claim.ticket_id == ticket_id:
                return True
        return False
