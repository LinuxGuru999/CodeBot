"""Runtime bootstrap, shutdown handlers, and main loop for the orchestrator.

Extracted from orchestrator.py to reduce its size and separate
runtime lifecycle concerns from coordination logic.
"""
from __future__ import annotations

import logging
import os
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("orchestrator")
STALE_DYNAMIC_WORKER_SECONDS = 600
_DYNAMIC_WORKER_NAME = re.compile(r".+-\d+$")
_DYNAMIC_WORKER_STATE_SUFFIXES = (
    "heartbeat",
    "state.json",
    "status.json",
    "checkpoint.json",
)


def _orchestrator_pid_path() -> Path:
    from codebot.state_manager import get_paths

    return get_paths().state_dir / ".orchestrator.pid"


def _process_is_running(pid: int) -> bool:
    try:
        _ = os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _claim_orchestrator_pid() -> Path:
    pid_path = _orchestrator_pid_path()
    pid_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        existing_pid = 0
    except ValueError:
        existing_pid = 0

    current_pid = os.getpid()
    if existing_pid and existing_pid != current_pid and _process_is_running(existing_pid):
        raise RuntimeError(f"orchestrator already running with PID {existing_pid}")

    pid_path.write_text(f"{current_pid}\n", encoding="utf-8")
    # Set restrictive permissions (0o600) to prevent PID spoofing attacks (Feedback #48)
    try:
        pid_path.chmod(0o600)
    except OSError as e:
        logger.warning("Failed to chmod orchestrator PID file %s: %s", pid_path, e)
    logger.info("Orchestrator PID registered: %s", current_pid)
    return pid_path


def _release_orchestrator_pid(pid_path: Path) -> None:
    try:
        recorded_pid = int(pid_path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        return
    except ValueError:
        return

    if recorded_pid == os.getpid():
        pid_path.unlink(missing_ok=True)
        logger.info("Orchestrator PID released: %s", recorded_pid)


def prune_stale_dynamic_bot_state(state_dir: Path, *, now: float | None = None) -> int:
    """Remove stale state files for retired dynamically named workers."""
    current_time = time.time() if now is None else now
    names = {
        path.name.removesuffix(".heartbeat")
        for path in state_dir.glob("*.heartbeat")
    }
    names.update(
        path.name.removesuffix(".state.json")
        for path in state_dir.glob("*.state.json")
    )
    names.update(
        path.name.removesuffix(".status.json")
        for path in state_dir.glob("*.status.json")
    )
    names.update(
        path.name.removesuffix(".checkpoint.json")
        for path in state_dir.glob("*.checkpoint.json")
    )
    removed = 0
    for name in names:
        if _DYNAMIC_WORKER_NAME.fullmatch(name) is None:
            continue
        heartbeat_path = state_dir / f"{name}.heartbeat"
        try:
            heartbeat = float(heartbeat_path.read_text(encoding="utf-8").strip())
        except FileNotFoundError:
            heartbeat = 0.0
        except ValueError:
            heartbeat = 0.0
        if current_time - heartbeat < STALE_DYNAMIC_WORKER_SECONDS:
            continue
        for suffix in _DYNAMIC_WORKER_STATE_SUFFIXES:
            path = state_dir / f"{name}.{suffix}"
            if path.exists():
                path.unlink()
                removed += 1
    return removed


def bootstrap_adapter(logger: Any, get_paths_fn: Any) -> Any:
    """Bootstrap project adapter and rebuild bot registry."""
    try:
        from codebot.codebot_bootstrap import bootstrap as _cb
        adapter = _cb(get_paths_fn().bots_dir)
        if adapter:
            logger.info("Bootstrapped: %s", adapter.project_name())
            from codebot.state_manager import set_adapter_instance
            set_adapter_instance(adapter)
            try:
                from codebot import rl_engine
                rl_engine.set_project_adapter(adapter)
            except Exception:
                pass
            return adapter
    except ImportError:
        pass
    except Exception as e:
        logger.warning("Bootstrap failed: %s", e)
    return None


_shutdown_requested = False


def setup_shutdown_handlers(bots: dict, stop_bot_fn: Any) -> None:
    global _shutdown_requested
    _shutdown_requested = False

    def shutdown_handler(signum, frame):
        global _shutdown_requested
        _shutdown_requested = True

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGCHLD, signal.SIG_IGN)


def run_main_loop(
    bots: dict,
    check_interval: int,
    *,
    check_all_bots_fn: Any,
    stop_bot_fn: Any,
    run_alignment_pipeline_for_all_fn: Any,
) -> None:
    """Run the main orchestrator loop.

    Uses deadline-based scheduling so that health checks fire at consistent
    intervals regardless of how long ``check_all_bots`` takes.  If a tick
    overruns the interval the next check runs immediately (no negative sleep).

    Also spawns the discovery daemon (outside the scheduler) so audit roles
    scan continuously without competing for scheduler slots.
    """
    logger.info("Orchestrator starting, health check every %ds", check_interval)
    pid_path = _claim_orchestrator_pid()
    disc_daemon = None
    try:
        pruned_count = prune_stale_dynamic_bot_state(pid_path.parent)
        if pruned_count:
            logger.info("Pruned %d stale dynamic-worker state files", pruned_count)
        try:
            from codebot.discovery_daemon import DiscoveryDaemon, DISCOVERY_ROLES as _DISC_ROLES
            disc_daemon = DiscoveryDaemon(bots, state_dir=pid_path.parent)
            disc_daemon.start()
            logger.info("Discovery daemon started (roles=%d, max_concurrent=%d)",
                        len(_DISC_ROLES), disc_daemon.max_concurrent)
        except Exception as e:
            logger.warning("Discovery daemon failed to start: %s", e)
            disc_daemon = None
        last_align = time.time()
        next_tick = time.time()
        while not _shutdown_requested:
            try:
                check_all_bots_fn(bots)
                now = time.time()
                next_tick += check_interval
                if next_tick <= now:
                    next_tick = now + check_interval
                sleep_duration = max(0.0, next_tick - time.time())
                while sleep_duration > 0 and not _shutdown_requested:
                    step = min(sleep_duration, 1.0)
                    time.sleep(step)
                    sleep_duration -= step
                if time.time() - last_align >= 1800:
                    t = threading.Thread(
                        target=run_alignment_pipeline_for_all_fn,
                        name="rl-pipeline-tick",
                        daemon=True,
                    )
                    t.start()
                    last_align = time.time()
            except KeyboardInterrupt:
                break
            except Exception as e:
                logger.error(f"Health check error: {e}")
                now = time.time()
                next_tick += check_interval
                if next_tick <= now:
                    next_tick = now + check_interval
                sleep_duration = max(0.0, next_tick - time.time())
                while sleep_duration > 0 and not _shutdown_requested:
                    step = min(sleep_duration, 1.0)
                    time.sleep(step)
                    sleep_duration -= step
        for b in bots.values():
            stop_bot_fn(b, "shutdown")
        if disc_daemon is not None:
            try:
                disc_daemon.stop(timeout=3.0)
            except Exception:
                pass
    finally:
        if disc_daemon is not None:
            try:
                disc_daemon.stop(timeout=1.0)
            except Exception:
                pass
        _release_orchestrator_pid(pid_path)
