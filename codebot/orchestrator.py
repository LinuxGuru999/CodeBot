#!/usr/bin/env python3
"""Bot Orchestrator — Pure Controller.

Purpose
-------
Manages bot lifecycle: start, stop, restart, health monitoring.
Detects stuck agents via heartbeat files and auto-restarts them.

The orchestrator does NO bot work itself. It is a pure process manager
that spawns bot subprocesses and monitors their health.

Why
---
Bots are autonomous agents that can freeze, deadlock, or exceed time limits.
The orchestrator provides centralized oversight without coupling to bot logic.
Each bot writes a heartbeat file; the orchestrator checks freshness.

Invariants
----------
- Single process (no multiprocessing)
- Heartbeat files are the ONLY communication channel from bots
- Orchestrator never reads bot output files (bots write directly to docs/)
- Kill signals: SIGTERM first (5s grace), then SIGKILL
- Max restarts per bot per hour: configurable (default 5)
"""

import json
import logging
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Optional

from typing import Protocol, Any

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
            # msvcrt.locking takes fd, mode, length
            # LOCK_EX equivalent is LK_LOCK (0x1) which locks the region
            # We lock the first byte as a proxy for the whole file
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

# Define constants if fcntl is not available
try:
    import fcntl
    LOCK_EX = fcntl.LOCK_EX
    LOCK_UN = fcntl.LOCK_UN
except ImportError:
    LOCK_EX = 2
    LOCK_UN = 8

# ---------------------------------------------------------------------------
# Paths — resolved via ProjectAdapter; fallback to CODEBOT_PROJECT_ROOT env or cwd
# ---------------------------------------------------------------------------

_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))

BOTS_DIR = _project_root
STATE_DIR = _project_root / ".codebot" / "state"
LOGS_DIR = _project_root / ".codebot" / "logs"
BACKUP_DIR = _project_root / ".codebot" / "state" / "backup"
_QUEUE_SNAPSHOT: tuple[Path, float, str] | None = None
_MANIFEST_SNAPSHOT: tuple[Path, tuple[tuple[str, float], ...], dict[str, dict]] | None = None
_CHECKPOINT_SNAPSHOTS: dict[Path, tuple[float, list[str] | int]] = {}
ALIGNMENT_EVENTS_DIR = STATE_DIR / "alignment_events"

# T4.3 incremental adapter seam: when a ProjectAdapter is provided, its paths
# override the defaults above.
_adapter_instance: Any = None
_alignment_service_instance: Any = None

try:
    from codebot.adaptive_rate_limiter import rate_limiter
    _HAS_RATE_LIMITER = True
except ImportError:
    _HAS_RATE_LIMITER = False
    rate_limiter = None


class AlignmentServiceProtocol(Protocol):
    """Interface for alignment service to decouple orchestrator from implementation."""
    def run_alignment_pipeline(self, bot_name: str, timeout: int = ...) -> bool: ...
    def run_alignment_pipeline_for_all(self) -> None: ...


def _lazy_run_alignment_pipeline(bot_name: str, timeout: int = 120) -> bool:
    """Lazy import helper for run_alignment_pipeline to avoid circular deps."""
    try:
        from codebot import alignment_service
        return alignment_service.run_alignment_pipeline(bot_name, timeout)
    except ImportError:
        try:
            import alignment_service  # type: ignore
            return alignment_service.run_alignment_pipeline(bot_name, timeout)
        except ImportError:
            logger.warning("alignment_service not available, skipping alignment pipeline")
            return False


def _lazy_run_alignment_pipeline_for_all() -> None:
    """Lazy import helper for run_alignment_pipeline_for_all to avoid circular deps."""
    try:
        from codebot import alignment_service
        alignment_service.run_alignment_pipeline_for_all()
    except ImportError:
        try:
            import alignment_service  # type: ignore
            alignment_service.run_alignment_pipeline_for_all()
        except ImportError:
            logger.warning("alignment_service not available, skipping alignment sweep")


class DefaultAlignmentService:
    """Minimal wrapper delegating to lazy-import helpers."""
    def run_alignment_pipeline(self, bot_name: str, timeout: int = 120) -> bool:
        return _lazy_run_alignment_pipeline(bot_name, timeout)

    def run_alignment_pipeline_for_all(self) -> None:
        _lazy_run_alignment_pipeline_for_all()


def get_alignment_service() -> AlignmentServiceProtocol:
    """Get the current alignment service instance, creating default if none set."""
    global _alignment_service_instance
    if _alignment_service_instance is None:
        _alignment_service_instance = DefaultAlignmentService()
    return _alignment_service_instance


def set_alignment_service(service: AlignmentServiceProtocol) -> None:
    """Inject a custom alignment service implementation (for testing or overrides)."""
    global _alignment_service_instance
    _alignment_service_instance = service


def set_project_adapter(adapter: Any) -> None:
    global _adapter_instance, BOTS_DIR, STATE_DIR, LOGS_DIR, BACKUP_DIR, ALIGNMENT_EVENTS_DIR, DRAIN_FILE, UPDATE_LOCK, RESTART_FILE
    _adapter_instance = adapter
    try:
        p = adapter.paths()
        BOTS_DIR = p.repository_root
        STATE_DIR = p.state_dir
        LOGS_DIR = p.logs_dir
        BACKUP_DIR = p.state_dir / "backup"
        ALIGNMENT_EVENTS_DIR = p.state_dir / "alignment_events"
        DRAIN_FILE = p.state_dir / ".drain"
        UPDATE_LOCK = p.state_dir / ".update_lock"
        RESTART_FILE = p.state_dir / ".restart"
    except Exception:
        pass


def get_adapter() -> Any:
    return _adapter_instance


# Ensure directories exist
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)
BACKUP_DIR.mkdir(parents=True, exist_ok=True)
ALIGNMENT_EVENTS_DIR.mkdir(parents=True, exist_ok=True)

DRAIN_FILE = STATE_DIR / ".drain"
UPDATE_LOCK = STATE_DIR / ".update_lock"
RESTART_FILE = STATE_DIR / ".restart"

try:
    from codebot.prompt_gateway import (
        build_message as _gateway_build_message,
        note_spawn as _gateway_note_spawn,
        MAX_CONCURRENT as _PG_MAX_CONCURRENT,
        MIN_SPAWN_GAP as GATEWAY_MIN_SPAWN_GAP,
    )
    _GATEWAY = True
except ImportError:
    _GATEWAY = False
    _PG_MAX_CONCURRENT = 10
    GATEWAY_MIN_SPAWN_GAP = int(os.getenv("CODEBOT_MIN_SPAWN_GAP", "25"))
GATEWAY_MAX_CONCURRENT = int(os.getenv("CODEBOT_MAX_CONCURRENT", "26"))

CODEBOT_MIN_MEMORY_MB = int(os.getenv("CODEBOT_MIN_MEMORY_MB", "60"))
USE_MANIFEST_SCHEDULER = os.getenv("CODEBOT_MANIFEST_SCHEDULER", "0") == "1"

IMPLEMENTER_ROLE_NAMES: frozenset[str] = frozenset({
    "general_implementer", "backend_implementer", "frontend_implementer",
    "test_implementer", "migration_implementer", "documentation_implementer",
})

DISCOVERY_ROLE_NAMES: frozenset[str] = frozenset({
    "bug_hunter", "security_auditor", "architecture_auditor", "performance_auditor",
    "test_gap_auditor", "documentation_auditor", "dependency_auditor", "ux_auditor",
    "feature_hunter",
})

REVIEWER_ROLE_NAMES: frozenset[str] = frozenset({
    "correctness_reviewer", "security_reviewer", "architecture_reviewer",
    "test_reviewer", "performance_reviewer", "simplicity_reviewer",
    "documentation_reviewer",
})
PLANNING_ROLE_NAMES: frozenset[str] = frozenset({
    "implementation_planner",
})

MAX_DISCOVERY_NO_TICKET_RUNS = 10
RATE_LIMIT_REQUEUE_S = int(os.getenv("CODEBOT_RATE_LIMIT_REQUEUE_S", "300"))
RATE_LIMIT_BACKOFF_MAX = int(os.getenv("CODEBOT_RATE_LIMIT_BACKOFF_MAX", "3600"))
RATE_LIMIT_DISABLE_AFTER = int(os.getenv("CODEBOT_RATE_LIMIT_DISABLE_AFTER", "20"))
_metrics_tick = 0

ALWAYS_RESPAWN = frozenset({
    "scheduler", "conflict_resolver",
})

TICKET_CLASS_TO_IMPLEMENTER: dict[str, str] = {
    "bug": "general_implementer",
    "feature": "general_implementer",
    "refactor": "general_implementer",
    "security": "backend_implementer",
    "performance": "backend_implementer",
    "architecture": "backend_implementer",
    "test": "test_implementer",
    "documentation": "documentation_implementer",
    "dependency": "migration_implementer",
    "infrastructure": "migration_implementer",
}

TICKET_CLASS_TO_REVIEWER: dict[str, str] = {
    "bug": "correctness_reviewer",
    "feature": "correctness_reviewer",
    "refactor": "simplicity_reviewer",
    "security": "security_reviewer",
    "performance": "performance_reviewer",
    "architecture": "architecture_reviewer",
    "test": "test_reviewer",
    "documentation": "documentation_reviewer",
    "dependency": "correctness_reviewer",
    "infrastructure": "correctness_reviewer",
}


def _get_available_memory_mb() -> float:
    """Return available memory in MB from /proc/meminfo, or 0.0 if unavailable."""
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            for line in f:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        try:
                            return float(parts[1]) / 1024.0
                        except ValueError:
                            return 0.0
                    break
    except (FileNotFoundError, OSError):
        return 0.0
    except Exception:
        return 0.0
    return 0.0


def is_draining() -> bool:
    return DRAIN_FILE.exists()


def _check_self_restart(bots: dict[str, BotState]) -> bool:
    if not RESTART_FILE.exists():
        return False
    try:
        reason = RESTART_FILE.read_text(encoding="utf-8").strip() or "manual"
    except OSError:
        reason = "manual"
    logger.info(f"Self-restart signal detected (reason: {reason}) — draining and restarting")
    for bot in bots.values():
        if bot.process is not None and bot.process.poll() is None:
            stop_bot(bot, "self-restart")
    try:
        RESTART_FILE.unlink(missing_ok=True)
    except OSError:
        pass
    python = sys.executable
    args = [python] + sys.argv
    logger.info(f"Executing self-restart: {' '.join(args)}")
    os.execv(python, args)
    return True

def _check_prompt_changes(bots: dict[str, BotState]) -> None:
    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        prompt_path = BOTS_DIR / bot.config.prompt_file
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
                stop_bot(bot, "prompt-hot-reload")
                bot.next_run_at = time.time()
        bot.last_prompt_mtime = current_mtime


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


_GLOBAL_CODE_MTIMES: dict[str, float] = {}


def _check_code_changes(bots: dict[str, BotState]) -> None:
    """Check for code changes in O(M+B) time.

    Uses a global mtime cache to avoid O(M*B) nested loops. Since all bots
    are restarted together when any module changes, per-bot granularity is
    unnecessary for detection. Each bot's last_code_mtimes is still updated
    to maintain compatibility with other subsystems.
    """
    global _GLOBAL_CODE_MTIMES
    current_mtimes = _get_code_mtimes()
    if not current_mtimes:
        return

    # O(M): detect changed modules against global baseline
    changed_modules: list[str] = []
    for mod_name, mtime in current_mtimes.items():
        prev = _GLOBAL_CODE_MTIMES.get(mod_name, 0.0)
        if prev > 0 and mtime > prev:
            changed_modules.append(mod_name)

    # O(B): update all bots' mtimes (always needed for convergence)
    for bot in bots.values():
        if not bot.last_code_mtimes:
            bot.last_code_mtimes = dict(current_mtimes)
        else:
            bot.last_code_mtimes.update(current_mtimes)

    if not changed_modules:
        # Update global baseline even if no changes detected
        _GLOBAL_CODE_MTIMES.update(current_mtimes)
        return

    unique_changed = sorted(set(changed_modules))
    logger.info(f"Code change detected in {unique_changed} — respawning active bots")

    # O(B): restart active bots
    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        alive = bot.process is not None and bot.process.poll() is None
        if alive:
            stop_bot(bot, f"code-hot-reload:{','.join(unique_changed)}")
            bot.next_run_at = time.time()

    # Update global baseline after handling changes
    _GLOBAL_CODE_MTIMES = dict(current_mtimes)


def _check_config_changes(bots: dict[str, BotState]) -> None:
    if _adapter_instance is None:
        return
    try:
        new_entries = _adapter_instance.bot_registry()
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
        if base in DECOMPOSER_ROLE_NAMES or base in PLANNING_ROLE_NAMES:
            new_interval = 30
        else:
            new_interval = entry.get("interval", bot.config.interval_seconds)
        new_model = entry.get("model", bot.config.model)
        new_tier = entry.get("tier", bot.config.tier)
        if new_interval != bot.config.interval_seconds or new_model != bot.config.model or new_tier != bot.config.tier:
            logger.info(f"Config changed for '{name}': interval={bot.config.interval_seconds}->{new_interval} model={bot.config.model}->{new_model}")
            bot.config.interval_seconds = new_interval
            bot.config.heartbeat_timeout = new_interval * 2
            bot.config.model = new_model
            bot.config.fallback_model = entry.get("fallback_model", bot.config.fallback_model)
            bot.config.tier = new_tier
            changed = True
    if changed:
        _rescale_registry()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOGS_DIR / "orchestrator.log"),
    ],
)
logger = logging.getLogger("orchestrator")

# ---------------------------------------------------------------------------
# Bot Registry
# ---------------------------------------------------------------------------

@dataclass
class BotConfig:
    """Configuration for a single bot."""
    name: str
    prompt_file: str          # Name of .md file in BOTS_DIR
    interval_seconds: int     # Seconds between runs
    heartbeat_timeout: int    # Seconds before considered stuck (2x interval)
    model: str = "xiaomi-mimo-2.5"  # Model from opencode.jsonc
    fallback_model: str = ""      # Fallback when primary model fails; empty = no fallback
    enabled: bool = True
    max_restarts: int = 5     # Max restarts per hour
    clean_exit_wait: bool = False  # True: after exit 0, wait interval_seconds before respawn
    tier: int = 2             # Symphony tier: 1=core loop, 2=quality gates, 3=infrequent
    runner_mode: str = "api"  # "api" = api_runner.run_bot subprocess
    max_tokens_per_run: int = 0  # 0 = unlimited; per-run token cap enforced by api_runner
    fallback_models: tuple[str, ...] = ()  # Ordered fallback chain beyond single fallback_model


# Worker pool reservation: six worker slots; all remaining global capacity
# rotates across scanners, auditors, and gates.
# Workers bypass the shared conductor cap so implementation throughput never
# starves; scanners/auditors/gates share the 2 remaining slots by tier priority.

MODEL_TIER_CHEAP = frozenset({"xiaomi-mimo-2.5"})
MODEL_TIER_EXPENSIVE = frozenset({"qwen-3.8-max", "qwen-3.8-max-thinking", "qwen-3.7-max", "qwen-3.7-max-thinking"})
_COMPLEXITY_RANK = {"trivial": 0, "small": 1, "medium": 2, "high": 3, "critical": 4}


def _model_tier_for_complexity(model: str, complexity: str, queue_has_tier_work: bool = True) -> bool:
    if model in MODEL_TIER_CHEAP:
        return _COMPLEXITY_RANK.get(complexity, 2) <= 2
    if model in MODEL_TIER_EXPENSIVE:
        if _COMPLEXITY_RANK.get(complexity, 2) >= 2:
            return True
        return not queue_has_tier_work
    return True


def _build_worker_pool() -> frozenset[str]:
    return frozenset(c.name for c in BOT_REGISTRY if c.name in IMPLEMENTER_ROLE_NAMES or any(c.name.startswith(f"{r}-") for r in IMPLEMENTER_ROLE_NAMES))


MIN_ROTATING_SLOTS = 8


def worker_reserved_slots(max_concurrent: int) -> int:
    return len(WORKER_POOL)


def rotating_slots(max_concurrent: int) -> int:
    return max(MIN_ROTATING_SLOTS, max_concurrent - worker_reserved_slots(max_concurrent))


# Tier 1 (core loop: find → triage → implement → verify → sync) always
# plays first; tier 3 (dependency/release/alignment) yields to tier 1.
# Sub-tiers (1.1-1.3) break ties within tier 1 to balance model contention.
# With 10 slots: 6 tier-1, 3 tier-2, 1 tier-3 max.
TIER_PRIORITY: dict[str, int] = {
    # Tier 1.1 — critical path (must never starve)
    "issues": 11, "bug_triage": 11, "build": 11, "github_bot": 11, "decomposer": 11,
    # Tier 1.2 — secondary core (rotate by queue depth)
    "gitsync": 12,
    "worker-1": 12, "worker-2": 12, "worker-3": 12, "worker-4": 12,
    "worker-7": 12, "worker-8": 12,
    # Tier 1.3 — complex/heavy (run when slots available)
    "worker-5": 13, "worker-6": 13, "worker-9": 13, "worker-10": 13, "worker-11": 13, "worker-12": 13,
    # Disabled bots (kept in registry but never scheduled)
    "github_issues": 99,
    # Tier 2 — quality gates (yield to tier 1)
    "features": 21, "ui_improve": 21, "e2e_smoke": 21,
    "test_coverage": 22, "code_quality": 22, "security_auditor": 22, "goal_steering": 22,
    # Tier 3 — infrequent (lowest priority)
    "doc_sync": 31, "dependency": 31,
    "prompt_opt": 99, "release": 99,  # prompt_opt: lowest tier priority but runs on schedule; release: disabled (enabled=False)
    "alignment": 31,
}


@dataclass
class ModelProfile:
    """Lockup behaviour profile for a model provider.

    Some models stall silently during long reasoning or large tool batches
    while others fail fast. Profiles tune detection sensitivity per model.
    """

    lockup_risk: str          # high | medium | low
    heartbeat_multiplier: float  # effective_timeout = interval * multiplier
    log_stall_seconds: int    # seconds log must be silent before suspicious
    restart_cooldown: int     # seconds to wait before respawn after stuck
    description: str


# Per-model lockup profiles — dialagram-local-router/* (12 models).
# Thinking variants highest risk (long opaque reasoning, no heartbeat);
# non-thinking plus/max medium; spark/xiaomi lowest.
MODEL_PROFILES: dict[str, ModelProfile] = {
    # --- Qwen thinking family (high risk) ---
    "qwen-3.8-max-thinking": ModelProfile(
        lockup_risk="high",
        heartbeat_multiplier=2.8,
        log_stall_seconds=280,
        restart_cooldown=12,
        description="Newest deep reasoning; longest silent thinking, prone to freeze on large file batches",
    ),
    "qwen-3.7-max-thinking": ModelProfile(
        lockup_risk="high",
        heartbeat_multiplier=2.5,
        log_stall_seconds=240,
        restart_cooldown=10,
        description="Deep reasoning; silent thinking ~90s normal, prone to freeze on large file batches",
    ),
    "qwen-3.6-plus-thinking": ModelProfile(
        lockup_risk="medium-high",
        heartbeat_multiplier=2.3,
        log_stall_seconds=210,
        restart_cooldown=9,
        description="Plus thinking; moderate-deep reasoning, stalls on broad scans",
    ),
    "qwen-3.5-plus-thinking": ModelProfile(
        lockup_risk="medium-high",
        heartbeat_multiplier=2.2,
        log_stall_seconds=200,
        restart_cooldown=8,
        description="Older thinking; slower, can stall on validation/reasoning loops",
    ),
    # --- Qwen non-thinking family (medium) ---
    "qwen-3.8-max": ModelProfile(
        lockup_risk="medium-high",
        heartbeat_multiplier=2.0,
        log_stall_seconds=180,
        restart_cooldown=7,
        description="Strong code gen; can hang on large edits / multi-file rewrites",
    ),
    "qwen-3.7-max": ModelProfile(
        lockup_risk="medium",
        heartbeat_multiplier=1.9,
        log_stall_seconds=175,
        restart_cooldown=30,
        description="Strong code gen; balanced performance, occasional stall on complex reasoning",
    ),
    "qwen-3.7-plus": ModelProfile(
        lockup_risk="medium",
        heartbeat_multiplier=1.9,
        log_stall_seconds=170,
        restart_cooldown=6,
        description="Balanced plus; steady, occasional stall on feature reasoning",
    ),
    "qwen-3.6-plus": ModelProfile(
        lockup_risk="medium",
        heartbeat_multiplier=1.9,
        log_stall_seconds=165,
        restart_cooldown=6,
        description="Balanced plus; steady on integration checks, rarely stalls",
    ),
    "qwen-3.5-plus": ModelProfile(
        lockup_risk="low-medium",
        heartbeat_multiplier=1.7,
        log_stall_seconds=140,
        restart_cooldown=4,
        description="Older plus; fast, lightweight, fails quickly",
    ),
    "qwen-3.5-omni-plus": ModelProfile(
        lockup_risk="low-medium",
        heartbeat_multiplier=1.7,
        log_stall_seconds=145,
        restart_cooldown=4,
        description="Omni; holistic cross-check, generally fast, broad context",
    ),
    # --- Creative / fast family (low) ---
    "meta-muse-spark-1.3": ModelProfile(
        lockup_risk="medium",
        heartbeat_multiplier=1.8,
        log_stall_seconds=150,
        restart_cooldown=5,
        description="Creative; steady but can stall on broad codebase scans",
    ),
    "meta-muse-spark-1.2": ModelProfile(
        lockup_risk="low-medium",
        heartbeat_multiplier=1.8,
        log_stall_seconds=150,
        restart_cooldown=5,
        description="Writing-heavy; generally fast, occasionally slow on doc diffs",
    ),
    "xiaomi-mimo-2.5": ModelProfile(
        lockup_risk="low",
        heartbeat_multiplier=1.5,
        log_stall_seconds=120,
        restart_cooldown=3,
        description="Balanced/fast; fails quickly if it fails — shortest patience",
    ),
}


def model_profile(model: str) -> ModelProfile | None:
    """Return profile for model, or None if unknown."""
    return MODEL_PROFILES.get(model)


def effective_heartbeat_timeout(bot: "BotState") -> int:
    """Effective heartbeat timeout after applying model profile.

    Uses max(profile-derived, explicit BotConfig value) so a profile can
    only raise patience, never silently reduce it below the declared 2x
    baseline. Thinking models get extra headroom; fast models keep the
    baseline.
    """
    prof = model_profile(bot.config.model)
    if prof:
        derived = int(bot.config.interval_seconds * prof.heartbeat_multiplier)
        return max(derived, bot.config.heartbeat_timeout)
    return bot.config.heartbeat_timeout


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


def _load_bot_registry() -> list["BotConfig"]:
    """Load bot registry from ProjectAdapter if available, else use minimal defaults.

    SELF-01: When codebot_adapter is loaded, this returns all 26 roles from
    role_registry.py mapped to BotConfig entries. Without an adapter, falls
    back to a minimal 3-bot set for backward compatibility.
    """
    if _adapter_instance is not None:
        try:
            entries = _adapter_instance.bot_registry()
            configs = []
            for e in entries:
                configs.append(BotConfig(
                    name=e["name"],
                    prompt_file=e.get("prompt", f"{e['name']}.md"),
                    interval_seconds=e.get("interval", 600),
                    heartbeat_timeout=e.get("interval", 600) * 2,
                    model=e.get("model", "default"),
                    fallback_model=e.get("fallback_model", ""),
                    enabled=e.get("enabled", True),
                    max_restarts=e.get("max_restarts", 5),
                    clean_exit_wait=e.get("clean_exit_wait", True),
                    tier=e.get("tier", 2),
                    runner_mode=e.get("runner_mode", "api"),
                    max_tokens_per_run=e.get("max_tokens_per_run", 0),
                ))
            if configs:
                logger.info("Loaded %d roles from project adapter", len(configs))
                return configs
        except Exception as e:
            logger.warning("Failed to load registry from adapter: %s", e)
    return [
        BotConfig("discovery", "bug_hunter.md", 1800, 3600, "default", clean_exit_wait=True),
        BotConfig("implementer", "general_implementer.md", 300, 750, "default", clean_exit_wait=True),
        BotConfig("reviewer", "correctness_reviewer.md", 600, 1500, "default", clean_exit_wait=True),
    ]


BOT_REGISTRY = _load_bot_registry()

WORKER_MODEL_CYCLE = (
    "xiaomi-mimo-2.5", "xiaomi-mimo-2.5", "qwen-3.7-plus", "qwen-3.7-plus",
    "qwen-3.8-max", "qwen-3.8-max", "qwen-3.6-plus", "qwen-3.5-plus",
    "qwen-3.8-max-thinking", "qwen-3.7-max-thinking",
    "meta-muse-spark-1.3", "meta-muse-spark-1.2",
    "qwen-3.7-max", "qwen-3.6-plus-thinking", "qwen-3.5-plus-thinking",
)

WORKER_FALLBACK_CYCLE = (
    "qwen-3.5-plus", "qwen-3.5-plus", "xiaomi-mimo-2.5", "xiaomi-mimo-2.5",
    "qwen-3.7-plus", "qwen-3.7-plus", "xiaomi-mimo-2.5", "xiaomi-mimo-2.5",
    "qwen-3.7-max-thinking", "qwen-3.7-plus",
    "qwen-3.6-plus", "qwen-3.5-plus",
    "qwen-3.6-plus", "qwen-3.7-max-thinking", "qwen-3.7-max-thinking",
)


