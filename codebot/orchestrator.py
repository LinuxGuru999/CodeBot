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
import signal
import sys
import time
from pathlib import Path
from typing import Any

# Re-exports for backward compatibility
from codebot.process_manager import (
    BotConfig, BotState,
    start_bot, stop_bot, restart_bot,
    is_stuck, is_log_stalled, effective_heartbeat_timeout,
    model_profile, ModelProfile, MODEL_PROFILES,
    read_heartbeat, heartbeat_path, checkpoint_path, read_checkpoint,
    update_bot_state, log_mtime,
    batch_read_heartbeats,
    get_prompt_gateway, set_prompt_gateway, clear_prompt_gateway,
)
from codebot.orchestrator_compat import (
    read_heartbeat as _compat_read_heartbeat,
    checkpoint_path as _compat_checkpoint_path,
    read_checkpoint as _compat_read_checkpoint,
    batch_read_heartbeats as _compat_batch_read_heartbeats,
    read_state_file as _compat_read_state_file,
    is_draining as _compat_is_draining,
)
from codebot.worker_scaler import _model_tier_for_complexity
from codebot.alignment_coordinator import write_alignment_event
from codebot.service_registry import get_run_alignment_pipeline_for_all, get_run_alignment_pipeline

# Canonical service accessors via registry (lazy, single import attempt per service)
# Alignment pipelines are obtained lazily via __getattr__ or direct registry calls.

from codebot import vcs_adapter

from codebot.ticket_dispatcher import (
    process_deferred_gates,
    process_deferred_tickets,
)
from codebot import role_registry as _role_registry
from codebot.state_manager import (
    PathConfig, get_paths, set_drain, clear_drain,
    drain_status, backup_botnet, restore_botnet, check_self_restart,
    safe_stop_all, set_project_adapter as _sm_set_project_adapter,
    get_adapter_instance,
)
from codebot.worker_scaler import load_bot_registry, build_bots, CLAIM_TTL_SECONDS, MIN_ROTATING_SLOTS
from codebot.config_reloader import check_prompt_changes, check_code_changes as _check_code_changes_impl, get_code_mtimes
from codebot.orchestrator_controller import OrchestratorController, ModelRouter
from codebot.orchestrator_services import (
    is_restart_budget_exceeded as _svc_is_restart_budget_exceeded,
    is_error_disabled as _svc_is_error_disabled,
    is_manifest_restart_budget_exceeded as _svc_manifest_restart_budget_exceeded,
    is_manifest_error_disabled as _svc_manifest_error_disabled,
    _read_state_file as _svc_read_state_file,
    _get_available_memory_mb as _svc_get_available_memory_mb,
)
from codebot.process_manager import (
    _write_json_atomic as _pm_write_json_atomic,
)
from codebot.worker_scaler import (
    _model_tier_for_complexity as _ws_model_tier,
)

logger = logging.getLogger("orchestrator")

# Backward-compatible aliases for orchestrator_services functions
_manifest_restart_budget_exceeded = _svc_manifest_restart_budget_exceeded
_manifest_error_disabled = _svc_manifest_error_disabled
is_restart_budget_exceeded = _svc_is_restart_budget_exceeded
is_error_disabled = _svc_is_error_disabled
_read_state_file = _svc_read_state_file
_get_available_memory_mb = _svc_get_available_memory_mb
_model_tier_for_complexity = _ws_model_tier
_write_json_atomic = _pm_write_json_atomic


def set_project_adapter(adapter) -> PathConfig:
    """Configure paths from adapter and return PathConfig.

    Delegates to state_manager.set_project_adapter. Returns the new
    PathConfig for explicit injection without relying on global mutation.
    """
    return _sm_set_project_adapter(adapter)


import codebot.process_manager as _process_manager


def start_bot(*args, **kwargs):
    'Delegate dynamically to process_manager.start_bot for patchability.'
    return _process_manager.start_bot(*args, **kwargs)


def stop_bot(*args, **kwargs):
    'Delegate dynamically to process_manager.stop_bot for patchability.'
    return _process_manager.stop_bot(*args, **kwargs)


def restart_bot(*args, **kwargs):
    'Delegate dynamically to process_manager.restart_bot for patchability.'
    return _process_manager.restart_bot(*args, **kwargs)


