#!/usr/bin/env python3
"""Bot Orchestrator — Thin Controller.

Purpose
-------
Coordinates bot lifecycle and ticket dispatching by delegating to
process_manager, ticket_dispatcher, alignment_service, and orchestrator_services.

The orchestrator is a pure process manager that spawns bot subprocesses
and monitors their health. It does NOT contain business logic for
dispatching, alignment, lease management, or health check helpers.

Invariants
----------
- Single process (no multiprocessing)
- Heartbeat files are the ONLY communication channel from bots
- Orchestrator never reads bot output files (bots write directly to docs/)
- Kill signals: SIGTERM first (5s grace), then SIGKILL
- Max restarts per bot per hour: configurable (default 5)
"""

import json
import logging
import os
import random
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Import from extracted services
from codebot.process_manager import (
    BotConfig,
    BotState,
    start_bot,
    stop_bot,
    restart_bot,
    is_stuck,
    effective_heartbeat_timeout,
    model_profile,
    read_heartbeat,
    log_mtime,
    update_bot_state,
    checkpoint_path,
    read_checkpoint,
    STATE_DIR,
    LOGS_DIR,
    BOTS_DIR,
    BACKUP_DIR,
    GATEWAY_MAX_CONCURRENT,
    GATEWAY_MIN_SPAWN_GAP,
    ALWAYS_RESPAWN,
    write_heartbeat,
    heartbeat_path,
    _write_json_atomic,
)

from codebot.ticket_dispatcher import (
    spawn_demand_agents,
    dispatch_decompose_agents,
    dispatch_planning_agents,
    advance_reviewed_tickets,
    gatekeeper_verify_tickets,
    route_ready_tickets,
    process_rework_tickets,
    recover_deferred_tickets,
    _sweep_orphan_claims,
)

from codebot.alignment_service import (
    run_alignment_pipeline,
    run_alignment_pipeline_for_all,
)

from codebot.orchestrator_services import (
    IMPLEMENTER_ROLE_NAMES,
    DISCOVERY_ROLE_NAMES,
    REVIEWER_ROLE_NAMES,
    PLANNING_ROLE_NAMES,
    DECOMPOSER_ROLE_NAMES,
    RATE_LIMIT_REQUEUE_S,
    RATE_LIMIT_BACKOFF_MAX,
    RATE_LIMIT_DISABLE_AFTER,
    load_bot_registry,
    is_draining,
    set_drain,
    clear_drain,
    drain_status,
    check_self_restart,
    retry_disabled_bot,
    retry_stuck_starting,
    rotate_model_on_error,
    write_alignment_event,
    log_bot_statuses,
    get_pipeline_state,
    is_needed_bot,
    apply_agent_availability,
    get_status,
    print_status,
    safe_stop_all,
    backup_botnet,
    restore_botnet,
    ALIGNMENT_EVENTS_DIR,
)

# Re-export for backward compatibility
__all__ = [
    "BotConfig",
    "BotState",
    "start_bot",
    "stop_bot",
    "restart_bot",
    "is_stuck",
    "effective_heartbeat_timeout",
    "model_profile",
    "read_heartbeat",
    "log_mtime",
    "update_bot_state",
    "checkpoint_path",
    "read_checkpoint",
    "STATE_DIR",
    "LOGS_DIR",
    "BOTS_DIR",
    "BACKUP_DIR",
    "GATEWAY_MAX_CONCURRENT",
    "GATEWAY_MIN_SPAWN_GAP",
    "ALWAYS_RESPAWN",
    "write_heartbeat",
    "heartbeat_path",
    "_write_json_atomic",
    "is_draining",
    "set_drain",
    "clear_drain",
    "get_status",
    "print_status",
    "safe_stop_all",
    "backup_botnet",
    "restore_botnet",
    "drain_status",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "orchestrator.log"),
    ],
)
logger = logging.getLogger("orchestrator")

# ---------------------------------------------------------------------------
# Bot Registry Loading
# ---------------------------------------------------------------------------

BOT_REGISTRY = load_bot_registry()

