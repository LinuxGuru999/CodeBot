# stats_collector

## Purpose
Tracks per-model API call statistics including success rates, costs, and token usage. Persists data across restarts via atomic JSON writes.

## Key Components
- `StatsCollector`: Class that loads/saves `model_stats.json` and provides `record_call()` and `get_stats()` methods.
- Atomic writes using tmp+replace pattern to prevent corruption.

## Dependencies
- None (stdlib-only).

## Invariants
- Thread-safe via atomic writes.
- Fail-open: stats loss is acceptable, crashes are not.
- Persists to `.codebot/state/model_stats.json`.