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
import threading
import time
from contextlib import contextmanager
from uuid import uuid4
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterator, Optional, Protocol, runtime_checkable

import codebot.health_monitor as _hm
from codebot.health_monitor import (
    read_heartbeat as _hm_read_heartbeat,
    batch_read_heartbeats as _hm_batch_read_heartbeats,
    log_mtime, is_log_stalled, is_stuck, effective_heartbeat_timeout,
)
from codebot.model_manager import model_profile, ModelProfile, MODEL_PROFILES
from codebot.state_manager import get_paths
from codebot.scheduler_config import MAX_CONCURRENT_AGENTS
from codebot.dispatch_service import batch_read_bot_states

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Bot Registry — lazy-loaded to avoid circular import with orchestrator_services
# ---------------------------------------------------------------------------
_BOT_REGISTRY_CACHE: list | None = None


def _load_bot_registry_lazy() -> list:
    """Load bot registry from orchestrator_services (lazy to avoid circular import)."""
    global _BOT_REGISTRY_CACHE
    if _BOT_REGISTRY_CACHE is None:
        try:
            from codebot.orchestrator_services import load_bot_registry as _svc_load
            _BOT_REGISTRY_CACHE = _svc_load()
        except Exception as e:
            logger.warning("Failed to load BOT_REGISTRY from orchestrator_services: %s", e)
            _BOT_REGISTRY_CACHE = []
    return _BOT_REGISTRY_CACHE


class _BotRegistryProxy(list):
    """List proxy that lazy-loads BOT_REGISTRY on first access."""
    def __iter__(self):
        return iter(_load_bot_registry_lazy())
    def __len__(self):
        return len(_load_bot_registry_lazy())
    def __getitem__(self, idx):
        return _load_bot_registry_lazy()[idx]
    def __contains__(self, item):
        return item in _load_bot_registry_lazy()
    def __bool__(self):
        return bool(_load_bot_registry_lazy())


BOT_REGISTRY: list = _BotRegistryProxy()


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
# Cross-platform file locking — single source of truth is codebot.locks.
# ---------------------------------------------------------------------------
# WHY (CB-5603529-75D5): private fcntl/msvcrt/no-op duplicates drifted from
# the canonical primitive (byte-range vs whole-file on Windows, missing
# LOCK_SH/LOCK_NB handling). Import the shared flock so every caller gets
# identical semantics; keep module-level LOCK_EX/LOCK_UN aliases plus a
# private _flock alias so existing tests/patches keep working.

try:
    from codebot.locks import LOCK_EX as _LOCK_EX_SHARED
    from codebot.locks import LOCK_UN as _LOCK_UN_SHARED
    from codebot.locks import flock as _shared_flock
    LOCK_EX = _LOCK_EX_SHARED
    LOCK_UN = _LOCK_UN_SHARED

    def _flock(fd: int, operation: int) -> None:
        """Delegate to the shared cross-platform flock (see codebot.locks)."""
        _shared_flock(fd, operation)
except ImportError:  # pragma: no cover - locks module must exist
    def _get_flock_function():  # type: ignore[no-redef]
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
    prompt_mtime: int = 0
    last_prompt_mtime: int = 0
    last_code_mtimes: dict[str, float] = field(default_factory=dict)
    status: str = ""


# ---------------------------------------------------------------------------
# Prompt Gateway Interface — injected service seam (CB-7976975-5D65)
# ---------------------------------------------------------------------------

@runtime_checkable
class PromptGatewayProtocol(Protocol):
    """Interface for prompt gateway operations.

    Abstracts codebot.prompt_gateway so orchestrator/process_manager delegate
    via the interface and tests can mock the gateway without direct imports.
    """

    def build_message(
        self,
        bot: str,
        model: str,
        prompt_text: str,
        heartbeat_file: str,
        ckpt_file: str,
        ckpt_block: str,
        state_dir: str,
        logs_dir: str,
        prompt_name: str,
    ) -> str:
        """Assemble the spawn message."""
        ...

    def note_spawn(self) -> None:
        """Record a successful spawn for gap pacing."""
        ...

    def spawn_allowed(self, bots: dict, model: str = "") -> tuple[bool, str]:
        """Check the global spawn gate without mutating state."""
        ...

    @property
    def max_concurrent(self) -> int:
        """Maximum concurrent bot subprocesses allowed."""
        ...


