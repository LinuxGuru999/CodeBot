#!/usr/bin/env python3
"""Process Manager — Bot lifecycle management.

Purpose
-------
Manages bot subprocesses: start, stop, restart.
Delegates health monitoring to health_monitor module.

Why
---
Extracted from orchestrator.py to maintain architectural boundaries.
The orchestrator should only coordinate, not manage low-level process details.

Invariants
----------
- Single process (no multiprocessing)
- Heartbeat files are the ONLY communication channel from bots
- Kill signals: SIGTERM first (5s grace), then SIGKILL
- Max restarts per bot per hour: configurable (default 5)
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional

import codebot.health_monitor as _hm
from codebot.health_monitor import (
    heartbeat_path as _hm_heartbeat_path,
    write_heartbeat as _hm_write_heartbeat,
    read_heartbeat as _hm_read_heartbeat,
    batch_read_heartbeats as _hm_batch_read_heartbeats,
    log_path as _hm_log_path,
    log_mtime, is_log_stalled, is_stuck, effective_heartbeat_timeout,
)
from codebot.model_manager import model_profile, ModelProfile, MODEL_PROFILES
from codebot.state_manager import get_paths

logger = logging.getLogger(__name__)


def _resolve_state_dir() -> Path:
    """Resolve state dir, honoring module-level STATE_DIR test patches."""
    # Check for an explicit module-level patch first (existing tests patch
    # codebot.process_manager.STATE_DIR to a tmp dir). This takes precedence
    # even over a patched get_paths, matching pre-refactor behavior where the
    # global was read directly.
    try:
        current_default = _project_root / ".codebot" / "state"
        mod = sys.modules.get(__name__)
        sd = getattr(mod, "STATE_DIR", None) if mod is not None else None
        if sd is not None and Path(sd) != Path(current_default):
            return Path(sd)
    except Exception:
        pass
    try:
        return get_paths().state_dir
    except Exception:
        try:
            mod = sys.modules.get(__name__)
            sd = getattr(mod, "STATE_DIR", None) if mod is not None else None
            if sd is not None:
                return Path(sd)
        except Exception:
            pass
        raise


def _resolve_logs_dir() -> Path:
    """Resolve logs dir, honoring module-level LOGS_DIR test patches."""
    try:
        current_default = _project_root / ".codebot" / "logs"
        mod = sys.modules.get(__name__)
        ld = getattr(mod, "LOGS_DIR", None) if mod is not None else None
        if ld is not None and Path(ld) != Path(current_default):
            return Path(ld)
    except Exception:
        pass
    try:
        return get_paths().logs_dir
    except Exception:
        try:
            mod = sys.modules.get(__name__)
            ld = getattr(mod, "LOGS_DIR", None) if mod is not None else None
            if ld is not None:
                return Path(ld)
        except Exception:
            pass
        raise


def _resolve_bots_dir() -> Path:
    """Resolve bots dir, honoring module-level BOTS_DIR test patches."""
    try:
        mod = sys.modules.get(__name__)
        bd = getattr(mod, "BOTS_DIR", None) if mod is not None else None
        if bd is not None and Path(bd) != Path(_project_root):
            return Path(bd)
    except Exception:
        pass
    try:
        return get_paths().bots_dir
    except Exception:
        try:
            mod = sys.modules.get(__name__)
            bd = getattr(mod, "BOTS_DIR", None) if mod is not None else None
            if bd is not None:
                return Path(bd)
        except Exception:
            pass
        raise


def read_heartbeat(bot_name: str) -> float:
    """Read heartbeat, honoring health_monitor.STATE_DIR test patches."""
    try:
        hm_state = getattr(_hm, "STATE_DIR", None)
        hm_default = _hm._project_root / ".codebot" / "state"
        if hm_state is not None and Path(hm_state) != Path(hm_default):
            hb = Path(hm_state) / f"{bot_name}.heartbeat"
            if not hb.exists():
                return 0.0
            txt = hb.read_text().strip()
            try:
                return float(txt)
            except (ValueError, OSError):
                return 0.0
    except Exception:
        pass
    return _hm_read_heartbeat(bot_name)


def batch_read_heartbeats(bot_names: list) -> dict:
    """Batch-read heartbeats, honoring health_monitor.STATE_DIR test patches."""
    try:
        hm_state = getattr(_hm, "STATE_DIR", None)
        hm_default = _hm._project_root / ".codebot" / "state"
        if hm_state is not None and Path(hm_state) != Path(hm_default):
            results: dict = {}
            for n in bot_names:
                hb = Path(hm_state) / f"{n}.heartbeat"
                if not hb.exists():
                    results[n] = 0.0
                    continue
                try:
                    results[n] = float(hb.read_text().strip())
                except (ValueError, OSError):
                    results[n] = 0.0
            return results
    except Exception:
        pass
    return _hm_batch_read_heartbeats(list(bot_names))


def write_heartbeat(bot_name: str) -> None:
    """Write heartbeat via dynamic path resolution."""
    _resolve_state_dir().mkdir(parents=True, exist_ok=True)
    (_resolve_state_dir() / f"{bot_name}.heartbeat").write_text(str(time.time()))


# ---------------------------------------------------------------------------
# Dynamic path helpers — resolve via get_paths() instead of globals
# ---------------------------------------------------------------------------

def heartbeat_path(bot_name: str) -> Path:
    """Path to bot's heartbeat file, resolved dynamically.

    Uses heartbeat_path()/log_path() helpers backed by get_paths() so bot
    spawning writes to adapter-provided locations. Module-level STATE_DIR
    patches from existing tests take precedence for backward compatibility.
    """
    return _resolve_state_dir() / f"{bot_name}.heartbeat"


def log_path(bot_name: str) -> Path:
    """Path to bot's log file, resolved dynamically."""
    return _resolve_logs_dir() / f"{bot_name}.log"


