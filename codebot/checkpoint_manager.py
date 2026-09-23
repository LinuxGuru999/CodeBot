#!/usr/bin/env python3
"""Checkpoint Manager — Atomic state persistence for bot recovery.

Purpose
-------
Provides atomic read/write of bot checkpoint files via tmp+replace.
Checkpoints enable crash recovery and worker handoff between sessions.

Why
---
Bots are autonomous agents that can timeout, crash, or exceed limits.
Checkpoints capture progress (completed steps, current task, queue) so
a replacement agent can resume without redoing work.

Invariants
----------
- All writes are atomic (tmp file then os.replace)
- Checkpoints stay under 4KB limit
- Corrupt checkpoints fall back to .bak file
- Missing checkpoints return None (caller handles gracefully)
"""

import json
import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Dict, Iterator, Optional

from codebot.file_lock import LOCK_EX, LOCK_UN, flock

logger = logging.getLogger(__name__)

# Default paths (overridden by set_state_dir)
_STATE_DIR: Path = Path(".codebot/state")
_MAX_CHECKPOINT_BYTES = 4096
_MAX_READ_CHECKPOINT_BYTES = 8192  # hard memory ceiling for reads (2× write limit)


def set_state_dir(state_dir: Path) -> None:
    """Configure the state directory for checkpoint operations."""
    global _STATE_DIR
    _STATE_DIR = state_dir
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def checkpoint_path(bot_name: str) -> Path:
    """Return the path to a bot's checkpoint file.

    Args:
        bot_name: Bot directory/name whose checkpoint location is needed.

    Returns:
        Path under ``_STATE_DIR`` for ``{bot_name}.checkpoint.json``. No I/O
        is performed; the file may not exist.
    """
    return _STATE_DIR / f"{bot_name}.checkpoint.json"


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON data to *path* atomically via tmp-file + ``os.replace``.

    Serialises *data* as indented JSON (or ``str(data)`` for non-dict/list)
    to ``{name}.{pid}.tmp`` alongside *path* and renames it over *path*,
    guaranteeing readers never observe a half-written file. Used for both
    checkpoint and state files.

    Args:
        path: Destination file path.
        data: JSON-serialisable payload (dict/list) or string fallback.

    Returns:
        None. Raises ``OSError`` on I/O failure.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else str(data))
    tmp.replace(path)


def _bounded_read_text(path: Path, max_bytes: int) -> Optional[str]:
    """Read *path* as UTF-8 text, returning ``None`` if the file exceeds *max_bytes*.

    Opens in binary mode and reads at most ``max_bytes + 1`` bytes so that
    oversize files are detected without loading them entirely into memory.
    This is TOCTOU-safe: unlike a ``stat()`` guard followed by ``read_text()``,
    the kernel enforces the byte ceiling on the file descriptor itself.

    Raises ``OSError`` / ``UnicodeDecodeError`` on I/O or encoding failures
    (callers already handle these).
    """
    with open(path, "rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        return None
    return data.decode("utf-8")


@contextmanager
def _state_write_lock(bot_name: str) -> Iterator[None]:
    """Acquire an exclusive advisory lock on a bot's state file.

    Creates/opens ``{bot_name}.state.lock`` under ``_STATE_DIR`` and holds
    an ``LOCK_EX`` flock for the duration of the ``with`` block, serialising
    concurrent read-modify-write cycles (e.g. ``update_bot_state`` vs
    ``_manifest_restart_record``). Always releases the lock even on exception.

    Args:
        bot_name: Bot whose state is being guarded.

    Yields:
        None — the critical section runs inside the ``with`` block.

    Invariants:
        - Routes through ``codebot.locks.flock`` (Unix fcntl / Windows msvcrt /
          documented no-op). See codebot.locks docstring for platform limits:
          byte-range (not whole-file) locks on Windows, LOCK_SH treated as
          LOCK_EX there, fail-open no-op with RuntimeWarning elsewhere.
        - Lock file is created with ``a+`` so it persists across calls.
    """
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = _STATE_DIR / f"{bot_name}.state.lock"
    lock_fd = None
    try:
        lock_fd = lock_path.open("a+", encoding="utf-8")
        flock(lock_fd.fileno(), LOCK_EX)
        yield
    finally:
        if lock_fd:
            flock(lock_fd.fileno(), LOCK_UN)
            lock_fd.close()


def write_checkpoint_handoff(bot_name: str, payload: Dict[str, Any]) -> None:
    """Write checkpoint atomically with standard metadata fields.
    
    Args:
        bot_name: Name of the bot (used for filename)
        payload: Checkpoint data (must be JSON-serializable)
    
    The payload is augmented with:
        - bot: bot_name (if not present)
        - updated_at: current Unix timestamp
        - updated_at_human: ISO format timestamp
    """
    p = checkpoint_path(bot_name)
    payload = dict(payload)
    payload.setdefault("bot", bot_name)
    payload.setdefault("updated_at", time.time())
    payload.setdefault("updated_at_human", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(payload["updated_at"])))
    _write_json_atomic(p, payload)


