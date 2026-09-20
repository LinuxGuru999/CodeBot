# Module: file_lock

## Summary
Cross-platform advisory file locking primitives for serializing access to shared state files.

## Purpose
Abstracts platform-specific locking mechanisms (`fcntl` on Unix, `msvcrt` on Windows) behind a unified API, enabling CodeBot to safely coordinate access to shared state files (leases, tickets, ledgers) across different operating systems.

## Why
The `fcntl` module is Unix-only. Hard imports break on Windows, violating CodeBot's portability goal. This module provides a stdlib-only solution that degrades gracefully on unsupported platforms with warnings, ensuring the system remains functional even without proper locking.

## Invariants
- stdlib-only (`fcntl` on Unix, `msvcrt` on Windows)
- Fail-open on unsupported platforms: locking becomes no-op with one-time `RuntimeWarning`
- Lock semantics match `fcntl.flock` where possible
- Thread-safe within a single process
- Warns only once per session to avoid log spam

## Dependencies
- `os`, `sys`, `warnings`, `typing`
- `fcntl` (Unix, optional)
- `msvcrt` (Windows, optional)

## Exports
- `flock(fd, operation)` — Apply or remove advisory lock on file descriptor
- `LOCK_SH` — Shared lock constant (0)
- `LOCK_EX` — Exclusive lock constant (1)
- `LOCK_UN` — Unlock constant (8)
- `LOCK_NB` — Non-blocking flag (4)

## Usage Example

```python
from codebot.file_lock import flock, LOCK_EX, LOCK_UN, LOCK_SH
import fcntl  # for comparison on Unix

# Open a file for locking
with open("state/ticket_ledger.json", "a+") as f:
    # Acquire exclusive lock (blocks until available)
    flock(f, LOCK_EX)
    
    try:
        # Critical section: read-modify-write
        f.seek(0)
        content = f.read()
        # ... process content ...
        f.write(updated_content)
    finally:
        # Always release lock
        flock(f, LOCK_UN)

# Non-blocking lock attempt
with open("state/claims.lock", "a+") as f:
    try:
        flock(f, LOCK_EX | LOCK_NB)  # LOCK_NB makes it non-blocking
        # Got the lock, proceed
        # ... do work ...
        flock(f, LOCK_UN)
    except OSError:
        # Lock held by another process, skip or retry later
        print("Could not acquire lock, skipping...")

# Shared lock (multiple readers allowed)
with open("state/config.json", "r") as f:
    flock(f, LOCK_SH)  # Multiple processes can hold shared locks
    try:
        config = json.load(f)
    finally:
        flock(f, LOCK_UN)
```

## Platform Behavior

| Platform | Implementation | Notes |
|----------|----------------|-------|
| Linux/macOS/BSD | `fcntl.flock()` | Full POSIX advisory locking |
| Windows | `msvcrt.locking()` | Byte-range locking; locks large region from current position |
| Other | No-op + warning | Logs `RuntimeWarning` once; proceeds without locking |

## Lock Constants

- `LOCK_SH` (0): Shared lock — multiple holders allowed
- `LOCK_EX` (1): Exclusive lock — only one holder
- `LOCK_UN` (8): Release lock
- `LOCK_NB` (4): Non-blocking flag (OR with LOCK_SH or LOCK_EX)

## Windows Semantics Note

On Windows, `msvcrt.locking()` operates on byte ranges rather than whole files:
- We lock a large region (0x3FFFFFFF bytes) from the start of the file
- `LOCK_SH` is treated as `LOCK_EX` (msvcrt doesn't distinguish)
- Position is saved/restored around lock operations to minimize side effects
- Non-blocking mode relies on `OSError` (errno 36, EDEADLOCK) when lock would block

## Error Handling

- Unsupported platform: Issues `RuntimeWarning` once, then no-op
- Invalid fd: Raises `ValueError`
- Lock conflict with `LOCK_NB`: Raises `OSError`
- Other locking failures: Raises `OSError` (caller should handle)
