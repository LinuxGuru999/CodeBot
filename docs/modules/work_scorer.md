# work_scorer.py

Assigns a numeric utility score to every schedulable work item so the scheduler can rank and select the highest-value work for available slots. Scores incorporate priority, bottleneck relief, dependency unlock value, age-based fairness, risk penalties, conflict penalties, and cost estimates.

## Key Exports
- `ScoredWorkItem`: Class
- `compute_aging_bonus()`: Function
- `compute_dependency_unlock_value()`: Function
- `compute_bottleneck_relief()`: Function
- `compute_risk_penalty()`: Function
- `compute_conflict_penalty()`: Function

## Invariants
- stdlib-only (dataclasses)
- Pure functions: no I/O, no side effects
- Scores are floats; higher = more valuable
- Priority aging never allows low-priority work to outrank security/critical
- Cost penalty is subtractive, not multiplicative (prevents zero-cost gaming)
