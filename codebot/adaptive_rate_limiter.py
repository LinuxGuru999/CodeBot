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
from contextlib import contextmanager
from dataclasses import dataclass, asdict, field
from pathlib import Path
from typing import Callable, Iterator, Optional

import fcntl

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
    request_times: list[float] = field(default_factory=list)
    observed_rpm: float = 0.0
    observation_count: int = 0

    @property
    def effective_interval(self) -> float:
        if self.consecutive_rate_limits > 0:
            return self.min_interval * (2 ** min(self.consecutive_rate_limits, 2))
        return self.min_interval

    def record_request(self) -> None:
        now = time.time()
        self.last_request = now
        self.total_requests += 1
        self.request_times = [ts for ts in self.request_times if now - ts < 60.0]
        self.request_times.append(now)
        self.observed_rpm = len(self.request_times)
        self.observation_count += 1

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
        self._lock_file = self._state_dir / "rate_limits.lock"

        self._default_limits: dict[str, float] = {
            "qwen-3.5-plus": 30,
            "qwen-3.6-plus": 30,
            "qwen-3.7-plus": 30,
            "qwen-3.7-plus-thinking": 30,
            "qwen-3.6-max-preview": 30,
            "qwen-3.6-max-preview-thinking": 30,
            "qwen-3.8-omni-flash": 30,
            "qwen-3.8-omni-flash-thinking": 30,
            "qwen-3.5-omni-plus": 30,
            "qwen-3.8-max": 20,
            "qwen-3.7-max": 30,
            "qwen-3.5-plus-thinking": 30,
            "qwen-3.6-plus-thinking": 30,
            "qwen-3.7-max-thinking": 30,
            "qwen-3.8-max-thinking": 10,
            "xiaomi-mimo-2.5": 30,
            "meta-muse-spark-1.2": 30,
            "meta-muse-spark-1.3": 30,
        }

        configured = os.getenv("CODEBOT_MODEL_RATE_LIMITS", "")
        if configured:
            try:
                payload = json.loads(configured)
                if isinstance(payload, dict):
                    self._default_limits.update(
                        {str(model): float(rpm) for model, rpm in payload.items() if float(rpm) > 0}
                    )
            except (TypeError, ValueError, json.JSONDecodeError):
                pass

        config_file = self._state_dir.parent / "model_rate_limits.json"
        try:
            payload = json.loads(config_file.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                self._default_limits.update(
                    {str(model): float(rpm) for model, rpm in payload.items() if not str(model).startswith("_") and float(rpm) > 0}
                )
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

        discovered_file = self._state_dir / "discovered_limits.json"
        if discovered_file.exists():
            try:
                import json as _json
                discovered = _json.loads(discovered_file.read_text())
                for model, info in discovered.items():
                    rpm = info.get("measured_rpm")
                    if rpm and rpm > 0:
                        self._default_limits[model] = min(float(rpm), self._default_limits.get(model, float(rpm)))
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

    @contextmanager
    def _state_lock(self) -> Iterator[None]:
        self._state_dir.mkdir(parents=True, exist_ok=True)
        with self._lock_file.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def _get_state(self, model: str) -> ModelRateState:
        data = self._load()
        configured_rpm = self._default_limits.get(model, 30)
        configured_interval = 60.0 / configured_rpm
        if model in data:
            d = data[model]
            return ModelRateState(
                model=model,
                learned_rpm=min(float(d.get("learned_rpm", configured_rpm)), configured_rpm),
                min_interval=max(float(d.get("min_interval", configured_interval)), configured_interval),
                last_request=d.get("last_request", 0.0),
                last_rate_limit=d.get("last_rate_limit", 0.0),
                consecutive_rate_limits=d.get("consecutive_rate_limits", 0),
                total_requests=d.get("total_requests", 0),
                total_rate_limits=d.get("total_rate_limits", 0),
                request_times=[float(ts) for ts in d.get("request_times", []) if isinstance(ts, (int, float))],
                observed_rpm=float(d.get("observed_rpm", 0.0)),
                observation_count=int(d.get("observation_count", 0)),
            )
        rpm = configured_rpm
        state = ModelRateState(model=model)
        state.learned_rpm = rpm
        state.min_interval = 60.0 / rpm
        return state

    def _save_state(self, state: ModelRateState) -> None:
        with self._state_lock():
            data = self._load()
            data[state.model] = asdict(state)
            self._save(data)
            self._save_discovery(state)

    def _update_state(self, model: str, update: Callable[[ModelRateState], None]) -> None:
        with self._state_lock():
            state = self._get_state(model)
            update(state)
            data = self._load()
            data[model] = asdict(state)
            self._save(data)
            self._save_discovery(state)

    def _save_discovery(self, state: ModelRateState) -> None:
        path = self._state_dir / "discovered_limits.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
            if not isinstance(payload, dict):
                payload = {}
            payload[state.model] = {
                "measured_rpm": round(state.observed_rpm, 3),
                "safe_rpm": round(min(state.learned_rpm, self._default_limits.get(state.model, state.learned_rpm)), 3),
                "confidence": round(min(1.0, state.observation_count / 20.0), 3),
                "total_requests": state.total_requests,
                "total_rate_limits": state.total_rate_limits,
                "updated_at": time.time(),
            }
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            os.replace(str(tmp), str(path))
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass

    def record_rate_limit(self, model: str, retry_after: Optional[float] = None) -> None:
        self._update_state(model, lambda state: state.record_rate_limit(retry_after))

    def record_success(self, model: str) -> None:
        self._update_state(model, lambda state: state.record_success())

    def can_spawn_now(self, model: str) -> tuple[bool, str]:
        state = self._get_state(model)

        delay = state.effective_interval - (time.time() - state.last_request)
        if delay > 0:
            return False, f"backoff {delay:.1f}s for {model}"

        return True, "ok"

    def acquire_slot(self, model: str) -> float:
        with self._state_lock():
            state = self._get_state(model)
            delay = state.effective_interval - (time.time() - state.last_request)
            if delay > 0:
                time.sleep(delay)
            state.record_request()
            data = self._load()
            data[model] = asdict(state)
            self._save(data)
            self._save_discovery(state)
            return max(0.0, delay)

    def wait_if_needed(self, model: str) -> float:
        state = self._get_state(model)
        delay = state.effective_interval - (time.time() - state.last_request)
        if delay > 0:
            import time as _time
            _time.sleep(delay)
            return delay
        return 0.0

    def acquire_slot(self, model: str) -> float:
        with self._state_lock():
            state = self._get_state(model)
            delay = state.effective_interval - (time.time() - state.last_request)
            if delay > 0:
                time.sleep(delay)
            state.record_request()
            data = self._load()
            data[state.model] = asdict(state)
            self._save(data)
            self._save_discovery(state)
            return max(0.0, delay)

    def record_request(self, model: str) -> None:
        self._update_state(model, lambda state: state.record_request())

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
                "observed_rpm": d.get("observed_rpm", 0.0),
                "observation_count": d.get("observation_count", 0),
                "safe_rpm": d.get("safe_rpm", d.get("learned_rpm", 0.0)),
            }
        return stats


rate_limiter = AdaptiveRateLimiter()
