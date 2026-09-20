#!/usr/bin/env python3
"""Bot Metrics — Performance tracking and telemetry for orchestrator.

Purpose
-------
Collects and persists bot run metrics including success/failure counts,
token consumption, run duration, and restart statistics.

Why
---
The orchestrator needs visibility into bot performance to make informed
decisions about model rotation, rate limiting, and capacity planning.

Invariants
----------
- Metric recording is append-only (JSONL event log) for O(1) write path
- A compacted snapshot (bot_metrics.json) is maintained for readers
- Periodic compaction merges events into snapshot when threshold is reached
- Read functions transparently merge snapshot + un-compacted events
- Metrics are persisted atomically via tmp+replace (for compaction only)
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# Default paths
_STATE_DIR: Path = Path(".codebot/state")
_METRICS_WINDOW_DAYS = 7
_MAX_RUNS_PER_BOT = 500
_MAX_METRICS_FILE_BYTES = 50000
_COMPACT_EVERY_N_EVENTS = 100  # compact after this many appended events


def set_state_dir(state_dir: Path) -> None:
    """Configure the state directory for metrics operations."""
    global _STATE_DIR
    _STATE_DIR = state_dir
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def _get_metrics_path() -> Path:
    """Return the filesystem path to the compacted bot metrics snapshot.

    The snapshot (``bot_metrics.json``) holds aggregated per-bot stats
    produced by periodic compaction of the append-only JSONL event log.
    No I/O is performed; the file may not yet exist on first run.

    Returns:
        Path under ``_STATE_DIR`` for ``bot_metrics.json``.
    """
    return _STATE_DIR / "bot_metrics.json"


def _get_events_path() -> Path:
    """Return path to the append-only JSONL event log."""
    return _STATE_DIR / "bot_metrics_events.jsonl"


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON data to *path* atomically via tmp-file + replace.

    Serialises *data* as indented JSON to a per-PID temporary file
    (``{name}.{pid}.tmp``) and renames it over *path*, guaranteeing readers
    never observe a half-written file. Used for the compacted snapshot and
    events-log truncation.

    Args:
        path: Destination file path.
        data: JSON-serialisable object to persist.

    Returns:
        None. Raises ``OSError`` on I/O failure.
    """
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# Append-only JSONL event log
# ---------------------------------------------------------------------------

def _append_event(event: dict) -> None:
    """Append a single metric event to the JSONL log (O(1) write)."""
    events_path = _get_events_path()
    line = json.dumps(event, separators=(",", ":")) + "\n"
    with open(events_path, "a", encoding="utf-8") as f:
        f.write(line)


def _read_events() -> list[dict]:
    """Read all un-compacted events from the JSONL log."""
    events_path = _get_events_path()
    if not events_path.exists():
        return []
    events: list[dict] = []
    try:
        for line in events_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue  # skip corrupt lines
    except OSError:
        pass
    return events


def _count_event_lines() -> int:
    """Quickly count lines in the events file (O(N) but fast for small files)."""
    events_path = _get_events_path()
    if not events_path.exists():
        return 0
    try:
        with open(events_path, "rb") as f:
            return sum(1 for _ in f)
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Snapshot management (compacted state)
# ---------------------------------------------------------------------------

