# task_splitter.py

When an agent hits SESSION_TIMEOUT, a fatal error, or a rate limit during a large ticket, the task splitter breaks the remaining work into smaller sub-tickets that can be claimed by different workers. Each sub-ticket references the parent and includes a scratchpad handoff note.

## Key Exports
- `compute_chunks()`: Function
- `should_split()`: Function
- `split_ticket()`: Function

## Invariants
- stdlib-only
- Sub-tickets always depend on parent ticket ID
- Split only happens on explicit trigger (timeout/failure/request)
- Never splits tickets already in COMPLETE or REJECTED state
- Max 10 sub-tickets per split to prevent unbounded fan-out
