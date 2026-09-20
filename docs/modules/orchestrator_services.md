# orchestrator_services.py

Supporting services for the orchestrator tick: bot registry loading,
drain control, agent availability, model rotation, alignment-event
writing, bot status logging, pipeline state aggregation. Several helpers
are re-exported here for backward compatibility — canonical homes are
noted where applicable.

## Key Exports
- `load_bot_registry()`: Function
- `build_bots()`: Function
- `is_draining()`: Function
- `set_drain()`: Function
- `clear_drain()`: Function
- `drain_status()`: Function
- `apply_agent_availability()`: Function
- `retry_disabled_bot()`: Function
- `retry_stuck_starting()`: Function
- `rotate_model_on_error()`: Function (see dispatch_service for canonical)
- `write_alignment_event()`: Function — delegates to alignment_events
- `log_bot_statuses()`: Function
- `get_pipeline_state()`: Function
- `get_status()`: Function
- `print_status()`: Function
- `backup_botnet()`: Function
- `restore_botnet()`: Function
- `safe_stop_all()`: Function
- `check_self_restart()`: Function
- `set_adapter_instance()`: Function
- `is_needed_bot()`: Function
- `rotating_slots()`: Function
- `worker_reserved_slots()`: Function
- `is_error_disabled()`: Function
- `is_restart_budget_exceeded()`: Function
- `is_manifest_error_disabled()`: Function
- `is_manifest_restart_budget_exceeded()`: Function

## Invariants
- Fail-open: service errors log warnings, never crash the tick
