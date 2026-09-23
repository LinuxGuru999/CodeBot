#!/usr/bin/env python3
"""Discovery Daemon — always-on discovery outside the scheduler.

Purpose
-------
Runs the 9 discovery roles (bug_hunter, security_auditor, etc.) continuously
in a small separate pool (default 2 concurrent) without competing for scheduler
slots. Discovery is read-only: each agent calls create_ticket via api_tools,
never claims tickets or holds a DispatchGate token.

Why outside scheduler
--------------------
Scheduler BucketDispatcher has no DISCOVERY bucket (DISCOVERED intentionally
absent — triage is platform code). Discovery was gated by is_needed_bot()=False
via dispatch_service/orchestrator_services, so it never received slots even
though it has valid work. This daemon restores continuous scanning with a
simple round-robin + per-role cooldown (interval=1800s).

Invariants
----------
 - Own concurrency: 5 slots, stagger 2s, independent of GATEWAY_MAX_CONCURRENT=90.
- Read-only tool policy enforced by role prompt (never writes code).
- Respects drain (.drain) and orchestrator shutdown (_shutdown_requested).
- Cooldown via BotState.next_run_at (set by health_check_loop.handle_exited_bots
  after each run: now + interval). In-memory _last_run covers cold start.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger("discovery_daemon")

_CHANGED_FILES_CACHED: tuple[float, str] = (0.0, "")


def _changed_files_block(state_dir: Path | None = None, limit: int = 30) -> str:
    global _CHANGED_FILES_CACHED
    now = time.time()
    cached_at, cached_block = _CHANGED_FILES_CACHED
    if cached_block and (now - cached_at) < 60:
        return cached_block
    files: list[str] = []
    try:
        import subprocess as _sp
        from codebot.process_manager import _resolve_bots_dir
        cwd = str(_resolve_bots_dir())
        for cmd in (
            ["git", "diff", "--name-only", "HEAD"],
            ["git", "diff", "--name-only"],
        ):
            try:
                r = _sp.run(cmd, capture_output=True, text=True, timeout=2, cwd=cwd)
                if r.returncode == 0 and r.stdout.strip():
                    for line in r.stdout.strip().splitlines():
                        p = line.strip()
                        if p and p not in files:
                            files.append(p)
                    if files:
                        break
            except Exception:
                continue
        if not files:
            try:
                r = _sp.run(["git", "status", "--porcelain"], capture_output=True, text=True, timeout=2, cwd=cwd)
                if r.returncode == 0 and r.stdout.strip():
                    for line in r.stdout.strip().splitlines():
                        parts = line.strip().split()
                        if parts:
                            p = parts[-1].strip().strip('"').strip("'")
                            if p and p not in files:
                                files.append(p)
            except Exception:
                pass
        files = files[:limit]
    except Exception:
        files = []
    if not files:
        block = "--- CHANGED_FILES ---\n(none — full scan)\n--- END CHANGED_FILES ---"
    else:
        block = "--- CHANGED_FILES (dirty-first priority; scan these first) ---\n" + "\n".join(files) + "\n--- END CHANGED_FILES ---"
    _CHANGED_FILES_CACHED = (now, block)
    return block


# Canonical discovery roles — mirrors codebot/roles.py and role_registry.py
DISCOVERY_ROLES: tuple[str, ...] = (
    "bug_hunter",
    "security_auditor",
    "architecture_auditor",
    "performance_auditor",
    "test_gap_auditor",
    "documentation_auditor",
    "dependency_auditor",
    "ux_auditor",
    "feature_hunter",
)

DISCOVERY_INTERVAL_SECONDS = 60
DISCOVERY_MAX_CONCURRENT = 5
DISCOVERY_STAGGER_SECONDS = 2.0
DISCOVERY_TICK_SECONDS = 10
DISCOVERY_HEARTBEAT_TIMEOUT = 300


class DiscoveryDaemon:
    """Small daemon that round-robins discovery roles outside the scheduler."""

    def __init__(
        self,
        bots: dict[str, Any],
        *,
        interval: int = DISCOVERY_INTERVAL_SECONDS,
        max_concurrent: int = DISCOVERY_MAX_CONCURRENT,
        stagger_seconds: float = DISCOVERY_STAGGER_SECONDS,
        tick_seconds: int = DISCOVERY_TICK_SECONDS,
        state_dir: Path | None = None,
    ) -> None:
        self.bots = bots
        self.interval = interval
        self.max_concurrent = max_concurrent
        self.stagger = stagger_seconds
        self.tick_seconds = tick_seconds
        self._state_dir = state_dir
        self._next_index = 0
        self._last_run: dict[str, float] = {}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    # -- helpers ----------------------------------------------------------

    def _running_count(self) -> int:
        n = 0
        for name, bot in self.bots.items():
            base = name.split("-")[0] if "-" in name else name
            if base not in DISCOVERY_ROLES and name not in DISCOVERY_ROLES:
                continue
            proc = getattr(bot, "process", None)
            if proc is not None and proc.poll() is None:
                n += 1
        return n

    def _is_on_cooldown(self, role: str, now: float | None = None) -> bool:
        if now is None:
            now = time.time()
        bot = self.bots.get(role)
        if bot is not None:
            nxt = getattr(bot, "next_run_at", 0) or 0
            try:
                nxt_f = float(nxt)
            except Exception:
                nxt_f = 0.0
            if nxt_f and now < nxt_f:
                return True
        last = self._last_run.get(role, 0.0)
        if last and (now - last) < self.interval:
            return True
        # also check heartbeat file mtime as cold-start cooldown
        try:
            from codebot.process_manager import _resolve_state_dir
            sd = self._state_dir or _resolve_state_dir()
            hb = sd / f"{role}.heartbeat"
            if hb.exists():
                try:
                    hb_ts = float(hb.read_text(encoding="utf-8").strip())
                    if hb_ts and (now - hb_ts) < self.interval:
                        return True
                except Exception:
                    # fallback to file mtime
                    try:
                        mtime = hb.stat().st_mtime
                        if (now - mtime) < self.interval:
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
        return False

    def _ensure_bot(self, role: str) -> Any:
        if role in self.bots:
            bot = self.bots[role]
            # ensure enabled & correct interval
            try:
                bot.config.enabled = True
                if bot.config.interval_seconds != self.interval:
                    bot.config.interval_seconds = self.interval
            except Exception:
                pass
            return bot
        from codebot.process_manager import BotConfig, BotState

        prompt_file = f"codebot/roles/{role}.md"
        # verify prompt exists; fallback to {role}.md at repo root
        try:
            from codebot.process_manager import _resolve_bots_dir
            bots_dir = _resolve_bots_dir()
            if not (bots_dir / prompt_file).exists():
                alt = f"{role}.md"
                if (bots_dir / alt).exists():
                    prompt_file = alt
        except Exception:
            pass
        cheap_roles = {"test_gap_auditor", "documentation_auditor", "dependency_auditor"}
        try:
            if role in cheap_roles:
                model, fallback = "xiaomi-mimo-2.5", "qwen-3.5-plus"
            else:
                from codebot.model_manager import next_model_for_role
                assignment = next_model_for_role(role)
                model = assignment.model
                fallback = assignment.fallback
        except Exception:
            model = "qwen-3.5-plus"
            fallback = "xiaomi-mimo-2.5"
        cfg = BotConfig(
            name=role,
            prompt_file=prompt_file,
            interval_seconds=self.interval,
            heartbeat_timeout=300,
            model=model,
            fallback_model=fallback,
            enabled=True,
            clean_exit_wait=False,
            runner_mode="api",
            tier=11,
            max_restarts=5,
        )
        bot = BotState(config=cfg)
        # restore prior state file values if present (restart_count etc.)
        try:
            from codebot.process_manager import _resolve_state_dir
            sd = self._state_dir or _resolve_state_dir()
            sf = sd / f"{role}.state.json"
            if sf.exists():
                import json
                data = json.loads(sf.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    try:
                        bot.consecutive_errors = int(data.get("consecutive_errors", 0) or 0)
                    except Exception:
                        pass
                    try:
                        nxt = float(data.get("next_run_at", 0) or 0)
                        bot.next_run_at = nxt
                    except Exception:
                        pass
                    try:
                        bot.restart_count = int(data.get("restart_count", 0) or 0)
                    except Exception:
                        pass
        except Exception:
            pass
        self.bots[role] = bot
        return bot

    def _launch(self, bot: Any) -> bool:
        try:
            from codebot.process_manager import _resolve_state_dir, _init_and_prepare_bot, _launch_bot_subprocess
            from codebot.model_manager import next_model_for_role

            sd = self._state_dir
            if sd is None:
                try:
                    sd = _resolve_state_dir()
                except Exception:
                    sd = None
            if sd is not None and (sd / f"{bot.config.name}.paused").exists():
                logger.debug("discovery %s paused, skipping", bot.config.name)
                return False
            # rotate model per launch (cheap tier pinned)
            try:
                base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
                if base in {"test_gap_auditor", "documentation_auditor", "dependency_auditor"}:
                    bot.config.model = "xiaomi-mimo-2.5"
                    bot.config.fallback_model = "qwen-3.5-plus"
                else:
                    assignment = next_model_for_role(base)
                    bot.config.model = assignment.model
                    bot.config.fallback_model = assignment.fallback
            except Exception:
                pass
            bot.config.enabled = True
            is_disc = (bot.config.name in DISCOVERY_ROLES or base in DISCOVERY_ROLES)
            extra = _changed_files_block(sd) if is_disc else ""
            heartbeat_file, ckpt_file, message = _init_and_prepare_bot(bot, True, extra_block=extra)
            state_data = getattr(_init_and_prepare_bot, "_last_state", {"started": time.time()})
            ok = _launch_bot_subprocess(bot, message, heartbeat_file, ckpt_file, state_data)
            if ok:
                logger.info("discovery spawn: %s -> %s", bot.config.name, bot.config.model)
            return bool(ok)
        except FileNotFoundError as e:
            logger.warning("discovery launch missing prompt for %s: %s", bot.config.name, e)
            return False
        except Exception as e:
            logger.warning("discovery launch failed for %s: %s", bot.config.name, e)
            return False

    # -- public -----------------------------------------------------------

    def tick(self) -> int:
        """Try to spawn up to (max_concurrent - running) discovery agents.
        Returns number spawned this tick."""
        try:
            from codebot.state_manager import is_draining
            if is_draining():
                return 0
        except Exception:
            pass
        running = self._running_count()
        if running >= self.max_concurrent:
            return 0
        to_spawn = self.max_concurrent - running
        spawned = 0
        now = time.time()
        start = self._next_index
        next_index = start
        for i in range(len(DISCOVERY_ROLES)):
            if spawned >= to_spawn:
                break
            role = DISCOVERY_ROLES[(start + i) % len(DISCOVERY_ROLES)]
            bot = self.bots.get(role)
            if bot is not None and getattr(bot, "process", None) is not None and bot.process.poll() is None:
                continue
            if self._is_on_cooldown(role, now=now):
                continue
            target = self._ensure_bot(role)
            ok = self._launch(target)
            if ok:
                self._last_run[role] = now
                next_index = (start + i + 1) % len(DISCOVERY_ROLES)
                spawned += 1
                if spawned < to_spawn:
                    time.sleep(self.stagger)
        if spawned:
            self._next_index = next_index
        return spawned

    def loop(self) -> None:
        logger.info(
            "Discovery daemon loop started roles=%s interval=%ss max_concurrent=%s",
            len(DISCOVERY_ROLES), self.interval, self.max_concurrent,
        )
        while not self._stop.is_set():
            try:
                from codebot.orchestrator_runtime import _shutdown_requested
                if _shutdown_requested:
                    break
            except Exception:
                pass
            try:
                self.tick()
            except Exception as e:
                logger.debug("discovery daemon tick error: %s", e)
            # interruptible sleep
            for _ in range(self.tick_seconds):
                if self._stop.is_set():
                    break
                try:
                    from codebot.orchestrator_runtime import _shutdown_requested
                    if _shutdown_requested:
                        break
                except Exception:
                    pass
                time.sleep(1)
        logger.info("Discovery daemon loop stopped")

    def start(self) -> threading.Thread:
        if self._thread is not None and self._thread.is_alive():
            return self._thread
        self._stop.clear()
        t = threading.Thread(target=self.loop, name="discovery-daemon", daemon=True)
        t.start()
        self._thread = t
        return t

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t.is_alive():
            t.join(timeout=timeout)