def _count_actionable_queue_items() -> int:
    try:
        from codebot.ticket_engine import TicketStore, TicketState
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if store_path.exists():
            ts = TicketStore(store_path)
            return (
                len(ts.list_ready())
                + len(ts.list_by_state(TicketState.DECOMPOSE))
                + len(ts.list_by_state(TicketState.REWORK))
            )
    except Exception:
        pass
    queue_path = BOTS_DIR / "docs" / "triage" / "QUEUE.md"
    try:
        content = queue_path.read_text(encoding="utf-8")
    except OSError:
        return 0
    count = 0
    for line in content.splitlines():
        if not line.startswith("|") or line.startswith("|---") or line.startswith("| ID "):
            continue
        parts = [p.strip().lower() for p in line.split("|")[1:-1]]
        if any(s in parts for s in ("confirmed", "approved")):
            count += 1
    return count


def _peek_ticket_classes() -> list[str]:
    try:
        from codebot.ticket_engine import TicketStore
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if store_path.exists():
            ts = TicketStore(store_path)
            ready = ts.list_ready()
            classes = []
            for t in ready:
                tc = getattr(t, 'ticket_class', None)
                classes.append(tc.value if hasattr(tc, 'value') else str(tc) if tc else "feature")
            return classes
    except Exception:
        pass
    return []

def _scale_workers_to_demand(registry: list[BotConfig], max_concurrent: int) -> list[BotConfig]:
    demand = _count_actionable_queue_items()

    def _is_planning_role(name: str) -> bool:
        base = name.split("-")[0] if "-" in name else name
        return base in ("decomposer", "implementation_planner")

    def _read_depths() -> dict[str, int]:
        try:
            from codebot.ticket_engine import TicketStore, TicketState
            store_path = STATE_DIR / "tickets.json"
            if not store_path.exists():
                store_path = Path(".codebot/state/tickets.json")
            if store_path.exists():
                ts = TicketStore(store_path)
                return ts.summary()
        except Exception:
            pass
        return {}

    depths = _read_depths()
    always_on_names = {"scheduler", "conflict_resolver"}

    def _role_has_demand(name: str) -> bool:
        base = name.split("-")[0] if "-" in name else name
        if base in always_on_names:
            return True
        if base in ("decomposer", "implementation_planner"):
            return False
        if base in IMPLEMENTER_ROLE_NAMES:
            return False
        if base in REVIEWER_ROLE_NAMES or base == "ux_reviewer":
            return depths.get("REVIEWING", 0) > 0
        if base == "quality_gate":
            return depths.get("VERIFYING", 0) > 0
        if base == "ticket_triager":
            return (depths.get("DISCOVERED", 0) + depths.get("VALIDATING", 0) + depths.get("TRIAGED", 0)) > 0
        if base in DISCOVERY_ROLE_NAMES:
            return depths.get("DISCOVERED", 0) > 0
        return False

    non_impl = [c for c in registry if c.name not in IMPLEMENTER_ROLE_NAMES and not _is_planning_role(c.name) and _role_has_demand(c.name)]
    base_impl = [c for c in registry if c.name in IMPLEMENTER_ROLE_NAMES]
    base_planning = [c for c in registry if _is_planning_role(c.name)]
    if not base_impl and not base_planning:
        return registry

    seen_planning_bases: set[str] = set()
    unique_planning: list[Any] = []
    for cfg in base_planning:
        base = cfg.name.split("-")[0] if "-" in cfg.name else cfg.name
        if base not in seen_planning_bases:
            seen_planning_bases.add(base)
            unique_planning.append(cfg)

    decomp_queue = depths.get("DECOMPOSE", 0)
    plan_queue = depths.get("PLANNING", 0)
    impl_queue = depths.get("IMPLEMENTING", 0) + depths.get("REWORK", 0)
    planning_instances = 0
    if decomp_queue > 0:
        planning_instances += min(max(1, decomp_queue // 3), 6)
    if plan_queue > 0:
        planning_instances += min(max(1, plan_queue // 2), 4)
    planning_instances = min(planning_instances, max(0, max_concurrent - len(non_impl) - 1))

    impl_budget = max_concurrent - len(non_impl) - planning_instances
    impl_demand = impl_queue
    target = min(impl_demand, max(impl_budget, 0))
    if impl_demand > 0:
        target = max(target, min(len(base_impl), max(impl_budget, 0)))
    role_map = {c.name: c for c in base_impl}
    ticket_classes = _peek_ticket_classes()
    out = list(non_impl)
    name_counts: dict[str, int] = {}
    for i in range(target):
        tc_val = ticket_classes[i] if i < len(ticket_classes) else "feature"
        base_name = TICKET_CLASS_TO_IMPLEMENTER.get(tc_val, "general_implementer")
        role = role_map.get(base_name, base_impl[0] if base_impl else None)
        if role is None:
            continue
        count = name_counts.get(role.name, 0)
        name_counts[role.name] = count + 1
        name = role.name if count == 0 else f"{role.name}-{count+1}"
        idx = i % len(WORKER_MODEL_CYCLE)
        model = WORKER_MODEL_CYCLE[idx]
        fb = WORKER_FALLBACK_CYCLE[idx] if idx < len(WORKER_FALLBACK_CYCLE) else _MODEL_FALLBACKS.get(model, "xiaomi-mimo-2.5")
        tier = 13 if model in MODEL_TIER_EXPENSIVE or "thinking" in model else 12
        out.append(BotConfig(
            name, role.prompt_file, role.interval_seconds, role.heartbeat_timeout,
            model, fallback_model=fb,
            clean_exit_wait=False, runner_mode="api", tier=tier,
            max_restarts=role.max_restarts,
        ))
        TIER_PRIORITY[name] = tier

    decomp_queue = depths.get("DECOMPOSE", 0)
    plan_queue = depths.get("PLANNING", 0)
    planning_name_counts: dict[str, int] = {}

    FAST_MODELS = ("xiaomi-mimo-2.5", "qwen-3.5-plus", "qwen-3.6-plus")
    planning_model_idx = 0
    fast_model_idx = 0
    for role_cfg in unique_planning:
        if role_cfg.name == "decomposer":
            needed = min(max(1, decomp_queue // 3), 6) if decomp_queue > 0 else 1
        else:
            needed = min(max(1, plan_queue // 2), 4) if plan_queue > 0 else 1
        budget_remaining = max_concurrent - len(out)
        needed = min(needed, max(budget_remaining, 0))
        for i in range(max(0, needed)):
            count = planning_name_counts.get(role_cfg.name, 0)
            planning_name_counts[role_cfg.name] = count + 1
            name = role_cfg.name if count == 0 else f"{role_cfg.name}-{count+1}"
            if role_cfg.name == "decomposer":
                model = FAST_MODELS[fast_model_idx % len(FAST_MODELS)]
                fb = _MODEL_FALLBACKS.get(model, "xiaomi-mimo-2.5")
                fast_model_idx += 1
            else:
                idx = planning_model_idx % len(WORKER_MODEL_CYCLE)
                model = WORKER_MODEL_CYCLE[idx]
                fb = WORKER_FALLBACK_CYCLE[idx] if idx < len(WORKER_FALLBACK_CYCLE) else _MODEL_FALLBACKS.get(model, "xiaomi-mimo-2.5")
                planning_model_idx += 1
            out.append(BotConfig(
                name, role_cfg.prompt_file, 30, 90,
                model, fallback_model=fb,
                clean_exit_wait=False, runner_mode="api", tier=11,
                max_restarts=role_cfg.max_restarts,
            ))
            TIER_PRIORITY[name] = 11

    review_queue = depths.get("REVIEWING", 0)
    verify_queue = depths.get("VERIFYING", 0)
    review_names = ["correctness_reviewer", "security_reviewer", "architecture_reviewer",
                    "test_reviewer", "performance_reviewer", "simplicity_reviewer",
                    "documentation_reviewer", "ux_reviewer"]
    impl_queue = depths.get("IMPLEMENTING", 0) + depths.get("REWORK", 0)
    needs_reviewers = review_queue > 0 or impl_queue > 0
    review_model_idx = 0
    for rname in review_names:
        if needs_reviewers and len(out) < max_concurrent:
            role_cfg = next((c for c in registry if c.name == rname), None)
            if role_cfg:
                idx = review_model_idx % len(WORKER_MODEL_CYCLE)
                model = WORKER_MODEL_CYCLE[idx]
                fb = WORKER_FALLBACK_CYCLE[idx] if idx < len(WORKER_FALLBACK_CYCLE) else _MODEL_FALLBACKS.get(model, "xiaomi-mimo-2.5")
                review_model_idx += 1
                out.append(BotConfig(
                    rname, role_cfg.prompt_file, 30, 90,
                    model, fallback_model=fb,
                    clean_exit_wait=False, runner_mode="api", tier=12,
                    max_restarts=role_cfg.max_restarts,
                ))
                TIER_PRIORITY[rname] = 12

    needs_verifier = verify_queue > 0 or review_queue > 0
    if needs_verifier and len(out) < max_concurrent:
        qg_cfg = next((c for c in registry if c.name == "quality_gate"), None)
        if qg_cfg:
            idx = review_model_idx % len(WORKER_MODEL_CYCLE)
            model = WORKER_MODEL_CYCLE[idx]
            fb = WORKER_FALLBACK_CYCLE[idx] if idx < len(WORKER_FALLBACK_CYCLE) else _MODEL_FALLBACKS.get(model, "xiaomi-mimo-2.5")
            out.append(BotConfig(
                "quality_gate", qg_cfg.prompt_file, 30, 90,
                model, fallback_model=fb,
                clean_exit_wait=False, runner_mode="api", tier=12,
                max_restarts=qg_cfg.max_restarts,
            ))
            TIER_PRIORITY["quality_gate"] = 12

    return out


WORKER_MODELS_NON_THINKING = (
    "xiaomi-mimo-2.5", "qwen-3.5-plus", "qwen-3.6-plus", "qwen-3.7-plus",
    "qwen-3.7-max", "qwen-3.8-max", "meta-muse-spark-1.2", "meta-muse-spark-1.3",
    "qwen-3.5-omni-plus",
)

WORKER_MODELS_THINKING = (
    "qwen-3.5-plus-thinking", "qwen-3.6-plus-thinking",
    "qwen-3.7-max-thinking", "qwen-3.8-max-thinking",
)

_MODEL_FALLBACKS: dict[str, str] = {
    "xiaomi-mimo-2.5": "qwen-3.5-plus",
    "qwen-3.5-plus": "xiaomi-mimo-2.5",
    "qwen-3.6-plus": "xiaomi-mimo-2.5",
    "qwen-3.7-plus": "qwen-3.6-plus",
    "qwen-3.7-max": "qwen-3.8-max",
    "qwen-3.8-max": "qwen-3.7-plus",
    "meta-muse-spark-1.2": "qwen-3.5-plus",
    "meta-muse-spark-1.3": "qwen-3.6-plus",
    "qwen-3.5-plus-thinking": "qwen-3.7-max-thinking",
    "qwen-3.6-plus-thinking": "qwen-3.7-max-thinking",
    "qwen-3.7-max-thinking": "qwen-3.8-max-thinking",
    "qwen-3.8-max-thinking": "qwen-3.7-max-thinking",
    "qwen-3.5-omni-plus": "xiaomi-mimo-2.5",
}

_model_rotation_index = 0
_model_allocation_counts: dict[str, int] = {}


def _next_worker_model(bots: dict[str, BotState] | None = None) -> tuple[str, str]:
    global _model_rotation_index
    all_models = WORKER_MODELS_THINKING + WORKER_MODELS_NON_THINKING

    if bots is not None:
        live_counts: dict[str, int] = {m: 0 for m in all_models}
        for b in bots.values():
            if b.process is not None and b.process.poll() is None:
                m = b.config.model
                live_counts[m] = live_counts.get(m, 0) + 1
        min_count = min(live_counts.get(m, 0) for m in all_models)
        candidates = [m for m in all_models if live_counts.get(m, 0) == min_count]
        model = candidates[_model_rotation_index % len(candidates)]
        _model_rotation_index += 1
        for m in all_models:
            _model_allocation_counts[m] = live_counts.get(m, 0)
    else:
        model = all_models[_model_rotation_index % len(all_models)]
        _model_rotation_index += 1

    fallback = _MODEL_FALLBACKS.get(model, "xiaomi-mimo-2.5")
    return model, fallback


def _rotate_model_on_error(bot: BotState, bots: dict[str, BotState] | None = None) -> str:
    """Pick a different model for a bot that just errored out.

    Cycles through all available models, skipping the one that just failed.
    Tracks consecutive failures per model to avoid retrying a broken provider.
    Considers rate limiter state to avoid rotating to heavily rate-limited models.
    """
    all_models = WORKER_MODELS_THINKING + WORKER_MODELS_NON_THINKING
    failed_model = bot.config.model

    if not hasattr(bot, '_model_failures'):
        bot._model_failures = {}  # type: ignore[attr-defined]
    failures = bot._model_failures  # type: ignore[attr-defined]
    failures[failed_model] = failures.get(failed_model, 0) + 1

    live_counts: dict[str, int] = {}
    if bots is not None:
        for b in bots.values():
            if b.process is not None and b.process.poll() is None:
                m = b.config.model
                live_counts[m] = live_counts.get(m, 0) + 1

    rate_limiter_available = False
    rate_limiter_state = {}
    try:
        from codebot.adaptive_rate_limiter import rate_limiter
        rate_limiter_available = True
        for m in all_models:
            can_spawn, _ = rate_limiter.can_spawn_now(m)
            state = rate_limiter._get_state(m)
            rate_limiter_state[m] = {
                "can_spawn": can_spawn,
                "effective_interval": state.effective_interval,
                "consecutive_rate_limits": state.consecutive_rate_limits,
            }
    except Exception:
        pass

    candidates = []
    for m in all_models:
        if m == failed_model:
            continue
        if failures.get(m, 0) >= 3:
            continue
        live = live_counts.get(m, 0)
        if rate_limiter_available and m in rate_limiter_state:
            rl = rate_limiter_state[m]
            if rl["consecutive_rate_limits"] >= 2:
                continue
            penalty = rl["effective_interval"] * 2
            candidates.append((live + penalty, m))
        else:
            candidates.append((live, m))

    if not candidates:
        candidates = [(live_counts.get(m, 0), m) for m in all_models if m != failed_model]

    if not candidates:
        return failed_model

    candidates.sort(key=lambda x: x[0])
    new_model = candidates[0][1]

    old_model = bot.config.model
    bot.config.model = new_model
    bot.config.fallback_model = _MODEL_FALLBACKS.get(new_model, "xiaomi-mimo-2.5")
    logger.info(f"Model rotation for '{bot.config.name}': {old_model} -> {new_model} (error on {old_model}, failures={failures})")
    return new_model


BOT_REGISTRY = _scale_workers_to_demand(BOT_REGISTRY, GATEWAY_MAX_CONCURRENT)
WORKER_POOL = _build_worker_pool()


def _rescale_registry() -> None:
    global BOT_REGISTRY, WORKER_POOL
    import codebot.orchestrator as _self_mod
    scaled = _scale_workers_to_demand(list(_self_mod.BOT_REGISTRY), GATEWAY_MAX_CONCURRENT)
    _self_mod.BOT_REGISTRY = scaled
    BOT_REGISTRY = scaled
    WORKER_POOL = _build_worker_pool()

# ---------------------------------------------------------------------------
# Heartbeat Protocol
# ---------------------------------------------------------------------------

def heartbeat_path(bot_name: str) -> Path:
    """Path to bot's heartbeat file."""
    return STATE_DIR / f"{bot_name}.heartbeat"

def write_heartbeat(bot_name: str) -> None:
    """Write current timestamp as heartbeat. Called by orchestrator on spawn."""
    hb = heartbeat_path(bot_name)
    hb.write_text(str(time.time()))

def read_heartbeat(bot_name: str) -> float:
    """Read bot's last heartbeat timestamp. Returns 0 if missing/stale/corrupt."""
    hb = heartbeat_path(bot_name)
    if not hb.exists():
        return 0.0
    txt = hb.read_text().strip()
    try:
        return float(txt)
    except (ValueError, OSError):
        pass
    try:
        import datetime
        token = txt.split()[0]
        token = token.replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(token)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        ts = dt.timestamp()
        now = time.time()
        if ts > now + 60 or ts < now - 86400:
            return 0.0
        return ts
    except Exception:
        return 0.0

def log_path(bot_name: str) -> Path:
    return LOGS_DIR / f"{bot_name}.log"


def log_mtime(bot_name: str) -> float:
    lp = log_path(bot_name)
    try:
        return lp.stat().st_mtime
    except OSError:
        return 0.0


CLAIM_TTL_SECONDS = 1800
SWEEP_INTERVAL = 300  # Seconds between full orphan claim sweeps to reduce I/O
_last_sweep_time: float = 0.0


def _reap_expired_claims(bot_name: str) -> int:
    """Delete my own expired claim files so dead owners never wedge tasks."""
    reaped = 0
    try:
        claims_dir = STATE_DIR / "claims"
        if not claims_dir.exists():
            return 0
        now = time.time()
        for p in claims_dir.glob(f"*.{bot_name}.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                at = float(data.get("at", 0))
                if now - at > CLAIM_TTL_SECONDS:
                    p.unlink()
                    reaped += 1
            except Exception:
                try:
                    if now - p.stat().st_mtime > CLAIM_TTL_SECONDS:
                        p.unlink()
                        reaped += 1
                except Exception:
                    pass
        if reaped:
            logger.info(f"Reaped {reaped} expired claim(s) for '{bot_name}'")
    except Exception:
        pass
    return reaped


def _clean_stale_heartbeats(max_age: float = 300.0) -> int:
    """Remove heartbeat files older than max_age seconds from previous orchestrator runs."""
    cleaned = 0
    now = time.time()
    for hb_file in STATE_DIR.glob("*.heartbeat"):
        try:
            age = now - hb_file.stat().st_mtime
            if age > max_age:
                hb_file.unlink(missing_ok=True)
                cleaned += 1
        except Exception:
            pass
    if cleaned:
        logger.info(f"Cleaned {cleaned} stale heartbeat(s)")
    return cleaned


def _sweep_orphan_claims(bots: dict[str, BotState]) -> int:
    """Delete claim files whose owning bot process is dead or timed out.

    Prevents permanently locked QUEUE items when a worker crashes mid-task
    without releasing its claim. Runs periodically to reduce I/O overhead.
    """
    global _last_sweep_time
    now = time.time()
    
    # Skip full sweep if performed recently (reduces I/O/CPU overhead)
    if now - _last_sweep_time < SWEEP_INTERVAL:
        return 0
    
    swept = 0
    claims_dir = STATE_DIR / "claims"
    if not claims_dir.exists():
        return 0
    
    alive_bots = {name for name, bot in bots.items() if bot.process is not None and bot.process.poll() is None}
    for p in claims_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            worker = data.get("worker", "")
            at = float(data.get("at", 0))
            age = now - at
            if worker not in alive_bots and age > CLAIM_TTL_SECONDS:
                p.unlink()
                swept += 1
                logger.info(f"Swept orphan claim {p.name} (worker={worker}, age={age:.0f}s)")
            elif age > CLAIM_TTL_SECONDS * 2:
                p.unlink()
                swept += 1
                logger.warning(f"Swept expired claim {p.name} (worker={worker}, age={age:.0f}s > {CLAIM_TTL_SECONDS * 2}s)")
        except (json.JSONDecodeError, ValueError, OSError):
            try:
                if now - p.stat().st_mtime > CLAIM_TTL_SECONDS:
                    p.unlink()
                    swept += 1
            except OSError:
                pass
    return swept


def _load_ticket_context(ticket_id: str) -> str:
    try:
        from codebot.ticket_engine import TicketStore
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if not store_path.exists():
            return ""
        ts = TicketStore(store_path)
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


_ADAPTIVE_SCHEDULER = None
_FREEZE_DETECTOR = None


def _get_freeze_detector():
    global _FREEZE_DETECTOR
    if _FREEZE_DETECTOR is None:
        try:
            from codebot.freeze_detector import FreezeDetector
            _FREEZE_DETECTOR = FreezeDetector(state_dir=STATE_DIR)
        except ImportError:
            pass
    return _FREEZE_DETECTOR


def _adaptive_schedule_gate(bots: dict[str, BotState]) -> None:
    global _ADAPTIVE_SCHEDULER
    try:
        from codebot.adaptive_scheduler import AdaptiveScheduler
        from codebot.scheduler_config import SchedulerConfig
        from codebot.pipeline_state import PipelineState, WorkerSlot
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return

    if _ADAPTIVE_SCHEDULER is None:
        try:
            cfg = SchedulerConfig.default()
            _ADAPTIVE_SCHEDULER = AdaptiveScheduler(config=cfg)
        except Exception:
            return

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return

    try:
        ts = TicketStore(store_path)
        counts = ts.summary()
    except Exception:
        return

    now = time.time()
    workers = []
    for name, bot in bots.items():
        if bot.process is not None and bot.process.poll() is None:
            base = name.split("-")[0] if "-" in name else name
            role = base if base in IMPLEMENTER_ROLE_NAMES | DISCOVERY_ROLE_NAMES | REVIEWER_ROLE_NAMES else base
            workers.append(WorkerSlot(
                worker_id=name,
                ticket_id=getattr(bot, "_assigned_ticket_id", "") or name,
                role=role,
                started_at=bot.started_at or now,
                heartbeat_at=read_heartbeat(name) or now,
                lease_expires=now + 600,
                model=bot.config.model,
            ))

    ps = PipelineState(
        discovered_count=counts.get("DISCOVERED", 0),
        validating_count=counts.get("VALIDATING", 0),
        triaged_count=counts.get("TRIAGED", 0),
        ready_count=counts.get("READY", 0),
        decompose_count=counts.get("DECOMPOSE", 0),
        planning_count=counts.get("PLANNING", 0),
        implementing_count=counts.get("IMPLEMENTING", 0),
        reviewing_count=counts.get("REVIEWING", 0),
        verifying_count=counts.get("VERIFYING", 0),
        rework_count=counts.get("REWORK", 0),
        blocked_count=counts.get("BLOCKED", 0),
        candidate_count=0,
        active_workers=tuple(workers),
        total_slots=GATEWAY_MAX_CONCURRENT,
        snapshot_time=now,
    )

    try:
        ready_tickets = ts.list_by_state(TicketState.READY)
        review_tickets = ts.list_by_state(TicketState.REVIEWING)
        verify_tickets = ts.list_by_state(TicketState.VERIFYING)
        rework_tickets = ts.list_by_state(TicketState.REWORK)
        planning_tickets = ts.list_by_state(TicketState.PLANNING)
        candidate_tickets = (
            ts.list_by_state(TicketState.DISCOVERED)
            + ts.list_by_state(TicketState.VALIDATING)
            + ts.list_by_state(TicketState.TRIAGED)
        )
    except Exception:
        ready_tickets = []
        review_tickets = []
        verify_tickets = []
        rework_tickets = []
        planning_tickets = []
        candidate_tickets = []

    _ADAPTIVE_SCHEDULER.tick(
        pipeline=ps,
        ready_tickets=ready_tickets,
        review_tickets=review_tickets,
        verify_tickets=verify_tickets,
        rework_tickets=rework_tickets,
        planning_tickets=planning_tickets,
        candidate_tickets=candidate_tickets,
        now=now,
    )


def _dispatch_tickets_to_implementers(bots: dict[str, BotState]) -> int:
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0
    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0
    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0
    ready = ts.list_by_state(TicketState.PLANNING)
    if not ready:
        return 0
    claims_dir = STATE_DIR / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    active_claims: set[str] = set()
    for p in claims_dir.glob("*.json"):
        active_claims.add(p.stem.rsplit(".", 1)[0])
    idle_impl = []
    unassigned_running = []
    for name, bot in bots.items():
        base_name = name.split("-")[0] if "-" in name else name
        if base_name not in IMPLEMENTER_ROLE_NAMES:
            continue
        if bot.process is not None and bot.process.poll() is None:
            if not getattr(bot, '_assigned_ticket_id', ''):
                unassigned_running.append((name, bot))
        else:
            idle_impl.append((name, bot))
    available = idle_impl + unassigned_running
    dispatched = 0
    for ticket in ready:
        if not available:
            break
        tid = getattr(ticket, 'id', '')
        if tid in active_claims:
            continue
        tc = getattr(ticket, 'ticket_class', None)
        tc_val = tc.value if hasattr(tc, 'value') else str(tc) if tc else "feature"
        target_base = TICKET_CLASS_TO_IMPLEMENTER.get(tc_val, "general_implementer")
        matched = None
        for i, (name, bot) in enumerate(available):
            base = name.split("-")[0] if "-" in name else name
            if base == target_base:
                matched = (i, name, bot)
                break
        if matched is None:
            for i, (name, bot) in enumerate(available):
                base = name.split("-")[0] if "-" in name else name
                if base == "general_implementer":
                    matched = (i, name, bot)
                    break
        if matched is None and available:
            matched = (0, available[0][0], available[0][1])
        if matched is None:
            break
        idx, bot_name, bot = matched
        available.pop(idx)
        claim_file = claims_dir / f"{tid}.{bot_name}.json"
        try:
            claim_data = {"ticket_id": tid, "bot": bot_name, "at": time.time(), "class": tc_val}
            tmp = claim_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(claim_data), encoding="utf-8")
            tmp.replace(claim_file)
        except OSError:
            continue
        try:
            ts.transition(tid, TicketState.IMPLEMENTING)
        except Exception as e:
            logger.warning(f"Ticket {tid} transition failed: {e} — skipping")
            continue
        bot._assigned_ticket_id = tid
        dispatched += 1
        logger.info(f"Dispatched ticket {tid} ({tc_val}) -> {bot_name}")
        if bot.process is not None and bot.process.poll() is None:
            stop_bot(bot, f"restarting with ticket {tid}")
            start_bot(bot, bots=bots)
    return dispatched


def _dispatch_tickets_to_reviewers(bots: dict[str, BotState]) -> int:
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0

    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0

    reviewing = ts.list_by_state(TicketState.REVIEWING)
    if not reviewing:
        return 0

    claims_dir = STATE_DIR / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)

    live_bots: dict[str, str] = {}
    for name, bot in bots.items():
        if bot.process is not None and bot.process.poll() is None:
            assigned = getattr(bot, '_assigned_ticket_id', '')
            if assigned:
                live_bots[name] = assigned

    now = time.time()
    for p in claims_dir.glob("*.json"):
        parts = p.stem.rsplit(".", 1)
        if len(parts) != 2:
            continue
        claimed_tid, claimed_bot = parts
        if claimed_bot not in live_bots or live_bots[claimed_bot] != claimed_tid:
            try:
                age = now - p.stat().st_mtime
                if age < 120:
                    continue
                p.unlink()
            except OSError:
                pass

    active_claims: set[str] = set()
    for p in claims_dir.glob("*.json"):
        active_claims.add(p.stem.rsplit(".", 1)[0])

    idle_reviewers = []
    unassigned_running = []
    busy_ticket_ids: set[str] = set()
    for name, bot in bots.items():
        base_name = name.split("-")[0] if "-" in name else name
        if base_name not in REVIEWER_ROLE_NAMES and base_name != "ux_reviewer":
            continue
        if bot.process is not None and bot.process.poll() is None:
            assigned = getattr(bot, '_assigned_ticket_id', '')
            if assigned:
                busy_ticket_ids.add(assigned)
            else:
                unassigned_running.append((name, bot))
        else:
            idle_reviewers.append((name, bot))

    available = idle_reviewers + unassigned_running
    dispatched = 0

    for ticket in reviewing:
        if not available:
            break

        tid = getattr(ticket, 'id', '')
        if not tid or tid in active_claims or tid in busy_ticket_ids:
            continue

        tc = getattr(ticket, 'ticket_class', None)
        tc_val = tc.value if hasattr(tc, 'value') else str(tc) if tc else "feature"
        target_base = TICKET_CLASS_TO_REVIEWER.get(tc_val, "correctness_reviewer")

        matched = None
        for i, (name, bot) in enumerate(available):
            base = name.split("-")[0] if "-" in name else name
            if base == target_base:
                matched = (i, name, bot)
                break

        if matched is None:
            for i, (name, bot) in enumerate(available):
                base = name.split("-")[0] if "-" in name else name
                if base == "correctness_reviewer":
                    matched = (i, name, bot)
                    break

        if matched is None and available:
            matched = (0, available[0][0], available[0][1])

        if matched is None:
            break

        idx, bot_name, bot = matched
        available.pop(idx)

        existing_claim = claims_dir / f"{tid}.{bot_name}.json"
        if existing_claim.exists():
            continue

        claim_file = claims_dir / f"{tid}.{bot_name}.json"
        try:
            claim_data = {"ticket_id": tid, "bot": bot_name, "at": time.time(), "class": tc_val}
            tmp = claim_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(claim_data), encoding="utf-8")
            tmp.replace(claim_file)
        except OSError:
            continue

        bot._assigned_ticket_id = tid
        dispatched += 1
        logger.info(f"Dispatched review for ticket {tid} ({tc_val}) -> {bot_name}")
        if bot.process is not None and bot.process.poll() is None:
            pass
        else:
            start_bot(bot, bots=bots)

    for bot_name, bot in bots.items():
        base = bot_name.split("-")[0] if "-" in bot_name else bot_name
        if base not in REVIEWER_ROLE_NAMES and base != "ux_reviewer":
            continue
        assigned = getattr(bot, '_assigned_ticket_id', '')
        if assigned and bot.process is None:
            alive = False
            for p in claims_dir.glob(f"{assigned}.{bot_name}.json"):
                try:
                    age = time.time() - p.stat().st_mtime
                    if age < 120:
                        alive = True
                        break
                except OSError:
                    pass
            if alive:
                start_bot(bot, bots=bots)

    return dispatched


def _advance_reviewed_tickets(bots: dict[str, BotState]) -> int:
    """Transition REVIEWING tickets to VERIFYING or REWORK based on reviewer verdicts.

    Checks each reviewing ticket's assigned reviewer claim files for completed
    reviews. If all required reviewers have finished (claim released), advances
    the ticket to VERIFYING. If any reviewer flagged rework, sends to REWORK.
    """
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0

    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0

    reviewing = ts.list_by_state(TicketState.REVIEWING)
    if not reviewing:
        return 0

    claims_dir = STATE_DIR / "claims"
    if not claims_dir.exists():
        return 0

    advanced = 0
    for ticket in reviewing:
        tid = getattr(ticket, 'id', '')
        if not tid:
            continue

        review_claims = list(claims_dir.glob(f"{tid}.*.json"))
        if not review_claims:
            continue

        has_rework_flag = False
        all_reviewers_done = True

        for claim_file in review_claims:
            bot_name = claim_file.stem.rsplit(".", 1)[-1] if "." in claim_file.stem else ""
            base_name = bot_name.split("-")[0] if "-" in bot_name else bot_name
            if base_name not in REVIEWER_ROLE_NAMES:
                continue

            bot = bots.get(bot_name)
            if bot is None:
                continue

            is_running = bot.process is not None and bot.process.poll() is None
            assigned = getattr(bot, '_assigned_ticket_id', '')

            if is_running and assigned == tid:
                all_reviewers_done = False
                continue

            tasklog = LOGS_DIR / f"{bot_name}.tasklog"
            if tasklog.exists():
                try:
                    content = tasklog.read_text(encoding="utf-8", errors="ignore")
                    last_lines = content.strip().splitlines()[-5:] if content.strip() else []
                    last_text = " ".join(last_lines).upper()
                    if "VERDICT: REWORK" in last_text or "VERDICT: BLOCK" in last_text or "VERDICT: FAIL" in last_text:
                        has_rework_flag = True
                except OSError:
                    pass

        if not all_reviewers_done:
            continue

        try:
            if has_rework_flag:
                ts.transition(tid, TicketState.REWORK)
                logger.info(f"Review verdict: {tid} -> REWORK")
            else:
                ts.transition(tid, TicketState.VERIFYING)
                logger.info(f"Review verdict: {tid} -> VERIFYING")

            for claim_file in review_claims:
                try:
                    claim_file.unlink()
                except OSError:
                    pass

            for name, bot in bots.items():
                if getattr(bot, '_assigned_ticket_id', '') == tid:
                    bot._assigned_ticket_id = ''

            advanced += 1
        except ValueError as e:
            logger.warning(f"Failed to advance {tid}: {e}")

    return advanced


def _gatekeeper_verify_tickets() -> int:
    """Advance VERIFYING tickets to COMPLETE via quality gate evaluation.

    Runs deterministic quality gates (build, tests) on each verifying ticket.
    If all gates pass, transitions to COMPLETE. If gates fail, sends to REWORK.
    This is the sole authority for completion (§5).
    """
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0

    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0

    verifying = ts.list_by_state(TicketState.VERIFYING)
    if not verifying:
        return 0

    advanced = 0
    for ticket in verifying:
        tid = getattr(ticket, 'id', '')
        if not tid:
            continue

        try:
            from codebot.quality_gate import run_quality_gates, load_policy, record_gate_results
            policy = load_policy()
            workspace = Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))
            tc = getattr(ticket, 'ticket_class', None)
            tc_val = tc.value if hasattr(tc, 'value') else str(tc) if tc else "feature"
            passed, evaluations = run_quality_gates(
                policy=policy,
                workspace=workspace,
                ticket_class=tc_val,
            )
            record_gate_results(STATE_DIR, tid, passed, evaluations)

            if passed:
                changed_files = ticket.affected_modules if ticket.affected_modules else []
                files_actually_modified = False
                if not changed_files:
                    files_actually_modified = False
                else:
                    for mod in changed_files[:5]:
                        try:
                            r = subprocess.run(
                                ["git", "diff", "--name-only", "HEAD~5", "--", mod],
                                capture_output=True, text=True,
                                cwd=str(Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))),
                                timeout=10,
                            )
                            if r.stdout.strip():
                                files_actually_modified = True
                                break
                        except Exception:
                            pass
                if not files_actually_modified:
                    rework_count = getattr(ticket, 'rework_count', 0)
                    if rework_count < 3:
                        ts.transition(tid, TicketState.REWORK)
                        logger.info(f"Gatekeeper: {tid} -> REWORK (gates passed but no files modified in git)")
                    else:
                        ts.transition(tid, TicketState.REJECTED)
                        logger.warning(f"Gatekeeper: {tid} -> REJECTED (no modifications after {rework_count} reworks)")
                    advanced += 1
                    continue
                ts.transition(tid, TicketState.COMPLETE)
                logger.info(f"Gatekeeper: {tid} -> COMPLETE (all gates passed)")
                try:
                    from codebot.scratchpad import clear_scratchpad
                    clear_scratchpad(STATE_DIR, tid)
                except Exception:
                    pass
                advanced += 1
            else:
                rework_count = getattr(ticket, 'rework_count', 0)
                if rework_count < 3:
                    ts.transition(tid, TicketState.REWORK)
                    logger.info(f"Gatekeeper: {tid} -> REWORK (gates failed, attempt {rework_count + 1})")
                else:
                    ts.transition(tid, TicketState.REJECTED)
                    logger.warning(f"Gatekeeper: {tid} -> REJECTED (exceeded {rework_count} reworks)")
                advanced += 1
        except Exception as e:
            logger.warning(f"Gatekeeper verification failed for {tid}: {e}")

    return advanced


_LAST_PUSH_TIME: float = 0.0
_PUSH_COOLDOWN_SECONDS: int = 300


def _auto_push_to_master() -> None:
    """Commit and push local changes to master when tickets reach READY.

    Runs at most once per PUSH_COOLDOWN_SECONDS to avoid flooding the remote.
    Only pushes if there are actual uncommitted changes in the working tree.
    """
    global _LAST_PUSH_TIME
    now = time.time()
    if now - _LAST_PUSH_TIME < _PUSH_COOLDOWN_SECONDS:
        return

    project_root = Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))

    try:
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(project_root), timeout=10,
        )
        if status_result.returncode != 0:
            return
        dirty_lines = [l for l in status_result.stdout.strip().splitlines() if l.strip()]
        if not dirty_lines:
            return

        changed_files = []
        for line in dirty_lines[:20]:
            path = line[3:].strip() if len(line) > 3 else ""
            if path:
                changed_files.append(path)

        try:
            from codebot.ticket_engine import TicketStore, TicketState
            store_path = STATE_DIR / "tickets.json"
            if not store_path.exists():
                store_path = Path(".codebot/state/tickets.json")
            ready_count = 0
            if store_path.exists():
                ts = TicketStore(store_path)
                ready_count = len(ts.list_ready())
        except Exception:
            ready_count = 0

        msg_parts = [f"auto-sync: {len(dirty_lines)} file(s) changed"]
        if ready_count > 0:
            msg_parts.append(f"{ready_count} tickets READY")
        if changed_files:
            top_files = changed_files[:5]
            msg_parts.append(", ".join(top_files))
            if len(changed_files) > 5:
                msg_parts[-1] += f" +{len(changed_files)-5} more"
        commit_msg = " | ".join(msg_parts)

        add_result = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True, text=True, cwd=str(project_root), timeout=15,
        )
        if add_result.returncode != 0:
            logger.warning(f"Auto-push: git add failed: {add_result.stderr[:200]}")
            return

        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            capture_output=True, text=True, cwd=str(project_root), timeout=15,
        )
        if commit_result.returncode != 0:
            if "nothing to commit" in commit_result.stdout.lower():
                return
            logger.warning(f"Auto-push: git commit failed: {commit_result.stderr[:200]}")
            return

        push_result = subprocess.run(
            ["git", "push", "origin", "master"],
            capture_output=True, text=True, cwd=str(project_root), timeout=30,
        )
        if push_result.returncode == 0:
            _LAST_PUSH_TIME = now
            logger.info(f"Auto-push: committed and pushed to master ({commit_msg})")
        else:
            logger.warning(f"Auto-push: git push failed: {push_result.stderr[:200]}")

    except subprocess.TimeoutExpired:
        logger.warning("Auto-push: git operation timed out")
    except Exception as e:
        logger.warning(f"Auto-push failed: {e}")


