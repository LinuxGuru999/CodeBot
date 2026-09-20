# token_budget.py

Provides atomic, UTC-day token accounting and pure budget-state decisions for the scheduler.

## Key Exports
- `current_day_utc()`: Function
- `get_budget_state()`: Function
- `set_project_adapter()`: Function
- `get_adapter()`: Function
- `record_usage()`: Function
- `record_usage_locked()`: Function
- `day_total()`: Function

## Invariants
- stdlib-only
