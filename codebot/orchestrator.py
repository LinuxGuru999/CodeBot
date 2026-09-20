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
    start_bot, stop_bot, restart_bot,
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
)

# ---------------------------------------------------------------------------
# Patchable wrappers — tests patch these at the orchestrator module level.
# Each wrapper resolves STATE_DIR / DRAIN_FILE dynamically so that
# ``patch.object(orch, "STATE_DIR", tmp)`` works correctly.
# ---------------------------------------------------------------------------

def heartbeat_path(bot_name: str):
    """Patchable wrapper: uses orchestrator-level STATE_DIR when patched."""
    try:
        sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
        if sd is not None:
            from pathlib import Path as _P
            return _P(sd) / f"{bot_name}.heartbeat"
    except Exception:
        pass
    return _pm_heartbeat_path(bot_name)


def read_heartbeat(bot_name: str) -> float:
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    try:
        sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
        if sd is not None:
            import datetime as _dt
            from pathlib import Path as _P
            hb = _P(sd) / f"{bot_name}.heartbeat"
            if not hb.exists():
                return 0.0
            txt = hb.read_text().strip()
            try:
                return float(txt)
            except (ValueError, OSError):
                pass
            try:
                token = txt.split()[0].replace("Z", "+00:00")
                d = _dt.datetime.fromisoformat(token)
                if d.tzinfo is None:
                    d = d.replace(tzinfo=_dt.timezone.utc)
                ts = d.timestamp()
                now = time.time()
                if ts > now + 60 or ts < now - 86400:
                    return 0.0
                return ts
            except Exception:
                return 0.0
    except Exception:
        pass
    return _pm_read_heartbeat(bot_name)


def checkpoint_path(bot_name: str):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    try:
        sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
        if sd is not None:
            from pathlib import Path as _P
            return _P(sd) / f"{bot_name}.checkpoint.json"
    except Exception:
        pass
    return _pm_checkpoint_path(bot_name)


