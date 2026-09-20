# Module: bot_metrics

## Summary
Collects and persists bot run metrics including success/failure counts, token consumption, run duration, and restart statistics for orchestrator visibility.

## Purpose
Provides performance tracking and telemetry so the orchestrator can make informed decisions about model rotation, rate limiting, and capacity planning.

## Why
Without metrics, the orchestrator operates blindly. This module centralizes metric collection with atomic writes and bounded storage to prevent unbounded growth while maintaining accurate historical data.

## Invariants
- Metrics are persisted atomically via tmp+replace to prevent corrupt JSON
- Run history is capped to `_METRICS_WINDOW_DAYS` (7 days) and `_MAX_RUNS_PER_BOT` (500 runs)
- File size is bounded to `_MAX_METRICS_FILE_BYTES` (50KB) with iterative pruning
- Never raises — logs at WARNING on I/O error

## Dependencies
- `json`, `logging`, `os`, `time`, `pathlib`, `typing`

## Exports
- `set_state_dir(state_dir)` — Configure state directory for metrics operations
- `record_bot_metric(name, alive, exit_code, tokens_this_run=0, started_at=None)` — Record a bot run metric
- `get_bot_metrics(name)` — Get metrics for a specific bot
- `get_all_metrics()` — Get all bot metrics
- `get_success_rate(name)` — Calculate success rate (0.0-1.0) for a bot
- `get_token_burn_rate(name)` — Calculate average token consumption per run

## Usage Example

```python
from codebot.bot_metrics import set_state_dir, record_bot_metric, get_success_rate
from pathlib import Path
import time

# Configure state directory
set_state_dir(Path(".codebot/state"))

# Record a successful bot run
started = time.time() - 300  # Started 5 minutes ago
record_bot_metric(
    name="documentation_implementer-3",
    alive=False,
    exit_code=0,
    tokens_this_run=12500,
    started_at=started
)

# Check success rate
rate = get_success_rate("documentation_implementer-3")
if rate is not None:
    print(f"Success rate: {rate:.2%}")

# Get all metrics for dashboard
all_metrics = get_all_metrics()
for bot_name, metrics in all_metrics.items():
    print(f"{bot_name}: {metrics['successes']} successes, {metrics['failures']} failures")
```

## Metric Schema

Each bot's metrics include:
- `runs`: List of Unix timestamps for each run (capped to 500, filtered to 7-day window)
- `successes`: Count of runs with `exit_code == 0`
- `failures`: Count of runs with `exit_code != 0`
- `total_tokens`: Cumulative token consumption across all runs
- `total_duration_s`: Cumulative run duration in seconds

## Configuration Constants

- `_METRICS_WINDOW_DAYS = 7` — Only keep runs from last 7 days
- `_MAX_RUNS_PER_BOT = 500` — Maximum runs to track per bot
- `_MAX_METRICS_FILE_BYTES = 50000` — Maximum file size before pruning
