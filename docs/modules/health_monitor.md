# health_monitor.py

Heartbeat monitoring and stuck-bot detection. Extracted from process_manager.py
to separate health concerns from process lifecycle management.

Accepts `BotState` objects directly (duck-typed `.config` + `.last_heartbeat`);
field extraction degrades to safe defaults rather than raising. Internal
`_model_profile` / `_read_heartbeat` / `_is_log_stalled` bridges honor
orchestrator-level test patches.

## Key Exports
- `is_stuck()`: Function — BotState + optional heartbeat_cache → bool
- `is_log_stalled()`: Function — BotState or (name, model) → bool
- `effective_heartbeat_timeout()`: Function — BotState or explicit fields → seconds
- `read_heartbeat()`: Function
- `batch_read_heartbeats()`: Function
- `heartbeat_path()`: Function
- `write_heartbeat()`: Function
- `log_mtime()`: Function

## Invariants
- Heartbeat files are the ONLY communication channel from bots
- Stuck detection uses model-specific timeouts based on lockup risk
- Log stall detection is secondary to heartbeat age
- High-risk models require log-stall confirmation before declaring stuck
- Batch reading heartbeats is preferred for performance
