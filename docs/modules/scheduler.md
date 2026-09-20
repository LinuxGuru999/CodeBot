# scheduler.py

Ticket dispatch coordination. Thin dispatch entry points that route
IMPLEMENTING work to implementers and REVIEWING work to reviewers.

## Key Exports
- `dispatch_tickets_to_implementers()`: Function
- `dispatch_tickets_to_reviewers()`: Function

## Invariants
- Coordination only; `ticket_dispatcher` owns store access and transitions
