#!/usr/bin/env python3
"""Health check logic for bot orchestrator.

Extracted from orchestrator.py to satisfy SRP. Handles:
- Bot health monitoring (stuck detection, exit handling)
- Ticket dispatch coordination
- Bot lifecycle management during health checks
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict

logger = logging.getLogger("orchestrator.health_check")


def init_tick_cache() -> Any:
    """Initialize TicketStore cache for this tick and log overhead."""
    from codebot.ticket_dispatcher import clear_ticket_store_cache, get_ticket_store
    clear_ticket_store_cache()
    _tick_t0 = time.time()
    _ts = get_ticket_store()
    _tick_elapsed_ms = (time.time() - _tick_t0) * 1000
    if _ts is not None:
        _tick_ticket_count = len(getattr(_ts, '_tickets', {}))
        logger.debug(
            "TicketStore cache initialized: %d tickets in %.1fms (single read per tick)",
            _tick_ticket_count, _tick_elapsed_ms,
        )
    else:
        logger.debug("TicketStore cache: tickets.json unavailable")
    return _ts


def retry_disabled_and_stuck(bots: Dict[str, Any], hb_cache: Dict) -> None:
    """Retry disabled or stuck-starting bots."""
    from codebot.dispatch_service import retry_disabled_bot, retry_stuck_starting
    from codebot.process_manager import update_bot_state
    for name, bot in bots.items():
        if not bot.config.enabled and bot.process is None:
            if retry_disabled_bot(bot):
                update_bot_state(bot, "waiting")
        if bot.config.enabled and bot.process is not None:
            if retry_stuck_starting(bot, heartbeat_cache=hb_cache):
                logger.info(f"Retrying '{name}' stuck in starting")


def handle_exited_bots(bots: Dict[str, Any], now: float, ts: Any) -> None:
    """Handle bots that have exited, running alignment and transitioning tickets."""
    from codebot.state_manager import get_paths
    from codebot.alignment_coordinator import write_alignment_event
    from codebot.alignment_service import run_alignment_pipeline
    from codebot.dispatch_service import (
        transition_ticket_on_success, transition_ticket_on_error,
        compute_rate_limit_backoff, rotate_model_on_error,
    )
    from codebot.process_manager import update_bot_state
    from codebot.scratchpad import load_scratchpad, save_scratchpad
    current_paths = get_paths()
    for name, bot in list(bots.items()):
        if not bot.config.enabled or bot.process is None or bot.process.poll() is None:
            continue
        exit_code = bot.process.returncode
        write_alignment_event(
            name, exit_code=exit_code,
            exit_reason="clean" if exit_code == 0 else "error",
            started_at=bot.started_at,
        )
        try:
            run_alignment_pipeline(name)
        except Exception as e:
            logger.warning(f"Alignment pipeline failed for {name}: {e}")
        bot.process = None

        if exit_code == 0:
            bot.consecutive_errors = 0
            transition_ticket_on_success(bot, bots, store=ts)
            bot.next_run_at = now + bot.config.interval_seconds
            update_bot_state(bot, "waiting")
        elif exit_code == 3:
            backoff, should_disable = compute_rate_limit_backoff(bot)
            bot.next_run_at = now + backoff
            rotate_model_on_error(bot, bots)
            if should_disable:
                bot.config.enabled = False
                update_bot_state(bot, "disabled")
            else:
                update_bot_state(bot, "waiting")
        else:
            bot.consecutive_errors += 1
            if bot.consecutive_errors >= 3:
                old_model = bot.config.model
                rotate_model_on_error(bot, bots)
                bot.consecutive_errors = 0
                logger.warning(f"Bot '{name}' failed 3x on {old_model} -> {bot.config.model}")
            assigned_tid = getattr(bot, "_assigned_ticket_id", "")
            if assigned_tid:
                try:
                    scratch = load_scratchpad(current_paths.state_dir, assigned_tid)
                    scratch.mark_error(f"Bot exited with code {exit_code}")
                    scratch.finish_agent(f"error: exit_code={exit_code}")
                    save_scratchpad(current_paths.state_dir, scratch)
                except Exception as e:
                    logger.warning(f"Failed to finish scratchpad for ticket {assigned_tid}: {e}")
            transition_ticket_on_error(bot, bots, exit_code, store=ts)
            bot.next_run_at = now + 5
            update_bot_state(bot, "waiting")


def handle_stuck_bots(bots: Dict[str, Any], now: float, hb_cache: Dict) -> None:
    """Detect and restart stuck bots based on heartbeat age."""
    from codebot.alignment_coordinator import write_alignment_event
    from codebot.alignment_service import run_alignment_pipeline
    from codebot.process_manager import (
        is_stuck, effective_heartbeat_timeout, model_profile, restart_bot,
    )
    for name, bot in bots.items():
        if not bot.config.enabled or bot.process is None or bot.process.poll() is not None:
            continue
        if is_stuck(bot, heartbeat_cache=hb_cache):
            eff = effective_heartbeat_timeout(bot)
            hb = hb_cache.get(bot.config.name, 0.0)
            hb_age = now - hb if hb else 0
            prof = model_profile(bot.config.model)
            risk = prof.lockup_risk if prof else '?'
            logger.warning(f"Bot '{name}' stuck (hb {hb_age:.0f}s > {eff}s, risk={risk})")
            write_alignment_event(name, exit_code=None, exit_reason="stuck", started_at=bot.started_at)
            try:
                run_alignment_pipeline(name)
            except Exception as e:
                logger.warning(f"Alignment failed for {name}: {e}")
            restart_bot(bot, reason="stuck", bots=bots)


def run_dispatchers(bots: Dict[str, Any], ts: Any) -> None:
    """Run all dispatcher tasks sharing the single TicketStore instance."""
    from codebot.ticket_dispatcher import (
        _sweep_orphan_claims, apply_agent_availability, spawn_demand_agents,
        dispatch_decompose_agents, dispatch_planning_agents, advance_reviewed_tickets,
        gatekeeper_verify_tickets, route_ready_tickets, process_rework_tickets,
        recover_deferred_tickets,
    )
    from codebot.process_manager import GATEWAY_MAX_CONCURRENT, start_bot, stop_bot, update_bot_state
    DECOMPOSER_MAX_CONCURRENT = 12
    tasks = [
        (lambda: _sweep_orphan_claims(bots), "orphan sweep"),
        (lambda: apply_agent_availability(bots, stop_fn=stop_bot, update_state_fn=update_bot_state, store=ts), "availability"),
        (lambda: spawn_demand_agents(bots, GATEWAY_MAX_CONCURRENT, start_bot_fn=start_bot, store=ts), "demand"),
        (lambda: dispatch_decompose_agents(bots, max_agents=DECOMPOSER_MAX_CONCURRENT, start_bot_fn=start_bot, store=ts), "decompose"),
        (lambda: dispatch_planning_agents(bots, start_bot_fn=start_bot, store=ts), "planning"),
        (lambda: advance_reviewed_tickets(bots, store=ts), "review"),
        (lambda: gatekeeper_verify_tickets(store=ts), "gatekeeper"),
        (lambda: route_ready_tickets(store=ts), "route"),
        (lambda: process_rework_tickets(bots, store=ts), "rework"),
        (lambda: recover_deferred_tickets(store=ts), "deferred"),
    ]
    for fn, label in tasks:
        try:
            fn()
        except Exception as e:
            logger.warning(f"{label} failed: {e}")


def start_eligible_bots(bots: Dict[str, Any], now: float, ts: Any) -> None:
    """Start eligible bots, skipping implementers/reviewers handled by dispatchers.

    O(1) early exit when no work available; O(B_role) iteration only over
    relevant bot roles instead of O(B) full scan. Uses pipeline state to
    determine which roles are needed before iterating.
    """
    from codebot.dispatch_service import get_pipeline_state, is_needed_bot
    from codebot.state_manager import is_draining
    from codebot.process_manager import start_bot, update_bot_state
    from codebot.dispatch_service import (
        IMPLEMENTER_ROLE_NAMES, REVIEWER_ROLE_NAMES,
        DECOMPOSER_ROLE_NAMES, PLANNING_ROLE_NAMES, DISCOVERY_ROLE_NAMES,
    )

    pipeline = get_pipeline_state(store=ts)

    # O(1) early exit: no work in any queue
    has_decompose_work = pipeline.get("DECOMPOSE", 0) > 0 or pipeline.get("READY", 0) > 0
    has_planning_work = pipeline.get("PLANNING", 0) > 0
    has_verifying_work = pipeline.get("VERIFYING", 0) > 0
    has_discovery_work = (
        pipeline.get("DISCOVERED", 0) + pipeline.get("VALIDATING", 0) +
        pipeline.get("TRIAGED", 0)
    ) > 0

    if not (has_decompose_work or has_planning_work or has_verifying_work or has_discovery_work):
        # No work available - set all non-implementer/reviewer bots to waiting
        for name, bot in bots.items():
            base = name.split("-")[0] if "-" in name else name
            if base in IMPLEMENTER_ROLE_NAMES or base in REVIEWER_ROLE_NAMES or name == "ux_reviewer":
                continue
            if bot.config.enabled and bot.process is None:
                bot.next_run_at = now + bot.config.interval_seconds
                update_bot_state(bot, "waiting")
        return

    # Build set of roles that are actually needed this tick
    needed_roles: set[str] = set()
    if has_decompose_work:
        needed_roles.update(DECOMPOSER_ROLE_NAMES)
    if has_planning_work:
        needed_roles.update(PLANNING_ROLE_NAMES)
    if has_verifying_work:
        needed_roles.add("quality_gate")
    if has_discovery_work:
        needed_roles.update(DISCOVERY_ROLE_NAMES)

    # O(B_role) iteration: only check bots whose role is actually needed
    for name, bot in bots.items():
        if not bot.config.enabled or is_draining() or bot.process is not None:
            continue
        if bot.next_run_at and now < bot.next_run_at:
            continue
        base = name.split("-")[0] if "-" in name else name
        if base in IMPLEMENTER_ROLE_NAMES or base in REVIEWER_ROLE_NAMES or name == "ux_reviewer":
            continue
        # O(1) role membership check instead of calling is_needed_bot()
        if base not in needed_roles and name != "quality_gate":
            bot.next_run_at = now + bot.config.interval_seconds
            update_bot_state(bot, "waiting")
            continue
        # Double-check with is_needed_bot for edge cases (e.g., unknown roles)
        if is_needed_bot(name, pipeline):
            has_ticket = bool(getattr(bot, "_assigned_ticket_id", ""))
            start_bot(bot, bots=bots, is_demand=has_ticket)
        else:
            bot.next_run_at = now + bot.config.interval_seconds
            update_bot_state(bot, "waiting")


def check_all_bots(bots: Dict[str, Any]) -> None:
    """Main health-check loop. Delegates all business logic to helper functions."""
    from codebot.state_manager import check_self_restart
    from codebot.dispatch_service import batch_read_bot_statuses, log_bot_statuses
    from codebot.process_manager import batch_read_heartbeats
    if check_self_restart(bots, stop_bot_fn=lambda b, r: None):
        return

    ts = init_tick_cache()
    now = time.time()

    bot_names = list(bots.keys())
    heartbeat_cache = batch_read_heartbeats(bot_names)
    running_names = [n for n, b in bots.items()
                     if b.process is not None and b.process.poll() is None]
    status_cache = batch_read_bot_statuses(running_names) if running_names else {}

    retry_disabled_and_stuck(bots, heartbeat_cache)
    handle_exited_bots(bots, now, ts)
    handle_stuck_bots(bots, now, heartbeat_cache)
    log_bot_statuses(bots, preloaded_statuses=status_cache)
    run_dispatchers(bots, ts)
    start_eligible_bots(bots, now, ts)
