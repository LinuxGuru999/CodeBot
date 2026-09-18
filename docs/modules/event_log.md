# event_log.py

Provides append-only JSONL event records and bounded retrieval for operators.

## Key Exports
- `sanitize_data()`: Function
- `append_event()`: Function
- `read_events()`: Function

## Invariants
- Every event has a version, type, and structured data payload.
- Appends are atomic O_APPEND writes, safe for concurrent writers.
- Readers return at most the requested bounded tail.
