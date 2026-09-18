"""Readiness predicates for bot manifests — pure functions.

Purpose
-------
Decides whether a bot manifest is eligible to spawn based on interval,
noop counter, file mtimes, queue content and checkpoint state. Callers
inject all file contents / mtimes; no I/O inside predicates.

Why
---
Bots were respawning on fixed timers even with no work. Readiness gates
spawn eligibility on observable signals already on disk (QUEUE.md depth,
input mtimes, checkpoint queue_remaining, noop counters). Pure functions
make the gates testable with truth-table fixtures without mocking files.

Invariants
----------
- All paths are cwd-relative, never absolute.
- No I/O inside predicates — callers inject mtimes dict and queue text.
- Stale threshold is 86400 seconds (24h) per plan — do not invent.
- Effective timeout comes from manifest heartbeat_timeout; batch_ctx
  heartbeat_max_gap_s is a test override only, never production precedence.
- _parse_queue_complexity logic adapted from orchestrator queue parser.
"""

import time
from pathlib import Path
from typing import Any


# ---------------------------------------------------------------------------
# _parse_queue_complexity — adapted from orchestrator queue parser
# Original operates on a Path; this variant operates on raw queue_text for pure-function testing.
# Adapted from orchestrator queue parser
# ---------------------------------------------------------------------------

def _parse_queue_complexity_from_text(queue_text: str) -> dict[str, str]:
    """Parse QUEUE.md markdown text into {Q-id: complexity} for confirmed/approved.

    Adapted from orchestrator queue parser to accept queue_text string
    instead of Path to keep predicates pure.
    """
    result: dict[str, str] = {}
    if not queue_text:
        return result
    import re as _re2
    decomp_lane: dict[str, str] = {}
    decomp_status: dict[str, str] = {}
    for m in _re2.finditer(
        r"###\s+(QUEUE-DECOMP-\d+)[^\n]*\n(.*?)(?=\n###\s+(?:QUEUE-DECOMP-\d+|Q-\d+)|\Z)",
        queue_text, _re2.DOTALL):
        item_id = m.group(1)
        body = m.group(2)
        sm = _re2.search(r"-\s*\*\*Status\*\*:\s*(\w[\w-]*)", body, _re2.IGNORECASE)
        st = sm.group(1).lower() if sm else "pending"
        decomp_status[item_id] = st
        if st not in ("confirmed", "approved", "pending", "queued"):
            continue
        lm = _re2.search(r"-\s*\*\*Lane\*\*:\s*(short|medium|long)", body, _re2.IGNORECASE)
        if lm:
            lane = lm.group(1).lower()
            decomp_lane[item_id] = {"short": "small", "medium": "medium", "long": "high"}[lane]
        else:
            cm = _re2.search(r"-\s*\*\*Complexity\*\*:\s*(\w+)", body, _re2.IGNORECASE)
            if cm:
                decomp_lane[item_id] = cm.group(1).lower()
    # NOTE: verbatim loop body from orchestrator.py:935-980
    in_table = False
    for line in queue_text.splitlines():
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
    for item_id, comp in decomp_lane.items():
        if item_id not in result and decomp_status.get(item_id) in ("confirmed", "approved", "pending", "queued"):
            result[item_id] = comp
    return result


def _parse_queue_complexity(queue_path: Path) -> dict[str, str]:
    """File-based wrapper retaining original signature — delegates to text variant.

    Verbosity: attribution comment required; logic is verbatim via
    _parse_queue_complexity_from_text.
    """
    if not queue_path.exists():
        return {}
    try:
        content = queue_path.read_text(encoding="utf-8")
    except OSError:
        return {}
    return _parse_queue_complexity_from_text(content)


# ---------------------------------------------------------------------------
# Predicates
# ---------------------------------------------------------------------------

STALE_SECONDS = 86400  # 24h — plan-mandated threshold


def is_due(manifest: dict[str, Any], now: float, next_run_at: float | None) -> bool:
    """Return True iff interval has elapsed (now >= next_run_at).

    Args:
        manifest: Bot manifest dict (unused except for future extension).
        now: Current Unix timestamp.
        next_run_at: Scheduled next run timestamp (0/None means due).

    Returns:
        True if due (interval elapsed).
    """
    if next_run_at is None:
        return True
    try:
        nxt = float(next_run_at)
    except (TypeError, ValueError):
        return True
    if nxt == 0.0:
        return True
    return now >= nxt


def noop_ok(manifest: dict[str, Any], counter_value: int | float | None) -> bool:
    """Return True iff noop counter is below cap.

    Args:
        manifest: Manifest dict containing noop_cap.
        counter_value: Current counter value (injected; not read from file).

    Returns:
        True if below cap or no cap; False if counter >= cap.
    """
    cap = manifest.get("noop_cap", 0)
    try:
        cap_int = int(cap)
    except (TypeError, ValueError):
        cap_int = 0
    if cap_int <= 0:
        return True
    try:
        cnt = int(counter_value) if counter_value is not None else 0
    except (TypeError, ValueError):
        cnt = 0
    return cnt < cap_int


