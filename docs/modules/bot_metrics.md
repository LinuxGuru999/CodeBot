# Bot Metrics Module

This module provides performance tracking and telemetry for the orchestrator. It collects and persists bot run metrics including success/failure counts, token consumption, run duration, and restart statistics.

## Overview

- **Purpose**: Collects and persists bot run metrics.
- **Persistence**: Metrics are persisted atomically via tmp+replace to prevent corruption.
- **Data Retention**: Run history is capped to prevent unbounded growth; older entries are pruned automatically.

## Public API

### `record_bot_metric`

Records a bot run metric.

```python
def record_bot_metric(
    name: str,
    alive: bool,
    exit_code: Optional[int],
    tokens_this_run: int = 0,
    started_at: Optional[float] = None
) -> None
```

**Parameters:**
- `name` (str): The name of the bot.
- `alive` (bool): Whether the bot process is still running.
- `exit_code` (Optional[int]): The exit code if the process has ended (`None` if still running).
- `tokens_this_run` (int): Token consumption for this specific run (default: 0).
- `started_at` (Optional[float]): Unix timestamp when the bot started (default: current time).

**Behavior:**
- Updates success/failure counts based on `alive` and `exit_code`.
- Accumulates total tokens and duration.
- Prunes old run timestamps to stay within the configured time window (default 7 days).
- Caps the number of stored runs per bot to prevent file bloat.
- Writes data atomically to disk.

---

### `get_bot_metrics`

Retrieves metrics for a specific bot.

```python
def get_bot_metrics(name: str) -> Optional[Dict[str, Any]]
```

**Parameters:**
- `name` (str): The name of the bot.

**Returns:**
- A dictionary containing the bot's metrics (runs, successes, failures, total_tokens, total_duration_s), or `None` if no data exists for the bot or an error occurs.

---

### `get_all_metrics`

Retrieves metrics for all bots.

```python
def get_all_metrics() -> Dict[str, Any]
```

**Returns:**
- A dictionary mapping bot names to their respective metrics dictionaries. Returns an empty dict if no metrics file exists or an error occurs.

---

### `get_success_rate`

Calculates the success rate for a specific bot.

```python
def get_success_rate(name: str) -> Optional[float]
```

**Parameters:**
- `name` (str): The name of the bot.

**Returns:**
- A float between 0.0 and 1.0 representing the ratio of successful runs to total completed runs.
- Returns `None` if the bot has no recorded data or no completed runs (successes + failures == 0).

---

### `get_token_burn_rate`

Calculates the average token consumption per run for a specific bot.

```python
def get_token_burn_rate(name: str) -> Optional[float]
```

**Parameters:**
- `name` (str): The name of the bot.

**Returns:**
- A float representing the average number of tokens consumed per run.
- Returns `None` if the bot has no recorded runs.

## Configuration

### `set_state_dir`

Configures the state directory where metrics are stored.

```python
def set_state_dir(state_dir: Path) -> None
```

**Parameters:**
- `state_dir` (Path): The path to the state directory. If not set, defaults to `.codebot/state`.

**Usage:**
Call this function before recording or retrieving metrics if you need to override the default storage location.