def _load_snapshot() -> dict:
    """Load the compacted snapshot from disk."""
    snapshot_path = _get_metrics_path()
    if not snapshot_path.exists():
        return {}
    try:
        data = json.loads(snapshot_path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _apply_event(entry: dict, event: dict) -> None:
    """Apply a single metric event to a bot's metrics entry (in-place)."""
    now = event.get("t", time.time())
    cutoff = now - (_METRICS_WINDOW_DAYS * 86400)

    # Filter old runs and append new one
    runs = entry.get("runs", [])
    runs = [r for r in runs if r > cutoff]
    runs.append(now)
    entry["runs"] = runs

    # Update success/failure counts
    alive = event.get("a", True)
    exit_code = event.get("e")
    if not alive and exit_code is not None:
        if exit_code == 0:
            entry["successes"] = entry.get("successes", 0) + 1
        else:
            entry["failures"] = entry.get("failures", 0) + 1

    # Update duration and tokens
    started = event.get("s") if event.get("s") is not None else now
    entry["total_duration_s"] = entry.get("total_duration_s", 0) + (now - started)
    entry["total_tokens"] = entry.get("total_tokens", 0) + int(event.get("tk", 0))

    # Cap runs list
    if len(entry["runs"]) > _MAX_RUNS_PER_BOT:
        entry["runs"] = entry["runs"][-_MAX_RUNS_PER_BOT:]


def _apply_events_to_data(data: dict, events: list[dict]) -> dict:
    """Apply a list of events to a data dict (in-place). Returns the same dict."""
    for event in events:
        name = event.get("b", "")
        if not name:
            continue
        if name not in data:
            data[name] = {
                "runs": [],
                "successes": 0,
                "failures": 0,
                "total_tokens": 0,
                "total_duration_s": 0,
            }
        _apply_event(data[name], event)
    return data


def _prune_snapshot_size(data: dict) -> dict:
    """Reduce snapshot size to fit within _MAX_METRICS_FILE_BYTES.

    Uses a single serialization pass with progressively fewer runs per bot.
    No multiple re-serialization cycles.
    """
    serialized = json.dumps(data, indent=2)
    if len(serialized.encode("utf-8")) <= _MAX_METRICS_FILE_BYTES:
        return data

    # Binary-search-style pruning: start with current max, halve until fits
    max_runs = _MAX_RUNS_PER_BOT
    while max_runs >= 1:
        for bot_key in data:
            runs_list = data[bot_key].get("runs", [])
            if len(runs_list) > max_runs:
                data[bot_key]["runs"] = runs_list[-max_runs:]
        serialized = json.dumps(data, indent=2)
        if len(serialized.encode("utf-8")) <= _MAX_METRICS_FILE_BYTES:
            break
        max_runs = max(1, max_runs // 2)

    return data


def _compact() -> None:
    """Compact: rebuild snapshot from current snapshot + events, then clear events.

    This is the only place where the full snapshot is serialized, and it
    happens at most once per _COMPACT_EVERY_N_EVENTS calls to record_bot_metric.
    """
    data = _load_snapshot()
    events = _read_events()
    if not events:
        return  # nothing to compact

    _apply_events_to_data(data, events)
    data = _prune_snapshot_size(data)

    # Atomic write of compacted snapshot
    _write_json_atomic(_get_metrics_path(), data)

    # Clear events log (truncate to empty)
    events_path = _get_events_path()
    tmp = events_path.with_suffix(f".{os.getpid()}.tmp")
    try:
        tmp.write_text("", encoding="utf-8")
        tmp.replace(events_path)
    except OSError as e:
        logger.warning(f"Failed to clear events log: {e}")

    logger.debug("Compacted %d metric events into snapshot", len(events))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def record_bot_metric(name: str, alive: bool, exit_code: Optional[int],
                      tokens_this_run: int = 0, started_at: Optional[float] = None) -> None:
    """Record a bot run metric.

    Uses an append-only JSONL event log for O(1) writes. The snapshot is
    rebuilt via periodic compaction so the write path never re-serializes
    the entire dataset.

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
            "t": now,       # timestamp
            "b": name,      # bot name
            "a": alive,     # alive flag
            "e": exit_code, # exit code (None if still running)
            "tk": int(tokens_this_run),  # tokens consumed
            "s": started_at,            # started_at timestamp
        }
        _append_event(event)

        # Periodic compaction when threshold is reached
        if _count_event_lines() >= _COMPACT_EVERY_N_EVENTS:
            _compact()
    except Exception as e:
        logger.warning(f"Failed to record metric for '{name}': {e}")


def _load_merged_data() -> dict:
    """Load the full merged view: snapshot + un-compacted events."""
    data = _load_snapshot()
    events = _read_events()
    if events:
        # Don't modify the snapshot dict; create a working copy
        data = _apply_events_to_data(dict(data), events)
    return data


def get_bot_metrics(name: str) -> Optional[Dict[str, Any]]:
    """Get metrics for a specific bot.

    Merges compacted snapshot with any un-compacted events.

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


def get_all_metrics() -> Dict[str, Any]:
    """Get all bot metrics.

    Merges compacted snapshot with any un-compacted events.

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
    metrics = get_bot_metrics(name)
    if not metrics:
        return None
    successes = metrics.get("successes", 0)
    failures = metrics.get("failures", 0)
    total = successes + failures
    if total == 0:
        return None
    return successes / total


def get_token_burn_rate(name: str) -> Optional[float]:
    """Calculate average token consumption per run.

    Args:
        name: Bot name

    Returns:
        Average tokens per run or None if no data.
    """
    metrics = get_bot_metrics(name)
    if not metrics:
        return None
    total_tokens = metrics.get("total_tokens", 0)
    runs = metrics.get("runs", [])
    if len(runs) == 0:
        return None
    return total_tokens / len(runs)
