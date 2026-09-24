#!/usr/bin/env python3
"""Dispatch Service — Pipeline state, agent availability, and ticket transitions.

Purpose
-------
Manages pipeline-aware bot scaling, ticket state transitions on bot exit,
and model rotation for error recovery. Extracted from orchestrator.py to
maintain the thin-router/fat-service invariant (Constitution §4).

Why
---
The orchestrator should only handle process spawn/stop/heartbeat monitoring.
Pipeline state queries, agent availability decisions, and ticket transitions
are business logic that belongs in a dedicated service.

Invariants
----------
- No subprocess management here — only state inspection and mutation
- Ticket transitions are atomic via TicketStore
- Model rotation is deterministic and logged
- Error exits (non-zero, non-3) return tickets to DECOMP for retry, never REVIEW
- REVIEW state is only reached on clean exits (exit code 0)
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

from codebot.ticket_dispatcher import (
    IMPLEMENTER_ROLE_NAMES,
    DISCOVERY_ROLE_NAMES,
    REVIEWER_ROLE_NAMES,
)
from codebot.role_registry import (
    CONTROL_ROLE_NAMES,
    PLANNING_ROLE_NAMES,
)

DECOMPOSER_ROLE_NAMES = frozenset({"decomposer"})

logger = logging.getLogger(__name__)

_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = _CODEBOT_PKG_DIR.parent
STATE_DIR = _project_root / ".codebot" / "state"
LOGS_DIR = _project_root / ".codebot" / "logs"

# Role name sets are imported from ticket_dispatcher.py (single source of truth).

RATE_LIMIT_REQUEUE_S = int(os.getenv("CODEBOT_RATE_LIMIT_REQUEUE_S", "300"))
RATE_LIMIT_BACKOFF_MAX = int(os.getenv("CODEBOT_RATE_LIMIT_BACKOFF_MAX", "3600"))
RATE_LIMIT_DISABLE_AFTER = int(os.getenv("CODEBOT_RATE_LIMIT_DISABLE_AFTER", "20"))

ALL_MODELS = [
    "xiaomi-mimo-2.5", "qwen-3.5-plus", "qwen-3.6-plus", "qwen-3.7-plus",
    "qwen-3.7-max", "qwen-3.8-max",
]


# ---------------------------------------------------------------------------
# Pipeline State
# ---------------------------------------------------------------------------

def get_pipeline_state(store: Any | None = None) -> dict[str, int]:
    """Get current pipeline state counts from TicketStore.

    O(1) via TicketStore.summary() which uses cached per-state counts,
    instead of O(N*S) scanning of list_by_state per state.

    Args:
        store: Optional shared TicketStore instance for this tick. If supplied,
               used directly to avoid an extra tickets.json read/parse.
    """
    try:
        from codebot.ticket_dispatcher import get_ticket_store
        _store = store if store is not None else get_ticket_store()
        if _store is None:
            return {}
        # summary() returns cached _state_counts dict in O(1)
        return _store.summary()
    except Exception:
        return {}


def is_needed_bot(name: str, pipeline: dict[str, int]) -> bool:
    """Check if a bot is needed based on pipeline state."""
    triaged = pipeline.get("TRIAGED", 0)
    goal = pipeline.get("GOAL", 0)
    decomp = pipeline.get("DECOMP", 0)
    planning = pipeline.get("PLANNING", 0)
    implement = pipeline.get("IMPLEMENT", 0)
    review = pipeline.get("REVIEW", 0)

    always_on: set[str] = {"git_sync", "github_mirror"}
    if name in always_on:
        return True
    base_name = name.split("-")[0] if "-" in name else name
    if base_name in DECOMPOSER_ROLE_NAMES:
        return decomp > 0 or goal > 0
    if base_name in PLANNING_ROLE_NAMES:
        return planning > 0
    if base_name in IMPLEMENTER_ROLE_NAMES:
        return implement > 0
    if base_name in REVIEWER_ROLE_NAMES or base_name == "ux_reviewer":
        return review > 0
    if name == "quality_gate":
        return review > 0
    if name == "ticket_triager":
        return pipeline.get("DISCOVERED", 0) > 0
    if name == "goal_aligner":
        return triaged > 0
    return False


# ---------------------------------------------------------------------------
# Agent Availability
# ---------------------------------------------------------------------------

def apply_agent_availability(bots: dict[str, Any], stop_fn: Any = None, update_state_fn: Any = None, store: Any | None = None) -> None:
    """Enable or suppress bot spawns based on current ticket queue state.
    
    Args:
        bots: Dict of bot name to BotState
        stop_fn: Optional callable(bot, reason) to stop a process
        update_state_fn: Optional callable(bot, status) to persist state
        store: Optional shared TicketStore instance for this tick.
    """
    pipeline = get_pipeline_state(store=store)
    triaged = pipeline.get("TRIAGED", 0)
    goal = pipeline.get("GOAL", 0)
    decomp = pipeline.get("DECOMP", 0)
    planning = pipeline.get("PLANNING", 0)
    implement = pipeline.get("IMPLEMENT", 0)
    review = pipeline.get("REVIEW", 0)

    always_on: set[str] = {"git_sync", "github_mirror"}

    for name, bot in bots.items():
        base_name = name.split("-")[0] if "-" in name else name
        if base_name in always_on:
            continue
        if base_name in DISCOVERY_ROLE_NAMES:
            continue

        should_enable = False
        if base_name in DECOMPOSER_ROLE_NAMES:
            should_enable = decomp > 0 or goal > 0
        elif base_name in PLANNING_ROLE_NAMES:
            should_enable = planning > 0
        elif base_name in IMPLEMENTER_ROLE_NAMES:
            should_enable = implement > 0
        elif base_name in REVIEWER_ROLE_NAMES or base_name == "ux_reviewer":
            should_enable = review > 0
        else:
            continue

        if not should_enable and bot.config.enabled:
            bot.config.enabled = False
            if bot.process is not None and bot.process.poll() is None:
                if stop_fn:
                    stop_fn(bot, "queue-empty")
                else:
                    try:
                        bot.process.terminate()
                        bot.process.wait(timeout=5)
                    except Exception:
                        try:
                            bot.process.kill()
                        except Exception:
                            pass
                    bot.process = None
            if update_state_fn:
                update_state_fn(bot, "disabled")
        elif should_enable and not bot.config.enabled:
            bot.config.enabled = True
            logger.info(f"[queue-scale] Re-enabled '{name}': work available in queue")


# ---------------------------------------------------------------------------
# Model Rotation
# ---------------------------------------------------------------------------

def rotate_model_on_error(bot: Any, bots: dict[str, Any] | None = None) -> str:
    """Simple model rotation for error recovery.
    
    Uses model_router's provider chain to select next model.
    Falls back to round-robin through ALL_MODELS if router unavailable.
    """
    try:
        from codebot.model_router import build_default_chain
        chain = build_default_chain()
        healthy = chain.healthy_providers()
        if healthy:
            # Get all models from healthy providers
            available_models = []
            for p in healthy:
                for alias in p.model_aliases.values():
                    if alias not in available_models:
                        available_models.append(alias)
            # Also include canonical names
            for m in ALL_MODELS:
                if m not in available_models:
                    available_models.append(m)
            failed_model = bot.config.model
            candidates = [m for m in available_models if m != failed_model]
            if candidates:
                new_model = candidates[0]
                old_model = bot.config.model
                bot.config.model = new_model
                bot.config.fallback_model = "xiaomi-mimo-2.5"
                logger.info(f"Model rotation for '{bot.config.name}': {old_model} -> {new_model}")
                return new_model
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Model router rotation failed: {e}, using fallback")

    # Fallback: simple rotation
    failed_model = bot.config.model
    candidates = [m for m in ALL_MODELS if m != failed_model]
    if not candidates:
        return failed_model
    new_model = candidates[0]
    old_model = bot.config.model
    bot.config.model = new_model
    bot.config.fallback_model = "xiaomi-mimo-2.5"
    logger.info(f"Model rotation for '{bot.config.name}': {old_model} -> {new_model}")
    return new_model


# ---------------------------------------------------------------------------
# Ticket Transition on Bot Exit
# ---------------------------------------------------------------------------

def transition_ticket_on_success(bot: Any, bots: dict[str, Any], store: Any | None = None) -> None:
    """Transition assigned ticket when bot exits cleanly (exit code 0).
    
    Args:
        bot: The bot that completed
        bots: Dict of all bots
        store: Optional shared TicketStore instance for this tick.
    """
    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
    if not assigned_tid:
        try:
            claims_dir = STATE_DIR / "claims"
            if claims_dir.exists():
                for cf in list(claims_dir.glob(f"{bot.config.name}*.json")) + list(claims_dir.glob("*.claim.json")) + list(claims_dir.glob(f"*.{bot.config.name}.claim.json")):
                    try:
                        data = json.loads(cf.read_text(encoding="utf-8"))
                        tid = data.get("ticket_id", data.get("ticket", "")) or cf.name.split(".")[0]
                        agent = str(data.get("agent_id", data.get("worker", data.get("bot", ""))))
                        role = str(data.get("role", ""))
                        if agent and agent != getattr(bot, "_agent_id", "") and bot.config.name not in agent and role not in bot.config.name:
                            continue
                        if tid:
                            assigned_tid = tid
                            bot._assigned_ticket_id = tid
                            break
                    except Exception:
                        pass
                if not assigned_tid:
                    for cf in claims_dir.glob(f"*.{bot.config.name}.json"):
                        try:
                            data = json.loads(cf.read_text(encoding="utf-8"))
                            tid = data.get("ticket_id", data.get("ticket", ""))
                            if tid:
                                assigned_tid = tid
                                bot._assigned_ticket_id = tid
                                break
                        except Exception:
                            pass
        except Exception:
            pass
        if not assigned_tid:
            return
    
    base_role = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
    
    try:
        from codebot.ticket_dispatcher import (
            IMPLEMENTATION_ROLE_ORDER,
            get_ticket_store,
            next_implementation_role,
        )
        from codebot.ticket_engine import TicketState
        ts = store if store is not None else get_ticket_store()
        if ts is not None:
            t = ts.get(assigned_tid)
            if t is not None:
                if base_role in REVIEWER_ROLE_NAMES:
                    clear_reviewer_failures(assigned_tid)
                    logger.info(f"Reviewer {bot.config.name} completed for ticket {assigned_tid} (verdict evaluation deferred to advance_reviewed_tickets)")
                elif base_role == "ticket_triager":
                    status_path = STATE_DIR / "ticket_triager.status.json"
                    triage_decision = "TRIAGED"
                    try:
                        if status_path.exists():
                            raw = status_path.read_text(encoding="utf-8")
                            data = json.loads(raw)
                            if isinstance(data, dict):
                                for entry in data.get("triaged", []):
                                    if isinstance(entry, dict) and entry.get("ticket_id") == assigned_tid:
                                        triage_decision = entry.get("decision", "TRIAGED")
                                        break
                                for entry in data.get("rejected", []):
                                    if isinstance(entry, dict) and entry.get("ticket_id") == assigned_tid:
                                        triage_decision = "REJECTED"
                                        break
                                for entry in data.get("duplicates", []):
                                    if isinstance(entry, dict) and entry.get("ticket_id") == assigned_tid:
                                        triage_decision = "DUPLICATE"
                                        break
                    except (json.JSONDecodeError, OSError, ValueError):
                        pass
                    if t.state == TicketState.DISCOVERED:
                        target = TicketState.TRIAGED if triage_decision == "TRIAGED" else (
                            TicketState.REJECTED if triage_decision == "REJECTED" else TicketState.DUPLICATE
                        )
                        try:
                            ts.transition(assigned_tid, target, actor=bot.config.name)
                            logger.info(f"Ticket {assigned_tid} -> {target.value} (triager {bot.config.name} completed)")
                        except ValueError as ve:
                            logger.warning(f"Triage transition failed for {assigned_tid}: {ve}")
                    elif t.state == TicketState.TRIAGED:
                        ts.transition(assigned_tid, TicketState.GOAL, actor=bot.config.name)
                        logger.info(f"Ticket {assigned_tid} -> GOAL (triager {bot.config.name} confirmed)")
                elif base_role in PLANNING_ROLE_NAMES:
                    if t.state == TicketState.PLANNING:
                        try:
                            ts.transition(assigned_tid, TicketState.IMPLEMENT, actor=bot.config.name)
                            logger.info(f"Ticket {assigned_tid} -> IMPLEMENT (planner {bot.config.name} completed)")
                        except ValueError as ve:
                            logger.warning(f"Plan gate blocked PLANNING->IMPLEMENT for {assigned_tid}: {ve}")
                        except Exception as e:
                            logger.warning(f"PLANNING transition failed for {assigned_tid}: {e}")
                elif base_role in IMPLEMENTER_ROLE_NAMES:
                    if t.state == TicketState.IMPLEMENT:
                        approvals = list(getattr(t, "implementation_approvals", []) or [])
                        if base_role not in approvals:
                            approvals.append(base_role)
                        updated = ts.record_implementation_approval(assigned_tid, base_role, actor=bot.config.name)
                        if updated is None:
                            logger.warning(f"Ticket {assigned_tid} not found when recording {base_role} approval")
                        elif next_implementation_role(updated) is None:
                            ts.transition(assigned_tid, TicketState.REVIEW, actor=bot.config.name)
                            logger.info(f"Ticket {assigned_tid} -> REVIEW (final implementer {bot.config.name} completed)")
                        else:
                            logger.info(f"Ticket {assigned_tid} implementation approval {len(updated.implementation_approvals)}/{len(IMPLEMENTATION_ROLE_ORDER)} by {bot.config.name}")
                elif base_role == "user_agent":
                    origin_id = getattr(t, "origin_id", "") or ""
                    req_data = None
                    if origin_id:
                        for subdir in ("requests/processed", "requests"):
                            candidate = STATE_DIR / subdir / f"{origin_id}.json"
                            if candidate.exists():
                                try:
                                    req_data = json.loads(candidate.read_text(encoding="utf-8"))
                                    break
                                except (json.JSONDecodeError, OSError):
                                    pass
                    req_status = req_data.get("status", "") if req_data else ""
                    try:
                        if req_status == "complete":
                            ts.transition(assigned_tid, TicketState.COMPLETE, actor=bot.config.name)
                            logger.info(f"Ticket {assigned_tid} -> COMPLETE (user_agent {bot.config.name} evaluated)")
                        elif req_status == "rejected":
                            ts.transition(assigned_tid, TicketState.REJECTED, actor=bot.config.name)
                            logger.info(f"Ticket {assigned_tid} -> REJECTED (user_agent {bot.config.name} rejected)")
                        elif req_status == "awaiting_input":
                            ts.transition(assigned_tid, TicketState.DEFERRED, actor=bot.config.name)
                            logger.info(f"Ticket {assigned_tid} -> DEFERRED (user_agent {bot.config.name} awaiting input)")
                        else:
                            logger.warning(f"user_agent {bot.config.name} exited but request status '{req_status}' has no mapped transition for {assigned_tid}")
                    except ValueError as ve:
                        logger.warning(f"user_agent transition failed for {assigned_tid}: {ve}")
                else:
                    if t.state == TicketState.DECOMP:
                        ts.transition(assigned_tid, TicketState.PLANNING, actor=bot.config.name)
                        logger.info(f"Ticket {assigned_tid} -> PLANNING (agent {bot.config.name} completed)")
            claims_dir = STATE_DIR / "claims"
            if base_role not in REVIEWER_ROLE_NAMES:
                for cf in claims_dir.glob(f"{assigned_tid}.*.json"):
                    claim_name = cf.name
                    removed = False
                    try:
                        data = json.loads(cf.read_text(encoding="utf-8"))
                        claim_bot = data.get("bot", data.get("worker", data.get("agent", "")))
                        if claim_bot == bot.config.name:
                            cf.unlink(missing_ok=True)
                            removed = True
                    except Exception:
                        cf.unlink(missing_ok=True)
                        removed = True
                    if removed:
                        try:
                            from codebot.ticket_dispatcher import release_claim

                            release_claim(claim_name)
                        except (ImportError, ValueError, KeyError):
                            pass
    except Exception as te:
        logger.warning(f"Ticket transition failed for {assigned_tid}: {te}")
    
    bot._assigned_ticket_id = ''


def record_workforce_completion(bot: Any, completed_at: float | None = None) -> None:
    """Record a clean engineering-worker duration for the next allocation tick."""
    base_role = bot.config.name.split("-", 1)[0]
    if base_role in IMPLEMENTER_ROLE_NAMES:
        stage = "implementation"
    elif base_role in REVIEWER_ROLE_NAMES or base_role == "ux_reviewer":
        stage = "review"
    elif base_role == "quality_gate":
        stage = "verification"
    else:
        return
    started_at = getattr(bot, "started_at", 0.0)
    if not isinstance(started_at, (int, float)) or started_at <= 0:
        return
    finished_at = completed_at if completed_at is not None else time.time()
    from codebot.workforce_feedback import WorkforceFlowHistory

    history_path = STATE_DIR / "workforce-flow.json"
    history = WorkforceFlowHistory.load(history_path)
    history.record(stage, finished_at - started_at)
    history.save(history_path)


REVIEWER_FAILURE_LIMIT = 3


def _reviewer_failure_path(state_dir: Path, ticket_id: str) -> Path:
    directory = state_dir / "reviewer_failures"
    directory.mkdir(parents=True, exist_ok=True)
    safe = ticket_id.replace("/", "_").replace("\\", "_")
    return directory / f"{safe}.json"


def _read_reviewer_failures(state_dir: Path, ticket_id: str) -> dict[str, Any]:
    try:
        return json.loads(_reviewer_failure_path(state_dir, ticket_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def _write_reviewer_failures(state_dir: Path, ticket_id: str, payload: dict[str, Any]) -> None:
    path = _reviewer_failure_path(state_dir, ticket_id)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    temporary.replace(path)


def record_reviewer_failure(ticket_id: str, bot_name: str, exit_code: int, log_tail: str = "") -> dict[str, Any]:
    state_dir = STATE_DIR
    payload = _read_reviewer_failures(state_dir, ticket_id)
    count = int(payload.get("count", 0) or 0) + 1
    payload = {
        "ticket_id": ticket_id,
        "count": count,
        "last_bot": bot_name,
        "last_exit_code": exit_code,
        "last_log_tail": log_tail[-2000:],
        "updated_at": time.time(),
    }
    _write_reviewer_failures(state_dir, ticket_id, payload)
    return payload


def clear_reviewer_failures(ticket_id: str) -> None:
    try:
        _reviewer_failure_path(STATE_DIR, ticket_id).unlink(missing_ok=True)
    except OSError:
        pass


def transition_ticket_on_error(bot: Any, bots: dict[str, Any], exit_code: int, store: Any | None = None) -> None:
    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
    if not assigned_tid:
        logger.debug(f"Bot '{bot.config.name}' exited with error {exit_code} but has no assigned ticket")
        return
    
    try:
        from codebot.ticket_dispatcher import get_ticket_store
        from codebot.ticket_engine import TicketState
        ts = store if store is not None else get_ticket_store()
        if ts is None:
            logger.warning(f"Ticket store unavailable, cannot transition ticket {assigned_tid}")
            bot._assigned_ticket_id = ''
            return
        
        ticket = ts.get(assigned_tid)
        if not ticket:
            logger.warning(f"Ticket {assigned_tid} not found in store, clearing assignment")
            bot._assigned_ticket_id = ''
            return
        
        current_state = ticket.state
        base_role = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
        if base_role in REVIEWER_ROLE_NAMES and current_state == TicketState.REVIEW:
            log_tail = ""
            try:
                log_path = LOGS_DIR / f"{bot.config.name}.log"
                if log_path.exists():
                    log_tail = "\n".join(log_path.read_text(encoding="utf-8", errors="ignore").splitlines()[-20:])
            except OSError:
                log_tail = ""
            failures = record_reviewer_failure(assigned_tid, bot.config.name, exit_code, log_tail)
            if int(failures.get("count", 0)) >= REVIEWER_FAILURE_LIMIT:
                try:
                    from codebot.review_store import write_verdict
                    write_verdict(
                        STATE_DIR,
                        assigned_tid,
                        base_role,
                        {
                            "verdict": "REWORK",
                            "phase": "INDEPENDENT_REVIEW",
                            "ticket_id": assigned_tid,
                            "reviewer": base_role,
                            "findings": [
                                {
                                    "severity": "MAJOR",
                                    "category": "reviewer-failure",
                                    "finding": f"Reviewer {bot.config.name} failed {failures.get('count')} times (last exit {exit_code})",
                                    "file": "",
                                    "location": "",
                                    "evidence": log_tail[-1000:],
                                    "reproduction": "respawn reviewer with the current review packet",
                                    "expected": "reviewer writes a ticket-scoped verdict",
                                    "actual": f"exit code {exit_code}",
                                    "recommended_fix": "inspect reviewer log tail and rerun the listed packet tests",
                                }
                            ],
                            "checklist": {"items": {}, "notes": {}},
                            "summary": "Reviewer operational failure; route to rework",
                            "completed_at": time.time(),
                        },
                    )
                    clear_reviewer_failures(assigned_tid)
                except (OSError, ValueError):
                    pass
                logger.warning(f"Reviewer '{bot.config.name}' failed repeatedly for {assigned_tid}; recorded operational REWORK verdict")
            else:
                logger.warning(f"Reviewer '{bot.config.name}' errored (exit {exit_code}) for {assigned_tid}; failure {failures.get('count')}/{REVIEWER_FAILURE_LIMIT}")
        elif current_state == TicketState.IMPLEMENT:
            if exit_code == 3 and base_role in IMPLEMENTER_ROLE_NAMES:
                logger.info(f"Implementer '{bot.config.name}' hit 429 rate limit for {assigned_tid} — leaving in IMPLEMENT for transient retry")
            else:
                ts.transition(assigned_tid, TicketState.REWORK, actor=bot.config.name)
                logger.info(f"Bot '{bot.config.name}' errored (exit {exit_code}), returning ticket {assigned_tid} to REWORK for retry")
        elif current_state == TicketState.PLANNING:
            ts.transition(assigned_tid, TicketState.DECOMP, actor=bot.config.name)
            logger.info(f"Bot '{bot.config.name}' errored (exit {exit_code}), returning ticket {assigned_tid} to DECOMP for retry")
        elif current_state == TicketState.DECOMP:
            ts.transition(assigned_tid, TicketState.GOAL, actor=bot.config.name)
            logger.info(f"Bot '{bot.config.name}' errored (exit {exit_code}), returning ticket {assigned_tid} to GOAL for retry")
        elif current_state == TicketState.REVIEW:
            ts.transition(assigned_tid, TicketState.REWORK, actor=bot.config.name)
            logger.info(f"Bot '{bot.config.name}' errored (exit {exit_code}), returning ticket {assigned_tid} to REWORK for retry")
        else:
            logger.warning(
                f"Bot '{bot.config.name}' errored (exit {exit_code}) but ticket {assigned_tid} "
                f"is in state {current_state.value}, not IMPLEMENT. Skipping transition."
            )
        bot._assigned_ticket_id = ''
    except ValueError as ve:
        # Invalid transition - log but don't crash
        logger.warning(f"Invalid ticket transition for {assigned_tid}: {ve}")
        bot._assigned_ticket_id = ''
    except Exception as e:
        logger.warning(f"Failed to transition ticket {assigned_tid} on error exit: {e}")
    
    claims_dir = STATE_DIR / "claims"
    if claims_dir.exists():
        for cf in claims_dir.glob(f"{assigned_tid}.*.json"):
            try:
                claim_name = cf.name
                cf.unlink()
            except OSError:
                claim_name = cf.name
            try:
                from codebot.ticket_dispatcher import release_claim

                release_claim(claim_name)
            except (ImportError, ValueError, KeyError):
                pass


# ---------------------------------------------------------------------------
# Rate Limit Handling
# ---------------------------------------------------------------------------

def compute_rate_limit_backoff(bot: Any) -> tuple[float, bool]:
    """Compute backoff duration for rate-limited bot. Returns (backoff_seconds, should_disable)."""
    bot.consecutive_errors += 1
    backoff = min(RATE_LIMIT_REQUEUE_S * (2 ** (bot.consecutive_errors - 1)), RATE_LIMIT_BACKOFF_MAX)
    jitter = random.randint(0, min(60, backoff // 4))
    total = backoff + jitter
    should_disable = bot.consecutive_errors >= RATE_LIMIT_DISABLE_AFTER
    return total, should_disable


# ---------------------------------------------------------------------------
# Retry Helpers
# ---------------------------------------------------------------------------

def retry_disabled_bot(bot: Any) -> bool:
    """Re-enable a disabled bot after cooldown period."""
    state_file = STATE_DIR / f"{bot.config.name}.state.json"
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text())
            if data.get("status") == "disabled":
                last_update = data.get("last_update", 0)
                if time.time() - last_update > 10.0:
                    logger.info(f"Retrying disabled bot '{bot.config.name}' after 10s cooldown")
                    bot.consecutive_errors = 0
                    bot.config.enabled = True
                    return True
    except Exception:
        pass
    return False


def retry_stuck_starting(bot: Any,
                         heartbeat_cache: dict[str, float] | None = None) -> bool:
    """Check if a bot stuck in 'starting' state should be retried.

    Args:
        bot: BotState to check.
        heartbeat_cache: Optional preloaded heartbeats from batch_read_heartbeats().
    """
    if bot.process is not None and bot.process.poll() is not None:
        return True
    try:
        if heartbeat_cache is not None and bot.config.name in heartbeat_cache:
            last_hb = heartbeat_cache[bot.config.name]
        else:
            from codebot.process_manager import read_heartbeat
            last_hb = read_heartbeat(bot.config.name)
        if last_hb > 0:
            if time.time() - last_hb > 120.0:
                return True
            return False
    except ImportError:
        pass
    elapsed = time.time() - (bot.started_at or bot.last_heartbeat or time.time())
    return elapsed > 120.0


# ---------------------------------------------------------------------------
# Status Logging
# ---------------------------------------------------------------------------

def batch_read_bot_states(bot_names: list[str]) -> dict[str, dict | None]:
    """Read .state.json files for all bots in a single pass.

    Returns {name: parsed_dict_or_None}. Bots with missing/unreadable state
    files get None. This allows checking queued status without per-bot disk I/O.
    """
    results: dict[str, dict | None] = {}
    for name in bot_names:
        state_path = STATE_DIR / f"{name}.state.json"
        try:
            if state_path.exists():
                data = json.loads(state_path.read_text(encoding="utf-8"))
                results[name] = data if isinstance(data, dict) else None
            else:
                results[name] = None
        except Exception:
            results[name] = None
    return results


def batch_read_bot_statuses(bot_names: list[str],
                            sample_size: int | None = None) -> dict[str, dict | None]:
    """Read .status.json files for bots in a single pass.

    Args:
        bot_names: List of bot names to read statuses for.
        sample_size: If provided, only read statuses for a random sample of this
            many bots. Bounds I/O to O(sample_size) per tick. If None, reads all.

    Returns {name: parsed_dict_or_None}. Bots with missing/unreadable status
    files get None.
    """
    results: dict[str, dict | None] = {}
    
    # Apply sampling if requested
    names_to_read = bot_names
    if sample_size is not None and sample_size < len(bot_names):
        # Deterministic sampling based on current time for reproducibility within same tick
        tick_seed = int(time.time()) % 10000
        rng = random.Random(tick_seed)
        names_to_read = rng.sample(bot_names, sample_size)
    
    for name in names_to_read:
        status_path = STATE_DIR / f"{name}.status.json"
        try:
            if status_path.exists():
                data = json.loads(status_path.read_text(encoding="utf-8"))
                results[name] = data if isinstance(data, dict) else None
            else:
                results[name] = None
        except Exception:
            results[name] = None
    return results


def log_bot_statuses(bots: dict[str, Any],
                     preloaded_statuses: dict[str, dict | None] | None = None,
                     sample_size: int | None = None) -> None:
    """Log current activity for running bots.

    Args:
        bots: Dict of bot name to BotState.
        preloaded_statuses: Optional preloaded .status.json data from
            batch_read_bot_statuses(). If provided, used instead of per-file reads.
        sample_size: If provided, only log a random sample of this many bots per call.
            This bounds I/O to O(sample_size) per tick instead of O(n_bots).
            If None, logs all running bots (backward compatible).
    """
    now = time.time()
    
    # Collect alive bots first
    alive_bots = []
    for name, bot in bots.items():
        alive = bot.process is not None and bot.process.poll() is None
        if alive:
            alive_bots.append(name)
    
    # Apply sampling if requested
    if sample_size is not None and sample_size < len(alive_bots):
        # Deterministic sampling based on tick time to ensure coverage over time
        # Use time-based seed for reproducibility within same tick
        tick_seed = int(now) % 10000
        rng = random.Random(tick_seed)
        sampled_names = set(rng.sample(alive_bots, sample_size))
    else:
        sampled_names = set(alive_bots)
    
    for name in alive_bots:
        if name not in sampled_names:
            continue
            
        bot = bots[name]
        if preloaded_statuses is not None:
            data = preloaded_statuses.get(name)
        else:
            status_path = STATE_DIR / f"{name}.status.json"
            data = None
            try:
                if status_path.exists():
                    data = json.loads(status_path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict):
                        data = None
            except Exception:
                data = None
        if data is not None:
            task = data.get("current_task", "unknown")
            iteration = data.get("iteration", 0)
            files = data.get("files_touched", [])
            files_str = ", ".join(files[-3:]) if files else "none"
            age_s = now - data.get("updated_at", 0)
            logger.info(f"[status] {name}: task={task} iter={iteration} files=[{files_str}] age={age_s:.0f}s")


def dispatch_ready_tickets(
    bots: dict[str, Any],
    skip_route_tids: set[str] | None = None,
    start_bot_fn: Any = None,
    stop_fn: Any = None,
    update_state_fn: Any = None,
    store: Any = None,
) -> None:
    """Sequence routing logic directly without delegating to external orchestrator-like modules.
    
    This function implements the core dispatch sequencing inline, adhering to the
    thin-router/fat-service invariant (Constitution §4). It avoids indirect delegation
    to scheduler_v2 or workforce_dispatch to prevent circular architectural dependencies.
    
    Routing sequence:
    1. Process rework/stalled tickets first to free capacity
    2. Advance reviewed tickets to transition out of REVIEW state
    3. Spawn demand agents for IMPLEMENT and REVIEW stages
    """
    # Resolve store if not provided
    if store is None:
        try:
            from codebot.ticket_dispatcher import get_ticket_store
            store = get_ticket_store()
        except Exception:
            store = None
    
    if store is None:
        logger.debug("No store available for dispatch; skipping.")
        return
    
    # 1. Process rework/stalled tickets first
    try:
        from codebot.ticket_dispatcher import process_rework_tickets
        process_rework_tickets(bots, store=store)
    except Exception as e:
        logger.warning(f"process_rework_tickets failed: {e}")
    
    # 2. Advance reviewed tickets (transition REVIEW -> COMPLETE/REWORK/etc)
    try:
        from codebot.ticket_dispatcher import advance_reviewed_tickets
        advance_reviewed_tickets(bots, store=store)
    except Exception as e:
        logger.warning(f"advance_reviewed_tickets failed: {e}")
    
    # 3. Spawn demand agents for IMPLEMENT and REVIEW stages
    try:
        from codebot.ticket_dispatcher import spawn_demand_agents
        from codebot.process_manager import GATEWAY_MAX_CONCURRENT
        spawned = spawn_demand_agents(
            bots=bots,
            max_concurrent=GATEWAY_MAX_CONCURRENT,
            start_bot_fn=start_bot_fn,
            store=store,
        )
        if spawned:
            logger.info(f"Dispatch spawned {spawned} agents")
    except Exception as e:
        logger.warning(f"spawn_demand_agents failed: {e}")
