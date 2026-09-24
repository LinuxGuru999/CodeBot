# process_manager.py

Bot subprocess lifecycle: start, stop, restart. Delegates health monitoring
(heartbeat reads, stuck detection, log stall analysis) to `health_monitor`
and model profiles to `model_manager`. Discovery integration via `extra_block`.

## Key Exports
- `BotConfig`: Class — name, prompt_file, interval_seconds, heartbeat_timeout, model, fallback_model, tier, max_restarts, clean_exit_wait, runner_mode
- `BotState`: Class — carries config, process, last_heartbeat, next_run_at, restart counters, consecutive_errors
- `start_bot()`, `stop_bot()`, `restart_bot()`: Functions
- `update_bot_state()`, `read_checkpoint()`, `checkpoint_path()`: Functions
- `_prepare_prompt_with_context(bot, extra_block="")`: Function — base prompt (tournament variant if active) + ticket context + scratchpad + implementation packet + extra_block (CHANGED_FILES for discovery)
- `_init_and_prepare_bot(bot, resume_checkpoint, extra_block="")`: Function — state write + heartbeat + prompt assembly; stores `_last_state`
- `_changed_files_block()` callers: discovery daemon injects git dirty list as hint
- Re-exports from health_monitor: `read_heartbeat`, `write_heartbeat`, `heartbeat_path`, `batch_read_heartbeats`, `log_path`

## Invariants
- Single process (no multiprocessing); kill: SIGTERM first (5s grace), then SIGKILL
- Heartbeat files are the ONLY communication channel from bots
- Max restarts per bot per hour: 5 (via `BotState.restart_count`); daemon discovery uses 300s heartbeat timeout
- Health functions re-exported from health_monitor for backward compatibility
- Discovery `extra_block` (CHANGED_FILES dirty-first hint) only for `DISCOVERY_ROLE_NAMES`, non-discoverers ignore it
- `prompt_gateway.build_message()` is non-mutating; extra_block ordering: ticket context → scratchpad → implementation packet → extra_block
