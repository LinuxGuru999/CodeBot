#!/usr/bin/env python3
"""Ticket Dispatcher — Ticket-to-bot matching and claim management.

Purpose
-------
Handles dispatching tickets to implementers, reviewers, planners, and decomposers.
Manages claim files to prevent duplicate work.
Routes tickets through the pipeline states (READY -> DECOMPOSE -> PLANNING -> IMPLEMENTING -> REVIEWING -> VERIFYING -> COMPLETE).

Why
---
Extracted from orchestrator.py to maintain architectural boundaries.
The orchestrator should only coordinate, not manage ticket routing logic.

Invariants
----------
- Atomic writes for claim files
- Claims are released when bots finish or timeout
- Only one bot works on a ticket at a time per role type
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Paths resolved relative to the package root
_CODEBOT_PKG_DIR = Path(__file__).parent
_project_root = _CODEBOT_PKG_DIR.parent
STATE_DIR = _project_root / ".codebot" / "state"
LOGS_DIR = _project_root / ".codebot" / "logs"
BOTS_DIR = _project_root

# Ensure directories exist
STATE_DIR.mkdir(parents=True, exist_ok=True)
LOGS_DIR.mkdir(parents=True, exist_ok=True)

# Role name sets
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

DECOMPOSER_ROLE_NAMES: frozenset[str] = frozenset({
    "decomposer",
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

CLAIM_TTL_SECONDS = 1800
SWEEP_INTERVAL = 300
_last_sweep_time: float = 0.0

DEMAND_STAGGER_SECONDS = 1.0
REVIEWER_TYPES = (
    "correctness_reviewer", "security_reviewer", "architecture_reviewer",
    "test_reviewer", "performance_reviewer", "simplicity_reviewer",
    "documentation_reviewer",
)

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

_MODEL_FALLBACKS = {
    "qwen-3.8-max": "qwen-3.7-plus",
    "qwen-3.8-max-thinking": "qwen-3.7-max-thinking",
    "qwen-3.7-max": "qwen-3.6-plus",
    "qwen-3.7-max-thinking": "qwen-3.7-plus",
    "qwen-3.7-plus": "xiaomi-mimo-2.5",
    "qwen-3.6-plus": "xiaomi-mimo-2.5",
    "qwen-3.6-plus-thinking": "qwen-3.7-max-thinking",
    "qwen-3.5-plus": "xiaomi-mimo-2.5",
    "qwen-3.5-plus-thinking": "qwen-3.7-max-thinking",
    "meta-muse-spark-1.3": "qwen-3.6-plus",
    "meta-muse-spark-1.2": "qwen-3.5-plus",
    "xiaomi-mimo-2.5": "qwen-3.5-plus",
}

MODEL_TIER_EXPENSIVE = frozenset({"qwen-3.8-max", "qwen-3.8-max-thinking", "qwen-3.7-max", "qwen-3.7-max-thinking"})

_model_rotation_index = 0


def _get_ticket_store():
    """Get TicketStore instance with caching."""
    try:
        from codebot.ticket_engine import TicketStore
    except ImportError:
        return None

    store_path = STATE_DIR / "tickets.json"
    if not store_path.exists():
        store_path = Path(".codebot/state/tickets.json")
    if not store_path.exists():
        return None

    try:
        return TicketStore(store_path)
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Claim Management
# ---------------------------------------------------------------------------

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


def _sweep_orphan_claims(bots: dict[str, Any]) -> int:
    """Delete claim files whose owning bot process is dead or timed out."""
    global _last_sweep_time
    now = time.time()

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
            worker = data.get("worker", data.get("bot", ""))
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


# ---------------------------------------------------------------------------
# Ticket Dispatching
# ---------------------------------------------------------------------------

def spawn_demand_agents(bots: dict[str, Any], max_concurrent: int, start_bot_fn) -> int:
    """Spawn agents based on current ticket demand.
    
    Args:
        bots: Dict of bot name to BotState
        max_concurrent: Maximum concurrent bots allowed
        start_bot_fn: Function to start a bot (injected from orchestrator)
    
    Returns:
        Number of agents spawned
    """
    try:
        from codebot.ticket_engine import TicketState
    except ImportError:
        return 0
    
    ts = _get_ticket_store()
    if ts is None:
        return 0

    implementing = ts.list_by_state(TicketState.IMPLEMENTING)
    reviewing = ts.list_by_state(TicketState.REVIEWING)
    rework_tickets = ts.list_by_state(TicketState.REWORK)

    running_count = sum(
        1 for b in bots.values()
        if b.process is not None and b.process.poll() is None
    )
    budget = max(0, max_concurrent - running_count)
    if budget <= 0:
        return 0

    claims_dir = STATE_DIR / "claims"
    claims_dir.mkdir(parents=True, exist_ok=True)
    active_claims: set[str] = set()
    alive_bots = {name for name, b in bots.items() if b.process is not None and b.process.poll() is None}
    for p in claims_dir.glob("*.json"):
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            worker = data.get("bot", data.get("worker", ""))
            if worker in alive_bots:
                active_claims.add(p.stem.rsplit(".", 1)[0])
            else:
                try:
                    p.unlink()
                except OSError:
                    pass
        except Exception:
            pass

    spawned = 0
    global _model_rotation_index

    def _get_or_create_bot(base_name: str, suffix: str = "") -> Any:
        from codebot.process_manager import BotConfig, BotState
        bot_name = f"{base_name}-{suffix}" if suffix else base_name
        if bot_name in bots:
            bot = bots[bot_name]
            if not bot.config.enabled:
                bot.config.enabled = True
            return bot
        prompt_file = f"codebot/roles/{base_name}.md"
        prompt_path = BOTS_DIR / prompt_file
        if not prompt_path.exists():
            prompt_file = f"{base_name}.md"
            prompt_path = BOTS_DIR / prompt_file
            if not prompt_path.exists():
                return None
        global _model_rotation_index
        idx = _model_rotation_index % len(WORKER_MODEL_CYCLE)
        model = WORKER_MODEL_CYCLE[idx]
        fb = WORKER_FALLBACK_CYCLE[idx] if idx < len(WORKER_FALLBACK_CYCLE) else _MODEL_FALLBACKS.get(model, "xiaomi-mimo-2.5")
        _model_rotation_index += 1
        tier = 13 if model in MODEL_TIER_EXPENSIVE or "thinking" in model else 12
        cfg = BotConfig(
            name=bot_name,
            prompt_file=prompt_file,
            interval_seconds=30,
            heartbeat_timeout=90,
            model=model,
            fallback_model=fb,
            enabled=True,
            clean_exit_wait=False,
            runner_mode="api",
            tier=tier,
            max_restarts=5,
        )
        state = BotState(config=cfg)
        bots[bot_name] = state
        return state

    def _is_idle(bot: Any) -> bool:
        return bot.process is None or bot.process.poll() is not None

    def _assign_and_spawn(bot: Any, tid: str) -> bool:
        claim_file = claims_dir / f"{tid}.{bot.config.name}.json"
        if claim_file.exists():
            return False
        try:
            claim_data = {"ticket_id": tid, "bot": bot.config.name, "worker": bot.config.name, "at": time.time()}
            tmp = claim_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(claim_data), encoding="utf-8")
            tmp.replace(claim_file)
        except OSError:
            return False
        bot._assigned_ticket_id = tid
        ok = start_bot_fn(bot, bots=bots, is_demand=True)
        if ok:
            nonlocal spawned
            spawned += 1
            logger.info(f"Demand spawn: {bot.config.name} -> ticket {tid}")
            time.sleep(DEMAND_STAGGER_SECONDS)
            return True
        else:
            logger.warning(f"Demand spawn FAILED: {bot.config.name} -> ticket {tid}")
            try:
                claim_file.unlink(missing_ok=True)
            except OSError:
                pass
            bot._assigned_ticket_id = ""
            return False

    # Dispatch implementers — O(n+m) via role-indexed lookup
    impl_all = list(implementing) + list(rework_tickets)
    max_concurrent_impl = 8

    # Build role-indexed dictionary: O(m) where m = number of bots
    idle_bots_by_role: dict[str, list[Any]] = {}
    all_bots_by_role: dict[str, list[str]] = {}
    running_impl = 0
    for name, b in bots.items():
        base = name.split("-")[0] if "-" in name else name
        all_bots_by_role.setdefault(base, []).append(name)
        if b.process is not None and b.process.poll() is None:
            if base in IMPLEMENTER_ROLE_NAMES:
                running_impl += 1
        else:
            if b.config.enabled and not getattr(b, "_assigned_ticket_id", ""):
                idle_bots_by_role.setdefault(base, []).append(b)

    impl_budget = min(len(impl_all), budget, max(0, max_concurrent_impl - running_impl))

    for ticket in impl_all[:impl_budget]:
        if spawned >= impl_budget:
            break
        tid = getattr(ticket, "id", "")
        if not tid or tid in active_claims:
            continue
        tc = getattr(ticket, "ticket_class", None)
        tc_val = tc.value if hasattr(tc, "value") else str(tc) if tc else "feature"
        target_base = TICKET_CLASS_TO_IMPLEMENTER.get(tc_val, "general_implementer")

        assigned = False
        # O(1) average-case lookup by role instead of O(m) scan
        candidates = idle_bots_by_role.get(target_base, [])
        while candidates:
            bot = candidates.pop(0)
            if not bot.config.enabled or getattr(bot, "_assigned_ticket_id", ""):
                continue
            if _assign_and_spawn(bot, tid):
                assigned = True
                active_claims.add(tid)
                break
        if assigned:
            continue

        existing = all_bots_by_role.get(target_base, [])
        suffix = str(len(existing) + 1) if existing else ""
        bot = _get_or_create_bot(target_base, suffix)
        if bot and _is_idle(bot):
            if _assign_and_spawn(bot, tid):
                active_claims.add(tid)

    budget -= spawned
    if budget <= 0:
        return spawned

    # Dispatch reviewers — O(n+m) via role-indexed lookup
    max_concurrent_reviewers = 8

    # Reuse idle_bots_by_role built above; also build reviewer-specific indexes: O(m)
    idle_reviewers_by_role: dict[str, list[Any]] = {}
    all_reviewers_by_role: dict[str, list[str]] = {}
    running_reviewers = 0
    for name, b in bots.items():
        base = name.split("-")[0] if "-" in name else name
        if base in REVIEWER_ROLE_NAMES or name == "ux_reviewer":
            all_reviewers_by_role.setdefault(base, []).append(name)
            if b.process is not None and b.process.poll() is None:
                running_reviewers += 1
            elif b.config.enabled and not getattr(b, "_assigned_ticket_id", ""):
                idle_reviewers_by_role.setdefault(base, []).append(b)

    review_budget = min(len(reviewing) * len(REVIEWER_TYPES), budget, max(0, max_concurrent_reviewers - running_reviewers))
    reviewer_spawned = 0

    for ticket in reviewing:
        if reviewer_spawned >= review_budget:
            break
        tid = getattr(ticket, "id", "")
        if not tid or tid in active_claims:
            continue
        for rtype in REVIEWER_TYPES:
            if reviewer_spawned >= review_budget:
                break
            assigned = False
            # O(1) average-case lookup by role instead of O(m) scan
            candidates = idle_reviewers_by_role.get(rtype, [])
            while candidates:
                bot = candidates.pop(0)
                if not bot.config.enabled or getattr(bot, "_assigned_ticket_id", ""):
                    continue
                if _assign_and_spawn(bot, tid):
                    assigned = True
                    reviewer_spawned += 1
                    break
            if assigned:
                continue

            existing = all_reviewers_by_role.get(rtype, [])
            suffix = str(len(existing) + 1) if existing else ""
            bot = _get_or_create_bot(rtype, suffix)
            if bot and _is_idle(bot):
                if _assign_and_spawn(bot, tid):
                    reviewer_spawned += 1

    return spawned + reviewer_spawned


def dispatch_decompose_agents(bots: dict[str, Any], max_agents: int = 0, start_bot_fn=None) -> int:
    """Dispatch DECOMPOSE tickets to decomposer agents."""
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
        if base_name not in DECOMPOSER_ROLE_NAMES:
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

    # Phase 1: Collect transitions for completed decompositions and dispatch new ones
    transitions: list[tuple[str, Any, list[dict] | None]] = []
    transition_tids: list[str] = []
    transition_logs: list[str] = []

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
            transitions.append((tid, TicketState.PLANNING, None))
            transition_tids.append(tid)
            sub_count = len(artifact.get('sub_tickets', []))
            transition_logs.append(f"Decomposition complete: {tid} DECOMPOSE -> PLANNING (sub_tickets={sub_count})")
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
            if start_bot_fn:
                start_bot_fn(bot, bots=bots)

    # Phase 2: Apply all decomposition-complete transitions in a single batch
    if transitions:
        try:
            results = ts.batch_transition(transitions)
            dispatched += len(results)
            for msg in transition_logs:
                logger.info(msg)
            for tid in transition_tids:
                for cf in claims_dir.glob(f"{tid}.*.json"):
                    try:
                        cf.unlink()
                    except OSError:
                        pass
        except (ValueError, KeyError) as e:
            logger.warning(f"Batch decompose transition failed ({e}), falling back to individual")
            for i, (tid, target_state, fb) in enumerate(transitions):
                try:
                    ts.transition(tid, target_state, fb)
                    logger.info(transition_logs[i])
                    dispatched += 1
                    for cf in claims_dir.glob(f"{tid}.*.json"):
                        try:
                            cf.unlink()
                        except OSError:
                            pass
                except ValueError as ve:
                    logger.debug(f"Failed to advance {tid} to PLANNING: {ve}")

    return dispatched


def dispatch_planning_agents(bots: dict[str, Any], max_agents: int = 0, start_bot_fn=None) -> int:
    """Dispatch PLANNING tickets to planner agents."""
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

    # Phase 1: Collect transitions for completed plans and dispatch new ones
    transitions: list[tuple[str, Any, list[dict] | None]] = []
    transition_tids: list[str] = []

    for ticket in planning:
        tid = getattr(ticket, 'id', '')
        if not tid:
            continue

        plan_file = plans_dir / f"{tid}.plan.json"
        if plan_file.exists():
            transitions.append((tid, TicketState.IMPLEMENTING, None))
            transition_tids.append(tid)
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
            if start_bot_fn:
                start_bot_fn(bot, bots=bots)

    # Phase 2: Apply all planning-complete transitions in a single batch
    if transitions:
        try:
            results = ts.batch_transition(transitions)
            dispatched += len(results)
            for tid in transition_tids:
                logger.info(f"Planning complete: {tid} PLANNING -> IMPLEMENTING")
                for cf in claims_dir.glob(f"{tid}.*.json"):
                    try:
                        cf.unlink()
                    except OSError:
                        pass
        except (ValueError, KeyError) as e:
            logger.warning(f"Batch planning transition failed ({e}), falling back to individual")
            for i, (tid, target_state, fb) in enumerate(transitions):
                try:
                    ts.transition(tid, target_state, fb)
                    logger.info(f"Planning complete: {tid} PLANNING -> IMPLEMENTING")
                    dispatched += 1
                    for cf in claims_dir.glob(f"{tid}.*.json"):
                        try:
                            cf.unlink()
                        except OSError:
                            pass
                except ValueError as ve:
                    logger.debug(f"Failed to advance {tid} to IMPLEMENTING: {ve}")

    return dispatched


def advance_reviewed_tickets(bots: dict[str, Any]) -> int:
    """Transition REVIEWING tickets to VERIFYING or REWORK based on reviewer verdicts.

    Uses batch_transition to apply all state changes in memory and save once,
    avoiding O(K*N) serialization cost per dispatch cycle.
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

    # Phase 1: Collect all transitions by analyzing reviewer verdicts
    transitions: list[tuple[str, Any, list[dict] | None]] = []
    tickets_to_clean: list[tuple[str, list]] = []  # (tid, review_claims)

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

        target_state = TicketState.REWORK if has_rework_flag else TicketState.VERIFYING
        reviewer_feedback = None

        if has_rework_flag:
            try:
                from codebot.scratchpad import load_scratchpad, save_scratchpad
                scratch = load_scratchpad(STATE_DIR, tid)
                for claim_file in review_claims:
                    bn = claim_file.stem.rsplit(".", 1)[-1] if "." in claim_file.stem else ""
                    tl = LOGS_DIR / f"{bn}.tasklog"
                    if tl.exists():
                        try:
                            content = tl.read_text(encoding="utf-8", errors="ignore")
                            if content.strip():
                                scratch.agent_history.append({
                                    "agent": bn,
                                    "stage": "REVIEWING",
                                    "started_at": time.time(),
                                    "finished_at": time.time(),
                                    "completed_steps": [],
                                    "files_changed": [],
                                    "summary": "reviewer verdict: REWORK",
                                    "error": content.strip()[-2000:],
                                })
                        except OSError:
                            pass
                save_scratchpad(STATE_DIR, scratch)
            except Exception as se:
                logger.warning(f"Failed to write reviewer feedback to scratchpad for {tid}: {se}")

        transitions.append((tid, target_state, reviewer_feedback))
        tickets_to_clean.append((tid, review_claims))

    if not transitions:
        return 0

    # Phase 2: Apply all transitions in a single batch save
    advanced = 0
    results = ts.batch_transition(transitions)
    advanced = len(results)
    for i, (tid, target_state, _) in enumerate(transitions):
        if any(r.id == tid for r in results):
            logger.info(f"Review verdict: {tid} -> {target_state.value}")

    # Phase 3: Clean up claims and assignments for successfully transitioned tickets
    for tid, review_claims in tickets_to_clean:
        for claim_file in review_claims:
            try:
                claim_file.unlink()
            except OSError:
                pass
        for name, bot in bots.items():
            if getattr(bot, '_assigned_ticket_id', '') == tid:
                bot._assigned_ticket_id = ''

    return advanced


