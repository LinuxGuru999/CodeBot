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

import ast
import json
import logging
import os
import subprocess
import time
from collections import deque
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
IMPLEMENTER_ROLE_NAMES: frozenset[str] = frozenset({"implementer"})

IMPLEMENTATION_ROLE_ORDER: tuple[str, ...] = ("implementer",)


def next_implementation_role(ticket):
    """Return next implementation role needed for multi-role approval, or None if all done."""
    approvals = getattr(ticket, "implementation_approvals", []) or []
    for role in IMPLEMENTATION_ROLE_ORDER:
        if role not in approvals:
            return role
    return None


def is_implementer_role(base_role: str) -> bool:
    return base_role in IMPLEMENTER_ROLE_NAMES

DISCOVERY_ROLE_NAMES: frozenset[str] = frozenset({
    "bug_hunter", "security_auditor", "architecture_auditor", "performance_auditor",
    "test_gap_auditor", "documentation_auditor", "dependency_auditor", "ux_auditor",
    "feature_hunter",
})

REVIEWER_ROLE_NAMES: frozenset[str] = frozenset({
    "reviewer", "correctness_reviewer", "security_reviewer", "architecture_reviewer",
    "test_reviewer", "performance_reviewer", "simplicity_reviewer",
    "documentation_reviewer", "adversarial_reviewer", "concurrency_reviewer",
    "data_integrity_reviewer",
})

PLANNING_ROLE_NAMES: frozenset[str] = frozenset({
    "planner",
})

DECOMPOSER_ROLE_NAMES: frozenset[str] = frozenset({
    "decomposer",
})

TRIAGER_ROLE_NAMES: frozenset[str] = frozenset({
    "ticket_triager",
})

TICKET_CLASS_TO_IMPLEMENTER: dict[str, str] = {
    "bug": "implementer",
    "feature": "implementer",
    "refactor": "implementer",
    "security": "implementer",
    "performance": "implementer",
    "architecture": "implementer",
    "test": "implementer",
    "documentation": "implementer",
    "dependency": "implementer",
    "infrastructure": "implementer",
}

CLAIM_TTL_SECONDS = 1800
TRIAGER_MAX_CONCURRENT = 5
SWEEP_INTERVAL = 300
_last_sweep_time: float = 0.0


REVIEWER_TYPES = (
    "correctness_reviewer", "security_reviewer", "architecture_reviewer",
    "test_reviewer", "performance_reviewer", "simplicity_reviewer",
    "documentation_reviewer", "adversarial_reviewer",
)

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


import threading as _td_threading

_ticket_store_cache: dict[Path, Any] = {}
_ticket_store_fingerprints: dict[Path, tuple[float, int]] = {}
# Backward-compat alias for tests that reference _ticket_store_meta directly.
# Arch invariant §3 keys on canonical state_dir identity; fingerprint is
# mtime/size — legacy name _ticket_store_meta is kept as alias.
_ticket_store_meta: dict[Path, tuple[float, int]] = _ticket_store_fingerprints
_ticket_store_cache_lock = _td_threading.Lock()


