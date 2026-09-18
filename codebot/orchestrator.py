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

import fcntl
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
from typing import Iterator, Optional

# ---------------------------------------------------------------------------
# Paths — resolved via ProjectAdapter; fallback to CODEBOT_PROJECT_ROOT env or cwd
# ---------------------------------------------------------------------------

_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))

BOTS_DIR = _project_root
STATE_DIR = _project_root / "state"
LOGS_DIR = _project_root / "logs"
BACKUP_DIR = _project_root / "state" / "backup"
_QUEUE_SNAPSHOT: tuple[Path, float, str] | None = None
_MANIFEST_SNAPSHOT: tuple[Path, tuple[tuple[str, float], ...], dict[str, dict]] | None = None
_CHECKPOINT_SNAPSHOTS: dict[Path, tuple[float, list[str] | int]] = {}
ALIGNMENT_EVENTS_DIR = STATE_DIR / "alignment_events"

# T4.3 incremental adapter seam: when a ProjectAdapter is provided, its paths
# override the defaults above.
_adapter_instance: Any = None

try:
    from typing import Any as _Any
except ImportError:
    _Any = object  # type: ignore[misc,assignment]


def set_project_adapter(adapter: Any) -> None:
    global _adapter_instance, BOTS_DIR, STATE_DIR, LOGS_DIR, BACKUP_DIR, ALIGNMENT_EVENTS_DIR, DRAIN_FILE, UPDATE_LOCK
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
    GATEWAY_MIN_SPAWN_GAP = int(os.getenv("CODEBOT_MIN_SPAWN_GAP", "20"))
GATEWAY_MAX_CONCURRENT = int(os.getenv("CODEBOT_MAX_CONCURRENT", "48"))

CODEBOT_MIN_MEMORY_MB = int(os.getenv("CODEBOT_MIN_MEMORY_MB", "30"))
MAX_THINKING_CONCURRENT = int(os.getenv("CODEBOT_MAX_THINKING_CONCURRENT", "10"))
MAX_EXPENSIVE_CONCURRENT = int(os.getenv("CODEBOT_MAX_EXPENSIVE_CONCURRENT", "8"))
MAX_QWEN_38_CONCURRENT = int(os.getenv("CODEBOT_MAX_QWEN_38", "6"))
MAX_IMPLEMENTER_SLOTS = int(os.getenv("CODEBOT_MAX_IMPLEMENTERS", "20"))
MAX_NON_IMPLEMENTER_SLOTS = int(os.getenv("CODEBOT_MAX_NON_IMPLEMENTERS", "7"))
MAX_DISCOVERY_SLOTS = int(os.getenv("CODEBOT_MAX_DISCOVERY", "1"))
USE_MANIFEST_SCHEDULER = os.getenv("CODEBOT_MANIFEST_SCHEDULER", "0") == "1"

IMPLEMENTER_ROLE_NAMES: frozenset[str] = frozenset({
    "general_implementer", "backend_implementer", "frontend_implementer",
    "test_implementer", "migration_implementer", "documentation_implementer",
})