# ---------------------------------------------------------------------------
# Cross-platform file locking
# ---------------------------------------------------------------------------

def _get_flock_function():
    """Return a flock-like function compatible with the current platform."""
    try:
        import fcntl
        def _unix_flock(fd, operation):
            fcntl.flock(fd, operation)
        return _unix_flock
    except ImportError:
        pass

    try:
        import msvcrt
        def _windows_flock(fd, operation):
            if operation == 2:  # LOCK_EX
                msvcrt.locking(fd, 1, 1)  # LK_LOCK = 1
            elif operation == 8:  # LOCK_UN
                msvcrt.locking(fd, 2, 1)  # LK_UNLCK = 2
        return _windows_flock
    except ImportError:
        pass

    def _noop_flock(fd, operation):
        pass
    return _noop_flock

_flock = _get_flock_function()

try:
    import fcntl
    LOCK_EX = fcntl.LOCK_EX
    LOCK_UN = fcntl.LOCK_UN
except ImportError:
    LOCK_EX = 2
    LOCK_UN = 8

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = _CODEBOT_PKG_DIR.parent
STATE_DIR = _project_root / ".codebot" / "state"
LOGS_DIR = _project_root / ".codebot" / "logs"
BOTS_DIR = _project_root

# Ensure directories exist
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# ---------------------------------------------------------------------------
# Bot Configuration and State
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    """Configuration for a single bot."""
    name: str
    prompt_file: str
    interval_seconds: int
    heartbeat_timeout: int
    model: str = "xiaomi-mimo-2.5"
    fallback_model: str = ""
    enabled: bool = True
    max_restarts: int = 5
    clean_exit_wait: bool = False
    tier: int = 2
    runner_mode: str = "api"
    max_tokens_per_run: int = 0
    fallback_models: tuple[str, ...] = ()