def clear_ticket_store_cache() -> None:
    """Invalidate the cached TicketStore(s) so the next access reloads from disk.

    Clears the entire canonical-state_dir-keyed cache. Callers that hold a
    reference to a previously returned store may continue to use it until GC,
    but the next ``get_ticket_store(state_dir)`` for that state_dir will
    construct a fresh instance. This is the **sole** cache invalidation
    operation — no other cache-op exists (see docs/CODING_STANDARDS.md §3).

    Compatibility: tests historically set ``_ticket_store_cache = None`` or
    ``_ticket_store_cache is store`` (single-slot cache) and manipulated
    ``_ticket_store_meta = {}``. This function handles all shapes — None,
    single TicketStore instance, dict — and keeps ``_ticket_store_meta`` and
    ``_ticket_store_fingerprints`` synced so legacy fixtures remain safe.
    """
    global _ticket_store_cache, _ticket_store_fingerprints, _ticket_store_meta
    with _ticket_store_cache_lock:
        # Normalise _ticket_store_cache shape — handle legacy None / single instance
        if _ticket_store_cache is None:
            _ticket_store_cache = {}
        elif isinstance(_ticket_store_cache, dict):
            for _old in list(_ticket_store_cache.values()):
                try:
                    _old.close()
                except Exception:
                    pass
            _ticket_store_cache.clear()
        else:
            # Legacy single-instance cache (TicketStore object)
            try:
                _ticket_store_cache.close()  # type: ignore[union-attr]
            except Exception:
                pass
            _ticket_store_cache = {}
        # Clear fingerprints — handle both names (alias may have diverged via
        # test assignment ``td._ticket_store_meta = {}``)
        try:
            _ticket_store_fingerprints.clear()
        except Exception:
            _ticket_store_fingerprints = {}
        try:
            if isinstance(_ticket_store_meta, dict):
                _ticket_store_meta.clear()
            else:
                _ticket_store_meta = {}
        except Exception:
            _ticket_store_meta = {}
        # Re-sync alias — both names point to same dict going forward
        _ticket_store_meta = _ticket_store_fingerprints
    logger.debug("TicketStore cache cleared")


def _canonical_state_dir(state_dir: Path | str | None) -> Path | None:
    if state_dir is None:
        p = Path(STATE_DIR)
    else:
        p = Path(state_dir)
        if p.name == "tickets.json":
            p = p.parent
        elif p.suffix == ".json" and p.name.startswith("tickets"):
            p = p.parent
    try:
        return p.resolve() if p.exists() else p.absolute()
    except Exception:
        return p


def get_ticket_store(state_dir: Path | str | None = None):
    """Get the canonical TicketStore for *state_dir*.

    Factory is the **sole** TicketStore construction site in production
    (see docs/CODING_STANDARDS.md §3 — arch test enforces one hit). Keyed
    exclusively on canonical storage identity (resolved ``state_dir`` /
    ``tickets.json`` path), not on constructor flags. View behavior like
    ``include_workers`` belongs on method arguments, not on the cache key.

    ``state_dir`` may be ``None`` (default location), a directory Path/str,
    or a ``tickets.json`` file path for back-compat. Returns ``None`` if
    ``tickets.json`` is missing or unreadable (preserves legacy contract for
    callers that treat ``None`` as “no store yet”).

    Thread-safe; uses an mtime/size fingerprint to detect external file
    replacement (cross-process WAL writer) and refresh the cached instance.
    Explicit invalidation is via :func:`clear_ticket_store_cache`.
    """
    global _ticket_store_cache, _ticket_store_fingerprints, _ticket_store_meta
    with _ticket_store_cache_lock:
        if _ticket_store_cache is None or not isinstance(_ticket_store_cache, dict):
            if _ticket_store_cache is None:
                _ticket_store_cache = {}
            elif not isinstance(_ticket_store_cache, dict):
                try:
                    legacy_path = getattr(_ticket_store_cache, "_path", None)
                    if legacy_path is not None:
                        legacy_canonical = _canonical_state_dir(Path(legacy_path).parent)
                    else:
                        legacy_canonical = None
                    if legacy_canonical is not None:
                        _ticket_store_cache = {legacy_canonical: _ticket_store_cache}
                    else:
                        _ticket_store_cache = {}
                except Exception:
                    _ticket_store_cache = {}
        if not isinstance(_ticket_store_fingerprints, dict):
            _ticket_store_fingerprints = {}
        if _ticket_store_meta is not _ticket_store_fingerprints:
            _ticket_store_meta = _ticket_store_fingerprints
    canonical = _canonical_state_dir(state_dir)
    if canonical is None:
        return None

    store_path = canonical / "tickets.json"
    if not store_path.exists():
        if state_dir is None:
            alt = Path(".codebot/state/tickets.json")
            if alt.exists():
                store_path = alt
                try:
                    canonical = alt.parent.resolve() if alt.parent.exists() else alt.parent.absolute()
                except Exception:
                    canonical = alt.parent
        if not store_path.exists():
            return None
    if not store_path.exists():
        return None

    with _ticket_store_cache_lock:
        cached = _ticket_store_cache.get(canonical)
        if cached is not None:
            try:
                st = store_path.stat()
                mtime = float(st.st_mtime)
                size = int(st.st_size)
                fp = _ticket_store_fingerprints.get(canonical)
                if fp is not None and fp != (mtime, size):
                    try:
                        cached.close()
                    except Exception:
                        pass
                    _ticket_store_cache.pop(canonical, None)
                    _ticket_store_fingerprints.pop(canonical, None)
                    cached = None
                else:
                    logger.debug("Returning cached TicketStore for %s", canonical)
                    return cached
            except Exception:
                logger.debug("Returning cached TicketStore for %s (stat failed)", canonical)
                return cached

    try:
        from codebot.ticket_engine import TicketStore
    except ImportError:
        return None

    try:
        new_store = TicketStore(store_path)
        logger.debug("Creating new TicketStore for %s (cache miss)", canonical)
    except Exception:
        return None

    with _ticket_store_cache_lock:
        if not isinstance(_ticket_store_cache, dict):
            _ticket_store_cache = {}
        if not isinstance(_ticket_store_fingerprints, dict):
            _ticket_store_fingerprints = {}
            _ticket_store_meta = _ticket_store_fingerprints
        existing = _ticket_store_cache.get(canonical)
        if existing is not None:
            try:
                new_store.close()
            except Exception:
                pass
            return existing
        try:
            st = store_path.stat()
            _ticket_store_fingerprints[canonical] = (float(st.st_mtime), int(st.st_size))
            _ticket_store_meta = _ticket_store_fingerprints
        except Exception:
            _ticket_store_fingerprints[canonical] = (0.0, 0)
            _ticket_store_meta = _ticket_store_fingerprints
        _ticket_store_cache[canonical] = new_store
        return new_store


