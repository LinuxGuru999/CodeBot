# scratchpad.py

Provides a JSON-based scratchpad that agents use to persist intermediate state between tool calls, survive timeouts, and hand off work to other workers when they fail or hit rate limits.

## Key Exports
- `ScratchpadState`: Class
- `load_scratchpad()`: Function
- `save_scratchpad()`: Function
- `clear_scratchpad()`: Function
- `create_handoff_note()`: Function
- `to_dict()`: Function

## Invariants
- stdlib-only (json, os, time, pathlib)
- Atomic writes via tmp + os.replace
- Max 8KB per scratchpad (prevents bloat)
- Schema versioned for forward compatibility
- Read is fail-open (corrupt scratchpad = fresh start)
