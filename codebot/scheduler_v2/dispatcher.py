"""Bucket dispatcher, model selector, and Scheduler class with reconciliation and diagnostics."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .dispatch_gate import DispatchGate, DispatchResult
from .lifecycle import (
    AgentRecord,
    AgentState,
    Clock,
    FakeClock,
    InvalidTransitionError,
    RealClock,
    RealProcessSpawner,
    get_stale_timeout,
    is_agent_stale,
)

import logging

logger = logging.getLogger(__name__)

# Role sets for dispatch routing (centralized in codebot.roles to avoid circular deps)
from codebot.roles import (
    DECOMPOSER_ROLE_NAMES,
    DISCOVERY_ROLE_NAMES,
    IMPLEMENTER_ROLE_NAMES,
    PLANNING_ROLE_NAMES,
    REVIEWER_ROLE_NAMES,
)

try:
    from codebot.ticket_engine import TicketState, Ticket
except ImportError:  # pragma: no cover - ticket_engine always available
    TicketState = None  # type: ignore  # pragma: no cover
    Ticket = None  # type: ignore  # pragma: no cover

try:
    from codebot.ticket_dispatcher import TICKET_CLASS_TO_IMPLEMENTER
except ImportError:  # pragma: no cover
    TICKET_CLASS_TO_IMPLEMENTER: dict[str, str] = {  # pragma: no cover
        "bug": "implementer",
        "feature": "implementer",
        "refactor": "implementer",
        "security": "implementer",
        "performance": "implementer",
        "architecture": "implementer",
        "test": "implementer",
        "documentation": "implementer",
        "dependency": "implementer",
        "infrastructure": "implementer",
    }


# Ticket class to bucket mapping
TICKET_CLASS_TO_ROLES: dict[str, str] = TICKET_CLASS_TO_IMPLEMENTER

# Bucket definitions as ordered list of (bucket_name, ticket_states).
# Bucket name vs state name distinction: The first element in each tuple is a
# label used for role routing and metrics. The second element is the list of
# TicketState enum values queried via store.list_by_state(). So
# ("GOAL", ["TRIAGED"]) means: query tickets in TRIAGED state, label them as
# the GOAL bucket, route to goal_aligner role. DISCOVERED is intentionally
# absent — triage runs as platform code, not through BucketDispatcher.
BUCKET_ORDER: list[tuple[str, list[str]]] = [
    ("USER", ["REQUESTED"]),
    ("GOAL", ["TRIAGED"]),
    ("DECOMP", ["DECOMP"]),
    ("PLANNING", ["PLANNING"]),
    ("REWORK", ["REWORK"]),
    ("IMPLEMENT", ["IMPLEMENT"]),
    ("REVIEW", ["REVIEW"]),
]

BUCKET_TO_ROLE_SETS: dict[str, frozenset[str]] = {
    "USER": frozenset({"user_agent"}),
    "GOAL": frozenset({"goal_aligner"}),
    "DECOMP": DECOMPOSER_ROLE_NAMES,
    "PLANNING": PLANNING_ROLE_NAMES,
    "REWORK": IMPLEMENTER_ROLE_NAMES,
    "IMPLEMENT": IMPLEMENTER_ROLE_NAMES,
    "REVIEW": REVIEWER_ROLE_NAMES,
}


class ReasonCode(str, Enum):
    DISPATCHED = "DISPATCHED"
    NO_GLOBAL_CAPACITY = "NO_GLOBAL_CAPACITY"
    BUCKET_EMPTY = "BUCKET_EMPTY"
    ROLE_CAP = "ROLE_CAP"
    WAITING_FOR_STAGGER = "WAITING_FOR_STAGGER"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    BLOCKED = "BLOCKED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    SPAWN_FAILED = "SPAWN_FAILED"
    STALE_AGENT_RECOVERY = "STALE_AGENT_RECOVERY"
    DISCOVERY_BACKPRESSURE = "DISCOVERY_BACKPRESSURE"
    NO_ACTIONABLE_WORK = "NO_ACTIONABLE_WORK"
    UNKNOWN = "UNKNOWN"


class NoModelsAvailableError(RuntimeError):
    pass


class ModelSelector:
    def __init__(
        self,
        model_pool: list[str],
        state_path: Path | None = None,
        clock: Clock | None = None,
    ) -> None:
        if not model_pool:
            raise ValueError("model_pool must not be empty")
        self.model_pool = list(model_pool)
        self.state_path = Path(state_path) if state_path else None
        self._clock = clock or RealClock()
        self._index = 0
        self._unavailable: set[str] = set()
        self._lock = threading.Lock()
        self._load_index()

    def _load_index(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
            self._index = int(data.get("index", 0)) % max(1, len(self.model_pool))
        except Exception:
            self._index = 0

    def _save_index(self) -> None:  # pragma: no cover - filesystem persistence
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"index": self._index % max(1, len(self.model_pool))}), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:  # pragma: no cover - filesystem unavailable
            pass

    def next_model(self, role: str = "") -> str:
        with self._lock:
            available = [m for m in self.model_pool if m not in self._unavailable]
            if not available:
                raise NoModelsAvailableError("All models are marked unavailable")
            # Find next available model starting from _index
            for offset in range(len(self.model_pool)):
                candidate = self.model_pool[(self._index + offset) % len(self.model_pool)]
                if candidate in available:
                    self._index = (self.model_pool.index(candidate) + 1) % len(self.model_pool)
                    self._save_index()
                    return candidate
            raise NoModelsAvailableError("No available model found")  # pragma: no cover - unreachable: above checks all models unavailable

    def mark_unavailable(self, model: str) -> None:
        with self._lock:
            self._unavailable.add(model)

    def mark_available(self, model: str) -> None:
        with self._lock:
            self._unavailable.discard(model)

    @property
    def current_index(self) -> int:
        with self._lock:
            return self._index


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
        model_selector: ModelSelector | None = None,
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
            from codebot.ticket_engine import MIN_RISK_FOR_PLANNING
            from codebot.ticket_engine import TicketState as _TS
            from codebot.ticket_engine import _RISK_ORDER

            threshold = _RISK_ORDER.get(MIN_RISK_FOR_PLANNING.value, 1)
        except Exception:
            return tickets
        filtered: list[Any] = []
        for t in tickets:
            try:
                st = getattr(t, "state", None)
                try:
                    sval = st.value if hasattr(st, "value") else str(st) if st else ""
                    if sval == _TS.REWORK.value:
                        filtered.append(t)
                        continue
                except Exception:
                    pass
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
        single = self._role_for_ticket(ticket)
        return [single]

    def _ensure_packet(self, ticket: Any, bucket_name: str) -> None:
        pass

    def _snapshot_buckets(self, store: Any) -> dict[str, list[Any]]:
        result: dict[str, list[Any]] = {name: [] for name, _ in self._bucket_order}
        if store is None:
            return result
        if TicketState is None:
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
        tried_buckets: set[tuple[str, str]] = set()
        max_iterations = len(snapshots) * 20

        max_slots = self._gate.concurrency.max_slots

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
                        1 for t in tickets
                        if self._is_ticket_active(getattr(t, "id", ""))
                    )
                    # Pipeline-stage weighting: downstream stages get higher
                    # effective backlog so they aren't starved by large upstream queues.
                    stage_weight = {
                        "REVIEW": 8, "IMPLEMENT": 6, "REWORK": 6,
                        "PLANNING": 5, "DECOMP": 4, "GOAL": 1,
                    }
                    weighted_backlogs = {
                        b: len(snapshots.get(b, [])) * stage_weight.get(b, 1)
                        for b, _ in self._bucket_order
                    }
                    total_weighted = sum(weighted_backlogs.values()) or 1
                    bucket_weighted = weighted_backlogs.get(candidate, 0)
                    proportion = bucket_weighted / total_weighted
                    proportion = min(proportion, 0.5)
                    dynamic_cap = max(1, int(max_slots * proportion))
                    if candidate == "REVIEW":
                        dynamic_cap = max(dynamic_cap, 1)
                    backlog = len(snapshots.get(candidate, []))
                    cap = min(dynamic_cap, backlog) if backlog > 0 else dynamic_cap
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
            tried_buckets.clear()

            if bucket_name in ("IMPLEMENT", "REWORK"):
                filtered = self._filtered_for_plan_gate(tickets)
                if not filtered:
                    if not tickets:
                        continue
                    tried_key2 = (bucket_name, "plan-gate-empty")
                    if tried_key2 in tried_buckets:
                        snapshots[bucket_name] = []
                        continue
                    tried_buckets.add(tried_key2)
                    continue
                if len(filtered) != len(tickets):
                    snapshots[bucket_name] = filtered
                tickets = filtered

            ticket = self._select_ticket(tickets)
            if ticket is None:
                continue

            tid = getattr(ticket, "id", "")
            roles = self._roles_for_ticket(ticket)

            if bucket_name == "REVIEW":
                snapshots[bucket_name] = [t for t in snapshots[bucket_name] if getattr(t, "id", "") != tid]
            else:
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
            import uuid
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
                    except NoModelsAvailableError:  # pragma: no cover - model pool exhausted during tick
                        continue
                agent_id = str(uuid.uuid4())
                result = self._gate.try_dispatch(
                    ticket_id=tid, agent_id=agent_id, role=role, model=model,
                )
                if result.success:
                    dispatched += 1
                    dispatched_for_ticket += 1
                    if bucket_name in ("IMPLEMENT", "REWORK"):
                        ticket_files = set(getattr(ticket, "affected_modules", []) or [])
                        with self._lock:
                            for f in ticket_files:
                                self._file_claims[f] = tid
                    if bucket_name != "REVIEW":
                        break
                elif result.reason in ("ALREADY_CLAIMED", "NO_CAPACITY"):
                    last_result = result
                    if result.reason == "NO_CAPACITY":
                        break
                    if bucket_name != "REVIEW":
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
        def sort_key(t: Any) -> tuple[int, float, str]:
            prio = getattr(t, "priority", "") or ""
            prio_map = {"high": 0, "medium": 1, "low": 2, "": 3}
            prio_val = prio_map.get(str(prio).lower(), 3)
            age = float(getattr(t, "created_at", 0) or 0)
            tid = str(getattr(t, "id", ""))
            return (prio_val, age, tid)
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
        if state_val == "TRIAGED":
            return "goal_aligner"
        if state_val == "GOAL":
            return "decomposer"
        if state_val == "DECOMP":
            return "decomposer"
        if state_val == "PLANNING":
            return "planner"
        if state_val == "REVIEW":
            return "reviewer"
        try:
            from codebot.ticket_dispatcher import next_implementation_role
            required_role = next_implementation_role(ticket)
            if required_role is not None:
                return required_role
        except Exception:
            pass
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


class Scheduler:
    def __init__(
        self,
        max_slots: int = 40,
        state_dir: Path | str | None = None,
        clock: Clock | None = None,
        model_pool: list[str] | None = None,
        bucket_order: list[tuple[str, list[str]]] | None = None,
        bucket_weights: dict[str, int] | None = None,
        role_caps: dict[str, int] | None = None,
        stagger_seconds: float = 1.0,
    ) -> None:
        self._clock = clock or RealClock()
        self._state_dir = Path(state_dir) if state_dir else None
        self._gate = DispatchGate(
            max_slots=max_slots, state_dir=state_dir, clock=self._clock,
            stagger_seconds=stagger_seconds,
        )
        self._model_selector: ModelSelector | None = None
        if model_pool:
            model_state_path = Path(state_dir) / "model_selector.json" if state_dir else None
            self._model_selector = ModelSelector(
                model_pool=model_pool, state_path=model_state_path, clock=self._clock,
            )
        self._dispatcher = BucketDispatcher(
            gate=self._gate, model_selector=self._model_selector, clock=self._clock,
            bucket_order=bucket_order, bucket_weights=bucket_weights, role_caps=role_caps,
        )
        self._agents: dict[str, AgentRecord] = {}
        self._lock = threading.Lock()

    @property
    def gate(self) -> DispatchGate:
        return self._gate

    @property
    def dispatcher(self) -> BucketDispatcher:
        return self._dispatcher

    def tick(self, store: Any = None) -> int:  # pragma: no cover - integration path via BucketDispatcher
        self._reconcile()
        if store is None:
            try:
                from codebot.ticket_dispatcher import get_ticket_store
                store = get_ticket_store()
            except Exception:  # pragma: no cover
                return 0
        try:
            from codebot.request_ingestion import ingest_requests
            from codebot.state_manager import get_paths
            ingest_requests(store, get_paths().state_dir)
        except Exception:  # pragma: no cover
            pass
        try:
            from codebot.finding_ingestion import ingest_findings
            from codebot.state_manager import get_paths
            ingest_findings(store, get_paths().state_dir)
        except Exception:  # pragma: no cover
            pass
        return self._dispatcher.tick(store)

    def _reconcile(self) -> None:  # pragma: no cover - exercised via finalize_agent and explicit tests
        with self._lock:
            for agent_id, record in list(self._agents.items()):
                if record.state == AgentState.RUNNING:
                    if is_agent_stale(record, clock=self._clock):
                        try:
                            new_rec = record.transition(AgentState.ZOMBIE, timestamp=self._clock.now())
                            self._agents[agent_id] = new_rec
                        except InvalidTransitionError:  # pragma: no cover
                            pass
            for agent_id, record in list(self._agents.items()):
                if record.state == AgentState.ZOMBIE:
                    try:
                        new_rec = record.with_exit("zombie-cleanup", timestamp=self._clock.now())
                        self._agents[agent_id] = new_rec
                        self._gate.complete_dispatch(agent_id)
                    except (InvalidTransitionError, Exception):  # pragma: no cover
                        pass

    def register_agent(self, record: AgentRecord) -> None:
        with self._lock:
            self._agents[record.agent_id] = record

    def refresh_heartbeat(self, agent_id: str) -> bool:
        with self._lock:
            record = self._agents.get(agent_id)
            if record is None or record.state == AgentState.DEAD:
                return False
            self._agents[agent_id] = record.with_heartbeat(timestamp=self._clock.now())
            return True

    def get_agent(self, agent_id: str) -> AgentRecord | None:
        with self._lock:
            return self._agents.get(agent_id)

    def list_agents(self) -> list[AgentRecord]:
        with self._lock:
            return list(self._agents.values())

    def why_not_running(self, ticket_id: str, store: Any = None) -> ReasonCode:
        if self._gate.concurrency.free_slots <= 0:
            return ReasonCode.NO_GLOBAL_CAPACITY
        for _, claim, _ in self._gate._active.values():
            if claim.ticket_id == ticket_id:
                return ReasonCode.ALREADY_CLAIMED
        if store is not None:
            try:
                t = store.get(ticket_id) if hasattr(store, "get") else None
                if t is not None:
                    state_val = t.state.value if hasattr(t.state, "value") else str(t.state)
                    if state_val == "BLOCKED":
                        return ReasonCode.BLOCKED
                    if state_val in ("COMPLETE", "REJECTED", "DUPLICATE", "LATER", "NEVER", "NOT_ACTIONABLE", "RESOLVED", "SUPERSEDED", "CANCELLED"):
                        return ReasonCode.NO_ACTIONABLE_WORK
            except Exception:
                pass
        if len(self._gate.spawn_queue) > 0:
            for s in self._gate.spawn_queue._queue:
                if s.request.ticket_id == ticket_id:
                    return ReasonCode.WAITING_FOR_STAGGER
        return ReasonCode.UNKNOWN

    def finalize_agent(self, agent_id: str, outcome: str = "") -> bool:
        with self._lock:
            record = self._agents.get(agent_id)
            if record is None:
                return False
            if record.state == AgentState.DEAD:
                return False
            try:
                if record.state in (AgentState.CREATED, AgentState.STARTING, AgentState.RUNNING, AgentState.ZOMBIE):
                    new_rec = record.with_exit(outcome or "finalized", timestamp=self._clock.now())
                    self._agents[agent_id] = new_rec
                else:
                    return False
            except InvalidTransitionError:  # pragma: no cover
                return False
            tid = record.ticket_id
        self._gate.complete_dispatch(agent_id)
        self._dispatcher._release_file_claims(tid)
        return True

    def request_agent_spawn(
        self,
        role: str,
        ticket_id: str,
        bucket: str = "",
        model: str = "",
        cmd: list[str] | None = None,
    ) -> DispatchResult:
        import uuid
        agent_id = str(uuid.uuid4())
        resolved_model = model
        if not resolved_model and self._model_selector:
            try:
                resolved_model = self._model_selector.next_model(role)
            except NoModelsAvailableError:
                return DispatchResult(success=False, reason="MODEL_UNAVAILABLE", error="No models available")
        result = self._gate.try_dispatch(
            ticket_id=ticket_id, agent_id=agent_id, role=role,
            model=resolved_model, cmd=cmd,
        )
        if result.success:
            record = AgentRecord(
                agent_id=agent_id, ticket_id=ticket_id, role=role,
                model=resolved_model, pid=0, state=AgentState.CREATED,
                created_at=self._clock.now(),
            )
            with self._lock:
                self._agents[agent_id] = record
        return result

    def finalize_absent_agents(self, live_ids: set[str]) -> int:
        """Finalize registered agents whose ids are absent from ``live_ids``.

        Reconciles the in-memory gate with external truth (e.g. the actual
        running bot set). Every registered agent not in ``live_ids`` and not
        already DEAD is finalized, releasing its concurrency token and claim.
        Returns the number of agents finalized.
        """
        live = set(live_ids)
        with self._lock:
            stale_ids = [
                agent_id for agent_id, record in self._agents.items()
                if agent_id not in live and record.state != AgentState.DEAD
            ]
        finalized = 0
        for agent_id in stale_ids:
            if self.finalize_agent(agent_id, outcome="absent-from-live-set"):
                finalized += 1
        return finalized
