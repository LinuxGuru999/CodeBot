# Module: bot_metrics

## Summary
Collects and persists bot run metrics including success/failure counts, token consumption, run duration, and restart statistics.

## Why
The orchestrator needs visibility into bot performance to make informed decisions about model rotation, rate limiting, and capacity planning. Persistent metrics enable trend analysis and automated health monitoring.

## Invariants
- Metrics are persisted atomically via tmp+replace to prevent corruption
- Run history is capped to prevent unbounded growth (max 500 runs per bot)
- File size is bounded (max 50KB) with iterative pruning if exceeded
- Serializes fully to avoid corrupt JSON on crash

## Dependencies
- `json`, `logging`, `os`, `time`, `pathlib`
- State directory: `.codebot/state/bot_metrics.json`

## Public API

### `set_state_dir(state_dir: Path) -> None`
Configure the state directory for metrics operations.

**Args:**
- `state_dir`: Path to the state directory

### `record_bot_metric(name: str, alive: bool, exit_code: Optional[int], tokens_this_run: int = 0, started_at: Optional[float] = None) -> None`
Record a bot run metric. Updates success/failure counts, total tokens, and total duration.

**Args:**
- `name`: Bot name
- `alive`: Whether bot process is still running
- `exit_code`: Exit code if process ended (None if still running)
- `tokens_this_run`: Token consumption for this run
- `started_at`: Unix timestamp when bot started

**Example:**
```python
from codebot.bot_metrics import record_bot_metric
import time

start_time = time.time()
# ... bot runs ...
record_bot_metric(
    name="bug_hunter-1",
    alive=False,
    exit_code=0,
    tokens_this_run=15000,
    started_at=start_time
)
```

### `get_bot_metrics(name: str) -> Optional[Dict[str, Any]]`
Get metrics for a specific bot.

**Args:**
- `name`: Bot name

**Returns:**
Metrics dict or None if not found.

**Example:**
```python
from codebot.bot_metrics import get_bot_metrics

metrics = get_bot_metrics("bug_hunter-1")
if metrics:
    print(f"Successes: {metrics['successes']}, Failures: {metrics['failures']}")
```

### `get_all_metrics() -> Dict[str, Any]`
Get all bot metrics.

**Returns:**
Dict mapping bot names to their metrics.

### `get_success_rate(name: str) -> Optional[float]`
Calculate success rate for a bot.

**Args:**
- `name`: Bot name

**Returns:**
Success rate (0.0-1.0) or None if no data.

**Example:**
```python
from codebot.bot_metrics import get_success_rate

rate = get_success_rate("bug_hunter-1")
if rate is not None:
    print(f"Success rate: {rate:.2%}")
```

### `get_token_burn_rate(name: str) -> Optional[float]`
Calculate average token consumption per run.

**Args:**
- `name`: Bot name

**Returns:**
Average tokens per run or None if no data.

## Metrics Data Structure
```json
{
  "bug_hunter-1": {
    "runs": [1726848000.0, 1726848300.0],
    "successes": 5,
    "failures": 1,
    "total_tokens": 90000,
    "total_duration_s": 1800.5
  }
}
```
