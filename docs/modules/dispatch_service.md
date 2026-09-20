# dispatch_service.py

Ticket dispatch orchestration service: pipeline state queries, agent
availability, model rotation on error, success/error ticket transitions,
rate-limit backoff, stuck-starting retry, bot status logging.

## Key Exports
- `get_pipeline_state()`: Function
- `is_needed_bot()`: Function
- `apply_agent_availability()`: Function
- `rotate_model_on_error()`: Function
- `transition_ticket_on_success()`: Function
- `transition_ticket_on_error()`: Function
- `compute_rate_limit_backoff()`: Function
- `retry_disabled_bot()`: Function
- `retry_stuck_starting()`: Function
- `log_bot_statuses()`: Function
- `batch_read_bot_statuses()`: Function

## Invariants
- Stateless helpers; orchestrator owns the tick and passes bots/store in