MIN_READY_BACKLOG = 5


def _auto_triage_backlog() -> int:
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0
    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0
    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0
    ready_count = len(ts.list_ready())
    discovered = ts.list_by_state(TicketState.DISCOVERED)
    validating = ts.list_by_state(TicketState.VALIDATING)
    triaged = ts.list_by_state(TicketState.TRIAGED)
    backlog_depth = len(discovered) + len(validating) + len(triaged)
    max_advance_per_tick = max(5, min(backlog_depth, 20))
    advanced = 0
    for state in (TicketState.DISCOVERED, TicketState.VALIDATING, TicketState.TRIAGED):
        if advanced >= max_advance_per_tick:
            break
        for ticket in ts.list_by_state(state):
            if advanced >= max_advance_per_tick:
                break
            target = TicketState.VALIDATING if state == TicketState.DISCOVERED else (
                TicketState.TRIAGED if state == TicketState.VALIDATING else TicketState.READY
            )
            try:
                ts.transition(ticket.id, target)
                advanced += 1
                logger.info(f"Auto-triaged {ticket.id}: {state.value} -> {target.value}")
            except ValueError:
                pass
    return advanced


_AUTOPUSH_COOLDOWN_SECONDS = 300
_last_autopush_time: float = 0.0


def _autopush_to_master() -> bool:
    """Commit and push local changes to master to keep remote in sync.

    Runs after auto-triage advances tickets to READY. Only pushes if there
    are actual uncommitted changes and enough time has passed since the
    last push (cooldown prevents commit spam).

    Returns True if a push was performed.
    """
    global _last_autopush_time
    now = time.time()
    if now - _last_autopush_time < _AUTOPUSH_COOLDOWN_SECONDS:
        return False

    project_root = Path(os.environ.get("CODEBOT_PROJECT_ROOT", BOTS_DIR))
    try:
        status_result = subprocess.run(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, cwd=str(project_root), timeout=10,
        )
        if status_result.returncode != 0 or not status_result.stdout.strip():
            return False

        changed_files = []
        for line in status_result.stdout.strip().splitlines():
            if len(line) > 3:
                changed_files.append(line[3:].strip())

        try:
            from codebot.ticket_engine import TicketStore, TicketState
            store_path = STATE_DIR / "tickets.json"
            if not store_path.exists():
                store_path = Path(".codebot/state/tickets.json")
            ready_ids = []
            if store_path.exists():
                ts = TicketStore(store_path)
                ready_ids = [t.id for t in ts.list_by_state(TicketState.READY)[:5]]
        except Exception:
            ready_ids = []

        if ready_ids:
            msg = f"auto: sync local changes ({len(changed_files)} files) — READY tickets: {', '.join(ready_ids)}"
        else:
            msg = f"auto: sync local changes ({len(changed_files)} files)"

        add_result = subprocess.run(
            ["git", "add", "-A"],
            capture_output=True, text=True, cwd=str(project_root), timeout=15,
        )
        if add_result.returncode != 0:
            logger.warning(f"Auto-push: git add failed: {add_result.stderr[:200]}")
            return False

        commit_result = subprocess.run(
            ["git", "commit", "-m", msg],
            capture_output=True, text=True, cwd=str(project_root), timeout=15,
        )
        if commit_result.returncode != 0:
            if "nothing to commit" in commit_result.stdout.lower():
                return False
            logger.warning(f"Auto-push: git commit failed: {commit_result.stderr[:200]}")
            return False

        push_result = subprocess.run(
            ["git", "push", "origin", "master"],
            capture_output=True, text=True, cwd=str(project_root), timeout=30,
        )
        if push_result.returncode != 0:
            logger.warning(f"Auto-push: git push failed: {push_result.stderr[:200]}")
            return False

        _last_autopush_time = now
        logger.info(f"Auto-pushed to master: {msg}")
        return True
    except subprocess.TimeoutExpired:
        logger.warning("Auto-push: git command timed out")
        return False
    except Exception as e:
        logger.warning(f"Auto-push failed: {e}")
        return False


_METRICS_PATH = STATE_DIR / "bot_metrics.json"
_METRICS_WINDOW_DAYS = 7


def _record_bot_metric(name: str, bot: BotState, alive: bool, exit_code: int | None, tokens_this_run: int = 0) -> None:
    try:
        data = {}
        if _METRICS_PATH.exists():
            data = json.loads(_METRICS_PATH.read_text(encoding="utf-8"))
        if name not in data:
            data[name] = {"runs": [], "successes": 0, "failures": 0, "total_tokens": 0, "total_duration_s": 0}
        entry = data[name]
        now = time.time()
        cutoff = now - (_METRICS_WINDOW_DAYS * 86400)
        entry["runs"] = [r for r in entry.get("runs", []) if r > cutoff]
        entry["runs"].append(now)
        if not alive and exit_code is not None:
            if exit_code == 0:
                entry["successes"] = entry.get("successes", 0) + 1
            else:
                entry["failures"] = entry.get("failures", 0) + 1
        started = bot.started_at or now
        entry["total_duration_s"] = entry.get("total_duration_s", 0) + (now - started)
        # C2: accumulate per-run token counts for burn rate tracking.
        entry["total_tokens"] = entry.get("total_tokens", 0) + int(tokens_this_run)
        # Cap runs list to prevent unbounded growth; serialize fully to avoid corrupt JSON
        MAX_RUNS_PER_BOT = 500
        if len(entry["runs"]) > MAX_RUNS_PER_BOT:
            entry["runs"] = entry["runs"][-MAX_RUNS_PER_BOT:]
        serialized = json.dumps(data, indent=2)
        if len(serialized) > 50000:
            # Prune oldest runs across all bots until under budget
            for bot_key in data:
                runs_list = data[bot_key].get("runs", [])
                if len(runs_list) > 10:
                    data[bot_key]["runs"] = runs_list[-10:]
            serialized = json.dumps(data, indent=2)
        tmp = _METRICS_PATH.with_suffix(".tmp")
        tmp.write_text(serialized, encoding="utf-8")
        tmp.replace(_METRICS_PATH)
    except Exception:
        pass


