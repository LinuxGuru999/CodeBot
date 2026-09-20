# Module: file_lock

## Summary
Provides cross-platform advisory file locking primitives for serializing access to shared state files.

## Why
The `fcntl` module is Unix-only, but CodeBot aims for portability (Goal 1). Hard imports of `fcntl` break on Windows. This module abstracts platform differences behind a simple API, using `fcntl` on Unix and `msvcrt` on Windows, with a fail-open no-op on unsupported platforms.

## Invariants
- stdlib-only (`fcntl` on Unix, `msvcrt` on Windows).
- Fail-open on unsupported platforms: locking becomes a no-op with a `RuntimeWarning`.
- Lock semantics match `fcntl.flock` where possible.
- Thread-safe within a single process.

## Dependencies
- `os`, `sys`, `warnings`
- Platform-specific: `fcntl` (Unix) or `msvcrt` (Windows)

## Exports
- `LOCK_SH`: Shared lock constant (0).
- `LOCK_EX`: Exclusive lock constant (1).
- `LOCK_UN`: Unlock constant (8).
- `LOCK_NB`: Non-blocking flag (4).
- `flock(fd, operation)`: Apply or remove an advisory lock on a file descriptor.

## Usage Example

```python
from codebot.file_lock import flock, LOCK_EX, LOCK_UN
import os

# Open a file for locking
with open("state.lock", "a+") as f:
    try:
        # Acquire exclusive lock
        flock(f, LOCK_EX)
        
        # Perform critical section operations
        f.write("Critical data\n")
        f.flush()
        
    finally:
        # Release lock
        flock(f, LOCK_UN)
```
