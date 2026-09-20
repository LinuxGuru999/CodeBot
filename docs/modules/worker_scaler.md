# worker_scaler.py

Bot registry, resource checks, and scaling limits. Builds bot states from
the registry, computes rotating vs reserved slots, gates spawns on memory
and manifest budgets.

## Key Exports
- `is_manifest_error_disabled()`: Function
- `is_manifest_restart_budget_exceeded()`: Function
- `load_bot_registry()`: Function
- `build_bots()`: Function
- `rotating_slots()`: Function
- `worker_reserved_slots()`: Function

## Invariants
- Advisory limits; the orchestrator enforces spawn/cap decisions
