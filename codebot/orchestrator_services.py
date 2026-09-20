#!/usr/bin/env python3
"""Orchestrator Services — Health checks, status, and bot registry.

Purpose
-------
Extracted from orchestrator.py to maintain the 'Thin router, fat service'
architectural invariant. Contains:
- Bot registry loading
- Role name constants
- Health check helpers (retry, model rotation, pipeline state)
- Status and control functions (get_status, drain, backup/restore)

Why
---
The orchestrator should only coordinate process lifecycle, not contain
business logic for health checks, status reporting, or bot configuration.
"""

from __future__ import annotations

import json
import logging
import os
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from codebot.process_manager import (
    BotConfig,
    BotState,
    effective_heartbeat_timeout,
    model_profile,
    read_heartbeat,
    log_mtime,
    update_bot_state,
    stop_bot,
    STATE_DIR,
    LOGS_DIR,
    BOTS_DIR,
)

logger = logging.getLogger("orchestrator_services")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_project_root = Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))
_paths_state_dir = _project_root / ".codebot" / "state"
_paths_logs_dir = _project_root / ".codebot" / "logs"
_paths_backup_dir = _paths_state_dir / "backup"
_paths_drain_file = _paths_state_dir / ".drain"
_paths_update_lock = _paths_state_dir / ".update_lock"
_paths_restart_file = _paths_state_dir / ".restart"

# Ensure directories exist
_paths_state_dir.mkdir(parents=True, exist_ok=True)
_paths_logs_dir.mkdir(parents=True, exist_ok=True)
_paths_backup_dir.mkdir(parents=True, exist_ok=True)

# Backward-compatible _paths object
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

# Re-export for backward compatibility
ALIGNMENT_EVENTS_DIR = _paths.alignment_events_dir

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


def set_adapter_instance(adapter: Any) -> None:
    """Set the project adapter instance for bot registry loading."""
    global _adapter_instance
    _adapter_instance = adapter


def load_bot_registry() -> list[BotConfig]:
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

# ---------------------------------------------------------------------------
# Drain & Restart
# ---------------------------------------------------------------------------

def is_draining() -> bool:
    """Check if drain flag is active."""
    return _paths_drain_file.exists()


def set_drain(reason: str = "") -> None:
    """Set drain flag to prevent new bot spawns."""
    _paths_drain_file.write_text(f"{time.time()}\n{reason}\n")
    logger.info(f"Drain flag set: {reason}")


def clear_drain() -> None:
    """Clear drain flag to allow bot respawns."""
    for f in (_paths_drain_file, _paths_update_lock):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    logger.info("Drain cleared")


def drain_status() -> dict:
    """Get current drain status."""
    return {
        "draining": is_draining(),
        "drain_file": str(_paths_drain_file) if _paths_drain_file.exists() else None,
        "update_lock": str(_paths_update_lock) if _paths_update_lock.exists() else None,
        "drain_reason": _paths_drain_file.read_text().strip() if _paths_drain_file.exists() else None,
    }


def check_self_restart(bots: dict[str, BotState]) -> bool:
    """Check for self-restart signal and execute if found."""
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
    import sys
    python = sys.executable
    args = [python] + sys.argv
    logger.info(f"Executing self-restart: {' '.join(args)}")
    os.execv(python, args)
    return True

# ---------------------------------------------------------------------------
# Health Check Helpers
# ---------------------------------------------------------------------------

def retry_disabled_bot(bot: BotState, bots: dict[str, BotState]) -> bool:
    """Retry a disabled bot after cooldown period."""
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


def retry_stuck_starting(bot: BotState, bots: dict[str, BotState]) -> bool:
    """Check if a bot is stuck in starting state."""
    if bot.process is not None and bot.process.poll() is not None:
        return True
    last_hb = read_heartbeat(bot.config.name)
    if last_hb > 0:
        if time.time() - last_hb > 120.0:
            return True
        return False
    elapsed = time.time() - (bot.started_at or bot.last_heartbeat or time.time())
    return elapsed > 120.0


def rotate_model_on_error(bot: BotState, bots: dict[str, BotState] | None = None) -> str:
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