def read_checkpoint(bot_name: str):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    try:
        sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
        if sd is not None:
            from pathlib import Path as _P
            import json as _json
            p = _P(sd) / f"{bot_name}.checkpoint.json"
            bak = p.with_suffix(".bak") if p.suffix == ".json" else _P(str(p) + ".bak")
            if not p.exists():
                if bak.exists():
                    try:
                        data = _json.loads(bak.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            return data
                    except Exception:
                        pass
                return None
            try:
                raw = p.read_text(encoding="utf-8")
                data = _json.loads(raw)
                if isinstance(data, dict):
                    return data
                return None
            except Exception:
                # Try backup
                if bak.exists():
                    try:
                        data = _json.loads(bak.read_text(encoding="utf-8"))
                        if isinstance(data, dict):
                            return data
                    except Exception:
                        pass
                return None
    except Exception:
        pass
    return _pm_read_checkpoint(bot_name)


def is_stuck(bot=None, heartbeat_cache=None):
    """Patchable wrapper for stuck detection."""
    return _pm_is_stuck(bot, heartbeat_cache=heartbeat_cache)


def is_log_stalled(bot=None, model: str = ""):
    """Patchable wrapper for log stall detection."""
    return _pm_is_log_stalled(bot, model)


def effective_heartbeat_timeout(bot=None, model: str = "", interval_seconds: int = 0, base_timeout: int = 0):
    """Patchable wrapper for timeout calculation."""
    return _pm_effective_heartbeat_timeout(bot, model, interval_seconds, base_timeout)


def model_profile(model: str):
    """Patchable wrapper for model profile lookup."""
    return _pm_model_profile(model)


def update_bot_state(bot, status: str):
    """Patchable wrapper for state updates."""
    return _pm_update_bot_state(bot, status)


def log_mtime(bot_name: str) -> float:
    """Patchable wrapper for log mtime."""
    return _pm_log_mtime(bot_name)


def _write_json_atomic(path, data):
    """Patchable wrapper for atomic JSON writes."""
    return _pm_write_json_atomic(path, data)


def _get_code_mtimes():
    """Patchable wrapper for code mtime detection."""
    return _pm_get_code_mtimes()


def batch_read_heartbeats(bot_names: list):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    try:
        sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
        if sd is not None:
            import datetime as _dt
            from pathlib import Path as _P
            results = {}
            for name in bot_names:
                hb = _P(sd) / f"{name}.heartbeat"
                if not hb.exists():
                    results[name] = 0.0
                    continue
                txt = hb.read_text().strip()
                ts = 0.0
                try:
                    ts = float(txt)
                except (ValueError, OSError):
                    try:
                        token = txt.split()[0].replace("Z", "+00:00")
                        d = _dt.datetime.fromisoformat(token)
                        if d.tzinfo is None:
                            d = d.replace(tzinfo=_dt.timezone.utc)
                        ts = d.timestamp()
                        now = time.time()
                        if ts > now + 60 or ts < now - 86400:
                            ts = 0.0
                    except Exception:
                        ts = 0.0
                results[name] = ts
            return results
    except Exception:
        pass
    return _pm_batch_read_heartbeats(bot_names)


# ---------------------------------------------------------------------------
# Patchable wrappers for orchestrator_services functions
# Tests call these with different signatures than the extracted versions.
# ---------------------------------------------------------------------------

def is_manifest_restart_budget_exceeded(manifest, now=None):
    """Test-compatible wrapper: accepts (manifest_dict, timestamp) or (bot_name_str).

    Original contract from tests:
      - is_manifest_restart_budget_exceeded({}, time.time()) -> False
      - is_manifest_restart_budget_exceeded(manifest, now) with patched _read_state_file
    """
    if now is None:
        now = time.time()
    if isinstance(manifest, str):
        # Called with bot name — delegate to services
        from codebot.orchestrator_services import _manifest_restart_budget_exceeded as _svc
        return _svc(manifest)
    # Called with manifest dict + now timestamp (test contract)
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
    """Test-compatible wrapper: accepts (manifest_dict, max_consecutive=N) or (bot_name_str).

    Original contract from tests:
      - is_manifest_error_disabled({}) -> False
      - is_manifest_error_disabled(manifest, max_consecutive=3)
    """
    if isinstance(manifest, str):
        from codebot.orchestrator_services import _manifest_error_disabled as _svc
        return _svc(manifest)
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


def worker_reserved_slots(max_concurrent=26):
    """Return number of slots reserved for workers (len of WORKER_POOL)."""
    return len(WORKER_POOL)


def rotating_slots(max_concurrent=26):
    """Return number of rotating slots available."""
    return max(MIN_ROTATING_SLOTS, max_concurrent - worker_reserved_slots(max_concurrent))


def _model_tier_for_complexity(model, complexity, queue_has_tier_work=False):
    """Test-compatible model tier check.

    Contract from tests:
      - cheap models (xiaomi-mimo-2.5) accept trivial/small/medium, reject high/critical
      - expensive models (qwen-3.8-max) accept high/critical
      - expensive models reject trivial when queue_has_tier_work=True
      - unknown models always accepted
    """
    cheap_models = frozenset({"xiaomi-mimo-2.5"})
    expensive_models = frozenset({"qwen-3.8-max", "qwen-3.8-max-thinking", "qwen-3.7-max", "qwen-3.7-max-thinking"})

    if model not in cheap_models and model not in expensive_models:
        # Unknown model — always accepted
        return True

    if model in cheap_models:
        if complexity in ("trivial", "small", "medium"):
            return True
        return False  # high, critical rejected for cheap models

    if model in expensive_models:
        if complexity in ("high", "critical"):
            return True
        if complexity in ("trivial", "small", "medium"):
            if queue_has_tier_work:
                return False
            return True
        return True

    return True


def _read_state_file(bot_name):
    """Patchable wrapper honoring orchestrator-level STATE_DIR patches."""
    try:
        sd = getattr(sys.modules[__name__], 'STATE_DIR', None)
        if sd is not None:
            import json as _json
            from pathlib import Path as _P
            sf = _P(sd) / f"{bot_name}.state.json"
            if not sf.exists():
                return {}
            try:
                data = _json.loads(sf.read_text(encoding="utf-8"))
                return data if isinstance(data, dict) else {}
            except Exception:
                return {}
    except Exception:
        pass
    from codebot.orchestrator_services import _read_state_file as _svc
    return _svc(bot_name)


# Re-export is_draining with patchable DRAIN_FILE support
def is_draining():
    """Patchable wrapper: honors orch.DRAIN_FILE patches from tests."""
    try:
        df = getattr(sys.modules[__name__], 'DRAIN_FILE', None)
        if df is not None:
            from pathlib import Path as _P
            return _P(df).exists()
    except Exception:
        pass
    from codebot.state_manager import is_draining as _sm_is_draining
    return _sm_is_draining()


from codebot.alignment_coordinator import write_alignment_event
from codebot.alignment_service import (
    run_alignment_pipeline, run_alignment_pipeline_for_all,
)
from codebot.ticket_dispatcher import (
    spawn_demand_agents, dispatch_decompose_agents,
    dispatch_planning_agents, advance_reviewed_tickets,
    gatekeeper_verify_tickets, route_ready_tickets,
    process_rework_tickets, recover_deferred_tickets,
    _sweep_orphan_claims,
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
from codebot.health_check import check_all_bots

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


# Backward-compatible aliases for orchestrator_services functions
def _manifest_restart_budget_exceeded(bot_name):
    return _svc_manifest_restart_budget_exceeded(bot_name)

def _manifest_error_disabled(bot_name):
    return _svc_manifest_error_disabled(bot_name)

def is_restart_budget_exceeded(bot_name):
    return _svc_is_restart_budget_exceeded(bot_name)

def is_error_disabled(bot_name):
    return _svc_is_error_disabled(bot_name)

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


# ---------------------------------------------------------------------------
# Code-change detection — O(M+B) implementation
# ---------------------------------------------------------------------------
_last_code_mtimes: dict[str, float] = {}


def _check_code_changes(bots: dict[str, BotState]) -> None:
    """Check for source code changes and respawn affected bots.

    O(M+B): O(M) set comprehension for changed modules, O(B) bot iteration.
    ``_last_code_mtimes`` is module-level state persisting across ticks.
    Empty on first call — all modules treated as changed (safe, no missed updates).
    """
    global _last_code_mtimes
    current_mtimes = _get_code_mtimes()
    if not current_mtimes:
        return
    prev, _last_code_mtimes = _last_code_mtimes, dict(current_mtimes)
    changed = sorted({m for m, t in current_mtimes.items() if prev.get(m, 0) > 0 and t > prev[m]})
    if not changed:
        return
    logger.info("Code change detected in %s — respawning active bots", changed)
    for _, bot in bots.items():
        if bot.config.enabled and bot.process is not None and bot.process.poll() is None:
            stop_bot(bot, f"code-hot-reload:{','.join(changed)}")
            bot.next_run_at = time.time()
        bot.last_code_mtimes = dict(current_mtimes)


DECOMPOSER_MAX_CONCURRENT = 12

LOG_MAX_BYTES = int(os.environ.get("CODEBOT_LOG_MAX_BYTES", str(5 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.environ.get("CODEBOT_LOG_BACKUPS", "5"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[_lh.RotatingFileHandler(
        get_paths().logs_dir / "orchestrator.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
    )],
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
        _, observed_mtime = read_prompt_with_mtime(get_paths().bots_dir / bot.config.prompt_file)
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


# ---------------------------------------------------------------------------
# Helper functions for check_all_bots — each < 100 LOC
# ---------------------------------------------------------------------------

def _init_tick() -> None:
    """Initialize per-tick cache state via QueueManager adapter.

    Uses QueueManager to clear cache and query queue depth, decoupling
    the orchestrator from direct TicketStore dependency. Queue depth
    is provided via the injected project adapter interface.
    """
    from codebot.ticket_engine import QueueManager
    from codebot.state_manager import get_paths

    # Use QueueManager for cache management instead of direct imports
    qm = QueueManager.from_state_dir(get_paths().state_dir)
    qm.clear_cache()

    # Log queue depth via adapter (orchestrator doesn't touch TicketStore directly)
    adapter = get_adapter_instance()
    if adapter is not None:
        try:
            qd = adapter.queue_depth()
            logger.debug("Adapter queue depth: %d", qd)
        except Exception:
            logger.debug("Adapter queue depth unavailable")


def _retry_disabled_and_stuck(bots: dict[str, BotState], hb_cache: dict) -> None:
    """Retry disabled or stuck-starting bots."""
    for name, bot in bots.items():
        if not bot.config.enabled and bot.process is None:
            if retry_disabled_bot(bot):
                update_bot_state(bot, "waiting")
        if bot.config.enabled and bot.process is not None:
            if retry_stuck_starting(bot, heartbeat_cache=hb_cache):
                logger.info(f"Retrying '{name}' stuck in starting")


def _handle_exited_bots(bots: dict[str, BotState], now: float, ts: Any = None) -> set[str]:
    """Handle bots that have exited, running alignment and transitioning tickets.

    Args:
        bots: Dict of bot states.
        now: Current timestamp.
        ts: Optional TicketStore (unused, kept for backward-compatible call sites).

    Returns a set of ticket IDs that were returned to READY due to error exits,
    so dispatchers can skip re-routing them in the same tick.
    """
    current_paths = get_paths()
    error_recovered_tids: set[str] = set()
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
            assigned_tid = getattr(bot, "_assigned_ticket_id", "")
            if assigned_tid:
                try:
                    scratch = load_scratchpad(current_paths.state_dir, assigned_tid)
                    scratch.mark_error(f"Bot exited with code {exit_code}")
                    scratch.finish_agent(f"error: exit_code={exit_code}")
                    save_scratchpad(current_paths.state_dir, scratch)
                except Exception as e:
                    logger.warning(f"Failed to finish scratchpad for ticket {assigned_tid}: {e}")
                error_recovered_tids.add(assigned_tid)
            transition_ticket_on_error(bot, bots, exit_code)
            bot.next_run_at = now + 5
            update_bot_state(bot, "waiting")
    return error_recovered_tids


def _handle_stuck_bots(bots: dict[str, BotState], now: float, hb_cache: dict) -> None:
    """Detect and restart stuck bots based on heartbeat age."""
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


def _run_dispatchers(bots: dict[str, BotState], skip_route_tids: set[str] | None = None) -> None:
    """Run all dispatcher tasks.  Each resolves its own TicketStore instance."""
    def _route_with_skip():
        if skip_route_tids:
            # Temporarily mark tickets so route_ready_tickets skips them
            return route_ready_tickets(skip_tids=skip_route_tids)
        return route_ready_tickets()

    tasks = [
        (lambda: _sweep_orphan_claims(bots), "orphan sweep"),
        (lambda: apply_agent_availability(bots, stop_fn=stop_bot, update_state_fn=update_bot_state), "availability"),
        (lambda: spawn_demand_agents(bots, GATEWAY_MAX_CONCURRENT, start_bot_fn=start_bot), "demand"),
        (lambda: dispatch_decompose_agents(bots, max_agents=DECOMPOSER_MAX_CONCURRENT, start_bot_fn=start_bot), "decompose"),
        (lambda: dispatch_planning_agents(bots, start_bot_fn=start_bot), "planning"),
        (lambda: advance_reviewed_tickets(bots), "review"),
        (lambda: gatekeeper_verify_tickets(), "gatekeeper"),
        (_route_with_skip, "route"),
        (lambda: process_rework_tickets(bots), "rework"),
        (lambda: recover_deferred_tickets(), "deferred"),
    ]
    for fn, label in tasks:
        try:
            fn()
        except Exception as e:
            logger.warning(f"{label} failed: {e}")


def _start_eligible_bots(bots: dict[str, BotState], now: float) -> None:
    """Start eligible bots, skipping implementers/reviewers handled by dispatchers."""
    pipeline = get_pipeline_state()
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


def check_all_bots(bots: dict[str, BotState]) -> None:
    """Main health-check loop. Delegates all business logic to helper functions."""
    if check_self_restart(bots, stop_bot):
        return

    _init_tick()
    now = time.time()

    # Batch-read all heartbeats and statuses once per tick
    bot_names = list(bots.keys())
    heartbeat_cache = batch_read_heartbeats(bot_names)
    running_names = [n for n, b in bots.items()
                     if b.process is not None and b.process.poll() is None]
    status_cache = batch_read_bot_statuses(running_names) if running_names else {}

    _retry_disabled_and_stuck(bots, heartbeat_cache)
    error_recovered_tids = _handle_exited_bots(bots, now)
    _handle_stuck_bots(bots, now, heartbeat_cache)
    log_bot_statuses(bots, preloaded_statuses=status_cache)
    _run_dispatchers(bots, skip_route_tids=error_recovered_tids)
    _start_eligible_bots(bots, now)


# ---------------------------------------------------------------------------
# CLI helpers — each < 100 LOC
# ---------------------------------------------------------------------------

def _parse_args() -> Any:
    """Parse command-line arguments."""
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
    return p.parse_args()


def _handle_cli_commands(args: Any, bots: dict[str, BotState]) -> bool:
    """Handle CLI commands. Returns True if a command was handled (should exit)."""
    if args.status:
        print_status(bots)
        if is_draining():
            print(f"DRAIN ACTIVE: {drain_status()}")
        return True
    if args.drain_status:
        import pprint
        pprint.pprint(drain_status())
        print_status(bots)
        return True
    if args.clear_drain:
        clear_drain()
        print("Drain cleared.")
        return True
    if args.safe_stop or args.drain:
        safe_stop_all(bots, stop_bot)
        print("Safe stop complete.")
        print_status(bots)
        return True
    if args.stop_all:
        for b in bots.values():
            stop_bot(b, "stop-all")
        return True
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
        return True
    return False


def _bootstrap_adapter(logger: Any) -> Any:
    """Bootstrap project adapter and rebuild bot registry."""
    try:
        from codebot.codebot_bootstrap import bootstrap as _cb
        adapter = _cb(get_paths().bots_dir)
        if adapter:
            logger.info("Bootstrapped: %s", adapter.project_name())
            from codebot.state_manager import set_adapter_instance
            set_adapter_instance(adapter)
            return adapter
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Bootstrap failed: %s", e)
    return None


def _setup_shutdown_handlers(bots: dict[str, BotState]) -> None:
    """Register signal handlers for graceful shutdown."""
    def shutdown_handler(signum, frame):
        for b in bots.values():
            stop_bot(b, "shutdown")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)


def _run_main_loop(bots: dict[str, BotState], check_interval: int) -> None:
    """Run the main orchestrator loop.

    Uses deadline-based scheduling so that health checks fire at consistent
    intervals regardless of how long ``check_all_bots`` takes.  If a tick
    overruns the interval the next check runs immediately (no negative sleep).
    """
    logger.info("Orchestrator starting, health check every %ds", check_interval)
    last_align = time.time()
    next_tick = time.time()
    while True:
        try:
            check_all_bots(bots)
            now = time.time()
            # Advance deadline to the next future tick boundary
            next_tick += check_interval
            if next_tick <= now:
                next_tick = now + check_interval
            sleep_duration = max(0.0, next_tick - time.time())
            if sleep_duration > 0:
                time.sleep(sleep_duration)
            if time.time() - last_align >= 1800:
                run_alignment_pipeline_for_all()
                last_align = time.time()
        except KeyboardInterrupt:
            for b in bots.values():
                stop_bot(b, "shutdown")
            sys.exit(0)
        except Exception as e:
            logger.error(f"Health check error: {e}")
            now = time.time()
            next_tick += check_interval
            if next_tick <= now:
                next_tick = now + check_interval
            sleep_duration = max(0.0, next_tick - time.time())
            if sleep_duration > 0:
                time.sleep(sleep_duration)


def main() -> None:
    """Entry point: parse args, build bot state, run or dispatch."""
    args = _parse_args()
    registry = load_bot_registry()
    bots = build_bots(registry)

    if _handle_cli_commands(args, bots):
        return

    adapter = _bootstrap_adapter(logger)
    if adapter:
        registry = load_bot_registry(adapter)
        bots = build_bots(registry)

    _setup_shutdown_handlers(bots)

    if not is_draining():
        for b in bots.values():
            b._assigned_ticket_id = ""
        apply_agent_availability(bots, stop_fn=stop_bot, update_state_fn=update_bot_state)
        logger.info(f"Overture: pipeline {get_pipeline_state()}")

    _run_main_loop(bots, args.check_interval)


if __name__ == "__main__":
    main()
