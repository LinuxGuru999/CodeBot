#!/usr/bin/env python3
"""Bot Orchestrator — Thin coordinator delegating to focused modules.

Path Configuration
------------------
Paths (BOTS_DIR, STATE_DIR, etc.) are encapsulated in a ``PathConfig``
dataclass provided by ``codebot.state_manager``.  Components access paths
via ``get_paths()`` or the ``PathConfig`` object returned by
``set_project_adapter()``.  The old pattern of mutating module-level
globals with the ``global`` keyword has been removed.

Backward Compatibility
----------------------
``orchestrator.BOTS_DIR``, ``orchestrator.STATE_DIR``, etc. are still
accessible via ``__getattr__`` for external callers, but internal code
should always use ``get_paths()`` or the adapter/config object.
"""
from __future__ import annotations

import logging
import logging.handlers as _lh
import os
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
    start_bot as _pm_start_bot,
    stop_bot as _pm_stop_bot,
    restart_bot as _pm_restart_bot,
    is_stuck as _pm_is_stuck,
    is_log_stalled as _pm_is_log_stalled,
    effective_heartbeat_timeout as _pm_effective_heartbeat_timeout,
    read_heartbeat as _pm_read_heartbeat,
    heartbeat_path as _pm_heartbeat_path,
    checkpoint_path as _pm_checkpoint_path,
    read_checkpoint as _pm_read_checkpoint,
    update_bot_state as _pm_update_bot_state,
    model_profile as _pm_model_profile,
    ModelProfile, MODEL_PROFILES,
    _write_json_atomic as _pm_write_json_atomic,
    log_mtime as _pm_log_mtime,
    GATEWAY_MAX_CONCURRENT,
    _get_code_mtimes as _pm_get_code_mtimes,
    batch_read_heartbeats as _pm_batch_read_heartbeats,
    get_prompt_gateway, set_prompt_gateway, clear_prompt_gateway,
)

# ---------------------------------------------------------------------------
# Patchable wrappers — tests patch these at the orchestrator module level.
# Each wrapper resolves STATE_DIR / DRAIN_FILE dynamically so that
# ``patch.object(orch, "STATE_DIR", tmp)`` works correctly.
# ---------------------------------------------------------------------------

# heartbeat_path: thin alias, not patched directly by tests
heartbeat_path = _pm_heartbeat_path


# ---------------------------------------------------------------------------
# Patchable wrappers — delegates to orchestrator_compat module
# ---------------------------------------------------------------------------
from codebot.orchestrator_compat import (
    read_heartbeat as _compat_read_heartbeat,
    checkpoint_path as _compat_checkpoint_path,
    read_checkpoint as _compat_read_checkpoint,
    batch_read_heartbeats as _compat_batch_read_heartbeats,
    read_state_file as _compat_read_state_file,
    is_draining as _compat_is_draining,
)


def read_heartbeat(bot_name: str) -> float:
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
    return _compat_read_heartbeat(bot_name, state_dir_override=sd)


def checkpoint_path(bot_name: str):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
    return _compat_checkpoint_path(bot_name, state_dir_override=sd)


def read_checkpoint(bot_name: str):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
    return _compat_read_checkpoint(bot_name, state_dir_override=sd)


# Thin alias — not patched by tests directly
is_stuck = _pm_is_stuck


# Thin aliases for internal use and __all__ backward compatibility.
# is_log_stalled kept as function because tests patch it via patch.object
def is_log_stalled(bot=None, model: str = ""):
    """Patchable wrapper for log stall detection."""
    return _pm_is_log_stalled(bot, model)

effective_heartbeat_timeout = _pm_effective_heartbeat_timeout
model_profile = _pm_model_profile
update_bot_state = _pm_update_bot_state
log_mtime = _pm_log_mtime
_write_json_atomic = _pm_write_json_atomic


_get_code_mtimes = _pm_get_code_mtimes


def batch_read_heartbeats(bot_names: list):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
    return _compat_batch_read_heartbeats(bot_names, state_dir_override=sd)


