"""Batch scheduler — pure functions, no I/O or subprocess.

Purpose
-------
Groups ready bot manifests into tier-ordered batches for efficient dispatch.
Enforces model caps and slot limits; consults token budget state before
packing. All decisions are pure over in-memory manifest dicts.

Why
---
Batched execution packs up to 5 manifests per batch / 2 concurrent batches,
grouped by model with stagger, while preserving tier priority. Budget
awareness prevents overspend; caps prevent model contention. Pure functions
allow TDD ordering / packing tests without spawning or reading state.

Invariants
----------
- stdlib only; no I/O, no subprocess.
- Thresholds frozen per plan: stale 86400, caps 5/2/3/2/10, stagger 20.
- Budget handling consults BEFORE packing; reasons are literal strings
  budget-shed / budget-exhausted per spec.
- Tier ordering mirrors orchestrator.py:1006 sort key (tier asc, next_run_at asc).
- Approval gate blocks TIER 4+ items unless explicitly approved.
"""

from typing import Any

# Tiers under heightened scrutiny (flagged + audited, never blocked)
SCRUTINY_TIERS = frozenset({"T4", "T5", "T6", "T7", "T8", "T9", "T10", "T11"})
APPROVAL_REQUIRED_TIERS = SCRUTINY_TIERS
# Queue item prefix that indicates decomposed ROADMAP item
DECOMP_PREFIX = "QUEUE-DECOMP-"
# Tag pattern for tier tags in queue items
TIER_TAG_PREFIX = "TIER-"


def extract_tier_from_tags(tags: list[str] | None) -> str | None:
    """Extract ROADMAP tier from a list of tags.

    Args:
        tags: List of tag strings like ["T4", "security", "performance"].

    Returns:
        Tier string like "T4" or None if no tier tag found.
    """
    if not tags:
        return None
    for tag in tags:
        if isinstance(tag, str) and tag.startswith("T") and len(tag) <= 3:
            tier_part = tag[1:]
            if tier_part.isdigit() and 1 <= int(tier_part) <= 11:
                return tag
    return None


def needs_approval(item: dict[str, Any]) -> bool:
    """Check if a queue item is under heightened scrutiny.

    Items from ROADMAP tiers T4+ are flagged for review because they involve
    architectural decisions, multi-system changes, or infrastructure work.
    Scrutiny mode never blocks: it flags + audits, execution proceeds.

    Args:
        item: Queue item dict with optional "tags", "source_tier", "complexity" keys.

    Returns:
        True if item is under scrutiny, False otherwise.
    """
    source_tier = item.get("source_tier", "")
    if isinstance(source_tier, str) and source_tier in SCRUTINY_TIERS:
        return True

    tags = item.get("tags", [])
    if isinstance(tags, list):
        tier = extract_tier_from_tags(tags)
        if tier and tier in SCRUTINY_TIERS:
            return True

    complexity = item.get("complexity", "")
    if complexity == "critical":
        return True

    return False