def gatekeeper_verify_tickets() -> int:
    """Advance VERIFYING tickets to COMPLETE via quality gate evaluation.

    Uses batch_transition to apply all state changes in memory and save once,
    avoiding O(K*N) serialization cost per dispatch cycle.
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

    # Phase 1: Evaluate quality gates and collect transitions
    transitions: list[tuple[str, Any, list[dict] | None]] = []
    completed_tids: list[str] = []
    log_messages: list[tuple[str, str]] = []  # (level, message)

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
                if changed_files:
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
                        transitions.append((tid, TicketState.REWORK, None))
                        log_messages.append(("info", f"Gatekeeper: {tid} -> REWORK (gates passed but no files modified in git)"))
                    else:
                        transitions.append((tid, TicketState.REJECTED, None))
                        log_messages.append(("warning", f"Gatekeeper: {tid} -> REJECTED (no modifications after {rework_count} reworks)"))
                    continue
                transitions.append((tid, TicketState.COMPLETE, None))
                completed_tids.append(tid)
                log_messages.append(("info", f"Gatekeeper: {tid} -> COMPLETE (all gates passed)"))
            else:
                rework_count = getattr(ticket, 'rework_count', 0)
                if rework_count < 3:
                    transitions.append((tid, TicketState.REWORK, None))
                    log_messages.append(("info", f"Gatekeeper: {tid} -> REWORK (gates failed, attempt {rework_count + 1})"))
                else:
                    transitions.append((tid, TicketState.REJECTED, None))
                    log_messages.append(("warning", f"Gatekeeper: {tid} -> REJECTED (exceeded {rework_count} reworks)"))
        except Exception as e:
            logger.warning(f"Gatekeeper verification failed for {tid}: {e}")

    if not transitions:
        return 0

    # Phase 2: Apply all transitions in a single batch save
    advanced = 0
    results = ts.batch_transition(transitions)
    advanced = len(results)
    result_ids = {r.id for r in results}
    for i, (tid, target_state, fb) in enumerate(transitions):
        if tid in result_ids:
            level, msg = log_messages[i] if i < len(log_messages) else ("info", f"Gatekeeper: {tid} -> {target_state.value}")
            if level == "warning":
                logger.warning(msg)
            else:
                logger.info(msg)

    # Phase 3: Clear scratchpads for completed tickets
    for tid in completed_tids:
        try:
            from codebot.scratchpad import clear_scratchpad
            clear_scratchpad(STATE_DIR, tid)
        except Exception:
            pass

    return advanced


def route_ready_tickets() -> int:
    """Route READY tickets: decomposer sub-tickets to PLANNING, originals to DECOMPOSE.

    Uses batch_transition to apply all state changes in memory and save once,
    avoiding O(K*N) serialization cost per dispatch cycle.
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

    ready = ts.list_by_state(TicketState.READY)
    if not ready:
        return 0

    # Collect all transitions first, then apply in a single batch
    transitions: list[tuple[str, Any, list[dict] | None]] = []
    for ticket in ready:
        tid = getattr(ticket, 'id', '')
        if not tid:
            continue
        source = getattr(ticket, 'source', '')
        if source == 'decomposer':
            transitions.append((tid, TicketState.PLANNING, None))
        else:
            transitions.append((tid, TicketState.DECOMPOSE, None))

    if not transitions:
        return 0

    routed = 0
    try:
        results = ts.batch_transition(transitions)
        routed = len(results)
        for i, (tid, target_state, _) in enumerate(transitions):
            logger.info(f"Routed {tid} READY -> {target_state.value}" +
                        (" (decomposer sub-ticket)" if target_state == TicketState.PLANNING else ""))
    except (ValueError, KeyError) as e:
        # Fallback: apply individually if batch fails (e.g., one invalid transition)
        logger.warning(f"Batch route failed ({e}), falling back to individual transitions")
        for tid, target_state, _ in transitions:
            try:
                ts.transition(tid, target_state)
                logger.info(f"Routed {tid} READY -> {target_state.value}")
                routed += 1
            except ValueError as ve:
                logger.debug(f"Failed to route {tid}: {ve}")

    return routed