# ---------------------------------------------------------------------------
# Test-compatible manifest wrappers & thin re-exports
# ---------------------------------------------------------------------------
from codebot.worker_scaler import _model_tier_for_complexity
from codebot.orchestrator_services import _read_state_file as _svc_read_state_file


def worker_reserved_slots(max_concurrent=26):
    """Return number of slots reserved for workers (len of WORKER_POOL)."""
    return len(WORKER_POOL)


def rotating_slots(max_concurrent=26):
    """Return number of rotating slots available."""
    return max(MIN_ROTATING_SLOTS, max_concurrent - worker_reserved_slots(max_concurrent))


def _read_state_file(bot_name):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
    return _compat_read_state_file(bot_name, state_dir_override=sd)


def is_manifest_restart_budget_exceeded(manifest, now=None):
    """Test-compatible wrapper: accepts (manifest_dict, timestamp) or (bot_name_str)."""
    if now is None:
        now = time.time()
    if isinstance(manifest, str):
        return _svc_manifest_restart_budget_exceeded(manifest)
    max_restarts = manifest.get("max_restarts", 5) if isinstance(manifest, dict) else 5
    if max_restarts == 0:
        return False
    name = manifest.get("name", "") if isinstance(manifest, dict) else ""
    state = _read_state_file(name) if name else {}
    if "_state_error" in state:
        return True
    timestamps = state.get("restart_timestamps", [])
    if not isinstance(timestamps, list):
        return True
    recent = [t for t in timestamps if isinstance(t, (int, float)) and (now - t) < 3600]
    return len(recent) >= max_restarts


def is_manifest_error_disabled(manifest, max_consecutive=3):
    """Test-compatible wrapper: accepts (manifest_dict, max_consecutive=N) or (bot_name_str)."""
    if isinstance(manifest, str):
        return _svc_manifest_error_disabled(manifest)
    if not isinstance(manifest, dict) or not manifest:
        return False
    name = manifest.get("name", "")
    state = _read_state_file(name) if name else {}
    if "_state_error" in state:
        return True
    errors = state.get("consecutive_errors", 0)
    try:
        errors = int(errors)
    except (TypeError, ValueError):
        return True
    return errors >= max_consecutive


# Re-export is_draining with patchable DRAIN_FILE support
def is_draining():
    """Patchable wrapper: honors orch.DRAIN_FILE patches from tests."""
    df = getattr(sys.modules[__name__], 'DRAIN_FILE', None)
    return _compat_is_draining(drain_file_override=df)


from codebot.alignment_coordinator import write_alignment_event
from codebot.alignment_service import (
    run_alignment_pipeline, run_alignment_pipeline_for_all,
)
try:
    from codebot.alignment_service import set_project_adapter as _align_set_project_adapter
except ImportError:
    _align_set_project_adapter = None  # type: ignore[assignment]
from codebot.rl_engine import set_project_adapter as _rl_set_project_adapter
from codebot.ticket_dispatcher import (
    clear_ticket_store_cache, get_ticket_store,
    _sweep_orphan_claims, advance_reviewed_tickets, process_deferred_gates,
    process_rework_tickets,
    TICKET_CLASS_TO_IMPLEMENTER, TICKET_CLASS_TO_REVIEWER,
)
from codebot.dispatch_service import (
    get_pipeline_state, is_needed_bot, apply_agent_availability,
    rotate_model_on_error, transition_ticket_on_success,
    transition_ticket_on_error, compute_rate_limit_backoff,
    record_workforce_completion,
    retry_disabled_bot, retry_stuck_starting, log_bot_statuses,
    dispatch_ready_tickets,
    IMPLEMENTER_ROLE_NAMES, REVIEWER_ROLE_NAMES,
    batch_read_bot_statuses,
)
from codebot.scratchpad import load_scratchpad, save_scratchpad
from codebot.state_manager import (
    PathConfig, get_paths, set_drain, clear_drain,
    drain_status, backup_botnet, restore_botnet, check_self_restart,
    safe_stop_all, set_project_adapter as _sm_set_project_adapter,
    get_adapter_instance,
)
from codebot.worker_scaler import (
    load_bot_registry, build_bots,
    _get_available_memory_mb,
    CLAIM_TTL_SECONDS, MIN_ROTATING_SLOTS,
)
from codebot.orchestrator_services import (
    _manifest_restart_budget_exceeded as _svc_manifest_restart_budget_exceeded,
    _manifest_error_disabled as _svc_manifest_error_disabled,
    is_restart_budget_exceeded as _svc_is_restart_budget_exceeded,
    is_error_disabled as _svc_is_error_disabled,
)
# Paths are resolved dynamically via get_paths() or __getattr__.
# No module-level _paths cache; state_manager.get_paths() is the single source.


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


