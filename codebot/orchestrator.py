#!/usr/bin/env python3
"""Bot Orchestrator — Thin coordinator delegating to focused modules.

Path Configuration
------------------
Paths (BOTS_DIR, STATE_DIR, etc.) are encapsulated in a ``PathConfig``
dataclass provided by ``codebot.state_manager``.  Components access paths
via ``get_paths()`` or the ``PathConfig`` object returned by
``set_project_adapter()``.

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

heartbeat_path = _pm_heartbeat_path
is_stuck = _pm_is_stuck
effective_heartbeat_timeout = _pm_effective_heartbeat_timeout
model_profile = _pm_model_profile
update_bot_state = _pm_update_bot_state
log_mtime = _pm_log_mtime
_write_json_atomic = _pm_write_json_atomic
_get_code_mtimes = _pm_get_code_mtimes


def _get_sd():
    """Get STATE_DIR override for test patching."""
    return getattr(sys.modules[__name__], 'STATE_DIR', None)


def read_heartbeat(bot_name: str) -> float:
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    return _compat_read_heartbeat(bot_name, state_dir_override=_get_sd())

def checkpoint_path(bot_name: str):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    return _compat_checkpoint_path(bot_name, state_dir_override=_get_sd())

def read_checkpoint(bot_name: str):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    return _compat_read_checkpoint(bot_name, state_dir_override=_get_sd())

def batch_read_heartbeats(bot_names: list):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    return _compat_batch_read_heartbeats(bot_names, state_dir_override=_get_sd())

def is_log_stalled(bot=None, model: str = ""):
    """Patchable wrapper for log stall detection."""
    return _pm_is_log_stalled(bot, model)


# ---------------------------------------------------------------------------
# Test-compatible manifest wrappers & thin re-exports
# ---------------------------------------------------------------------------
from codebot.worker_scaler import _model_tier_for_complexity
from codebot.orchestrator_services import _read_state_file as _svc_read_state_file


def worker_reserved_slots(max_concurrent=26):
    """Return number of slots reserved for workers (len of WORKER_POOL)."""
    return len(_get_default_controller().worker_pool)


def rotating_slots(max_concurrent=26):
    """Return number of rotating slots available."""
    return max(MIN_ROTATING_SLOTS, max_concurrent - worker_reserved_slots(max_concurrent))


def _read_state_file(bot_name):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    return _compat_read_state_file(bot_name, state_dir_override=_get_sd())


def is_draining():
    """Patchable wrapper: honors orch.DRAIN_FILE patches from tests."""
    df = getattr(sys.modules[__name__], 'DRAIN_FILE', None)
    return _compat_is_draining(drain_file_override=df)


from codebot.alignment_coordinator import write_alignment_event
try:
    from codebot.alignment_service import (
        run_alignment_pipeline, run_alignment_pipeline_for_all,
    )
except ModuleNotFoundError:
    def run_alignment_pipeline(*a: Any, **kw: Any) -> bool:  # type: ignore[misc]
        return False
    def run_alignment_pipeline_for_all() -> None:  # type: ignore[misc]
        pass


def _periodic_rl_pipeline_tick() -> None:
    """Combined periodic task: legacy alignment sweep + new RL pipeline processing."""
    try:
        run_alignment_pipeline_for_all()
    except Exception:
        pass
    try:
        from codebot.rl_diagnostics import run_pipeline_tick
        run_pipeline_tick()
    except Exception:
        pass


from codebot.ticket_dispatcher import (
    clear_ticket_store_cache as _clear_ticket_store_cache,
    get_ticket_store as _get_ticket_store,
    _sweep_orphan_claims, advance_reviewed_tickets, process_deferred_gates,
    process_rework_tickets, process_deferred_tickets,
    TICKET_CLASS_TO_IMPLEMENTER, TICKET_CLASS_TO_REVIEWER,
)

# Explicit re-exports for backward compatibility and test visibility
get_ticket_store = _get_ticket_store
clear_ticket_store_cache = _clear_ticket_store_cache
from codebot.role_registry import RoleCategory, roles_by_category
from codebot.dispatch_service import (
    get_pipeline_state, is_needed_bot, apply_agent_availability,
    rotate_model_on_error, transition_ticket_on_success,
    transition_ticket_on_error, compute_rate_limit_backoff,
    record_workforce_completion,
    retry_disabled_bot, retry_stuck_starting, log_bot_statuses,
    dispatch_ready_tickets,
    batch_read_bot_statuses,
)
from codebot.scratchpad import load_scratchpad, save_scratchpad
from codebot.state_manager import (
    PathConfig, get_paths, set_drain, clear_drain,
    drain_status, backup_botnet, restore_botnet, check_self_restart,
    safe_stop_all, set_project_adapter as _sm_set_project_adapter,
    get_adapter_instance,
)
from codebot.process_supervisor import ProcessSupervisor
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
from codebot.checkpoint_manager import (
    is_manifest_restart_budget_exceeded as _cm_is_manifest_restart_budget_exceeded,
    is_manifest_error_disabled as _cm_is_manifest_error_disabled,
)
from codebot.config_reloader import check_prompt_changes
from codebot.orchestrator_controller import OrchestratorController, ModelRouter
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
    # Delegate WORKER_POOL and BOT_REGISTRY to singleton controller
    if name == "WORKER_POOL":
        return _get_default_controller().worker_pool
    if name == "BOT_REGISTRY":
        return _get_default_controller().registry
    if name == "_adapter":
        return _get_default_controller().adapter
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


# Backward-compatible aliases for orchestrator_services functions
_manifest_restart_budget_exceeded = _svc_manifest_restart_budget_exceeded
_manifest_error_disabled = _svc_manifest_error_disabled
is_restart_budget_exceeded = _svc_is_restart_budget_exceeded
is_error_disabled = _svc_is_error_disabled

# Public API aliases (dict-based manifest) from checkpoint_manager for test compatibility
is_manifest_restart_budget_exceeded = _cm_is_manifest_restart_budget_exceeded
is_manifest_error_disabled = _cm_is_manifest_error_disabled

# Singleton controller for backward-compatible attribute access.
# Tests and external code accessing orch.WORKER_POOL or orch.BOT_REGISTRY
# will be served via __getattr__ delegation to this controller.
class _ControllerHolder:
    """Mutable holder to avoid 'global' keyword in lazy initialization."""
    instance: OrchestratorController | None = None

_holder = _ControllerHolder()


def _get_default_controller() -> OrchestratorController:
    """Lazily instantiate the singleton controller for backward compat."""
    if _holder.instance is None:
        _holder.instance = OrchestratorController()
    return _holder.instance


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
    """Delegate to state_manager.set_project_adapter; no module-level mutation."""
    config = _sm_set_project_adapter(adapter)
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
    from codebot.state_manager import get_adapter_instance
    tick_store = current_tick_store()
    now = time.time()

    # Batch-read all heartbeats and statuses once per tick
    bot_names = list(bots.keys())
    heartbeat_cache = batch_read_heartbeats(bot_names)
    running_names = [n for n, b in bots.items()
                     if b.process is not None and b.process.poll() is None]
    status_cache = batch_read_bot_statuses(running_names) if running_names else {}

    # Check for prompt changes using injected adapter
    try:
        adapter = get_adapter_instance()
        check_prompt_changes(bots, BOTS_DIR, stop_bot, adapter=adapter)
    except Exception:
        # Fallback to legacy behavior if adapter retrieval fails
        try:
            check_prompt_changes(bots, BOTS_DIR, stop_bot)
        except Exception:
            logger.debug("check_prompt_changes failed", exc_info=True)

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
    try:
        undeferred = process_deferred_tickets(store=tick_store)
        if undeferred:
            logger.info("process_deferred_tickets undeferred %d tickets", undeferred)
    except Exception as e:
        logger.debug("process_deferred_tickets failed: %s", e)
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
# Scheduler V2 — delegates to orchestrator_scheduler module
# ---------------------------------------------------------------------------
from codebot.orchestrator_scheduler import (
    get_scheduler as _sched_get_scheduler,
    count_active as _sched_count_active,
    check_all_bots_with_scheduler as _sched_check_all_bots,
)


def _v2_get_scheduler(state_dir: Path | None = None) -> Any | None:
    """Backward-compatible wrapper for V2 scheduler access."""
    return _sched_get_scheduler(state_dir)


def _v2_count_active() -> int:
    """Backward-compatible wrapper for active task count."""
    return _sched_count_active()


def _v2_check_all_bots(bots: dict[str, BotState]) -> None:
    """Backward-compatible wrapper ensuring scheduler is initialized."""
    _sched_check_all_bots(bots, check_all_bots)


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
    controller = OrchestratorController(adapter=adapter)
    registry = controller.registry
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
        run_alignment_pipeline_for_all_fn=_periodic_rl_pipeline_tick,
    )


if __name__ == "__main__":
    main()
