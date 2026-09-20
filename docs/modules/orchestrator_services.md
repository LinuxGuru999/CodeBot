# orchestrator_services.py

Supporting services for the orchestrator tick: manifest budgets, error
disables, model rotation helpers, alignment-event writing, bot status
logging, pipeline state aggregation.

## Key Exports
- `write_alignment_event()`: Function — delegates to alignment_events
- `log_bot_statuses()`: Function
- `get_pipeline_state()`: Function
- `rotate_model_on_error()`: Function (see dispatch_service for canonical)

## Invariants
- Fail-open: service errors log warnings, never crash the tick