# Public API wrappers with full implementation for test compatibility
def is_manifest_restart_budget_exceeded(manifest, now=None) -> bool:
    """Check if restart budget is exceeded, using patchable _read_state_file."""
    if now is None:
        now = time.time()
    if not isinstance(manifest, dict) or not manifest:
        return False
    max_restarts = manifest.get("max_restarts", 5)
    try:
        max_r = int(max_restarts)
    except Exception:
        max_r = 5
    if max_r <= 0:
        return False
    name = manifest.get("name", "")
    state = _read_state_file(name)
    if state.get("_state_error") == "corrupt":
        return True
    timestamps = state.get("restart_timestamps", [])
    if not isinstance(timestamps, list):
        return True
    recent = [t for t in timestamps if isinstance(t, (int, float)) and (now - t) < 3600]
    return len(recent) >= max_r


def is_manifest_error_disabled(manifest, max_consecutive=3) -> bool:
    """Check if bot is error-disabled, using patchable _read_state_file."""
    if not isinstance(manifest, dict) or not manifest:
        return False
    name = manifest.get("name", "")
    state = _read_state_file(name)
    if state.get("_state_error") == "corrupt":
        return True
    consecutive = state.get("consecutive_errors", 0)
    try:
        c = int(consecutive)
    except Exception:
        c = 0
    return c >= max_consecutive

def registered_role_names() -> frozenset[str]:
    """Return all role names known to the registry.

    The orchestrator deliberately does not own role sets; it queries
    ``codebot.role_registry`` so new roles can be added without editing
    this module.
    """
    return frozenset(_role_registry.ROLE_REGISTRY)


def role_names_for_category(category: Any) -> frozenset[str]:
    """Return role names for a registry category."""
    return frozenset(role.name for role in _role_registry.roles_by_category(category))


# Backward compatibility: module-level globals are now resolved dynamically
# via get_paths() or explicit dependency injection to avoid mutable global state.
# The OrchestratorController is instantiated in main() and passed explicitly.


def __getattr__(name: str) -> Any:
    'Backward compatibility aliases via delegation.'
    if name.endswith('_ROLE_NAMES') or name == 'IMPLEMENTATION_ROLE_ORDER':
        try:
            return getattr(_role_registry, name)
        except AttributeError as exc:
            raise AttributeError(f'module {__name__!r} has no attribute {name!r}') from exc
    if name in ('TICKET_CLASS_TO_IMPLEMENTER', 'TICKET_CLASS_TO_REVIEWER', 'REVIEWER_TYPES'):
        from codebot import ticket_dispatcher as _ticket_dispatcher
        return getattr(_ticket_dispatcher, name)
    if name in ('transition_ticket_on_success', 'transition_ticket_on_error', 'compute_rate_limit_backoff', 'rotate_model_on_error'):
        from codebot import dispatch_service as _dispatch_service
        return getattr(_dispatch_service, name)
    if name == 'run_alignment_pipeline':
        return get_run_alignment_pipeline()
    if name in ('BOTS_DIR', 'STATE_DIR', 'LOGS_DIR', 'BACKUP_DIR',
                'ALIGNMENT_EVENTS_DIR', 'DRAIN_FILE', 'UPDATE_LOCK', 'RESTART_FILE'):
        p = get_paths()
        mapping = {
            'BOTS_DIR': p.bots_dir, 'STATE_DIR': p.state_dir,
            'LOGS_DIR': p.logs_dir, 'BACKUP_DIR': p.backup_dir,
            'ALIGNMENT_EVENTS_DIR': p.alignment_events_dir,
            'DRAIN_FILE': p.drain_file, 'UPDATE_LOCK': p.update_lock,
            'RESTART_FILE': p.restart_file,
        }
        return mapping[name]
    # WORKER_POOL, BOT_REGISTRY, and _adapter are no longer exposed as module globals
    # because they depend on a specific OrchestratorController instance which is
    # created in main() and passed explicitly. Accessing them here would require
    # a global singleton, which violates the architecture goal of this ticket.
    # Callers should use the controller instance directly.
    raise AttributeError(f'module {__name__!r} has no attribute {name!r}. Use OrchestratorController instance instead.')