# Backward-compatible aliases for orchestrator_services functions
_manifest_restart_budget_exceeded = _svc_manifest_restart_budget_exceeded
_manifest_error_disabled = _svc_manifest_error_disabled
is_restart_budget_exceeded = _svc_is_restart_budget_exceeded
is_error_disabled = _svc_is_error_disabled

# Build WORKER_POOL from bot registry for backward compatibility
def _build_worker_pool() -> frozenset:
    """Build worker pool from current bot registry."""
    try:
        registry = load_bot_registry()
        impl_names = IMPLEMENTER_ROLE_NAMES
        return frozenset(
            c.name for c in registry
            if c.name in impl_names or any(c.name.startswith(f"{r}-") for r in impl_names)
        )
    except Exception:
        return frozenset({"worker-1", "worker-2"})

WORKER_POOL = _build_worker_pool()

# Adapter instance for queue depth and ticket class queries.
# The adapter is registered via state_manager.set_adapter_instance() during
# bootstrap and queried via get_adapter_instance() at tick boundaries.
# The module-level _adapter variable is kept for backward-compatible test patches.
_adapter: Any = None


__all__ = [
    "BotConfig", "BotState", "ModelProfile", "MODEL_PROFILES", "start_bot", "stop_bot", "restart_bot",
    "is_stuck", "is_log_stalled", "effective_heartbeat_timeout", "model_profile", "read_heartbeat",
    "heartbeat_path", "log_mtime", "update_bot_state", "checkpoint_path", "read_checkpoint",
    "BOTS_DIR", "STATE_DIR", "LOGS_DIR", "is_draining", "set_drain", "clear_drain", "get_status",
    "print_status", "safe_stop_all", "backup_botnet", "restore_botnet", "drain_status", "PathConfig",
    "set_project_adapter", "rotating_slots", "worker_reserved_slots", "_get_available_memory_mb",
    "_model_tier_for_complexity", "is_manifest_error_disabled", "is_manifest_restart_budget_exceeded",
    "_read_state_file", "CLAIM_TTL_SECONDS", "MIN_ROTATING_SLOTS", "_write_json_atomic",
    "_get_code_mtimes", "write_alignment_event", "_manifest_restart_budget_exceeded",
    "_manifest_error_disabled", "is_restart_budget_exceeded", "is_error_disabled",
    "batch_read_heartbeats", "WORKER_POOL",
    "get_prompt_gateway", "set_prompt_gateway", "clear_prompt_gateway",
    "get_ticket_store", "clear_ticket_store_cache",
]


def set_project_adapter(adapter: Any) -> PathConfig:
    """Delegate to state_manager.set_project_adapter; no module-level mutation.
    Also injects the adapter into alignment_service and rl_engine to ensure consistent path resolution."""
    config = _sm_set_project_adapter(adapter)
    try:
        _align_set_project_adapter(adapter)
    except Exception as e:
        logger.warning("Failed to set project adapter for alignment_service: %s", e)
    try:
        _rl_set_project_adapter(adapter)
    except Exception as e:
        logger.warning("Failed to set project adapter for rl_engine: %s", e)
    return config


# ---------------------------------------------------------------------------
# Delegation Wrappers for Process Management
# These ensure explicit delegation and allow tests to patch process_manager
# functions while calling through orchestrator.
# ---------------------------------------------------------------------------

def start_bot(bot: BotState, resume_checkpoint: bool = True,
              bots: dict[str, BotState] | None = None,
              is_demand: bool = False) -> bool:
    """Delegate to process_manager.start_bot (dynamic lookup for test patches)."""
    import codebot.process_manager as _pm_mod
    return _pm_mod.start_bot(bot, resume_checkpoint=resume_checkpoint,
                         bots=bots, is_demand=is_demand)


