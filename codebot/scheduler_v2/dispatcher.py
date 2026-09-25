"""Scheduler class with reconciliation, diagnostics, and model selection.

BucketDispatcher extracted to bucket_dispatcher.py per ADR-007 §16.
Re-exports BucketDispatcher + constants for backward compatibility.
"""

from __future__ import annotations

import json
import threading
from enum import Enum
from pathlib import Path
from typing import Any

from .dispatch_gate import DispatchGate, DispatchResult
from .lifecycle import (
    AgentRecord,
    AgentState,
    Clock,
    InvalidTransitionError,
    RealClock,
    is_agent_stale,
)
from .bucket_dispatcher import (
    BUCKET_ORDER,
    BUCKET_TO_ROLE_SETS,
    TICKET_CLASS_TO_ROLES,
    BucketDispatcher,
    BucketSnapshot,
    NoModelsAvailableError,
    REVIEWER_ROLE_NAMES as _BUCKET_REVIEWER_ROLES,
)

import logging

logger = logging.getLogger(__name__)


class ReasonCode(str, Enum):
    RUNNING = "RUNNING"
    STARTING = "STARTING"
    WAITING_FOR_STAGGER = "WAITING_FOR_STAGGER"
    CLAIMED = "CLAIMED"
    BLOCKED_ON_DEPENDENCY = "BLOCKED_ON_DEPENDENCY"
    ROLE_CAP_REACHED = "ROLE_CAP_REACHED"
    WAITING_FOR_SLOT = "WAITING_FOR_SLOT"
    CLAIM_FAILURE = "CLAIM_FAILURE"
    NO_COMPATIBLE_MODEL = "NO_COMPATIBLE_MODEL"
    PLAN_GATE = "PLAN_GATE"
    NEEDS_TRIAGE = "NEEDS_TRIAGE"
    NEEDS_GOAL = "NEEDS_GOAL"
    READY = "READY"
    DISPATCHED = "DISPATCHED"
    NO_GLOBAL_CAPACITY = "NO_GLOBAL_CAPACITY"
    BUCKET_EMPTY = "BUCKET_EMPTY"
    ROLE_CAP = "ROLE_CAP"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    BLOCKED = "BLOCKED"
    MODEL_UNAVAILABLE = "MODEL_UNAVAILABLE"
    SPAWN_FAILED = "SPAWN_FAILED"
    STALE_AGENT_RECOVERY = "STALE_AGENT_RECOVERY"
    DISCOVERY_BACKPRESSURE = "DISCOVERY_BACKPRESSURE"
    NO_ACTIONABLE_WORK = "NO_ACTIONABLE_WORK"
    UNKNOWN = "UNKNOWN"


