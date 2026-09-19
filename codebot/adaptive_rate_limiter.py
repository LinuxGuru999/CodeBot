#!/usr/bin/env python3
"""Adaptive Rate Limiter — file-based rate limit coordination across processes.

Purpose
-------
Coordinates API rate limits across all bot subprocesses via shared JSON state.
Each process reads/writes the same rate limit file, learning from 429 responses
so the entire fleet slows down together instead of thundering independently.

Why
---
Without cross-process coordination:
- Bot A hits 429, backs off, but bots B-Z keep hammering
- No global learning from rate limit events
- Each bot independently discovers limits (wasted tokens)

With file-based coordination:
- All bots share one rate limit state file
- 429 on model X delays ALL bots on model X
- Limits learned once apply to entire fleet

Invariants
----------
- stdlib-only (json, time, os, pathlib)
- File-based state: .codebot/state/rate_limits.json
- Atomic writes via tmp+replace
- Per-model learned intervals
- Auto-recovery when limits allow
"""

import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional

STATE_DIR = Path(os.getenv("CODEBOT_PROJECT_ROOT", ".")) / ".codebot" / "state"
RATE_LIMIT_FILE = STATE_DIR / "rate_limits.json"


@dataclass
class ModelRateState:
    """Rate limiting state for a single model."""
    model: str
    learned_rpm: float = 30.0
    min_interval: float = 2.0
    last_request: float = 0.0
    last_rate_limit: float = 0.0
    consecutive_rate_limits: int = 0
    total_requests: int = 0
    total_rate_limits: int = 0

    @property
    def effective_interval(self) -> float:
        if self.consecutive_rate_limits > 0:
            return self.min_interval * (2 ** min(self.consecutive_rate_limits, 2))
        return self.min_interval

    def record_request(self) -> None:
        self.last_request = time.time()
        self.total_requests += 1

    def record_rate_limit(self, retry_after: Optional[float] = None) -> None:
        self.last_rate_limit = time.time()
        self.consecutive_rate_limits += 1
        self.total_rate_limits += 1

        if retry_after:
            self.min_interval = max(self.min_interval, min(retry_after, 3.0))
        else:
            self.min_interval = min(self.min_interval * 1.2, 3.0)

        self.learned_rpm = max(1.0, 60.0 / self.min_interval)

    def record_success(self) -> None:
        if self.consecutive_rate_limits > 0:
            self.consecutive_rate_limits = max(0, self.consecutive_rate_limits - 1)
            if self.consecutive_rate_limits == 0:
                self.min_interval = max(0.5, self.min_interval * 0.8)
                self.learned_rpm = 60.0 / self.min_interval


class AdaptiveRateLimiter:
    """File-based rate limiter for cross-process coordination."""

    def __init__(self, state_dir: Optional[Path] = None):
        self._state_dir = state_dir or STATE_DIR
        self._file = self._state_dir / "rate_limits.json"

        self._default_limits = {
            "qwen-3.5-plus": 120,
            "qwen-3.6-plus": 120,
            "qwen-3.7-plus": 120,
            "qwen-3.8-max": 120,
            "qwen-3.7-max": 120,
            "qwen-3.5-plus-thinking": 120,
            "qwen-3.6-plus-thinking": 120,
            "qwen-3.7-max-thinking": 120,
            "qwen-3.8-max-thinking": 120,
            "xiaomi-mimo-2.5": 120,
            "meta-muse-spark-1.2": 60,
            "meta-muse-spark-1.3": 60,
        }

        discovered_file = self._state_dir / "discovered_limits.json"
        if discovered_file.exists():
            try:
                import json as _json
                discovered = _json.loads(discovered_file.read_text())
                for model, info in discovered.items():
                    rpm = info.get("measured_rpm")
                    if rpm and rpm > 0:
                        self._default_limits[model] = rpm
            except Exception:
                pass

    def _load(self) -> dict[str, dict]:
        try:
            if self._file.exists():
                raw = self._file.read_text(encoding="utf-8")
                if raw.strip():
                    return json.loads(raw)
        except (json.JSONDecodeError, OSError):
            pass
        return {}

    def _save(self, data: dict[str, dict]) -> None:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        tmp = self._file.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(self._file))
        except OSError:
            pass

    def _get_state(self, model: str) -> ModelRateState:
        data = self._load()
        if model in data:
            d = data[model]
            return ModelRateState(
                model=model,
                learned_rpm=d.get("learned_rpm", 30.0),
                min_interval=d.get("min_interval", 2.0),
                last_request=d.get("last_request", 0.0),
                last_rate_limit=d.get("last_rate_limit", 0.0),
                consecutive_rate_limits=d.get("consecutive_rate_limits", 0),
                total_requests=d.get("total_requests", 0),
                total_rate_limits=d.get("total_rate_limits", 0),
            )
        rpm = self._default_limits.get(model, 30)
        state = ModelRateState(model=model)
        state.learned_rpm = rpm
        state.min_interval = 60.0 / rpm
        return state

    def _save_state(self, state: ModelRateState) -> None:
        data = self._load()
        data[state.model] = asdict(state)
        self._save(data)

    def record_rate_limit(self, model: str, retry_after: Optional[float] = None) -> None:
        state = self._get_state(model)
        state.record_rate_limit(retry_after)
        self._save_state(state)

    def record_success(self, model: str) -> None:
        state = self._get_state(model)
        state.record_success()
        self._save_state(state)

    def can_spawn_now(self, model: str) -> tuple[bool, str]:
        state = self._get_state(model)

        delay = state.effective_interval - (time.time() - state.last_request)
        if delay > 0:
            return False, f"backoff {delay:.1f}s for {model}"

        return True, "ok"

    def wait_if_needed(self, model: str) -> float:
        state = self._get_state(model)
        delay = state.effective_interval - (time.time() - state.last_request)
        if delay > 0:
            import time as _time
            _time.sleep(delay)
            return delay
        return 0.0

    def record_request(self, model: str) -> None:
        state = self._get_state(model)
        state.record_request()
        self._save_state(state)

    def get_stats(self) -> dict:
        data = self._load()
        stats = {"models": {}}
        for model, d in data.items():
            stats["models"][model] = {
                "learned_rpm": d.get("learned_rpm", 0),
                "min_interval": d.get("min_interval", 0),
                "effective_interval": ModelRateState(**d).effective_interval if "min_interval" in d else 0,
                "total_requests": d.get("total_requests", 0),
                "total_rate_limits": d.get("total_rate_limits", 0),
                "consecutive_rate_limits": d.get("consecutive_rate_limits", 0),
            }
        return stats


rate_limiter = AdaptiveRateLimiter()
