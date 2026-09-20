# metrics_service.py

Bot metrics collection without blocking the tick. Per-bot run records
(success, duration, stuck restarts, model changes) plus pipeline-wide
aggregates, persisted as JSON.

## Key Exports
- `bot_metrics_path()`: Function
- `collect_bot_status_snapshot()`: Function
- `pipeline_metrics_path()`: Function
- `read_pipeline_metrics()`: Function
- `save_pipeline_metrics()`: Function
- `BotMetrics`: Class
- `PipelineMetrics`: Class
- `record_bot_run()`: Function
- `record_stuck_restart()`: Function
- `record_model_change()`: Function
- `get_all_bot_metrics()`: Function
- `read_bot_metrics()`: Function
- `save_bot_metrics()`: Function

## Invariants
- Atomic JSON writes; readers tolerate missing/corrupt files
- Recording never raises into the orchestrator tick
