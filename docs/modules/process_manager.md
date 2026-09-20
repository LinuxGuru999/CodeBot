# process_manager.py

Bot subprocess lifecycle: start, stop, restart. Delegates health monitoring
(heartbeat reads, stuck detection, log stall analysis) to `health_monitor`
and model profiles to `model_manager`.

## Key Exports
- `BotConfig`: Class
- `BotState`: Class — carries config, process, last_heartbeat, restart counters
- `start_bot()`: Function
- `stop_bot()`: Function
- `restart_bot()`: Function
- `update_bot_state()`: Function
- `read_checkpoint()`: Function
- `batch_read_bot_statuses()`: Function

## Invariants
- Single process (no multiprocessing)
- Heartbeat files are the ONLY communication channel from bots
- Kill signals: SIGTERM first (5s grace), then SIGKILL
- Max restarts per bot per hour: configurable (default 5)
- Health functions re-exported from health_monitor for backward compatibility
