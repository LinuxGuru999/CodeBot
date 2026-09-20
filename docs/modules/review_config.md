# review_config.py / review_types.py / review_metrics.py

Structured review verdicts, blocking-severity policy, and review metrics.
The gatekeeper loads `*_review.json` decisions per ticket; unresolved
BLOCKER/CRITICAL/MAJOR findings block COMPLETE. Changes to review or
gatekeeper config files trigger elevated scrutiny (gate-tampering detection).

## Key Exports
- `ReviewConfig`: Class
- `load_review_config()`: Function
- `reset_review_config()`: Function
- `ReviewDecision`: Class (review_types)
- `StructuredFinding`: Class (review_types)
- `CompletionEvidence`: Class (review_types)
- `get_review_config()`: Function (review_config)

## Invariants
- Reviewer verdicts are evidence, not authority — only the gatekeeper
  transitions to COMPLETE
- Blocking severities configurable; defaults cover BLOCKER/CRITICAL/MAJOR
