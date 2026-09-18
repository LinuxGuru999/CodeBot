# ticket_engine.py

Defines the v2 ticket schema per CODEBOT-ROADMAP.md §4 and the formal state machine governing ticket lifecycle. All CodeBot work flows through normalized tickets; no agent may discover an issue and immediately modify code without an authorized work item in READY or later state.

## Key Exports
- `TicketState`: Class
- `TicketClass`: Class
- `Severity`: Class
- `RiskLevel`: Class
- `Ticket`: Class
- `generate_ticket_id()`: Function
- `create_ticket()`: Function
- `evidence_hash()`: Function
- `transition()`: Function
- `to_dict()`: Function

## Invariants
- stdlib-only (json, enum, dataclasses, hashlib, time, re)
- Tickets are immutable once created; state changes produce new snapshots
- State transitions are validated; invalid transitions raise ValueError
- Evidence hash is SHA-256 of canonical evidence string for deduplication
- All timestamps are Unix epoch floats
- Schema version is embedded for forward compatibility