def process_rework_tickets(bots: dict[str, Any]) -> int:
    """Process REWORK tickets by routing them back to PLANNING or DECOMPOSE.

    Uses batch_transition to apply all state changes in memory and save once,
    avoiding O(K*N) serialization cost per dispatch cycle.
    """
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

    # Collect transitions into batches; high-rework tickets need special handling
    transitions: list[tuple[str, Any, list[dict] | None]] = []
    reject_fallbacks: list[str] = []

    for ticket in rework:
        tid = ticket.id
        rework_count = getattr(ticket, 'rework_count', 0)
        if rework_count >= 3:
            transitions.append((tid, TicketState.DECOMPOSE, None))
            reject_fallbacks.append(tid)
        else:
            plan_file = plans_dir / f"{tid}.plan.json"
            current_state = getattr(ticket, 'state', None)
            if current_state == TicketState.PLANNING:
                continue
            target_state = TicketState.PLANNING if plan_file.exists() else TicketState.DECOMPOSE
            transitions.append((tid, target_state, None))

    if not transitions:
        return 0

    advanced = 0
    results = ts.batch_transition(transitions)
    advanced = len(results)
    result_ids = {r.id for r in results}
    for i, (tid, target_state, _) in enumerate(transitions):
        if tid in result_ids:
            rc = getattr(rework[i], 'rework_count', 0) if i < len(rework) else 0
            if target_state == TicketState.DECOMPOSE and rc >= 3:
                logger.warning(f"Rework ticket {tid} -> DECOMPOSE (failed {rc} implementations, needs fresh decomposition)")
            else:
                logger.info(f"Rework ticket {tid} -> {target_state.value} (rework_count={rc})")

    return advanced


