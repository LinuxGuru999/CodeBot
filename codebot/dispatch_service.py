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
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = _CODEBOT_PKG_DIR.parent
STATE_DIR = _project_root / ".codebot" / "state"
LOGS_DIR = _project_root / ".codebot" / "logs"

# Role name sets (duplicated from orchestrator for independence)
IMPLEMENTER_ROLE_NAMES: frozenset[str] = frozenset({
    "general_implementer", "backend_implementer", "frontend_implementer",
    "test_implementer", "migration_implementer", "documentation_implementer",
})

DISCOVERY_ROLE_NAMES: frozenset[str] = frozenset({
    "bug_hunter", "security_auditor", "architecture_auditor", "performance_auditor",
    "test_gap_auditor", "documentation_auditor", "dependency_auditor", "ux_auditor",
    "feature_hunter",
})

REVIEWER_ROLE_NAMES: frozenset[str] = frozenset({
    "correctness_reviewer", "security_reviewer", "architecture_reviewer",
    "test_reviewer", "performance_reviewer", "simplicity_reviewer",
    "documentation_reviewer",
})

PLANNING_ROLE_NAMES: frozenset[str] = frozenset({
    "implementation_planner",
})

DECOMPOSER_ROLE_NAMES: frozenset[str] = frozenset({
    "decomposer",
})

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

def get_pipeline_state() -> dict[str, int]:
    """Get current pipeline state counts from TicketStore."""
    try:
        from codebot.ticket_engine import TicketStore, TicketState
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if not store_path.exists():
            return {}
        store = TicketStore(store_path)
        counts: dict[str, int] = {}
        for state in TicketState:
            tickets = store.list_by_state(state)
            if tickets:
                counts[state.value] = len(tickets)
        return counts
    except Exception:
        return {}


def is_needed_bot(name: str, pipeline: dict[str, int]) -> bool:
    """Check if a bot is needed based on pipeline state."""
    ready = pipeline.get("READY", 0)
    decompose = pipeline.get("DECOMPOSE", 0)
    planning = pipeline.get("PLANNING", 0)
    implementing = pipeline.get("IMPLEMENTING", 0)
    reviewing = pipeline.get("REVIEWING", 0)
    verifying = pipeline.get("VERIFYING", 0)

    always_on = {"scheduler", "conflict_resolver"}
    if name in always_on:
        return True
    base_name = name.split("-")[0] if "-" in name else name
    if base_name in DECOMPOSER_ROLE_NAMES:
        return decompose > 0 or ready > 0
    if base_name in PLANNING_ROLE_NAMES:
        return planning > 0
    if base_name in IMPLEMENTER_ROLE_NAMES:
        return implementing > 0
    if base_name in REVIEWER_ROLE_NAMES or base_name == "ux_reviewer":
        return reviewing > 0
    if name == "quality_gate":
        return verifying > 0
    if name == "ticket_triager":
        discovered = pipeline.get("DISCOVERED", 0) + pipeline.get("VALIDATING", 0) + pipeline.get("TRIAGED", 0)
        return discovered > 0
    return False


# ---------------------------------------------------------------------------
# Agent Availability
# ---------------------------------------------------------------------------

def apply_agent_availability(bots: dict[str, Any], stop_fn: Any = None, update_state_fn: Any = None) -> None:
    """Enable or suppress bot spawns based on current ticket queue state.
    
    Args:
        bots: Dict of bot name to BotState
        stop_fn: Optional callable(bot, reason) to stop a process
        update_state_fn: Optional callable(bot, status) to persist state
    """
    pipeline = get_pipeline_state()
    ready = pipeline.get("READY", 0)
    decompose = pipeline.get("DECOMPOSE", 0)
    planning = pipeline.get("PLANNING", 0)
    implementing = pipeline.get("IMPLEMENTING", 0)
    reviewing = pipeline.get("REVIEWING", 0)

    always_on = {"scheduler", "conflict_resolver"}

    for name, bot in bots.items():
        base_name = name.split("-")[0] if "-" in name else name
        if base_name in always_on:
            continue

        should_enable = False
        if base_name in DECOMPOSER_ROLE_NAMES:
            should_enable = decompose > 0 or ready > 0
        elif base_name in PLANNING_ROLE_NAMES:
            should_enable = planning > 0
        elif base_name in IMPLEMENTER_ROLE_NAMES:
            should_enable = implementing > 0
        elif base_name in REVIEWER_ROLE_NAMES or base_name == "ux_reviewer":
            should_enable = reviewing > 0
        elif base_name in DISCOVERY_ROLE_NAMES:
            should_enable = False  # Discovery is demand-driven
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