@dataclass
class BotState:
    config: BotConfig
    process: Optional[subprocess.Popen] = None
    last_heartbeat: float = 0.0
    last_log_mtime: float = 0.0
    next_run_at: float = 0.0
    restart_count: int = 0
    last_restart_reset: float = 0.0
    consecutive_errors: int = 0
    last_throttle_log: float = 0.0
    started_at: float | None = None
    prompt_mtime: float = 0.0
    last_prompt_mtime: float = 0.0
    last_code_mtimes: dict[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Process Management
# ---------------------------------------------------------------------------

GATEWAY_MAX_CONCURRENT = int(os.getenv("CODEBOT_MAX_CONCURRENT", "26"))
GATEWAY_MIN_SPAWN_GAP = int(os.getenv("CODEBOT_MIN_SPAWN_GAP", "25"))

_last_spawn_time: float = 0.0
_SPAWN_STAGGER_SECONDS: float = 5.0


def _count_api_runner_processes() -> int:
    """Count running module-invoked api runner processes using pgrep.

    FAIL-CLOSED: On any error (timeout, missing pgrep, OS error) we return
    a large number instead of 0.  Returning 0 would make the spawn gate
    think no processes are running, allowing unlimited spawning which can
    exhaust system resources.  Returning a large number blocks new spawns
    until the next successful check — the safe direction.
    """
    _FAIL_CLOSED = 9999
    try:
        result = subprocess.run(
            ["pgrep", "-f", "codebot.api_runner"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return len([line for line in result.stdout.strip().split("\n") if line.strip()])
        # pgrep returns 1 when no processes match — that's a real 0, not an error
        return 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("_count_api_runner_processes failed (%s) — returning fail-closed count", type(exc).__name__)
        return _FAIL_CLOSED


def _spawn_gate(bots: dict[str, BotState] | None = None, is_queued: bool = False,
                runner_mode: str = "api", bot_model: str = "", bot_name: str = "",
                is_overture: bool = False, is_demand: bool = False) -> tuple[bool, str]:
    global _last_spawn_time
    now = time.time()
    running = _count_api_runner_processes()

    cap = GATEWAY_MAX_CONCURRENT
    if running >= cap:
        return False, f"cap {running}/{cap} running"

    has_assignment = bot_name and any(
        b.config.name == bot_name and getattr(b, '_assigned_ticket_id', '')
        for b in (bots.values() if bots else [])
    )
    if not has_assignment and bot_name:
        claims_dir = _resolve_state_dir() / "claims"
        if claims_dir.exists():
            for cf in claims_dir.glob(f"*.{bot_name}.json"):
                try:
                    age = time.time() - cf.stat().st_mtime
                    if age < 60:
                        has_assignment = True
                        break
                except OSError:
                    pass
    if has_assignment or is_demand:
        cap = GATEWAY_MAX_CONCURRENT + 2
        if running >= cap:
            return False, f"cap {running}/{cap} running (with assignment)"

    if is_demand or is_overture or has_assignment:
        return True, "slot available"

    if now - _last_spawn_time < _SPAWN_STAGGER_SECONDS:
        gap = now - _last_spawn_time
        worker_gap = _SPAWN_STAGGER_SECONDS
        if bot_name and bot_name.startswith("worker-"):
            if gap < worker_gap:
                return False, f"gap {gap:.0f}s<{worker_gap}s (worker)"
        elif gap < GATEWAY_MIN_SPAWN_GAP:
            return False, f"gap {gap:.0f}s<{GATEWAY_MIN_SPAWN_GAP}s"

    return True, "slot available"


def _write_json_atomic(path: Path, data: dict | list | str) -> None:
    """Write JSON to path atomically via tmp+replace."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else data)
    tmp.replace(path)


@contextmanager
def _state_write_lock(bot_name: str) -> Iterator[None]:
    sd = _resolve_state_dir()
    sd.mkdir(parents=True, exist_ok=True)
    lock_path = sd / f"{bot_name}.state.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        _flock(lock.fileno(), LOCK_EX)
        try:
            yield
        finally:
            _flock(lock.fileno(), LOCK_UN)


@contextmanager
def _prompt_read_lock(path: Path) -> Iterator[None]:
    """Acquire an exclusive lock on a prompt file for atomic stat+read.

    Uses fcntl.flock on Unix or msvcrt.locking on Windows.
    The lock is released when the context manager exits.
    This prevents race conditions where concurrent writes modify the file
    between stat() and read_text() calls.
    """
    fd = None
    try:
        fd = os.open(str(path), os.O_RDONLY)
        _flock(fd, LOCK_EX)
        yield
    finally:
        if fd is not None:
            try:
                _flock(fd, LOCK_UN)
            except OSError:
                pass
            try:
                os.close(fd)
            except OSError:
                pass


def update_bot_state(bot: BotState, status: str) -> None:
    state_file = _resolve_state_dir() / f"{bot.config.name}.state.json"
    try:
        with _state_write_lock(bot.config.name):
            try:
                data = json.loads(state_file.read_text()) if state_file.exists() else {}
            except (json.JSONDecodeError, ValueError):
                data = {}
            if not isinstance(data, dict):
                data = {}
            data["status"] = status
            data["last_update"] = time.time()
            data["restart_count"] = bot.restart_count
            data["consecutive_errors"] = bot.consecutive_errors
            data["next_run_at"] = bot.next_run_at
            if bot.restart_count > 0:
                existing = data.get("restart_timestamps", [])
                if not isinstance(existing, list):
                    existing = []
                existing_clean = [float(t) for t in existing if isinstance(t, (int, float))]
                data["restart_timestamps"] = existing_clean
                if len(existing_clean) == 0 and bot.last_restart_reset > 0:
                    data["restart_timestamps"] = [bot.last_restart_reset]
            _write_json_atomic(state_file, data)
    except Exception as e:
        logger.error(f"Failed to update state for '{bot.config.name}': {e}")


def checkpoint_path(bot_name: str) -> Path:
    return _resolve_state_dir() / f"{bot_name}.checkpoint.json"


# ---------------------------------------------------------------------------
# Checkpoint Helpers
# ---------------------------------------------------------------------------

def _load_json_file(path: Path) -> tuple[dict | None, str]:
    """Read and parse a JSON file. Returns (data, raw_text) or (None, '')."""
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        return data, raw
    except (json.JSONDecodeError, UnicodeDecodeError, OSError):
        return None, ""


def _discard_and_read_backup(p: Path, bak: Path) -> dict | None:
    """Move corrupt p to bak, then try reading bak. Returns dict or None."""
    try:
        p.rename(bak)
    except OSError:
        try:
            p.unlink()
        except OSError:
            pass
    if not bak.exists():
        return None
    data, _ = _load_json_file(bak)
    return data if isinstance(data, dict) else None


def read_checkpoint(bot_name: str) -> dict | None:
    p = checkpoint_path(bot_name)
    bak = p.with_suffix(".bak") if p.suffix == ".json" else Path(str(p) + ".bak")

    # Main file missing — try backup
    if not p.exists():
        if bak.exists():
            data, _ = _load_json_file(bak)
            if isinstance(data, dict):
                logger.info(f"Restored last-good checkpoint for '{bot_name}' from .bak")
                return data
        return None

    # Read and parse main file
    data, raw = _load_json_file(p)
    if data is None and not raw:
        # File exists but couldn't read at all
        if not p.exists():
            # _load_json_file might have failed mid-read
            return _discard_and_read_backup(p, bak) if p.exists() else None
    if data is None:
        logger.warning(f"Checkpoint corrupt for '{bot_name}' — falling back to .bak")
        return _discard_and_read_backup(p, bak)

    if not isinstance(data, dict):
        logger.warning(f"Checkpoint for '{bot_name}' is not a JSON object — falling back to .bak")
        return _discard_and_read_backup(p, bak)

    # Check size and backup good copy
    if len(raw.encode("utf-8")) > 4096:
        logger.warning(f"Checkpoint for '{bot_name}' exceeds 4KB — truncating")
    try:
        bak.write_text(raw, encoding="utf-8")
    except OSError:
        pass
    return data


# ---------------------------------------------------------------------------
# Start Bot Helpers
# ---------------------------------------------------------------------------

_ALWAYS_RESPAWN: frozenset[str] = frozenset()


def _check_inputs_changed(bot: BotState, last_run_mtime: float) -> bool:
    """Check if manifest inputs or prompt file changed since last run."""
    if last_run_mtime <= 0:
        return True
    # Check manifest inputs
    bots_dir = _resolve_bots_dir()
    try:
        manifest_path = bots_dir / "manifests" / f"{bot.config.name.replace('-', '_')}.json"
        if manifest_path.exists():
            mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
            for inp in mdata.get("input", []):
                ipath = Path(inp.get("path", ""))
                if not ipath.is_absolute():
                    ipath = bots_dir / ipath
                if ipath.exists() and ipath.stat().st_mtime > last_run_mtime:
                    return True
    except Exception:
        return True
    # Check prompt file
    prompt_path = bots_dir / bot.config.prompt_file
    if prompt_path.exists() and prompt_path.stat().st_mtime > last_run_mtime:
        return True
    return False


def _should_skip_run(bot: BotState, last_run_mtime: float, is_demand: bool = False) -> bool:
    """Determine if bot should skip this run (no input changes, not worker/demand)."""
    # Demand-driven agents and ticket-assigned bots always run
    if is_demand or getattr(bot, '_assigned_ticket_id', ''):
        return False
    if _check_inputs_changed(bot, last_run_mtime):
        return False
    if last_run_mtime <= 0:
        return False
    if bot.config.name in _ALWAYS_RESPAWN:
        return False
    # Workers always run
    if bot.config.name.startswith("worker-"):
        return False
    return True


def _prepare_prompt_with_context(bot: BotState) -> str:
    """Read prompt file and inject ticket context + scratchpad handoff.

    Acquires an exclusive lock on the prompt file before stat() and holds it
    through read() to prevent race conditions with concurrent writes that
    could cause stale/inconsistent reads or mismatched mtime/content.
    """
    prompt_path = _resolve_bots_dir() / bot.config.prompt_file

    # Acquire lock, then stat() and read() atomically
    try:
        with _prompt_read_lock(prompt_path):
            bot.prompt_mtime = prompt_path.stat().st_mtime
            bot.last_prompt_mtime = bot.prompt_mtime
            prompt_text = prompt_path.read_text()
    except FileNotFoundError:
        logger.warning(f"Prompt file not found: {prompt_path}")
        bot.prompt_mtime = 0.0
        bot.last_prompt_mtime = 0.0
        return ""
    except Exception as e:
        logger.error(f"Failed to read prompt file {prompt_path}: {e}")
        bot.prompt_mtime = 0.0
        bot.last_prompt_mtime = 0.0
        return ""

    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
    logger.info(f"start_bot '{bot.config.name}': assigned_tid='{assigned_tid}', prompt={len(prompt_text)} chars")

    if assigned_tid:
        ticket_ctx = _load_ticket_context(assigned_tid)
        if ticket_ctx:
            prompt_text = f"{prompt_text}\n\n{ticket_ctx}"
            logger.info(f"Injected ticket context for {bot.config.name}: {assigned_tid} ({len(ticket_ctx)} chars)")
        else:
            logger.warning(f"No ticket context found for {bot.config.name}: {assigned_tid}")

    # Inject handoff note from scratchpad
    try:
        from codebot.scratchpad import load_scratchpad, create_handoff_note
        if assigned_tid:
            scratch_state = load_scratchpad(_resolve_state_dir(), assigned_tid)
            if scratch_state.agent_history or scratch_state.completed_steps:
                handoff = create_handoff_note(scratch_state)
                prompt_text = f"{prompt_text}\n\n{handoff}\nResume from where the previous agent left off. Do NOT redo completed work."
    except Exception:
        pass
    return prompt_text


def _build_checkpoint_block(bot_name: str, ckpt: dict | None, ckpt_file: Path) -> str:
    """Build the checkpoint handoff text block for the mission message."""
    if not ckpt:
        return ""
    try:
        return (
            f"\n--- CHECKPOINT HANDOFF (previous agent stalled) ---\n"
            f"The previous agent for '{bot_name}' left this checkpoint at "
            f"{ckpt.get('updated_at', '?')} (reason: {ckpt.get('reason', 'lockup')}).\n"
            f"Resume from here first \u2014 do NOT redo completed work:\n"
            f"```json\n{json.dumps(ckpt, indent=2)[:6000]}\n```\n"
            f"Checkpoint file: {ckpt_file}\n"
            f"After resuming, update the checkpoint with your progress.\n"
        )
    except Exception:
        return ""


def _build_mission_message(bot: BotState, prompt_text: str, heartbeat_file: Path,
                           ckpt_file: Path, ckpt_block: str) -> str:
    """Build the full mission message for the bot subprocess."""
    state_dir = _resolve_state_dir()
    logs_dir = _resolve_logs_dir()
    bots_dir = _resolve_bots_dir()
    prompt_path = bots_dir / bot.config.prompt_file
    try:
        from codebot.prompt_gateway import build_message as _gateway_build_message
        _GATEWAY = True
    except ImportError:
        _GATEWAY = False

    if _GATEWAY:
        return _gateway_build_message(
            bot.config.name, bot.config.model, prompt_text,
            str(heartbeat_file), str(ckpt_file), ckpt_block,
            str(state_dir), str(logs_dir), prompt_path.name)

    return (
        f"Sisyphus \u2014 delegated task: '{bot.config.name}' workflow (model {bot.config.model}).\n"
        f"Remain Sisyphus; do not adopt a new identity. Execute the specification below as a bounded delegated task, not an infinite daemon.\n"
        f"- At startup and after every atomic task, write Unix timestamp to {heartbeat_file} using the `write` tool.\n"
        f"- After every atomic task, write <4KB checkpoint to {ckpt_file} using the `write` tool (atomic tmp->replace).\n"
        f"- Before each atomic task, run `bash` with `test -f {state_dir}/.drain || test -f {state_dir}/.update_lock && echo DRAIN` to check for drain. If output contains DRAIN, exit 0. Do NOT use the `read` tool for drain checks.\n"
        f"- State dir: {state_dir}  Log dir: {logs_dir}  Prompt: {prompt_path.name}\n"
        f"{ckpt_block}\n"
        f"--- Task Specification ({prompt_path.name}) ---\n"
        f"{prompt_text}"
    )


def _build_popen_args(bot: BotState, heartbeat_file: Path,
                      ckpt_file: Path, mission_file: Path) -> list[str]:
    """Build the argument list for the api_runner subprocess."""
    fb_models = ",".join(bot.config.fallback_models) if bot.config.fallback_models else ""
    return [
        sys.executable, "-m", "codebot.api_runner",
        bot.config.name, bot.config.model,
        str(heartbeat_file), str(ckpt_file), str(mission_file),
        bot.config.fallback_model, str(bot.config.max_tokens_per_run), fb_models,
    ]


def _update_bot_after_launch(bot: BotState, state_data: dict) -> None:
    """Update bot state fields after successful subprocess launch."""
    bot.last_heartbeat = time.time()
    bot.last_log_mtime = time.time()
    bot.next_run_at = time.time() + bot.config.interval_seconds
    bot.consecutive_errors = 0
    bot.started_at = state_data["started"]
    global _last_spawn_time
    _last_spawn_time = time.time()
    try:
        (_resolve_state_dir() / ".last_spawn").write_text(str(time.time()))
    except OSError:
        pass


def _launch_bot_subprocess(bot: BotState, message: str, heartbeat_file: Path,
                           ckpt_file: Path, state_data: dict) -> bool:
    """Launch bot subprocess with mission message. Returns True on success."""
    logs_dir = _resolve_logs_dir()
    bots_dir = _resolve_bots_dir()
    mission_file = logs_dir / f"{bot.config.name}.mission"
    mission_file.write_text(message, encoding="utf-8")
    child_env = os.environ.copy()
    child_env["PYTHONPATH"] = str(bots_dir)
    log_file = logs_dir / f"{bot.config.name}.log"
    try:
        log_fh = open(log_file, "a")
        args = _build_popen_args(bot, heartbeat_file, ckpt_file, mission_file)
        process = subprocess.Popen(
            args, stdout=log_fh, stderr=subprocess.STDOUT,
            cwd=str(bots_dir), env=child_env, start_new_session=True,
        )
        bot.process = process
        _update_bot_after_launch(bot, state_data)
        log_fh.close()
        logger.info(f"Started bot '{bot.config.name}' (PID {process.pid})")
        return True
    except (FileNotFoundError, Exception) as e:
        if isinstance(e, FileNotFoundError):
            logger.error(f"Prompt runner not found for '{bot.config.name}'")
        else:
            logger.error(f"Failed to start bot '{bot.config.name}': {e}")
        if 'log_fh' in locals():
            log_fh.close()
        return False


# ---------------------------------------------------------------------------
# Main Start/Stop/Restart
# ---------------------------------------------------------------------------

def _init_and_prepare_bot(bot: BotState, resume_checkpoint: bool) -> tuple[Path, Path, str]:
    """Initialize state, prepare prompt, and build mission message. Returns (heartbeat_file, ckpt_file, message)."""
    bots_dir = _resolve_bots_dir()
    state_dir = _resolve_state_dir()
    prompt_file = bots_dir / bot.config.prompt_file
    if not prompt_file.exists():
        raise FileNotFoundError(f"Prompt file not found: {prompt_file}")

    state_data = {"bot": bot.config.name, "started": time.time(), "session": 0, "status": "starting"}
    _write_json_atomic(state_dir / f"{bot.config.name}.state.json", state_data)
    write_heartbeat(bot.config.name)
    bot.last_heartbeat = time.time()

    prompt_text = _prepare_prompt_with_context(bot)
    if not bot.last_code_mtimes:
        bot.last_code_mtimes = _get_code_mtimes()

    heartbeat_file = heartbeat_path(bot.config.name)
    ckpt_file = checkpoint_path(bot.config.name)
    ckpt = read_checkpoint(bot.config.name) if resume_checkpoint else None
    ckpt_block = _build_checkpoint_block(bot.config.name, ckpt, ckpt_file)
    message = _build_mission_message(bot, prompt_text, heartbeat_file, ckpt_file, ckpt_block)

    # Stash state_data on the function object for the caller
    _init_and_prepare_bot._last_state = state_data
    return heartbeat_file, ckpt_file, message


def start_bot(bot: BotState, resume_checkpoint: bool = True,
              checkpoint_reason: str | None = None,
              bots: dict[str, BotState] | None = None,
              is_overture: bool = False, is_demand: bool = False) -> bool:
    """Spawn a bot as a subprocess. Returns True on success."""
    state_dir = _resolve_state_dir()
    if (state_dir / f"{bot.config.name}.paused").exists():
        update_bot_state(bot, "paused")
        return False

    ckpt = checkpoint_path(bot.config.name)
    last_run_mtime = ckpt.stat().st_mtime if ckpt.exists() else 0.0

    if _should_skip_run(bot, last_run_mtime, is_demand=is_demand):
        logger.info(f"Bot '{bot.config.name}' skipped \u2014 no input change since last run")
        bot.next_run_at = time.time() + bot.config.interval_seconds
        update_bot_state(bot, "noop")
        return False

    due = bot.next_run_at
    is_queued = _is_queued(bot)
    ok, why = _spawn_gate(bots=bots, is_queued=is_queued, runner_mode="api",
                          bot_model=bot.config.model, bot_name=bot.config.name,
                          is_overture=is_overture, is_demand=is_demand)
    if not ok:
        if due:
            bot.next_run_at = due
        logger.info(f"Queued '{bot.config.name}' ({why})")
        update_bot_state(bot, "queued")
        return False

    try:
        heartbeat_file, ckpt_file, message = _init_and_prepare_bot(bot, resume_checkpoint)
        state_data = _init_and_prepare_bot._last_state
    except FileNotFoundError as e:
        logger.error(str(e))
        return False

    return _launch_bot_subprocess(bot, message, heartbeat_file, ckpt_file, state_data)


def stop_bot(bot: BotState, reason: str = "manual") -> bool:
    """Stop a bot gracefully. SIGTERM → 5s → SIGKILL."""
    if not bot.process:
        return True

    pid = bot.process.pid
    logger.info(f"Stopping bot '{bot.config.name}' (PID {pid}, reason: {reason})")

    try:
        os.kill(pid, signal.SIGTERM)
        try:
            bot.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            logger.warning(f"Bot '{bot.config.name}' did not stop, sending SIGKILL")
            os.kill(pid, signal.SIGKILL)
            bot.process.wait(timeout=3)
    except ProcessLookupError:
        pass
    except Exception as e:
        logger.error(f"Error stopping bot '{bot.config.name}': {e}")

    bot.process = None
    return True


def restart_bot(bot: BotState, reason: str = "stuck",
                bots: dict[str, BotState] | None = None) -> bool:
    """Stop and restart a bot. Respects rate limits."""
    per_bot_drain = _resolve_state_dir() / f".drain_{bot.config.name}"
    if per_bot_drain.exists():
        try:
            per_bot_drain.unlink()
        except OSError:
            pass
    now = time.time()

    if now - bot.last_restart_reset > 3600:
        bot.restart_count = 0
        bot.last_restart_reset = now

    if bot.restart_count >= bot.config.max_restarts:
        logger.error(
            f"Bot '{bot.config.name}' exceeded max restarts "
            f"({bot.config.max_restarts}/hour). Disabling."
        )
        bot.config.enabled = False
        return False

    stop_bot(bot, reason)
    prof = model_profile(bot.config.model)
    cooldown = prof.restart_cooldown if prof else 2
    time.sleep(cooldown)
    bot.restart_count += 1
    try:
        _manifest_restart_record(bot.config.name, now)
    except Exception:
        pass
    logger.info(
        f"Restarting bot '{bot.config.name}' "
        f"(restart {bot.restart_count}/{bot.config.max_restarts})"
    )
    return start_bot(bot, bots=bots)


def _is_queued(bot: BotState) -> bool:
    state_file = _resolve_state_dir() / f"{bot.config.name}.state.json"
    try:
        if state_file.exists():
            return json.loads(state_file.read_text()).get("status") == "queued"
    except Exception:
        pass
    return False


def _get_code_mtimes() -> dict[str, float]:
    mtimes: dict[str, float] = {}
    pkg_dir = Path(__file__).parent
    try:
        for py_file in pkg_dir.glob("*.py"):
            try:
                mtimes[py_file.name] = py_file.stat().st_mtime
            except OSError:
                pass
    except OSError:
        pass
    return mtimes


def _load_ticket_context(ticket_id: str) -> str:
    """Load ticket context for injection into bot prompt."""
    try:
        from codebot.ticket_dispatcher import get_ticket_store
        ts = get_ticket_store()
        if ts is None:
            return ""
        t = ts.get(ticket_id)
        if t is None:
            return ""
        lines = [
            f"--- ASSIGNED TICKET: {t.id} ---",
            f"Title: {t.title}",
            f"Class: {t.ticket_class.value}",
            f"Severity: {t.severity.value}",
            f"Problem: {t.problem_statement}",
            f"Desired State: {t.desired_state}",
        ]
        if t.acceptance_criteria:
            lines.append("Acceptance Criteria:")
            for ac in t.acceptance_criteria:
                lines.append(f"  - {ac}")
        if t.affected_modules:
            lines.append(f"Affected Modules: {', '.join(t.affected_modules)}")
        if t.rework_count > 0:
            lines.append(f"Rework Count: {t.rework_count}")
        if t.reviewer_feedback:
            lines.append("")
            lines.append("=== REVIEWER FEEDBACK (address these issues) ===")
            for i, fb in enumerate(t.reviewer_feedback, 1):
                lines.append(f"\nFeedback #{i} from {fb.get('reviewer', 'unknown')}:")
                if fb.get('file'):
                    lines.append(f"  File: {fb['file']}")
                if fb.get('description'):
                    lines.append(f"  Issue: {fb['description']}")
                if fb.get('recommendation'):
                    lines.append(f"  Fix: {fb['recommendation']}")
            lines.append("=== END REVIEWER FEEDBACK ===")
        lines.append("--- END TICKET CONTEXT ---")
        return "\n".join(lines)
    except Exception:
        return ""


def _manifest_restart_record(name: str, now: float) -> None:
    with _state_write_lock(name):
        state_file = _resolve_state_dir() / f"{name}.state.json"
        try:
            if state_file.exists():
                state = json.loads(state_file.read_text())
            else:
                state = {}
        except (json.JSONDecodeError, ValueError):
            state = {}
        timestamps = state.get("restart_timestamps", [])
        if not isinstance(timestamps, list):
            timestamps = []
        timestamps = [float(t) for t in timestamps if isinstance(t, (int, float))]
        timestamps.append(now)
        state["restart_timestamps"] = timestamps
        state["restart_count"] = len([t for t in timestamps if (now - t) < 3600])
        state["last_update"] = now
        state["last_restart"] = now
        _write_json_atomic(state_file, state)


# Placeholder for ALWAYS_RESPAWN - should be imported or defined
ALWAYS_RESPAWN: frozenset[str] = frozenset()
