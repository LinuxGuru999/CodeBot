# lifecycle_scheduler.py / scheduler.py / worker_scaler.py / freeze_detector.py / config_reloader.py / __main__.py

Lifecycle and scheduling support: periodic scheduling entry points,
worker-count scaling against queue pressure, freeze detection, hot-reload
of prompts/source/registry, CLI entry (`python -m codebot serve`).

## Key Exports
- `LifecycleDispatchEntry`: Class
- `LifecyclePhase`: Class
- `PhaseDemand`: Class
- `compute_agent_demand()`: Function
- `get_queue_demands()`: Function
- `lifecycle_scheduler.dispatch_lifecycle()`: Function
- `worker_scaler`: worker-count scaling helpers
- `freeze_detector`: stuck-process detection helpers
- `config_reloader`: hot-reload helpers

## Invariants
- Schedulers propose; the orchestrator disposes (spawns/restarts)
- Scaling respects gateway concurrency caps and drain state
