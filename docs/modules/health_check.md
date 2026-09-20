# health_check.py

Standalone health evaluation: bot liveness, queue depth, dispatcher
progress, eligible-bot startup with O(1) early exit when no work is queued.

## Key Exports
- `run_dispatchers()`: Function
- `start_eligible_bots()`: Function — O(B_role) iteration over needed roles only

## Invariants
- Read-only evaluation; never mutates tickets or spawns outside caller control
