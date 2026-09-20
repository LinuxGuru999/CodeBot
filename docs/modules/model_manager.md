# model_manager.py / metrics_service.py

Model profiles (heartbeat multipliers, log-stall thresholds, lockup risk,
restart cooldowns) and per-bot metrics snapshots (effective timeout,
heartbeat age, restart counts).

## Key Exports
- `model_profile()`: Function (model_manager)
- `ModelProfile`: Class (model_manager)
- `MODEL_PROFILES`: Constant (model_manager)

## Invariants
- Unknown models degrade to safe defaults (180s stall, base timeout)
- Profiles are data, not policy; callers decide restart/scale actions
