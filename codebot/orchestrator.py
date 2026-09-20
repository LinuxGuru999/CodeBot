#!/usr/bin/env python3
"""Bot Orchestrator — Thin coordinator delegating to focused modules.

Purpose
-------
Coordinates bot lifecycle by delegating to:
- process_manager: bot subprocess management, health, prompt/code checks
- alignment_coordinator: post-exit alignment events
- ticket_dispatcher: ticket-to-bot routing and claims
- worker_scaler: registry, scaling limits, resource checks
- state_manager: paths, drain/lock, backup/restore

The orchestrator contains NO business logic. It only wires modules
and runs the main event loop.
"""
from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Imports & Re-exports for Backward Compatibility
# ---------------------------------------------------------------------------
from codebot.process_manager import (
    BotConfig, BotState,
    start_bot, stop_bot, restart_bot,
    is_stuck, is_log_stalled, effective_heartbeat_timeout, read_heartbeat,
    heartbeat_path, checkpoint_path, read_checkpoint, update_bot_state,
    model_profile, ModelProfile, MODEL_PROFILES,
    _write_json_atomic, log_mtime,
    BOTS_DIR, STATE_DIR, LOGS_DIR, GATEWAY_MAX_CONCURRENT,
    _get_code_mtimes,
    batch_read_heartbeats,
)
from codebot.alignment_coordinator import write_alignment_event
from codebot.alignment_service import (
    run_alignment_pipeline, run_alignment_pipeline_for_all,
)
from codebot.ticket_dispatcher import (
    spawn_demand_agents, dispatch_decompose_agents,
    dispatch_planning_agents, advance_reviewed_tickets,
    gatekeeper_verify_tickets, route_ready_tickets,
    process_rework_tickets, recover_deferred_tickets,
    _sweep_orphan_claims, clear_ticket_store_cache, get_ticket_store,
    TICKET_CLASS_TO_IMPLEMENTER, TICKET_CLASS_TO_REVIEWER,
)
from codebot.dispatch_service import (
    get_pipeline_state, is_needed_bot, apply_agent_availability,
    rotate_model_on_error, transition_ticket_on_success,
    transition_ticket_on_error, compute_rate_limit_backoff,
    retry_disabled_bot, retry_stuck_starting, log_bot_statuses,
    IMPLEMENTER_ROLE_NAMES, REVIEWER_ROLE_NAMES,
    batch_read_bot_statuses,
)
from codebot.scratchpad import load_scratchpad, save_scratchpad
from codebot.state_manager import (
    PathConfig, get_paths, is_draining, set_drain, clear_drain,
    drain_status, backup_botnet, restore_botnet, check_self_restart,
    safe_stop_all, set_project_adapter as _sm_set_project_adapter,
)
from codebot.worker_scaler import (
    load_bot_registry, build_bots, rotating_slots, worker_reserved_slots,
    _get_available_memory_mb, _model_tier_for_complexity,
    is_manifest_error_disabled, is_manifest_restart_budget_exceeded,
    _read_state_file, CLAIM_TTL_SECONDS, MIN_ROTATING_SLOTS,
)
from codebot.orchestrator_services import (
    _manifest_restart_budget_exceeded,
    _manifest_error_disabled,
    is_restart_budget_exceeded,
    is_error_disabled,
)

# Module-level path configuration instance — delegates to state_manager.
# Paths are always read dynamically from state_manager.get_paths().
_paths: PathConfig = get_paths()


