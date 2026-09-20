# readiness.py

Decides whether a bot manifest is eligible to spawn based on interval, noop counter, file mtimes, queue content and checkpoint state. Callers inject all file contents / mtimes; no I/O inside predicates.

## Key Exports
- `effective_timeout()`: Function
- `filter_unapproved_items()`: Function
- `load_approved_ids()`: Function
- `parse_queue_ids_from_text()`: Function
- `is_due()`: Function
- `noop_ok()`: Function
- `queue_has_work()`: Function
- `signals_ok()`: Function
- `ready()`: Function

## Invariants
- All paths are cwd-relative, never absolute.
- No I/O inside predicates — callers inject mtimes dict and queue text.
- Stale threshold is 86400 seconds (24h) per plan — do not invent.
- Effective timeout comes from manifest heartbeat_timeout; batch_ctx
- heartbeat_max_gap_s is a test override only, never production precedence.
- _parse_queue_complexity logic adapted from orchestrator queue parser.