def init_checkpoint(bot_name: str, scan_iteration: int = 0) -> None:
    """Initialize a new checkpoint file if it doesn't exist.
    
    Args:
        bot_name: Name of the bot
        scan_iteration: Initial iteration counter (default 0)
    """
    p = checkpoint_path(bot_name)
    if p.exists():
        return
    write_checkpoint_handoff(bot_name, {
        "scan_iteration": scan_iteration,
        "current_task": None,
        "completed": [],
        "queue": [],
        "findings_so_far": [],
        "reason": "init",
    })


def checkpoint_backup_path(path: Path) -> Path:
    if path.suffix == ".json":
        return path.with_name(f"{path.stem}.checkpoint.bak")
    return Path(f"{path}.bak")


def read_checkpoint(bot_name: str) -> Optional[Dict[str, Any]]:
    p = checkpoint_path(bot_name)
    bak = checkpoint_backup_path(p)
    
    if not p.exists():
        if bak.exists():
            try:
                # Bounded read — TOCTOU-safe, no full-file load
                raw = _bounded_read_text(bak, _MAX_READ_CHECKPOINT_BYTES)
                if raw is None:
                    logger.warning(f"Backup checkpoint for '{bot_name}' exceeds {_MAX_READ_CHECKPOINT_BYTES}B — rejecting")
                    return None
                data = json.loads(raw)
                if isinstance(data, dict):
                    logger.info(f"Restored last-good checkpoint for '{bot_name}' from .bak")
                    return data
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        return None
    
    # CRITICAL: Use bounded read to prevent memory exhaustion from malicious/corrupt files.
    # This is TOCTOU-safe as the kernel enforces the byte ceiling on the file descriptor.
    try:
        raw = _bounded_read_text(p, _MAX_READ_CHECKPOINT_BYTES)
        if raw is None:
            logger.warning(f"Checkpoint for '{bot_name}' exceeds {_MAX_READ_CHECKPOINT_BYTES}B — rejecting to prevent memory exhaustion")
            return None
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(f"Checkpoint corrupt for '{bot_name}': {exc} — falling back to .bak")
        try:
            quarantine = p.parent / "checkpoint_quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            p.replace(quarantine / f"{p.stem}-corrupt.json")
        except OSError:
            try:
                p.unlink()
            except OSError:
                pass
        if bak.exists():
            try:
                # Size-check backup before reading
                if bak.stat().st_size > _MAX_CHECKPOINT_BYTES:
                    logger.warning(f"Backup checkpoint for '{bot_name}' exceeds {_MAX_CHECKPOINT_BYTES}B — rejecting")
                    return None
                fallback_raw = bak.read_text(encoding="utf-8")
                fallback_data = json.loads(fallback_raw)
                if isinstance(fallback_data, dict):
                    return fallback_data
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        return None
    except Exception:
        return None
    
    if not isinstance(data, dict):
        logger.warning(f"Checkpoint for '{bot_name}' is not a JSON object — falling back to .bak")
        try:
            quarantine = p.parent / "checkpoint_quarantine"
            quarantine.mkdir(parents=True, exist_ok=True)
            p.replace(quarantine / f"{p.stem}-non-dict.json")
        except OSError:
            try:
                p.unlink()
            except OSError:
                pass
        if bak.exists():
            try:
                # Size-check backup before reading
                if bak.stat().st_size > _MAX_CHECKPOINT_BYTES:
                    logger.warning(f"Backup checkpoint for '{bot_name}' exceeds {_MAX_CHECKPOINT_BYTES}B — rejecting")
                    return None
                fallback_raw = bak.read_text(encoding="utf-8")
                fallback_data = json.loads(fallback_raw)
                if isinstance(fallback_data, dict):
                    return fallback_data
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        return None
    
    # Size check already done via stat() above, but keep this as defense-in-depth
    if len(raw.encode("utf-8")) > _MAX_CHECKPOINT_BYTES:
        logger.warning(f"Checkpoint for '{bot_name}' exceeds {_MAX_CHECKPOINT_BYTES}B after read — this should not happen")
        return None
    
    try:
        bak.write_text(raw, encoding="utf-8")
    except OSError:
        pass
    
    return data