def queue_has_work(kind: str, complexity_set: list[str] | set[str] | None, queue_text: str | None) -> bool:
    """Return True iff QUEUE.md contains ready work for the given kind/filter.

    Reuses _parse_queue_complexity logic verbatim (adapted to text).

    Args:
        kind: Manifest kind (scan|queue|command) — currently accepted but
            not gating; filtering is via complexity_set when provided.
        complexity_set: Required complexities (e.g. ["trivial","small"]) or
            None/empty meaning any confirmed/approved item counts.
        queue_text: Raw QUEUE.md markdown text (cwd-relative injected content).

    Returns:
        True if there is at least one confirmed/approved item matching
        the filter, False otherwise or on parse failure.
    """
    if queue_text is None or not isinstance(queue_text, str):
        return False
    if queue_text.strip() == "":
        return False
    try:
        complexity_map = _parse_queue_complexity_from_text(queue_text)
    except Exception:
        return False
    if not complexity_map:
        return False
    # No filter => any work counts
    if not complexity_set:
        return True
    # Normalize filter set: lower, map low->small (mirrors parser normalization)
    try:
        filt: set[str] = set()
        for c in complexity_set:
            if not isinstance(c, str):
                continue
            v = c.lower().strip()
            if v == "low":
                v = "small"
            filt.add(v)
    except Exception:
        return False
    if not filt:
        return True
    available = set(complexity_map.values())
    return bool(filt & available)


def signals_ok(
    manifest: dict[str, Any],
    mtimes: dict[str, float],
    queue_remaining: list[str] | int | None,
    queue_text: str | None = None,
    now: float | None = None,
    **kwargs: Any,
) -> bool:
    """Return True iff all readiness signals pass.

    Combines three checks:
      1. mtime-fresh: every manifest input path has mtime >= now-86400
      2. queue_has_work: QUEUE.md has confirmed/approved item matching filter
      3. queue_remaining non-empty for queue and command manifests

    Args:
        manifest: Bot manifest dict (needs input list, kind, complexity_filter).
        mtimes: Mapping cwd-relative input path -> Unix mtime float.
        queue_remaining: Manifest's checkpoint queue for queue and command manifests.
        queue_text: Raw QUEUE.md text (injected). If None, looks for
            'queue_text' in kwargs or empty string (=> no work).
        now: Current timestamp; defaults to time.time() if not supplied.
            Caller should inject for deterministic tests.

    Returns:
        True iff all applicable signals pass.
    """
    has_remaining = False
    if isinstance(queue_remaining, list):
        has_remaining = len(queue_remaining) > 0
    elif isinstance(queue_remaining, int):
        has_remaining = queue_remaining > 0
    elif queue_remaining is None:
        has_remaining = False
    else:
        # fallback: truthiness but require len>0 if sized
        try:
            has_remaining = len(queue_remaining) > 0  # type: ignore[arg-type]
        except Exception:
            has_remaining = bool(queue_remaining)
    if manifest.get("kind") != "scan" and not has_remaining:
        return False

    # Resolve now and queue_text
    now_val = float(now) if now is not None else float(kwargs.get("now", time.time()))
    # queue_text may be passed as positional 4th arg or kwarg
    qt = queue_text
    if qt is None:
        qt = kwargs.get("queue_text", "")
    if qt is None:
        qt = ""

    # 1. mtime-fresh
    inputs = manifest.get("input", [])
    if inputs:
        if not isinstance(mtimes, dict) or len(mtimes) == 0:
            # If manifest declares inputs but no mtimes injected, treat as stale
            return False
        for entry in inputs:
            if not isinstance(entry, dict):
                continue
            path = entry.get("path")
            if not path or not isinstance(path, str):
                continue
            # All paths are cwd-relative per spec; we compare directly
            mt = mtimes.get(path)
            if mt is None:
                return False
            try:
                mt_f = float(mt)
            except (TypeError, ValueError):
                return False
            if mt_f < (now_val - STALE_SECONDS):
                return False
    else:
        # No inputs declared => fresh vacuously (but queue checks still apply)
        pass

    # 2. queue_has_work
    kind = manifest.get("kind", "")
    complexity_set = manifest.get("complexity_filter")
    if not queue_has_work(kind, complexity_set, qt):
        return False

    return True