def stop_bot(bot: BotState, reason: str = "manual") -> bool:
    """Delegate to process_manager.stop_bot (dynamic lookup for test patches)."""
    import codebot.process_manager as _pm_mod
    return _pm_mod.stop_bot(bot, reason=reason)


def restart_bot(bot: BotState, reason: str = "stuck",
                bots: dict[str, BotState] | None = None) -> bool:
    """Delegate to process_manager.restart_bot (dynamic lookup for test patches)."""
    import codebot.process_manager as _pm_mod
    if bots is not None:
        return _pm_mod.restart_bot(bot, reason=reason, bots=bots)
    return _pm_mod.restart_bot(bot, reason=reason)


logger = logging.getLogger("orchestrator")


# ---------------------------------------------------------------------------
# Status reporting — delegates to orchestrator_status module
# ---------------------------------------------------------------------------
from codebot.orchestrator_status import get_status as _os_get_status, print_status as _os_print_status


def get_status(bots: dict[str, BotState]) -> dict:
    """Return a dict of bot statuses for display or API."""
    return _os_get_status(
        bots,
        batch_read_heartbeats_fn=batch_read_heartbeats,
        model_profile_fn=model_profile,
        effective_heartbeat_timeout_fn=effective_heartbeat_timeout,
    )


def print_status(bots: dict[str, BotState]) -> None:
    """Print a human-readable status table to stdout."""
    _os_print_status(bots, get_status_fn=get_status)


# ---------------------------------------------------------------------------
# Health-check loop — delegates to health_check_loop module
# ---------------------------------------------------------------------------
from codebot.health_check_loop import (
    init_tick as _hcl_init_tick,
    retry_disabled_and_stuck as _hcl_retry_disabled_and_stuck,
    handle_exited_bots as _hcl_handle_exited_bots,
    handle_stuck_bots as _hcl_handle_stuck_bots,
    run_dispatchers as _hcl_run_dispatchers,
    start_eligible_bots as _hcl_start_eligible_bots,
)

# Backward-compatible wrappers with old signatures for tests
# Tests call: _handle_exited_bots(bots, now, ts=None)
# Tests call: _handle_stuck_bots(bots, now, hb_cache)
def _handle_exited_bots(bots: dict[str, BotState], now: float, ts: Any = None) -> None:
    """Backward-compatible wrapper matching old orchestrator signature."""
    _hcl_handle_exited_bots(bots, now, start_bot, stop_bot, ts)

def _handle_stuck_bots(bots: dict[str, BotState], now: float, hb_cache: dict) -> None:
    """Backward-compatible wrapper matching old orchestrator signature."""
    _hcl_handle_stuck_bots(bots, now, hb_cache, restart_bot)

def _init_tick() -> None:
    return _hcl_init_tick()


def check_all_bots(bots: dict[str, BotState]) -> None:
    if check_self_restart(bots, stop_bot):
        return

    _hcl_init_tick()
    from codebot.health_check_loop import current_tick_store, flush_tick_store
    tick_store = current_tick_store()
    now = time.time()

    # Batch-read all heartbeats and statuses once per tick
    bot_names = list(bots.keys())
    heartbeat_cache = batch_read_heartbeats(bot_names)
    running_names = [n for n, b in bots.items()
                     if b.process is not None and b.process.poll() is None]
    status_cache = batch_read_bot_statuses(running_names) if running_names else {}

    _hcl_retry_disabled_and_stuck(bots, heartbeat_cache)
    error_recovered_tids = _hcl_handle_exited_bots(bots, now, start_bot, stop_bot, tick_store)
    _hcl_handle_stuck_bots(bots, now, heartbeat_cache, restart_bot)
    log_bot_statuses(bots, preloaded_statuses=status_cache)
    _hcl_run_dispatchers(bots, start_bot, stop_bot, update_bot_state, skip_route_tids=error_recovered_tids, store=tick_store)
    try:
        advanced = advance_reviewed_tickets(bots, store=tick_store)
        if advanced:
            logger.info("advance_reviewed_tickets processed %d tickets", advanced)
    except Exception as e:
        logger.debug("advance_reviewed_tickets failed: %s", e)
    try:
        gated = process_deferred_gates(store=tick_store)
        if gated:
            logger.info("process_deferred_gates completed %d tickets", gated)
    except Exception as e:
        logger.debug("process_deferred_gates failed: %s", e)
    try:
        rework_cleaned = process_rework_tickets(bots, store=tick_store)
        if rework_cleaned:
            logger.info("process_rework_tickets cleaned %d stale claims", rework_cleaned)
    except Exception as e:
        logger.debug("process_rework_tickets failed: %s", e)
    _hcl_start_eligible_bots(bots, now, start_bot, store=tick_store)
    flush_tick_store(tick_store)


