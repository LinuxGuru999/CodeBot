# queue_pressure

## Purpose
Calculates pressure ratios for each pipeline stage (implementation, review, verification, etc.) to identify bottlenecks. Used by the adaptive scheduler to dynamically allocate slots.

## Key Components
- `QueuePressure`: Dataclass holding pressure ratios for each stage. A ratio > 1.0 indicates congestion.
- `calculate_pressure()`: Computes pressure based on `PipelineState` and `SchedulerConfig`.
- `determine_mode()`: Maps pressure signals to a `SchedulerMode` (e.g., `REVIEW_HEAVY`, `BALANCED`).
- `forecast_downstream_demand()`: Estimates future work in review/verification/rework based on current implementation load.

## Dependencies
- `pipeline_state`: Provides the current state snapshot.
- `scheduler_config`: Provides capacity limits and watermarks.

## Invariants
- Stdlib-only.
- Pure functions with no side effects.
- Deterministic bottleneck detection.

## Key Exports
- `compute_implementation_cap()`: Function
- `estimate_downstream_demand()`: Function
