# risk_classifier.py

Computes a numeric risk score for each ticket based on its class, severity, affected modules, security impact, blast radius, and dependency count. The score drives autonomy level decisions: low-risk tickets proceed autonomously, high-risk tickets generate QA-stage recommendations resolved through the adversarial review pipeline.

## Key Exports
- `classify_risk()`: Function
- `autonomy_level_for_risk()`: Function
- `is_autonomous_allowed()`: Function

## Invariants
- stdlib-only
- Score range: 0-100
- Scoring is deterministic: same inputs always produce same output
- No I/O, no side effects — pure computation
- Constitution-protected categories always score >= 70 (generate QA-stage recommendations)
