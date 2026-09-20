# Module: bot_metrics

## Summary
Collects and persists bot run metrics including success/failure counts, token consumption, run duration, and restart statistics for orchestrator visibility.

## Purpose
Tracks per-bot performance telemetry over a rolling window. The orchestrator uses this data to make informed decisions about model rotation, rate limiting, capacity planning, and identifying bots that consistently fail or consume excessive tokens.

## Why
Without centralized metrics, the orchestrator operates blind — it cannot distinguish between a bot that fails due to prompt drift versus one that hits rate limits, nor can it track whether a new model version improves success rates. Metrics must be persisted atomically to survive crashes mid-write, and bounded in size to prevent unbounded disk growth from long-running orchestrators.

## Invariants
- Metrics are persisted atomically via tmp+replace (PID-tagged temp files)
- Run history is capped to `_MAX_RUNS_PER_BOT` (500) entries per bot to prevent unbounded growth
- Total file size is pruned iteratively if it exceeds `_MAX_METRICS_FILE_BYTES` (50KB)
- Only runs within `_METRICS_WINDOW_DAYS` (7 days) are retained in the active window
- All public functions are fail-open: exceptions are logged at WARNING and return safe defaults

## Dependencies
- `json`, `time`, `os`, `pathlib.Path` (stdlib)
- State directory: `.codebot/state/bot_metrics.json`

## Exports

### `set_state_dir(state_dir: Path) -> None`
Configure the state directory for metrics operations. Creates the directory if it doesn't exist. Call once at startup.

### `record_bot_metric(name: str, alive: bool, exit_code: Optional[int], tokens_this_run: int = 0, started_at: Optional[float] = None) -> None`
Record a single bot run metric. Updates success/failure counters, appends timestamp to run history, accumulates token count and duration.

**Args:**
- `name`: Bot name (used as key in metrics dict)
- `alive`: Whether bot process is still running (if True, no success/failure recorded)
- `exit_code`: Exit code if process ended (None if still running; 0=success, non-zero=failure)
- `tokens_this_run`: Token consumption for this run (default 0)
- `started_at`: Unix timestamp when bot started (used to compute duration)

### `get_bot_metrics(name: str) -> Optional[Dict[str, Any]]`
Get metrics for a specific bot.

**Returns:** Metrics dict with keys `runs`, `successes`, `failures`, `total_tokens`, `total_duration_s`, or None if not found.

### `get_all_metrics() -> Dict[str, Any]`
Get all bot metrics.

**Returns:** Dict mapping bot names to their metrics dicts. Empty dict on error.

### `get_success_rate(name: str) -> Optional[float]`
Calculate success rate for a bot over its recorded history.

**Returns:** Success rate (0.0-1.0) or None if no completed runs exist.

### `get_token_burn_rate(name: str) -> Optional[float]`
Calculate average token consumption per run.

**Returns:** Average tokens per run or None if no data.

## Usage Example

```python
from pathlib import Path
from codebot.bot_metrics import (
    set_state_dir,
    record_bot_metric,
    get_success_rate,
    get_token_burn_rate,
)

# Configure at startup
set_state_dir(Path(".codebot/state"))

# After a bot run completes
record_bot_metric(
    name="bug_hunter",
    alive=False,
    exit_code=0,
    tokens_this_run=4500,
    started_at=1716120000.0
)

# Query for decision-making
rate = get_success_rate("bug_hunter")
if rate is not None and rate < 0.5:
    print("Consider rotating model or adjusting prompt")

burn = get_token_burn_rate("bug_hunter")
print(f"Average tokens per run: {burn}")
```
