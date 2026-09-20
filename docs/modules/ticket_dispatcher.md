# ticket_dispatcher.py

Routes tickets through pipeline states (READY → DECOMPOSE → PLANNING →
IMPLEMENTING → REVIEWING → VERIFYING → COMPLETE). All dispatchers share a
single per-tick TicketStore instance; transitions are batched in memory and
saved once per cycle.

## Key Exports
- `get_ticket_store()`: Function — per-tick cached TicketStore
- `clear_ticket_store_cache()`: Function
- `spawn_demand_agents()`: Function
- `dispatch_decompose_agents()`: Function
- `dispatch_planning_agents()`: Function
- `advance_reviewed_tickets()`: Function
- `gatekeeper_verify_tickets()`: Function — VERIFYING → COMPLETE/REWORK, then
  Phase 4 commits each newly-COMPLETE ticket's own files (fail-open) and
  records the SHA via `TicketStore.record_commit()`
- `route_ready_tickets()`: Function
- `process_rework_tickets()`: Function
- `recover_deferred_tickets()`: Function

## Invariants
- One TicketStore read per tick; dispatchers never instantiate their own
- Batch transitions are atomic per cycle (rollback on failure)
- Completion commits are scoped to the ticket's affected_modules, never -A
- Commit failures are fail-open: logged, never block the transition
