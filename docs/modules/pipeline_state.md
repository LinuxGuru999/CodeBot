# pipeline_state

## Purpose
Provides a frozen, point-in-time snapshot of the entire CodeBot engineering pipeline. This dataclass is consumed by the adaptive scheduler to make allocation decisions without querying multiple live systems.

## Key Components
- `WorkerSlot`: Represents an occupied worker slot with heartbeat and lease info.
- `PipelineState`: Immutable dataclass containing counts for all ticket states, active workers, budget status, and dependency conflicts.
- `inspect_pipeline()`: Factory function to build a `PipelineState` from live `TicketStore` and worker data.
- `PipelineInspector`: Class-based inspector for backward compatibility.

## Dependencies
- `ticket_engine`: For ticket state counts.
- `dependency_graph`: For unsatisfied dependency detection.
- `role_registry`: For worker categorization by role.

## Invariants
- Stdlib-only.
- Frozen dataclasses ensure immutability.
- All counts are non-negative.