# ---------------------------------------------------------------------------
# CLI — delegates to orchestrator_cli module
# ---------------------------------------------------------------------------
from codebot.orchestrator_cli import parse_args as _cli_parse_args, handle_cli_commands as _cli_handle


def _parse_args() -> Any:
    """Parse command-line arguments."""
    return _cli_parse_args()


def _handle_cli_commands(args: Any, bots: dict[str, BotState]) -> bool:
    """Handle CLI commands. Returns True if a command was handled (should exit)."""
    return _cli_handle(
        args, bots,
        print_status_fn=print_status,
        is_draining_fn=is_draining,
        drain_status_fn=drain_status,
        clear_drain_fn=clear_drain,
        safe_stop_all_fn=safe_stop_all,
        stop_bot_fn=stop_bot,
        start_bot_fn=start_bot,
    )


# ---------------------------------------------------------------------------
# Scheduler V2 — unconditional dispatch
# ---------------------------------------------------------------------------
_v2_scheduler: Any = None


def _v2_get_scheduler(state_dir: Path | None = None) -> Any | None:
    global _v2_scheduler
    if _v2_scheduler is not None:
        return _v2_scheduler
    from codebot.scheduler_v2.dispatcher import Scheduler as V2Scheduler
    try:
        from codebot.ticket_dispatcher import WORKER_MODEL_CYCLE
        model_pool = list(dict.fromkeys(WORKER_MODEL_CYCLE))
    except ImportError:
        model_pool = ["qwen-3.5-plus"]

    _v2_scheduler = V2Scheduler(
        max_slots=GATEWAY_MAX_CONCURRENT,
        state_dir=state_dir or get_paths().state_dir,
        model_pool=model_pool,
        stagger_seconds=1.0,
    )
    return _v2_scheduler


def _v2_count_active() -> int:
    if _v2_scheduler is not None:
        try:
            return _v2_scheduler.gate.count_active()
        except Exception:
            pass
    return 0


def _v2_check_all_bots(bots: dict[str, BotState]) -> None:
    _v2_get_scheduler()
    check_all_bots(bots)


# ---------------------------------------------------------------------------
# Runtime — delegates to orchestrator_runtime module
# ---------------------------------------------------------------------------
from codebot.orchestrator_runtime import (
    bootstrap_adapter as _rt_bootstrap_adapter,
    setup_shutdown_handlers as _rt_setup_shutdown_handlers,
    run_main_loop as _rt_run_main_loop,
)


def main() -> None:
    """Entry point: parse args, build bot state, run or dispatch."""
    args = _parse_args()
    adapter = _rt_bootstrap_adapter(logger, get_paths)
    registry = load_bot_registry(adapter)
    bots = build_bots(registry)

    if _handle_cli_commands(args, bots):
        return

    _rt_setup_shutdown_handlers(bots, stop_bot)

    if not is_draining():
        for b in bots.values():
            b._assigned_ticket_id = ""
        logger.info(f"Overture: pipeline {get_pipeline_state()}")

    _rt_run_main_loop(
        bots, args.check_interval,
        check_all_bots_fn=_v2_check_all_bots,
        stop_bot_fn=stop_bot,
        run_alignment_pipeline_for_all_fn=run_alignment_pipeline_for_all,
    )


if __name__ == "__main__":
    main()
