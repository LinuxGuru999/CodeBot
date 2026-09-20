"""Per-model success/cost statistics collection.

Purpose
-------
Tracks success rates, costs, and token usage per model provider to inform
adaptive model routing decisions. Records outcomes of every LLM API call
so the scheduler can learn which models perform best for which task classes.

Why
---
Spec section 29 requires the scheduler to route based on historical results.
Without per-model statistics, model selection is blind. This module provides
the telemetry foundation for cost-aware scheduling.

Invariants
----------
- stdlib-only (json, time, pathlib)
- Append-only records; never mutates historical data
- Missing fields degrade gracefully to zero counts
- Data persists across restarts via atomic JSON file writes
"""
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)


_CLEANUP_INTERVAL = 10  # trigger cleanup every N record_call invocations
_CLEANUP_DEFAULT_MAX_SUCCESSES = 3  # remove after N consecutive successes
_CLEANUP_DEFAULT_MAX_AGE = 86400.0  # remove entries older than 24 hours

# Module-level counter for amortised cleanup triggering
_calls_since_cleanup: int = 0


class StatsCollector:
    """Collects and queries per-model API call statistics.
    
    Thread-safety: Uses atomic tmp+replace for writes.
    Persistence: Stores data in state_dir/model_stats.json.
    """
    
    def __init__(self, state_dir: str = ".codebot/state"):
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.stats_file = self.state_dir / "model_stats.json"
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._load()
    
    def _load(self) -> None:
        """Load stats from disk into memory cache."""
        if self.stats_file.exists():
            try:
                raw = self.stats_file.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    self._cache = data
            except (json.JSONDecodeError, OSError):
                self._cache = {}
    
    def _save(self) -> None:
        """Atomically persist cache to disk."""
        tmp = self.stats_file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(self._cache, indent=2), encoding="utf-8")
            tmp.replace(self.stats_file)
        except OSError:
            # Fail-open: stats loss is acceptable, crash is not
            pass
    
    def record_call(
        self,
        model_name: str,
        task_type: str,
        success: bool,
        cost: float,
        tokens_in: int,
        tokens_out: int
    ) -> None:
        """Record a single API call outcome.
        
        Args:
            model_name: Identifier of the model used
            task_type: Category of task (e.g., 'code_generation')
            success: Whether the call succeeded
            cost: Monetary cost of the call
            tokens_in: Input token count
            tokens_out: Output token count
        """
        global _calls_since_cleanup
        key = f"{model_name}:{task_type}"
        if key not in self._cache:
            self._cache[key] = {
                "total_calls": 0,
                "successful_calls": 0,
                "failed_calls": 0,
                "total_cost": 0.0,
                "total_tokens_in": 0,
                "total_tokens_out": 0,
                "last_updated": 0.0,
                "consecutive_successes": 0
            }
        
        entry = self._cache[key]
        entry["total_calls"] = entry.get("total_calls", 0) + 1
        if success:
            entry["successful_calls"] = entry.get("successful_calls", 0) + 1
            entry["consecutive_successes"] = entry.get("consecutive_successes", 0) + 1
        else:
            entry["failed_calls"] = entry.get("failed_calls", 0) + 1
            entry["consecutive_successes"] = 0
        entry["total_cost"] = entry.get("total_cost", 0.0) + cost
        entry["total_tokens_in"] = entry.get("total_tokens_in", 0) + tokens_in
        entry["total_tokens_out"] = entry.get("total_tokens_out", 0) + tokens_out
        entry["last_updated"] = time.time()

        # Check if this specific entry qualifies for immediate cleanup
        if entry.get("consecutive_successes", 0) >= _CLEANUP_DEFAULT_MAX_SUCCESSES:
            logger.debug("Cleaning up recovered stats entry: %s", key)
            del self._cache[key]
            self._save()
            return

        self._save()

        # Amortised cleanup: trigger periodically to avoid per-call overhead
        _calls_since_cleanup += 1
        if _calls_since_cleanup >= _CLEANUP_INTERVAL:
            _calls_since_cleanup = 0
            self.cleanup_stale_entries()
    
    def cleanup_stale_entries(
        self,
        max_consecutive_successes: int = _CLEANUP_DEFAULT_MAX_SUCCESSES,
        max_age_seconds: float = _CLEANUP_DEFAULT_MAX_AGE
    ) -> int:
        """Remove entries that have recovered or gone stale.
        
        An entry is removed if either condition is met:
          - consecutive_successes >= max_consecutive_successes (model has recovered)
          - time since last_updated > max_age_seconds (entry is stale)
        
        Args:
            max_consecutive_successes: Threshold for consecutive successes to prune.
            max_age_seconds: Maximum age in seconds before an entry is considered stale.
        
        Returns:
            Number of entries removed.
        """
        now = time.time()
        keys_to_remove: list[str] = []
        for key, entry in self._cache.items():
            successes = entry.get("consecutive_successes", 0)
            last_updated = entry.get("last_updated", 0.0)
            age = now - last_updated if last_updated > 0 else float("inf")
            if successes >= max_consecutive_successes or age > max_age_seconds:
                keys_to_remove.append(key)
        
        for key in keys_to_remove:
            logger.debug("Cleaning up stale stats entry: %s", key)
            del self._cache[key]
        
        if keys_to_remove:
            self._save()
        
        return len(keys_to_remove)

    def get_stats(
        self,
        model_name: Optional[str] = None,
        task_type: Optional[str] = None
    ) -> Dict[str, Dict[str, Any]]:
        """Query statistics with optional filtering.
        
        Args:
            model_name: Filter by model (exact match)
            task_type: Filter by task type (exact match)
            
        Returns:
            Dict mapping composite keys to stat entries
        """
        result = {}
        for key, entry in self._cache.items():
            parts = key.split(":", 1)
            if len(parts) != 2:
                continue
            m, t = parts
            if model_name is not None and m != model_name:
                continue
            if task_type is not None and t != task_type:
                continue
            result[key] = entry
        return result
