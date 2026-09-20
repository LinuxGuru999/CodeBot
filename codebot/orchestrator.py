#!/usr/bin/env python3
"""Bot Orchestrator — Thin Controller.

Purpose
-------
Coordinates bot lifecycle and ticket dispatching by delegating to
process_manager and ticket_dispatcher services.

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
import subprocess
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

# Backward-compatible _paths object for tests
from dataclasses import dataclass
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
    state_dir=_paths_state_dir,
    logs_dir=_paths_logs_dir,
    backup_dir=_paths_backup_dir,
    drain_file=_paths_drain_file,
    update_lock=_paths_update_lock,
    restart_file=_paths_restart_file,
    alignment_events_dir=_paths_state_dir / "alignment_events",
)

# Re-export ALIGNMENT_EVENTS_DIR for backward compatibility
ALIGNMENT_EVENTS_DIR = _paths.alignment_events_dir

# Ensure directories exist
_paths_state_dir.mkdir(parents=True, exist_ok=True)
_paths_logs_dir.mkdir(parents=True, exist_ok=True)
_paths_backup_dir.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(_paths_logs_dir / "orchestrator.log"),
    ],
)
logger = logging.getLogger("orchestrator")

# ---------------------------------------------------------------------------
# Role Names
# ---------------------------------------------------------------------------

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

MAX_DISCOVERY_NO_TICKET_RUNS = 10
RATE_LIMIT_REQUEUE_S = int(os.getenv("CODEBOT_RATE_LIMIT_REQUEUE_S", "300"))
RATE_LIMIT_BACKOFF_MAX = int(os.getenv("CODEBOT_RATE_LIMIT_BACKOFF_MAX", "3600"))
RATE_LIMIT_DISABLE_AFTER = int(os.getenv("CODEBOT_RATE_LIMIT_DISABLE_AFTER", "20"))

USE_MANIFEST_SCHEDULER = os.getenv("CODEBOT_MANIFEST_SCHEDULER", "0") == "1"

# ---------------------------------------------------------------------------
# Bot Registry Loading
# ---------------------------------------------------------------------------

_adapter_instance: Any = None


def _load_bot_registry() -> list[BotConfig]:
    """Load bot registry from ProjectAdapter if available, else use minimal defaults."""
    if _adapter_instance is not None:
        try:
            entries = _adapter_instance.bot_registry()
            configs = []
            for e in entries:
                configs.append(BotConfig(
                    name=e["name"],
                    prompt_file=e.get("prompt", f"{e['name']}.md"),
                    interval_seconds=e.get("interval", 600),
                    heartbeat_timeout=e.get("interval", 600) * 2,
                    model=e.get("model", "default"),
                    fallback_model=e.get("fallback_model", ""),
                    enabled=e.get("enabled", True),
                    max_restarts=e.get("max_restarts", 5),
                    clean_exit_wait=e.get("clean_exit_wait", True),
                    tier=e.get("tier", 2),
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
    python = sys.executable
    args = [python] + sys.argv
    logger.info(f"Executing self-restart: {' '.join(args)}")
    os.execv(python, args)
    return True


# ---------------------------------------------------------------------------
# Health Check Helpers
# ---------------------------------------------------------------------------

def _retry_disabled_bot(bot: BotState, bots: dict[str, BotState]) -> bool:
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
                    update_bot_state(bot, "waiting")
                    return True
    except Exception:
        pass
    return False


def _retry_stuck_starting(bot: BotState, bots: dict[str, BotState]) -> bool:
    if bot.process is not None and bot.process.poll() is not None:
        return True
    last_hb = read_heartbeat(bot.config.name)
    if last_hb > 0:
        if time.time() - last_hb > 120.0:
            return True
        return False
    elapsed = time.time() - (bot.started_at or bot.last_heartbeat or time.time())
    return elapsed > 120.0


def _rotate_model_on_error(bot: BotState, bots: dict[str, BotState] | None = None) -> str:
    """Simple model rotation for error recovery."""
    all_models = [
        "xiaomi-mimo-2.5", "qwen-3.5-plus", "qwen-3.6-plus", "qwen-3.7-plus",
        "qwen-3.7-max", "qwen-3.8-max",
    ]
    failed_model = bot.config.model
    candidates = [m for m in all_models if m != failed_model]
    if not candidates:
        return failed_model
    new_model = candidates[0]
    old_model = bot.config.model
    bot.config.model = new_model
    bot.config.fallback_model = "xiaomi-mimo-2.5"
    logger.info(f"Model rotation for '{bot.config.name}': {old_model} -> {new_model}")
    return new_model


def _write_alignment_event(bot_name: str, exit_code: int | None, exit_reason: str, started_at: float | None = None) -> None:
    """Write alignment event for RL scoring."""
    try:
        from codebot.alignment_service import _write_alignment_event as _wae
        _wae(bot_name, exit_code, exit_reason, started_at)
    except Exception as e:
        logger.warning(f"Failed to write alignment event for {bot_name}: {e}")


def _log_bot_statuses(bots: dict[str, BotState]) -> None:
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


def _get_pipeline_state() -> dict[str, int]:
    """Get current pipeline state counts."""
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


def _is_needed_bot(name: str, pipeline: dict[str, int]) -> bool:
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


def _apply_agent_availability(bots: dict[str, BotState]) -> None:
    """Enable or suppress bot spawns based on current ticket queue state."""
    pipeline = _get_pipeline_state()
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
                try:
                    bot.process.terminate()
                    bot.process.wait(timeout=5)
                except Exception:
                    try:
                        bot.process.kill()
                    except Exception:
                        pass
                bot.process = None
            update_bot_state(bot, "disabled")
        elif should_enable and not bot.config.enabled:
            bot.config.enabled = True
            logger.info(f"[queue-scale] Re-enabled '{name}': work available in queue")


# ---------------------------------------------------------------------------
# Main Health Check Loop
# ---------------------------------------------------------------------------

def check_all_bots(bots: dict[str, BotState]) -> None:
    """Main health check loop for all bots."""
    if _check_self_restart(bots):
        return

    now = time.time()

    # Retry disabled/stuck bots
    for name, bot in bots.items():
        if not bot.config.enabled and bot.process is None:
            _retry_disabled_bot(bot, bots)
        if bot.config.enabled and bot.process is not None:
            if _retry_stuck_starting(bot, bots):
                logger.info(f"Retrying '{name}' stuck in starting")

    # Handle exited bots
    for name, bot in list(bots.items()):
        if not bot.config.enabled:
            continue
        if bot.process is None or bot.process.poll() is not None:
            continue
        # Bot is still running, skip
        pass

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
        _write_alignment_event(name, exit_code=exit_code, exit_reason="clean" if exit_code == 0 else "error", started_at=bot.started_at)

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

            if bot.config.clean_exit_wait:
                bot.next_run_at = now + bot.config.interval_seconds
                logger.info(f"Bot '{name}' completed cleanly — next run in {bot.config.interval_seconds}s")
                update_bot_state(bot, "waiting")
            else:
                bot.next_run_at = now + bot.config.interval_seconds
                update_bot_state(bot, "waiting")

        elif exit_code == 3:  # Rate limited
            bot.consecutive_errors += 1
            backoff = min(RATE_LIMIT_REQUEUE_S * (2 ** (bot.consecutive_errors - 1)), RATE_LIMIT_BACKOFF_MAX)
            import random
            jitter = random.randint(0, min(60, backoff // 4))
            bot.next_run_at = now + backoff + jitter
            _rotate_model_on_error(bot, bots)
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
                _rotate_model_on_error(bot, bots)
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
            _write_alignment_event(name, exit_code=None, exit_reason="stuck", started_at=bot.started_at)
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
    _log_bot_statuses(bots)

    # Sweep orphan claims
    try:
        _sweep_orphan_claims(bots)
    except Exception:
        pass

    # Apply agent availability
    try:
        _apply_agent_availability(bots)
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

        pipeline = _get_pipeline_state()
        if _is_needed_bot(name, pipeline):
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
        hb_age = time.time() - read_heartbeat(bot.config.name) if read_heartbeat(bot.config.name) > 0 else None
        if hb_age is not None and hb_age < 0:
            hb_age = 0.0
        prof = model_profile(bot.config.model)
        status[name] = {
            "enabled": bot.config.enabled,
            "running": running,
            "pid": process_pid,
            "model": bot.config.model,
            "risk": prof.lockup_risk if prof else "unknown",
            "eff_timeout": effective_heartbeat_timeout(bot),
            "heartbeat_age_seconds": round(hb_age, 1) if hb_age else None,
            "next_run_in": round(bot.next_run_at - time.time(), 1) if bot.next_run_at and not running else None,
            "restart_count": bot.restart_count,
            "consecutive_errors": bot.consecutive_errors,
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
        hb = f"{info['heartbeat_age_seconds']}s" if info['heartbeat_age_seconds'] else "-"
        nxt = f"{info['next_run_in']:.0f}s" if info['next_run_in'] and info['next_run_in'] > 0 else "-"
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
        BOT_REGISTRY = _load_bot_registry()
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
        _apply_agent_availability(bots)
        pipeline = _get_pipeline_state()
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
