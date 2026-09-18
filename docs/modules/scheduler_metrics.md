# scheduler_metrics.py

Aggregates all scheduler telemetry required by spec §30-31: slot utilization, tickets completed per hour/day, mean cycle time, review queue age, discovery yield, cost per ticket, first-pass review rate, rework rate, human intervention rate, and productive (not just raw) slot utilization.

## Key Exports
- `SchedulerMetrics`: Class
- `CompletionRecord`: Class
- `MetricsAccumulator`: Class
- `summary()`: Function
- `record_completion()`: Function
- `record_review()`: Function
- `record_discovery()`: Function
- `record_human_intervention()`: Function

## Invariants
- stdlib-only (dataclasses, time, json, pathlib)
- All metrics computed from event history; no external dependencies
- Atomic writes via tmp->replace pattern
- Bounded history to prevent unbounded memory growth
- Pure computation functions; I/O isolated to load/save