# Backward compatibility aliases via __getattr__ — no global mutation needed.
def __getattr__(name: str) -> Any:
    if name in ("BOTS_DIR", "STATE_DIR", "LOGS_DIR", "BACKUP_DIR",
                "ALIGNMENT_EVENTS_DIR", "DRAIN_FILE", "UPDATE_LOCK", "RESTART_FILE"):
        p = get_paths()
        mapping = {
            "BOTS_DIR": p.bots_dir,
            "STATE_DIR": p.state_dir,
            "LOGS_DIR": p.logs_dir,
            "BACKUP_DIR": p.backup_dir,
            "ALIGNMENT_EVENTS_DIR": p.alignment_events_dir,
            "DRAIN_FILE": p.drain_file,
            "UPDATE_LOCK": p.update_lock,
            "RESTART_FILE": p.restart_file,
        }
        return mapping[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "BotConfig", "BotState", "ModelProfile", "MODEL_PROFILES",
    "start_bot", "stop_bot", "restart_bot",
    "is_stuck", "is_log_stalled", "effective_heartbeat_timeout", "model_profile",
    "read_heartbeat", "heartbeat_path", "log_mtime", "update_bot_state",
    "checkpoint_path", "read_checkpoint", "BOTS_DIR", "STATE_DIR", "LOGS_DIR",
    "is_draining", "set_drain", "clear_drain", "get_status", "print_status",
    "safe_stop_all", "backup_botnet", "restore_botnet", "drain_status",
    "PathConfig", "set_project_adapter",
    "rotating_slots", "worker_reserved_slots", "_get_available_memory_mb",
    "_model_tier_for_complexity", "is_manifest_error_disabled",
    "is_manifest_restart_budget_exceeded", "_read_state_file",
    "CLAIM_TTL_SECONDS", "MIN_ROTATING_SLOTS",
    "_write_json_atomic",
    "_get_code_mtimes",
    "write_alignment_event",
    "_manifest_restart_budget_exceeded",
    "_manifest_error_disabled",
    "is_restart_budget_exceeded",
    "is_error_disabled",
]


def set_project_adapter(adapter: Any) -> PathConfig:
    """Delegate to state_manager.set_project_adapter and update local _paths reference.

    Uses module-level reference instead of ``global`` to avoid global-state
    mutation patterns.  Components should use the returned config object or
    call ``get_paths()`` after this function has been invoked during bootstrap.
    """
    import codebot.orchestrator as _mod
    _mod._paths = _sm_set_project_adapter(adapter)
    return _mod._paths


DECOMPOSER_MAX_CONCURRENT = 12

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(get_paths().logs_dir / "orchestrator.log")],
)
logger = logging.getLogger("orchestrator")


def read_prompt_with_mtime(prompt_path: Path) -> tuple[str, float]:
    try:
        content = prompt_path.read_text(encoding="utf-8")
        return content, prompt_path.stat().st_mtime
    except FileNotFoundError:
        return "", 0.0


def _check_prompt_changes(bots: dict[str, BotState]) -> None:
    for bot in bots.values():
        _, observed_mtime = read_prompt_with_mtime(BOTS_DIR / bot.config.prompt_file)
        if observed_mtime <= bot.last_prompt_mtime:
            continue
        bot.last_prompt_mtime = observed_mtime
        if bot.process is not None and bot.process.poll() is None:
            stop_bot(bot, "prompt-hot-reload")


def get_status(bots: dict[str, BotState]) -> dict:
    """Return a dict of bot statuses for display or API."""
    out: dict[str, dict[str, Any]] = {}
    # Batch-read all heartbeats once
    heartbeat_cache = batch_read_heartbeats([b.config.name for b in bots.values()])
    for n, b in bots.items():
        pid = b.process.pid if b.process and b.process.poll() is None else None
        hb = heartbeat_cache.get(b.config.name, 0.0)
        ha = max(0.0, time.time() - hb) if hb > 0 else None
        pr = model_profile(b.config.model)
        out[n] = {
            "enabled": b.config.enabled, "running": pid is not None, "pid": pid,
            "model": b.config.model, "risk": pr.lockup_risk if pr else "unknown",
            "eff_timeout": effective_heartbeat_timeout(b),
            "heartbeat_age_seconds": round(ha, 1) if ha else None,
            "next_run_in": round(b.next_run_at - time.time(), 1) if b.next_run_at and not pid else None,
            "restart_count": b.restart_count, "consecutive_errors": b.consecutive_errors,
        }
    return out


def print_status(bots: dict[str, BotState]) -> None:
    """Print a human-readable status table to stdout."""
    st = get_status(bots)
    print("\n" + "=" * 90 + "\nBOT ORCHESTRATOR STATUS\n" + "=" * 90)
    for n, i in st.items():
        s = "RUNNING" if i["running"] else ("DISABLED" if not i["enabled"] else ("WAITING" if i["next_run_in"] and i["next_run_in"] > 0 else "STOPPED"))
        nxt_val = i.get("next_run_in")
        nxt_str = f"{nxt_val:.0f}s" if nxt_val and nxt_val > 0 else "-"
        hb_str = f"{i['heartbeat_age_seconds']}s" if i['heartbeat_age_seconds'] else "-"
        print(f"  {n:15s} {s:9s} PID={str(i['pid'] or '-'):>6s} HB={hb_str:>7s} NEXT={nxt_str:>6s} eff={i['eff_timeout']:4.0f}s risk={i['risk']:11s} {i['model']}")
    print("=" * 90 + "\n")


