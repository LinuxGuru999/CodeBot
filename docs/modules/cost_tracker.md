# cost_tracker.py

Bridges the fleet-wide token_ledger.json (maintained by token_budget.py) with individual ticket lifecycle events, producing per-ticket cost records that feed the economics engine described in CODEBOT-ROADMAP.md §15.

## Key Exports
- `TicketCost`: Class
- `CostTracker`: Class
- `to_dict()`: Function
- `record_phase_cost()`: Function
- `get_ticket_total()`: Function
- `rotate()`: Function
- `build_summary()`: Function

## Invariants
- stdlib-only (json, time, os, pathlib)
- All writes are atomic via tmp->replace
- Cost records are append-only; never mutated after write
- Missing ledger data degrades to zero-cost, never crashes
- Ticket IDs are validated against CB-* pattern before recording
