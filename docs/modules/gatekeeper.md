# gatekeeper.py

The sole authority that transitions tickets from VERIFYING to COMPLETE. Implementers and reviewers produce evidence; the gatekeeper evaluates it against the quality gate policy and makes the final determination.

After a successful VERIFYING → COMPLETE transition it commits the ticket's
own files (`_commit_completed_ticket`, fail-open) and records the SHA via
`TicketStore.record_commit()` — this is the only commit path. The old
`api_runner._auto_commit` at IMPLEMENTING exit has been removed.

## Key Exports
- `Gatekeeper`: Class
- `verify_ticket()`: Function
- `get_history()`: Function

## Invariants
- stdlib-only
- Only this module may transition tickets to COMPLETE
- Every COMPLETE transition is logged with full gate evidence
- Gate failures trigger REWORK, not silent bypass
- Maximum 3 rework attempts before cycling back to REWORK for QA review
