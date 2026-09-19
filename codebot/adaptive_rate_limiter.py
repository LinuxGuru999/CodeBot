#!/usr/bin/env python3
"""Adaptive Rate Limiter — request queue with learned rate limits.

Purpose
-------
Queues API requests per model, learns rate limits from 429 responses,
and automatically adjusts timing so all bots eventually run without
hitting provider limits.

Why
---
Without queueing:
- Bots hit rate limits and waste tokens on retries
- Some bots never run because they're always blocked
- No learning from past rate limit events

With queueing:
- All requests are queued and processed in order
- Rate limits are learned automatically
- Bots get delayed instead of blocked
- Throughput maximized while staying under limits

Invariants
----------
- stdlib-only (time, threading, queue, logging)
- Thread-safe via locks
- Per-model request queues with learned limits
- Auto-recovery when limits allow
"""

import logging
import threading
import time
from dataclasses import dataclass, field
from queue import Queue, Empty
from typing import Optional, Callable, Any

logger = logging.getLogger("adaptive_rate_limiter")


@dataclass
class ModelRateState:
    """Rate limiting state for a single model."""
    model: str
    learned_rpm: float = 60.0
    min_interval: float = 1.0
    last_request: float = 0.0
    last_rate_limit: float = 0.0
    consecutive_rate_limits: int = 0
    total_requests: int = 0
    total_rate_limits: int = 0
    
    @property
    def effective_interval(self) -> float:
        if self.consecutive_rate_limits > 0:
            return self.min_interval * (2 ** min(self.consecutive_rate_limits, 4))
        return self.min_interval
    
    def record_request(self) -> None:
        self.last_request = time.time()
        self.total_requests += 1
    
    def record_rate_limit(self, retry_after: Optional[float] = None) -> None:
        self.last_rate_limit = time.time()
        self.consecutive_rate_limits += 1
        self.total_rate_limits += 1
        
        if retry_after:
            self.min_interval = max(self.min_interval, retry_after)
        else:
            self.min_interval = min(self.min_interval * 2, 30.0)
        
        self.learned_rpm = max(1.0, 60.0 / self.min_interval)
    
    def record_success(self) -> None:
        if self.consecutive_rate_limits > 0:
            self.consecutive_rate_limits = max(0, self.consecutive_rate_limits - 1)
            if self.consecutive_rate_limits == 0:
                self.min_interval = max(0.5, self.min_interval * 0.9)
                self.learned_rpm = 60.0 / self.min_interval


@dataclass
class QueuedRequest:
    """A queued API request."""
    model: str
    bot_name: str
    callback: Callable[..., Any]
    args: tuple = ()
    kwargs: dict = field(default_factory=dict)
    enqueued_at: float = field(default_factory=time.time)
    priority: int = 0


class AdaptiveRateLimiter:
    """Request queue with learned rate limits."""
    
    def __init__(self):
        self._lock = threading.Lock()
        self._models: dict[str, ModelRateState] = {}
        self._queues: dict[str, Queue] = {}
        self._threads: dict[str, threading.Thread] = {}
        self._shutdown = False
        
        self._default_limits = {
            "qwen-3.5-plus": 30,
            "qwen-3.6-plus": 30,
            "qwen-3.7-plus": 25,
            "qwen-3.8-max": 20,
            "qwen-3.7-max": 20,
            "qwen-3.5-plus-thinking": 15,
            "qwen-3.6-plus-thinking": 15,
            "qwen-3.7-max-thinking": 10,
            "qwen-3.8-max-thinking": 10,
            "xiaomi-mimo-2.5": 40,
            "meta-muse-spark-1.2": 20,
            "meta-muse-spark-1.3": 20,
        }
    
    def _get_state(self, model: str) -> ModelRateState:
        if model not in self._models:
            rpm = self._default_limits.get(model, 30)
            state = ModelRateState(model=model)
            state.learned_rpm = rpm
            state.min_interval = 60.0 / rpm
            self._models[model] = state
        return self._models[model]
    
    def _get_queue(self, model: str) -> Queue:
        if model not in self._queues:
            self._queues[model] = Queue()
        return self._queues[model]
    
    def _process_queue(self, model: str) -> None:
        state = self._get_state(model)
        queue = self._get_queue(model)
        
        while not self._shutdown:
            try:
                request = queue.get(timeout=1.0)
            except Empty:
                continue
            
            if request is None:
                break
            
            delay = state.effective_interval - (time.time() - state.last_request)
            if delay > 0:
                time.sleep(delay)
            
            try:
                result = request.callback(*request.args, **request.kwargs)
                state.record_success()
            except Exception as e:
                error_str = str(e).lower()
                if "429" in error_str or "rate" in error_str or "limit" in error_str:
                    state.record_rate_limit()
                    queue.put(request)
                else:
                    raise
    
    def ensure_worker(self, model: str) -> None:
        with self._lock:
            if model not in self._threads or not self._threads[model].is_alive():
                thread = threading.Thread(
                    target=self._process_queue,
                    args=(model,),
                    daemon=True,
                    name=f"rate-limiter-{model}"
                )
                self._threads[model] = thread
                thread.start()
    
    def enqueue(self, model: str, bot_name: str, callback: Callable, 
                args: tuple = (), kwargs: dict = None, priority: int = 0) -> None:
        if self._shutdown:
            return
        
        self.ensure_worker(model)
        
        request = QueuedRequest(
            model=model,
            bot_name=bot_name,
            callback=callback,
            args=args,
            kwargs=kwargs or {},
            priority=priority
        )
        
        queue = self._get_queue(model)
        queue.put(request)
        
        state = self._get_state(model)
        logger.debug(
            f"Enqueued request for {model} (bot={bot_name}, "
            f"queue_size={queue.qsize()}, learned_rpm={state.learned_rpm:.1f})"
        )
    
    def record_rate_limit(self, model: str, retry_after: Optional[float] = None) -> None:
        with self._lock:
            state = self._get_state(model)
            state.record_rate_limit(retry_after)
            logger.warning(
                f"Rate limit for {model}: min_interval={state.min_interval:.2f}s, "
                f"learned_rpm={state.learned_rpm:.1f}, consecutive={state.consecutive_rate_limits}"
            )
    
    def record_success(self, model: str) -> None:
        with self._lock:
            state = self._get_state(model)
            state.record_success()
    
    def can_spawn_now(self, model: str) -> tuple[bool, str]:
        state = self._get_state(model)
        queue = self._get_queue(model)
        
        delay = state.effective_interval - (time.time() - state.last_request)
        if delay > 0:
            return False, f"backoff {delay:.1f}s for {model}"
        
        if queue.qsize() > 10:
            return False, f"queue full ({queue.qsize()} pending)"
        
        return True, "ok"
    
    def get_queue_size(self, model: str) -> int:
        return self._get_queue(model).qsize()
    
    def get_stats(self) -> dict:
        with self._lock:
            stats = {"models": {}}
            for model, state in self._models.items():
                queue = self._get_queue(model)
                stats["models"][model] = {
                    "learned_rpm": state.learned_rpm,
                    "min_interval": state.min_interval,
                    "effective_interval": state.effective_interval,
                    "total_requests": state.total_requests,
                    "total_rate_limits": state.total_rate_limits,
                    "consecutive_rate_limits": state.consecutive_rate_limits,
                    "queue_size": queue.qsize(),
                }
            return stats
    
    def shutdown(self) -> None:
        self._shutdown = True
        for model, queue in self._queues.items():
            queue.put(None)
        for thread in self._threads.values():
            thread.join(timeout=5.0)


rate_limiter = AdaptiveRateLimiter()
