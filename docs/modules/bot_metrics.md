# Module: bot_metrics

## Summary
Collects and persists bot run metrics including success/failure counts, token consumption, run duration, and restart statistics.

## Why
The orchestrator needs visibility into bot performance to make informed decisions about model rotation, rate limiting, and capacity planning. This module provides a centralized way to track and query these metrics.

## Invariants
- Metrics are persisted atomically via tmp+replace to prevent corruption.
- Run history is capped (default 500 runs per bot) to prevent unbounded growth.
- File size is monitored and pruned if it exceeds 50KB.
- Serializes fully to avoid corrupt JSON.

## Dependencies
- `json`, `logging`, `os`, `time`, `pathlib`

## Exports
- `set_state_dir(state_dir)`: Configure the state directory for metrics operations.
- `record_bot_metric(name, alive, exit_code, tokens_this_run=0, started_at=None)`: Record a bot run metric.
- `get_bot_metrics(name)`: Get metrics for a specific bot.
- `get_all_metrics()`: Get all bot metrics.
- `get_success_rate(name)`: Calculate success rate for a bot (0.0-1.0).
- `get_token_burn_rate(name)`: Calculate average token consumption per run.

## Usage Example

```python
from codebot.bot_metrics import record_bot_metric, get_success_rate

# Record a successful run
record_bot_metric(
    name="bug_hunter-1",
    alive=False,
    exit_code=0,
    tokens_this_run=1500,
    started_at=1726800000.0
)

# Check success rate
rate = get_success_rate("bug_hunter-1")
print(f"Success rate: {rate}")
```