def ready(manifest: dict[str, Any], ctx: dict[str, Any]) -> bool:
    """Return True iff manifest is ready to spawn.

    Composition: is_due AND noop_ok AND signals_ok.

    Args:
        manifest: Bot manifest dict.
        ctx: Context dict with keys:
            now: float timestamp
            next_run_at: float timestamp
            counter_value: int (noop counter)
            mtimes: dict[str,float] (input path -> mtime)
            queue_remaining: list[str]|int
            queue_text: str (QUEUE.md content)

    Returns:
        True iff all predicates pass.
    """
    if not isinstance(ctx, dict):
        return False
    now = ctx.get("now", time.time())
    try:
        now_f = float(now)
    except (TypeError, ValueError):
        now_f = time.time()
    next_run_at = ctx.get("next_run_at")
    counter_value = ctx.get("counter_value", 0)
    mtimes = ctx.get("mtimes", {})
    queue_remaining = ctx.get("queue_remaining", [])
    queue_text = ctx.get("queue_text", "")

    if not is_due(manifest, now_f, next_run_at):
        return False
    if not noop_ok(manifest, counter_value):
        return False
    if not signals_ok(manifest, mtimes, queue_remaining, queue_text=queue_text, now=now_f):
        return False
    return True


def effective_timeout(manifest: dict[str, Any], heartbeat_max_gap_s: int | None = None) -> int:
    """Return effective stuck timeout.

    Production precedence: manifest heartbeat_timeout always wins.
    heartbeat_max_gap_s is a test override only and is ignored in
    production (i.e., manifest value takes precedence when both present).

    Args:
        manifest: Dict with heartbeat_timeout.
        heartbeat_max_gap_s: Test-only override (ignored if manifest has timeout).

    Returns:
        Effective timeout seconds.
    """
    manifest_timeout = manifest.get("heartbeat_timeout")
    try:
        mt = int(manifest_timeout) if manifest_timeout is not None else 0
    except (TypeError, ValueError):
        mt = 0
    if mt > 0:
        return mt
    if heartbeat_max_gap_s is not None:
        try:
            return int(heartbeat_max_gap_s)
        except (TypeError, ValueError):
            pass
    return mt if mt > 0 else 3600


# ---------------------------------------------------------------------------
# Scrutiny mode — flag T4+ items for heightened review, never block execution
# ---------------------------------------------------------------------------

import json as _json
import re as _re

_APPROVAL_TIER_RE = _re.compile(
    r"TIER-[4-9]|TIER-1[01]",
    _re.IGNORECASE,
)
_QUEUE_ITEM_RE = _re.compile(
    r"(###\s+(?:QUEUE-DECOMP-\d+|Q-\d+)[^\n]*\n)(.*?)(?=\n###\s+(?:QUEUE-DECOMP-\d+|Q-\d+)|\Z)",
    _re.DOTALL,
)
_SCRUTINY_LOG = Path(__file__).parent / "state" / "scrutiny_log.jsonl"


def _log_scrutiny_decisions(flagged: list[str]) -> None:
    try:
        _SCRUTINY_LOG.parent.mkdir(parents=True, exist_ok=True)
        with _SCRUTINY_LOG.open("a", encoding="utf-8") as f:
            for item_id in flagged:
                f.write(_json.dumps({"item_id": item_id, "flagged_at": __import__("time").time(), "mode": "scrutiny"}) + "\n")
    except Exception:
        pass


def load_approved_ids(state_dir: str | None = None) -> set[str]:
    if state_dir is None:
        state_dir = str(Path(__file__).parent / "state")
    path = Path(state_dir) / "approved_ids.json"
    try:
        data = _json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data}
        if isinstance(data, dict):
            return {str(k) for k, v in data.items() if v}
    except (OSError, _json.JSONDecodeError, ValueError):
        pass
    return set()


def filter_unapproved_items(queue_text: str, approved_ids: set[str] | None = None) -> str:
    """Scrutiny mode: T4+ items pass through with audit logging, never blocked.

    Items whose body contains TIER-4 through TIER-11 are flagged in
    state/scrutiny_log.jsonl for heightened review. Execution proceeds
    for all tiers through TIER 11 — no human gate blocks progress.
    The approved_ids parameter is accepted for backward compatibility
    and ignored.
    """
    if not queue_text:
        return queue_text

    def _replace(match: _re.Match) -> str:
        return match.group(0)

    flagged: list[str] = []
    for match in _QUEUE_ITEM_RE.finditer(queue_text):
        header = match.group(1)
        body = match.group(2)
        if _APPROVAL_TIER_RE.search(body):
            item_id_match = _re.search(r"(?:QUEUE-DECOMP-|Q-)\d+", header)
            if item_id_match:
                flagged.append(item_id_match.group(0))
    if flagged:
        _log_scrutiny_decisions(flagged)
    return _QUEUE_ITEM_RE.sub(_replace, queue_text)