def read_heartbeat(bot_name: str, *, state_dir: Path | None = None) -> float:
    """Read heartbeat for *bot_name*.

    Parameters
    ----------
    bot_name:
        Name of the bot whose heartbeat to read.
    state_dir:
        Optional explicit state directory (dependency injection).
        When ``None``, resolved via ``get_paths().state_dir``.
    """
    sd = state_dir if state_dir is not None else get_paths().state_dir
    return _compat_read_heartbeat(bot_name, state_dir_override=sd)


def checkpoint_path(bot_name: str, *, state_dir: Path | None = None) -> Path:
    """Return the checkpoint path for *bot_name*.

    Accepts an optional *state_dir* for dependency injection; falls back
    to ``get_paths().state_dir`` when omitted.
    """
    sd = state_dir if state_dir is not None else get_paths().state_dir
    return _compat_checkpoint_path(bot_name, state_dir_override=sd)


def read_checkpoint(bot_name: str, *, state_dir: Path | None = None):
    """Read checkpoint for *bot_name*.

    Accepts an optional *state_dir* for dependency injection; falls back
    to ``get_paths().state_dir`` when omitted.
    """
    sd = state_dir if state_dir is not None else get_paths().state_dir
    return _compat_read_checkpoint(bot_name, state_dir_override=sd)


def batch_read_heartbeats(bot_names: list, *, state_dir: Path | None = None):
    """Batch read heartbeats.

    Accepts an optional *state_dir* for dependency injection; falls back
    to ``get_paths().state_dir`` when omitted.
    """
    sd = state_dir if state_dir is not None else get_paths().state_dir
    return _compat_batch_read_heartbeats(bot_names, state_dir_override=sd)


def _read_state_file(bot_name, *, state_dir: Path | None = None):
    """Read state file for *bot_name*.

    Accepts an optional *state_dir* for dependency injection; falls back
    to ``get_paths().state_dir`` when omitted.
    """
    sd = state_dir if state_dir is not None else get_paths().state_dir
    return _compat_read_state_file(bot_name, state_dir_override=sd)


def is_draining(*, drain_file: Path | None = None):
    """Check drain status."""
    from codebot.state_manager import get_paths
    if drain_file is not None:
        df = drain_file
    else:
        try:
            df = get_paths().drain_file
        except Exception:
            return False
    return _compat_is_draining(drain_file_override=df)


def worker_reserved_slots(max_concurrent=26, controller: OrchestratorController | None = None):
    """Return number of slots reserved for workers.

    Args:
        max_concurrent: Maximum concurrent bots (unused, kept for signature compat).
        controller: Optional OrchestratorController instance. If None, raises error
            as global singleton access is removed.
    """
    if controller is None:
        raise RuntimeError(
            "worker_reserved_slots requires an explicit OrchestratorController instance. "
            "Global singleton access has been removed to avoid mutable global state."
        )
    return len(controller.worker_pool)


def rotating_slots(max_concurrent=26, controller: OrchestratorController | None = None):
    """Return number of rotating slots available.

    Args:
        max_concurrent: Maximum concurrent bots.
        controller: Optional OrchestratorController instance. If None, raises error.
    """
    if controller is None:
        raise RuntimeError(
            "rotating_slots requires an explicit OrchestratorController instance. "
            "Global singleton access has been removed to avoid mutable global state."
        )
    return max(MIN_ROTATING_SLOTS, max_concurrent - worker_reserved_slots(max_concurrent, controller=controller))


def _periodic_rl_pipeline_tick() -> None:
    """Combined periodic task: legacy alignment sweep + new RL pipeline processing."""
    try:
        run_alignment_pipeline_for_all()
    except Exception:
        pass


