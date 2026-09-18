# adaptive_scheduler.py

Implements the 30-slot shared worker pool scheduler described in the spec. Dynamically allocates slots across rework, verification, review, implementation, planning, validation, and discovery based on real-time pipeline pressure, backlog depth, dependency constraints, conflict detection, cost budgets, and historical yield data.

## Key Exports
- `SlotAssignment`: Class
- `SchedulerDecision`: Class
- `HysteresisState`: Class
- `AdaptiveScheduler`: Class
- `summary()`: Function
- `should_shift()`: Function
- `record()`: Function
- `add_pressure_sample()`: Function
- `tick()`: Function

## Invariants
- stdlib-only (dataclasses, time, json, logging)
- Pure allocation logic: tick() returns decisions, doesn't spawn processes
- max_slots is enforced globally at every step (§1)
- Implementers never review their own work (§18)
- Blocked tickets never execute (§20)
- Conflicting tickets are serialized (§21)
- Discovery never modifies production code (§8)
- Cost budgets are hard limits, not suggestions (§28)
- Hysteresis prevents oscillation (§36)
