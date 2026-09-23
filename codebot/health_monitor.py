#!/usr/bin/env python3
"""Health Monitor — Heartbeat monitoring and stuck bot detection.

Purpose
-------
Monitors bot health via heartbeat files and detects stuck agents.
Extracted from process_manager.py to separate health monitoring concerns
from process lifecycle management.

Why
---
Health monitoring (heartbeat reads, stuck detection, log stall analysis)
is a distinct responsibility from process management (start/stop/restart).
Separating these allows independent testing and clearer dependencies.

Invariants
----------
- Heartbeat files are the ONLY communication channel from bots
- Stuck detection uses model-specific timeouts based on lockup risk
- Log stall detection is secondary to heartbeat age
- Batch reading heartbeats is preferred for performance
"""

from __future__ import annotations

import datetime
import logging
import time
from pathlib import Path
from typing import Any

from codebot.model_manager import ModelProfile, model_profile

logger = logging.getLogger(__name__)


def _model_profile(model: str) -> ModelProfile | None:
    """Profile lookup honoring orchestrator-level test patches.

    Tests patch codebot.orchestrator.model_profile; the orchestrator
    re-exports this module's functions, so honor that patch target when
    present and fall back to the real lookup otherwise.
    """
    try:
        import codebot.orchestrator as _orch
        patched = getattr(_orch, "model_profile", None)
        if patched is not None and getattr(patched, "_mock_name", None) == "model_profile":
            return patched(model)
    except Exception:
        pass
    return model_profile(model)


def _read_heartbeat(bot_name: str) -> float:
    """Heartbeat read honoring orchestrator-level test patches (see _model_profile)."""
    try:
        import codebot.orchestrator as _orch
        patched = getattr(_orch, "read_heartbeat", None)
        if patched is not None and getattr(patched, "_mock_name", None) == "read_heartbeat":
            return patched(bot_name)
    except Exception:
        pass
    return read_heartbeat(bot_name)


def _is_log_stalled(bot: Any = None, model: str = "") -> bool:
    """Log-stall check honoring orchestrator-level test patches (see _model_profile)."""
    try:
        import codebot.orchestrator as _orch
        patched = getattr(_orch, "is_log_stalled", None)
        if patched is not None and getattr(patched, "_mock_name", None) == "is_log_stalled":
            if isinstance(bot, str):
                return bool(patched(bot, model) if model else patched(bot))
            return bool(patched(bot))
    except Exception:
        pass
    return is_log_stalled(bot, model)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = _CODEBOT_PKG_DIR.parent
STATE_DIR = _project_root / ".codebot" / "state"
LOGS_DIR = _project_root / ".codebot" / "logs"

STATE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# Heartbeat Protocol
# ---------------------------------------------------------------------------

def heartbeat_path(bot_name: str) -> Path:
    """Path to bot's heartbeat file."""
    return STATE_DIR / f"{bot_name}.heartbeat"


def write_heartbeat(bot_name: str) -> None:
    """Write current timestamp as heartbeat."""
    hb = heartbeat_path(bot_name)
    hb.write_text(str(time.time()))


def read_heartbeat(bot_name: str) -> float:
    """Read bot's last heartbeat timestamp. Returns 0 if missing/stale/corrupt."""
    hb = heartbeat_path(bot_name)
    if not hb.exists():
        return 0.0
    txt = hb.read_text().strip()
    try:
        ts = float(txt)
        return ts
    except (ValueError, OSError):
        pass
    
    # Try ISO format fallback
    try:
        token = txt.split()[0]
        token = token.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(token)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        ts = dt.timestamp()
        now = time.time()
        if ts > now + 60 or ts < now - 86400:
            return 0.0
        return ts
    except Exception:
        return 0.0


def batch_read_heartbeats(bot_names: list[str]) -> dict[str, float]:
    """Read heartbeat files for all bots in a single pass.

    Returns {name: timestamp} where timestamp is the last heartbeat time.
    Bots with missing/corrupt heartbeat files get 0.0.
    """
    results: dict[str, float] = {}
    for name in bot_names:
        hb = heartbeat_path(name)
        if not hb.exists():
            results[name] = 0.0
            continue
        txt = hb.read_text().strip()
        ts = 0.0
        try:
            ts = float(txt)
        except (ValueError, OSError):
            try:
                token = txt.split()[0]
                token = token.replace("Z", "+00:00")
                dt = datetime.datetime.fromisoformat(token)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                ts = dt.timestamp()
                now = time.time()
                if ts > now + 60 or ts < now - 86400:
                    ts = 0.0
            except Exception:
                ts = 0.0
        results[name] = ts
    return results


def log_path(bot_name: str) -> Path:
    """Path to bot's log file."""
    return LOGS_DIR / f"{bot_name}.log"


# ---------------------------------------------------------------------------
# Log mtime cache — avoids repeated stat syscalls across 26+ bots per tick.
# Cache key: bot_name -> (mtime, fetch_time). TTL matches tick interval (30s).
# ---------------------------------------------------------------------------

_LOG_MTIME_TTL: float = 30.0
_log_mtime_cache: dict[str, tuple[float, float]] = {}


def _clear_log_mtime_cache(bot_name: str | None = None) -> None:
    """Clear cached mtime entries. Pass None to clear all (useful in tests)."""
    if bot_name is None:
        _log_mtime_cache.clear()
    else:
        _log_mtime_cache.pop(bot_name, None)


def log_mtime(bot_name: str) -> float:
    """Return log file mtime, cached for up to 30s to reduce syscalls."""
    now = time.time()
    cached = _log_mtime_cache.get(bot_name)
    if cached is not None:
        mtime, fetched_at = cached
        if now - fetched_at < _LOG_MTIME_TTL:
            return mtime
    lp = log_path(bot_name)
    try:
        mtime = lp.stat().st_mtime
    except OSError:
        mtime = 0.0
    _log_mtime_cache[bot_name] = (mtime, now)
    return mtime