class DefaultPromptGateway:
    """Default implementation that lazy-imports codebot.prompt_gateway.

    Each method performs an inside-body import to avoid top-level coupling
    between process_manager and prompt_gateway modules.
    """

    def build_message(
        self,
        bot: str,
        model: str,
        prompt_text: str,
        heartbeat_file: str,
        ckpt_file: str,
        ckpt_block: str,
        state_dir: str,
        logs_dir: str,
        prompt_name: str,
    ) -> str:
        import codebot.prompt_gateway as _pg
        return _pg.build_message(
            bot, model, prompt_text, heartbeat_file, ckpt_file,
            ckpt_block, state_dir, logs_dir, prompt_name,
        )

    def note_spawn(self) -> None:
        import codebot.prompt_gateway as _pg
        _pg.note_spawn()

    def spawn_allowed(self, bots: dict, model: str = "") -> tuple[bool, str]:
        import codebot.prompt_gateway as _pg
        return _pg.spawn_allowed(bots, model)

    @property
    def max_concurrent(self) -> int:
        import codebot.prompt_gateway as _pg
        return _pg.MAX_CONCURRENT


_prompt_gateway_instance: Optional[PromptGatewayProtocol] = None


def get_prompt_gateway() -> PromptGatewayProtocol:
    """Return the current gateway instance, creating default if needed."""
    global _prompt_gateway_instance
    if _prompt_gateway_instance is None:
        _prompt_gateway_instance = DefaultPromptGateway()
    return _prompt_gateway_instance


def set_prompt_gateway(gw: Optional[PromptGatewayProtocol]) -> None:
    """Inject a custom gateway (or None to restore default)."""
    global _prompt_gateway_instance
    _prompt_gateway_instance = gw


def clear_prompt_gateway() -> None:
    """Reset gateway to default (for test teardown)."""
    global _prompt_gateway_instance
    _prompt_gateway_instance = None


# ---------------------------------------------------------------------------
# Process Management
# ---------------------------------------------------------------------------

# Backward-compatible aliases delegating to gateway when available,
# falling back to env vars. Kept for external callers (orchestrator, health_check).
GATEWAY_MAX_CONCURRENT = int(os.getenv("CODEBOT_MAX_CONCURRENT", str(MAX_CONCURRENT_AGENTS)))

# Process count cache with TTL to avoid repeated pgrep calls
_PROCESS_COUNT_TTL: float = 5.0
_cached_process_count: int | None = None
_cached_process_count_at: float = 0.0


def _clear_process_count_cache() -> None:
    """Reset the process count cache (useful for testing)."""
    global _cached_process_count, _cached_process_count_at
    _cached_process_count = None
    _cached_process_count_at = 0.0


