#!/usr/bin/env python3
"""Bot Orchestrator — Thin Controller.

Purpose
-------
Coordinates bot lifecycle and ticket dispatching by delegating to
process_manager, ticket_dispatcher, and dispatch_service modules.

The orchestrator is a pure process manager that spawns bot subprocesses
and monitors their health. It does NOT contain business logic for
dispatching, alignment, or lease management.

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
import signal
import sys
import time
from dataclasses import dataclass
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

from codebot.dispatch_service import (
    get_pipeline_state,
    is_needed_bot,
    apply_agent_availability,
    rotate_model_on_error,
    transition_ticket_on_success,
    transition_ticket_on_error,
    compute_rate_limit_backoff,
    retry_disabled_bot,
    retry_stuck_starting,
    log_bot_statuses,
)

# Re-export for backward compatibility
__all__ = [
    "BotConfig", "BotState", "start_bot", "stop_bot", "restart_bot",
    "is_stuck", "effective_heartbeat_timeout", "model_profile",
    "read_heartbeat", "log_mtime", "update_bot_state", "checkpoint_path",
    "read_checkpoint", "STATE_DIR", "LOGS_DIR", "BOTS_DIR", "BACKUP_DIR",
    "GATEWAY_MAX_CONCURRENT", "GATEWAY_MIN_SPAWN_GAP", "ALWAYS_RESPAWN",
    "write_heartbeat", "heartbeat_path", "_write_json_atomic",
    "is_draining", "set_drain", "clear_drain", "get_status", "print_status",
    "safe_stop_all", "backup_botnet", "restore_botnet", "drain_status",
]

# ---------------------------------------------------------------------------
# Paths & Config
# ---------------------------------------------------------------------------

_project_root = Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))
_paths_state_dir = _project_root / ".codebot" / "state"
_paths_logs_dir = _project_root / ".codebot" / "logs"
_paths_backup_dir = _paths_state_dir / "backup"
_paths_drain_file = _paths_state_dir / ".drain"
_paths_update_lock = _paths_state_dir / ".update_lock"
_paths_restart_file = _paths_state_dir / ".restart"

@dataclass
class _PathsCompat:
    state_dir: Path
    logs_dir: Path
    backup_dir: Path
    drain_file: Path
    update_lock: Path
    restart_file: Path
    alignment_events_dir: Path

_paths = _PathsCompat(
    state_dir=_paths_state_dir, logs_dir=_paths_logs_dir,
    backup_dir=_paths_backup_dir, drain_file=_paths_drain_file,
    update_lock=_paths_update_lock, restart_file=_paths_restart_file,
    alignment_events_dir=_paths_state_dir / "alignment_events",
)
ALIGNMENT_EVENTS_DIR = _paths.alignment_events_dir

_paths_state_dir.mkdir(parents=True, exist_ok=True)
_paths_logs_dir.mkdir(parents=True, exist_ok=True)
_paths_backup_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.FileHandler(_paths_logs_dir / "orchestrator.log")],
)
logger = logging.getLogger("orchestrator")

_adapter_instance: Any = None


def _load_bot_registry() -> list[BotConfig]:
    """Load bot registry from ProjectAdapter if available, else use minimal defaults."""
    if _adapter_instance is not None:
        try:
            entries = _adapter_instance.bot_registry()
            configs = []
            for e in entries:
                configs.append(BotConfig(
                    name=e["name"], prompt_file=e.get("prompt", f"{e['name']}.md"),
                    interval_seconds=e.get("interval", 600),
                    heartbeat_timeout=e.get("interval", 600) * 2,
                    model=e.get("model", "default"), fallback_model=e.get("fallback_model", ""),
                    enabled=e.get("enabled", True), max_restarts=e.get("max_restarts", 5),
                    clean_exit_wait=e.get("clean_exit_wait", True), tier=e.get("tier", 2),
                    runner_mode=e.get("runner_mode", "api"),
                    max_tokens_per_run=e.get("max_tokens_per_run", 0),
                ))
            if configs:
                logger.info("Loaded %d roles from project adapter", len(configs))
                return configs
        except Exception as e:
            logger.warning("Failed to load registry from adapter: %s", e)
    return [
        BotConfig("discovery", "bug_hunter.md", 1800, 3600, "default", clean_exit_wait=True),
        BotConfig("implementer", "general_implementer.md", 300, 750, "default", clean_exit_wait=True),
        BotConfig("reviewer", "correctness_reviewer.md", 600, 1500, "default", clean_exit_wait=True),
    ]

BOT_REGISTRY = _load_bot_registry()


# ---------------------------------------------------------------------------
# Drain & Restart
# ---------------------------------------------------------------------------

def is_draining() -> bool:
    return _paths_drain_file.exists()


def _check_self_restart(bots: dict[str, BotState]) -> bool:
    if not _paths_restart_file.exists():
        return False
    try:
        reason = _paths_restart_file.read_text(encoding="utf-8").strip() or "manual"
    except OSError:
        reason = "manual"
    logger.info(f"Self-restart signal detected (reason: {reason}) — draining and restarting")
    for bot in bots.values():
        if bot.process is not None and bot.process.poll() is None:
            stop_bot(bot, "self-restart")
    try:
        _paths_restart_file.unlink(missing_ok=True)
    except OSError:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)
    return True


def _write_alignment_event(bot_name: str, exit_code: int | None, exit_reason: str, started_at: float | None = None) -> None:
    try:
        from codebot.alignment_service import _write_alignment_event as _wae
        _wae(bot_name, exit_code, exit_reason, started_at)
    except Exception as e:
        logger.warning(f"Failed to write alignment event for {bot_name}: {e}")


# ---------------------------------------------------------------------------
# Main Health Check Loop
# ---------------------------------------------------------------------------

def check_all_bots(bots: dict[str, BotState]) -> None:
    """Main health check loop for all bots. Delegates business logic to services."""
    if _check_self_restart(bots):
        return
    now = time.time()

    # Retry disabled/stuck bots
    for name, bot in bots.items():
        if not bot.config.enabled and bot.process is None:
            if retry_disabled_bot(bot):
                update_bot_state(bot, "waiting")
        if bot.config.enabled and bot.process is not None:
            if retry_stuck_starting(bot):
                logger.info(f"Retrying '{name}' stuck in starting")

    # Process exited bots
    for name, bot in list(bots.items()):
        if not bot.config.enabled:
            continue
        if bot.process is None or bot.process.poll() is None:
            continue

        exit_code = bot.process.returncode
        _write_alignment_event(name, exit_code=exit_code,
                               exit_reason="clean" if exit_code == 0 else "error",
                               started_at=bot.started_at)
        try:
            run_alignment_pipeline(name)
        except Exception as e:
            logger.warning(f"Alignment pipeline failed for {name}: {e}")
        bot.process = None

        if exit_code == 0:
            bot.consecutive_errors = 0
            transition_ticket_on_success(bot, bots)
            bot.next_run_at = now + bot.config.interval_seconds
            update_bot_state(bot, "waiting")
            logger.info(f"Bot '{name}' completed cleanly — next run in {bot.config.interval_seconds}s")

        elif exit_code == 3:  # Rate limited
            backoff, should_disable = compute_rate_limit_backoff(bot)
            bot.next_run_at = now + backoff
            rotate_model_on_error(bot, bots)
            if should_disable:
                logger.error(f"Bot '{name}' rate-limited {bot.consecutive_errors}x — disabling")
                bot.config.enabled = False
                update_bot_state(bot, "disabled")
            else:
                update_bot_state(bot, "waiting")
                logger.info(f"Bot '{name}' rate-limited (exit 3) — backoff {backoff:.0f}s")

        else:  # Error exit
            bot.consecutive_errors += 1
            if bot.consecutive_errors >= 3:
                old_model = bot.config.model
                rotate_model_on_error(bot, bots)
                bot.consecutive_errors = 0
                logger.warning(f"Bot '{name}' failed 3 times on {old_model} — rotated to {bot.config.model}")
            transition_ticket_on_error(bot, bots, exit_code)
            bot.next_run_at = now + 5
            update_bot_state(bot, "waiting")

    # Handle stuck bots
    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        if bot.process is None or bot.process.poll() is not None:
            continue
        if is_stuck(bot):
            eff = effective_heartbeat_timeout(bot)
            hb_age = now - read_heartbeat(bot.config.name) if read_heartbeat(bot.config.name) else 0
            prof = model_profile(bot.config.model)
            risk = prof.lockup_risk if prof else "unknown"
            logger.warning(f"Bot '{name}' stuck (hb {hb_age:.0f}s > {eff}s, risk={risk})")
            _write_alignment_event(name, exit_code=None, exit_reason="stuck", started_at=bot.started_at)
            try:
                run_alignment_pipeline(name)
            except Exception as e:
                logger.warning(f"Alignment pipeline failed for {name}: {e}")
            ck = read_checkpoint(name)
            if ck:
                logger.info(f"Checkpoint handoff ready for '{name}': excess={hb_age - eff:.0f}s")
            restart_bot(bot, reason="stuck", bots=bots)

    # Delegate service calls
    log_bot_statuses(bots)
    try:
        _sweep_orphan_claims(bots)
    except Exception:
        pass
    try:
        apply_agent_availability(bots, stop_fn=stop_bot, update_state_fn=update_bot_state)
    except Exception as e:
        logger.warning(f"Agent availability check failed: {e}")
    try:
        spawn_demand_agents(bots, GATEWAY_MAX_CONCURRENT, start_bot_fn=start_bot)
    except Exception as e:
        logger.warning(f"Demand agent spawn failed: {e}")
    try:
        dispatch_decompose_agents(bots, start_bot_fn=start_bot)
    except Exception as e:
        logger.warning(f"Decompose dispatch failed: {e}")
    try:
        dispatch_planning_agents(bots, start_bot_fn=start_bot)
    except Exception as e:
        logger.warning(f"Planning dispatch failed: {e}")
    try:
        advance_reviewed_tickets(bots)
    except Exception as e:
        logger.warning(f"Review advance failed: {e}")
    try:
        gatekeeper_verify_tickets()
    except Exception as e:
        logger.warning(f"Gatekeeper verify failed: {e}")
    try:
        route_ready_tickets()
    except Exception as e:
        logger.warning(f"Route ready tickets failed: {e}")
    try:
        process_rework_tickets(bots)
    except Exception as e:
        logger.warning(f"Process rework tickets failed: {e}")
    try:
        recover_deferred_tickets()
    except Exception as e:
        logger.warning(f"Recover deferred tickets failed: {e}")

    # Spawn interval-based bots
    pipeline = get_pipeline_state()
    for name, bot in bots.items():
        if not bot.config.enabled or is_draining():
            continue
        if bot.process is not None:
            continue
        if bot.next_run_at and now < bot.next_run_at:
            continue
        if is_needed_bot(name, pipeline):
            logger.info(f"Bot '{name}' interval elapsed — respawning")
            start_bot(bot, bots=bots)
        else:
            bot.next_run_at = now + bot.config.interval_seconds
            update_bot_state(bot, "waiting")


# ---------------------------------------------------------------------------
# Status & Control
# ---------------------------------------------------------------------------

def get_status(bots: dict[str, BotState]) -> dict:
    status = {}
    for name, bot in bots.items():
        process_pid = bot.process.pid if bot.process is not None and bot.process.poll() is None else None
        running = process_pid is not None
        hb_val = read_heartbeat(bot.config.name)
        hb_age = time.time() - hb_val if hb_val > 0 else None
        if hb_age is not None and hb_age < 0:
            hb_age = 0.0
        prof = model_profile(bot.config.model)
        status[name] = {
            "enabled": bot.config.enabled, "running": running, "pid": process_pid,
            "model": bot.config.model, "risk": prof.lockup_risk if prof else "unknown",
            "eff_timeout": effective_heartbeat_timeout(bot),
            "heartbeat_age_seconds": round(hb_age, 1) if hb_age else None,
            "next_run_in": round(bot.next_run_at - time.time(), 1) if bot.next_run_at and not running else None,
            "restart_count": bot.restart_count, "consecutive_errors": bot.consecutive_errors,
        }
    return status


def print_status(bots: dict[str, BotState]) -> None:
    status = get_status(bots)
    print("\n" + "=" * 90)
    print("BOT ORCHESTRATOR STATUS")
    print("=" * 90)
    for name, info in status.items():
        state = "RUNNING" if info["running"] else ("WAITING" if info["next_run_in"] and info["next_run_in"] > 0 else "STOPPED")
        if not info["enabled"]:
            state = "DISABLED"
        hb = f"{info['heartbeat_age_seconds']}s" if info["heartbeat_age_seconds"] else "-"
        nxt = f"{info['next_run_in']:.0f}s" if info["next_run_in"] and info["next_run_in"] > 0 else "-"
        pid = str(info['pid']) if info['pid'] else "-"
        print(f"  {name:15s} {state:9s} PID={pid:6s} HB={hb:7s} NEXT={nxt:6s} eff={info['eff_timeout']:4.0f}s risk={info['risk']:11s} {info['model']}")
    print("=" * 90 + "\n")


def set_drain(reason: str = "") -> None:
    _paths_drain_file.write_text(f"{time.time()}\n{reason}\n")
    logger.info(f"Drain flag set: {reason}")


def clear_drain() -> None:
    for f in (_paths_drain_file, _paths_update_lock):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    logger.info("Drain cleared")


def drain_status() -> dict:
    return {
        "draining": is_draining(),
        "drain_file": str(_paths_drain_file) if _paths_drain_file.exists() else None,
        "update_lock": str(_paths_update_lock) if _paths_update_lock.exists() else None,
        "drain_reason": _paths_drain_file.read_text().strip() if _paths_drain_file.exists() else None,
    }


def safe_stop_all(bots: dict[str, BotState]) -> dict:
    set_drain("safe-stop requested")
    results: dict[str, str] = {}
    for name, bot in bots.items():
        if bot.process is None or bot.process.poll() is not None:
            results[name] = "already stopped"
            update_bot_state(bot, "stopped")
            continue
        logger.info(f"Safe-stopping {name}")
        stop_bot(bot, reason="safe-stop drain")
        results[name] = "stopped"
        update_bot_state(bot, "drained")
    logger.info(f"Safe stop complete: {results}")
    return results


def backup_botnet(tag: str | None = None) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"botnet-{tag}-{ts}" if tag else f"botnet-{ts}"
    dest = _paths_backup_dir / name
    dest.mkdir(parents=True, exist_ok=True)
    for p in BOTS_DIR.glob("*.md"):
        (dest / p.name).write_bytes(p.read_bytes())
    logger.info(f"Botnet backup -> {dest}")
    return dest


def restore_botnet(backup_dir: Path) -> None:
    if not backup_dir.exists():
        raise FileNotFoundError(str(backup_dir))
    for p in backup_dir.glob("*.md"):
        (BOTS_DIR / p.name).write_bytes(p.read_bytes())
    logger.info(f"Restored botnet from {backup_dir}")


# ---------------------------------------------------------------------------
# Main Entry Point
# ---------------------------------------------------------------------------

def _build_bots(registry: list[BotConfig]) -> dict[str, BotState]:
    """Build BotState dict from registry, restoring persisted state."""
    bots: dict[str, BotState] = {}
    for config in registry:
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
    return bots


def main() -> None:
    """Run orchestrator. Pure controller — no bot work here."""
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
    bots = _build_bots(BOT_REGISTRY)

    if args.status:
        print_status(bots)
        if is_draining(): print(f"DRAIN ACTIVE: {drain_status()}")
        return
    if args.drain_status:
        import pprint; pprint.pprint(drain_status()); print_status(bots); return
    if args.clear_drain:
        clear_drain(); print("Drain cleared."); return
    if args.safe_stop or args.drain:
        safe_stop_all(bots); print("Safe stop complete."); print_status(bots); return
    if args.stop_all:
        for b in bots.values(): stop_bot(b, "stop-all")
        logger.info("All bots stopped"); return
    if args.start is not None:
        if is_draining(): print("Refusing --start while draining", file=sys.stderr); sys.exit(4)
        for n in (args.start or [c.name for c in BOT_REGISTRY]):
            if n in bots and bots[n].config.enabled: start_bot(bots[n])
        if args.start: time.sleep(2); print_status(bots)
        return

    try:
        from codebot.codebot_bootstrap import bootstrap as _cb
        adapter = _cb(BOTS_DIR)
        if adapter: logger.info("Bootstrapped: %s", adapter.project_name())
    except ImportError: adapter = None
    except Exception as e: logger.warning("Bootstrap failed: %s", e); adapter = None

    if adapter:
        global BOT_REGISTRY
        BOT_REGISTRY = _load_bot_registry()
        bots = _build_bots(BOT_REGISTRY)

    def shutdown_handler(signum, frame):
        logger.info("Shutdown signal received")
        for b in bots.values(): stop_bot(b, "shutdown")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    if not is_draining():
        for b in bots.values(): b._assigned_ticket_id = ''
        apply_agent_availability(bots, stop_fn=stop_bot, update_state_fn=update_bot_state)
        logger.info(f"Overture: pipeline {get_pipeline_state()}")
    else:
        logger.warning("Starting with drain active")

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
