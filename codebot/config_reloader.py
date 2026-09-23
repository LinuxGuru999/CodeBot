"""Configuration and code hot-reloading for orchestrator.

Extracted from orchestrator.py to satisfy SRP. Handles detection of changes
to prompt files, source code modules, and bot registry configuration,
triggering graceful respawns when updates are detected.
"""

import logging
import os
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
    adapter: Optional[Any] = None,
) -> None:
    """Check for prompt file changes and trigger graceful respawn.
    
    Uses atomic mtime read under exclusive flock (via _prompt_read_lock)
    to prevent TOCTOU race conditions. Uses nanosecond precision (st_mtime_ns)
    to handle filesystem granularity issues and ensure rapid successive updates
    are detected.
    
    Args:
        bots: Dictionary of bot states
        bots_dir: Root directory containing prompt files (fallback if adapter is None)
        stop_bot_fn: Callback to stop a bot (signature: stop_bot(bot, reason))
        adapter: Optional ProjectAdapter instance; if provided, resolves prompt
                 directory via adapter.prompt_directory(), otherwise uses bots_dir.
    """
    # Lazy import to avoid circular dependency issues
    from codebot.process_manager import _prompt_read_lock
    
    # Resolve effective directory: adapter takes precedence, fallback to explicit bots_dir
    if adapter is not None:
        try:
            effective_dir = adapter.prompt_directory()
        except Exception:
            effective_dir = bots_dir
    else:
        effective_dir = bots_dir
    
    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        prompt_path = effective_dir / bot.config.prompt_file
        current_mtime_ns = 0
        
        # Atomic mtime read: acquire exclusive lock, stat (single call), then release
        # Using st_mtime_ns (integer nanoseconds) avoids float precision issues
        # and TOCTOU races from separate exists()/stat() calls.
        try:
            with _prompt_read_lock(prompt_path):
                current_mtime_ns = prompt_path.stat().st_mtime_ns
        except (OSError, FileNotFoundError):
            # File doesn't exist or can't be opened; skip this tick
            continue
        
        if current_mtime_ns == 0:
            continue
        
        # Ensure bot.last_prompt_mtime is treated as nanoseconds (int)
        # If it was initialized as float (seconds), convert on first comparison
        last_mtime_ns = bot.last_prompt_mtime
        if isinstance(last_mtime_ns, float):
            # Convert float seconds to int nanoseconds
            last_mtime_ns = int(last_mtime_ns * 1e9)
            bot.last_prompt_mtime = last_mtime_ns  # Update stored value to int
        
        if last_mtime_ns > 0 and current_mtime_ns > last_mtime_ns:
            alive = bot.process is not None and bot.process.poll() is None
            if alive:
                logger.info(f"Prompt changed for '{name}' \u2014 triggering live reload (graceful respawn)")
                stop_bot_fn(bot, "prompt-hot-reload")
                bot.next_run_at = time.time()
        bot.last_prompt_mtime = current_mtime_ns


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


# Module-level baseline -- persists across ticks for O(M+B) change detection.
_last_code_mtimes: Dict[str, float] = {}


def check_code_changes(
    bots: Dict[str, "BotState"],
    pkg_dir: Path,
    stop_bot_fn: Any,
) -> None:
    """Check for code changes in orchestrator package and respawn affected bots.

    Uses O(M+B) complexity:
    - O(M) set comprehension to detect changed modules against a module-level
      baseline (no per-bot O(M) merge needed)
    - O(B) single iteration over bots to stop active ones

    ``_last_code_mtimes`` persists across ticks.  Empty on first call --
    all modules are treated as changed (safe, no missed updates).

    Args:
        bots: Dictionary of bot states
        pkg_dir: Package directory to monitor
        stop_bot_fn: Callback to stop a bot
    """
    global _last_code_mtimes

    current_mtimes = get_code_mtimes(pkg_dir)
    if not current_mtimes:
        return

    prev, _last_code_mtimes = _last_code_mtimes, dict(current_mtimes)

    # O(M) -- set comprehension over modules only (no per-bot iteration)
    changed_modules: set[str] = {
        mod_name
        for mod_name, mtime in current_mtimes.items()
        if prev.get(mod_name, 0.0) > 0 and mtime > prev[mod_name]
    }

    if not changed_modules:
        return

    unique_changed = sorted(changed_modules)
    logger.info(f"Code change detected in {unique_changed} \u2014 respawning active bots")

    # O(B) -- iterate bots once to stop active ones
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