def _read_bot_status(bot_name: str) -> dict | None:
    """Read bot status file for orchestrator visibility.

    Returns parsed status dict or None if unavailable/corrupt.
    Status file: state/{bot_name}.status.json
    """
    status_path = STATE_DIR / f"{bot_name}.status.json"
    try:
        if not status_path.exists():
            return None
        data = json.loads(status_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None
        # Check staleness: status older than 5 minutes is stale
        updated_at = data.get("updated_at", 0)
        if time.time() - updated_at > 300:
            return None
        return data
    except Exception:
        return None


def _log_bot_statuses(bots: dict[str, "BotState"]) -> None:
    """Log current activity for all running bots from status + tasklog files."""
    detector = _get_freeze_detector()
    for name, bot in bots.items():
        alive = bot.process is not None and bot.process.poll() is None
        if not alive:
            continue
        status = _read_bot_status(name)
        if not status:
            continue
        task = status.get("current_task", "unknown")
        desc = status.get("task_description", "")
        iteration = status.get("iteration", 0)
        files = status.get("files_touched", [])
        files_str = ", ".join(files[-3:]) if files else "none"
        age_s = time.time() - status.get("updated_at", 0)
        taskline = _read_bot_taskline(name)
        logger.info(
            f"[status] {name}: task={task} iter={iteration} "
            f"files=[{files_str}] age={age_s:.0f}s"
            + (f" desc={desc[:120]}" if desc else "")
            + (f" | {taskline}" if taskline else "")
        )
        if detector is not None:
            progress = 1 if any(t in task for t in ("create_ticket", "write", "transition")) else 0
            detector.observe(
                bot_name=name,
                iteration=iteration,
                current_task=task,
                updated_at=status.get("updated_at", time.time()),
                files_touched=files,
                progress_actions=progress,
            )


def _read_bot_taskline(bot_name: str) -> str:
    """Return the last tasklog line for a bot (short current-task summary)."""
    try:
        path = LOGS_DIR / f"{bot_name}.tasklog"
        if not path.exists():
            return ""
        lines = path.read_text(encoding="utf-8").splitlines()
        if not lines:
            return ""
        return lines[-1].strip()[:200]
    except Exception:
        return ""


def is_log_stalled(bot: BotState) -> bool:
    prof = model_profile(bot.config.model)
    stall = prof.log_stall_seconds if prof else 180
    mtime = log_mtime(bot.config.name)
    if mtime == 0.0:
        return False
    return (time.time() - mtime) > stall


def is_stuck(bot: BotState) -> bool:
    detector = _get_freeze_detector()
    if detector is not None:
        report = detector.is_frozen(bot.config.name)
        if report.frozen:
            logger.warning(f"Freeze detected for '{bot.config.name}': {report.reason} {report.details}")
            return True
    eff = effective_heartbeat_timeout(bot)
    last = read_heartbeat(bot.config.name)
    if last == 0.0:
        if bot.process and bot.process.poll() is None:
            elapsed = time.time() - bot.last_heartbeat
            if elapsed <= eff:
                return False
            return is_log_stalled(bot) or elapsed > eff + 120
        return False
    bot.last_heartbeat = last
    hb_age = time.time() - last
    if hb_age < 0:
        hb_age = 0.0
    if hb_age <= eff:
        return False
    prof = model_profile(bot.config.model)
    if prof and prof.lockup_risk in ("high", "medium-high"):
        return is_log_stalled(bot)
    return True

# ---------------------------------------------------------------------------
# Bot Recovery
# ---------------------------------------------------------------------------

def _is_disabled_too_long(bot: BotState, threshold: float = 10.0) -> bool:
    state_file = STATE_DIR / f"{bot.config.name}.state.json"
    try:
        if state_file.exists():
            data = json.loads(state_file.read_text())
            if data.get("status") == "disabled":
                last_update = data.get("last_update", 0)
                if time.time() - last_update > threshold:
                    return True
    except Exception:
        pass
    return False

def _retry_disabled_bot(bot: BotState, bots: dict[str, BotState]) -> bool:
    if not _is_disabled_too_long(bot):
        return False
    
    logger.info(f"Retrying disabled bot '{bot.config.name}' after 10s cooldown")
    bot.consecutive_errors = 0
    bot.config.enabled = True
    update_bot_state(bot, "waiting")
    return True

def _is_stuck_starting(bot: BotState, threshold: float = 120.0) -> bool:
    # If process is dead, we're definitely stuck (died during startup)
    if bot.process is not None and bot.process.poll() is not None:
        return True
    
    # If no process yet (orchestrator just started), check heartbeat age
    # Stale heartbeat from previous run = stuck
    last_hb = read_heartbeat(bot.config.name)
    if last_hb > 0:
        # Heartbeat exists — is it fresh or stale?
        if time.time() - last_hb > threshold:
            return True  # Stale heartbeat = stuck
        return False  # Fresh heartbeat = still starting normally
    
    # No heartbeat at all: check elapsed since orchestrator started this bot
    elapsed = time.time() - (bot.started_at or bot.last_heartbeat or time.time())
    return elapsed > threshold

def _retry_stuck_starting(bot: BotState, bots: dict[str, BotState]) -> bool:
    if not _is_stuck_starting(bot):
        return False
    
    bot.restart_count += 1
    
    if bot.restart_count >= 2 and bot.config.fallback_model and bot.config.model != bot.config.fallback_model:
        logger.info(f"Retrying bot '{bot.config.name}' with fallback model '{bot.config.fallback_model}' (attempt {bot.restart_count})")
        bot.config.model = bot.config.fallback_model
    else:
        logger.info(f"Retrying bot '{bot.config.name}' stuck starting >2min (attempt {bot.restart_count})")
    
    try:
        bot.process.kill()
    except Exception:
        pass
    bot.process = None
    update_bot_state(bot, "waiting")
    start_bot(bot, bots=bots, is_overture=True)
    return True

# ---------------------------------------------------------------------------
# Process Management
# ---------------------------------------------------------------------------

def _count_queued_bots(bots: dict[str, BotState]) -> int:
    count = 0
    for b in bots.values():
        state_file = STATE_DIR / f"{b.config.name}.state.json"
        try:
            if state_file.exists() and json.loads(state_file.read_text()).get("status") == "queued":
                count += 1
        except Exception:
            pass
    return count


def _is_queued(bot: BotState) -> bool:
    state_file = STATE_DIR / f"{bot.config.name}.state.json"
    try:
        if state_file.exists():
            return json.loads(state_file.read_text()).get("status") == "queued"
    except Exception:
        pass
    return False


def _count_running_by_model(bots: dict[str, BotState]) -> dict[str, int]:
    """Count running bots grouped by model type."""
    counts: dict[str, int] = {}
    for b in bots.values():
        proc = getattr(b, "process", None)
        if proc is not None and proc.poll() is None:
            model = b.config.model
            counts[model] = counts.get(model, 0) + 1
    return counts


def _count_api_runner_processes() -> int:
    count = 0
    try:
        for process_dir in Path("/proc").iterdir():
            if not process_dir.name.isdigit():
                continue
            try:
                command = (process_dir / "cmdline").read_bytes()
            except OSError:
                continue
            if b"api_runner.py" in command:
                count += 1
    except OSError:
        return 0
    return count


def _count_running_workers(bots: dict[str, BotState] | None = None) -> int:
    if not bots:
        return 0
    n = 0
    for name in WORKER_POOL:
        bot = bots.get(name)
        if bot is not None and bot.process is not None and bot.process.poll() is None:
            n += 1
    return n


def _count_running_non_workers(bots: dict[str, BotState] | None = None) -> int:
    if not bots:
        return 0
    n = 0
    for name, bot in bots.items():
        if name in WORKER_POOL:
            continue
        if bot.process is not None and bot.process.poll() is None:
            n += 1
    return n


def _count_running_by_category(bots: dict[str, BotState] | None) -> tuple[int, int, int, int]:
    implementers = 0
    discovery = 0
    reviewers = 0
    other = 0
    if not bots:
        return implementers, discovery, reviewers, other
    for name, bot in bots.items():
        base = name.split("-")[0] if "-" in name else name
        if bot.process is None or bot.process.poll() is not None:
            continue
        if base in IMPLEMENTER_ROLE_NAMES:
            implementers += 1
        elif base in DISCOVERY_ROLE_NAMES:
            discovery += 1
        elif base in REVIEWER_ROLE_NAMES:
            reviewers += 1
        else:
            other += 1
    return implementers, discovery, reviewers, other


_last_spawn_time: float = 0.0
_SPAWN_STAGGER_SECONDS: float = 5.0


def _spawn_gate(bots: dict[str, BotState] | None = None, is_queued: bool = False, runner_mode: str = "api", bot_model: str = "", bot_name: str = "", is_overture: bool = False) -> tuple[bool, str]:
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
        claims_dir = STATE_DIR / "claims"
        if claims_dir.exists():
            for cf in claims_dir.glob(f"*.{bot_name}.json"):
                try:
                    age = time.time() - cf.stat().st_mtime
                    if age < 120:
                        has_assignment = True
                        break
                except OSError:
                    pass
    is_demand_driven = False
    if bot_name:
        base = bot_name.split("-")[0] if "-" in bot_name else bot_name
        if base in ("decomposer", "implementation_planner"):
            is_demand_driven = True
    since_last = now - _last_spawn_time
    if since_last < _SPAWN_STAGGER_SECONDS and not has_assignment and not is_overture and not is_demand_driven:
        return False, f"stagger {since_last:.1f}s < {_SPAWN_STAGGER_SECONDS}s"

    if runner_mode == "api":
        available = _get_available_memory_mb()
        if available < CODEBOT_MIN_MEMORY_MB:
            return False, f"memory {available:.0f}MB < {CODEBOT_MIN_MEMORY_MB}MB"
    
    marker = STATE_DIR / ".last_spawn"
    try:
        last = float(marker.read_text().strip().split()[0]) if marker.exists() else 0.0
    except Exception:
        last = 0.0
    gap = now - last
    if not is_overture and not has_assignment and not is_demand_driven:
        if bot_name in WORKER_POOL:
            worker_gap = max(3, GATEWAY_MIN_SPAWN_GAP // 4)
            if gap < worker_gap:
                return False, f"gap {gap:.0f}s<{worker_gap}s (worker)"
        elif gap < GATEWAY_MIN_SPAWN_GAP:
            return False, f"gap {gap:.0f}s<{GATEWAY_MIN_SPAWN_GAP}s"
    
    return True, "slot available"


def start_bot(bot: BotState, resume_checkpoint: bool = True, checkpoint_reason: str | None = None, bots: dict[str, BotState] | None = None, is_overture: bool = False) -> bool:
    """Spawn a bot as a subprocess. Returns True on success."""
    if (STATE_DIR / f"{bot.config.name}.paused").exists():
        update_bot_state(bot, "paused")
        return False
    if bot.config.name in WORKER_POOL:
        rotated_model, rotated_fallback = _next_worker_model(bots)
        bot.config.model = rotated_model
        bot.config.fallback_model = rotated_fallback
    ckpt = checkpoint_path(bot.config.name)
    last_run_mtime = ckpt.stat().st_mtime if ckpt.exists() else 0.0
    inputs_changed = False
    try:
        manifest_path = BOTS_DIR / "manifests" / f"{bot.config.name.replace('-', '_')}.json"
        if manifest_path.exists():
            mdata = json.loads(manifest_path.read_text(encoding="utf-8"))
            for inp in mdata.get("input", []):
                ipath = Path(inp.get("path", ""))
                if not ipath.is_absolute():
                    ipath = BOTS_DIR / ipath
                if ipath.exists() and ipath.stat().st_mtime > last_run_mtime:
                    inputs_changed = True
                    break
    except Exception:
        inputs_changed = True
    prompt_path = BOTS_DIR / bot.config.prompt_file
    if prompt_path.exists() and prompt_path.stat().st_mtime > last_run_mtime:
        inputs_changed = True
    if not inputs_changed and last_run_mtime > 0 and bot.config.name not in WORKER_POOL and bot.config.name not in ALWAYS_RESPAWN:
        base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
        depths = _get_pipeline_state()
        has_demand = (
            _is_decomposer_role(bot.config.name) and depths.get("DECOMPOSE", 0) > 0
            or base in PLANNING_ROLE_NAMES and depths.get("PLANNING", 0) > 0
        )
        if not has_demand:
            logger.info(f"Bot '{bot.config.name}' skipped — no input changes since last run")
            bot.next_run_at = time.time() + bot.config.interval_seconds
            update_bot_state(bot, "noop")
            return False
    due = bot.next_run_at
    is_queued = _is_queued(bot)
    ok, why = _spawn_gate(bots=bots, is_queued=is_queued, runner_mode="api", bot_model=bot.config.model, bot_name=bot.config.name, is_overture=is_overture)
    if not ok:
        if due:
            bot.next_run_at = due
        if now_gate_logged(bot):
            if why.startswith("memory"):
                logger.info(f"Queued '{bot.config.name}' ({why})")
            else:
                logger.info(f"Queued '{bot.config.name}' (due, {why}) — conductor holds slot")
        update_bot_state(bot, "queued")
        return False
    prompt_file = BOTS_DIR / bot.config.prompt_file
    if not prompt_file.exists():
        logger.error(f"Prompt file not found: {prompt_file}")
        return False

    log_file = LOGS_DIR / f"{bot.config.name}.log"
    state_file = STATE_DIR / f"{bot.config.name}.state.json"

    # Write initial state
    state_data = {
        "bot": bot.config.name,
        "started": time.time(),
        "session": 0,
        "status": "starting",
    }
    _write_json_atomic(state_file, state_data)

    # Write initial heartbeat
    write_heartbeat(bot.config.name)
    bot.last_heartbeat = time.time()

    prompt_text = prompt_file.read_text()
    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
    logger.info(f"start_bot '{bot.config.name}': assigned_tid='{assigned_tid}', prompt={len(prompt_text)} chars")
    if assigned_tid:
        ticket_ctx = _load_ticket_context(assigned_tid)
        if ticket_ctx:
            prompt_text = f"{prompt_text}\n\n{ticket_ctx}"
            logger.info(f"Injected ticket context for {bot.config.name}: {assigned_tid} ({len(ticket_ctx)} chars)")
        else:
            logger.warning(f"No ticket context found for {bot.config.name}: {assigned_tid}")
    try:
        from codebot.scratchpad import load_scratchpad, create_handoff_note
        if assigned_tid:
            scratch_state = load_scratchpad(STATE_DIR, assigned_tid)
            if scratch_state.agent_history or scratch_state.completed_steps:
                handoff = create_handoff_note(scratch_state)
                prompt_text = f"{prompt_text}\n\n{handoff}\nResume from where the previous agent left off. Do NOT redo completed work."
    except Exception:
        pass
    try:
        bot.prompt_mtime = prompt_file.stat().st_mtime
        bot.last_prompt_mtime = bot.prompt_mtime
    except OSError:
        bot.prompt_mtime = 0.0
        bot.last_prompt_mtime = 0.0
    if not bot.last_code_mtimes:
        bot.last_code_mtimes = _get_code_mtimes()
    heartbeat_file = STATE_DIR / f"{bot.config.name}.heartbeat"
    ckpt_file = checkpoint_path(bot.config.name)
    ckpt = read_checkpoint(bot.config.name) if resume_checkpoint else None
    ckpt_block = ""
    if ckpt:
        try:
            ckpt_block = (
                f"\n--- CHECKPOINT HANDOFF (previous agent stalled) ---\n"
                f"The previous agent for '{bot.config.name}' left this checkpoint at "
                f"{ckpt.get('updated_at', '?')} (reason: {ckpt.get('reason', 'lockup')}).\n"
                f"Resume from here first — do NOT redo completed work:\n"
                f"```json\n{json.dumps(ckpt, indent=2)[:6000]}\n```\n"
                f"Checkpoint file: {ckpt_file}\n"
                f"After resuming, update the checkpoint with your progress.\n"
            )
        except Exception:
            ckpt_block = ""
    if _GATEWAY:
        message = _gateway_build_message(
            bot.config.name, bot.config.model, prompt_text,
            str(heartbeat_file), str(ckpt_file), ckpt_block,
            str(STATE_DIR), str(LOGS_DIR), prompt_file.name)
    else:
        message = (
            f"Sisyphus — delegated task: '{bot.config.name}' workflow (model {bot.config.model}).\n"
            f"Remain Sisyphus; do not adopt a new identity. Execute the specification below as a bounded delegated task, not an infinite daemon.\n"
            f"- At startup and after every atomic task, write Unix timestamp to {heartbeat_file} using the `write` tool.\n"
            f"- After every atomic task, write <4KB checkpoint to {ckpt_file} using the `write` tool (atomic tmp->replace).\n"
            f"- Before each atomic task, run `bash` with `test -f {STATE_DIR}/.drain || test -f {STATE_DIR}/.update_lock && echo DRAIN` to check for drain. If output contains DRAIN, exit 0. Do NOT use the `read` tool for drain checks.\n"
            f"- State dir: {STATE_DIR}  Log dir: {LOGS_DIR}  Prompt: {prompt_file.name}\n"
            f"{ckpt_block}\n"
            f"--- Task Specification ({prompt_file.name}) ---\n"
            f"{prompt_text}"
        )

    try:
        log_fh = open(log_file, "a")

        mission_file = LOGS_DIR / f"{bot.config.name}.mission"
        mission_file.write_text(message, encoding="utf-8")
        child_env = os.environ.copy()
        child_env["PYTHONPATH"] = str(BOTS_DIR)
        process = subprocess.Popen(
            [
                sys.executable,
                "-m", "codebot.api_runner",
                bot.config.name,
                bot.config.model,
                str(heartbeat_file),
                str(ckpt_file),
                str(mission_file),
                bot.config.fallback_model,
                str(bot.config.max_tokens_per_run),
                ",".join(bot.config.fallback_models) if bot.config.fallback_models else "",
            ],
            stdout=log_fh,
            stderr=subprocess.STDOUT,
            cwd=str(BOTS_DIR),
            env=child_env,
            start_new_session=True,
        )

        bot.process = process
        bot.last_heartbeat = time.time()
        bot.last_log_mtime = time.time()
        bot.next_run_at = time.time() + bot.config.interval_seconds
        bot.consecutive_errors = 0
        bot.started_at = state_data["started"]

        # Parent's copy of the fd is unused after Popen inherits it; close to prevent fd leak
        log_fh.close()

        global _last_spawn_time
        _last_spawn_time = time.time()
        try:
            (STATE_DIR / ".last_spawn").write_text(str(time.time()))
        except OSError:
            pass
        if _GATEWAY:
            _gateway_note_spawn()

        logger.info(f"Started bot '{bot.config.name}' (PID {process.pid})")
        return True

    except FileNotFoundError:
        if 'log_fh' in locals():
            log_fh.close()
        return False
    except Exception as e:
        logger.error(f"Failed to start bot '{bot.config.name}': {e}")
        if 'log_fh' in locals():
            log_fh.close()
        return False

def stop_bot(bot: BotState, reason: str = "manual") -> bool:
    """Stop a bot gracefully. SIGTERM → 5s → SIGKILL."""
    if not bot.process:
        return True

    pid = bot.process.pid
    logger.info(f"Stopping bot '{bot.config.name}' (PID {pid}, reason: {reason})")

    try:
        # SIGTERM first
        os.kill(pid, signal.SIGTERM)
        try:
            bot.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            # Force kill
            logger.warning(f"Bot '{bot.config.name}' did not stop, sending SIGKILL")
            os.kill(pid, signal.SIGKILL)
            bot.process.wait(timeout=3)
    except ProcessLookupError:
        pass  # Already dead
    except Exception as e:
        logger.error(f"Error stopping bot '{bot.config.name}': {e}")

    bot.process = None
    return True

def restart_bot(bot: BotState, reason: str = "stuck", bots: dict[str, BotState] | None = None) -> bool:
    """Stop and restart a bot. Respects rate limits."""
    per_bot_drain = STATE_DIR / f".drain_{bot.config.name}"
    if per_bot_drain.exists():
        try:
            per_bot_drain.unlink()
        except OSError:
            pass
    now = time.time()

    # Reset restart count every hour
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

def update_bot_state(bot: BotState, status: str) -> None:
    state_file = STATE_DIR / f"{bot.config.name}.state.json"
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
    return STATE_DIR / f"{bot_name}.checkpoint.json"


def read_checkpoint(bot_name: str) -> dict | None:
    p = checkpoint_path(bot_name)
    bak = p.with_suffix(".checkpoint.bak")
    if not p.exists():
        if bak.exists():
            try:
                raw = bak.read_text(encoding="utf-8")
                data = json.loads(raw)
                if isinstance(data, dict):
                    logger.info(f"Restored last-good checkpoint for '{bot_name}' from .bak")
                    return data
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(f"Checkpoint corrupt for '{bot_name}': {exc} — falling back to .bak")
        try:
            p.rename(bak)
        except OSError:
            try:
                p.unlink()
            except OSError:
                pass
        if bak.exists():
            try:
                fallback_raw = bak.read_text(encoding="utf-8")
                fallback_data = json.loads(fallback_raw)
                if isinstance(fallback_data, dict):
                    return fallback_data
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        return None
    except Exception:
        return None
    if not isinstance(data, dict):
        logger.warning(f"Checkpoint for '{bot_name}' is not a JSON object — falling back to .bak")
        try:
            p.rename(bak)
        except OSError:
            try:
                p.unlink()
            except OSError:
                pass
        if bak.exists():
            try:
                fallback_raw = bak.read_text(encoding="utf-8")
                fallback_data = json.loads(fallback_raw)
                if isinstance(fallback_data, dict):
                    return fallback_data
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                pass
        return None
    if len(raw.encode("utf-8")) > 4096:
        logger.warning(f"Checkpoint for '{bot_name}' exceeds 4KB ({len(raw.encode('utf-8'))} bytes) — truncating")
    try:
        bak.write_text(raw, encoding="utf-8")
    except OSError:
        pass
    return data


def _write_json_atomic(path: Path, data: dict | list | str) -> None:
    """Write JSON to path atomically via tmp+replace to prevent mid-write corruption."""
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(data, indent=2) if isinstance(data, (dict, list)) else data)
    tmp.replace(path)


@contextmanager
def _state_write_lock(bot_name: str) -> Iterator[None]:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = STATE_DIR / f"{bot_name}.state.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        _flock(lock.fileno(), LOCK_EX)
        try:
            yield
        finally:
            _flock(lock.fileno(), LOCK_UN)


def _read_state_file(bot_name: str) -> dict:
    state_file = STATE_DIR / f"{bot_name}.state.json"
    try:
        if state_file.exists():
            return json.loads(state_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"_state_error": "corrupt"}
    return {}


def _write_state_file(bot_name: str, data: dict) -> None:
    state_file = STATE_DIR / f"{bot_name}.state.json"
    _write_json_atomic(state_file, data)


def _manifest_restart_budget_exceeded(manifest: dict, now: float) -> tuple[bool, str]:
    name = manifest.get("name", "")
    max_restarts = manifest.get("max_restarts", 5)
    try:
        max_r = int(max_restarts)
    except Exception:
        max_r = 5
    if max_r <= 0:
        return False, ""
    state = _read_state_file(name)
    if state.get("_state_error") == "corrupt":
        return True, "state-corrupt"
    timestamps = state.get("restart_timestamps", [])
    if not isinstance(timestamps, list):
        timestamps = []
    recent = [float(ts) for ts in timestamps if isinstance(ts, (int, float)) and (now - float(ts)) < 3600]
    if len(recent) >= max_r:
        return True, f"restart-budget-exceeded ({len(recent)}/{max_r} in 1h)"
    return False, ""


def is_manifest_restart_budget_exceeded(manifest: dict, now: float) -> bool:
    return _manifest_restart_budget_exceeded(manifest, now)[0]


def _manifest_error_disabled(manifest: dict, max_consecutive: int = 3) -> tuple[bool, str]:
    name = manifest.get("name", "")
    state = _read_state_file(name)
    if state.get("_state_error") == "corrupt":
        return True, "state-corrupt"
    consecutive = state.get("consecutive_errors", 0)
    try:
        c = int(consecutive)
    except Exception:
        c = 0
    if c >= max_consecutive:
        return True, f"error-disabled ({c} consecutive errors)"
    return False, ""


def is_manifest_error_disabled(manifest: dict, max_consecutive: int = 3) -> bool:
    return _manifest_error_disabled(manifest, max_consecutive)[0]


def _manifest_is_error_disabled(manifest: dict, now: float | None = None) -> tuple[bool, str]:
    return _manifest_error_disabled(manifest)


def _is_restart_budget_exceeded(manifest: dict, now: float) -> tuple[bool, str]:
    return _manifest_restart_budget_exceeded(manifest, now)


def is_restart_budget_exceeded(manifest: dict, now: float) -> bool:
    return _manifest_restart_budget_exceeded(manifest, now)[0]


def _is_error_disabled(manifest: dict, now: float | None = None) -> tuple[bool, str]:
    return _manifest_error_disabled(manifest)


def is_error_disabled(manifest: dict, now: float | None = None) -> bool:
    return _manifest_error_disabled(manifest)[0]


def _check_restart_budget(manifest: dict, now: float) -> tuple[bool, str]:
    return _manifest_restart_budget_exceeded(manifest, now)


def _check_error_disabled(manifest: dict, now: float | None = None) -> tuple[bool, str]:
    return _manifest_error_disabled(manifest)


def _manifest_restart_record(name: str, now: float) -> None:
    with _state_write_lock(name):
        state = _read_state_file(name)
        timestamps = state.get("restart_timestamps", [])
        if not isinstance(timestamps, list):
            timestamps = []
        timestamps = [float(t) for t in timestamps if isinstance(t, (int, float))]
        timestamps.append(now)
        state["restart_timestamps"] = timestamps
        state["restart_count"] = len([t for t in timestamps if (now - t) < 3600])
        state["last_update"] = now
        state["last_restart"] = now
        _write_state_file(name, state)


def _escalate_failure(name: str, bot: BotState, exit_code: int | None, bots: dict[str, BotState] | None = None) -> None:
    n = bot.consecutive_errors
    if n == 1:
        logger.info(f"Escalation L1 for '{name}': retry same model on next tick")
    elif n == 2:
        if bot.config.fallback_model and bot.config.model != bot.config.fallback_model:
            old = bot.config.model
            bot.config.model = bot.config.fallback_model
            logger.info(f"Escalation L2 for '{name}': switching model {old} -> {bot.config.model}")
        else:
            logger.info(f"Escalation L2 for '{name}': no fallback model, retry same")
    elif n == 3:
        logger.info(f"Escalation L3 for '{name}': requesting task split via decomposer")
        _request_task_split(name)


def _request_task_split(bot_name: str) -> None:
    try:
        split_req = STATE_DIR / "split_requests.json"
        existing: list = []
        if split_req.exists():
            existing = json.loads(split_req.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        existing.append({"bot": bot_name, "requested_at": time.time(), "status": "pending"})
        tmp = split_req.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing[-50:], indent=2), encoding="utf-8")
        tmp.replace(split_req)
    except Exception:
        pass


def _queue_human_review(name: str, bot: BotState, exit_code: int | None) -> None:
    try:
        hq = STATE_DIR / "human_review_queue.json"
        existing: list = []
        if hq.exists():
            existing = json.loads(hq.read_text(encoding="utf-8"))
            if not isinstance(existing, list):
                existing = []
        ck = read_checkpoint(name)
        existing.append({
            "bot": name,
            "exit_code": exit_code,
            "model": bot.config.model,
            "consecutive_errors": bot.consecutive_errors,
            "checkpoint": ck,
            "queued_at": time.time(),
        })
        tmp = hq.with_suffix(".tmp")
        tmp.write_text(json.dumps(existing[-50:], indent=2), encoding="utf-8")
        tmp.replace(hq)
        logger.warning(f"Human review queued for '{name}' after {bot.consecutive_errors} failures")
    except Exception:
        pass


def _manifest_error_record(name: str, is_error: bool) -> None:
    with _state_write_lock(name):
        state = _read_state_file(name)
        if is_error:
            state["consecutive_errors"] = int(state.get("consecutive_errors", 0)) + 1
        else:
            state["consecutive_errors"] = 0
        state["last_update"] = time.time()
        if state["consecutive_errors"] >= 3:
            state["status"] = "error-disabled"
        _write_state_file(name, state)


def _started_at_for(bot_name: str) -> float | None:
    """Best-effort spawn time from state file; None if unavailable."""
    p = STATE_DIR / f"{bot_name}.state.json"
    try:
        if p.exists():
            j = json.loads(p.read_text())
            v = j.get("started")
            if isinstance(v, (int, float)) and v > 0:
                return float(v)
    except Exception:
        pass
    return None


def _write_alignment_event(
    bot_name: str,
    exit_code: int | None,
    exit_reason: str,
    started_at: float | None = None,
) -> None:
    """Write state/alignment_events/<bot>.exit.json atomically (fail-open).

    Called immediately after detecting a bot exit so ALIGNMENT_BOT can score
    the run and produce an RL reward. Never raises — logs at WARNING on I/O
    error so the health loop stays fail-open.
    """
    try:
        ALIGNMENT_EVENTS_DIR.mkdir(parents=True, exist_ok=True)
        now = time.time()
        started = started_at if started_at is not None else _started_at_for(bot_name)
        run_duration = (now - started) if started else None
        hb_age = None
        try:
            hb_raw = read_heartbeat(bot_name)
            if hb_raw:
                hb_age = max(0.0, now - hb_raw)
        except Exception:
            pass
        log_bytes = None
        try:
            lp = log_path(bot_name)
            if lp.exists():
                log_bytes = lp.stat().st_size
        except Exception:
            pass
        payload: dict = {
            "bot": bot_name,
            "exit_code": exit_code,
            "exit_reason": exit_reason,
            "exit_time": now,
            "exit_time_human": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "run_duration": round(run_duration, 2) if run_duration is not None else None,
            "started_at": started,
            "log_path": f"logs/{bot_name}.log",
            "stream_path": f"logs/{bot_name}.stream.json",
            "checkpoint_path": f"state/{bot_name}.checkpoint.json",
            "heartbeat_age_at_exit": round(hb_age, 1) if hb_age is not None else None,
            "log_bytes_at_exit": log_bytes,
            "processed": False,
            "processed_at": None,
            "version": 1,
        }
        target = ALIGNMENT_EVENTS_DIR / f"{bot_name}.exit.json"
        tmp = Path(str(target) + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2))
        tmp.replace(target)
        logger.info(
            f"Alignment event: {bot_name} exit_code={exit_code} reason={exit_reason} duration={run_duration}"
        )
    except Exception as e:
        logger.warning(f"Failed to write alignment event for {bot_name}: {e}")


def _collect_reviewer_feedback_for_trigger(bot_name: str) -> list[dict]:
    feedback = []
    try:
        from codebot.ticket_engine import TicketStore
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if not store_path.exists():
            return feedback
        ts = TicketStore(store_path)
        for ticket in ts._tickets.values():
            if ticket.assigned_agent == bot_name and ticket.rework_count >= 3 and ticket.reviewer_feedback:
                for fb in ticket.reviewer_feedback:
                    feedback.append({
                        "ticket_id": ticket.id,
                        "ticket_title": ticket.title,
                        "rework_count": ticket.rework_count,
                        "reviewer": fb.get("reviewer", ""),
                        "file": fb.get("file", ""),
                        "description": fb.get("description", ""),
                        "recommendation": fb.get("recommendation", ""),
                    })
    except Exception as e:
        logger.debug(f"Failed to collect reviewer feedback for {bot_name}: {e}")
    return feedback


def _run_alignment_pipeline(bot_name: str, timeout: int = 120) -> bool:
    """Run alignment scoring + prompt optimization synchronously for a bot.

    Delegates to alignment_service to maintain architectural boundaries.
    """
    try:
        from codebot.alignment_service import run_alignment_pipeline as _rap
    except ImportError:
        try:
            from alignment_service import run_alignment_pipeline as _rap
        except ImportError:
            logger.warning("alignment_service not available, skipping alignment pipeline")
            return False

    return _rap(bot_name, timeout=timeout)


def _run_alignment_pipeline_for_all() -> None:
    """Run alignment scoring for all pending events (called every 30 minutes).
    
    Delegates to alignment_service to maintain architectural boundaries.
    """
    try:
        from codebot.alignment_service import run_alignment_pipeline_for_all as _rapfa
    except ImportError:
        try:
            from alignment_service import run_alignment_pipeline_for_all as _rapfa
        except ImportError:
            logger.warning("alignment_service not available, skipping alignment sweep")
            return

    _rapfa()
    
    # Collect metrics after alignment sweep
    try:
        import subprocess
        subprocess.run(
            ["python3", str(BOTS_DIR / "scripts" / "collect_metrics.py")],
            timeout=30,
            capture_output=True
        )
        logger.info("Metrics collected after alignment sweep")
    except Exception as e:
        logger.warning(f"Metrics collection failed: {e}")


def write_checkpoint_handoff(bot_name: str, payload: dict) -> None:
    p = checkpoint_path(bot_name)
    payload = dict(payload)
    payload.setdefault("bot", bot_name)
    payload.setdefault("updated_at", time.time())
    payload.setdefault("updated_at_human", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(payload["updated_at"])))
    _write_json_atomic(p, payload)


def init_checkpoint(bot_name: str, scan_iteration: int = 0) -> None:
    p = checkpoint_path(bot_name)
    if p.exists():
        return
    write_checkpoint_handoff(bot_name, {
        "scan_iteration": scan_iteration,
        "current_task": None,
        "completed": [],
        "queue": [],
        "findings_so_far": [],
        "reason": "init",
    })


def _verify_orchestrator() -> bool:
    try:
        import ast as _ast
        _ast.parse((BOTS_DIR / "orchestrator.py").read_text())
        return True
    except SyntaxError as e:
        logger.error(f"orchestrator.py syntax error: {e}")
        return False


