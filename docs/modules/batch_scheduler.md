# batch_scheduler.py

Groups ready bot manifests into tier-ordered batches for efficient dispatch. Enforces model caps and slot limits; consults token budget state before packing. All decisions are pure over in-memory manifest dicts.

## Key Exports
- `apply_caps()`: Function
- `extract_tier_from_tags()`: Function
- `needs_approval()`: Function
- `filter_approved()`: Function
- `order_by_tier()`: Function
- `pack_batches()`: Function

## Invariants
- stdlib only; no I/O, no subprocess.
- Thresholds frozen per plan: stale 86400, caps 5/2/3/2/10, stagger 20.
- Budget handling consults BEFORE packing; reasons are literal strings
- budget-shed / budget-exhausted per spec.
- Tier ordering mirrors orchestrator.py:1006 sort key (tier asc, next_run_at asc).
- Approval gate blocks TIER 4+ items unless explicitly approved.
