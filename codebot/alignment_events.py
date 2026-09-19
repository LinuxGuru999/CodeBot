#!/usr/bin/env python3
"""Alignment Events — Exit event tracking for RL scoring.

Purpose
-------
Writes structured exit events for bots so the alignment pipeline can
score runs and produce RL rewards for prompt evolution.

Why
---
The orchestrator needs to record bot exits with context (exit code,
duration, heartbeat age, log size) so the RL engine can evaluate
alignment and trigger prompt optimization when needed.

Invariants
----------
- Events are written atomically via tmp+replace
- Events include all data needed for scoring
- Never raises — logs at WARNING on I/O error (fail-open)
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# Default paths
_STATE_DIR: Path = Path(".codebot/state")
_LOGS_DIR: Path = Path("logs")
_ALIGNMENT_EVENTS_DIR: Path = _STATE_DIR / "alignment_events"


def set_dirs(state_dir: Path, logs_dir: Path) -> None:
    """Configure directories for alignment event operations."""
    global _STATE_DIR, _LOGS_DIR, _ALIGNMENT_EVENTS_DIR
    _STATE_DIR = state_dir
    _LOGS_DIR = logs_dir
    _ALIGNMENT_EVENTS_DIR = _STATE_DIR / "alignment_events"
    _ALIGNMENT_EVENTS_DIR.mkdir(parents=True, exist_ok=True)


def _ensure_dirs() -> None:
    """Ensure alignment events directory exists."""
    _ALIGNMENT_EVENTS_DIR.mkdir(parents=True, exist_ok=True)


def _started_at_for(bot_name: str) -> Optional[float]:
    """Best-effort spawn time from state file; None if unavailable."""
    p = _STATE_DIR / f"{bot_name}.state.json"
    try:
        if p.exists():
            j = json.loads(p.read_text(encoding="utf-8"))
            v = j.get("started")
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    except Exception:
        pass
    return None


def _read_heartbeat(bot_name: str) -> float:
    """Read bot's last heartbeat timestamp. Returns 0 if missing/stale/corrupt."""
    hb = _STATE_DIR / f"{bot_name}.heartbeat"
    if not hb.exists():
        return 0.0
    txt = hb.read_text().strip()
    try:
        return float(txt)
    except (ValueError, OSError):
        pass
    try:
        import datetime
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


def _log_path(bot_name: str) -> Path:
    """Return path to bot's log file."""
    return _LOGS_DIR / f"{bot_name}.log"


def _log_bytes(bot_name: str) -> Optional[int]:
    """Get log file size in bytes, or None if unavailable."""
    lp = _log_path(bot_name)
    try:
        if lp.exists():
            return lp.stat().st_size
    except Exception:
        pass
    return None


def write_alignment_event(
    bot_name: str,
    exit_code: Optional[int],
    exit_reason: str,
    started_at: Optional[float] = None,
) -> None:
    """Write state/alignment_events/<bot>.exit.json atomically (fail-open).
    
    Called immediately after detecting a bot exit so ALIGNMENT_BOT can score
    the run and produce an RL reward. Never raises — logs at WARNING on I/O
    error so the health loop stays fail-open.
    
    Args:
        bot_name: Name of the bot
        exit_code: Exit code (0=success, non-zero=error, None=stuck/killed)
        exit_reason: Human-readable reason (e.g., 'clean', 'error', 'stuck')
        started_at: Unix timestamp when bot started (optional)
    """
    try:
        _ensure_dirs()
        now = time.time()
        started = started_at if started_at is not None else _started_at_for(bot_name)
        run_duration = (now - started) if started else None
        hb_age = None
        try:
            hb_raw = _read_heartbeat(bot_name)
            if hb_raw:
                hb_age = max(0.0, now - hb_raw)
        except Exception:
            pass
        log_bytes = _log_bytes(bot_name)
        
        payload: Dict[str, Any] = {
            "bot": bot_name,
            "exit_code": exit_code,
            "exit_reason": exit_reason,
            "exit_time": now,
            "exit_time_human": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "run_duration": round(run_duration, 2) if run_duration is not None else None,
            "started_at": started,
            "log_path": f"logs/{bot_name}.log",
            "stream_path": f"logs/{bot_name}.stream.json",
            "checkpoint_path": f"state/{bot_name}.checkpoint.json",
            "heartbeat_age_at_exit": round(hb_age, 1) if hb_age is not None else None,
            "log_bytes_at_exit": log_bytes,
            "processed": False,
            "processed_at": None,
            "version": 1,
        }
        
        target = _ALIGNMENT_EVENTS_DIR / f"{bot_name}.exit.json"
        tmp = Path(str(target) + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(target)
        logger.info(
            f"Alignment event: {bot_name} exit_code={exit_code} reason={exit_reason} duration={run_duration}"
        )
    except Exception as e:
        logger.warning(f"Failed to write alignment event for {bot_name}: {e}")


def list_pending_events() -> List[tuple]:
    """List all unprocessed alignment events.
    
    Returns:
        List of (event_path, event_data) tuples for events with processed=False.
    """
    pending = []
    try:
        _ensure_dirs()
        for event_file in _ALIGNMENT_EVENTS_DIR.glob("*.exit.json"):
            try:
                event = json.loads(event_file.read_text(encoding="utf-8"))
                if not event.get("processed", False):
                    pending.append((event_file, event))
            except Exception:
                pass
    except Exception:
        pass
    return pending


def mark_event_processed(
    event_file: Path,
    event: Dict[str, Any],
    score: float,
    reward: float,
    verdict: str,
) -> None:
    """Mark an alignment event as processed with scoring results.
    
    Args:
        event_file: Path to the event file
        event: Event data dict
        score: Alignment score (0.0-1.0)
        reward: RL reward value
        verdict: Verdict string (e.g., 'aligned', 'misaligned')
    """
    try:
        event["processed"] = True
        event["processed_at"] = time.time()
        event["score"] = score
        event["reward"] = reward
        event["verdict"] = verdict
        tmp = event_file.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(event, indent=2), encoding="utf-8")
        tmp.replace(event_file)
    except Exception as e:
        logger.warning(f"Failed to mark event processed: {e}")


def collect_reviewer_feedback_for_trigger(bot_name: str) -> List[Dict[str, Any]]:
    """Collect reviewer feedback for tickets assigned to this bot.
    
    Used when triggering prompt evolution after consecutive failures.
    
    Args:
        bot_name: Name of the bot
    
    Returns:
        List of feedback dicts with ticket_id, reviewer, file, description, recommendation.
    """
    feedback = []
    try:
        from codebot.ticket_engine import TicketStore
        store_path = _STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if not store_path.exists():
            return feedback
        ts = TicketStore(store_path)
        for ticket in ts._tickets.values():
            if ticket.assigned_agent == bot_name and ticket.rework_count >= 3 and ticket.reviewer_feedback:
                for fb in ticket.reviewer_feedback:
                    feedback.append({
                        "ticket_id": ticket.id,
                        "ticket_title": ticket.title,
                        "rework_count": ticket.rework_count,
                        "reviewer": fb.get("reviewer", ""),
                        "file": fb.get("file", ""),
                        "description": fb.get("description", ""),
                        "recommendation": fb.get("recommendation", ""),
                    })
    except Exception as e:
        logger.debug(f"Failed to collect reviewer feedback for {bot_name}: {e}")
    return feedback