def rotate_logs(max_bytes: int = 10_000_000, keep: int = 1) -> None:
    """Archive oversized active logs and retain a bounded archive count."""
    try:
        for lf in LOGS_DIR.glob("*.log"):
            try:
                if lf.stat().st_size > max_bytes:
                    for index in range(max(keep - 1, 0), 0, -1):
                        archive = lf.with_name(f"{lf.name}.{index}")
                        if archive.exists():
                            archive.replace(lf.with_name(f"{lf.name}.{index + 1}"))
                    if keep > 0:
                        lf.replace(lf.with_name(f"{lf.name}.1"))
                    else:
                        lf.unlink()
                    lf.write_bytes(b"")
                    logger.info(f"Rotated log {lf.name} (exceeded {max_bytes} bytes)")
            except OSError as e:
                logger.warning(f"Failed to rotate log {lf}: {e}")
    except Exception as e:
        logger.warning(f"rotate_logs check failed: {e}")


# ---------------------------------------------------------------------------
# Health Monitor
# ---------------------------------------------------------------------------


def _parse_queue_complexity(queue_path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    if not queue_path.exists():
        return result
    try:
        content = queue_path.read_text(encoding="utf-8")
    except OSError:
        return result
    in_table = False
    for line in content.splitlines():
        if not line.startswith("|"):
            continue
        if line.startswith("|---") or line.startswith("| -"):
            continue
        parts = [p.strip() for p in line.split("|")[1:-1]]
        while parts and parts[-1] == "":
            parts.pop()
        if not in_table:
            if len(parts) >= 7 and parts[0].lower() == "id" and parts[1].lower() == "source":
                in_table = True
            continue
        if not parts or not parts[0].startswith("Q-"):
            continue
        item_id = parts[0]
        known_status = {"confirmed", "approved", "implemented", "wontfix", "deferred", "queued", "in_progress", "blocked", "false-positive"}
        status_idx = None
        for idx, val in enumerate(parts):
            if val.lower() in known_status:
                status_idx = idx
                break
        if status_idx is None or status_idx == 0:
            continue
        status = parts[status_idx].lower()
        if status not in ("confirmed", "approved"):
            continue
        comp_idx = status_idx - 1
        if comp_idx < len(parts):
            complexity = parts[comp_idx].lower()
            if complexity == "":
                complexity = "medium"
            elif complexity in ("trivial", "small"):
                pass
            elif complexity == "low":
                complexity = "small"
            elif complexity == "medium":
                complexity = "medium"
            elif complexity == "high":
                complexity = "high"
            else:
                complexity = "medium"
        else:
            complexity = "medium"
        result[item_id] = complexity
    return result


def now_gate_logged(bot: BotState) -> bool:
    if time.time() - bot.last_throttle_log > 600:
        bot.last_throttle_log = time.time()
        return True
    return False


def _dynamic_scale_bots(bots: dict[str, BotState]) -> None:
    """Enable or suppress bot spawns based on current ticket queue state.

    Called every health-check tick before the spawn loop. Reads READY,
    REVIEWING, VERIFYING, REWORK, and DISCOVERED counts from TicketStore
    and adjusts bot.config.enabled so interval-based spawns only fire
    when there is actual work for that role category.

    Rules:
    - Implementers suppressed when READY + REWORK == 0
    - Reviewers suppressed when REVIEWING == 0
    - Discovery suppressed when DISCOVERED > 50 (backlog too deep) or READY > backlog_high
    - Control roles (scheduler, quality_gate, etc.) always allowed
    - Running bots are never killed; suppression only prevents new spawns
    """
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return

    try:
        ts = TicketStore(store_path)
        ready_count = len(ts.list_by_state(TicketState.READY))
        rework_count = len(ts.list_by_state(TicketState.REWORK))
        reviewing_count = len(ts.list_by_state(TicketState.REVIEWING))
        verifying_count = len(ts.list_by_state(TicketState.VERIFYING))
        discovered_count = len(ts.list_by_state(TicketState.DISCOVERED))
    except Exception:
        return

    actionable = ready_count + rework_count
    backlog_high = 80

    for name, bot in bots.items():
        base_role = name.split("-")[0] if "-" in name else name

        if base_role in IMPLEMENTER_ROLE_NAMES:
            if actionable == 0 and bot.process is None:
                if bot.config.enabled:
                    bot.config.enabled = False
                    logger.info(f"[queue-scale] Suppressed '{name}': no actionable tickets (ready={ready_count}, rework={rework_count})")
            elif actionable > 0 and not bot.config.enabled:
                bot.config.enabled = True
                logger.info(f"[queue-scale] Enabled '{name}': {actionable} actionable tickets available")

        elif base_role in REVIEWER_ROLE_NAMES:
            if reviewing_count == 0 and bot.process is None:
                if bot.config.enabled:
                    bot.config.enabled = False
                    logger.info(f"[queue-scale] Suppressed '{name}': no tickets in REVIEWING")
            elif reviewing_count > 0 and not bot.config.enabled:
                bot.config.enabled = True
                logger.info(f"[queue-scale] Enabled '{name}': {reviewing_count} tickets awaiting review")

        elif base_role in DISCOVERY_ROLE_NAMES | (PLANNING_ROLE_NAMES - {"implementation_planner"}):
            if discovered_count > 50 and bot.process is None:
                if bot.config.enabled:
                    bot.config.enabled = False
                    logger.info(f"[queue-scale] Suppressed '{name}': discovery backlog too deep ({discovered_count})")
            elif ready_count > backlog_high and bot.process is None:
                if bot.config.enabled:
                    bot.config.enabled = False
                    logger.info(f"[queue-scale] Suppressed '{name}': ready backlog full ({ready_count} > {backlog_high})")
            elif discovered_count <= 50 and ready_count <= backlog_high and not bot.config.enabled:
                bot.config.enabled = True
        elif base_role == "implementation_planner" or base_role.startswith("implementation_planner-"):
            planning_count = len(ts.list_by_state(TicketState.PLANNING)) if hasattr(ts, 'list_by_state') else 0
            if planning_count > 0 and not bot.config.enabled:
                bot.config.enabled = True
                logger.info(f"[queue-scale] Enabled '{name}': {planning_count} tickets need plans")
            elif planning_count == 0 and bot.config.enabled and bot.process is None:
                bot.config.enabled = False
                logger.info(f"[queue-scale] Suppressed '{name}': no tickets in PLANNING")


def _recover_stuck_implementing_tickets(bots: dict[str, BotState]) -> int:
    """Sweep IMPLEMENTING tickets whose workers have exited and advance them.

    Catches tickets stuck in IMPLEMENTING because the worker exited with a
    non-zero code (rate limit, error) before the completion handler could
    transition them. Also releases stale claims blocking dispatch.
    """
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0

    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0

    implementing = ts.list_by_state(TicketState.IMPLEMENTING)
    if not implementing:
        return 0

    active_ticket_ids = set()
    for name, bot in bots.items():
        base_role = name.split("-")[0] if "-" in name else name
        if base_role in IMPLEMENTER_ROLE_NAMES:
            if bot.process is not None and bot.process.poll() is None:
                tid = getattr(bot, '_assigned_ticket_id', '')
                if tid:
                    active_ticket_ids.add(tid)

    claims_dir = STATE_DIR / "claims"
    recovered = 0
    for ticket in implementing:
        tid = getattr(ticket, 'id', '')
        if not tid or tid in active_ticket_ids:
            continue

        has_active_claim = False
        if claims_dir.exists():
            for cf in claims_dir.glob(f"{tid}.*.json"):
                try:
                    data = json.loads(cf.read_text(encoding="utf-8"))
                    worker = data.get("bot", "")
                    bot = bots.get(worker)
                    if bot and bot.process is not None and bot.process.poll() is None:
                        has_active_claim = True
                        break
                except Exception:
                    pass

        if has_active_claim:
            continue

        try:
            ts.transition(tid, TicketState.REWORK)
            logger.info(f"Recovered stuck ticket {tid}: IMPLEMENTING -> REWORK (worker exited without completing)")
            recovered += 1
        except ValueError:
            try:
                ts.transition(tid, TicketState.READY)
                logger.info(f"Recovered stuck ticket {tid}: IMPLEMENTING -> READY (transition to REWORK failed)")
                recovered += 1
            except ValueError as e:
                logger.warning(f"Cannot recover ticket {tid}: {e}")

        if claims_dir.exists():
            for cf in claims_dir.glob(f"{tid}.*.json"):
                try:
                    cf.unlink()
                except OSError:
                    pass

        for name, bot in bots.items():
            if getattr(bot, '_assigned_ticket_id', '') == tid:
                bot._assigned_ticket_id = ''

    return recovered


def _route_ready_tickets() -> int:
    """Route READY tickets: decomposer sub-tickets to PLANNING, originals to DECOMPOSE."""
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0

    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0

    ready = ts.list_by_state(TicketState.READY)
    routed = 0

    for ticket in ready:
        tid = getattr(ticket, 'id', '')
        if not tid:
            continue
        source = getattr(ticket, 'source', '')
        try:
            if source == 'decomposer':
                ts.transition(tid, TicketState.PLANNING)
                logger.info(f"Routed {tid} READY -> PLANNING (decomposer sub-ticket)")
            else:
                ts.transition(tid, TicketState.DECOMPOSE)
                logger.info(f"Routed {tid} READY -> DECOMPOSE")
            routed += 1
        except ValueError as e:
            logger.debug(f"Failed to route {tid}: {e}")

    return routed


DECOMPOSER_ROLE_NAMES: frozenset[str] = frozenset({
    "decomposer",
})


def _is_decomposer_role(name: str) -> bool:
    base = name.split("-")[0] if "-" in name else name
    return base in DECOMPOSER_ROLE_NAMES


def _dispatch_decompose_agents(bots: dict[str, BotState], max_agents: int = 0) -> int:
    """Dispatch DECOMPOSE tickets to decomposer agents, advance to PLANNING when done.

    For each DECOMPOSE ticket without an active claim: find an idle decomposer bot,
    create a claim, and start/restart it. For tickets whose decomposition artifact
    already exists: transition DECOMPOSE -> PLANNING.
    """
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0

    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0

    decomposing = ts.list_by_state(TicketState.DECOMPOSE)
    if not decomposing:
        return 0

    decomp_dir = STATE_DIR / "decompositions"
    decomp_dir.mkdir(parents=True, exist_ok=True)

    claims_dir = STATE_DIR / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)

    live_bots: dict[str, str] = {}
    for name, bot in bots.items():
        if bot.process is not None and bot.process.poll() is None:
            assigned = getattr(bot, '_assigned_ticket_id', '')
            if assigned:
                live_bots[name] = assigned

    now = time.time()
    claim_grace_seconds = 60
    for p in claims_dir.glob("*.json"):
        parts = p.stem.rsplit(".", 1)
        if len(parts) != 2:
            continue
        claimed_tid, claimed_bot = parts
        if claimed_bot not in live_bots or live_bots[claimed_bot] != claimed_tid:
            try:
                age = now - p.stat().st_mtime
                if age < claim_grace_seconds:
                    continue
                p.unlink()
            except OSError:
                pass

    active_claims: set[str] = set()
    for p in claims_dir.glob("*.json"):
        active_claims.add(p.stem.rsplit(".", 1)[0])

    idle_decomposers = []
    unassigned_running = []
    busy_ticket_ids: set[str] = set()
    for name, bot in bots.items():
        base_name = name.split("-")[0] if "-" in name else name
        if not _is_decomposer_role(name):
            continue
        if bot.process is not None and bot.process.poll() is None:
            assigned = getattr(bot, '_assigned_ticket_id', '')
            if assigned:
                busy_ticket_ids.add(assigned)
            else:
                unassigned_running.append((name, bot))
        else:
            idle_decomposers.append((name, bot))

    available = idle_decomposers + unassigned_running
    if max_agents > 0:
        available = available[:max_agents]
    dispatched = 0

    for ticket in decomposing:
        tid = getattr(ticket, 'id', '')
        if not tid:
            continue

        decomp_file = decomp_dir / f"{tid}.decomp.json"
        if decomp_file.exists():
            try:
                artifact = json.loads(decomp_file.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                artifact = {}
            try:
                ts.transition(tid, TicketState.PLANNING)
                logger.info(f"Decomposition complete: {tid} DECOMPOSE -> PLANNING (sub_tickets={len(artifact.get('sub_tickets', []))})")
                dispatched += 1
                for cf in claims_dir.glob(f"{tid}.*.json"):
                    try:
                        cf.unlink()
                    except OSError:
                        pass
            except ValueError as e:
                logger.debug(f"Failed to advance {tid} to PLANNING: {e}")
            continue

        if tid in active_claims:
            continue

        if tid in busy_ticket_ids:
            continue

        if not available:
            break

        bot_name, bot = available.pop(0)

        existing_claim = claims_dir / f"{tid}.{bot_name}.json"
        if existing_claim.exists():
            continue

        claim_file = claims_dir / f"{tid}.{bot_name}.json"
        try:
            claim_data = {"ticket_id": tid, "bot": bot_name, "at": time.time(), "class": "decompose"}
            tmp = claim_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(claim_data), encoding="utf-8")
            tmp.replace(claim_file)
        except OSError:
            continue

        bot._assigned_ticket_id = tid
        dispatched += 1
        logger.info(f"Dispatched decomposition for {tid} -> {bot_name}")
        if bot.process is not None and bot.process.poll() is None:
            pass
        else:
            start_bot(bot, bots=bots)

    for bot_name, bot in bots.items():
        if not _is_decomposer_role(bot_name):
            continue
        assigned = getattr(bot, '_assigned_ticket_id', '')
        if assigned and bot.process is None:
            alive = False
            for p in claims_dir.glob(f"{assigned}.{bot_name}.json"):
                try:
                    age = time.time() - p.stat().st_mtime
                    if age < 120:
                        alive = True
                        break
                except OSError:
                    pass
            if alive:
                start_bot(bot, bots=bots)

    return dispatched


def _dispatch_planning_agents(bots: dict[str, BotState], max_agents: int = 0) -> int:
    """Dispatch PLANNING tickets to planner agents, advance to IMPLEMENTING when done.

    For each PLANNING ticket without an active claim: find an idle planner bot,
    create a claim, and start/restart it. For tickets whose plan file already
    exists (planner finished): transition PLANNING -> IMPLEMENTING.
    """
    try:
        from codebot.ticket_engine import TicketStore, TicketState
    except ImportError:
        return 0

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0

    try:
        ts = TicketStore(store_path)
    except Exception:
        return 0

    planning = ts.list_by_state(TicketState.PLANNING)
    if not planning:
        return 0

    plans_dir = STATE_DIR / "plans"
    plans_dir.mkdir(parents=True, exist_ok=True)

    claims_dir = STATE_DIR / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)

    live_bots: dict[str, str] = {}
    for name, bot in bots.items():
        if bot.process is not None and bot.process.poll() is None:
            assigned = getattr(bot, '_assigned_ticket_id', '')
            if assigned:
                live_bots[name] = assigned

    now = time.time()
    claim_grace_seconds = 60
    for p in claims_dir.glob("*.json"):
        parts = p.stem.rsplit(".", 1)
        if len(parts) != 2:
            continue
        claimed_tid, claimed_bot = parts
        if claimed_bot not in live_bots or live_bots[claimed_bot] != claimed_tid:
            try:
                age = now - p.stat().st_mtime
                if age < claim_grace_seconds:
                    continue
                p.unlink()
            except OSError:
                pass

    active_claims: set[str] = set()
    for p in claims_dir.glob("*.json"):
        active_claims.add(p.stem.rsplit(".", 1)[0])

    idle_planners = []
    unassigned_running = []
    busy_ticket_ids: set[str] = set()
    for name, bot in bots.items():
        base_name = name.split("-")[0] if "-" in name else name
        if base_name not in PLANNING_ROLE_NAMES:
            continue
        if bot.process is not None and bot.process.poll() is None:
            assigned = getattr(bot, '_assigned_ticket_id', '')
            if assigned:
                busy_ticket_ids.add(assigned)
            else:
                unassigned_running.append((name, bot))
        else:
            idle_planners.append((name, bot))

    available = idle_planners + unassigned_running
    if max_agents > 0:
        available = available[:max_agents]
    dispatched = 0

    for ticket in planning:
        tid = getattr(ticket, 'id', '')
        if not tid:
            continue

        plan_file = plans_dir / f"{tid}.plan.json"
        if plan_file.exists():
            try:
                ts.transition(tid, TicketState.IMPLEMENTING)
                logger.info(f"Planning complete: {tid} PLANNING -> IMPLEMENTING")
                dispatched += 1
                for cf in claims_dir.glob(f"{tid}.*.json"):
                    try:
                        cf.unlink()
                    except OSError:
                        pass
            except ValueError as e:
                logger.debug(f"Failed to advance {tid} to IMPLEMENTING: {e}")
            continue

        if tid in active_claims:
            continue

        if tid in busy_ticket_ids:
            continue

        if not available:
            break

        bot_name, bot = available.pop(0)

        existing_claim = claims_dir / f"{tid}.{bot_name}.json"
        if existing_claim.exists():
            continue

        claim_file = claims_dir / f"{tid}.{bot_name}.json"
        try:
            claim_data = {"ticket_id": tid, "bot": bot_name, "at": time.time(), "class": "planning"}
            tmp = claim_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(claim_data), encoding="utf-8")
            tmp.replace(claim_file)
        except OSError:
            continue

        bot._assigned_ticket_id = tid
        dispatched += 1
        logger.info(f"Dispatched planning for {tid} -> {bot_name}")
        if bot.process is not None and bot.process.poll() is None:
            pass
        else:
            start_bot(bot, bots=bots)

    for bot_name, bot in bots.items():
        base = bot_name.split("-")[0] if "-" in bot_name else bot_name
        if base not in PLANNING_ROLE_NAMES:
            continue
        assigned = getattr(bot, '_assigned_ticket_id', '')
        if assigned and bot.process is None:
            alive = False
            for p in claims_dir.glob(f"{assigned}.{bot_name}.json"):
                try:
                    age = time.time() - p.stat().st_mtime
                    if age < 120:
                        alive = True
                        break
                except OSError:
                    pass
            if alive:
                start_bot(bot, bots=bots)

    return dispatched


def _process_rework_tickets(bots: dict[str, BotState]) -> int:
    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0
    try:
        from codebot.ticket_engine import TicketStore, TicketState
        ts = TicketStore(store_path)
        rework = ts.list_by_state(TicketState.REWORK)
    except Exception:
        return 0
    if not rework:
        return 0
    plans_dir = STATE_DIR / "plans"
    advanced = 0
    for ticket in rework:
        tid = ticket.id
        rework_count = getattr(ticket, 'rework_count', 0)
        if rework_count >= 3:
            try:
                ts.transition(tid, TicketState.DECOMPOSE)
                logger.warning(f"Rework ticket {tid} -> DECOMPOSE (failed {rework_count} implementations, needs fresh decomposition)")
                advanced += 1
            except ValueError as e:
                try:
                    ts.transition(tid, TicketState.REJECTED)
                    logger.warning(f"Rework ticket {tid} -> REJECTED (decompose transition failed: {e})")
                    advanced += 1
                except ValueError:
                    pass
            continue
        plan_file = plans_dir / f"{tid}.plan.json"
        target_state = TicketState.IMPLEMENTING if plan_file.exists() else TicketState.PLANNING
        try:
            ts.transition(tid, target_state)
            logger.info(f"Rework ticket {tid} -> {target_state.value} (rework_count={rework_count})")
            advanced += 1
        except ValueError as e:
            logger.warning(f"Rework ticket {tid} transition failed: {e}")
    return advanced


def _recover_deferred_tickets() -> int:
    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return 0
    try:
        from codebot.ticket_engine import TicketStore, TicketState
        ts = TicketStore(store_path)
        deferred = ts.list_by_state(TicketState.DEFERRED)
        decompose_count = len(ts.list_by_state(TicketState.DECOMPOSE))
    except Exception:
        return 0
    if not deferred:
        return 0
    recovered = 0
    for ticket in deferred:
        try:
            if decompose_count == 0:
                ts.transition(ticket.id, TicketState.DECOMPOSE)
                logger.info(f"Recovered deferred ticket {ticket.id} -> DECOMPOSE (queue cleared)")
            else:
                ts.transition(ticket.id, TicketState.READY)
                logger.info(f"Recovered deferred ticket {ticket.id} -> READY")
            recovered += 1
        except ValueError as e:
            logger.warning(f"Deferred ticket {ticket.id} recovery failed: {e}")
    return recovered


def _get_pipeline_state() -> dict[str, int]:
    try:
        from codebot.ticket_engine import TicketStore, TicketState
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            logger.info("_get_pipeline_state: tickets.json not found at %s", store_path)
            return {}
        store = TicketStore(store_path)
        counts: dict[str, int] = {}
        for state in TicketState:
            tickets = store.list_by_state(state)
            if tickets:
                counts[state.value] = len(tickets)
        logger.info("_get_pipeline_state: %s", counts)
        return counts
    except Exception as e:
        logger.info("_get_pipeline_state failed: %s %s", type(e).__name__, e)
        return {}


def _compute_dynamic_priority(pipeline: dict[str, int]) -> dict[str, int]:
    ready = pipeline.get("READY", 0)
    decompose = pipeline.get("DECOMPOSE", 0)
    planning = pipeline.get("PLANNING", 0)
    implementing = pipeline.get("IMPLEMENTING", 0)
    reviewing = pipeline.get("REVIEWING", 0)
    verifying = pipeline.get("VERIFYING", 0)
    discovered = pipeline.get("DISCOVERED", 0)
    triaged = pipeline.get("TRIAGED", 0)
    complete = pipeline.get("COMPLETE", 0)
    total_non_complete = ready + decompose + planning + implementing + reviewing + verifying + discovered + triaged

    priorities: dict[str, int] = {}

    if total_non_complete == 0:
        discovery_roles = {
            "bug_hunter", "security_auditor", "architecture_auditor",
            "performance_auditor", "test_gap_auditor", "documentation_auditor",
            "dependency_auditor", "ux_auditor", "feature_hunter",
        }
        for r in discovery_roles:
            priorities[r] = 10
        for r in {"general_implementer", "backend_implementer", "frontend_implementer",
                   "test_implementer", "migration_implementer", "documentation_implementer"}:
            priorities[r] = 50
        for r in {"implementation_planner", "implementation_planner-2",
                   "implementation_planner-3", "implementation_planner-4"}:
            priorities[r] = 50
        return priorities

    if ready >= 50:
        for r in {"implementation_planner", "implementation_planner-2",
                   "implementation_planner-3", "implementation_planner-4"}:
            priorities[r] = 10
    elif ready >= 20:
        for r in {"implementation_planner", "implementation_planner-2",
                   "implementation_planner-3", "implementation_planner-4"}:
            priorities[r] = 15

    if implementing >= 10 or planning > 0:
        for r in {"general_implementer", "general_implementer-2", "general_implementer-3",
                   "general_implementer-4", "backend_implementer", "backend_implementer-2",
                   "frontend_implementer", "test_implementer", "migration_implementer",
                   "documentation_implementer"}:
            priorities[r] = 10

    if reviewing >= 5 or implementing >= 15:
        for r in {"correctness_reviewer", "security_reviewer", "architecture_reviewer",
                   "test_reviewer", "performance_reviewer", "simplicity_reviewer",
                   "documentation_reviewer"}:
            priorities[r] = 10

    if verifying >= 3:
        priorities["scheduler"] = 10

    if discovered >= 10 or triaged >= 10:
        discovery_roles = {
            "bug_hunter", "security_auditor", "architecture_auditor",
            "performance_auditor", "test_gap_auditor", "documentation_auditor",
            "dependency_auditor", "ux_auditor", "feature_hunter",
        }
        for r in discovery_roles:
            priorities[r] = 25

    return priorities


def _is_needed_bot(name: str, pipeline: dict[str, int]) -> bool:
    ready = pipeline.get("READY", 0)
    decompose = pipeline.get("DECOMPOSE", 0)
    planning = pipeline.get("PLANNING", 0)
    implementing = pipeline.get("IMPLEMENTING", 0)
    reviewing = pipeline.get("REVIEWING", 0)
    verifying = pipeline.get("VERIFYING", 0)

    always_on = {"scheduler", "conflict_resolver"}
    if name in always_on:
        return True
    if name == "decomposer" or name.startswith("decomposer-"):
        return decompose > 0 or ready > 0
    if name == "implementation_planner" or name.startswith("implementation_planner-"):
        return planning > 0
    base_name = name.split("-")[0] if "-" in name else name
    if base_name in IMPLEMENTER_ROLE_NAMES:
        return implementing > 0
    if base_name in REVIEWER_ROLE_NAMES or name == "ux_reviewer":
        return reviewing > 0
    if name == "quality_gate":
        return verifying > 0
    if name == "ticket_triager":
        discovered = pipeline.get("DISCOVERED", 0) + pipeline.get("VALIDATING", 0) + pipeline.get("TRIAGED", 0)
        return discovered > 0
    return False


def _apply_agent_availability(bots: dict[str, BotState]) -> None:
    pipeline = _get_pipeline_state()
    ready = pipeline.get("READY", 0)
    decompose = pipeline.get("DECOMPOSE", 0)
    planning = pipeline.get("PLANNING", 0)
    implementing = pipeline.get("IMPLEMENTING", 0)
    reviewing = pipeline.get("REVIEWING", 0)

    always_on = {"scheduler", "conflict_resolver"}

    for name, bot in bots.items():
        base_name = name.split("-")[0] if "-" in name else name

        if base_name in always_on:
            continue

        if base_name == "decomposer":
            should_enable = decompose > 0 or ready > 0
        elif base_name == "implementation_planner":
            should_enable = planning > 0
        elif base_name in IMPLEMENTER_ROLE_NAMES:
            should_enable = implementing > 0
        elif base_name in REVIEWER_ROLE_NAMES or base_name == "ux_reviewer":
            should_enable = reviewing > 0
        elif base_name in DISCOVERY_ROLE_NAMES:
            should_enable = False
        else:
            continue

        if not should_enable and bot.config.enabled:
            bot.config.enabled = False
            if bot.process is not None and bot.process.poll() is None:
                try:
                    bot.process.terminate()
                    bot.process.wait(timeout=5)
                except Exception:
                    try:
                        bot.process.kill()
                    except Exception:
                        pass
                bot.process = None
            write_heartbeat(bot.config.name)
            update_bot_state(bot, "disabled")
            status_path = STATE_DIR / f"{bot.config.name}.status.json"
            try:
                sdata = json.loads(status_path.read_text()) if status_path.exists() else {}
                sdata["current_task"] = "waiting"
                sdata["task_description"] = "pipeline idle — no tickets to process"
                _write_json_atomic(status_path, sdata)
            except Exception:
                pass
        elif should_enable and not bot.config.enabled:
            bot.config.enabled = True
            logger.info(f"[queue-scale] Re-enabled '{name}': work available in queue")


