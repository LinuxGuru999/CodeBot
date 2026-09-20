#!/usr/bin/env python3
"""Metrics Service — Bot metrics collection without blocking.

Purpose
-------
Collects and aggregates bot metrics (uptime, restart counts, error rates,
ticket throughput) for monitoring and alerting. Designed to be non-blocking
and safe to call from the main orchestrator loop.

Why
---
Metrics collection should not interfere with bot lifecycle management or
ticket dispatch. This service provides atomic, non-blocking metric reads
and writes that can be safely called from any part of the system.

Invariants
----------
- All metric operations are non-blocking
- Metrics files are written atomically via tmp+replace
- No external dependencies (no Prometheus, no statsd)
- Metrics are persisted to disk for crash recovery
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = _CODEBOT_PKG_DIR.parent
STATE_DIR = _project_root / ".codebot" / "state"
METRICS_DIR = STATE_DIR / "metrics"

METRICS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class BotMetrics:
    """Metrics for a single bot."""
    name: str
    total_runs: int = 0
    successful_runs: int = 0
    failed_runs: int = 0
    stuck_restarts: int = 0
    total_uptime_seconds: float = 0.0
    last_run_at: float = 0.0
    last_success_at: float = 0.0
    last_failure_at: float = 0.0
    consecutive_errors: int = 0
    model_changes: int = 0
    first_seen_at: float = field(default_factory=time.time)


@dataclass
class PipelineMetrics:
    """Aggregate pipeline metrics."""
    tickets_created: int = 0
    tickets_completed: int = 0
    tickets_reworked: int = 0
    avg_resolution_time_seconds: float = 0.0
    last_updated: float = field(default_factory=time.time)


def _write_json_atomic(path: Path, data: dict | list) -> None:
    """Write JSON atomically via tmp+replace."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _load_metrics_file(path: Path) -> dict:
    """Load metrics from file, returning empty dict on error."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def bot_metrics_path(bot_name: str) -> Path:
    """Path to bot's metrics file."""
    return METRICS_DIR / f"{bot_name}.metrics.json"


def read_bot_metrics(bot_name: str) -> BotMetrics:
    """Read persisted metrics for a bot.
    
    Args:
        bot_name: Name of the bot
    
    Returns:
        BotMetrics object with loaded or default values
    """
    path = bot_metrics_path(bot_name)
    data = _load_metrics_file(path)
    
    return BotMetrics(
        name=data.get("name", bot_name),
        total_runs=data.get("total_runs", 0),
        successful_runs=data.get("successful_runs", 0),
        failed_runs=data.get("failed_runs", 0),
        stuck_restarts=data.get("stuck_restarts", 0),
        total_uptime_seconds=data.get("total_uptime_seconds", 0.0),
        last_run_at=data.get("last_run_at", 0.0),
        last_success_at=data.get("last_success_at", 0.0),
        last_failure_at=data.get("last_failure_at", 0.0),
        consecutive_errors=data.get("consecutive_errors", 0),
        model_changes=data.get("model_changes", 0),
        first_seen_at=data.get("first_seen_at", time.time()),
    )


def save_bot_metrics(metrics: BotMetrics) -> None:
    """Persist bot metrics to disk atomically.
    
    Args:
        metrics: BotMetrics object to save
    """
    path = bot_metrics_path(metrics.name)
    data = {
        "name": metrics.name,
        "total_runs": metrics.total_runs,
        "successful_runs": metrics.successful_runs,
        "failed_runs": metrics.failed_runs,
        "stuck_restarts": metrics.stuck_restarts,
        "total_uptime_seconds": metrics.total_uptime_seconds,
        "last_run_at": metrics.last_run_at,
        "last_success_at": metrics.last_success_at,
        "last_failure_at": metrics.last_failure_at,
        "consecutive_errors": metrics.consecutive_errors,
        "model_changes": metrics.model_changes,
        "first_seen_at": metrics.first_seen_at,
    }
    _write_json_atomic(path, data)