def write_alignment_event(bot_name: str, exit_code: int | None, exit_reason: str, started_at: float | None = None) -> None:
    """Write alignment event for RL scoring."""
    try:
        from codebot.alignment_service import _write_alignment_event as _wae
        _wae(bot_name, exit_code, exit_reason, started_at)
    except Exception as e:
        logger.warning(f"Failed to write alignment event for {bot_name}: {e}")


def log_bot_statuses(bots: dict[str, BotState]) -> None:
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


def get_pipeline_state(store: Any | None = None) -> dict[str, int]:
    """Get current pipeline state counts."""
    try:
        from codebot.ticket_engine import TicketState
        from codebot.ticket_dispatcher import get_ticket_store
        _store = store if store is not None else get_ticket_store()
        if _store is None:
            return {}
        counts: dict[str, int] = {}
        for state in TicketState:
            tickets = _store.list_by_state(state)
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

    always_on: set[str] = set()
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


def apply_agent_availability(bots: dict[str, BotState], store: Any | None = None) -> None:
    """Enable or suppress bot spawns based on current ticket queue state."""
    pipeline = get_pipeline_state(store=store)
    ready = pipeline.get("READY", 0)
    decompose = pipeline.get("DECOMPOSE", 0)
    planning = pipeline.get("PLANNING", 0)
    implementing = pipeline.get("IMPLEMENTING", 0)
    reviewing = pipeline.get("REVIEWING", 0)

    always_on: set[str] = set()

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
# Status & Control
# ---------------------------------------------------------------------------

def get_status(bots: dict[str, BotState]) -> dict:
    """Get status of all bots."""
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
    """Print formatted status of all bots."""
    status = get_status(bots)
    print("\n" + "=" * 90)
    print("BOT ORCHESTRATOR STATUS")
    print("=" * 90)
    for name, info in status.items():
        state = "RUNNING" if info["running"] else ("WAITING" if info["next_run_in"] and info["next_run_in"] > 0 else "STOPPED")
        if not info["enabled"]:
            state = "DISABLED"
        hb = f"{info['heartbeat_age_seconds']}s" if info['heartbeat_age_seconds'] else "-"
        nxt_val = info.get('next_run_in')
        nxt = f'{nxt_val:.0f}s' if nxt_val and nxt_val > 0 else '-'
        pid = str(info['pid']) if info['pid'] else "-"
        print(f"  {name:15s} {state:9s} PID={pid:6s} HB={hb:7s} NEXT={nxt:6s} eff={info['eff_timeout']:4.0f}s risk={info['risk']:11s} {info['model']}")
    print("=" * 90 + "\n")


def safe_stop_all(bots: dict[str, BotState]) -> dict:
    """Gracefully stop all bots via drain."""
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
    """Backup all bot prompt files."""
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"botnet-{tag}-{ts}" if tag else f"botnet-{ts}"
    dest = _paths_backup_dir / name
    dest.mkdir(parents=True, exist_ok=True)
    for p in BOTS_DIR.glob("*.md"):
        (dest / p.name).write_bytes(p.read_bytes())
    logger.info(f"Botnet backup -> {dest}")
    return dest


def restore_botnet(backup_dir: Path) -> None:
    """Restore bot prompt files from backup."""
    if not backup_dir.exists():
        raise FileNotFoundError(str(backup_dir))
    for p in backup_dir.glob("*.md"):
        (BOTS_DIR / p.name).write_bytes(p.read_bytes())
    logger.info(f"Restored botnet from {backup_dir}")


# ---------------------------------------------------------------------------
# Backward Compatibility & State Utilities
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
    """Alias for is_error_disabled for backward compatibility."""
    return is_error_disabled(bot_name)


def is_manifest_restart_budget_exceeded(bot_name: str) -> bool:
    """Alias for is_restart_budget_exceeded for backward compatibility."""
    return is_restart_budget_exceeded(bot_name)


def _read_state_file(bot_name: str) -> dict:
    """Read bot state file and return as dict."""
    state_file = STATE_DIR / f"{bot_name}.state.json"
    try:
        if state_file.exists():
            return json.loads(state_file.read_text())
    except Exception:
        pass
    return {}


def build_bots(registry: list[BotConfig]) -> dict[str, BotState]:
    """Build bot state dictionary from registry."""
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