def due_bots_first(bots: dict[str, BotState]) -> list[str]:
    now = time.time()
    due = [n for n, b in bots.items()
           if b.config.enabled and b.process is None
           and b.next_run_at and now >= b.next_run_at]

    pipeline = _get_pipeline_state()
    dynamic = _compute_dynamic_priority(pipeline)

    def sort_key(name: str) -> tuple[int, float]:
        dyn = dynamic.get(name)
        if dyn is not None:
            return (dyn, bots[name].next_run_at)
        return (TIER_PRIORITY.get(name, 2), bots[name].next_run_at)

    due.sort(key=sort_key)
    rest = [n for n in bots if n not in due]
    return due + rest

# ---------------------------------------------------------------------------
# Manifest-driven scheduler helpers (USE_MANIFEST_SCHEDULER — flag OFF: legacy untouched)
# ---------------------------------------------------------------------------

def _manifest_resolve_file(rel_path: str) -> Path:
    """Resolve a manifest-relative path via BOTS_DIR, never hardcoded absolute."""
    # Why: manifests store cwd-relative literals; orchestrator may run from
    # Work or Work/bots. Probe both bases so mtimes/noop counters resolve.
    p = Path(rel_path)
    if p.is_absolute():
        return p
    cand1 = BOTS_DIR / rel_path
    if cand1.exists():
        return cand1
    cand2 = BOTS_DIR / rel_path
    if cand2.exists():
        return cand2
    # Fallback to BOTS_DIR-relative
    return cand1


def _manifest_load_queue_text() -> str:
    """Load queue text for readiness checks, filtering unapproved T4+ items.

    WIRE-02: When ticket_engine is available, loads tickets from the normalized
    TicketStore instead of parsing QUEUE.md markdown. Falls back to QUEUE.md
    parsing when ticket_engine is unavailable or no ticket store exists yet.
    """
    try:
        from codebot.ticket_engine import TicketStore, TicketState
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if store_path.exists():
            ts = TicketStore(store_path)
            ready = ts.list_ready()
            if ready:
                lines = []
                for t in ready:
                    tid = getattr(t, 'id', None) or getattr(t, 'ticket_id', str(t))
                    title = getattr(t, 'title', str(t))
                    severity = getattr(t, 'severity', None)
                    sev_val = severity.value if hasattr(severity, 'value') else str(severity) if severity else "medium"
                    state = getattr(t, 'state', None)
                    state_val = state.value if hasattr(state, 'value') else str(state) if state else "READY"
                    ac = getattr(t, 'acceptance_criteria', []) or []
                    lines.append(f"### [{tid}] {title}")
                    lines.append(f"   Complexity: {sev_val}")
                    lines.append(f"   Status: {state_val}")
                    if ac:
                        lines.append(f"   Acceptance: {'; '.join(ac[:3])}")
                    lines.append("")
                return "\n".join(lines)
    except ImportError:
        pass
    except Exception:
        pass

    candidates = [
        BOTS_DIR / "docs" / "triage" / "QUEUE.md",
        BOTS_DIR / "QUEUE.md",
        Path("docs/triage/QUEUE.md"),
    ]
    global _QUEUE_SNAPSHOT
    raw = ""
    for qp in candidates:
        try:
            if qp.exists():
                mtime = qp.stat().st_mtime
                if _QUEUE_SNAPSHOT and _QUEUE_SNAPSHOT[:2] == (qp, mtime):
                    raw = _QUEUE_SNAPSHOT[2]
                    break
                raw = qp.read_text(encoding="utf-8")
                _QUEUE_SNAPSHOT = (qp, mtime, raw)
                break
        except OSError:
            continue
    if not raw:
        return raw
    try:
        from codebot.readiness import load_approved_ids, filter_unapproved_items
    except ImportError:
        try:
            from codebot.readiness import load_approved_ids, filter_unapproved_items
        except ImportError:
            return raw
    approved = load_approved_ids(str(STATE_DIR))
    return filter_unapproved_items(raw, approved)


def _manifest_read_noop_value(manifest: dict) -> int:
    """Read noop counter for manifest; missing file or cap==0 -> 0."""
    cap = manifest.get("noop_cap", 0)
    try:
        cap_i = int(cap)
    except Exception:
        cap_i = 0
    if cap_i <= 0:
        return 0
    counter_file = manifest.get("noop_counter_file", "")
    if not counter_file or not isinstance(counter_file, str):
        return 0
    path = _manifest_resolve_file(counter_file)
    try:
        if path.exists():
            txt = path.read_text(encoding="utf-8").strip().split()[0]
            return int(txt.strip())
    except Exception:
        return 0
    return 0


def _manifest_read_mtimes(manifest: dict) -> dict[str, float]:
    """Build mtimes dict for manifest inputs via BOTS_DIR resolution."""
    mtimes: dict[str, float] = {}
    inputs = manifest.get("input", [])
    if not isinstance(inputs, list):
        return mtimes
    for entry in inputs:
        if not isinstance(entry, dict):
            continue
        rel = entry.get("path")
        if not isinstance(rel, str) or not rel:
            continue
        path = _manifest_resolve_file(rel)
        try:
            if path.exists():
                mtimes[rel] = path.stat().st_mtime
        except OSError:
            continue
    return mtimes


def _manifest_read_queue_remaining(name: str) -> list[str] | int:
    """Read checkpoint queue_remaining for a manifest bot (injectable, no I/O inside predicates)."""
    # Why: readiness requires checkpoint's queue_remaining; orchestrator injects it.
    ckpt = STATE_DIR / f"{name}.checkpoint.json"
    candidates = [ckpt, BOTS_DIR / "state" / f"{name}.checkpoint.json"]
    for p in candidates:
        try:
            if p.exists():
                mtime = p.stat().st_mtime
                cached = _CHECKPOINT_SNAPSHOTS.get(p)
                if cached and cached[0] == mtime:
                    return cached[1]
                data = json.loads(p.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    if "queue_remaining" in data:
                        v = data["queue_remaining"]
                        if isinstance(v, (list, int)):
                            _CHECKPOINT_SNAPSHOTS[p] = (mtime, v)
                        return v
                    if "queue" in data:
                        v = data["queue"]
                        if isinstance(v, (list, int)):
                            _CHECKPOINT_SNAPSHOTS[p] = (mtime, v)
                        return v
                    if "remaining" in data:
                        v = data["remaining"] or []
                        if isinstance(v, (list, int)):
                            _CHECKPOINT_SNAPSHOTS[p] = (mtime, v)
                        return v
        except Exception:
            continue
    return []


def _manifest_get_budget_state() -> str | None:
    """Return budget_state string (ok|warn|shed_tier3|stop) or None if budget unavailable."""
    # Why: todo-11 token_budget is optional until stable; missing ledger -> None (treat as ok)
    # keeps preview and packing working without budget module.
    try:
        # Prefer package import (Work cwd), fallback to bots.* path
        try:
            import token_budget as _tb  # type: ignore
        except ImportError:
            try:
                from bots import token_budget as _tb  # type: ignore
            except ImportError:
                return None
        # If token_budget has get_budget_state, call it
        if hasattr(_tb, "get_budget_state"):
            # Try to compute day_total first if available
            try:
                if hasattr(_tb, "day_total"):
                    import datetime as _dt
                    day = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d")
                    try:
                        total = _tb.day_total(day)
                    except Exception:
                        total = None
                    if total is not None:
                        return _tb.get_budget_state(total)
                    # Fallback: call without arg if signature allows
                    try:
                        return _tb.get_budget_state()
                    except TypeError:
                        return None
                else:
                    return _tb.get_budget_state(0)
            except Exception:
                try:
                    return _tb.get_budget_state(0)
                except Exception:
                    return None
        return None
    except Exception:
        return None


def _manifest_identity_errors(manifests: dict[str, dict]) -> list[str]:
    registry_names = {config.name for config in BOT_REGISTRY}
    manifest_names = set(manifests)
    return (
        [f"missing manifest: {name}" for name in sorted(registry_names - manifest_names)]
        + [f"extra manifest: {name}" for name in sorted(manifest_names - registry_names)]
    )


def _load_manifests_safe() -> dict[str, dict]:
    """Load manifests via manifest_schema, failing open with log on corruption."""
    try:
        try:
            from manifest_schema import load_all_manifests as _lam  # type: ignore
        except ImportError:
            from codebot.manifest_schema import load_all_manifests as _lam  # type: ignore
        # Resolve via BOTS_DIR/manifests (cwd-relative, never absolute)
        mdir = BOTS_DIR / "manifests"
        if not mdir.exists():
            mdir = BOTS_DIR / ".codebot" / "manifests"
        global _MANIFEST_SNAPSHOT
        fingerprint = tuple(sorted((path.name, path.stat().st_mtime) for path in mdir.glob("*.json")))
        if _MANIFEST_SNAPSHOT and _MANIFEST_SNAPSHOT[:2] == (mdir, fingerprint):
            return _MANIFEST_SNAPSHOT[2]
        manifests = _lam(mdir)
        identity_errors = _manifest_identity_errors(manifests)
        if identity_errors:
            logger.warning("manifest identity mismatch (no spawn): %s", "; ".join(identity_errors))
            return {}
        _MANIFEST_SNAPSHOT = (mdir, fingerprint, manifests)
        return manifests
    except Exception as e:
        # Why: corrupt manifest must skip, not crash orchestrator (fail-open for health loop,
        # but preview surfaces the error). Log once, return empty so tick still logs.
        logger.warning(f"manifest load failed (skipping, no spawn): {e}")
        return {}


def _collect_manifest_readiness(
    bots: dict[str, BotState], now: float, queue_text: str | None = None
) -> tuple[list[dict], list[tuple[str, str]], int]:
    """Collect ready manifests via readiness.ready() plus skipped reasons."""
    # Why: pure readiness predicates are injected with file contents (no I/O inside);
    # orchestrator gathers mtimes/noop/queue_remaining and asks readiness.
    manifests = _load_manifests_safe()
    considered = len(manifests)
    if considered == 0:
        return [], [], 0
    if queue_text is None:
        queue_text = _manifest_load_queue_text()
    # Lazy import readiness (keeps legacy import-free when flag off)
    try:
        try:
            from codebot.readiness import is_due as _is_due, noop_ok as _noop_ok, ready as _ready, signals_ok as _signals_ok  # type: ignore
        except ImportError:
            from codebot.readiness import is_due as _is_due, noop_ok as _noop_ok, ready as _ready, signals_ok as _signals_ok  # type: ignore
    except Exception as e:
        logger.warning(f"readiness import failed: {e}")
        return [], [(n, "readiness-import-failed") for n in manifests], considered

    ready_list: list[dict] = []
    skipped: list[tuple[str, str]] = []
    for name, manifest in manifests.items():
        # enabled gate first (manifest owns enabled when flag ON)
        if not manifest.get("enabled", True):
            skipped.append((name, "disabled"))
            continue
        if (STATE_DIR / f"{name}.paused").exists():
            skipped.append((name, "paused"))
            continue
        # running gate: don't schedule a bot already alive
        bot = bots.get(name)
        if bot is not None:
            proc = getattr(bot, "process", None)
            if proc is not None and proc.poll() is None:
                skipped.append((name, "running"))
                continue
            next_run_at = bot.next_run_at
            # Also respect BotState disabled via 3-error gate
            if not bot.config.enabled:
                skipped.append((name, "disabled"))
                continue
        else:
            next_run_at = 0.0
        try:
            exceeded, reason = _manifest_restart_budget_exceeded(manifest, now)
            if exceeded:
                skipped.append((name, reason or "restart-budget-exceeded"))
                continue
        except Exception:
            pass
        try:
            disabled, reason2 = _manifest_error_disabled(manifest)
            if disabled:
                skipped.append((name, reason2 or "error-disabled"))
                continue
        except Exception:
            pass
        counter_value = _manifest_read_noop_value(manifest)
        mtimes = _manifest_read_mtimes(manifest)
        queue_remaining = _manifest_read_queue_remaining(name)
        ctx = {
            "now": now,
            "next_run_at": next_run_at,
            "counter_value": counter_value,
            "mtimes": mtimes,
            "queue_remaining": queue_remaining,
            "queue_text": queue_text,
        }
        # is_due / noop_ok fast path for clearer reason strings
        try:
            if not _is_due(manifest, now, next_run_at):
                skipped.append((name, "not-due"))
                continue
        except Exception:
            skipped.append((name, "not-due"))
            continue
        try:
            if not _noop_ok(manifest, counter_value):
                skipped.append((name, "noop-cap"))
                continue
        except Exception:
            skipped.append((name, "noop-cap"))
            continue
        try:
            is_ready = _ready(manifest, ctx)
        except Exception as e:
            skipped.append((name, f"readiness-error:{e}"))
            continue
        if not is_ready:
            # Diagnose which signals_ok piece failed for reasoning
            try:
                if not _signals_ok(manifest, mtimes, queue_remaining, queue_text=queue_text, now=now):
                    # Distinguish stale vs queue vs remaining via direct checks
                    # Check mtime-fresh first
                    stale = False
                    inputs = manifest.get("input", [])
                    if isinstance(inputs, list) and inputs:
                        for ent in inputs:
                            if not isinstance(ent, dict):
                                continue
                            rel = ent.get("path")
                            if not isinstance(rel, str):
                                continue
                            mt = mtimes.get(rel)
                            if mt is None or float(mt) < (now - 86400):
                                stale = True
                                break
                    if stale:
                        skipped.append((name, "stale-mtime"))
                    else:
                        # Check queue_has_work vs queue_remaining
                        try:
                            from codebot.readiness import queue_has_work as _qhw  # type: ignore
                        except ImportError:
                            from codebot.readiness import queue_has_work as _qhw  # type: ignore
                        try:
                            has_work = _qhw(manifest.get("kind", ""), manifest.get("complexity_filter"), queue_text)
                        except Exception:
                            has_work = False
                        if not has_work:
                            skipped.append((name, "no-queue-work"))
                        else:
                            # Remaining must be empty
                            is_empty = False
                            if isinstance(queue_remaining, list):
                                is_empty = len(queue_remaining) == 0
                            elif isinstance(queue_remaining, int):
                                is_empty = queue_remaining <= 0
                            else:
                                is_empty = not queue_remaining
                            if manifest.get("kind") != "scan" and is_empty:
                                skipped.append((name, "no-queue-remaining"))
                            else:
                                skipped.append((name, "not-ready"))
                    continue
            except Exception:
                pass
            skipped.append((name, "not-ready"))
            continue
        # E3: model-tier complexity routing — cheap models skip critical/high,
        # expensive models skip trivial/small unless their tier queue is empty.
        bot_model = str(manifest.get("model", ""))
        if bot_model in MODEL_TIER_CHEAP or bot_model in MODEL_TIER_EXPENSIVE:
            try:
                try:
                    from codebot.readiness import _parse_queue_complexity_from_text as _pqct  # type: ignore
                except ImportError:
                    from codebot.readiness import _parse_queue_complexity_from_text as _pqct  # type: ignore
                queue_complexities = _pqct(queue_text) if queue_text else {}
            except Exception:
                queue_complexities = {}
            available = set(queue_complexities.values()) if queue_complexities else set()
            has_tier_work = any(
                _model_tier_for_complexity(bot_model, c, queue_has_tier_work=False)
                for c in available
            ) if available else False
            manifest_filter = manifest.get("complexity_filter")
            if isinstance(manifest_filter, list) and manifest_filter:
                allowed_any = any(
                    _model_tier_for_complexity(bot_model, c, queue_has_tier_work=has_tier_work)
                    for c in manifest_filter
                )
                if not allowed_any:
                    skipped.append((name, "model-tier-mismatch"))
                    continue
            elif available and not has_tier_work and bot_model in MODEL_TIER_EXPENSIVE:
                pass
            elif available:
                sample = next(iter(available))
                if not _model_tier_for_complexity(bot_model, sample, queue_has_tier_work=has_tier_work):
                    skipped.append((name, "model-tier-mismatch"))
                    continue
        ready_list.append(manifest)
    return ready_list, skipped, considered


def _plan_manifest_batches(
    ready: list[dict], bots: dict[str, BotState]
) -> dict:
    """Order, pack and cap ready manifests for dispatch."""
    if not ready:
        return {"batches": [], "dropped": [], "reason": None, "warning": None, "staggers": [], "stagger_s": 20}
    # Lazy import scheduler
    try:
        try:
            from batch_scheduler import order_by_tier as _obt, pack_batches as _pb, apply_caps as _ac  # type: ignore
        except ImportError:
            from codebot.batch_scheduler import order_by_tier as _obt, pack_batches as _pb, apply_caps as _ac  # type: ignore
    except Exception as e:
        logger.warning(f"batch_scheduler import failed: {e}")
        return {"batches": [ready], "dropped": [], "reason": None, "warning": None, "staggers": [0], "stagger_s": 20}

    tier_map = {}
    next_run_map: dict[str, float] = {}
    for m in ready:
        n = m.get("name", "")
        try:
            tier_map[n] = int(m.get("tier_priority", 999))
        except Exception:
            tier_map[n] = 999
        bot = bots.get(n)
        if bot is not None and getattr(bot, "next_run_at", None):
            try:
                next_run_map[n] = float(bot.next_run_at)
            except Exception:
                next_run_map[n] = 0.0
        else:
            next_run_map[n] = 0.0
    names = [m.get("name", "") for m in ready if isinstance(m, dict) and m.get("name")]
    ordered_names = _obt(names, tier_map, next_run_map)
    # Reorder ready to match tier order
    name_to_manifest = {m.get("name"): m for m in ready if isinstance(m, dict) and m.get("name")}
    ready_ordered = [name_to_manifest[n] for n in ordered_names if n in name_to_manifest]
    # Preserve any manifests whose names missing from ordered (should not happen)
    extra = [m for m in ready if m.get("name") not in ordered_names]
    ready_ordered.extend(extra)

    budget_state = _manifest_get_budget_state()
    packed = _pb(ready_ordered, max_per_batch=10, max_batches=3, budget_state=budget_state, stagger_s=3)
    # Apply model/slot caps
    try:
        capped = _ac(packed, thinking_cap=6, qwen38max_cap=4, slot_cap=30)
        return capped
    except Exception as e:
        logger.warning(f"apply_caps failed: {e}")
        return packed


def _dispatch_manifest_batches(packed: dict, bots: dict[str, BotState]) -> None:
    """Dispatch batches: api via api_runner.run_batch, opencode via start_bot."""
    batches = packed.get("batches", []) if isinstance(packed, dict) else []
    if not batches:
        return
    # Resolve api_runner.run_batch lazily (keeps legacy import-free)
    run_batch_fn = None
    try:
        try:
            from api_runner import run_batch as _rb  # type: ignore
            run_batch_fn = _rb
        except ImportError:
            from codebot.api_runner import run_batch as _rb  # type: ignore
            run_batch_fn = _rb
    except Exception:
        run_batch_fn = None

    queue_path = BOTS_DIR / "docs" / "triage" / "QUEUE.md"
    queue_text_for_items = packed.get("queue_text") if isinstance(packed, dict) else None
    if isinstance(queue_text_for_items, str) and queue_text_for_items:
        try:
            from codebot.readiness import _parse_queue_complexity_from_text as _pqct  # type: ignore
        except ImportError:
            _pqct = None  # type: ignore
        queue_items = _pqct(queue_text_for_items) if _pqct is not None else _parse_queue_complexity(queue_path)
    else:
        queue_items = _parse_queue_complexity(queue_path)

    for idx, batch in enumerate(batches):
        if not isinstance(batch, list):
            continue
        leased_batch: list[dict] = []
        for manifest in batch:
            if not isinstance(manifest, dict) or manifest.get("kind") != "queue":
                leased_batch.append(manifest)
                continue
            filters = manifest.get("complexity_filter") or []
            allowed = {"small" if value == "low" else value for value in filters if isinstance(value, str)}
            work_item_id = next(
                (item_id for item_id, complexity in queue_items.items() if not allowed or complexity in allowed),
                None,
            )
            if work_item_id is None:
                logger.info("queue manifest %s skipped: no matching queue item", manifest.get("name", "?"))
                continue
            try:
                try:
                    from lease_state import acquire as acquire_lease  # type: ignore
                except ImportError:
                    from codebot.lease_state import acquire as acquire_lease  # type: ignore
                lease = acquire_lease(STATE_DIR, work_item_id, str(manifest.get("name", "")), now=time.time(), lease_seconds=int(manifest.get("session_timeout", 300)))
            except Exception as exc:
                logger.warning("queue manifest %s skipped: lease unavailable: %s", manifest.get("name", "?"), exc)
                continue
            if lease.get("status") != "acquired":
                logger.info("queue manifest %s skipped: lease %s", manifest.get("name", "?"), lease.get("status"))
                continue
            leased_manifest = dict(manifest)
            leased_manifest["work_item_id"] = work_item_id
            leased_batch.append(leased_manifest)
            del queue_items[work_item_id]
        api_manifests = [m for m in leased_batch if isinstance(m, dict) and m.get("runner") == "api"]
        opencode_manifests = [m for m in leased_batch if isinstance(m, dict) and m.get("runner") != "api"]
        # Gate drain before each batch
        if is_draining() or UPDATE_LOCK.exists():
            logger.info(f"manifest dispatch aborted at batch {idx}: drain active")
            break
        if api_manifests and run_batch_fn is not None:
            batch_ctx: dict = {
                "pool_per_manifest": 50,
                "heartbeat_max_gap_s": 120,
                "heartbeat_dir": str(STATE_DIR),
                "state_dir": str(STATE_DIR),
                "drain_check_cb": lambda: is_draining() or UPDATE_LOCK.exists(),
                "budget_check_cb": lambda: _manifest_get_budget_state(),
            }
            try:
                # Why: token report is optional; ledger single-writer is inside run_batch
                batch_result = run_batch_fn(api_manifests, batch_ctx)
                completed_names = {
                    result.get("name") for result in batch_result.get("results", [])
                    if isinstance(result, dict) and result.get("status") == "completed"
                }
                try:
                    try:
                        from lease_state import release as release_lease  # type: ignore
                    except ImportError:
                        from codebot.lease_state import release as release_lease  # type: ignore
                    for manifest in api_manifests:
                        work_item_id = manifest.get("work_item_id")
                        name = manifest.get("name")
                        if isinstance(work_item_id, str) and name in completed_names:
                            release_lease(STATE_DIR, work_item_id, str(name))
                except Exception as exc:
                    logger.warning("manifest batch %s lease release unavailable: %s", idx, exc)
                logger.info(f"manifest batch {idx}: dispatched {len(api_manifests)} api manifests via run_batch")
            except Exception as e:
                logger.error(f"manifest batch {idx} api run_batch failed: {e}")
        # Opencode keeps classic subprocess spawning
        for m in opencode_manifests:
            name = m.get("name", "")
            bot = bots.get(name)
            if bot is None:
                logger.warning(f"opencode manifest '{name}' has no BotState — skipped")
                continue
            if not bot.config.enabled:
                continue
            # Respect spawn gate and slot caps
            ok, why = _spawn_gate(bots=bots, is_queued=False, runner_mode="api", bot_model=m.get("model", ""))
            if ok:
                start_bot(bot, bots=bots)
                time.sleep(GATEWAY_MIN_SPAWN_GAP)
            else:
                logger.info(f"Queued opencode manifest '{name}' (manifest due, {why}) — conductor holds slot")
                update_bot_state(bot, "queued")
        # The health loop must remain responsive between independently scheduled batches.
        if idx < len(batches) - 1:
            try:
                stagger = 20
                if isinstance(packed, dict):
                    stagger = int(packed.get("stagger_s", 20))
                logger.info("manifest batch %s queued next batch with stagger %ss", idx, stagger)
            except Exception:
                logger.warning("manifest batch %s could not record stagger", idx)


def _check_all_bots_manifest(bots: dict[str, BotState]) -> None:
    """Manifest-driven health loop (flag ON only)."""
    rotate_logs()
    if is_draining():
        for bot in bots.values():
            if bot.process is not None and bot.process.poll() is None:
                continue
            if bot.process is not None:
                update_bot_state(bot, "drained")
        return
    now = time.time()
    # Retry disabled bots after 10s cooldown
    for name, bot in bots.items():
        if not bot.config.enabled and bot.process is None:
            if _retry_disabled_bot(bot, bots):
                logger.info(f"Retrying '{name}' after disabled cooldown")
    # Retry bots stuck in starting >2min
    for name, bot in bots.items():
        if bot.config.enabled and bot.process is not None:
            if _retry_stuck_starting(bot, bots):
                logger.info(f"Retrying '{name}' stuck in starting")
    # Queued dequeue (same as legacy, keep kill switches intact)
    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        if bot.process is not None and bot.process.poll() is None:
            continue
        if _is_queued(bot):
            ok, why = _spawn_gate(bots=bots, is_queued=True, bot_name=name)
            if ok:
                logger.info(f"Dequeuing '{name}' — slot available")
                update_bot_state(bot, "starting")
                start_bot(bot, bots=bots)
    for name, bot in list(bots.items()):
        if not bot.config.enabled or bot.process is not None:
            continue
        status_file = STATE_DIR / f"{name}.status.json"
        if status_file.exists():
            try:
                sdata = json.loads(status_file.read_text())
                if isinstance(sdata, dict) and sdata.get("current_task") in ("starting", ""):
                    sdata["current_task"] = "idle"
                    sdata["task_description"] = "process exited"
                    _write_json_atomic(status_file, sdata)
            except Exception:
                pass
    # Exit + stuck handling for all bots (before manifest scheduling)
    for name, bot in list(bots.items()):
        if not bot.config.enabled:
            continue
        alive = bot.process is not None and bot.process.poll() is None
        if not alive and bot.process is not None:
            exit_code = bot.process.returncode
            _write_alignment_event(name, exit_code=exit_code, exit_reason="clean" if exit_code == 0 else "error", started_at=bot.started_at)
            try:
                _run_alignment_pipeline(name)
            except Exception as e:
                logger.warning(f"Alignment pipeline failed for {name}: {e}")
            bot.process = None
            try:
                _manifest_error_record(name, is_error=(exit_code != 0))
            except Exception:
                pass
            if exit_code in (0, 3):
                bot.consecutive_errors = 0
                bot.restart_count = 0
            if exit_code == 3:
                bot.consecutive_errors += 1
                backoff = min(RATE_LIMIT_REQUEUE_S * (2 ** (bot.consecutive_errors - 1)), RATE_LIMIT_BACKOFF_MAX)
                import random
                jitter = random.randint(0, min(60, backoff // 4))
                bot.next_run_at = now + backoff + jitter
                update_bot_state(bot, "waiting")
                _rotate_model_on_error(bot, bots)
                try:
                    from codebot.scratchpad import load_scratchpad, save_scratchpad
                    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
                    if assigned_tid:
                        scratch = load_scratchpad(STATE_DIR, assigned_tid)
                        scratch.mark_error(f"rate-limited (exit 3), backoff {backoff+jitter}s")
                        scratch.finish_agent(f"rate-limited")
                        save_scratchpad(STATE_DIR, scratch)
                except Exception:
                    pass
                if bot.consecutive_errors >= RATE_LIMIT_DISABLE_AFTER:
                    logger.warning(f"Bot '{name}' rate-limited {bot.consecutive_errors}x — rotated to {bot.config.model}, continuing")
                    bot.consecutive_errors = 0
                else:
                    logger.info(f"Bot '{name}' rate-limited (exit 3) — backoff {backoff+jitter}s, rotated to {bot.config.model}, attempt {bot.consecutive_errors}/{RATE_LIMIT_DISABLE_AFTER}")
            elif exit_code != 0:
                bot.consecutive_errors += 1
                base_role = name.split("-")[0] if "-" in name else name
                if base_role in IMPLEMENTER_ROLE_NAMES:
                    old_model = bot.config.model
                    _rotate_model_on_error(bot, bots)
                    if bot.config.model != old_model:
                        logger.info(f"Bot '{name}' rotated model {old_model} -> {bot.config.model} (exit {exit_code})")
                    if bot.consecutive_errors >= 5:
                        bot.consecutive_errors = 0
                        logger.warning(f"Bot '{name}' failed 5 times across models — resetting error count")
                elif bot.consecutive_errors >= 3:
                    _rotate_model_on_error(bot, bots)
                    bot.consecutive_errors = 0
                    logger.warning(f"Bot '{name}' failed 3 times — rotated to {bot.config.model}")
                else:
                    logger.warning(f"Bot '{name}' exited with code {exit_code} (attempt {bot.consecutive_errors}/3)")
                base_role = name.split("-")[0] if "-" in name else name
                assigned_tid = getattr(bot, '_assigned_ticket_id', '')
                if assigned_tid and base_role in IMPLEMENTER_ROLE_NAMES:
                    try:
                        from codebot.scratchpad import load_scratchpad, save_scratchpad
                        scratch = load_scratchpad(STATE_DIR, assigned_tid)
                        scratch.mark_error(f"exited with code {exit_code}, attempt {bot.consecutive_errors}")
                        scratch.finish_agent(f"interrupted: exit_code={exit_code}")
                        save_scratchpad(STATE_DIR, scratch)
                    except Exception:
                        pass
                    bot.next_run_at = now + 5
                    update_bot_state(bot, "waiting")
                    continue
            if exit_code == 0 and bot.config.clean_exit_wait:
                bot.next_run_at = now + bot.config.interval_seconds
                logger.info(f"Bot '{name}' completed cleanly — next run in {bot.config.interval_seconds}s")
                update_bot_state(bot, "waiting")
            else:
                bot.next_run_at = now + bot.config.interval_seconds
                update_bot_state(bot, "waiting")
            continue
        if alive and is_stuck(bot):
            eff = effective_heartbeat_timeout(bot)
            try:
                hb_raw = read_heartbeat(bot.config.name)
                hb_age = max(0.0, now - hb_raw) if hb_raw else 0
            except Exception:
                hb_age = 0
            try:
                lm = log_mtime(bot.config.name)
                log_age = now - lm if lm else 0
            except Exception:
                log_age = 0
            prof = model_profile(bot.config.model)
            risk = prof.lockup_risk if prof else "unknown"
            logger.warning(f"Bot '{name}' stuck (hb {hb_age:.0f}s > {eff}s, log {log_age:.0f}s silent, risk={risk}, model={bot.config.model})")
            _write_alignment_event(name, exit_code=None, exit_reason="stuck", started_at=bot.started_at)
            try:
                _run_alignment_pipeline(name)
            except Exception as e:
                logger.warning(f"Alignment pipeline failed for {name}: {e}")
            ck = read_checkpoint(name)
            if ck:
                try:
                    excess = hb_age - eff
                    logger.info(f"Checkpoint handoff ready for '{name}': iteration={ck.get('scan_iteration')} current={ck.get('current_task')} completed={len(ck.get('completed',[]))} excess={excess:.0f}s")
                except Exception:
                    pass
            restart_bot(bot, reason="stuck", bots=bots)
            continue
        if alive and bot.prompt_mtime > 0:
            pf = BOTS_DIR / bot.config.prompt_file
            try:
                cur_mtime = pf.stat().st_mtime
            except OSError:
                cur_mtime = 0.0
            if cur_mtime > bot.prompt_mtime:
                per_bot_drain = STATE_DIR / f".drain_{name}"
                if not per_bot_drain.exists():
                    try:
                        per_bot_drain.write_text(str(time.time()), encoding="utf-8")
                    except OSError:
                        pass
                    logger.info(f"Bot '{name}' prompt updated ({cur_mtime - bot.prompt_mtime:.0f}s ago) — draining for hot-reload")
        if alive:
            update_bot_state(bot, "running")
    _log_bot_statuses(bots)
    # Sweep orphaned claims from crashed workers
    try:
        _sweep_orphan_claims(bots)
    except Exception:
        pass
    # Adaptive scheduler gate: throttle implementers when review-congested
    try:
        _adaptive_schedule_gate(bots)
    except Exception as e:
        logger.warning(f"Adaptive schedule gate failed: {e}")
    try:
        _dispatch_tickets_to_implementers(bots)
    except Exception as e:
        logger.warning(f"Ticket dispatch failed: {e}")
    try:
        _dispatch_tickets_to_reviewers(bots)
    except Exception as e:
        logger.warning(f"Review dispatch failed: {e}")
    try:
        _advance_reviewed_tickets(bots)
    except Exception as e:
        logger.warning(f"Review advance failed: {e}")
    try:
        _gatekeeper_verify_tickets()
    except Exception as e:
        logger.warning(f"Gatekeeper verify failed: {e}")
    try:
        _process_verifying_tickets()
    except Exception as e:
        logger.warning(f"VERIFYING ticket processing failed: {e}")
    # Manifest readiness / ordering / packing / dispatch
    queue_text = _manifest_load_queue_text()
    ready, skipped, considered = _collect_manifest_readiness(bots, now, queue_text=queue_text)
    packed = _plan_manifest_batches(ready, bots)
    if isinstance(packed, dict):
        packed["queue_text"] = queue_text
    batches = packed.get("batches", []) if isinstance(packed, dict) else []
    dropped = packed.get("dropped", []) if isinstance(packed, dict) else []
    all_skipped: list[tuple[str, str]] = list(skipped)
    for d in dropped:
        if isinstance(d, dict):
            all_skipped.append((d.get("name", "?"), d.get("reason", "dropped")))
    batched_count = sum(len(b) for b in batches) if isinstance(batches, list) else 0
    # Per-tick reasoning log (required by acceptance)
    logger.info(f"tick: considered={considered} ready={len(ready)} batched={batched_count} skipped={all_skipped}")
    # Metrics collection is I/O-heavy (~375 file reads); run at most every
    # CODEBOT_METRICS_EVERY_TICKS ticks so the single-threaded loop stays responsive.
    try:
        global _metrics_tick
        _metrics_tick += 1
        _metrics_every = max(1, int(os.getenv("CODEBOT_METRICS_EVERY_TICKS", "4")))
        if _metrics_tick % _metrics_every == 1:
            import subprocess as _sp
            _sp.run(["python3", str(BOTS_DIR / "metrics_collector.py"), "--incremental"], timeout=60, capture_output=True)
    except Exception:
        pass
    if batches:
        _dispatch_manifest_batches(packed, bots)


def _preview_manifest_batches() -> dict:
    """Build dry-run plan without spawning; prints manifest/tier/reason."""
    now = time.time()
    queue_text = _manifest_load_queue_text()
    manifests = _load_manifests_safe()
    # Surface load error without crash
    if not manifests:
        print("manifest scheduler preview: no manifests loaded (check .codebot/manifests/*.json)")
        print("batches: 0")
        print(f"considered: 0 ready: 0 batched: 0 skipped: []")
        return {"batches": [], "dropped": [], "considered": 0, "ready": [], "skipped": []}

    # Collect readiness via shared helper (needs bots snapshot for next_run_at)
    # For preview we synthesize bots with due timestamps so interval never blocks preview
    preview_bots: dict[str, BotState] = {}
    for name, m in manifests.items():
        # Find matching registry entry for interval/model realism, else synthesize
        cfg = next((c for c in BOT_REGISTRY if c.name == name), None)
        if cfg is not None:
            # Use registry config but mark next_run_at as due
            st = BotState(config=cfg)
            st.next_run_at = now - 10
            preview_bots[name] = st
        else:
            # Synthetic config from manifest
            try:
                synth = BotConfig(
                    name=name,
                    prompt_file=m.get("prompt_file", f"{name}.md"),
                    interval_seconds=int(m.get("interval_seconds", 600)),
                    heartbeat_timeout=int(m.get("heartbeat_timeout", 1200)),
                    model=m.get("model", "xiaomi-mimo-2.5"),
                    fallback_model="",
                    enabled=bool(m.get("enabled", True)),
                    max_restarts=int(m.get("max_restarts", 5)),
                    clean_exit_wait=bool(m.get("clean_exit_wait", False)),
                    tier=2,
                    runner_mode=m.get("runner", "api"),
                )
                synth_tier = int(m.get("tier_priority", 99))
                # keep tier mapping via TIER_PRIORITY override for preview ordering
                st = BotState(config=synth)
                st.next_run_at = now - 10
                preview_bots[name] = st
            except Exception:
                continue

    ready, skipped, considered = _collect_manifest_readiness(preview_bots, now, queue_text=queue_text)
    packed = _plan_manifest_batches(ready, preview_bots)
    batches = packed.get("batches", []) if isinstance(packed, dict) else []
    dropped = packed.get("dropped", []) if isinstance(packed, dict) else []
    reason = packed.get("reason") if isinstance(packed, dict) else None
    warning = packed.get("warning") if isinstance(packed, dict) else None
    batched_count = sum(len(b) for b in batches) if isinstance(batches, list) else 0

    # Determine budget display (todo-11 ledger: day_total + state)
    budget_state = _manifest_get_budget_state()
    day_total_display = "unknown"
    try:
        try:
            import token_budget as _tb2  # type: ignore
        except ImportError:
            from bots import token_budget as _tb2  # type: ignore
        if hasattr(_tb2, "day_total"):
            import datetime as _dt2
            day = _dt2.datetime.now(_dt2.timezone.utc).strftime("%Y-%m-%d")
            try:
                dtv = _tb2.day_total(day)
                day_total_display = str(dtv)
            except Exception:
                pass
    except Exception:
        pass

    print("manifest scheduler preview (dry-run — no spawn)")
    print(f"considered: {considered} ready: {len(ready)} batched: {batched_count} skipped: {skipped + [(d.get('name'), d.get('reason')) for d in dropped if isinstance(d, dict)]}")
    print(f"batches: {len(batches)}")
    if budget_state is not None:
        print(f"budget_state: {budget_state} day_total: {day_total_display}")
    else:
        print(f"budget_state: ok day_total: {day_total_display}")
    if reason:
        print(f"reason: {reason}")
    if warning:
        print(f"warning: {warning}")
    for idx, batch in enumerate(batches):
        stagger = 0
        try:
            staggers = packed.get("staggers", []) if isinstance(packed, dict) else []
            if isinstance(staggers, list) and idx < len(staggers):
                stagger = staggers[idx]
            else:
                stagger = idx * int(packed.get("stagger_s", 20)) if isinstance(packed, dict) else idx * 20
        except Exception:
            stagger = idx * 20
        print(f"batch {idx} (stagger {stagger}s):")
        if isinstance(batch, list):
            for m in batch:
                if not isinstance(m, dict):
                    print(f"  - {m}")
                    continue
                name = m.get("name", "?")
                tier = m.get("tier_priority", "?")
                model = m.get("model", "?")
                runner = m.get("runner", "?")
                print(f"  - manifest={name} tier={tier} model={model} runner={runner} reason=included")
    # Skipped with reasons (readiness + dropped)
    if skipped:
        print("skipped (readiness):")
        for n, r in skipped:
            print(f"  - manifest={n} reason={r}")
    if dropped:
        print("dropped (packing/caps/budget):")
        for d in dropped:
            if isinstance(d, dict):
                print(f"  - manifest={d.get('name','?')} reason={d.get('reason','?')} tier={d.get('manifest',{}).get('tier_priority','?') if isinstance(d.get('manifest'), dict) else '?'}")
            else:
                print(f"  - {d}")
    if not batches:
        print("batches: 0")
    return {"batches": batches, "dropped": dropped, "considered": considered, "ready": ready, "skipped": skipped, "budget_state": budget_state, "day_total": day_total_display}


def _process_verifying_tickets() -> None:
    """Process VERIFYING tickets through the Gatekeeper.

    The Gatekeeper handles its own state transitions internally via
    _transition_ticket(). We just call verify_ticket() and log the result.
    Tickets with no changed_files skip quality gates entirely since there's
    nothing to compile or test.
    """
    try:
        from codebot.ticket_engine import TicketStore, TicketState
        from codebot.gatekeeper import Gatekeeper

        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if not store_path.exists():
            return

        store = TicketStore(store_path)
        verifying = store.list_by_state(TicketState.VERIFYING)

        if not verifying:
            return

        logger.info(f"Processing {len(verifying)} VERIFYING tickets through gatekeeper")

        for ticket in verifying:
            try:
                changed_files = ticket.affected_modules if ticket.affected_modules else []
                ticket_class = ticket.ticket_class.value if hasattr(ticket.ticket_class, 'value') else str(ticket.ticket_class)

                if not changed_files:
                    try:
                        ts_fresh = TicketStore(store_path)
                        t = ts_fresh.get(ticket.id)
                        if t and t.state == TicketState.VERIFYING:
                            rework_count = getattr(t, 'rework_count', 0)
                            if rework_count < 3:
                                ts_fresh.transition(ticket.id, TicketState.REWORK)
                                logger.info(f"Gatekeeper: {ticket.id} -> REWORK (no files changed by implementer)")
                            else:
                                ts_fresh.transition(ticket.id, TicketState.REJECTED)
                                logger.warning(f"Gatekeeper: {ticket.id} -> REJECTED (no files after {rework_count} reworks)")
                    except ValueError:
                        pass
                    continue

                files_actually_modified = False
                for mod in changed_files[:5]:
                    try:
                        r = subprocess.run(
                            ["git", "diff", "--name-only", "HEAD~5", "--", mod],
                            capture_output=True, text=True,
                            cwd=str(Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))),
                            timeout=10,
                        )
                        if r.stdout.strip():
                            files_actually_modified = True
                            break
                    except Exception:
                        pass
                if not files_actually_modified:
                    try:
                        ts_fresh = TicketStore(store_path)
                        t = ts_fresh.get(ticket.id)
                        if t and t.state == TicketState.VERIFYING:
                            rework_count = getattr(t, 'rework_count', 0)
                            if rework_count < 3:
                                ts_fresh.transition(ticket.id, TicketState.REWORK)
                                logger.info(f"Gatekeeper: {ticket.id} -> REWORK (no files actually modified in git)")
                            else:
                                ts_fresh.transition(ticket.id, TicketState.REJECTED)
                                logger.warning(f"Gatekeeper: {ticket.id} -> REJECTED (no modifications after {rework_count} reworks)")
                    except ValueError:
                        pass
                    continue

                gk = Gatekeeper(
                    state_dir=STATE_DIR,
                    policy_path=STATE_DIR.parent / "quality_gates.yaml" if (STATE_DIR.parent / "quality_gates.yaml").exists() else None,
                    workspace=BOTS_DIR,
                )

                result = gk.verify_ticket(
                    ticket_id=ticket.id,
                    ticket_class=ticket_class,
                    changed_files=changed_files,
                    rework_count=ticket.rework_count,
                )

                decision = result.get('decision', '')
                logger.info(f"Gatekeeper: {ticket.id} -> {decision}")

            except Exception as e:
                logger.error(f"Failed to process VERIFYING ticket {ticket.id}: {e}")

    except Exception as e:
        logger.error(f"Failed to process VERIFYING tickets: {e}")


def _evaluate_budget() -> str:
    ledger_path = STATE_DIR / "token_ledger.json"
    daily_limit_tokens = 500_000
    try:
        from codebot.scheduler_config import load_scheduler_config
        cfg = load_scheduler_config()
        daily_limit_usd = cfg.cost.daily_limit_usd
        if daily_limit_usd > 0:
            daily_limit_tokens = int(daily_limit_usd * 10_000)
    except Exception:
        pass

    total_actual = 0
    if ledger_path.exists():
        try:
            data = json.loads(ledger_path.read_text(encoding="utf-8"))
            total_actual = int(data.get("total_actual", 0))
        except Exception:
            pass

    pct = (total_actual / daily_limit_tokens * 100) if daily_limit_tokens > 0 else 0.0

    if pct >= 100:
        status = "halt"
    elif pct >= 80:
        status = "throttle"
    else:
        status = "ok"

    status_path = STATE_DIR / "budget_controller.status.json"
    try:
        status_data = {
            "status": status,
            "daily_pct": round(pct, 2),
            "total_actual": total_actual,
            "per_ticket_flags": [],
            "updated_at": time.time(),
        }
        tmp = status_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(status_data), encoding="utf-8")
        tmp.replace(status_path)
    except OSError:
        pass

    return status


