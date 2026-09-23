#!/usr/bin/env python3
"""Bot performance metrics tracking.

Purpose
-------
Collects and persists bot run metrics including success/failure counts,
token consumption, run duration, and restart statistics.

Why
---
The orchestrator needs visibility into bot performance to make informed
decisions about model rotation, rate limiting, and capacity planning.
Persistent metrics enable trend analysis and automated health monitoring.

Invariants
----------
- Metrics are persisted atomically via tmp+replace to prevent corruption
- Run history is capped to prevent unbounded growth (max 500 runs per bot)
- File size is bounded (max 50KB) with iterative pruning if exceeded
- Serializes fully to avoid corrupt JSON on crash
- Append-only JSONL for fast writes; periodic compaction into snapshot
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

_CODEBOT_PKG_DIR = Path(__file__).parent
_PROJECT_ROOT = _CODEBOT_PKG_DIR.parent
_DEFAULT_STATE_DIR = _PROJECT_ROOT / ".codebot" / "state"

# Configuration constants
_MAX_RUNS_PER_BOT = 500
_MAX_METRICS_FILE_BYTES = 50 * 1024  # 50KB
_COMPACT_EVERY_N_EVENTS = 100
_METRICS_WINDOW_DAYS = 30

_state_dir: Path | None = None


def set_state_dir(state_dir: Path) -> None:
    """Configure the state directory for metrics operations."""
    global _state_dir
    _state_dir = Path(state_dir)


def _resolve_state_dir() -> Path:
    """Resolve the state directory, creating it if needed."""
    if _state_dir is not None:
        return _state_dir
    env = os.environ.get("CODEBOT_STATE_DIR") or os.environ.get("CODEBOT_PROJECT_ROOT")
    if env:
        p = Path(env)
        if not p.name == "state":
            p = p / ".codebot" / "state"
        if p.exists():
            return p
    return _DEFAULT_STATE_DIR


def _get_metrics_path() -> Path:
    """Get path to the metrics snapshot file."""
    return _resolve_state_dir() / "bot_metrics.json"


def _get_events_path() -> Path:
    """Get path to the JSONL events file."""
    return _resolve_state_dir() / "bot_metrics_events.jsonl"


def _load_snapshot() -> dict[str, Any]:
    """Load the metrics snapshot from disk.
    
    Returns empty dict on any error (missing file, invalid JSON, non-dict).
    """
    path = _get_metrics_path()
    try:
        if not path.exists():
            return {}
        text = path.read_text(encoding="utf-8")
        data = json.loads(text)
        if not isinstance(data, dict):
            return {}
        return data
    except (json.JSONDecodeError, OSError):
        return {}


def _read_events() -> list[dict[str, Any]]:
    """Read and parse JSONL events file.
    
    Skips invalid JSON lines. Returns empty list on OSError.
    """
    path = _get_events_path()
    events: list[dict[str, Any]] = []
    try:
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
                if isinstance(event, dict):
                    events.append(event)
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return events


def _count_event_lines() -> int:
    """Count lines in events file without parsing JSON.
    
    Returns 0 on OSError or missing file.
    """
    path = _get_events_path()
    try:
        if not path.exists():
            return 0
        with open(path, encoding="utf-8") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


def _apply_event(entry: dict[str, Any], event: dict[str, Any]) -> None:
    """Apply a single event to a bot's metrics entry.
    
    Updates runs list (capped), success/failure counts, tokens, and duration.
    The cap is applied BEFORE appending to ensure the list never exceeds max.
    """
    now = event.get("t", time.time())
    alive = event.get("a", False)
    exit_code = event.get("e")
    tokens = event.get("tk", 0) or 0
    started_at = event.get("s")
    
    # Cap runs list BEFORE appending (critical for CB-3810728-0230)
    runs = entry.get("runs", [])
    if len(runs) >= _MAX_RUNS_PER_BOT:
        runs = runs[-(_MAX_RUNS_PER_BOT - 1):]
    runs.append(now)
    entry["runs"] = runs
    
    # Update counters only for completed runs (not alive)
    if not alive and exit_code is not None:
        if exit_code == 0:
            entry["successes"] = entry.get("successes", 0) + 1
        else:
            entry["failures"] = entry.get("failures", 0) + 1
    
    entry["total_tokens"] = entry.get("total_tokens", 0) + tokens
    
    # Calculate duration if started_at is provided
    if started_at is not None and not alive:
        duration = now - started_at
        if duration > 0:
            entry["total_duration_s"] = entry.get("total_duration_s", 0.0) + duration


def _apply_events_to_data(
    data: dict[str, Any], events: list[dict[str, Any]]
) -> dict[str, Any]:
    """Apply a list of events to the metrics data.
    
    Skips events with empty bot names.
    """
    for event in events:
        bot_name = event.get("b", "")
        if not bot_name:
            continue
        if bot_name not in data:
            data[bot_name] = {
                "runs": [],
                "successes": 0,
                "failures": 0,
                "total_tokens": 0,
                "total_duration_s": 0.0,
            }
        _apply_event(data[bot_name], event)
    return data


def _prune_snapshot_size(data: dict[str, Any]) -> dict[str, Any]:
    """Prune snapshot data to fit within _MAX_METRICS_FILE_BYTES.
    
    Iteratively halves max_runs per bot until serialized size is within budget.
    If reducing runs to 1 still doesn't fit, the data is returned as-is
    (cannot be made smaller without removing bots, which we avoid).
    """
    max_runs = _MAX_RUNS_PER_BOT
    prev_size = None
    while True:
        serialized = json.dumps(data, indent=2)
        size = len(serialized.encode("utf-8"))
        if size <= _MAX_METRICS_FILE_BYTES:
            break
        # Prevent infinite loop if size doesn't change after trimming
        if size == prev_size:
            break
        prev_size = size
        # Halve max_runs and trim all bots
        max_runs = max(1, max_runs // 2)
        changed = False
        for bot_name, entry in data.items():
            runs = entry.get("runs", [])
            if len(runs) > max_runs:
                entry["runs"] = runs[-max_runs:]
                changed = True
        # If no changes were made, we've reached minimum runs (1) for all bots
        if not changed:
            break
    return data


def _compact() -> None:
    """Compact JSONL events into the snapshot file.
    
    Merges events into snapshot, prunes if needed, writes atomically,
    then clears events file.
    """
    try:
        snapshot = _load_snapshot()
        events = _read_events()
        
        if not events:
            return
        
        # Merge events into snapshot
        merged = _apply_events_to_data(snapshot, events)
        
        # Prune if necessary
        pruned = _prune_snapshot_size(merged)
        
        # Write snapshot atomically
        metrics_path = _get_metrics_path()
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = metrics_path.with_suffix(f".tmp.{os.getpid()}")
        serialized = json.dumps(pruned, indent=2)
        tmp_path.write_text(serialized, encoding="utf-8")
        tmp_path.replace(metrics_path)
        
        # Clear events file atomically
        events_path = _get_events_path()
        try:
            tmp_events = events_path.with_suffix(f".tmp.{os.getpid()}")
            tmp_events.write_text("", encoding="utf-8")
            tmp_events.replace(events_path)
        except OSError as e:
            logger.warning("Failed to clear events file after compaction: %s", e)
        
        logger.debug("Compacted %d events into snapshot", len(events))
    except Exception as e:
        logger.warning("Compaction failed: %s", e)


def _append_event(event: dict[str, Any]) -> None:
    """Append an event to the JSONL events file.
    
    Uses O_APPEND for atomic concurrent appends.
    """
    events_path = _get_events_path()
    events_path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(event, separators=(",", ":")) + "\n"
    fd = os.open(str(events_path), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o644)
    try:
        os.write(fd, line.encode("utf-8"))
    finally:
        os.close(fd)


def _load_merged_data() -> dict[str, Any]:
    """Load snapshot and merge with pending events.
    
    Used by read operations to get current state.
    """
    snapshot = _load_snapshot()
    events = _read_events()
    return _apply_events_to_data(snapshot, events)


def record_bot_metric(
    name: str,
    alive: bool,
    exit_code: Optional[int] = None,
    tokens_this_run: int = 0,
    started_at: Optional[float] = None,
) -> None:
    """Record a bot run metric.
    
    Updates success/failure counts, total tokens, and total duration.
    Appends to JSONL events file for fast writes; compaction happens
    periodically when event count reaches threshold.
    
    Args:
        name: Bot name
        alive: Whether bot process is still running
        exit_code: Exit code if process ended (None if still running)
        tokens_this_run: Token consumption for this run
        started_at: Unix timestamp when bot started
    """
    try:
        now = time.time()
        event = {
            "t": now,
            "b": name,
            "a": alive,
            "e": exit_code,
            "tk": tokens_this_run,
            "s": started_at,
        }
        _append_event(event)
        
        # Check if compaction is needed
        line_count = _count_event_lines()
        if line_count >= _COMPACT_EVERY_N_EVENTS:
            _compact()
    except Exception as e:
        logger.warning("Failed to record bot metric for %s: %s", name, e)


def get_bot_metrics(name: str) -> Optional[dict[str, Any]]:
    """Get metrics for a specific bot.
    
    Args:
        name: Bot name
        
    Returns:
        Metrics dict or None if not found.
    """
    try:
        data = _load_merged_data()
        return data.get(name)
    except Exception:
        return None


def get_all_metrics() -> dict[str, Any]:
    """Get all bot metrics.
    
    Returns:
        Dict mapping bot names to their metrics.
    """
    try:
        return _load_merged_data()
    except Exception:
        return {}


def get_success_rate(name: str) -> Optional[float]:
    """Calculate success rate for a bot.
    
    Args:
        name: Bot name
        
    Returns:
        Success rate (0.0-1.0) or None if no data.
    """
    try:
        metrics = get_bot_metrics(name)
        if metrics is None:
            return None
        successes = metrics.get("successes", 0)
        failures = metrics.get("failures", 0)
        total = successes + failures
        if total == 0:
            return None
        return successes / total
    except Exception:
        return None


def get_token_burn_rate(name: str) -> Optional[float]:
    """Calculate average token consumption per run.
    
    Args:
        name: Bot name
        
    Returns:
        Average tokens per run or None if no data.
    """
    try:
        metrics = get_bot_metrics(name)
        if metrics is None:
            return None
        runs = metrics.get("runs", [])
        if not runs:
            return None
        total_tokens = metrics.get("total_tokens", 0)
        return total_tokens / len(runs)
    except Exception:
        return None
