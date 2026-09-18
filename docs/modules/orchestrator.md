# orchestrator.py

Manages bot lifecycle: start, stop, restart, health monitoring. Detects stuck agents via heartbeat files and auto-restarts them.  The orchestrator does NO bot work itself. It is a pure process manager that spawns bot subprocesses and monitors their health.

## Key Exports
- `BotConfig`: Class
- `ModelProfile`: Class
- `BotState`: Class
- `set_project_adapter()`: Function
- `get_adapter()`: Function
- `is_draining()`: Function
- `worker_reserved_slots()`: Function
- `rotating_slots()`: Function

## Invariants
- Single process (no multiprocessing)
- Heartbeat files are the ONLY communication channel from bots
- Orchestrator never reads bot output files (bots write directly to docs/)
- Kill signals: SIGTERM first (5s grace), then SIGKILL
- Max restarts per bot per hour: configurable (default 5)