_QUALITY_TRACKER = None


def _record_quality_metrics() -> None:
    global _QUALITY_TRACKER
    if _QUALITY_TRACKER is None:
        try:
            from codebot.quality_metrics import QualityMetricsTracker
            _QUALITY_TRACKER = QualityMetricsTracker(
                state_dir=STATE_DIR,
                project_root=Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd())),
            )
        except ImportError:
            return
    try:
        snapshot = _QUALITY_TRACKER.maybe_record()
        if snapshot:
            logger.info(
                f"Quality metrics: complete={snapshot.complete_tickets}/{snapshot.total_tickets} "
                f"rework_rate={snapshot.rework_rate} completion_rate={snapshot.completion_rate} "
                f"escaped={snapshot.escaped_defects} throughput_hr={snapshot.completions_last_hour}/hr "
                f"median_lifecycle={snapshot.median_lifecycle_minutes}min"
            )
    except Exception as e:
        logger.warning(f"Quality metrics recording failed: {e}")


def check_all_bots(bots: dict[str, BotState]) -> None:
    if _check_self_restart(bots):
        return
    if USE_MANIFEST_SCHEDULER:
        return _check_all_bots_manifest(bots)
    _evaluate_budget()
    _record_quality_metrics()
    _apply_agent_availability(bots)
    _sweep_orphan_claims(bots)
    rotate_logs()
    _check_prompt_changes(bots)
    _check_code_changes(bots)
    _check_config_changes(bots)
    if is_draining():
        for bot in bots.values():
            if bot.process is not None and bot.process.poll() is None:
                continue
            if bot.process is not None:
                update_bot_state(bot, "drained")
        return
    now = time.time()

    try:
        _dynamic_scale_bots(bots)
    except Exception as e:
        logger.warning(f"Dynamic scaling failed: {e}")
    try:
        _recover_stuck_implementing_tickets(bots)
    except Exception as e:
        logger.warning(f"Stuck ticket recovery failed: {e}")

    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        if bot.process is not None and bot.process.poll() is None:
            continue
        if _is_queued(bot):
            ok, why = _spawn_gate(bots=bots, is_queued=True)
            if ok:
                pipeline = _get_pipeline_state()
                if _is_needed_bot(name, pipeline):
                    logger.info(f"Dequeuing '{name}' — slot available")
                    update_bot_state(bot, "starting")
                    start_bot(bot, bots=bots)
                else:
                    logger.info(f"Dequeuing '{name}' — not needed, staying idle")
                    update_bot_state(bot, "waiting")

    for name, bot in list(bots.items()):
        if not bot.config.enabled or bot.process is not None:
            continue
        status_file = STATE_DIR / f"{name}.status.json"
        if status_file.exists():
            try:
                sdata = json.loads(status_file.read_text())
                if isinstance(sdata, dict) and sdata.get("current_task") in ("starting", ""):
                    sdata["current_task"] = "idle"
                    sdata["task_description"] = "process exited"
                    _write_json_atomic(status_file, sdata)
            except Exception:
                pass

    for name in due_bots_first(bots):
        bot = bots[name]
        if not bot.config.enabled:
            continue
        alive = bot.process is not None and bot.process.poll() is None
        if not alive and bot.process is not None:
            exit_code = bot.process.returncode
            base_role = name.split("-")[0] if "-" in name else name
            if base_role in DISCOVERY_ROLE_NAMES | (PLANNING_ROLE_NAMES - {"implementation_planner"}):
                stream_path = LOGS_DIR / f"{name}.stream.json"
                created_ticket = False
                if stream_path.exists():
                    try:
                        sdata = json.loads(stream_path.read_text(encoding="utf-8", errors="ignore"))
                        for m in sdata.get("messages", []):
                            if m.get("role") == "assistant":
                                for tc in m.get("tool_calls", []):
                                    if tc.get("function", {}).get("name") == "create_ticket":
                                        created_ticket = True
                                        break
                            if created_ticket:
                                break
                    except Exception:
                        pass
                if not created_ticket:
                    log_path = LOGS_DIR / f"{name}.log"
                    if log_path.exists():
                        try:
                            log_text = log_path.read_text(errors="ignore")
                            if "create_ticket" in log_text:
                                created_ticket = True
                        except Exception:
                            pass
                if not created_ticket:
                    marker = STATE_DIR / f"{name}.no_tickets"
                    try:
                        prev = 0
                        if marker.exists():
                            try:
                                prev = int(marker.read_text(encoding="utf-8").strip().split("|")[0])
                            except (ValueError, IndexError):
                                prev = 0
                        new_count = prev + 1
                        marker.write_text(f"{new_count}|{time.time()}", encoding="utf-8")
                        logger.info(f"Discovery agent {name} exited without create_ticket — penalty marker written ({new_count} consecutive)")
                        if new_count >= MAX_DISCOVERY_NO_TICKET_RUNS:
                            bot.config.enabled = False
                            logger.error(f"Discovery agent '{name}' disabled after {new_count} runs without creating tickets")
                            update_bot_state(bot, "disabled")
                    except OSError:
                        pass
                else:
                    marker = STATE_DIR / f"{name}.no_tickets"
                    try:
                        if marker.exists():
                            marker.unlink()
                    except OSError:
                        pass
            _write_alignment_event(name, exit_code=exit_code, exit_reason="clean" if exit_code == 0 else "error", started_at=bot.started_at)
            try:
                _run_alignment_pipeline(name)
            except Exception as e:
                logger.warning(f"Alignment pipeline failed for {name}: {e}")
            bot.process = None
            if exit_code == 3:
                bot.consecutive_errors += 1
                backoff = min(RATE_LIMIT_REQUEUE_S * (2 ** (bot.consecutive_errors - 1)), RATE_LIMIT_BACKOFF_MAX)
                import random
                jitter = random.randint(0, min(60, backoff // 4))
                bot.next_run_at = now + backoff + jitter
                _rotate_model_on_error(bot, bots)
                update_bot_state(bot, "waiting")
                try:
                    from codebot.scratchpad import load_scratchpad, save_scratchpad
                    assigned_tid = getattr(bot, '_assigned_ticket_id', '')
                    if assigned_tid:
                        scratch = load_scratchpad(STATE_DIR, assigned_tid)
                        scratch.mark_error(f"rate-limited (exit 3), backoff {backoff+jitter}s")
                        scratch.finish_agent(f"rate-limited after {tool_iterations} iters")
                        save_scratchpad(STATE_DIR, scratch)
                except Exception:
                    pass
                if bot.consecutive_errors >= RATE_LIMIT_DISABLE_AFTER:
                    logger.error(f"Bot '{name}' rate-limited {bot.consecutive_errors}x — disabling (model {bot.config.model})")
                    bot.config.enabled = False
                    update_bot_state(bot, "disabled")
                else:
                    logger.info(f"Bot '{name}' rate-limited (exit 3) — backoff {backoff+jitter}s, attempt {bot.consecutive_errors}/{RATE_LIMIT_DISABLE_AFTER}")
                continue
            if exit_code == 0:
                bot.consecutive_errors = 0
                assigned_tid = getattr(bot, '_assigned_ticket_id', '')
                base_role = name.split("-")[0] if "-" in name else name
                if assigned_tid:
                    try:
                        from codebot.ticket_engine import TicketStore, TicketState
                        store_path = STATE_DIR / "tickets.json"
                        if not store_path.exists():
                            store_path = Path(".codebot/state/tickets.json")
                        if store_path.exists():
                            ts = TicketStore(store_path)
                            t = ts.get(assigned_tid)
                            if t is not None:
                                if base_role in REVIEWER_ROLE_NAMES:
                                    if t.state == TicketState.REVIEWING:
                                        ts.transition(assigned_tid, TicketState.VERIFYING)
                                        logger.info(f"Ticket {assigned_tid} -> VERIFYING (reviewer {name} completed)")
                                else:
                                    if t.state == TicketState.IMPLEMENTING:
                                        ts.transition(assigned_tid, TicketState.REVIEWING)
                                        logger.info(f"Ticket {assigned_tid} -> REVIEWING (agent {name} completed)")
                                    else:
                                        logger.info(f"Ticket {assigned_tid} in {t.state.value}, skipping REVIEWING transition")
                            claims_dir = STATE_DIR / "claims"
                            for cf in claims_dir.glob(f"{assigned_tid}.*.json"):
                                cf.unlink(missing_ok=True)
                    except Exception as te:
                        logger.warning(f"Ticket transition failed for {assigned_tid}: {te}")
                    bot._assigned_ticket_id = ''
            if base_role in DISCOVERY_ROLE_NAMES:
                    stream_path = LOGS_DIR / f"{name}.stream.json"
                    created_ticket = False
                    if stream_path.exists():
                        try:
                            sdata = json.loads(stream_path.read_text(encoding="utf-8", errors="ignore"))
                            for m in sdata.get("messages", []):
                                if m.get("role") == "assistant":
                                    for tc in m.get("tool_calls", []):
                                        if tc.get("function", {}).get("name") == "create_ticket":
                                            created_ticket = True
                                            break
                                if created_ticket:
                                    break
                        except Exception:
                            pass
                    if not created_ticket:
                        log_path = LOGS_DIR / f"{name}.log"
                        if log_path.exists():
                            try:
                                log_text = log_path.read_text(errors="ignore")
                                if "create_ticket" in log_text:
                                    created_ticket = True
                            except Exception:
                                pass
                    if not created_ticket:
                        marker = STATE_DIR / f"{name}.no_tickets"
                        try:
                            prev = 0
                            if marker.exists():
                                try:
                                    prev = int(marker.read_text(encoding="utf-8").strip().split("|")[0])
                                except (ValueError, IndexError):
                                    prev = 0
                            new_count = prev + 1
                            marker.write_text(f"{new_count}|{time.time()}", encoding="utf-8")
                            logger.info(f"Discovery agent {name} exited without create_ticket — penalty marker written ({new_count} consecutive)")
                            if new_count >= MAX_DISCOVERY_NO_TICKET_RUNS:
                                bot.config.enabled = False
                                logger.error(f"Discovery agent '{name}' disabled after {new_count} runs without creating tickets")
                                update_bot_state(bot, "disabled")
                        except OSError:
                            pass
                    else:
                        marker = STATE_DIR / f"{name}.no_tickets"
                        try:
                            if marker.exists():
                                marker.unlink()
                        except OSError:
                            pass
            else:
                bot.consecutive_errors += 1
                if bot.consecutive_errors >= 3:
                    old_model = bot.config.model
                    _rotate_model_on_error(bot, bots)
                    bot.consecutive_errors = 0
                    logger.warning(f"Bot '{name}' failed 3 times on {old_model} — rotated to {bot.config.model}")
                assigned_tid = getattr(bot, '_assigned_ticket_id', '')
                if assigned_tid:
                    try:
                        from codebot.ticket_engine import TicketStore, TicketState
                        store_path = STATE_DIR / "tickets.json"
                        if not store_path.exists():
                            store_path = Path(".codebot/state/tickets.json")
                        if store_path.exists():
                            ts = TicketStore(store_path)
                            ticket = ts.get(assigned_tid)
                            if ticket and ticket.state == TicketState.IMPLEMENTING:
                                ts.transition(assigned_tid, TicketState.REVIEWING)
                                logger.info(f"Bot '{name}' errored but completed ticket {assigned_tid} -> REVIEWING")
                            bot._assigned_ticket_id = ''
                    except Exception as e:
                        logger.warning(f"Failed to transition ticket {assigned_tid} on error exit: {e}")
                    claims_dir = STATE_DIR / "claims"
                    if claims_dir.exists():
                        for cf in claims_dir.glob(f"{assigned_tid}.*.json"):
                            try:
                                cf.unlink()
                            except OSError:
                                pass

            # Immediate alignment pipeline attempted above; periodic sweep remains as idempotent backstop
            base_role = name.split("-")[0] if "-" in name else name
            is_demand_driven = base_role in DECOMPOSER_ROLE_NAMES or base_role in PLANNING_ROLE_NAMES
            if exit_code == 0 and bot.config.clean_exit_wait and not is_demand_driven:
                pipeline = _get_pipeline_state()
                if _is_needed_bot(name, pipeline):
                    bot.next_run_at = now + bot.config.interval_seconds
                    logger.info(f"Bot '{name}' completed cleanly — next run in {bot.config.interval_seconds}s")
                    update_bot_state(bot, "waiting")
                else:
                    logger.info(f"Bot '{name}' completed cleanly — not needed, staying idle")
                    update_bot_state(bot, "waiting")
            else:
                pipeline = _get_pipeline_state()
                if _is_needed_bot(name, pipeline):
                    start_bot(bot, bots=bots)
                else:
                    logger.info(f"Bot '{name}' skipped respawn — not needed (pipeline ready={pipeline.get('READY',0)})")
            continue
        if not alive and bot.process is None:
            if is_draining():
                continue
            if bot.next_run_at and now < bot.next_run_at:
                continue
            if bot.next_run_at and now >= bot.next_run_at:
                pipeline = _get_pipeline_state()
                if _is_needed_bot(name, pipeline):
                    ok, why = _spawn_gate(bots=bots, is_queued=False, bot_name=name)
                    if ok:
                        logger.info(f"Bot '{name}' interval elapsed — respawning (model {bot.config.model})")
                        start_bot(bot, bots=bots)
                    else:
                        logger.info(f"Bot '{name}' interval elapsed — queued ({why})")
                        update_bot_state(bot, "queued")
                else:
                    bot.next_run_at = now + bot.config.interval_seconds
                    update_bot_state(bot, "waiting")
            continue
        if alive and is_stuck(bot):
            eff = effective_heartbeat_timeout(bot)
            hb_age = now - read_heartbeat(bot.config.name) if read_heartbeat(bot.config.name) else 0
            log_age = now - log_mtime(bot.config.name) if log_mtime(bot.config.name) else 0
            prof = model_profile(bot.config.model)
            risk = prof.lockup_risk if prof else "unknown"
            logger.warning(f"Bot '{name}' stuck (hb {hb_age:.0f}s > {eff}s, log {log_age:.0f}s silent, risk={risk}, model={bot.config.model})")
            _write_alignment_event(name, exit_code=None, exit_reason="stuck", started_at=bot.started_at)
            try:
                _run_alignment_pipeline(name)
            except Exception as e:
                logger.warning(f"Alignment pipeline failed for {name}: {e}")
            ck = read_checkpoint(name)
            if ck:
                excess = hb_age - eff
                logger.info(f"Checkpoint handoff ready for '{name}': iteration={ck.get('scan_iteration')} current={ck.get('current_task')} completed={len(ck.get('completed',[]))} excess={excess:.0f}s")
            restart_bot(bot, reason="stuck", bots=bots)
            continue
        if alive:
            update_bot_state(bot, "running")
    _log_bot_statuses(bots)
    try:
        _sweep_orphan_claims(bots)
    except Exception:
        pass
    try:
        _auto_triage_backlog()
    except Exception as e:
        logger.warning(f"Auto-triage failed: {e}")
    try:
        _auto_triage_backlog()
    except Exception as e:
        logger.warning(f"Auto-triage failed: {e}")
    try:
        _autopush_to_master()
    except Exception as e:
        logger.warning(f"Auto-push failed: {e}")
    try:
        _adaptive_schedule_gate(bots)
    except Exception as e:
        logger.warning(f"Adaptive schedule gate failed: {e}")
    try:
        from codebot.lifecycle_scheduler import dispatch_lifecycle
        from codebot.ticket_engine import TicketStore
        store_path = STATE_DIR / "tickets.json"
        if not store_path.exists():
            store_path = Path(".codebot/state/tickets.json")
        if store_path.exists():
            ts = TicketStore(store_path)
            counts = ts.summary()
            handler_registry = {
                "_auto_triage_backlog": _auto_triage_backlog,
                "_route_ready_tickets": _route_ready_tickets,
                "_dispatch_decompose_agents": _dispatch_decompose_agents,
                "_dispatch_planning_agents": _dispatch_planning_agents,
                "_dispatch_tickets_to_implementers": _dispatch_tickets_to_implementers,
                "_dispatch_tickets_to_reviewers": _dispatch_tickets_to_reviewers,
                "_gatekeeper_verify_tickets": _gatekeeper_verify_tickets,
                "_process_rework_tickets": _process_rework_tickets,
                "_recover_deferred_tickets": _recover_deferred_tickets,
            }
            dispatch_lifecycle(counts, handler_registry, bots)
            _refresh_dependency_graph()
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Lifecycle dispatch failed: {e}")
    try:
        _advance_reviewed_tickets(bots)
    except Exception as e:
        logger.warning(f"Review advance failed: {e}")
    try:
        _process_verifying_tickets()
    except Exception as e:
        logger.warning(f"VERIFYING ticket processing failed: {e}")
    try:
        from codebot.prompt_optimizer import consume_triggers
        triggers_dir = STATE_DIR / "alignment_triggers"
        roles_dir = BOTS_DIR / "codebot" / "roles"
        consumed = consume_triggers(triggers_dir, roles_dir)
        if consumed:
            logger.info(f"Prompt optimizer: evolved {consumed} role prompt(s)")
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"Prompt optimizer failed: {e}")

# ---------------------------------------------------------------------------
# Status & Control
# ---------------------------------------------------------------------------

def get_status(bots: dict[str, BotState]) -> dict:
    def external_pid(name: str) -> int | None:
        try:
            lines = subprocess.check_output(
                ["ps", "-eo", "pid=,args="], text=True, timeout=5
            ).splitlines()
        except (OSError, subprocess.SubprocessError):
            return None
        needle = f"api_runner.py {name} "
        for line in lines:
            pid_text, _, command = line.strip().partition(" ")
            if needle in command:
                try:
                    return int(pid_text)
                except ValueError:
                    return None
        return None

    status = {}
    for name, bot in bots.items():
        process_pid = bot.process.pid if bot.process is not None and bot.process.poll() is None else None
        observed_pid = process_pid or external_pid(name)
        running = observed_pid is not None
        hb_age = time.time() - read_heartbeat(bot.config.name) if read_heartbeat(bot.config.name) > 0 else None
        if hb_age is not None and hb_age < 0:
            hb_age = 0.0
        prof = model_profile(bot.config.model)
        status[name] = {
            "enabled": bot.config.enabled,
            "running": running,
            "pid": observed_pid,
            "model": bot.config.model,
            "risk": prof.lockup_risk if prof else "unknown",
            "eff_timeout": effective_heartbeat_timeout(bot),
            "heartbeat_age_seconds": round(hb_age, 1) if hb_age else None,
            "log_age_seconds": round(time.time() - log_mtime(bot.config.name), 1) if log_mtime(bot.config.name) else None,
            "next_run_in": round(bot.next_run_at - time.time(), 1) if bot.next_run_at and not running else None,
            "restart_count": bot.restart_count,
            "consecutive_errors": bot.consecutive_errors,
        }
    return status

def print_status(bots: dict[str, BotState]) -> None:
    status = get_status(bots)
    print("\n" + "=" * 90)
    print("BOT ORCHESTRATOR STATUS (model-aware)")
    print("=" * 90)
    for name, info in status.items():
        state = "RUNNING" if info["running"] else ("WAITING" if info["next_run_in"] and info["next_run_in"] > 0 else "STOPPED")
        if not info["enabled"]:
            state = "DISABLED"
        hb = f"{info['heartbeat_age_seconds']}s" if info['heartbeat_age_seconds'] else "-"
        loga = f"{info['log_age_seconds']}s" if info['log_age_seconds'] else "-"
        nxt = f"{info['next_run_in']:.0f}s" if info['next_run_in'] and info['next_run_in'] > 0 else "-"
        pid = str(info['pid']) if info['pid'] else "-"
        print(f"  {name:15s} {state:9s} PID={pid:6s} HB={hb:7s} LOG={loga:7s} NEXT={nxt:6s} eff={info['eff_timeout']:4.0f}s risk={info['risk']:11s} {info['model']}")
    print("=" * 90 + "\n")

# ---------------------------------------------------------------------------
# Main Loop
# ---------------------------------------------------------------------------

def set_drain(reason: str = "") -> None:
    DRAIN_FILE.write_text(f"{time.time()}\n{reason}\n")
    logger.info(f"Drain flag set: {reason}")


def clear_drain() -> None:
    for f in (DRAIN_FILE, UPDATE_LOCK):
        try:
            f.unlink()
        except FileNotFoundError:
            pass
    logger.info("Drain cleared")


def drain_status() -> dict:
    return {
        "draining": is_draining(),
        "drain_file": str(DRAIN_FILE) if DRAIN_FILE.exists() else None,
        "update_lock": str(UPDATE_LOCK) if UPDATE_LOCK.exists() else None,
        "drain_reason": DRAIN_FILE.read_text().strip() if DRAIN_FILE.exists() else None,
    }


def safe_stop_all(bots: dict[str, BotState], timeout_per_bot: int = 25) -> dict:
    set_drain("safe-stop requested")
    results: dict[str, str] = {}
    for name, bot in bots.items():
        if bot.process is None or bot.process.poll() is not None:
            results[name] = "already stopped"
            update_bot_state(bot, "stopped")
            continue
        logger.info(f"Safe-stopping {name} (will not respawn until drain cleared)")
        stop_bot(bot, reason="safe-stop drain")
        results[name] = "stopped"
        update_bot_state(bot, "drained")
    logger.info(f"Safe stop complete: {results}")
    return results


def backup_botnet(tag: str | None = None) -> Path:
    ts = time.strftime("%Y%m%d-%H%M%S")
    name = f"botnet-{tag}-{ts}" if tag else f"botnet-{ts}"
    dest = BACKUP_DIR / name
    dest.mkdir(parents=True, exist_ok=True)
    for p in BOTS_DIR.glob("*.md"):
        (dest / p.name).write_bytes(p.read_bytes())
    if (BOTS_DIR / "orchestrator.py").exists():
        (dest / "orchestrator.py").write_bytes((BOTS_DIR / "orchestrator.py").read_bytes())
    if (BOTS_DIR / "safe_update.sh").exists():
        (dest / "safe_update.sh").write_bytes((BOTS_DIR / "safe_update.sh").read_bytes())
    logger.info(f"Botnet backup -> {dest}")
    return dest


def restore_botnet(backup_dir: Path) -> None:
    if not backup_dir.exists():
        raise FileNotFoundError(str(backup_dir))
    for p in backup_dir.glob("*.md"):
        (BOTS_DIR / p.name).write_bytes(p.read_bytes())
    for extra in ("orchestrator.py", "safe_update.sh"):
        src = backup_dir / extra
        if src.exists():
            (BOTS_DIR / extra).write_bytes(src.read_bytes())
    logger.info(f"Restored botnet from {backup_dir}")


def main() -> None:
    """Run orchestrator. Pure controller — no bot work here."""
    import argparse
    parser = argparse.ArgumentParser(description="Bot Orchestrator")
    parser.add_argument("--status", action="store_true", help="Print status and exit")
    parser.add_argument("--stop-all", action="store_true", help="Stop all bots and exit")
    parser.add_argument("--safe-stop", action="store_true", help="Graceful drain: stop all bots and inhibit respawn until --clear-drain")
    parser.add_argument("--drain", action="store_true", help="Alias for --safe-stop")
    parser.add_argument("--clear-drain", action="store_true", help="Clear drain flag so bots may respawn")
    parser.add_argument("--drain-status", action="store_true", help="Show drain/update-lock status")
    parser.add_argument("--update", nargs="*", metavar="PATH", help="Safe update: drain, apply file(s)/dir to project root, verify, restart (or rollback)")
    parser.add_argument("--rollback", type=str, default=None, metavar="BACKUP_DIR", help="Rollback botnet from a backup dir under state/backup/")
    parser.add_argument("--start", nargs="*", help="Start specific bots (or all if none specified)")
    parser.add_argument("--preview-batch", action="store_true", help="Dry-run: print batch plan (manifest, tier, reason) without spawning")
    parser.add_argument("--check-interval", type=int, default=30, help="Health check interval (seconds)")
    args = parser.parse_args()

    if args.preview_batch and not USE_MANIFEST_SCHEDULER:
        print("preview-batch requires USE_MANIFEST_SCHEDULER=1", file=sys.stderr)
        sys.exit(2)
    if args.preview_batch:
        _preview_manifest_batches()
        return

    # Note: bots dict is populated after bootstrap in main loop below
    # For early-exit commands (--status, --drain, etc.), build from module-level registry
    bots: dict[str, BotState] = {}
    for config in BOT_REGISTRY:
        bot = BotState(config=config)
        state_file = STATE_DIR / f"{config.name}.state.json"
        if state_file.exists():
            try:
                sdata = json.loads(state_file.read_text())
                if isinstance(sdata, dict):
                    bot.consecutive_errors = sdata.get("consecutive_errors", 0)
                    bot.next_run_at = sdata.get("next_run_at", 0.0)
                    bot.restart_count = sdata.get("restart_count", 0)
            except Exception:
                pass
        bots[config.name] = bot

    if args.status:
        print_status(bots)
        if is_draining():
            print(f"DRAIN ACTIVE: {drain_status()}")
        return

    if args.drain_status:
        import pprint as _pp
        _pp.pprint(drain_status())
        print_status(bots)
        return

    if args.clear_drain:
        clear_drain()
        print("Drain cleared — respawn re-enabled.")
        return

    if args.safe_stop or args.drain:
        safe_stop_all(bots)
        print("Safe stop complete. Bots drained; will not respawn until --clear-drain.")
        print_status(bots)
        return

    if args.rollback is not None:
        bdir = Path(args.rollback)
        if not bdir.is_absolute():
            bdir = BACKUP_DIR / bdir
        set_drain(f"rollback {bdir}")
        safe_stop_all(bots)
        restore_botnet(bdir)
        if not _verify_orchestrator():
            print(f"Rollback target failed syntax check: {bdir}", file=sys.stderr)
            sys.exit(2)
        clear_drain()
        for bot in bots.values():
            if bot.config.enabled:
                bot.consecutive_errors = 0
                bot.restart_count = 0
        logger.info("Rollback complete — restarting bots")
        for bot in sorted(bots.values(),
                          key=lambda b: (TIER_PRIORITY.get(b.config.name, 2),
                                         b.config.interval_seconds)):
            if bot.config.enabled:
                start_bot(bot, bots=bots)
                time.sleep(GATEWAY_MIN_SPAWN_GAP)
        print_status(bots)
        return

    if args.update is not None:
        srcs = [Path(s) for s in args.update]
        if not srcs:
            parser.error("--update requires at least one PATH (file or directory) to apply")
        backup = backup_botnet(tag="pre-update")
        set_drain(f"update from {', '.join(str(s) for s in srcs)}")
        UPDATE_LOCK.write_text(f"{time.time()}\n")
        safe_stop_all(bots)
        try:
            for src in srcs:
                if not src.exists():
                    raise FileNotFoundError(str(src))
                if src.is_dir():
                    for p in src.rglob("*"):
                        if p.is_file():
                            rel = p.relative_to(src)
                            dest = BOTS_DIR / rel
                            dest.parent.mkdir(parents=True, exist_ok=True)
                            dest.write_bytes(p.read_bytes())
                else:
                    dest = BOTS_DIR / src.name
                    if src.resolve() != dest.resolve():
                        dest.write_bytes(src.read_bytes())
            if not _verify_orchestrator():
                raise RuntimeError("orchestrator.py syntax check failed — rolling back")
            clear_drain()
            for bot in bots.values():
                bot.consecutive_errors = 0
                bot.restart_count = 0
            for bot in sorted(bots.values(),
                               key=lambda b: (TIER_PRIORITY.get(b.config.name, 2),
                                              b.config.interval_seconds)):
                if bot.config.enabled:
                    start_bot(bot, bots=bots)
                    time.sleep(GATEWAY_MIN_SPAWN_GAP)
            print(f"Update applied from {srcs} (backup {backup}). Restarted.")
            print_status(bots)
            return
        except Exception as e:
            logger.error(f"Update failed: {e} — rolling back to {backup}")
            try:
                restore_botnet(backup)
            except Exception as re:
                logger.error(f"Rollback also failed: {re}")
            clear_drain()
            sys.exit(3)

    if args.stop_all:
        for bot in bots.values():
            stop_bot(bot, "stop-all")
        logger.info("All bots stopped")
        return

    if args.start is not None:
        if is_draining():
            print(f"Refusing --start while drain active: {drain_status()}", file=sys.stderr)
            print("Run --clear-drain first.", file=sys.stderr)
            sys.exit(4)
        targets = args.start if args.start else [c.name for c in BOT_REGISTRY]
        for name in targets:
            if name in bots and bots[name].config.enabled:
                start_bot(bots[name])
        if args.start:
            time.sleep(2)
            print_status(bots)
            return

    # WIRE-01: Bootstrap CodeBot core (incremental, fail-open).
    # When codebot_bootstrap is available, injects ProjectAdapter into all
    # core modules so path resolution goes through adapter instead of hardcoded
    # defaults. If unavailable or fails, legacy paths remain active.
    _codebot_adapter = None
    try:
        from codebot.codebot_bootstrap import bootstrap as _cb_bootstrap
        _codebot_adapter = _cb_bootstrap(BOTS_DIR)
        if _codebot_adapter:
            logger.info("CodeBot core bootstrapped: project=%s", _codebot_adapter.project_name())
    except ImportError:
        pass
    except Exception as _cb_err:
        logger.warning("CodeBot bootstrap failed (continuing with legacy): %s", _cb_err)

    # SELF-01: Reload bot registry now that adapter is injected.
    # Module-level BOT_REGISTRY was built before bootstrap ran, so it used
    # the 3-bot fallback. Now that the adapter is wired, reload from it.
    import codebot.orchestrator as _self_mod
    _self_mod.BOT_REGISTRY = _load_bot_registry()
    _rescale_registry()
    bots: dict[str, BotState] = {}
    for config in _self_mod.BOT_REGISTRY:
        bot = BotState(config=config)
        state_file = STATE_DIR / f"{config.name}.state.json"
        if state_file.exists():
            try:
                sdata = json.loads(state_file.read_text())
                if isinstance(sdata, dict):
                    bot.consecutive_errors = sdata.get("consecutive_errors", 0)
                    bot.next_run_at = sdata.get("next_run_at", 0.0)
                    bot.restart_count = sdata.get("restart_count", 0)
            except Exception:
                pass
        bots[config.name] = bot

    def shutdown_handler(signum, frame):
        logger.info("Shutdown signal received")
        for bot in bots.values():
            stop_bot(bot, "shutdown")
        logger.info("Orchestrator stopped")
        sys.exit(0)

    signal.signal(signal.SIGINT, shutdown_handler)
    signal.signal(signal.SIGTERM, shutdown_handler)

    if is_draining():
        logger.warning(f"Starting with drain active {drain_status()} — bots will remain stopped")
    else:
        _clean_stale_heartbeats()
        for bot in bots.values():
            bot._assigned_ticket_id = ''
        _apply_agent_availability(bots)
        pipeline = _get_pipeline_state()
        dynamic = _compute_dynamic_priority(pipeline)
        ready = pipeline.get("READY", 0)
        decompose = pipeline.get("DECOMPOSE", 0)
        implementing = pipeline.get("IMPLEMENTING", 0)
        verifying = pipeline.get("VERIFYING", 0)
        discovered = pipeline.get("DISCOVERED", 0) + pipeline.get("TRIAGED", 0)
        planning = pipeline.get("PLANNING", 0)
        logger.info(f"Overture: pipeline ready={ready} decomp={decompose} plan={planning} impl={implementing} verify={verifying} disc={discovered}")
        order = sorted(
            [b for b in bots.values() if b.config.enabled],
            key=lambda b: (dynamic.get(b.config.name, TIER_PRIORITY.get(b.config.name, 2)), b.config.interval_seconds),
        )
        demand_driven_roles = frozenset({"decomposer", "implementation_planner"})
        needed = []
        for bot in order:
            base = bot.config.name.split("-")[0] if "-" in bot.config.name else bot.config.name
            if base in demand_driven_roles:
                continue
            if _is_needed_bot(bot.config.name, pipeline):
                needed.append(bot)
        logger.info(f"Overture: {len(needed)}/{len(order)} bots needed, staggered start ({_SPAWN_STAGGER_SECONDS}s interval)")
        for bot in needed:
            start_bot(bot, bots=bots, is_overture=True)
            time.sleep(_SPAWN_STAGGER_SECONDS)

    logger.info("Orchestrator starting")
    logger.info(f"Health check every {args.check_interval}s")
    last_alignment_run = time.time()
    alignment_interval = 1800  # 30 minutes
    while True:
        try:
            check_all_bots(bots)
            time.sleep(args.check_interval)
            if time.time() - last_alignment_run >= alignment_interval:
                _run_alignment_pipeline_for_all()
                last_alignment_run = time.time()
        except KeyboardInterrupt:
            shutdown_handler(None, None)
        except Exception as e:
            logger.error(f"Health check error: {e}")
            time.sleep(args.check_interval)

if __name__ == "__main__":
    main()