def check_all_bots(bots: dict[str, BotState]) -> None:
    """Main health-check loop. Delegates all business logic to services."""
    _paths = get_paths()
    if check_self_restart(bots, stop_bot):
        return

    # Clear TicketStore cache to ensure fresh data for this tick
    clear_ticket_store_cache()

    # Eagerly initialize TicketStore cache once per tick and log overhead
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

    now = time.time()

    # --- Batch-read all heartbeats and statuses once per tick ---
    bot_names = list(bots.keys())
    heartbeat_cache = batch_read_heartbeats(bot_names)
    running_names = [n for n, b in bots.items()
                     if b.process is not None and b.process.poll() is None]
    status_cache = batch_read_bot_statuses(running_names) if running_names else {}

    # Retry disabled or stuck-starting bots
    for name, bot in bots.items():
        if not bot.config.enabled and bot.process is None:
            if retry_disabled_bot(bot):
                update_bot_state(bot, "waiting")
        if bot.config.enabled and bot.process is not None:
            if retry_stuck_starting(bot, heartbeat_cache=heartbeat_cache):
                logger.info(f"Retrying '{name}' stuck in starting")

    # Handle exited bots
    current_paths = get_paths()
    for name, bot in list(bots.items()):
        if not bot.config.enabled or bot.process is None or bot.process.poll() is None:
            continue
        exit_code = bot.process.returncode
        write_alignment_event(name, exit_code=exit_code, exit_reason="clean" if exit_code == 0 else "error", started_at=bot.started_at)
        try:
            run_alignment_pipeline(name)
        except Exception as e:
            logger.warning(f"Alignment pipeline failed for {name}: {e}")
        bot.process = None

        if exit_code == 0:
            bot.consecutive_errors = 0
            transition_ticket_on_success(bot, bots, store=_ts)
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
            transition_ticket_on_error(bot, bots, exit_code, store=_ts)
            bot.next_run_at = now + 5
            update_bot_state(bot, "waiting")

    # Handle stuck bots — use cached heartbeats
    for name, bot in bots.items():
        if not bot.config.enabled or bot.process is None or bot.process.poll() is not None:
            continue
        if is_stuck(bot, heartbeat_cache=heartbeat_cache):
            eff = effective_heartbeat_timeout(bot)
            hb = heartbeat_cache.get(bot.config.name, 0.0)
            hb_age = now - hb if hb else 0
            prof = model_profile(bot.config.model)
            logger.warning(f"Bot '{name}' stuck (hb {hb_age:.0f}s > {eff}s, risk={prof.lockup_risk if prof else '?'})")
            write_alignment_event(name, exit_code=None, exit_reason="stuck", started_at=bot.started_at)
            try:
                run_alignment_pipeline(name)
            except Exception as e:
                logger.warning(f"Alignment failed for {name}: {e}")
            restart_bot(bot, reason="stuck", bots=bots)

    log_bot_statuses(bots, preloaded_statuses=status_cache)

    # Run dispatcher tasks — all share the single TicketStore instance for this tick
    for fn, label in [
        (lambda: _sweep_orphan_claims(bots), "orphan sweep"),
        (lambda: apply_agent_availability(bots, stop_fn=stop_bot, update_state_fn=update_bot_state, store=_ts), "availability"),
        (lambda: spawn_demand_agents(bots, GATEWAY_MAX_CONCURRENT, start_bot_fn=start_bot, store=_ts), "demand"),
        (lambda: dispatch_decompose_agents(bots, max_agents=DECOMPOSER_MAX_CONCURRENT, start_bot_fn=start_bot, store=_ts), "decompose"),
        (lambda: dispatch_planning_agents(bots, start_bot_fn=start_bot, store=_ts), "planning"),
        (lambda: advance_reviewed_tickets(bots, store=_ts), "review"),
        (lambda: gatekeeper_verify_tickets(store=_ts), "gatekeeper"),
        (lambda: route_ready_tickets(store=_ts), "route"),
        (lambda: process_rework_tickets(bots, store=_ts), "rework"),
        (lambda: recover_deferred_tickets(store=_ts), "deferred"),
    ]:
        try:
            fn()
        except Exception as e:
            logger.warning(f"{label} failed: {e}")

    # Start eligible bots (skip implementers/reviewers — spawn_demand_agents handles them)
    pipeline = get_pipeline_state(store=_ts)
    for name, bot in bots.items():
        if not bot.config.enabled or is_draining() or bot.process is not None:
            continue
        if bot.next_run_at and now < bot.next_run_at:
            continue
        base = name.split("-")[0] if "-" in name else name
        if base in IMPLEMENTER_ROLE_NAMES or base in REVIEWER_ROLE_NAMES or name == "ux_reviewer":
            continue
        if is_needed_bot(name, pipeline):
            has_ticket = bool(getattr(bot, "_assigned_ticket_id", ""))
            start_bot(bot, bots=bots, is_demand=has_ticket)
        else:
            bot.next_run_at = now + bot.config.interval_seconds
            update_bot_state(bot, "waiting")


