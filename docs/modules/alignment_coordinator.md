# alignment_coordinator.py

Post-exit alignment events and pipeline coordination. Thin wrapper that
delegates to `alignment_events.write_alignment_event` (fail-open).

## Key Exports
- `write_alignment_event()`: Function

## Invariants
- Never raises; logs at WARNING on I/O error so the health loop stays fail-open
