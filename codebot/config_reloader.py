"""Configuration and code hot-reloading for orchestrator.

Extracted from orchestrator.py to satisfy SRP. Handles detection of changes
to prompt files, source code modules, and bot registry configuration,
triggering graceful respawns when updates are detected.
"""

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from codebot.orchestrator import BotState

logger = logging.getLogger("orchestrator.config_reloader")


def check_prompt_changes(
    bots: Dict[str, "BotState"],
    bots_dir: Path,
    stop_bot_fn: Any,
) -> None:
    """Check for prompt file changes and trigger graceful respawn.
    
    Args:
        bots: Dictionary of bot states
        bots_dir: Root directory containing prompt files
        stop_bot_fn: Callback to stop a bot (signature: stop_bot(bot, reason))
    """
    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        prompt_path = bots_dir / bot.config.prompt_file
        try:
            current_mtime = prompt_path.stat().st_mtime if prompt_path.exists() else 0.0
        except OSError:
            current_mtime = 0.0
        if current_mtime == 0.0:
            continue
        if bot.last_prompt_mtime > 0 and current_mtime > bot.last_prompt_mtime:
            alive = bot.process is not None and bot.process.poll() is None
            if alive:
                logger.info(f"Prompt changed for '{name}' — triggering live reload (graceful respawn)")
                stop_bot_fn(bot, "prompt-hot-reload")
                bot.next_run_at = time.time()
        bot.last_prompt_mtime = current_mtime


def get_code_mtimes(pkg_dir: Path) -> Dict[str, float]:
    """Get modification times for all Python files in package directory.
    
    Args:
        pkg_dir: Directory to scan for .py files
        
    Returns:
        Dictionary mapping filename to mtime
    """
    mtimes: Dict[str, float] = {}
    try:
        for py_file in pkg_dir.glob("*.py"):
            try:
                mtimes[py_file.name] = py_file.stat().st_mtime
            except OSError:
                pass
    except OSError:
        pass
    return mtimes


def check_code_changes(
    bots: Dict[str, "BotState"],
    pkg_dir: Path,
    stop_bot_fn: Any,
) -> None:
    """Check for code changes in orchestrator package and respawn affected bots.
    
    Args:
        bots: Dictionary of bot states
        pkg_dir: Package directory to monitor
        stop_bot_fn: Callback to stop a bot
    """
    current_mtimes = get_code_mtimes(pkg_dir)
    if not current_mtimes:
        return
    
    changed_modules: list[str] = []
    for mod_name, mtime in current_mtimes.items():
        for bot in bots.values():
            prev = bot.last_code_mtimes.get(mod_name, 0.0)
            if prev > 0 and mtime > prev:
                changed_modules.append(mod_name)
                break
    
    if not changed_modules:
        for bot in bots.values():
            if not bot.last_code_mtimes:
                bot.last_code_mtimes = dict(current_mtimes)
            else:
                bot.last_code_mtimes.update(current_mtimes)
        return
    
    unique_changed = sorted(set(changed_modules))
    logger.info(f"Code change detected in {unique_changed} — respawning active bots")
    
    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        alive = bot.process is not None and bot.process.poll() is None
        if alive:
            stop_bot_fn(bot, f"code-hot-reload:{','.join(unique_changed)}")
            bot.next_run_at = time.time()
        bot.last_code_mtimes = dict(current_mtimes)


def check_config_changes(
    bots: Dict[str, "BotState"],
    adapter: Optional[Any],
    rescale_fn: Any,
) -> None:
    """Check for bot registry configuration changes via ProjectAdapter.
    
    Args:
        bots: Dictionary of bot states
        adapter: ProjectAdapter instance (or None)
        rescale_fn: Callback to rescale worker registry
    """
    if adapter is None:
        return
    
    try:
        new_entries = adapter.bot_registry()
    except Exception:
        return
    
    if not new_entries:
        return
    
    new_map = {e["name"]: e for e in new_entries}
    changed = False
    
    for name, bot in bots.items():
        base = name.split("-")[0] if "-" in name else name
        entry = new_map.get(base)
        if not entry:
            continue
        
        new_interval = entry.get("interval", bot.config.interval_seconds)
        new_model = entry.get("model", bot.config.model)
        new_tier = entry.get("tier", bot.config.tier)
        
        if (new_interval != bot.config.interval_seconds or 
            new_model != bot.config.model or 
            new_tier != bot.config.tier):
            logger.info(
                f"Config changed for '{name}': "
                f"interval={bot.config.interval_seconds}->{new_interval} "
                f"model={bot.config.model}->{new_model}"
            )
            bot.config.interval_seconds = new_interval
            bot.config.heartbeat_timeout = new_interval * 2
            bot.config.model = new_model
            bot.config.fallback_model = entry.get("fallback_model", bot.config.fallback_model)
            bot.config.tier = new_tier
            changed = True
    
    if changed:
        rescale_fn()
