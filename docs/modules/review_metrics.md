# review_metrics.py

Review and gatekeeper calibration metrics. Per-reviewer stats, gatekeeper
decision stats, and escaped-defect records for tuning blocking severities.

## Key Exports
- `check_quality_health()`: Function
- `get_audit_stats()`: Function
- `record_audit_result()`: Function
- `select_tickets_for_audit()`: Function
- `record_reviewer_metric()`: Function
- `record_gatekeeper_metric()`: Function
- `record_escaped_defect()`: Function
- `get_reviewer_stats()`: Function
- `get_gatekeeper_stats()`: Function

## Invariants
- Metrics only; never influence a live gate decision
