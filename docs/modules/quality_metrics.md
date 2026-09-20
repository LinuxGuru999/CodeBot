# quality_metrics.py

Quality-specific metrics tracking: snapshots of completion rate, rework
rate, escaped defects, coverage delta, cost/tokens per accepted ticket,
commit survival, cross-ticket regression. Backs botop metrics views.

## Key Exports
- `QualityMetricsTracker`: Class
- `summary()`: Function — windowed aggregate (default 24h)

## Invariants
- Read-only over tickets/state; never mutates pipeline
- Snapshots bounded by max_history