def recover_deferred_tickets() -> int:
    """Recover DEFERRED tickets back to READY or DECOMPOSE.

    Uses batch_transition to apply all state changes in memory and save once,
    avoiding O(K*N) serialization cost per dispatch cycle.
    """
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

    # Collect all transitions, then apply in a single batch
    target_state = TicketState.DECOMPOSE if decompose_count == 0 else TicketState.READY
    transitions: list[tuple[str, Any, list[dict] | None]] = [
        (ticket.id, target_state, None) for ticket in deferred
    ]

    recovered = 0
    try:
        results = ts.batch_transition(transitions)
        recovered = len(results)
        reason = "(queue cleared)" if target_state == TicketState.DECOMPOSE else ""
        for tid, _, _ in transitions:
            logger.info(f"Recovered deferred ticket {tid} -> {target_state.value} {reason}".strip())
    except (ValueError, KeyError) as e:
        # Fallback: apply individually if batch fails
        logger.warning(f"Batch deferred recovery failed ({e}), falling back to individual transitions")
        for ticket in deferred:
            try:
                ts.transition(ticket.id, target_state)
                reason = "(queue cleared)" if target_state == TicketState.DECOMPOSE else ""
                logger.info(f"Recovered deferred ticket {ticket.id} -> {target_state.value} {reason}".strip())
                recovered += 1
            except ValueError as ve:
                logger.warning(f"Deferred ticket {ticket.id} recovery failed: {ve}")

    return recovered
