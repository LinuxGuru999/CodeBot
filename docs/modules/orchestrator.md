# orchestrator.py

Manages bot lifecycle: start, stop, restart, health monitoring. Detects stuck agents via heartbeat files and auto-restarts them.  The orchestrator does NO bot work itself. It is a pure process manager that spawns bot subprocesses and monitors their health.

## Key Exports
- `BotConfig`: Class
- `ModelProfile`: Class
- `BotState`: Class
- `PathConfig`: Dataclass (re-exported from `state_manager`)
- `set_project_adapter()`: Function — updates `_paths` config object
- `get_adapter()`: Function
- `is_draining()`: Function
- `worker_reserved_slots()`: Function
- `rotating_slots()`: Function

## Path Configuration
Paths (`BOTS_DIR`, `STATE_DIR`, etc.) are encapsulated in a `PathConfig`
dataclass provided by `codebot.state_manager`. Components access paths
via `get_paths()` or the `PathConfig` object returned by
`set_project_adapter()`. The old pattern of mutating module-level globals
with the `global` keyword has been removed.

Backward-compatible access via `orchestrator.BOTS_DIR`,
`orchestrator.STATE_DIR`, etc. is still available through `__getattr__`,
but internal code should always use `get_paths()` or the adapter/config
object.

## Invariants
- Single process (no multiprocessing)
- Heartbeat files are the ONLY communication channel from bots
- Orchestrator never reads bot output files (bots write directly to docs/)
- Kill signals: SIGTERM first (5s grace), then SIGKILL
- Max restarts per bot per hour: configurable (default 5)