# Backward-compatible alias for tests and internal callers
_get_ticket_store = get_ticket_store


# ---------------------------------------------------------------------------
# Claim Management
# ---------------------------------------------------------------------------

MAX_CLAIM_FILE_SIZE = 1024  # 1KB safety limit to prevent memory exhaustion


def _validate_claim_schema(data: dict, claim_path: Path) -> bool:
    """Validate claim JSON schema to prevent type confusion attacks.
    
    Args:
        data: Parsed claim JSON data
        claim_path: Path to claim file (for logging)
    
    Returns:
        True if schema is valid, False otherwise
    
    Required fields:
        - worker: must be a string
        - at: must be a numeric type (int or float)
    """
    if not isinstance(data, dict):
        logger.warning("Claim file %s has invalid schema: root is not a dict", claim_path)
        return False
    
    # Validate 'worker' field if present
    if "worker" in data:
        worker = data["worker"]
        if not isinstance(worker, str):
            logger.warning(
                "Claim file %s has invalid schema: 'worker' is %s, expected string",
                claim_path, type(worker).__name__
            )
            return False
    
    # Validate 'at' field if present
    if "at" in data:
        at = data["at"]
        if not isinstance(at, (int, float)):
            logger.warning(
                "Claim file %s has invalid schema: 'at' is %s, expected numeric",
                claim_path, type(at).__name__
            )
            return False
    
    return True


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
                file_size = p.stat().st_size
                if file_size > MAX_CLAIM_FILE_SIZE:
                    logger.warning(
                        "Skipping oversized claim file %s (%d bytes > %d byte limit)",
                        p, file_size, MAX_CLAIM_FILE_SIZE,
                    )
                    continue
                data = json.loads(p.read_text(encoding="utf-8"))
                
                # Validate schema before processing
                if not _validate_claim_schema(data, p):
                    logger.warning("Skipping malformed claim file %s", p)
                    continue
                
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
    """Remove claim files belonging to bots that are no longer active.

    Security: Enforces max file size (1KB) before parsing to prevent memory exhaustion.
    Skips oversized files with a warning.
    """
    swept = 0
    try:
        claims_dir = STATE_DIR / "claims"
        if not claims_dir.exists():
            return 0

        valid_bot_names = set(bots.keys())

        for p in claims_dir.glob("*.json"):
            try:
                # SECURITY CHECK: Verify file size before reading/parsing
                file_size = p.stat().st_size
                if file_size > MAX_CLAIM_FILE_SIZE:
                    logger.warning(
                        "Sweep: Skipping oversized claim file %s (%d bytes > %d byte limit)",
                        p, file_size, MAX_CLAIM_FILE_SIZE,
                    )
                    continue

                raw_content = p.read_text(encoding="utf-8")
                data = json.loads(raw_content)

                # Validate schema to ensure 'worker' field is trustworthy
                if not _validate_claim_schema(data, p):
                    logger.warning("Sweep: Skipping malformed claim file %s", p)
                    continue

                worker = data.get("worker", "")
                if worker and worker not in valid_bot_names:
                    # Orphan claim: worker no longer exists in active bots
                    p.unlink()
                    swept += 1
                    logger.debug("Sweep: Removed orphan claim %s for inactive worker '%s'", p.name, worker)

            except json.JSONDecodeError:
                logger.warning("Sweep: Failed to decode JSON in claim file %s", p)
            except OSError as e:
                logger.error("Sweep: OS error accessing claim file %s: %s", p, e)
            except Exception as e:
                logger.error("Sweep: Unexpected error processing claim file %s: %s", p, e)

        if swept:
            logger.info(f"Swept {swept} orphan claim(s)")
    except Exception as e:
        logger.error(f"Sweep failed: {e}")
    return swept




