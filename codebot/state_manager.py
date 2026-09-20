"""State Manager — Path configuration, drain/lock control, backup/restore."""
from __future__ import annotations

import json
import logging
import os
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = _CODEBOT_PKG_DIR.parent

@dataclass
class PathConfig:
    bots_dir: Path
    state_dir: Path
    logs_dir: Path
    backup_dir: Path
    alignment_events_dir: Path
    drain_file: Path
    update_lock: Path
    restart_file: Path

_paths_state_dir = _project_root / ".codebot" / "state"
_paths_logs_dir = _project_root / ".codebot" / "logs"

_paths = PathConfig(
    bots_dir=_project_root,
    state_dir=_paths_state_dir,
    logs_dir=_paths_logs_dir,
    backup_dir=_paths_state_dir / "backup",
    drain_file=_paths_state_dir / ".drain",
    update_lock=_paths_state_dir / ".update_lock",
    restart_file=_paths_state_dir / ".restart",
    alignment_events_dir=_paths_state_dir / "alignment_events",
)

# Ensure dirs exist
for d in (_paths.state_dir, _paths.logs_dir, _paths.backup_dir, _paths.alignment_events_dir):
    d.mkdir(parents=True, exist_ok=True)

_adapter_instance: Any = None

def set_adapter_instance(adapter: Any) -> None:
    global _adapter_instance
    _adapter_instance = adapter

def set_project_adapter(adapter: Any) -> PathConfig:
    """Return a new PathConfig derived from the adapter without mutating globals.

    Components should use the returned config object or call get_paths() after
    this function has been invoked during bootstrap.  The module-level _paths
    reference is reassigned atomically so that subsequent get_paths() calls
    observe the updated configuration, but no ``global`` keyword is used and
    no individual fields are mutated in-place.
    """
    import codebot.state_manager as _self
    set_adapter_instance(adapter)
    current = _self._paths
    try:
        p = adapter.paths()
        new_bots_dir = getattr(p, 'repository_root', current.bots_dir)
        new_state_dir = getattr(p, 'state_dir', current.state_dir)
        new_logs_dir = getattr(p, 'logs_dir', current.logs_dir)
        new_config = PathConfig(
            bots_dir=new_bots_dir,
            state_dir=new_state_dir,
            logs_dir=new_logs_dir,
            backup_dir=new_state_dir / "backup",
            drain_file=new_state_dir / ".drain",
            update_lock=new_state_dir / ".update_lock",
            restart_file=new_state_dir / ".restart",
            alignment_events_dir=new_state_dir / "alignment_events",
        )
        for d in (new_config.state_dir, new_config.logs_dir, new_config.backup_dir, new_config.alignment_events_dir):
            d.mkdir(parents=True, exist_ok=True)
        _self._paths = new_config
    except Exception as e:
        logger.warning("Failed to update paths from adapter: %s", e)
    return _self._paths

def is_draining() -> bool:
    return get_paths().drain_file.exists()

def set_drain(reason: str = "") -> None:
    get_paths().drain_file.write_text(f"{time.time()}\n{reason}\n")
    logger.info(f"Drain set: {reason}")

def clear_drain() -> None:
    p = get_paths()
    for f in (p.drain_file, p.update_lock):
        try: f.unlink()
        except FileNotFoundError: pass
    logger.info("Drain cleared")

def drain_status() -> dict:
    p = get_paths()
    return {
        "draining": is_draining(),
        "drain_file": str(p.drain_file) if p.drain_file.exists() else None,
        "update_lock": str(p.update_lock) if p.update_lock.exists() else None,
        "drain_reason": p.drain_file.read_text().strip() if p.drain_file.exists() else None,
    }

def backup_botnet(tag: str | None = None) -> Path:
    from codebot.process_manager import BOTS_DIR
    d = get_paths().backup_dir / f"botnet-{tag + '-' if tag else ''}{time.strftime('%Y%m%d-%H%M%S')}"
    d.mkdir(parents=True, exist_ok=True)
    for p in BOTS_DIR.glob("*.md"):
        (d / p.name).write_bytes(p.read_bytes())
    return d

def restore_botnet(backup_dir: Path) -> None:
    from codebot.process_manager import BOTS_DIR
    if not backup_dir.exists():
        raise FileNotFoundError(str(backup_dir))
    for p in backup_dir.glob("*.md"):
        (BOTS_DIR / p.name).write_bytes(p.read_bytes())

def check_self_restart(bots: dict, stop_fn) -> bool:
    p = get_paths()
    if not p.restart_file.exists():
        return False
    try:
        reason = p.restart_file.read_text(encoding="utf-8").strip() or "manual"
    except OSError:
        reason = "manual"
    logger.info(f"Self-restart signal detected (reason: {reason})")
    for bot in bots.values():
        if bot.process is not None and bot.process.poll() is None:
            stop_fn(bot, "self-restart")
    try:
        p.restart_file.unlink(missing_ok=True)
    except OSError:
        pass
    os.execv(sys.executable, [sys.executable] + sys.argv)
    return True

def safe_stop_all(bots: dict, stop_fn) -> dict:
    set_drain("safe-stop")
    res = {}
    from codebot.process_manager import update_bot_state
    for n, b in bots.items():
        if b.process is None or b.process.poll() is not None:
            res[n] = "already stopped"
            update_bot_state(b, "stopped")
            continue
        stop_fn(b, "safe-stop")
        res[n] = "stopped"
        update_bot_state(b, "drained")
    return res

def get_paths() -> PathConfig:
    import codebot.state_manager as _self
    return _self._paths