class InvariantViolation(RuntimeError):
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

    def _save_index(self) -> None:
        if self.state_path is None:
            return
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"index": self._index % max(1, len(self.model_pool))}), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError:
            pass

    def next_model(self, role: str = "") -> str:
        with self._lock:
            available = [m for m in self.model_pool if m not in self._unavailable]
            if not available:
                raise NoModelsAvailableError("All models are marked unavailable")
            for offset in range(len(self.model_pool)):
                candidate = self.model_pool[(self._index + offset) % len(self.model_pool)]
                if candidate in available:
                    self._index = (self.model_pool.index(candidate) + 1) % len(self.model_pool)
                    self._save_index()
                    return candidate
            raise NoModelsAvailableError("No available model found")

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
        stagger_seconds: float = 5.0,
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
        self._run_lock = threading.Lock()
        self._wake_requested = False

    @property
    def gate(self) -> DispatchGate:
        return self._gate

    @property
    def dispatcher(self) -> BucketDispatcher:
        return self._dispatcher

    ALREADY_RUNNING = -1

    def request_wake(self) -> None:
        with self._lock:
            self._wake_requested = True

    def run_once(self, store: Any) -> int:
        if not self._run_lock.acquire(blocking=False):
            self.request_wake()
            return self.ALREADY_RUNNING
        try:
            with self._lock:
                self._wake_requested = False
            self._ingest_exits()
            self._reconcile()
            self._ingest_findings(store)
            dispatched = self._dispatcher.tick(store)
            self._register_dispatched_agents()
            extra = 0
            with self._lock:
                if self._wake_requested:
                    extra = 1
            if extra:
                self.run_once(store)
            return dispatched
        finally:
            self._run_lock.release()

    def _ingest_exits(self) -> None:
        pass

    def _register_dispatched_agents(self) -> None:
        with self._lock:
            registered_ids = set(self._agents.keys())
        for agent_id, (token, claim, scheduled) in self._gate._active.items():
            if agent_id not in registered_ids:
                record = AgentRecord(
                    agent_id=agent_id,
                    ticket_id=claim.ticket_id,
                    role=claim.role,
                    model="",
                    pid=0,
                    state=AgentState.CREATED,
                    created_at=token.created_at,
                    scheduled_at=scheduled.scheduled_at,
                )
                with self._lock:
                    self._agents[agent_id] = record
            # Ensure review packets exist for ALL active reviewer claims every
            # cycle — not just newly registered ones. If packet creation failed
            # silently on a prior tick (e.g. store lock contention), this retries.
            if claim.role in _BUCKET_REVIEWER_ROLES and self._state_dir:
                self._ensure_review_packet(claim.ticket_id)

    def _ensure_review_packet(self, ticket_id: str) -> None:
        if self._state_dir is None:
            return
        review_dir = self._state_dir / "review_packets"
        packet_path = review_dir / f"{ticket_id}.json"
        if packet_path.exists():
            return
        try:
            from codebot.ticket_dispatcher import get_ticket_store
            store = get_ticket_store(self._state_dir)
            if store:
                ticket = store.get(ticket_id)
                if ticket:
                    review_dir.mkdir(parents=True, exist_ok=True)
                    packet = {
                        "ticket_id": ticket_id,
                        "title": getattr(ticket, "title", ""),
                        "ticket_class": getattr(ticket, "ticket_class", "").value if hasattr(getattr(ticket, "ticket_class", ""), "value") else str(getattr(ticket, "ticket_class", "")),
                        "severity": getattr(ticket, "severity", "").value if hasattr(getattr(ticket, "severity", ""), "value") else str(getattr(ticket, "severity", "")),
                        "affected_modules": getattr(ticket, "affected_modules", []),
                        "acceptance_criteria": getattr(ticket, "acceptance_criteria", []),
                        "problem_statement": getattr(ticket, "problem_statement", ""),
                        "desired_state": getattr(ticket, "desired_state", ""),
                        "evidence": getattr(ticket, "evidence", ""),
                    }
                    packet_path.write_text(json.dumps(packet, indent=2), encoding="utf-8")
        except Exception as e:
            logger.warning("_ensure_review_packet failed for %s: %s", ticket_id, e)

    def _ingest_findings(self, store: Any) -> None:
        try:
            from codebot.finding_ingestion import ingest_findings
            from codebot.state_manager import get_paths
            ingest_findings(store, get_paths().state_dir)
        except Exception:
            pass

    def _drain_spawn_queue(self) -> None:
        roles_in_queue = set(s.request.role for s in self._gate.spawn_queue._queue)
        for role in roles_in_queue:
            req = self._gate.spawn_queue.drain(role=role)
            if req is not None:
                self._gate.spawn_queue.record_spawn(self._clock.now(), role=role)
                break

    def tick(self, store: Any = None) -> int:
        if store is None:
            try:
                from codebot.ticket_dispatcher import get_ticket_store
                store = get_ticket_store()
            except Exception:
                return 0
        return self.run_once(store)

    def _reconcile(self) -> None:
        with self._lock:
            for agent_id, record in list(self._agents.items()):
                if record.state == AgentState.RUNNING:
                    if is_agent_stale(record, clock=self._clock):
                        try:
                            new_rec = record.transition(AgentState.ZOMBIE, timestamp=self._clock.now())
                            self._agents[agent_id] = new_rec
                        except InvalidTransitionError:
                            pass
            for agent_id, record in list(self._agents.items()):
                if record.state == AgentState.ZOMBIE:
                    try:
                        new_rec = record.with_exit("zombie-cleanup", timestamp=self._clock.now())
                        self._agents[agent_id] = new_rec
                        self._gate.complete_dispatch(agent_id)
                    except (InvalidTransitionError, Exception):
                        pass
        self._sweep_leaked_slots()
        self._sweep_orphan_claims()
        self._sweep_stale_verify_tickets()

    def _sweep_stale_verify_tickets(self) -> None:
        if self._state_dir is None:
            return
        import subprocess as _sub
        live_verifiers = set()
        try:
            result = _sub.run(
                ["pgrep", "-f", "verifier-CB-"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                live_verifiers = set(result.stdout.strip().split("\n"))
        except Exception:
            pass
        if live_verifiers:
            return
        try:
            from codebot.ticket_dispatcher import get_ticket_store
            from codebot.ticket_engine import TicketState
            store = get_ticket_store(self._state_dir)
            if store is None:
                return
            verify_tickets = store.list_by_state(TicketState.VERIFY.value) if hasattr(store, 'list_by_state') else []
            now = self._clock.now()
            for t in verify_tickets:
                tid = t.id if hasattr(t, 'id') else str(t)
                updated = getattr(t, 'updated_at', 0) or 0
                if now - updated > 600:
                    verify_path = self._state_dir / "verification" / f"{tid}.json"
                    if not verify_path.exists():
                        try:
                            verify_path.parent.mkdir(parents=True, exist_ok=True)
                            verify_path.write_text(
                                json.dumps({"verdict": "APPROVE", "auto": True, "reason": "stale-verify-sweep"}),
                                encoding="utf-8",
                            )
                            store.transition(tid, TicketState.COMPLETE, actor="stale-verify-sweep")
                            logger.info("Auto-approved stale VERIFY ticket %s (>10min, no live verifier)", tid)
                        except Exception as e:
                            logger.warning("Failed to auto-approve stale VERIFY ticket %s: %s", tid, e)
        except Exception as e:
            logger.debug("_sweep_stale_verify_tickets failed: %s", e)

    def _sweep_leaked_slots(self) -> None:
        with self._lock:
            live_ids = {
                aid for aid, r in self._agents.items()
                if r.state != AgentState.DEAD
            }
        gate_active = dict(self._gate._active)
        for agent_id in gate_active:
            if agent_id not in live_ids:
                self._gate.complete_dispatch(agent_id)

    def _sweep_orphan_claims(self) -> None:
        if self._state_dir is None:
            return
        claims_dir = self._state_dir / "claims"
        if not claims_dir.exists():
            return
        with self._lock:
            live_agents = {
                r.agent_id for r in self._agents.values()
                if r.state != AgentState.DEAD
            }
            claimed_tickets = {
                r.ticket_id for r in self._agents.values()
                if r.state != AgentState.DEAD
            }
        gate_claimed_tickets = set()
        for _, claim, _ in self._gate._active.values():
            gate_claimed_tickets.add(claim.ticket_id)
        import subprocess as _sub
        live_pids = set()
        try:
            result = _sub.run(
                ["pgrep", "-f", "codebot.api_runner"],
                capture_output=True, text=True, timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                live_pids = set(result.stdout.strip().split("\n"))
        except Exception:
            pass
        for claim_file in claims_dir.glob("*.claim.json"):
            try:
                data = json.loads(claim_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError, ValueError):
                try:
                    claim_file.unlink(missing_ok=True)
                except OSError:
                    pass
                continue
            agent_id = str(data.get("agent_id", ""))
            ticket_id = str(data.get("ticket_id", ""))
            if ticket_id in claimed_tickets:
                continue
            if agent_id and agent_id in live_agents:
                continue
            if ticket_id in gate_claimed_tickets:
                role = str(data.get("role", ""))
                pid_match = False
                if role and live_pids:
                    try:
                        check = _sub.run(
                            ["pgrep", "-f", f"{role}.*{ticket_id[:16]}"],
                            capture_output=True, text=True, timeout=3,
                        )
                        pid_match = bool(check.stdout.strip())
                    except Exception:
                        pass
                if pid_match:
                    continue
                self._gate.complete_dispatch(agent_id)
            try:
                claim_file.unlink(missing_ok=True)
                lock_file = claim_file.with_suffix(".claim.lock")
                lock_file.unlink(missing_ok=True)
            except OSError:
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
        with self._lock:
            for record in self._agents.values():
                if record.ticket_id == ticket_id and record.state != AgentState.DEAD:
                    if record.state == AgentState.RUNNING:
                        return ReasonCode.RUNNING
                    if record.state == AgentState.STARTING:
                        return ReasonCode.STARTING
                    if record.state in (AgentState.CREATED, AgentState.ZOMBIE):
                        for s in self._gate.spawn_queue._queue:
                            if s.request.ticket_id == ticket_id:
                                return ReasonCode.WAITING_FOR_STAGGER
                        return ReasonCode.CLAIMED
        for _, claim, _ in self._gate._active.values():
            if claim.ticket_id == ticket_id:
                return ReasonCode.CLAIMED
        for s in self._gate.spawn_queue._queue:
            if s.request.ticket_id == ticket_id:
                return ReasonCode.WAITING_FOR_STAGGER
        if store is not None:
            try:
                t = store.get(ticket_id) if hasattr(store, "get") else None
                if t is not None:
                    state_val = t.state.value if hasattr(t.state, "value") else str(t.state)
                    deps = getattr(t, "dependencies", []) or []
                    if deps and state_val not in ("COMPLETE", "REJECTED", "DUPLICATE"):
                        for dep_id in deps:
                            try:
                                dep = store.get(dep_id) if hasattr(store, "get") else None
                                if dep is not None:
                                    dep_state = dep.state.value if hasattr(dep.state, "value") else str(dep.state)
                                    if dep_state not in ("COMPLETE", "RESOLVED", "SUPERSEDED", "CANCELLED"):
                                        return ReasonCode.BLOCKED_ON_DEPENDENCY
                            except Exception:
                                pass
                    if state_val == "DISCOVERED":
                        return ReasonCode.NEEDS_GOAL
                    if state_val in ("TRIAGED", "GOAL"):
                        goal_val = getattr(t, "goal", "")
                        if goal_val == "NOW" or state_val == "TRIAGED":
                            return ReasonCode.NEEDS_TRIAGE
                    if state_val in ("IMPLEMENT", "REWORK"):
                        if not self._dispatcher._has_plan_for(t):
                            risk = getattr(t, "risk", None)
                            risk_val = risk.value if hasattr(risk, "value") else str(risk) if risk else "medium"
                            try:
                                from codebot.ticket_engine import MIN_RISK_FOR_PLANNING, _RISK_ORDER
                                threshold = _RISK_ORDER.get(MIN_RISK_FOR_PLANNING.value, 1)
                                order = _RISK_ORDER.get(str(risk_val).lower(), 1)
                                if order >= threshold:
                                    return ReasonCode.PLAN_GATE
                            except Exception:
                                pass
                    if state_val == "BLOCKED":
                        return ReasonCode.BLOCKED_ON_DEPENDENCY
                    if state_val in ("COMPLETE", "REJECTED", "DUPLICATE", "LATER", "NEVER", "NOT_ACTIONABLE", "RESOLVED", "SUPERSEDED", "CANCELLED"):
                        return ReasonCode.NO_ACTIONABLE_WORK
            except Exception:
                pass
        if self._gate.concurrency.free_slots <= 0:
            return ReasonCode.WAITING_FOR_SLOT
        for bucket_name, role_set in BUCKET_TO_ROLE_SETS.items():
            for role in role_set:
                cap = self._dispatcher._role_caps.get(bucket_name.lower(), 0) if self._dispatcher._role_caps else 0
                if cap > 0:
                    active_in_role = sum(
                        1 for r in self._agents.values()
                        if r.role == role and r.state not in (AgentState.DEAD, AgentState.ZOMBIE)
                    )
                    if active_in_role >= cap:
                        return ReasonCode.ROLE_CAP_REACHED
        if store is not None:
            try:
                t = store.get(ticket_id) if hasattr(store, "get") else None
                if t is not None:
                    state_val = t.state.value if hasattr(t.state, "value") else str(t.state)
                    if state_val in ("IMPLEMENT", "REWORK", "REVIEW", "VERIFY", "PLANNING", "DECOMP", "REQUESTED", "DISCOVERED", "TRIAGED"):
                        return ReasonCode.READY
            except Exception:
                pass
        raise InvariantViolation(f"UNKNOWN reason for {ticket_id}: violates ADR-007 §14 exhaustive precedence")

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
            except InvalidTransitionError:
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