# ---------------------------------------------------------------------------
# Main Health Check Loop
# ---------------------------------------------------------------------------

def check_all_bots(bots: dict[str, BotState]) -> None:
    """Main health check loop for all bots."""
    if check_self_restart(bots):
        return

    now = time.time()

    # Retry disabled/stuck bots
    for name, bot in bots.items():
        if not bot.config.enabled and bot.process is None:
            retry_disabled_bot(bot, bots)
        if bot.config.enabled and bot.process is not None:
            if retry_stuck_starting(bot, bots):
                logger.info(f"Retrying '{name}' stuck in starting")

    # Process exited bots
    for name, bot in list(bots.items()):
        if not bot.config.enabled:
            continue
        if bot.process is not None and bot.process.poll() is None:
            continue  # Still running
        if bot.process is None:
            continue  # Already handled

        exit_code = bot.process.returncode
        base_role = name.split("-")[0] if "-" in name else name
        assigned_tid = getattr(bot, '_assigned_ticket_id', '')

        # Write alignment event
        write_alignment_event(name, exit_code=exit_code, exit_reason="clean" if exit_code == 0 else "error", started_at=bot.started_at)

        # Run alignment pipeline
        try:
            run_alignment_pipeline(name)
        except Exception as e:
            logger.warning(f"Alignment pipeline failed for {name}: {e}")

        bot.process = None

        if exit_code == 0:
            bot.consecutive_errors = 0
            # Transition ticket if assigned
            if assigned_tid:
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
                                    logger.info(f"Ticket {assigned_tid} -> VERIFYING (reviewer {name} completed)")
                            else:
                                if t.state == TicketState.IMPLEMENTING:
                                    ts.transition(assigned_tid, TicketState.REVIEWING)
                                    logger.info(f"Ticket {assigned_tid} -> REVIEWING (agent {name} completed)")
                        # Clean up claims
                        claims_dir = STATE_DIR / "claims"
                        for cf in claims_dir.glob(f"{assigned_tid}.*.json"):
                            cf.unlink(missing_ok=True)
                except Exception as te:
                    logger.warning(f"Ticket transition failed for {assigned_tid}: {te}")
                bot._assigned_ticket_id = ''

            bot.next_run_at = now + bot.config.interval_seconds
            logger.info(f"Bot '{name}' completed cleanly — next run in {bot.config.interval_seconds}s")
            update_bot_state(bot, "waiting")

        elif exit_code == 3:  # Rate limited
            bot.consecutive_errors += 1
            backoff = min(RATE_LIMIT_REQUEUE_S * (2 ** (bot.consecutive_errors - 1)), RATE_LIMIT_BACKOFF_MAX)
            jitter = random.randint(0, min(60, backoff // 4))
            bot.next_run_at = now + backoff + jitter
            rotate_model_on_error(bot, bots)
            update_bot_state(bot, "waiting")
            if bot.consecutive_errors >= RATE_LIMIT_DISABLE_AFTER:
                logger.error(f"Bot '{name}' rate-limited {bot.consecutive_errors}x — disabling")
                bot.config.enabled = False
                update_bot_state(bot, "disabled")
            else:
                logger.info(f"Bot '{name}' rate-limited (exit 3) — backoff {backoff+jitter}s, attempt {bot.consecutive_errors}/{RATE_LIMIT_DISABLE_AFTER}")

        else:  # Error exit
            bot.consecutive_errors += 1
            if bot.consecutive_errors >= 3:
                old_model = bot.config.model
                rotate_model_on_error(bot, bots)
                bot.consecutive_errors = 0
                logger.warning(f"Bot '{name}' failed 3 times on {old_model} — rotated to {bot.config.model}")

            if assigned_tid:
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
                            logger.info(f"Bot '{name}' errored (exit {exit_code}), returning ticket {assigned_tid} to READY for retry")
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

            bot.next_run_at = now + 5
            update_bot_state(bot, "waiting")

    # Handle stuck bots
    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        alive = bot.process is not None and bot.process.poll() is None
        if not alive:
            continue
        if is_stuck(bot):
            eff = effective_heartbeat_timeout(bot)
            hb_age = now - read_heartbeat(bot.config.name) if read_heartbeat(bot.config.name) else 0
            log_age = now - log_mtime(bot.config.name) if log_mtime(bot.config.name) else 0
            prof = model_profile(bot.config.model)
            risk = prof.lockup_risk if prof else "unknown"
            logger.warning(f"Bot '{name}' stuck (hb {hb_age:.0f}s > {eff}s, log {log_age:.0f}s silent, risk={risk})")
            write_alignment_event(name, exit_code=None, exit_reason="stuck", started_at=bot.started_at)
            try:
                run_alignment_pipeline(name)
            except Exception as e:
                logger.warning(f"Alignment pipeline failed for {name}: {e}")
            ck = read_checkpoint(name)
            if ck:
                excess = hb_age - eff
                logger.info(f"Checkpoint handoff ready for '{name}': excess={excess:.0f}s")
            restart_bot(bot, reason="stuck", bots=bots)

    # Log statuses
    log_bot_statuses(bots)

    # Sweep orphan claims
    try:
        _sweep_orphan_claims(bots)
    except Exception:
        pass

    # Apply agent availability
    try:
        apply_agent_availability(bots)
    except Exception as e:
        logger.warning(f"Agent availability check failed: {e}")

    # Spawn demand agents
    try:
        spawn_demand_agents(bots, GATEWAY_MAX_CONCURRENT, start_bot_fn=start_bot)
    except Exception as e:
        logger.warning(f"Demand agent spawn failed: {e}")

    # Dispatch decompose/planning agents
    try:
        dispatch_decompose_agents(bots, start_bot_fn=start_bot)
    except Exception as e:
        logger.warning(f"Decompose dispatch failed: {e}")

    try:
        dispatch_planning_agents(bots, start_bot_fn=start_bot)
    except Exception as e:
        logger.warning(f"Planning dispatch failed: {e}")

    # Advance reviewed tickets
    try:
        advance_reviewed_tickets(bots)
    except Exception as e:
        logger.warning(f"Review advance failed: {e}")

    # Gatekeeper verify
    try:
        gatekeeper_verify_tickets()
    except Exception as e:
        logger.warning(f"Gatekeeper verify failed: {e}")

    # Route ready tickets
    try:
        route_ready_tickets()
    except Exception as e:
        logger.warning(f"Route ready tickets failed: {e}")

    # Process rework tickets
    try:
        process_rework_tickets(bots)
    except Exception as e:
        logger.warning(f"Process rework tickets failed: {e}")

    # Recover deferred tickets
    try:
        recover_deferred_tickets()
    except Exception as e:
        logger.warning(f"Recover deferred tickets failed: {e}")

    # Spawn interval-based bots
    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        if bot.process is not None and bot.process.poll() is None:
            continue  # Still running
        if bot.process is not None:
            continue  # Already handled above

        if is_draining():
            continue

        if bot.next_run_at and now < bot.next_run_at:
            continue

        pipeline = get_pipeline_state()
        if is_needed_bot(name, pipeline):
            logger.info(f"Bot '{name}' interval elapsed — respawning")
            start_bot(bot, bots=bots)
        else:
            bot.next_run_at = now + bot.config.interval_seconds
            update_bot_state(bot, "waiting")


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run orchestrator. Pure controller — no bot work here."""
    import argparse
    parser = argparse.ArgumentParser(description="Bot Orchestrator")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    parser.add_argument("--stop-all", action="store_true", help="Stop all bots and exit")
    parser.add_argument("--safe-stop", action="store_true", help="Graceful drain")
    parser.add_argument("--drain", action="store_true", help="Alias for --safe-stop")
    parser.add_argument("--clear-drain", action="store_true", help="Clear drain flag")
    parser.add_argument("--drain-status", action="store_true", help="Show drain status")
    parser.add_argument("--start", nargs="*", help="Start specific bots")
    parser.add_argument("--check-interval", type=int, default=30, help="Health check interval (seconds)")
    args = parser.parse_args()

    # Build bots dict
    bots: dict[str, BotState] = {}
    for config in BOT_REGISTRY:
        bot = BotState(config=config)
        state_file = STATE_DIR / f"{config.name}.state.json"
        if state_file.exists():
            try:
                sdata = json.loads(state_file.read_text())
                if isinstance(sdata, dict):
                    bot.consecutive_errors = sdata.get("consecutive_errors", 0)
                    bot.next_run_at = sdata.get("next_run_at", 0.0)
                    bot.restart_count = sdata.get("restart_count", 0)
            except Exception:
                pass
        bots[config.name] = bot

    if args.status:
        print_status(bots)
        if is_draining():
            print(f"DRAIN ACTIVE: {drain_status()}")
        return

    if args.drain_status:
        import pprint as _pp
        _pp.pprint(drain_status())
        print_status(bots)
        return

    if args.clear_drain:
        clear_drain()
        print("Drain cleared — respawn re-enabled.")
        return

    if args.safe_stop or args.drain:
        safe_stop_all(bots)
        print("Safe stop complete.")
        print_status(bots)
        return

    if args.stop_all:
        for bot in bots.values():
            stop_bot(bot, "stop-all")
        logger.info("All bots stopped")
        return

    if args.start is not None:
        if is_draining():
            print(f"Refusing --start while drain active", file=sys.stderr)
            sys.exit(4)
        targets = args.start if args.start else [c.name for c in BOT_REGISTRY]
        for name in targets:
            if name in bots and bots[name].config.enabled:
                start_bot(bots[name])
        if args.start:
            time.sleep(2)
            print_status(bots)
            return

    # Bootstrap adapter if available
    _codebot_adapter = None
    try:
        from codebot.codebot_bootstrap import bootstrap as _cb_bootstrap
        _codebot_adapter = _cb_bootstrap(BOTS_DIR)
        if _codebot_adapter:
            logger.info("CodeBot core bootstrapped: project=%s", _codebot_adapter.project_name())
    except ImportError:
        pass
    except Exception as _cb_err:
        logger.warning("CodeBot bootstrap failed (continuing with legacy): %s", _cb_err)

    # Reload registry if adapter was loaded
    if _codebot_adapter:
        global BOT_REGISTRY
        from codebot.orchestrator_services import set_adapter_instance
        set_adapter_instance(_codebot_adapter)
        BOT_REGISTRY = load_bot_registry()
        bots = {}
        for config in BOT_REGISTRY:
            bot = BotState(config=config)
            state_file = STATE_DIR / f"{config.name}.state.json"
            if state_file.exists():
                try:
                    sdata = json.loads(state_file.read_text())
                    if isinstance(sdata, dict):
                        bot.consecutive_errors = sdata.get("consecutive_errors", 0)
                        bot.next_run_at = sdata.get("next_run_at", 0.0)
                        bot.restart_count = sdata.get("restart_count", 0)
                except Exception:
                    pass
            bots[config.name] = bot

    def shutdown_handler(signum, frame):
        logger.info("Shutdown signal received")
        for bot in bots.values():
            stop_bot(bot, "shutdown")
        logger.info("Orchestrator stopped")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    if is_draining():
        logger.warning(f"Starting with drain active")
    else:
        for bot in bots.values():
            bot._assigned_ticket_id = ''
        apply_agent_availability(bots)
        pipeline = get_pipeline_state()
        logger.info(f"Overture: pipeline {pipeline}")

    logger.info("Orchestrator starting")
    logger.info(f"Health check every {args.check_interval}s")
    last_alignment_run = time.time()
    alignment_interval = 1800  # 30 minutes

    while True:
        try:
            check_all_bots(bots)
            time.sleep(args.check_interval)
            if time.time() - last_alignment_run >= alignment_interval:
                run_alignment_pipeline_for_all()
                last_alignment_run = time.time()
        except KeyboardInterrupt:
            shutdown_handler(None, None)
        except Exception as e:
            logger.error(f"Health check error: {e}")
            time.sleep(args.check_interval)


if __name__ == "__main__":
    main()