DISCOVERY_ROLE_NAMES: frozenset[str] = frozenset({
    "bug_hunter", "security_auditor", "architecture_auditor", "performance_auditor",
    "test_gap_auditor", "documentation_auditor", "dependency_auditor", "ux_auditor",
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

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

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
MODEL_TIER_EXPENSIVE = frozenset({"qwen-3.8-max", "qwen-3.8-max-thinking"})
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
    "issues": 11, "bug_triage": 11, "build": 11, "github_bot": 11, "feature_decomposer": 11,
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
            return max(len(ts.list_ready()), 0)
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
    non_impl = [c for c in registry if c.name not in IMPLEMENTER_ROLE_NAMES]
    base_impl = [c for c in registry if c.name in IMPLEMENTER_ROLE_NAMES]
    if not base_impl:
        return registry
    target = min(demand, MAX_IMPLEMENTER_SLOTS, max_concurrent - len(non_impl))
    target = max(target, len(base_impl))
    role_map = {c.name: c for c in base_impl}
    ticket_classes = _peek_ticket_classes()
    out = list(non_impl)
    name_counts: dict[str, int] = {}
    for i in range(target):
        tc_val = ticket_classes[i] if i < len(ticket_classes) else "feature"
        base_name = TICKET_CLASS_TO_IMPLEMENTER.get(tc_val, "general_implementer")
        role = role_map.get(base_name, base_impl[0])
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
    return out


WORKER_MODELS_NON_THINKING = (
    "xiaomi-mimo-2.5", "qwen-3.7-plus", "qwen-3.8-max",
)

WORKER_MODELS_THINKING = ()

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
_thinking_worker_count = 0


def _next_worker_model() -> tuple[str, str]:
    global _model_rotation_index, _thinking_worker_count
    if WORKER_MODELS_THINKING and _thinking_worker_count < max(1, MAX_THINKING_CONCURRENT // 2):
        pool = WORKER_MODELS_THINKING
        idx = _thinking_worker_count % len(pool)
        model = pool[idx]
        _thinking_worker_count += 1
    else:
        pool = WORKER_MODELS_NON_THINKING
        model = pool[_model_rotation_index % len(pool)]
        _model_rotation_index += 1
    fallback = _MODEL_FALLBACKS.get(model, "xiaomi-mimo-2.5")
    return model, fallback


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


CLAIM_TTL_SECONDS = 7200


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


def _sweep_orphan_claims(bots: dict[str, BotState]) -> int:
    """Delete claim files whose owning bot process is dead or timed out.

    Prevents permanently locked QUEUE items when a worker crashes mid-task
    without releasing its claim. Runs once per health-check cycle.
    """
    swept = 0
    claims_dir = STATE_DIR / "claims"
    if not claims_dir.exists():
        return 0
    now = time.time()
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
        lines.append("--- END TICKET CONTEXT ---")
        return "\n".join(lines)
    except Exception:
        return ""


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
    ready = ts.list_ready()
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
        except Exception:
            pass
        bot._assigned_ticket_id = tid
        dispatched += 1
        logger.info(f"Dispatched ticket {tid} ({tc_val}) -> {bot_name}")
    return dispatched


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
        tmp = _METRICS_PATH.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2)[:50000], encoding="utf-8")
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


def _count_running_by_category(bots: dict[str, BotState] | None) -> tuple[int, int, int]:
    implementers = 0
    discovery = 0
    other = 0
    if not bots:
        return implementers, discovery, other
    for name, bot in bots.items():
        if bot.process is None or bot.process.poll() is not None:
            continue
        if name in IMPLEMENTER_ROLE_NAMES:
            implementers += 1
        elif name in DISCOVERY_ROLE_NAMES:
            discovery += 1
        else:
            other += 1
    return implementers, discovery, other


def _spawn_gate(bots: dict[str, BotState] | None = None, is_queued: bool = False, runner_mode: str = "api", bot_model: str = "", bot_name: str = "", is_overture: bool = False) -> tuple[bool, str]:
    now = time.time()
    running = _count_api_runner_processes()
    if bots is not None and bot_model:
        model_counts = _count_running_by_model(bots)
        if "thinking" in bot_model:
            thinking_count = sum(count for model, count in model_counts.items() if "thinking" in model)
            if thinking_count >= MAX_THINKING_CONCURRENT:
                return False, f"thinking cap {thinking_count}/{MAX_THINKING_CONCURRENT}"
        if bot_model == "qwen-3.8-max":
            if model_counts.get("qwen-3.8-max", 0) >= MAX_QWEN_38_CONCURRENT:
                return False, f"qwen-3.8-max cap {model_counts['qwen-3.8-max']}/{MAX_QWEN_38_CONCURRENT}"
    if bot_name in WORKER_POOL:
        reserved = worker_reserved_slots(GATEWAY_MAX_CONCURRENT)
        if _count_running_workers(bots) >= reserved:
            return False, f"worker pool full ({reserved}/{reserved})"
    elif bots is not None:
        non_workers = {n: b for n, b in bots.items() if n not in WORKER_POOL}
        running_non = sum(1 for b in non_workers.values()
                          if b.process is not None and b.process.poll() is None)
        slots = rotating_slots(GATEWAY_MAX_CONCURRENT)
        if running_non >= slots:
            return False, f"rotating slots full ({slots}/{slots})"
    impl_running, discovery_running, other_running = _count_running_by_category(bots)
    if bot_name in IMPLEMENTER_ROLE_NAMES:
        if impl_running >= MAX_IMPLEMENTER_SLOTS:
            return False, f"implementer slots full ({impl_running}/{MAX_IMPLEMENTER_SLOTS})"
    elif bot_name in DISCOVERY_ROLE_NAMES:
        if discovery_running >= MAX_DISCOVERY_SLOTS:
            return False, f"discovery slots full ({discovery_running}/{MAX_DISCOVERY_SLOTS})"
    else:
        if other_running >= MAX_NON_IMPLEMENTER_SLOTS:
            return False, f"non-implementer slots full ({other_running}/{MAX_NON_IMPLEMENTER_SLOTS})"
    cap = GATEWAY_MAX_CONCURRENT
    if running >= cap:
        return False, f"cap {running}/{cap} running"
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
    if not is_overture:
        if bot_name in WORKER_POOL:
            if gap < 0:
                return False, f"gap {gap:.1f}s<0s"
        elif gap < GATEWAY_MIN_SPAWN_GAP:
            return False, f"gap {gap:.0f}s<{GATEWAY_MIN_SPAWN_GAP}s"
    return True, "slot available"


def start_bot(bot: BotState, resume_checkpoint: bool = True, checkpoint_reason: str | None = None, bots: dict[str, BotState] | None = None, is_overture: bool = False) -> bool:
    """Spawn a bot as a subprocess. Returns True on success."""
    if (STATE_DIR / f"{bot.config.name}.paused").exists():
        update_bot_state(bot, "paused")
        return False
    if bot.config.name in WORKER_POOL:
        rotated_model, rotated_fallback = _next_worker_model()
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
                    ipath = BOTS_DIR.parent / ipath
                if ipath.exists() and ipath.stat().st_mtime > last_run_mtime:
                    inputs_changed = True
                    break
    except Exception:
        inputs_changed = True
    prompt_path = BOTS_DIR / bot.config.prompt_file
    if prompt_path.exists() and prompt_path.stat().st_mtime > last_run_mtime:
        inputs_changed = True
    ALWAYS_RESPAWN = frozenset({
        "github_bot", "issues", "features", "bug_triage",
        "goal_steering", "ui_improve", "doc_sync", "test_coverage",
        "code_quality", "prompt_opt", "dependency", "build",
        "e2e_smoke", "security_auditor", "feature_decomposer",
    })
    if not inputs_changed and last_run_mtime > 0 and bot.config.name not in WORKER_POOL and bot.config.name not in ALWAYS_RESPAWN:
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
    if assigned_tid:
        ticket_ctx = _load_ticket_context(assigned_tid)
        if ticket_ctx:
            prompt_text = f"{prompt_text}\n\n{ticket_ctx}"
    try:
        bot.prompt_mtime = prompt_file.stat().st_mtime
    except OSError:
        bot.prompt_mtime = 0.0
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
            f"- Before each atomic task, check if `state/.drain` or `state/.update_lock` exists via `read` tool. If either exists, exit 0.\n"
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
    if not p.exists():
        return None
    try:
        raw = p.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        logger.warning(f"Checkpoint corrupt for '{bot_name}': {exc} — deleting and starting fresh")
        try:
            p.unlink()
        except OSError:
            pass
        return None
    except Exception:
        return None
    if not isinstance(data, dict):
        logger.warning(f"Checkpoint for '{bot_name}' is not a JSON object — deleting")
        try:
            p.unlink()
        except OSError:
            pass
        return None
    if len(raw.encode("utf-8")) > 4096:
        logger.warning(f"Checkpoint for '{bot_name}' exceeds 4KB ({len(raw.encode('utf-8'))} bytes) — truncating")
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
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


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
        logger.info(f"Escalation L3 for '{name}': requesting task split via feature_decomposer")
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


def _run_alignment_pipeline(bot_name: str, timeout: int = 120) -> bool:
    """Run alignment scoring + prompt optimization synchronously for a bot.

    Called after bot exit. Blocks until complete or timeout. Returns True if
    prompt optimization was triggered (bot should wait for next run).
    """
    try:
        from codebot.rl_engine import (
            list_pending_events,
            score_event,
            reward_from_score,
            record_event_reward,
            mark_event_processed,
            write_trigger,
            load_rl_state,
            save_rl_state,
            ensure_bot,
        )
    except ImportError:
        try:
            from rl_engine import (
                list_pending_events,
                score_event,
                reward_from_score,
                record_event_reward,
                mark_event_processed,
                write_trigger,
                load_rl_state,
                save_rl_state,
                ensure_bot,
            )
        except ImportError:
            logger.warning("rl_engine not available, skipping alignment pipeline")
            return False

    event_file = ALIGNMENT_EVENTS_DIR / f"{bot_name}.exit.json"
    if not event_file.exists():
        return False

    try:
        event = json.loads(event_file.read_text())
    except Exception:
        return False

    if event.get("processed"):
        return False

    # Self-target guard: never trigger optimization on prompt_opt itself
    if bot_name == "prompt_opt":
        mark_event_processed(event_file, event, 0, 0.5, "self_target")
        return False

    start_time = time.time()
    logger.info(f"Alignment pipeline: scoring {bot_name} exit")

    try:
        score_result = score_event(event)
        metrics_entry: dict = {}
        try:
            from codebot import metrics_collector as _mc  # type: ignore
        except ImportError:
            try:
                import metrics_collector as _mc  # type: ignore
            except ImportError:
                _mc = None  # type: ignore
        if _mc is not None:
            try:
                _snap = _mc.collect_all()
                _bots = _snap.get("bots") or {}
                if isinstance(_bots.get(bot_name), dict):
                    metrics_entry = _bots[bot_name]
            except Exception:
                metrics_entry = {}
        reward = reward_from_score(
            score_result["score"],
            event.get("exit_reason", "unknown"),
            score_result.get("rebellion_count", 0),
            metrics=metrics_entry or None,
        )
        rl = load_rl_state()
        record_event_reward(
            rl, bot_name, reward, score_result["score"],
            event.get("exit_code"), event.get("exit_reason", "clean"),
        )
        save_rl_state(rl)
        bot_state = ensure_bot(rl, bot_name)

        if reward < 0.6:
            write_trigger(
                bot_name, score_result["score"], reward,
                score_result.get("verdict", "misaligned"),
                score_result.get("evidence", "low reward"),
                score_result.get("breakdown", {}),
                event, bot_state,
            )
            logger.info(f"Alignment pipeline: {bot_name} reward={reward:.2f} < 0.6, trigger written")
            mark_event_processed(
                event_file, event,
                score_result["score"], reward,
                score_result.get("verdict", "misaligned"),
            )
            return True

        mark_event_processed(
            event_file, event,
            score_result["score"], reward,
            score_result.get("verdict", "aligned"),
        )
        logger.info(f"Alignment pipeline: {bot_name} reward={reward:.2f} >= 0.6, no trigger needed")
        return False

    except Exception as e:
        logger.error(f"Alignment pipeline error for {bot_name}: {e}")
        return False


def _run_alignment_pipeline_for_all() -> None:
    """Run alignment scoring for all pending events (called every 30 minutes)."""
    try:
        from rl_engine import list_pending_events
    except ImportError:
        return

    pending = list_pending_events()
    if not pending:
        return

    logger.info(f"Alignment pipeline: processing {len(pending)} pending events")
    for event_file, _event_data in pending:
        bot_name = event_file.stem.replace(".exit", "")
        _run_alignment_pipeline(bot_name, timeout=120)
    
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


def due_bots_first(bots: dict[str, BotState]) -> list[str]:
    now = time.time()
    due = [n for n, b in bots.items()
           if b.config.enabled and b.process is None
           and b.next_run_at and now >= b.next_run_at]
    due.sort(key=lambda n: (TIER_PRIORITY.get(n, 2), bots[n].next_run_at))
    # Worker pool uses WORKER_BOT.md (homogeneous); complexity routing is soft
    # affinity in the prompt, not a hard scheduler gate.
    worker_pool = WORKER_POOL
    queue_path = Path(__file__).parent.parent / "docs" / "triage" / "QUEUE.md"
    complexity_map = _parse_queue_complexity(queue_path)
    if not complexity_map:
        rest = [n for n in bots if n not in due]
        return due + rest
    has_worker_due = any(n in worker_pool for n in due)
    if not has_worker_due:
        rest = [n for n in bots if n not in due]
        return due + rest
    available = set(complexity_map.values())
    filtered_due: list[str] = []
    for name in due:
        if name in worker_pool:
            if available:
                filtered_due.append(name)
        else:
            filtered_due.append(name)
    rest = [n for n in bots if n not in filtered_due]
    return filtered_due + rest

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
    cand2 = BOTS_DIR.parent / rel_path
    if cand2.exists():
        return cand2
    # Fallback to BOTS_DIR-relative (most common for bots/state/*)
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
        BOTS_DIR.parent / "docs" / "triage" / "QUEUE.md",
        BOTS_DIR / "QUEUE.md",
        BOTS_DIR / "docs" / "triage" / "QUEUE.md",
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
    # Also probe parent state dir for cross-cwd runs
    candidates = [ckpt, BOTS_DIR.parent / "state" / f"{name}.checkpoint.json"]
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
            # Fallback for Work cwd runs where import is bots.*
            alt = BOTS_DIR.parent / "bots" / "manifests"
            if alt.exists():
                mdir = alt
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
    packed = _pb(ready_ordered, max_per_batch=5, max_batches=2, budget_state=budget_state, stagger_s=20)
    # Apply model/slot caps
    try:
        capped = _ac(packed, thinking_cap=3, qwen38max_cap=2, slot_cap=10)
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

    queue_path = BOTS_DIR.parent / "docs" / "triage" / "QUEUE.md"
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
            if exit_code == 0:
                bot.consecutive_errors = 0
            else:
                bot.consecutive_errors += 1
                if bot.consecutive_errors >= 3:
                    logger.error(f"Bot '{name}' failed 3 times — disabling (model {bot.config.model})")
                    bot.config.enabled = False
                    update_bot_state(bot, "disabled")
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
    # Dispatch READY tickets to idle implementers by class
    try:
        _dispatch_tickets_to_implementers(bots)
    except Exception as e:
        logger.warning(f"Ticket dispatch failed: {e}")
    # Manifest readiness / ordering / packing / dispatch
    queue_text = _manifest_load_queue_text()
    ready, skipped, considered = _collect_manifest_readiness(bots, now, queue_text=queue_text)
    packed = _plan_manifest_batches(ready, bots)
    batches = packed.get("batches", []) if isinstance(packed, dict) else []
    dropped = packed.get("dropped", []) if isinstance(packed, dict) else []
    all_skipped: list[tuple[str, str]] = list(skipped)
    for d in dropped:
        if isinstance(d, dict):
            all_skipped.append((d.get("name", "?"), d.get("reason", "dropped")))
    batched_count = sum(len(b) for b in batches) if isinstance(batches, list) else 0
    # Per-tick reasoning log (required by acceptance)
    logger.info(f"tick: considered={considered} ready={len(ready)} batched={batched_count} skipped={all_skipped}")
    try:
        import subprocess as _sp
        _sp.run(["python3", str(BOTS_DIR / "metrics_collector.py")], timeout=60, capture_output=True)
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
        print("manifest scheduler preview: no manifests loaded (check bots/manifests/*.json)")
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


def check_all_bots(bots: dict[str, BotState]) -> None:
    if USE_MANIFEST_SCHEDULER:
        return _check_all_bots_manifest(bots)
    rotate_logs()
    if is_draining():
        for bot in bots.values():
            if bot.process is not None and bot.process.poll() is None:
                continue
            if bot.process is not None:
                update_bot_state(bot, "drained")
        return
    now = time.time()

    for name, bot in bots.items():
        if not bot.config.enabled:
            continue
        if bot.process is not None and bot.process.poll() is None:
            continue
        if _is_queued(bot):
            ok, why = _spawn_gate(bots=bots, is_queued=True)
            if ok:
                logger.info(f"Dequeuing '{name}' — slot available")
                update_bot_state(bot, "starting")
                start_bot(bot, bots=bots)

    for name in due_bots_first(bots):
        bot = bots[name]
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
            if exit_code == 0:
                bot.consecutive_errors = 0
            else:
                bot.consecutive_errors += 1
                if bot.consecutive_errors >= 3:
                    logger.error(f"Bot '{name}' failed 3 times — disabling (model {bot.config.model})")
                    bot.config.enabled = False
                    update_bot_state(bot, "disabled")
                    continue

            # Immediate alignment pipeline attempted above; periodic sweep remains as idempotent backstop
            if exit_code == 0 and bot.config.clean_exit_wait:
                bot.next_run_at = now + bot.config.interval_seconds
                logger.info(f"Bot '{name}' completed cleanly — next run in {bot.config.interval_seconds}s")
                update_bot_state(bot, "waiting")
            else:
                start_bot(bot, bots=bots)
            continue
        if not alive and bot.process is None:
            if is_draining():
                continue
            if bot.next_run_at and now < bot.next_run_at:
                continue
            if bot.next_run_at and now >= bot.next_run_at:
                logger.info(f"Bot '{name}' interval elapsed — respawning (model {bot.config.model})")
                start_bot(bot, bots=bots)
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
        _dispatch_tickets_to_implementers(bots)
    except Exception as e:
        logger.warning(f"Ticket dispatch failed: {e}")

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
    parser.add_argument("--update", nargs="*", metavar="PATH", help="Safe update: drain, apply file(s)/dir to ~/Work/bots/, verify, restart (or rollback)")
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
        bots[config.name] = BotState(config=config)

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
        bots[config.name] = BotState(config=config)

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
        order = sorted(
            [b for b in bots.values() if b.config.enabled],
            key=lambda b: (TIER_PRIORITY.get(b.config.name, 2), b.config.interval_seconds),
        )
        logger.info(f"Overture: {len(order)} bots tier-ordered, immediate start")
        for bot in order:
            start_bot(bot, bots=bots, is_overture=True)

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