# Import delegated modules
from codebot.orchestrator_status import get_status as _os_get_status, print_status as _os_print_status
from codebot.health_check_loop import (
    init_tick as _hcl_init_tick,
    retry_disabled_and_stuck as _hcl_retry_disabled_and_stuck,
    handle_exited_bots as _hcl_handle_exited_bots,
    handle_stuck_bots as _hcl_handle_stuck_bots,
    run_dispatchers as _hcl_run_dispatchers,
    current_tick_store, flush_tick_store,
)
from codebot.dispatch_service import get_pipeline_state, log_bot_statuses, batch_read_bot_statuses
from codebot.scratchpad import load_scratchpad, save_scratchpad
from codebot.orchestrator_cli import parse_args as _cli_parse_args, handle_cli_commands as _cli_handle
from codebot.orchestrator_scheduler import (
    get_scheduler as _v2_get_scheduler,
    check_all_bots_with_scheduler as _sched_check_all_bots,
)
from codebot.orchestrator_runtime import (
    bootstrap_adapter as _rt_bootstrap_adapter,
    setup_shutdown_handlers as _rt_setup_shutdown_handlers,
    run_main_loop as _rt_run_main_loop,
)


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


def _handle_exited_bots(bots: dict[str, BotState], now: float, ts: Any = None) -> None:
    """Backward-compatible wrapper matching old orchestrator signature."""
    _hcl_handle_exited_bots(bots, now, start_bot, stop_bot, ts)


def _handle_stuck_bots(bots: dict[str, BotState], now: float, hb_cache: dict) -> None:
    """Backward-compatible wrapper matching old orchestrator signature."""
    _hcl_handle_stuck_bots(bots, now, hb_cache, restart_bot)


def _init_tick() -> None:
    return _hcl_init_tick()


def check_all_bots(bots: dict[str, BotState]) -> None:
    """Main health-check loop coordinating all delegated modules."""
    if check_self_restart(bots, stop_bot):
        return

    _hcl_init_tick()
    tick_store = current_tick_store()
    now = time.time()

    bot_names = list(bots.keys())
    heartbeat_cache = batch_read_heartbeats(bot_names)
    running_names = [n for n, b in bots.items()
                     if b.process is not None and b.process.poll() is None]
    # Sample at most 5 bots per tick to bound I/O (CB-F0169)
    status_sample_size = min(5, len(running_names)) if running_names else None
    status_cache = batch_read_bot_statuses(running_names, sample_size=status_sample_size) if running_names else {}

    try:
        adapter = get_adapter_instance()
        check_prompt_changes(bots, get_paths().bots_dir, stop_bot, adapter=adapter)
    except Exception:
        try:
            check_prompt_changes(bots, get_paths().bots_dir, stop_bot)
        except Exception:
            logger.debug("check_prompt_changes failed", exc_info=True)

    _hcl_retry_disabled_and_stuck(bots, heartbeat_cache)
    error_recovered_tids = _hcl_handle_exited_bots(bots, now, start_bot, stop_bot, tick_store)
    _hcl_handle_stuck_bots(bots, now, heartbeat_cache, restart_bot)
    # Sample at most 5 bots per tick to bound I/O (CB-F0169)
    log_sample_size = min(5, len(running_names)) if running_names else None
    log_bot_statuses(bots, preloaded_statuses=status_cache, sample_size=log_sample_size)
    _hcl_run_dispatchers(bots, start_bot, stop_bot, update_bot_state, skip_route_tids=error_recovered_tids, store=tick_store)

    for fn, name in [
        (process_deferred_gates, "process_deferred_gates"),
        (process_deferred_tickets, "process_deferred_tickets"),
    ]:
        try:
            result = fn(store=tick_store)
            if result:
                logger.info("%s processed %d tickets", name, result)
        except Exception as e:
            logger.debug("%s failed: %s", name, e)

    flush_tick_store(tick_store)


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


def main() -> None:
    """Entry point: parse args, build bot state, run or dispatch."""
    import logging as _logging
    if not _logging.root.handlers:
        _logging.basicConfig(
            level=_logging.INFO,
            format="%(asctime)s %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        )
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
        try:
            from codebot.dispatch_service import get_pipeline_state as _gps
            logger.info(f"Overture: pipeline {_gps()}")
        except Exception:
            logger.info("Overture: pipeline unavailable")

    def _v2_check_all_bots(bots: dict) -> None:
        _sched_check_all_bots(bots, check_all_bots)

    _rt_run_main_loop(
        bots, args.check_interval,
        check_all_bots_fn=_v2_check_all_bots,
        stop_bot_fn=stop_bot,
        run_alignment_pipeline_for_all_fn=_periodic_rl_pipeline_tick,
    )


if __name__ == "__main__":
    main()
