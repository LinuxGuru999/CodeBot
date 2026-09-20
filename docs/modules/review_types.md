# review_types.py

Adversarial review and gatekeeping core types. Severity lattice, checklist
results, review phases, gate decisions, risk classes, structured findings,
review checklists, completion evidence, and review decisions.

## Key Exports
- `ChecklistResult`: Class
- `ReviewPhase`: Class
- `RiskClass`: Class
- `parse_severity()`: Function
- `risk_class_for_score()`: Function
- `severity_at_least()`: Function
- `FindingSeverity`: Class
- `ReviewChecklist`: Class
- `StructuredFinding`: Class
- `CompletionEvidence`: Class
- `ReviewDecision`: Class
- `GateDecision`: Class
- `severity_rank()`: Function
- `highest_unresolved_severity()`: Function

## Invariants
- Types only; no I/O, no store access
- `CompletionEvidence.has_missing_evidence()` is the gatekeeper's
  completeness signal alongside deterministic gates
