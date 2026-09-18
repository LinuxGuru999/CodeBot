# migrate_queue.py

One-time migration script that parses the legacy Monitor QUEUE.md format and creates normalized tickets in the CodeBot ticket engine. Run once during big-bang cutover, then delete.  Usage: python3 -m codebot.migrate_queue --project /path/to/monitor --queue docs/triage/QUEUE.md

## Key Exports
- `parse_queue_md()`: Function
- `migrate()`: Function
- `main()`: Function

## Invariants
- stdlib-only
