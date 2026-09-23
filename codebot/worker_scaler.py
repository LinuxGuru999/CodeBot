"""Worker Scaler — Bot registry, resource checks, and scaling limits."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

CLAIM_TTL_SECONDS = 300
MIN_ROTATING_SLOTS = 4

def rotating_slots(max_concurrent: int = 26) -> int:
    try:
        from codebot.process_manager import _count_api_runner_processes
        running = _count_api_runner_processes()
        return max(0, max_concurrent - running)
    except Exception:
        return 0

def worker_reserved_slots() -> int:
    return 2

def _get_available_memory_mb() -> float:
    try:
        meminfo = Path("/proc/meminfo").read_text()
        for line in meminfo.splitlines():
            if line.startswith("MemAvailable:"):
                return float(line.split()[1]) / 1024
    except Exception:
        pass
    return 0.0

def _model_tier_for_complexity(model: str, complexity: str, queue_has_tier_work: bool = False) -> bool:
    cheap_models = frozenset({"xiaomi-mimo-2.5"})
    expensive_models = frozenset({"qwen-3.8-max", "qwen-3.8-max-thinking", "qwen-3.7-max", "qwen-3.7-max-thinking"})
    if complexity in ("trivial", "small", "medium"):
        return True
    if complexity == "high":
        return model in expensive_models or model not in cheap_models
    if complexity == "critical":
        return model in expensive_models
    return True

def is_manifest_error_disabled(bot_name: str) -> bool:
    from codebot.process_manager import STATE_DIR
    state_file = STATE_DIR / f"{bot_name}.state.json"
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text())
            return data.get("status") == "disabled"
    except Exception:
        pass
    return False

def is_manifest_restart_budget_exceeded(bot_name: str) -> bool:
    from codebot.process_manager import STATE_DIR
    state_file = STATE_DIR / f"{bot_name}.state.json"
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text())
            restart_count = data.get("restart_count", 0)
            max_restarts = 5
            return restart_count >= max_restarts
    except Exception:
        pass
    return False

def _read_state_file(bot_name: str) -> dict:
    from codebot.process_manager import STATE_DIR
    state_file = STATE_DIR / f"{bot_name}.state.json"
    try:
        if state_file.exists():
            return json.loads(state_file.read_text())
    except Exception:
        pass
    return {}

def load_bot_registry(adapter: Any | None = None) -> list:
    from codebot.process_manager import BotConfig
    if adapter is not None:
        try:
            entries = adapter.bot_registry()
            configs = [BotConfig(
                name=e["name"], prompt_file=e.get("prompt", f"{e['name']}.md"),
                interval_seconds=e.get("interval", 600),
                heartbeat_timeout=e.get("interval", 600) * 2,
                model=e.get("model", "default"), fallback_model=e.get("fallback_model", ""),
                enabled=e.get("enabled", True), max_restarts=e.get("max_restarts", 5),
                clean_exit_wait=e.get("clean_exit_wait", True), tier=e.get("tier", 2),
                runner_mode=e.get("runner_mode", "api"),
                max_tokens_per_run=e.get("max_tokens_per_run", 0),
            ) for e in entries]
            if configs:
                logger.info("Loaded %d roles from project adapter", len(configs))
                return configs
        except Exception as e:
            logger.warning("Failed to load registry from adapter: %s", e)
    return [
        BotConfig("discovery", "bug_hunter.md", 1800, 3600, "default", clean_exit_wait=True),
        BotConfig("implementer", "implementer.md", 300, 750, "default", clean_exit_wait=True),
        BotConfig("reviewer", "correctness_reviewer.md", 600, 1500, "default", clean_exit_wait=True),
    ]

def build_bots(registry: list) -> dict:
    from codebot.process_manager import BotState, STATE_DIR
    bots = {}
    for config in registry:
        bot = BotState(config=config)
        sf = STATE_DIR / f"{config.name}.state.json"
        if sf.exists():
            try:
                sd = json.loads(sf.read_text())
                if isinstance(sd, dict):
                    bot.consecutive_errors = sd.get("consecutive_errors", 0)
                    bot.next_run_at = sd.get("next_run_at", 0.0)
                    bot.restart_count = sd.get("restart_count", 0)
            except Exception:
                pass
        bots[config.name] = bot
    return bots