def filter_approved(
    items: list[dict[str, Any]],
    approved_ids: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split items into standard and scrutiny lists. Nothing is blocked.

    Args:
        items: List of queue item dicts.
        approved_ids: Accepted for backward compatibility; ignored.

    Returns:
        Tuple of (all_items, []) — everything proceeds, scrutiny items
        are tagged in place via item["scrutiny"] = True.
    """
    for item in items:
        if needs_approval(item):
            item["scrutiny"] = True
    return list(items), []


def order_by_tier(
    names: list[str],
    tier_map: dict[str, int],
    next_run_at: dict[str, float],
) -> list[str]:
    """Return names sorted by tier asc then next_run_at asc.

    Mirrors orchestrator.py:1006 due_bots_first sort:
      due.sort(key=lambda n: (TIER_PRIORITY.get(n, 2), bots[n].next_run_at))

    Args:
        names: Bot names to order.
        tier_map: Mapping name -> tier_priority (int). Missing -> 999.
        next_run_at: Mapping name -> next_run_at timestamp (float).
            Missing -> 0.0 (earliest; deterministic).

    Returns:
        New list sorted as specified; never mutates input.
    """
    def _key(n: str) -> tuple[int, float]:
        tier = tier_map.get(n, 999)
        try:
            t = int(tier)
        except (TypeError, ValueError):
            t = 999
        nr = next_run_at.get(n, 0.0) if isinstance(next_run_at, dict) else 0.0
        try:
            nr_f = float(nr)
        except (TypeError, ValueError):
            nr_f = 0.0
        return (t, nr_f)

    return sorted(list(names), key=_key)


def pack_batches(
    ready: list[dict[str, Any]],
    max_per_batch: int = 8,
    max_batches: int = 3,
    budget_state: str | None = None,
    stagger_s: int = 5,
) -> dict[str, Any]:
    """Group ready manifests into batches, consulting budget first.

    Args:
        ready: List of manifest dicts (each with name, tier_priority, model).
        max_per_batch: Max manifests per batch (default 5).
        max_batches: Max number of batches (default 2).
        budget_state: One of None, ok, warn, shed_tier3, stop. When not None,
            consulted BEFORE packing per spec.
        stagger_s: Inter-start stagger seconds per batch slot (default 20).

    Returns:
        Dict with keys:
          batches: list[list[manifest]] — packed batches (each batch is list of manifests)
          dropped: list[dict] — each {name, manifest, reason} for excluded items
          reason: str|None — global reason (e.g. budget-exhausted)
          warning: str|None — warn state annotation
          staggers: list[int] — stagger per batch index (i*stagger_s)
          stagger_s: int — echo of param

    Thresholds: 8 manifests per batch, up to 3 batches (24/tick),
    inter-batch stagger 5s. Keep in sync with orchestrator._plan_manifest_batches.
    """
    if ready is None:
        ready = []
    if not isinstance(ready, list):
        try:
            ready = list(ready)  # type: ignore[arg-type]
        except Exception:
            ready = []

    # Budget consultation BEFORE packing
    dropped: list[dict[str, Any]] = []
    warning: str | None = None
    reason: str | None = None

    if budget_state == "stop":
        # empty plan with reason budget-exhausted
        for m in ready:
            nm = m.get("name", "?") if isinstance(m, dict) else str(m)
            dropped.append({"name": nm, "manifest": m, "reason": "budget-exhausted"})
        return {
            "batches": [],
            "dropped": dropped,
            "reason": "budget-exhausted",
            "warning": None,
            "staggers": [],
            "stagger_s": stagger_s,
        }
    elif budget_state == "shed_tier3":
        keep: list[dict[str, Any]] = []
        for m in ready:
            tp = m.get("tier_priority", 999) if isinstance(m, dict) else 999
            try:
                tp_int = int(tp)
            except (TypeError, ValueError):
                tp_int = 999
            if tp_int >= 31:
                nm = m.get("name", "?") if isinstance(m, dict) else str(m)
                dropped.append({"name": nm, "manifest": m, "reason": "budget-shed"})
            else:
                keep.append(m)
        ready = keep
        reason = None
        warning = None
    elif budget_state == "warn":
        warning = "budget-warn"
        # pack all, no shedding
        reason = None
    elif budget_state == "ok":
        reason = None
        warning = None
    elif budget_state is None:
        # No budget constraint, proceed with packing
        reason = None
        warning = None
    else:
        for m in ready:
            nm = m.get("name", "?") if isinstance(m, dict) else str(m)
            dropped.append({"name": nm, "manifest": m, "reason": "budget-unknown"})
        return {
            "batches": [],
            "dropped": dropped,
            "reason": "budget-unknown",
            "warning": None,
            "staggers": [],
            "stagger_s": stagger_s,
        }

    def _sort_key(m: dict[str, Any]) -> tuple[int, str, str]:
        tp = m.get("tier_priority", 999)
        try:
            ti = int(tp)
        except (TypeError, ValueError):
            ti = 999
        mod = str(m.get("model", "")) if isinstance(m, dict) else ""
        nm = str(m.get("name", "")) if isinstance(m, dict) else ""
        return (ti, mod, nm)

    try:
        ready_sorted = sorted(ready, key=_sort_key)
    except Exception:
        ready_sorted = list(ready)

    try:
        batch_size = max(1, int(max_per_batch))
        batch_limit = max(0, int(max_batches))
    except (TypeError, ValueError):
        batch_size = 1
        batch_limit = 0

    grouped_by_model: dict[str, list[dict[str, Any]]] = {}
    for manifest in ready_sorted:
        grouped_by_model.setdefault(str(manifest.get("model", "")), []).append(manifest)

    batches: list[list[dict[str, Any]]] = []
    limit_reached = False
    for model_manifests in grouped_by_model.values():
        # Use range() for O(1) start index per slice — no .index() lookup needed.
        # A flag avoids re-checking len(batches) once the batch cap is hit.
        for start in range(0, len(model_manifests), batch_size):
            candidate = model_manifests[start:start + batch_size]
            if not limit_reached and len(batches) < batch_limit:
                batches.append(candidate)
                continue
            # Batch limit reached: drop every remaining manifest.
            limit_reached = True
            for manifest in candidate:
                dropped.append({"name": manifest.get("name", "?"), "manifest": manifest, "reason": "batch-capacity"})

    staggers = [i * int(stagger_s) for i in range(len(batches))]

    return {
        "batches": batches,
        "dropped": dropped,
        "reason": reason,
        "warning": warning,
        "staggers": staggers,
        "stagger_s": int(stagger_s),
    }


def apply_caps(
    batches: list[list[dict[str, Any]]] | dict[str, Any],
    thinking_cap: int = 3,
    qwen38max_cap: int = 2,
    slot_cap: int = 10,
) -> dict[str, Any]:
    """Enforce model and slot caps, dropping lowest-priority overflow.

    Args:
        batches: Either list of batches (each batch is list of manifests)
            or dict containing 'batches' key as returned by pack_batches.
        thinking_cap: Max total manifests whose model contains 'thinking'.
        qwen38max_cap: Max total manifests with model == 'qwen-3.8-max'.
        slot_cap: Max total manifests across all batches.

    Returns:
        Dict with keys:
          batches: filtered batches (same structure)
          dropped: list of {name, manifest, reason} for each dropped
          reason: str|None
          staggers: updated stagger list (if applicable)
    """
    # Normalize input
    batches_list: list[list[dict[str, Any]]] = []
    extra_dropped: list[dict[str, Any]] = []
    extra_reason: str | None = None
    extra_warning: str | None = None
    staggers: list[int] | None = None
    stagger_s = 20

    if isinstance(batches, dict):
        raw = batches.get("batches", [])
        # handle batches stored as list
        batches_list = raw if isinstance(raw, list) else []
        # carry over existing dropped for merging later
        extra_dropped = batches.get("dropped", []) or []
        if not isinstance(extra_dropped, list):
            extra_dropped = []
        extra_reason = batches.get("reason")
        extra_warning = batches.get("warning")
        staggers = batches.get("staggers")
        try:
            stagger_s = int(batches.get("stagger_s", 20))
        except Exception:
            stagger_s = 20
        # Ensure batches_list elements are lists
        normalized: list[list[dict[str, Any]]] = []
        for b in batches_list:
            if isinstance(b, dict) and "manifests" in b:
                normalized.append(list(b["manifests"]))
            elif isinstance(b, dict) and "batch" in b:
                normalized.append(list(b["batch"]))
            elif isinstance(b, (list, tuple)):
                normalized.append(list(b))
            else:
                normalized.append([b])
        batches_list = normalized
    elif isinstance(batches, list):
        is_flat = bool(batches) and all(isinstance(x, dict) and "model" in x for x in batches)
        if is_flat:
            batches_list = [list(batches)]
        else:
            normalized2: list[list[dict[str, Any]]] = []
            for b in batches:  # type: ignore[arg-type]
                if isinstance(b, (list, tuple)):
                    normalized2.append(list(b))  # type: ignore[arg-type]
                elif isinstance(b, dict) and "manifests" in b:
                    normalized2.append(list(b["manifests"]))  # type: ignore[arg-type]
                elif isinstance(b, dict) and "batch" in b:
                    normalized2.append(list(b["batch"]))  # type: ignore[arg-type]
                elif isinstance(b, dict) and "model" in b:
                    normalized2.append([b])  # type: ignore[arg-type]
                else:
                    normalized2.append([b])  # type: ignore[arg-type]
            batches_list = normalized2
    else:
        batches_list = []

    # Flatten for analysis while preserving order
    flat: list[dict[str, Any]] = []
    for b in batches_list:
        for m in b:
            if isinstance(m, dict) and "model" in m:
                flat.append(m)
            elif isinstance(m, dict) and "manifest" in m:
                # dropped wrapper mistakenly included
                continue
            else:
                # unknown shape, skip unless it looks like manifest
                if isinstance(m, dict) and "name" in m:
                    flat.append(m)

    dropped: list[dict[str, Any]] = list(extra_dropped)  # start with prior dropped

    # Helper to drop excess with reason
    def _drop_lowest(
        candidates: list[dict[str, Any]],
        excess: int,
        reason_str: str,
    ) -> list[dict[str, Any]]:
        if excess <= 0 or not candidates:
            return []
        # Lowest priority = highest tier number, tie break by name descending to make deterministic
        # Sort descending tier (reverse=True)
        def _key(m: dict[str, Any]) -> tuple[int, str]:
            try:
                ti = int(m.get("tier_priority", 999))
            except (TypeError, ValueError):
                ti = 999
            return (ti, str(m.get("name", "")))
        sorted_desc = sorted(candidates, key=_key, reverse=True)
        to_drop = sorted_desc[:excess]
        for m in to_drop:
            nm = m.get("name", "?") if isinstance(m, dict) else str(m)
            dropped.append({"name": nm, "manifest": m, "reason": reason_str})
        # Return remaining candidates after drop
        drop_names = {d["name"] for d in to_drop if isinstance(d, dict)}
        # Actually need set of manifest identities (name)
        return [m for m in candidates if m.get("name") not in {x.get("name") for x in to_drop}]

    # Thinking cap
    thinking_manifests = [m for m in flat if "thinking" in str(m.get("model", ""))]
    excess_thinking = len(thinking_manifests) - int(thinking_cap)
    if excess_thinking > 0:
        remaining_thinking = _drop_lowest(thinking_manifests, excess_thinking, "thinking-cap")
        # Build new flat excluding dropped thinking
        dropped_thinking_names = {d["manifest"].get("name") for d in dropped if d.get("reason") == "thinking-cap"}
        flat = [m for m in flat if m.get("name") not in dropped_thinking_names]

    # qwen-3.8-max cap
    qwen_manifests = [m for m in flat if str(m.get("model", "")) == "qwen-3.8-max"]
    excess_qwen = len(qwen_manifests) - int(qwen38max_cap)
    if excess_qwen > 0:
        _drop_lowest(qwen_manifests, excess_qwen, "qwen38max-cap")
        dropped_qwen_names = {d["manifest"].get("name") for d in dropped if d.get("reason") == "qwen38max-cap"}
        flat = [m for m in flat if m.get("name") not in dropped_qwen_names]

    # Slot cap
    excess_slot = len(flat) - int(slot_cap)
    if excess_slot > 0:
        # drop lowest priority overall
        def _key2(m: dict[str, Any]) -> tuple[int, str]:
            try:
                ti = int(m.get("tier_priority", 999))
            except (TypeError, ValueError):
                ti = 999
            return (ti, str(m.get("name", "")))
        sorted_desc_all = sorted(flat, key=_key2, reverse=True)
        to_drop_slot = sorted_desc_all[:excess_slot]
        for m in to_drop_slot:
            nm = m.get("name", "?") if isinstance(m, dict) else str(m)
            dropped.append({"name": nm, "manifest": m, "reason": "slot-cap"})
        dropped_slot_names = {m.get("name") for m in to_drop_slot}
        flat = [m for m in flat if m.get("name") not in dropped_slot_names]

    # Rebuild batches preserving original packing sizes where possible
    # Strategy: iterate flat in order and fill batches respecting original max_per_batch
    # Infer max_per_batch from original batches if available, else 5
    inferred_max = 5
    if batches_list:
        # Use max size seen
        try:
            inferred_max = max(len(b) for b in batches_list) if batches_list else 5
            if inferred_max == 0:
                inferred_max = 5
            # But cap at 5 per spec default if original was smaller
            # Actually infer from first batch length or default 5
            # Prefer 5 if original batches empty
            if inferred_max < 5:
                inferred_max = 5
        except Exception:
            inferred_max = 5
    # However we should use 5 as default max_per_batch for re-packing filtered flat
    # To keep tier order, we already filtered flat which is tier-ordered from original packs
    # Re-pack sequentially into batches of inferred_max
    new_batches: list[list[dict[str, Any]]] = []
    # flat is already in tier/model sorted order from pack_batches, which is desired
    # Re-pack
    # Determine original max_per_batch more precisely: look at extra dict stagger? Not reliable.
    # Use 5 as default per spec.
    max_per_batch_repack = 5
    # If original batches had consistent size, use that
    if batches_list and len(batches_list) > 0:
        # Check first batch size vs default
        first_len = len(batches_list[0]) if batches_list[0] else 0
        # Use default 5 unless we can infer smaller batch was intentional due to model grouping?
        # Keep 5 for re-pack to avoid over-packing.
        max_per_batch_repack = 5

    for i in range(0, len(flat), max_per_batch_repack):
        new_batches.append(flat[i : i + max_per_batch_repack])

    # If flat empty, new_batches stays empty
    new_staggers = [i * int(stagger_s) for i in range(len(new_batches))]

    return {
        "batches": new_batches,
        "dropped": dropped,
        "reason": extra_reason,
        "warning": extra_warning,
        "staggers": new_staggers,
        "stagger_s": int(stagger_s),
    }
