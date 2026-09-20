# Module: bot_metrics

## Summary
Collects and persists bot run metrics including success/failure counts, token consumption, run duration, and restart statistics.

## Why
The orchestrator needs visibility into bot performance to make informed decisions about model rotation, rate limiting, and capacity planning. This module provides atomic persistence of these metrics with automatic pruning to prevent unbounded growth.

## Invariants
- Metrics are persisted atomically via tmp+replace to prevent corruption.
- Run history is capped to `_MAX_RUNS_PER_BOT` (default 500) per bot.
- Total metrics file size is capped to `_MAX_METRICS_FILE_BYTES` (default 50KB); older runs are pruned if exceeded.
- Metrics window is limited to `_METRICS_WINDOW_DAYS` (default 7 days).

## Dependencies
- `json`, `logging`, `os`, `time`, `pathlib`
- Relies on state directory structure (`.codebot/state`).

## Exports
- `set_state_dir(state_dir)`: Configure the state directory for metrics operations.
- `record_bot_metric(name, alive, exit_code, tokens_this_run, started_at)`: Record a bot run metric.
- `get_bot_metrics(name)`: Get metrics for a specific bot.
- `get_all_metrics()`: Get all bot metrics.
- `get_success_rate(name)`: Calculate success rate (0.0-1.0) for a bot.
- `get_token_burn_rate(name)`: Calculate average token consumption per run.

## Usage Example

```python
from codebot.bot_metrics import record_bot_metric, get_success_rate

# Record a successful run
record_bot_metric(
    name="my_bot",
    alive=False,
    exit_code=0,
    tokens_this_run=1500,
    started_at=1726848000.0
)

# Check success rate
rate = get_success_rate("my_bot")
if rate is not None:
    print(f"Success rate: {rate:.2%}")
```
