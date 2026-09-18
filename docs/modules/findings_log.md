# findings_log.py

Append-only JSONL file where discovery agents record findings and control agents read them to inform prioritization.

## Key Exports
- `append_finding()`: Function
- `read_findings()`: Function
- `rotate_findings()`: Function

## Invariants
- stdlib-only (json, time, pathlib, logging).
- Append-only; never modify or delete existing lines.
- Each line is a valid JSON object with FINDINGS_SCHEMA_KEYS.
- Corrupt lines are skipped on read, never crash the reader.