def main() -> None:
    """Entry point: parse args, build bot state, run or dispatch."""
    import argparse
    p = argparse.ArgumentParser(description="Bot Orchestrator")
    p.add_argument("--status", action="store_true")
    p.add_argument("--stop-all", action="store_true")
    p.add_argument("--safe-stop", action="store_true")
    p.add_argument("--drain", action="store_true")
    p.add_argument("--clear-drain", action="store_true")
    p.add_argument("--drain-status", action="store_true")
    p.add_argument("--start", nargs="*")
    p.add_argument("--check-interval", type=int, default=30)
    args = p.parse_args()

    registry = load_bot_registry()
    bots = build_bots(registry)

    if args.status:
        print_status(bots)
        if is_draining():
            print(f"DRAIN ACTIVE: {drain_status()}")
        return
    if args.drain_status:
        import pprint
        pprint.pprint(drain_status())
        print_status(bots)
        return
    if args.clear_drain:
        clear_drain()
        print("Drain cleared.")
        return
    if args.safe_stop or args.drain:
        safe_stop_all(bots, stop_bot)
        print("Safe stop complete.")
        print_status(bots)
        return
    if args.stop_all:
        for b in bots.values():
            stop_bot(b, "stop-all")
        return
    if args.start is not None:
        if is_draining():
            print("Refusing --start while draining", file=sys.stderr)
            sys.exit(4)
        for n in args.start or [b.config.name for b in bots.values()]:
            if n in bots and bots[n].config.enabled:
                start_bot(bots[n])
        if args.start:
            time.sleep(2)
            print_status(bots)
        return

    # Bootstrap adapter
    try:
        from codebot.codebot_bootstrap import bootstrap as _cb
        adapter = _cb(BOTS_DIR)
        if adapter:
            logger.info("Bootstrapped: %s", adapter.project_name())
            registry = load_bot_registry(adapter)
            bots = build_bots(registry)
            from codebot.state_manager import set_adapter_instance
            set_adapter_instance(adapter)
    except ImportError:
        adapter = None
    except Exception as e:
        logger.warning("Bootstrap failed: %s", e)

    def shutdown_handler(signum, frame):
        for b in bots.values():
            stop_bot(b, "shutdown")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    if not is_draining():
        for b in bots.values():
            b._assigned_ticket_id = ""
        apply_agent_availability(bots, stop_fn=stop_bot, update_state_fn=update_bot_state)
        logger.info(f"Overture: pipeline {get_pipeline_state()}")

    logger.info("Orchestrator starting, health check every %ds", args.check_interval)
    last_align = time.time()
    while True:
        try:
            check_all_bots(bots)
            time.sleep(args.check_interval)
            if time.time() - last_align >= 1800:
                run_alignment_pipeline_for_all()
                last_align = time.time()
        except KeyboardInterrupt:
            shutdown_handler(None, None)
        except Exception as e:
            logger.error(f"Health check error: {e}")
            time.sleep(args.check_interval)


if __name__ == "__main__":
    main()