# ---------------------------------------------------------------------------
# Ticket Dispatching
# ---------------------------------------------------------------------------

def _rank_tickets_for_dispatch(
    tickets: list[Any],
    state_counts: dict[str, int] | None = None,
) -> list[Any]:
    """No-op: work_scorer deleted per ADR-007 Phase 2. Authoritative ordering via BucketDispatcher GOAL tier."""
    return tickets












def gatekeeper_verify_tickets(store: Any | None = None) -> int:
    """Advance VERIFYING tickets to COMPLETE via quality gate evaluation.

    Uses batch_transition to apply all state changes in memory and save once,
    avoiding O(K*N) serialization cost per dispatch cycle.
    """
    try:
        from codebot.ticket_engine import TicketState
    except ImportError:
        return 0

    ts = store if store is not None else get_ticket_store()
    if ts is None:
        return 0

    try:
        from codebot.review_config import get_review_config
        max_rework = get_review_config().max_rework_cycles
    except Exception:
        max_rework = 5

    try:
        verifying = ts.list_by_state(TicketState.VERIFYING)
    except Exception:
        return 0
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
            from codebot.quality_gate import run_quality_gates_with_cache, load_policy, record_gate_results
            policy = load_policy()
            workspace = Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))
            tc = getattr(ticket, 'ticket_class', None)
            tc_val = tc.value if hasattr(tc, 'value') else str(tc) if tc else "feature"
            changed = list(getattr(ticket, 'affected_modules', None) or [])
            t_rev = getattr(ticket, "updated_at", 0.0)
            passed, evaluations = run_quality_gates_with_cache(
                policy=policy,
                workspace=workspace,
                state_dir=STATE_DIR,
                ticket_id=tid,
                ticket_class=tc_val,
                changed_files=changed,
                ticket_revision=t_rev,
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
                    if rework_count < max_rework:
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
                if rework_count < max_rework:
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

    # Phase 4: Commit each newly-COMPLETE ticket's files on cb/<ticket>,
    # open PR + auto-merge, record SHA+URL (all fail-open)
    if completed_tids:
        try:
            from codebot.completion_commit import (
                commit_ticket_files, open_pull_request, auto_merge_pull_request,
                push_current_branch, sync_ticket_issue, _push_enabled,
            )
            workspace = Path(os.environ.get("CODEBOT_PROJECT_ROOT", Path.cwd()))
            pr_flow = _push_enabled()
            for tid in completed_tids:
                try:
                    ticket = ts.get(tid)
                    if ticket is None or getattr(ticket, "commit_sha", ""):
                        continue
                    files = list(getattr(ticket, "affected_modules", None) or [])
                    if not files:
                        continue
                    title = getattr(ticket, "title", "")
                    ok, sha = commit_ticket_files(
                        workspace, tid, title, files, branch=True,
                    )
                    if ok and sha:
                        pr_url = getattr(ticket, "pr_url", "")
                        if not pr_url and pr_flow:
                            pok, pr_url = open_pull_request(
                                workspace, tid, title, sha,
                            )
                            if pok and pr_url:
                                auto_merge_pull_request(pr_url)
                            else:
                                pr_url = ""
                        ts.record_commit(tid, sha, pr_url)
                        push_current_branch(workspace)
                        sync_ticket_issue(tid, title, sha, "COMPLETE")
                except Exception as e:
                    logger.warning(f"ticket {tid}: completion commit failed (fail-open): {e}")
        except Exception as e:
            logger.warning(f"completion commit phase failed (fail-open): {e}")

    return advanced


def route_ready_tickets(store: Any | None = None, skip_tids: set[str] | None = None) -> int:
    """Route READY tickets with risk-aware fast-path for low-risk items.

    Low-risk tickets (risk < MIN_RISK_FOR_PLANNING) go READY -> IMPLEMENTING
    directly, skipping DECOMPOSE and PLANNING. Medium+ tickets follow the
    existing decomposer/original routing.

    Uses batch_transition to apply all state changes in memory and save once,
    avoiding O(K*N) serialization cost per dispatch cycle.

    Args:
        store: Optional pre-loaded TicketStore instance.
        skip_tids: Ticket IDs to skip routing this tick (e.g., just returned
            to READY by error recovery — prevents immediate re-routing).
    """
    try:
        from codebot.ticket_engine import TicketState
    except ImportError:
        return 0

    ts = store if store is not None else get_ticket_store()
    if ts is None:
        return 0

    ready = ts.list_by_state(TicketState.READY)
    if not ready:
        return 0

    _skip = skip_tids or set()

    try:
        from codebot.ticket_engine import MIN_RISK_FOR_PLANNING as _min_risk
        from codebot.ticket_engine import _RISK_ORDER as _risk_order_map
        _threshold_order: int = _risk_order_map.get(_min_risk.value, 1)
        _risk_order: dict[str, int] = _risk_order_map
    except ImportError:
        _threshold_order = 1
        _risk_order = {"low": 0, "medium": 1, "high": 2, "critical": 3}

    transitions: list[tuple[str, Any, list[dict] | None]] = []
    for ticket in ready:
        tid = getattr(ticket, 'id', '')
        if not tid or tid in _skip:
            continue
        risk_obj = getattr(ticket, 'risk', None)
        if isinstance(risk_obj, str):
            risk_val = risk_obj
        elif risk_obj is not None:
            v = getattr(risk_obj, "value", None)
            risk_val = v if isinstance(v, str) else "low"
        else:
            risk_val = "low"
        if _risk_order.get(risk_val, 0) < _threshold_order:
            transitions.append((tid, TicketState.IMPLEMENTING, None))
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




def recover_deferred_tickets(store: Any | None = None) -> int:
    """Recover DEFERRED tickets back to IMPLEMENT or DECOMP.

    Uses batch_transition to apply all state changes in memory and save once,
    avoiding O(K*N) serialization cost per dispatch cycle.
    """
    try:
        from codebot.ticket_engine import TicketState
    except ImportError:
        return 0

    ts = store if store is not None else get_ticket_store()
    if ts is None:
        return 0
    try:
        deferred = ts.list_by_state(TicketState.DEFERRED)
        decompose_count = len(ts.list_by_state(TicketState.DECOMPOSE))
    except Exception:
        return 0
    if not deferred:
        return 0

    target_state = TicketState.DECOMP if decompose_count == 0 else TicketState.IMPLEMENT
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


def process_deferred_gates(store: Any | None = None, timeout_per_gate: int = 15, max_budget_seconds: float = 10.0) -> int:
    try:
        from codebot.ticket_engine import TicketState
    except ImportError:
        return 0
    ts = store if store is not None else get_ticket_store()
    if ts is None:
        return 0
    advanced = 0
    for state_name in ("REVIEW", "VERIFYING"):
        target_tickets = [t for t in ts._tickets.values() if getattr(t.state, "value", str(t.state)) == state_name]
        for ticket in target_tickets:
            tid = getattr(ticket, "id", "")
            if not tid:
                continue
            verdicts_dir = STATE_DIR / "reviews" / tid
            if not verdicts_dir.exists():
                continue
            verdicts = [f for f in verdicts_dir.glob("*.json") if f.parent.name != "quarantine"]
            if not verdicts:
                continue
            has_rework = False
            has_approve = False
            for vf in verdicts:
                try:
                    data = json.loads(vf.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    import ast as _ast
                    try:
                        data = _ast.literal_eval(vf.read_text(encoding="utf-8"))
                    except (ValueError, SyntaxError):
                        continue
                if not isinstance(data, dict):
                    continue
                v = str(data.get("verdict", "")).upper()
                if v in ("REWORK", "ESCALATE", "BLOCK"):
                    has_rework = True
                elif v in ("APPROVE", "PASS", "COMPLETE"):
                    has_approve = True
            target = TicketState.REWORK if has_rework else TicketState.COMPLETE
            if target == TicketState.COMPLETE:
                try:
                    ts.record_gate_result(tid, True)
                except Exception:
                    pass
            try:
                ts.transition(tid, target)
                advanced += 1
                logger.info("Deferred gate: %s -> %s (rework=%s approve=%s)", tid, target.value, has_rework, has_approve)
            except ValueError as ve:
                logger.debug("Deferred gate transition failed for %s: %s", tid, ve)
    return advanced


def process_deferred_tickets(store: Any | None = None) -> int:
    """Process DEFERRED tickets: route to DECOMP, or BLOCK after max cycles.

    Pipeline: REWORK(×5) → DEFERRED → DECOMP → DEFERRED(×2) → BLOCKED
    Each time a ticket enters DEFERRED, deferred_count increments.
    After max_deferred_cycles (default 2), ticket is permanently BLOCKED.
    Otherwise routes to DECOMP for fresh decomposition.
    """
    try:
        from codebot.ticket_engine import TicketState
    except ImportError:
        return 0

    ts = store if store is not None else get_ticket_store()
    if ts is None:
        return 0
    try:
        deferred = ts.list_by_state(TicketState.DEFERRED)
    except Exception:
        return 0
    if not deferred:
        return 0

    try:
        from codebot.review_config import get_review_config
        max_deferred = get_review_config().max_deferred_cycles
    except Exception:
        max_deferred = 2

    transitions: list[tuple[str, Any, list[dict] | None]] = []

    for ticket in deferred:
        tid = ticket.id
        deferred_count = getattr(ticket, 'deferred_count', 0)
        if deferred_count >= max_deferred:
            transitions.append((tid, TicketState.BLOCKED, None))
        else:
            transitions.append((tid, TicketState.DECOMP, None))

    if not transitions:
        return 0

    for tid, target_state, _ in transitions:
        if target_state == TicketState.DECOMP:
            try:
                tkt = ts._tickets.get(tid)
                if tkt is not None:
                    dc = getattr(tkt, 'deferred_count', 0)
                    updates = {'deferred_count': dc + 1}
                    updated_tkt = type(tkt)(**{**tkt.__dict__, **updates}) if hasattr(tkt, '__dict__') else tkt
                    ts._tickets[tid] = updated_tkt
            except Exception:
                pass

    advanced = 0
    results = ts.batch_transition(transitions)
    advanced = len(results)
    result_ids = {r.id for r in results}
    for i, (tid, target_state, _) in enumerate(transitions):
        if tid in result_ids:
            dc = getattr(deferred[i], 'deferred_count', 0) if i < len(deferred) else 0
            if target_state == TicketState.BLOCKED:
                logger.warning(f"Deferred ticket {tid} -> BLOCKED (deferred {dc} times, exceeded max)")
            else:
                logger.info(f"Deferred ticket {tid} -> DECOMP (deferred_count={dc + 1})")

    return advanced
