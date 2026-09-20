#!/usr/bin/env python3
"""Bot Orchestrator — Thin Controller.

Purpose
-------
Coordinates bot lifecycle and ticket dispatching by delegating to
process_manager, ticket_dispatcher, dispatch_service, and alignment_service.

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

from codebot.process_manager import (
    BotConfig, BotState, ModelProfile, MODEL_PROFILES,
    start_bot, stop_bot, restart_bot,
    is_stuck, is_log_stalled, effective_heartbeat_timeout, model_profile,
    read_heartbeat, log_mtime, update_bot_state,
    checkpoint_path, read_checkpoint, STATE_DIR, LOGS_DIR,
    BOTS_DIR, GATEWAY_MAX_CONCURRENT,
    GATEWAY_MIN_SPAWN_GAP, ALWAYS_RESPAWN, write_heartbeat,
    heartbeat_path, _write_json_atomic,
)
BACKUP_DIR = STATE_DIR / "backup"

from codebot.ticket_dispatcher import (
    spawn_demand_agents, dispatch_decompose_agents,
    dispatch_planning_agents, advance_reviewed_tickets,
    gatekeeper_verify_tickets, route_ready_tickets,
    process_rework_tickets, recover_deferred_tickets,
    _sweep_orphan_claims,
)
from codebot.alignment_service import (
    run_alignment_pipeline, run_alignment_pipeline_for_all,
)
from codebot.dispatch_service import (
    get_pipeline_state, is_needed_bot, apply_agent_availability,
    rotate_model_on_error, transition_ticket_on_success,
    transition_ticket_on_error, compute_rate_limit_backoff,
    retry_disabled_bot, retry_stuck_starting, log_bot_statuses,
)
from codebot.scratchpad import load_scratchpad, save_scratchpad

__all__ = [
    "BotConfig", "BotState", "ModelProfile", "MODEL_PROFILES",
    "start_bot", "stop_bot", "restart_bot",
    "is_stuck", "is_log_stalled", "effective_heartbeat_timeout", "model_profile",
    "read_heartbeat", "log_mtime", "update_bot_state", "checkpoint_path",
    "read_checkpoint", "STATE_DIR", "LOGS_DIR", "BOTS_DIR", "BACKUP_DIR",
    "GATEWAY_MAX_CONCURRENT", "GATEWAY_MIN_SPAWN_GAP", "ALWAYS_RESPAWN",
    "write_heartbeat", "heartbeat_path", "_write_json_atomic",
    "is_draining", "set_drain", "clear_drain", "get_status", "print_status",
    "safe_stop_all", "backup_botnet", "restore_botnet", "drain_status",
    "PathConfig", "set_project_adapter",
]

_project_root = Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))
_paths_state_dir = _project_root / ".codebot" / "state"
_paths_logs_dir = _project_root / ".codebot" / "logs"
_paths_backup_dir = _paths_state_dir / "backup"
_paths_drain_file = _paths_state_dir / ".drain"
_paths_update_lock = _paths_state_dir / ".update_lock"
_paths_restart_file = _paths_state_dir / ".restart"

@dataclass
class PathConfig:
    bots_dir: Path
    state_dir: Path
    logs_dir: Path
    backup_dir: Path
    alignment_events_dir: Path
    drain_file: Path
    update_lock: Path
    restart_file: Path

_PathsCompat = PathConfig

_paths = PathConfig(
    bots_dir=BOTS_DIR,
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

def set_adapter_instance(adapter: Any) -> None:
    global _adapter_instance
    _adapter_instance = adapter

def set_project_adapter(adapter: Any) -> None:
    """Update paths from a project adapter without using global path variables."""
    set_adapter_instance(adapter)
    try:
        p = adapter.paths()
        _paths.bots_dir = getattr(p, 'repository_root', _paths.bots_dir)
        _paths.state_dir = getattr(p, 'state_dir', _paths.state_dir)
        _paths.logs_dir = getattr(p, 'logs_dir', _paths.logs_dir)
        _paths.backup_dir = _paths.state_dir / "backup"
        _paths.drain_file = _paths.state_dir / ".drain"
        _paths.update_lock = _paths.state_dir / ".update_lock"
        _paths.restart_file = _paths.state_dir / ".restart"
        _paths.alignment_events_dir = _paths.state_dir / "alignment_events"
    except Exception as e:
        logger.warning("Failed to update paths from adapter: %s", e)

_COMPAT_PATHS = {"STATE_DIR": "state_dir", "LOGS_DIR": "logs_dir", "BOTS_DIR": "bots_dir",
                 "BACKUP_DIR": "backup_dir", "ALIGNMENT_EVENTS_DIR": "alignment_events_dir"}

def __getattr__(name: str) -> Any:
    if name in _COMPAT_PATHS:
        return getattr(_paths, _COMPAT_PATHS[name])
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

# ---------------------------------------------------------------------------
# Backward Compatibility Stubs
# ---------------------------------------------------------------------------

def _manifest_error_disabled(bot_name: str) -> bool:
    """Check if a bot manifest indicates disabled status."""
    state_file = STATE_DIR / f"{bot_name}.state.json"
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text())
            return data.get("status") == "disabled"
    except Exception:
        pass
    return False

def is_error_disabled(bot_name: str) -> bool:
    """Check if a bot is disabled due to errors."""
    return _manifest_error_disabled(bot_name)

def _manifest_restart_budget_exceeded(bot_name: str) -> bool:
    """Check if restart budget is exceeded based on state file."""
    state_file = STATE_DIR / f"{bot_name}.state.json"
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text())
            restart_count = data.get("restart_count", 0)
            max_restarts = 5
            return restart_count >= max_restarts
    except Exception:
        pass
    return False

def is_restart_budget_exceeded(bot_name: str) -> bool:
    """Check if restart budget is exceeded for a bot."""
    return _manifest_restart_budget_exceeded(bot_name)

def rotating_slots(max_concurrent: int = 26) -> int:
    """Return number of rotating slots available."""
    try:
        from codebot.process_manager import _count_api_runner_processes
        running = _count_api_runner_processes()
        return max(0, max_concurrent - running)
    except Exception:
        return 0

def worker_reserved_slots() -> int:
    """Return number of slots reserved for workers."""
    return 2

def _get_available_memory_mb() -> float:
    """Return available system memory in MB."""
    try:
        meminfo = Path("/proc/meminfo").read_text()
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                parts = line.split()
                return float(parts[1]) / 1024
    except Exception:
        pass
    return 0.0

def _model_tier_for_complexity(model: str, complexity: str, queue_has_tier_work: bool = False) -> bool:
    """Check if model is appropriate for given complexity tier."""
    cheap_models = frozenset({"xiaomi-mimo-2.5"})
    expensive_models = frozenset({"qwen-3.8-max", "qwen-3.8-max-thinking", "qwen-3.7-max", "qwen-3.7-max-thinking"})
    if complexity in ("trivial", "small", "medium"):
        return True
    if complexity == "high":
        return model in expensive_models or model not in cheap_models
    if complexity == "critical":
        return model in expensive_models
    return True

CLAIM_TTL_SECONDS = 300
MIN_ROTATING_SLOTS = 4

def is_manifest_error_disabled(bot_name: str) -> bool:
    return is_error_disabled(bot_name)

def is_manifest_restart_budget_exceeded(bot_name: str) -> bool:
    return is_restart_budget_exceeded(bot_name)

def _read_state_file(bot_name: str) -> dict:
    state_file = STATE_DIR / f"{bot_name}.state.json"
    try:
        if state_file.exists():
            return json.loads(state_file.read_text())
    except Exception:
        pass
    return {}

__all__.extend([
    "ModelProfile", "MODEL_PROFILES", "is_log_stalled",
    "is_error_disabled", "is_restart_budget_exceeded",
    "rotating_slots", "worker_reserved_slots",
    "_get_available_memory_mb", "_model_tier_for_complexity",
    "is_manifest_error_disabled", "is_manifest_restart_budget_exceeded",
    "CLAIM_TTL_SECONDS", "MIN_ROTATING_SLOTS",
    "_read_state_file",
])

def _load_bot_registry() -> list[BotConfig]:
    if _adapter_instance is not None:
        try:
            entries = _adapter_instance.bot_registry()
            configs = [BotConfig(
                name=e["name"], prompt_file=e.get("prompt", f"{e['name']}.md"),
                interval_seconds=e.get("interval", 600),
                heartbeat_timeout=e.get("interval", 600) * 2,
                model=e.get("model", "default"), fallback_model=e.get("fallback_model", ""),
                enabled=e.get("enabled", True), max_restarts=e.get("max_restarts", 5),
                clean_exit_wait=e.get("clean_exit_wait", True), tier=e.get("tier", 2),
                runner_mode=e.get("runner_mode", "api"),
                max_tokens_per_run=e.get("max_tokens_per_run", 0),
            ) for e in entries]
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

def is_draining() -> bool:
    return _paths.drain_file.exists()

def _check_self_restart(bots: dict[str, BotState]) -> bool:
    if not _paths.restart_file.exists():
        return False
    try:
        reason = _paths.restart_file.read_text(encoding="utf-8").strip() or "manual"
    except OSError:
        reason = "manual"
    logger.info(f"Self-restart signal detected (reason: {reason})")
    for bot in bots.values():
        if bot.process is not None and bot.process.poll() is None:
            stop_bot(bot, "self-restart")
    try:
        _paths.restart_file.unlink(missing_ok=True)
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

def check_all_bots(bots: dict[str, BotState]) -> None:
    """Main health check loop. Delegates business logic to services."""
    if _check_self_restart(bots):
        return
    now = time.time()
    for name, bot in bots.items():
        if not bot.config.enabled and bot.process is None:
            if retry_disabled_bot(bot):
                update_bot_state(bot, "waiting")
        if bot.config.enabled and bot.process is not None and retry_stuck_starting(bot):
            logger.info(f"Retrying '{name}' stuck in starting")

    for name, bot in list(bots.items()):
        if not bot.config.enabled or bot.process is None or bot.process.poll() is None:
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
            # Finish scratchpad with error info before transitioning ticket
            assigned_tid = getattr(bot, '_assigned_ticket_id', '')
            if assigned_tid:
                try:
                    scratch = load_scratchpad(STATE_DIR, assigned_tid)
                    scratch.mark_error(f"Bot exited with code {exit_code}")
                    scratch.finish_agent(f"error: exit_code={exit_code}")
                    save_scratchpad(STATE_DIR, scratch)
                except Exception as e:
                    logger.warning(f"Failed to finish scratchpad for ticket {assigned_tid}: {e}")
            transition_ticket_on_error(bot, bots, exit_code)
            bot.next_run_at = now + 5
            update_bot_state(bot, "waiting")

    for name, bot in bots.items():
        if not bot.config.enabled or bot.process is None or bot.process.poll() is not None:
            continue
        if is_stuck(bot):
            eff = effective_heartbeat_timeout(bot)
            hb_age = now - read_heartbeat(bot.config.name) if read_heartbeat(bot.config.name) else 0
            prof = model_profile(bot.config.model)
            logger.warning(f"Bot '{name}' stuck (hb {hb_age:.0f}s > {eff}s, risk={prof.lockup_risk if prof else '?'})")
            _write_alignment_event(name, exit_code=None, exit_reason="stuck", started_at=bot.started_at)
            try:
                run_alignment_pipeline(name)
            except Exception as e:
                logger.warning(f"Alignment failed for {name}: {e}")
            restart_bot(bot, reason="stuck", bots=bots)

    log_bot_statuses(bots)
    for fn, label in [
        (lambda: _sweep_orphan_claims(bots), "orphan sweep"),
        (lambda: apply_agent_availability(bots, stop_fn=stop_bot, update_state_fn=update_bot_state), "availability"),
        (lambda: spawn_demand_agents(bots, GATEWAY_MAX_CONCURRENT, start_bot_fn=start_bot), "demand"),
        (lambda: dispatch_decompose_agents(bots, start_bot_fn=start_bot), "decompose"),
        (lambda: dispatch_planning_agents(bots, start_bot_fn=start_bot), "planning"),
        (lambda: advance_reviewed_tickets(bots), "review"),
        (lambda: gatekeeper_verify_tickets(), "gatekeeper"),
        (lambda: route_ready_tickets(), "route"),
        (lambda: process_rework_tickets(bots), "rework"),
        (lambda: recover_deferred_tickets(), "deferred"),
    ]:
        try:
            fn()
        except Exception as e:
            logger.warning(f"{label} failed: {e}")

    pipeline = get_pipeline_state()
    for name, bot in bots.items():
        if not bot.config.enabled or is_draining() or bot.process is not None:
            continue
        if bot.next_run_at and now < bot.next_run_at:
            continue
        if is_needed_bot(name, pipeline):
            has_ticket = bool(getattr(bot, '_assigned_ticket_id', ''))
            start_bot(bot, bots=bots, is_demand=has_ticket)
        else:
            bot.next_run_at = now + bot.config.interval_seconds
            update_bot_state(bot, "waiting")

def get_status(bots: dict[str, BotState]) -> dict:
    out = {}
    for n, b in bots.items():
        pid = b.process.pid if b.process and b.process.poll() is None else None
        hb = read_heartbeat(b.config.name)
        ha = max(0.0, time.time() - hb) if hb > 0 else None
        pr = model_profile(b.config.model)
        out[n] = {"enabled": b.config.enabled, "running": pid is not None, "pid": pid,
                  "model": b.config.model, "risk": pr.lockup_risk if pr else "unknown",
                  "eff_timeout": effective_heartbeat_timeout(b),
                  "heartbeat_age_seconds": round(ha, 1) if ha else None,
                  "next_run_in": round(b.next_run_at - time.time(), 1) if b.next_run_at and not pid else None,
                  "restart_count": b.restart_count, "consecutive_errors": b.consecutive_errors}
    return out

def print_status(bots: dict[str, BotState]) -> None:
    st = get_status(bots)
    print("\n" + "=" * 90 + "\nBOT ORCHESTRATOR STATUS\n" + "=" * 90)
    for n, i in st.items():
        s = "RUNNING" if i["running"] else ("DISABLED" if not i["enabled"] else ("WAITING" if i["next_run_in"] and i["next_run_in"] > 0 else "STOPPED"))
        nxt_val = i.get('next_run_in')
        nxt_str = f"{nxt_val:.0f}s" if nxt_val and nxt_val > 0 else "-"
        hb_str = f"{i['heartbeat_age_seconds']}s" if i['heartbeat_age_seconds'] else "-"
        print(f"  {n:15s} {s:9s} PID={str(i['pid'] or '-'):>6s} HB={hb_str:>7s} NEXT={nxt_str:>6s} eff={i['eff_timeout']:4.0f}s risk={i['risk']:11s} {i['model']}")
    print("=" * 90 + "\n")

def set_drain(reason: str = "") -> None:
    _paths.drain_file.write_text(f"{time.time()}\n{reason}\n")
    logger.info(f"Drain set: {reason}")

def clear_drain() -> None:
    for f in (_paths.drain_file, _paths.update_lock):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    logger.info("Drain cleared")

def drain_status() -> dict:
    return {"draining": is_draining(), "drain_file": str(_paths.drain_file) if _paths.drain_file.exists() else None,
            "update_lock": str(_paths.update_lock) if _paths.update_lock.exists() else None,
            "drain_reason": _paths.drain_file.read_text().strip() if _paths.drain_file.exists() else None}

def safe_stop_all(bots: dict[str, BotState]) -> dict:
    set_drain("safe-stop")
    res = {}
    for n, b in bots.items():
        if b.process is None or b.process.poll() is not None:
            res[n] = "already stopped"
            update_bot_state(b, "stopped")
            continue
        stop_bot(b, "safe-stop")
        res[n] = "stopped"
        update_bot_state(b, "drained")
    return res

def backup_botnet(tag: str | None = None) -> Path:
    d = _paths.backup_dir / f"botnet-{tag + '-' if tag else ''}{time.strftime('%Y%m%d-%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    for p in BOTS_DIR.glob("*.md"):
        (d / p.name).write_bytes(p.read_bytes())
    return d

def restore_botnet(backup_dir: Path) -> None:
    if not backup_dir.exists():
        raise FileNotFoundError(str(backup_dir))
    for p in backup_dir.glob("*.md"):
        (BOTS_DIR / p.name).write_bytes(p.read_bytes())

def _build_bots(registry: list[BotConfig]) -> dict[str, BotState]:
    bots: dict[str, BotState] = {}
    for config in registry:
        bot = BotState(config=config)
        sf = STATE_DIR / f"{config.name}.state.json"
        if sf.exists():
            try:
                sd = json.loads(sf.read_text())
                if isinstance(sd, dict):
                    bot.consecutive_errors = sd.get("consecutive_errors", 0)
                    bot.next_run_at = sd.get("next_run_at", 0.0)
                    bot.restart_count = sd.get("restart_count", 0)
            except Exception:
                pass
        bots[config.name] = bot
    return bots

def main() -> None:
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
        safe_stop_all(bots)
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
        for n in (args.start or [c.name for c in BOT_REGISTRY]):
            if n in bots and bots[n].config.enabled:
                start_bot(bots[n])
        if args.start:
            time.sleep(2)
            print_status(bots)
        return

    try:
        from codebot.codebot_bootstrap import bootstrap as _cb
        adapter = _cb(BOTS_DIR)
        if adapter:
            logger.info("Bootstrapped: %s", adapter.project_name())
    except ImportError:
        adapter = None
    except Exception as e:
        logger.warning("Bootstrap failed: %s", e)
        adapter = None

    if adapter:
        import codebot.orchestrator as _self
        _self.BOT_REGISTRY = _load_bot_registry()
        bots = _build_bots(_self.BOT_REGISTRY)

    def shutdown_handler(signum, frame):
        for b in bots.values():
            stop_bot(b, "shutdown")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    if not is_draining():
        for b in bots.values():
            b._assigned_ticket_id = ''
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

__all__.extend([
    "ModelProfile", "MODEL_PROFILES", "is_log_stalled",
    "is_error_disabled", "is_restart_budget_exceeded",
    "rotating_slots", "worker_reserved_slots",
    "_get_available_memory_mb", "_model_tier_for_complexity",
    "is_manifest_error_disabled", "is_manifest_restart_budget_exceeded",
    "CLAIM_TTL_SECONDS", "MIN_ROTATING_SLOTS",
    "_read_state_file",
])
