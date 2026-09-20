# orchestrator.py

Process lifecycle coordinator: delegates to process_manager (subprocesses),
ticket_dispatcher (routing), worker_scaler (registry/limits), state_manager
(paths/drain), and dispatch_service (pipeline helpers). Runs the main event
loop (`check_all_bots` per tick, `main` for CLI/serve).

Health functions (`is_stuck`, `is_log_stalled`, `effective_heartbeat_timeout`,
heartbeats, checkpoints, bot state) are re-exported from process_manager /
health_monitor for backward compatibility — see those pages for semantics.

## Key Exports
- `is_error_disabled()`: Function
- `is_manifest_error_disabled()`: Function
- `is_manifest_restart_budget_exceeded()`: Function
- `is_restart_budget_exceeded()`: Function
- `check_all_bots()`: Function — one tick: heartbeats, exits, stuck, dispatch
- `get_status()`: Function
- `print_status()`: Function
- `main()`: Function
- `set_project_adapter()`: Function — updates `_paths` config object
- `is_draining()`: Function
- `read_prompt_with_mtime()`: Function
- Re-exports: `BotConfig`, `BotState`, `PathConfig`, `is_stuck`,
  `is_log_stalled`, `effective_heartbeat_timeout`, `model_profile`,
  `read_heartbeat`, `heartbeat_path`, `checkpoint_path`, `read_checkpoint`,
  `update_bot_state`, `log_mtime`, `batch_read_heartbeats`,
  `worker_reserved_slots`, `rotating_slots`

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