def record_bot_run(bot_name: str, success: bool, duration_seconds: float = 0.0) -> None:
    """Record a bot run completion.
    
    Args:
        bot_name: Name of the bot
        success: True if run completed successfully
        duration_seconds: Duration of the run
    """
    metrics = read_bot_metrics(bot_name)
    metrics.total_runs += 1
    metrics.last_run_at = time.time()
    
    if success:
        metrics.successful_runs += 1
        metrics.last_success_at = time.time()
        metrics.consecutive_errors = 0
    else:
        metrics.failed_runs += 1
        metrics.last_failure_at = time.time()
        metrics.consecutive_errors += 1
    
    metrics.total_uptime_seconds += duration_seconds
    save_bot_metrics(metrics)


def record_stuck_restart(bot_name: str) -> None:
    """Record a stuck-induced restart.
    
    Args:
        bot_name: Name of the bot
    """
    metrics = read_bot_metrics(bot_name)
    metrics.stuck_restarts += 1
    save_bot_metrics(metrics)


def record_model_change(bot_name: str) -> None:
    """Record a model rotation event.
    
    Args:
        bot_name: Name of the bot
    """
    metrics = read_bot_metrics(bot_name)
    metrics.model_changes += 1
    save_bot_metrics(metrics)


def get_all_bot_metrics() -> dict[str, BotMetrics]:
    """Get metrics for all bots.
    
    Returns:
        Dict mapping bot name to BotMetrics
    """
    results: dict[str, BotMetrics] = {}
    if not METRICS_DIR.exists():
        return results
    
    for metrics_file in METRICS_DIR.glob("*.metrics.json"):
        bot_name = metrics_file.stem.replace(".metrics", "")
        results[bot_name] = read_bot_metrics(bot_name)
    
    return results


def pipeline_metrics_path() -> Path:
    """Path to pipeline metrics file."""
    return METRICS_DIR / "pipeline.metrics.json"


def read_pipeline_metrics() -> PipelineMetrics:
    """Read aggregated pipeline metrics."""
    path = pipeline_metrics_path()
    data = _load_metrics_file(path)
    
    return PipelineMetrics(
        tickets_created=data.get("tickets_created", 0),
        tickets_completed=data.get("tickets_completed", 0),
        tickets_reworked=data.get("tickets_reworked", 0),
        avg_resolution_time_seconds=data.get("avg_resolution_time_seconds", 0.0),
        last_updated=data.get("last_updated", time.time()),
    )


def save_pipeline_metrics(metrics: PipelineMetrics) -> None:
    """Persist pipeline metrics atomically."""
    path = pipeline_metrics_path()
    data = {
        "tickets_created": metrics.tickets_created,
        "tickets_completed": metrics.tickets_completed,
        "tickets_reworked": metrics.tickets_reworked,
        "avg_resolution_time_seconds": metrics.avg_resolution_time_seconds,
        "last_updated": metrics.last_updated,
    }
    _write_json_atomic(path, data)


def collect_bot_status_snapshot(bots: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Collect a non-blocking snapshot of bot statuses.
    
    Args:
        bots: Dict of bot name to BotState
    
    Returns:
        Dict mapping bot name to status dict
    """
    from codebot.health_monitor import batch_read_heartbeats, effective_heartbeat_timeout
    from codebot.model_manager import model_profile
    
    bot_names = list(bots.keys())
    heartbeat_cache = batch_read_heartbeats(bot_names)
    now = time.time()
    
    snapshot: dict[str, dict[str, Any]] = {}
    for name, bot in bots.items():
        pid = None
        if bot.process is not None and bot.process.poll() is None:
            pid = bot.process.pid
        
        hb = heartbeat_cache.get(name, 0.0)
        hb_age = max(0.0, now - hb) if hb > 0 else None
        
        prof = model_profile(bot.config.model)
        
        snapshot[name] = {
            "enabled": bot.config.enabled,
            "running": pid is not None,
            "pid": pid,
            "model": bot.config.model,
            "lockup_risk": prof.lockup_risk if prof else "unknown",
            "effective_timeout": effective_heartbeat_timeout(
                bot.config.interval_seconds, bot.config.model, bot.config.heartbeat_timeout
            ),
            "heartbeat_age_seconds": round(hb_age, 1) if hb_age else None,
            "next_run_in": round(bot.next_run_at - now, 1) if bot.next_run_at and not pid else None,
            "restart_count": bot.restart_count,
            "consecutive_errors": bot.consecutive_errors,
        }
    
    return snapshot