def transition_ticket_on_success(bot: Any, bots: dict[str, Any]) -> None:
    """Transition assigned ticket when bot exits cleanly (exit code 0)."""
    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
    if not assigned_tid:
        return
    
    base_role = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
    
    try:
        from codebot.ticket_engine import TicketStore, TicketState
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if store_path.exists():
            ts = TicketStore(store_path)
            t = ts.get(assigned_tid)
            if t is not None:
                if base_role in REVIEWER_ROLE_NAMES:
                    if t.state == TicketState.REVIEWING:
                        ts.transition(assigned_tid, TicketState.VERIFYING)
                        ts.flush()  # Ensure changes are written to disk immediately
                        logger.info(f"Ticket {assigned_tid} -> VERIFYING (reviewer {bot.config.name} completed)")
                else:
                    if t.state == TicketState.IMPLEMENTING:
                        ts.transition(assigned_tid, TicketState.REVIEWING)
                        ts.flush()  # Ensure changes are written to disk immediately
                        logger.info(f"Ticket {assigned_tid} -> REVIEWING (agent {bot.config.name} completed)")
            # Clean up claims
            claims_dir = STATE_DIR / "claims"
            for cf in claims_dir.glob(f"{assigned_tid}.*.json"):
                cf.unlink(missing_ok=True)
    except Exception as te:
        logger.warning(f"Ticket transition failed for {assigned_tid}: {te}")
    
    bot._assigned_ticket_id = ''


def transition_ticket_on_error(bot: Any, bots: dict[str, Any], exit_code: int) -> None:
    """Return ticket to READY when bot exits with error."""
    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
    if not assigned_tid:
        return
    
    try:
        from codebot.ticket_engine import TicketStore, TicketState
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if store_path.exists():
            ts = TicketStore(store_path)
            ticket = ts.get(assigned_tid)
            if ticket and ticket.state == TicketState.IMPLEMENTING:
                ts.transition(assigned_tid, TicketState.READY)
                ts.flush()  # Ensure changes are written to disk immediately
                logger.info(f"Bot '{bot.config.name}' errored (exit {exit_code}), returning ticket {assigned_tid} to READY for retry")
            bot._assigned_ticket_id = ''
    except Exception as e:
        logger.warning(f"Failed to transition ticket {assigned_tid} on error exit: {e}")
    
    # Clean up claims
    claims_dir = STATE_DIR / "claims"
    if claims_dir.exists():
        for cf in claims_dir.glob(f"{assigned_tid}.*.json"):
            try:
                cf.unlink()
            except OSError:
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


def retry_stuck_starting(bot: Any) -> bool:
    """Check if a bot stuck in 'starting' state should be retried."""
    if bot.process is not None and bot.process.poll() is not None:
        return True
    try:
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

def log_bot_statuses(bots: dict[str, Any]) -> None:
    """Log current activity for all running bots."""
    for name, bot in bots.items():
        alive = bot.process is not None and bot.process.poll() is None
        if not alive:
            continue
        status_path = STATE_DIR / f"{name}.status.json"
        try:
            if status_path.exists():
                data = json.loads(status_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    task = data.get("current_task", "unknown")
                    iteration = data.get("iteration", 0)
                    files = data.get("files_touched", [])
                    files_str = ", ".join(files[-3:]) if files else "none"
                    age_s = time.time() - data.get("updated_at", 0)
                    logger.info(f"[status] {name}: task={task} iter={iteration} files=[{files_str}] age={age_s:.0f}s")
        except Exception:
            pass
