# ticket_engine.py

Defines the v2 ticket schema per CODEBOT-ROADMAP.md §4 and the formal state machine governing ticket lifecycle. All CodeBot work flows through normalized tickets; no agent may discover an issue and immediately modify code without an authorized work item in READY or later state.

## Key Exports
- `TicketState`: Class
- `TicketClass`: Class
- `Severity`: Class
- `RiskLevel`: Class
- `Ticket`: Class
- `generate_ticket_id()`: Function
- `create_ticket()`: Function
- `evidence_hash()`: Function
- `transition()`: Function
- `to_dict()`: Function

## Invariants
- Uses stdlib plus codebot.file_lock for cross-process file locking (codebot.file_lock itself is stdlib-only via fcntl/msvcrt; exposes flock/LOCK_EX/LOCK_SH/LOCK_UN)
- Tickets are immutable once created; state changes produce new snapshots
- State transitions are validated; invalid transitions raise ValueError
- Evidence hash is SHA-256 of canonical evidence string for deduplication
- All timestamps are Unix epoch floats
- Schema version is embedded for forward compatibility

## Dependencies
- stdlib: json, enum, dataclasses, hashlib, time, re, logging, queue, shutil, threading, uuid, pathlib, typing
- intra-repo: codebot.file_lock (re-export shim for codebot.locks) — required for TicketStore persistence locking; no third-party packages
