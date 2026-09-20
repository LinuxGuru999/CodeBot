# findings_log.py

Append-only JSONL file where discovery agents record findings and control agents read them to inform prioritization.

## Key Exports
- `get_adapter()`: Function
- `get_default_findings_path()`: Function
- `set_project_adapter()`: Function
- `append_finding()`: Function
- `read_findings()`: Function
- `rotate_findings()`: Function

## Invariants
- stdlib-only (json, time, pathlib, logging).
- Append-only; never modify or delete existing lines.
- Each line is a valid JSON object with FINDINGS_SCHEMA_KEYS.
- Corrupt lines are skipped on read, never crash the reader.