def _read_state_file(bot_name: str) -> Dict[str, Any]:
    """Read bot's state file (separate from checkpoint).
    
    Args:
        bot_name: Name of the bot
    
    Returns:
        Parsed state dict, or empty dict if missing/corrupt.
    """
    state_file = _STATE_DIR / f"{bot_name}.state.json"
    try:
        if state_file.exists():
            return json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"_state_error": "corrupt"}
    return {}


def _write_state_file(bot_name: str, data: Dict[str, Any]) -> None:
    """Write bot's state file atomically.
    
    Args:
        bot_name: Name of the bot
        data: State data to write
    """
    state_file = _STATE_DIR / f"{bot_name}.state.json"
    _write_json_atomic(state_file, data)


def update_bot_state(bot_name: str, status: str, restart_count: int = 0,
                     consecutive_errors: int = 0, next_run_at: float = 0.0) -> None:
    """Update bot's state file with current status.
    
    Args:
        bot_name: Name of the bot
        status: Current status string (e.g., 'running', 'waiting', 'disabled')
        restart_count: Number of restarts in current hour
        consecutive_errors: Count of consecutive error exits
        next_run_at: Unix timestamp for next scheduled run
    """
    state_file = _STATE_DIR / f"{bot_name}.state.json"
    try:
        with _state_write_lock(bot_name):
            try:
                data = json.loads(state_file.read_text()) if state_file.exists() else {}
            except (json.JSONDecodeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data["status"] = status
            data["last_update"] = time.time()
            data["restart_count"] = restart_count
            data["consecutive_errors"] = consecutive_errors
            data["next_run_at"] = next_run_at
            _write_json_atomic(state_file, data)
    except Exception as e:
        logger.error(f"Failed to update state for '{bot_name}': {e}")


def _manifest_restart_budget_exceeded(manifest: Dict[str, Any], now: float) -> tuple:
    """Check if bot has exceeded its restart budget within the last hour.
    
    Args:
        manifest: Bot manifest with name and max_restarts
        now: Current Unix timestamp
    
    Returns:
        Tuple of (exceeded: bool, reason: str)
    """
    name = manifest.get("name", "")
    max_restarts = manifest.get("max_restarts", 5)
    try:
        max_r = int(max_restarts)
    except Exception:
        max_r = 5
    if max_r <= 0:
        return False, ""
    state = _read_state_file(name)
    if state.get("_state_error") == "corrupt":
        return True, "state-corrupt"
    timestamps = state.get("restart_timestamps", [])
    if not isinstance(timestamps, list):
        timestamps = []
    recent = [float(ts) for ts in timestamps if isinstance(ts, (int, float)) and (now - float(ts)) < 3600]
    if len(recent) >= max_r:
        return True, f"restart-budget-exceeded ({len(recent)}/{max_r} in 1h)"
    return False, ""


def _manifest_error_disabled(manifest: Dict[str, Any], max_consecutive: int = 3) -> tuple:
    """Check if bot is disabled due to consecutive errors.
    
    Args:
        manifest: Bot manifest with name
        max_consecutive: Maximum allowed consecutive errors (default 3)
    
    Returns:
        Tuple of (disabled: bool, reason: str)
    """
    name = manifest.get("name", "")
    state = _read_state_file(name)
    if state.get("_state_error") == "corrupt":
        return True, "state-corrupt"
    consecutive = state.get("consecutive_errors", 0)
    try:
        c = int(consecutive)
    except Exception:
        c = 0
    if c >= max_consecutive:
        return True, f"error-disabled ({c} consecutive errors)"
    return False, ""


def _manifest_restart_record(name: str, now: float) -> None:
    """Record a restart timestamp for budget tracking.
    
    Args:
        name: Bot name
        now: Current Unix timestamp
    """
    with _state_write_lock(name):
        state = _read_state_file(name)
        timestamps = state.get("restart_timestamps", [])
        if not isinstance(timestamps, list):
            timestamps = []
        timestamps = [float(t) for t in timestamps if isinstance(t, (int, float))]
        timestamps.append(now)
        state["restart_timestamps"] = timestamps
        state["restart_count"] = len([t for t in timestamps if (now - t) < 3600])
        state["last_update"] = now
        state["last_restart"] = now
        _write_state_file(name, state)


def is_manifest_restart_budget_exceeded(manifest: Dict[str, Any], now: float) -> bool:
    """Public API: Check if restart budget exceeded."""
    return _manifest_restart_budget_exceeded(manifest, now)[0]


def is_manifest_error_disabled(manifest: Dict[str, Any], max_consecutive: int = 3) -> bool:
    """Public API: Check if bot is error-disabled."""
    return _manifest_error_disabled(manifest, max_consecutive)[0]