def _count_api_runner_processes() -> int:
    """Count running module-invoked api runner processes using pgrep.

    Results are cached for ``_PROCESS_COUNT_TTL`` seconds to avoid spawning
    an external process on every health tick.

    FAIL-CLOSED: On any error (timeout, missing pgrep, OS error) we return
    a large number instead of 0.  Returning 0 would make the spawn gate
    think no processes are running, allowing unlimited spawning which can
    exhaust system resources.  Returning a large number blocks new spawns
    until the next successful check — the safe direction.
    """
    global _cached_process_count, _cached_process_count_at

    now = time.monotonic()
    if (
        _cached_process_count is not None
        and (now - _cached_process_count_at) < _PROCESS_COUNT_TTL
    ):
        return _cached_process_count

    _FAIL_CLOSED = 9999
    try:
        result = subprocess.run(
            ["pgrep", "-f", "codebot.api_runner"],
            capture_output=True, text=True, timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            count = len([line for line in result.stdout.strip().split("\n") if line.strip()])
        else:
            # pgrep returns 1 when no processes match — that's a real 0, not an error
            count = 0
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.warning("_count_api_runner_processes failed (%s) — returning fail-closed count", type(exc).__name__)
        count = _FAIL_CLOSED

    _cached_process_count = count
    _cached_process_count_at = now
    return count


def _v2_active_count() -> int:
    try:
        from codebot.orchestrator_scheduler import count_active
        return count_active()
    except Exception:
        return 0


def _spawn_gate(bots: dict[str, BotState] | None = None, is_queued: bool = False,
                runner_mode: str = "api", bot_model: str = "", bot_name: str = "",
                is_overture: bool = False, is_demand: bool = False) -> tuple[bool, str]:
    v2_active = _v2_active_count()
    cap = GATEWAY_MAX_CONCURRENT
    if is_demand or is_overture:
        cap = GATEWAY_MAX_CONCURRENT + 2
    if v2_active >= cap:
        return False, f"cap {v2_active}/{cap} (v2)"
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
    bot.status = status
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


CHECKPOINT_MAX_BYTES = 4096


def checkpoint_backup_path(path: Path) -> Path:
    if path.suffix == ".json":
        return path.with_name(f"{path.stem}.checkpoint.bak")
    return Path(f"{path}.bak")


def validate_checkpoint_payload(data: object) -> dict | None:
    if not isinstance(data, dict):
        return None
    bot = data.get("bot", "")
    if bot is not None and not isinstance(bot, str):
        return None
    updated_at = data.get("updated_at", 0)
    if updated_at is not None:
        try:
            float(updated_at)
        except (TypeError, ValueError):
            return None
    try:
        if len(json.dumps(data).encode("utf-8")) > CHECKPOINT_MAX_BYTES:
            return None
    except (TypeError, ValueError):
        return None
    return data


def write_platform_checkpoint(bot_name: str, payload: dict) -> Path | None:
    if not isinstance(payload, dict):
        return None
    data = dict(payload)
    data.setdefault("bot", bot_name)
    try:
        data.setdefault("updated_at", __import__("time").time())
    except Exception:
        data.setdefault("updated_at", 0.0)
    validated = validate_checkpoint_payload(data)
    if validated is None:
        return None
    path = checkpoint_path(bot_name)
    try:
        _write_json_atomic(path, validated)
    except OSError:
        return None
    backup = checkpoint_backup_path(path)
    try:
        backup.write_text(json.dumps(validated, indent=2), encoding="utf-8")
    except OSError:
        pass
    return path


def _quarantine_checkpoint(path: Path, reason: str) -> None:
    try:
        quarantine_dir = path.parent / "checkpoint_quarantine"
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        target = quarantine_dir / f"{path.stem}-{int(__import__('time').time())}-{reason}.json"
        path.replace(target)
    except OSError:
        try:
            path.unlink()
        except OSError:
            pass


def _discard_and_read_backup(p: Path, bak: Path) -> dict | None:
    _quarantine_checkpoint(p, "corrupt")
    for candidate in (bak, p.with_suffix(".bak"), Path(f"{p}.bak")):
        try:
            if not candidate.exists():
                continue
            data, _ = _load_json_file(candidate)
            if isinstance(data, dict) and validate_checkpoint_payload(data) is not None:
                return data
        except OSError:
            continue
    return None


def read_checkpoint(bot_name: str) -> dict | None:
    p = checkpoint_path(bot_name)
    bak = checkpoint_backup_path(p)

    if not p.exists():
        if bak.exists():
            data, _ = _load_json_file(bak)
            if isinstance(data, dict) and validate_checkpoint_payload(data) is not None:
                logger.info(f"Restored last-good checkpoint for '{bot_name}' from .bak")
                return data
        return None

    data, raw = _load_json_file(p)
    if data is None and not raw:
        if not p.exists():
            return _discard_and_read_backup(p, bak) if p.exists() else None
    if data is None:
        logger.warning(f"Checkpoint corrupt for '{bot_name}' — falling back to .bak")
        return _discard_and_read_backup(p, bak)

    validated = validate_checkpoint_payload(data)
    if validated is None:
        logger.warning(f"Checkpoint for '{bot_name}' failed validation — falling back to .bak")
        return _discard_and_read_backup(p, bak)

    if len(raw.encode("utf-8")) > CHECKPOINT_MAX_BYTES:
        logger.warning(f"Checkpoint for '{bot_name}' exceeds 4KB — quarantining")
        return _discard_and_read_backup(p, bak)
    try:
        bak.write_text(raw, encoding="utf-8")
    except OSError:
        pass
    return validated


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


def _prepare_prompt_with_context(bot: BotState, extra_block: str = "") -> str:
    """Read prompt file and inject ticket context + scratchpad handoff.

    Build order: choose base (canonical OR tournament variant) → ticket context → scratchpad → return.
    When a tournament is active for this role, the variant prompt replaces the canonical base entirely.
    Context layers are added once on top of whichever base was selected.
    """
    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
    base_role = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
    prompt_text = ""
    using_variant = False

    # Step 1: Choose base prompt — tournament variant or canonical
    try:
        from codebot.rl_tournament import load_state, get_variant_prompt_path, assign_ticket_to_variant
        state_dir = _resolve_state_dir()
        ts = load_state(state_dir, base_role)
        if ts.active and assigned_tid:
            variant = assign_ticket_to_variant(base_role, assigned_tid, state_dir)
            if variant:
                vpath = get_variant_prompt_path(base_role, variant, state_dir)
                if vpath:
                    try:
                        prompt_text = vpath.read_text(encoding="utf-8")
                        using_variant = True
                        logger.info(
                            "Tournament variant %s replaced base prompt for %s (ticket %s)",
                            variant, base_role, assigned_tid,
                        )
                    except OSError:
                        pass
    except Exception:
        pass

    # Fall back to canonical prompt if no variant loaded
    if not prompt_text:
        prompt_path = _resolve_bots_dir() / bot.config.prompt_file
        try:
            with _prompt_read_lock(prompt_path):
                # Use st_mtime_ns (integer nanoseconds) for consistency with config_reloader
                mtime_ns = prompt_path.stat().st_mtime_ns
                bot.prompt_mtime = mtime_ns
                bot.last_prompt_mtime = mtime_ns
                prompt_text = prompt_path.read_text()
        except FileNotFoundError:
            logger.warning(f"Prompt file not found: {prompt_path}")
            bot.prompt_mtime = 0
            bot.last_prompt_mtime = 0
            return ""
        except Exception as e:
            logger.error(f"Failed to read prompt file {prompt_path}: {e}")
            bot.prompt_mtime = 0
            bot.last_prompt_mtime = 0
            return ""

    logger.info(f"start_bot '{bot.config.name}': assigned_tid='{assigned_tid}', prompt={len(prompt_text)} chars, variant={using_variant}")

    # Step 2: Add ticket context
    if assigned_tid:
        ticket_ctx = _load_ticket_context(assigned_tid)
        if ticket_ctx:
            prompt_text = f"{prompt_text}\n\n{ticket_ctx}"
            logger.info(f"Injected ticket context for {bot.config.name}: {assigned_tid} ({len(ticket_ctx)} chars)")
        else:
            logger.warning(f"No ticket context found for {bot.config.name}: {assigned_tid}")

    # Step 3: Add scratchpad handoff
    try:
        from codebot.scratchpad import load_scratchpad, create_handoff_note
        if assigned_tid:
            scratch_state = load_scratchpad(_resolve_state_dir(), assigned_tid)
            if scratch_state.agent_history or scratch_state.completed_steps:
                handoff = create_handoff_note(scratch_state)
                prompt_text = f"{prompt_text}\n\n{handoff}\nResume from where the previous agent left off. Do NOT redo completed work."
    except Exception:
        pass

    # Step 4: Inline implementation packet to eliminate startup read cascade
    if assigned_tid:
        try:
            packet_path = _resolve_state_dir() / "implementation_packets" / f"{assigned_tid}.json"
            if packet_path.exists():
                packet_text = packet_path.read_text(encoding="utf-8")
                prompt_text = f"{prompt_text}\n\n--- IMPLEMENTATION PACKET ---\n{packet_text}\n--- END PACKET ---"
        except Exception:
            pass

    if extra_block:
        prompt_text = f"{prompt_text}\n\n{extra_block}"

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
    """Build the full mission message for the bot subprocess via injected gateway."""
    state_dir = _resolve_state_dir()
    logs_dir = _resolve_logs_dir()
    bots_dir = _resolve_bots_dir()
    prompt_path = bots_dir / bot.config.prompt_file
    gw = get_prompt_gateway()
    return gw.build_message(
        bot.config.name, bot.config.model, prompt_text,
        str(heartbeat_file), str(ckpt_file), ckpt_block,
        str(state_dir), str(logs_dir), prompt_path.name)


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
    _clear_process_count_cache()
    bot.last_heartbeat = time.time()
    bot.last_log_mtime = time.time()
    bot.next_run_at = time.time() + bot.config.interval_seconds
    bot.consecutive_errors = 0
    bot.started_at = state_data["started"]
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
        
        # Write PID file atomically for safe, race-free termination by control_server
        # Atomic pattern: write tmp, chmod 0o600, rename to target. Prevents partial reads.
        try:
            state_dir = _resolve_state_dir()
            pid_file = state_dir / f"{bot.config.name}.pid"
            tmp_pid = pid_file.with_name(f"{pid_file.name}.{os.getpid()}.{threading.get_ident()}.{uuid4().hex}.tmp")
            tmp_pid.write_text(str(process.pid), encoding="utf-8")
            tmp_pid.chmod(0o600)
            tmp_pid.replace(pid_file)
        except OSError as e:
            logger.warning(f"Failed to write atomic PID file for '{bot.config.name}': {e}")
            # Cleanup tmp file if it exists
            try:
                if 'tmp_pid' in locals():
                    tmp_pid.unlink(missing_ok=True)
            except OSError:
                pass
        
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

def _init_and_prepare_bot(bot: BotState, resume_checkpoint: bool, extra_block: str = "") -> tuple[Path, Path, str]:
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

    prompt_text = _prepare_prompt_with_context(bot, extra_block=extra_block)
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

    from codebot.model_manager import next_model_for_role
    base_role = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
    tc_val = ""
    sev_val = ""
    risk_val = ""
    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
    if assigned_tid:
        try:
            from codebot.ticket_dispatcher import get_ticket_store
            ts = get_ticket_store()
            if ts is not None:
                t = ts.get(assigned_tid)
                if t is not None:
                    tc = getattr(t, "ticket_class", None)
                    tc_val = tc.value if hasattr(tc, "value") else str(tc) if tc else ""
                    sv = getattr(t, "severity", None)
                    sev_val = sv.value if hasattr(sv, "value") else str(sv) if sv else ""
                    rk = getattr(t, "risk", None)
                    risk_val = rk.value if hasattr(rk, "value") else str(rk) if rk else ""
        except Exception:
            pass
    assignment = next_model_for_role(base_role, ticket_class=tc_val, severity=sev_val, risk=risk_val)
    old_model = bot.config.model
    bot.config.model = assignment.model
    bot.config.fallback_model = assignment.fallback
    if old_model != assignment.model:
        logger.info(f"Model rotation '{bot.config.name}': {old_model} -> {assignment.model} (fallback {assignment.fallback})")

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


def _is_queued(bot: BotState, cached_states: dict[str, dict | None] | None = None) -> bool:
    """Check if bot is in queued status using in-memory cache only.

    Args:
        bot: BotState instance.
        cached_states: Ignored; retained for backward compatibility.

    Returns:
        True if bot.status == "queued", False otherwise.
    """
    return bot.status == "queued"


def _count_queued_bots(bots: dict[str, BotState], cached_states: dict[str, dict | None] | None = None) -> int:
    """Count how many bots are in queued status.

    Args:
        bots: Dict mapping bot name to BotState.
        cached_states: Optional preloaded .state.json data from batch_read_bot_states().
            If provided, avoids per-bot disk reads entirely.

    Returns:
        Number of bots with status="queued".
    """
    if cached_states is not None:
        return sum(1 for name in bots if cached_states.get(name, {}).get("status") == "queued")

    # Fallback: read each bot's state file (N+1 I/O pattern - avoid in hot paths)
    count = 0
    for bot in bots.values():
        if _is_queued(bot):
            count += 1
    return count


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


def _truncate_field(text: str, max_bytes: int) -> str:
    """Truncate text to max_bytes UTF-8 bytes with '... [truncated]' suffix if exceeded."""
    if not text:
        return text
    suffix = "... [truncated]"
    # Reserve space for suffix
    suffix_bytes = suffix.encode("utf-8")
    max_content_bytes = max(max_bytes - len(suffix_bytes), 0)
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    # Truncate and decode with ignore to handle multi-byte characters at boundary
    truncated = encoded[:max_content_bytes].decode("utf-8", "ignore")
    return truncated + suffix


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
            f"Problem: {_truncate_field(t.problem_statement, 2048)}",
            f"Desired State: {_truncate_field(t.desired_state, 2048)}",
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
                    lines.append(f"  Issue: {_truncate_field(fb['description'], 2048)}")
                if fb.get('recommendation'):
                    lines.append(f"  Fix: {_truncate_field(fb['recommendation'], 2048)}")
            lines.append("=== END REVIEWER FEEDBACK ===")
        origin_type = getattr(t, "origin_type", "") or ""
        origin_id = getattr(t, "origin_id", "") or ""
        if origin_type == "user_request" and origin_id:
            try:
                from codebot.state_manager import get_paths
                state_dir = get_paths().state_dir
                for subdir in ("requests/processed", "requests"):
                    req_path = state_dir / subdir / f"{origin_id}.json"
                    if req_path.exists():
                        req_text = req_path.read_text(encoding="utf-8")
                        lines.append("")
                        lines.append("=== USER REQUEST CONTEXT ===")
                        lines.append(_truncate_field(req_text, 4096))
                        lines.append("=== END USER REQUEST CONTEXT ===")
                        break
            except Exception:
                pass
        lines.append("--- END TICKET CONTEXT ---")
        return _truncate_field("\n".join(lines), 8192)
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