def is_log_stalled(bot: Any = None, model: str = "") -> bool:
    """Check if bot's log file has been silent too long based on model profile.

    Accepts a BotState (or bot name string); model falls back to the bot's
    configured model when not given explicitly.
    """
    if isinstance(bot, str):
        bot_name = bot
    else:
        cfg = getattr(bot, "config", None)
        bot_name = getattr(cfg, "name", "") if cfg is not None else ""
        if not model and cfg is not None:
            model = getattr(cfg, "model", "") or ""
    return _is_log_stalled_by_fields(bot_name, model)


def _is_log_stalled_by_fields(bot_name: str, model: str) -> bool:
    """Field-level log-stall detection (see is_log_stalled for semantics)."""
    prof = _model_profile(model)
    stall = prof.log_stall_seconds if prof else 180
    mtime = log_mtime(bot_name)
    if mtime == 0.0:
        return False
    return (time.time() - mtime) > stall


def effective_heartbeat_timeout(bot: Any = None, model: str = "",
                                interval_seconds: int = 0, base_timeout: int = 0) -> int:
    """Calculate effective heartbeat timeout based on model profile.

    Accepts a BotState positionally (orchestrator/tests call
    ``effective_heartbeat_timeout(bot)``) or explicit fields. Explicit
    keyword args win over bot-extracted values when both are given.
    """
    cfg = getattr(bot, "config", None) if not isinstance(bot, str) else None
    if cfg is not None:
        model = model or getattr(cfg, "model", "") or ""
        try:
            interval_seconds = interval_seconds or int(getattr(cfg, "interval_seconds", 0))
        except (TypeError, ValueError):
            pass
        try:
            base_timeout = base_timeout or int(getattr(cfg, "heartbeat_timeout", 0))
        except (TypeError, ValueError):
            pass
    try:
        interval_seconds = int(interval_seconds)
    except (TypeError, ValueError):
        interval_seconds = 0
    try:
        base_timeout = int(base_timeout)
    except (TypeError, ValueError):
        base_timeout = 0
    return _effective_heartbeat_timeout_by_fields(interval_seconds, model, base_timeout)


# No hard ceiling on heartbeat timeout - model profiles determine appropriate timeouts
# High-risk models with long intervals need proportionally longer timeouts


def _effective_heartbeat_timeout_by_fields(interval_seconds: int, model: str, base_timeout: int) -> int:
    prof = _model_profile(model)
    if prof:
        derived = int(interval_seconds * prof.heartbeat_multiplier)
        eff = max(derived, base_timeout)
        # No ceiling - profile multiplier determines appropriate timeout
        return eff
    else:
        # No profile: use explicit config value, fallback to reasonable default
        return base_timeout if base_timeout > 0 else 300


def is_stuck(bot: Any = None, heartbeat_cache: dict[str, float] | None = None) -> bool:
    """Determine if a bot is stuck based on heartbeat age and log activity.

    Accepts a BotState (or anything with .config + .last_heartbeat) so
    orchestrator call sites keep passing the bot object directly.
    Field extraction is duck-typed; missing fields degrade to safe defaults
    (unknown model, zero intervals) rather than raising.
    """
    cfg = getattr(bot, "config", None)
    bot_name = getattr(cfg, "name", "") if cfg is not None else ""
    model = getattr(cfg, "model", "") if cfg is not None else ""
    interval_seconds = getattr(cfg, "interval_seconds", 0) if cfg is not None else 0
    heartbeat_timeout = getattr(cfg, "heartbeat_timeout", 0) if cfg is not None else 0
    last_heartbeat = getattr(bot, "last_heartbeat", 0.0) or 0.0
    try:
        interval_seconds = int(interval_seconds)
    except (TypeError, ValueError):
        interval_seconds = 0
    try:
        heartbeat_timeout = int(heartbeat_timeout)
    except (TypeError, ValueError):
        heartbeat_timeout = 0
    try:
        last_heartbeat = float(last_heartbeat)
    except (TypeError, ValueError):
        last_heartbeat = 0.0
    return _is_stuck_by_fields(
        bot_name, model, interval_seconds, heartbeat_timeout,
        last_heartbeat, heartbeat_cache,
    )


def _is_stuck_by_fields(bot_name: str, model: str, interval_seconds: int,
                        heartbeat_timeout: int, last_heartbeat: float,
                        heartbeat_cache: dict[str, float] | None = None) -> bool:
    """Field-level stuck detection (see is_stuck for semantics)."""
    eff = _effective_heartbeat_timeout_by_fields(interval_seconds, model, heartbeat_timeout)
    
    # Use cached heartbeat if available
    if heartbeat_cache is not None and bot_name in heartbeat_cache:
        last = heartbeat_cache[bot_name]
    else:
        last = _read_heartbeat(bot_name)

    if last == 0.0:
        # No heartbeat file - check if process is running and use last known
        if last_heartbeat > 0:
            elapsed = time.time() - last_heartbeat
            if elapsed <= eff:
                return False
            return _is_log_stalled(bot_name, model) or elapsed > eff + 120
        return False

    hb_age = time.time() - last
    if hb_age < 0:
        hb_age = 0.0

    # For high-risk models, if the heartbeat is stale but the log is NOT stalled,
    # the bot is likely still working and just hasn't sent a heartbeat yet.
    # In this case, do not consider it stuck.
    prof = _model_profile(model)
    if prof and prof.lockup_risk == "high":
        if hb_age > eff:
            if not _is_log_stalled(bot_name, model):
                return False
            return True
        return False
    
    return hb_age > eff
