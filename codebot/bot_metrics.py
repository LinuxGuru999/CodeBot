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
- Metrics are persisted atomically via tmp+replace
- Run history is capped to prevent unbounded growth
- Serializes fully to avoid corrupt JSON
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


def set_state_dir(state_dir: Path) -> None:
    """Configure the state directory for metrics operations."""
    global _STATE_DIR
    _STATE_DIR = state_dir
    _STATE_DIR.mkdir(parents=True, exist_ok=True)


def _get_metrics_path() -> Path:
    """Return path to bot metrics file."""
    return _STATE_DIR / "bot_metrics.json"


def _write_json_atomic(path: Path, data: Any) -> None:
    """Write JSON atomically via tmp+replace."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


def record_bot_metric(name: str, alive: bool, exit_code: Optional[int],
                      tokens_this_run: int = 0, started_at: Optional[float] = None) -> None:
    """Record a bot run metric.
    
    Args:
        name: Bot name
        alive: Whether bot process is still running
        exit_code: Exit code if process ended (None if still running)
        tokens_this_run: Token consumption for this run
        started_at: Unix timestamp when bot started
    """
    metrics_path = _get_metrics_path()
    try:
        data = {}
        if metrics_path.exists():
            data = json.loads(metrics_path.read_text(encoding="utf-8"))
        
        if name not in data:
            data[name] = {
                "runs": [],
                "successes": 0,
                "failures": 0,
                "total_tokens": 0,
                "total_duration_s": 0
            }
        
        entry = data[name]
        now = time.time()
        cutoff = now - (_METRICS_WINDOW_DAYS * 86400)
        entry["runs"] = [r for r in entry.get("runs", []) if r > cutoff]
        entry["runs"].append(now)
        
        if not alive and exit_code is not None:
            if exit_code == 0:
                entry["successes"] = entry.get("successes", 0) + 1
            else:
                entry["failures"] = entry.get("failures", 0) + 1
        
        started = started_at if started_at else now
        entry["total_duration_s"] = entry.get("total_duration_s", 0) + (now - started)
        entry["total_tokens"] = entry.get("total_tokens", 0) + int(tokens_this_run)
        
        # Cap runs list to prevent unbounded growth
        if len(entry["runs"]) > _MAX_RUNS_PER_BOT:
            entry["runs"] = entry["runs"][-_MAX_RUNS_PER_BOT:]
        
        serialized = json.dumps(data, indent=2)
        
        # Prune if file exceeds budget
        if len(serialized.encode("utf-8")) > _MAX_METRICS_FILE_BYTES:
            for bot_key in data:
                runs_list = data[bot_key].get("runs", [])
                if len(runs_list) > 10:
                    data[bot_key]["runs"] = runs_list[-10:]
            serialized = json.dumps(data, indent=2)
        
        _write_json_atomic(metrics_path, serialized)
    except Exception as e:
        logger.warning(f"Failed to record metric for '{name}': {e}")


def get_bot_metrics(name: str) -> Optional[Dict[str, Any]]:
    """Get metrics for a specific bot.
    
    Args:
        name: Bot name
    
    Returns:
        Metrics dict or None if not found.
    """
    metrics_path = _get_metrics_path()
    try:
        if not metrics_path.exists():
            return None
        data = json.loads(metrics_path.read_text(encoding="utf-8"))
        return data.get(name)
    except Exception:
        return None


def get_all_metrics() -> Dict[str, Any]:
    """Get all bot metrics.
    
    Returns:
        Dict mapping bot names to their metrics.
    """
    metrics_path = _get_metrics_path()
    try:
        if not metrics_path.exists():
            return {}
        return json.loads(metrics_path.read_text(encoding="utf-8"))
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
