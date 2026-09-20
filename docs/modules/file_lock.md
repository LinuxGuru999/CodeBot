# Module: file_lock

## Summary
Provides cross-platform advisory file locking primitives for serializing access to shared state files.

## Why
The standard `fcntl` module is Unix-only, which breaks portability on Windows. This module abstracts platform differences behind a simple API, using `fcntl` on Unix and `msvcrt` on Windows, while failing open with a warning on unsupported platforms.

## Invariants
- stdlib-only (`fcntl` on Unix, `msvcrt` on Windows).
- Fail-open on unsupported platforms: locking becomes a no-op with a `RuntimeWarning`.
- Lock semantics match `fcntl.flock` where possible.
- Thread-safe within a single process.

## Dependencies
- `os`, `sys`, `warnings`
- `fcntl` (Unix) or `msvcrt` (Windows)

## Exports
- `flock(fd, operation)`: Apply or remove an advisory lock on a file descriptor.
- Constants: `LOCK_SH` (Shared), `LOCK_EX` (Exclusive), `LOCK_UN` (Unlock), `LOCK_NB` (Non-blocking).

## Usage Example

```python
import os
from codebot.file_lock import flock, LOCK_EX, LOCK_UN

# Open a state file
path = ".codebot/state/my_bot.state.lock"
with open(path, "a+") as f:
    # Acquire exclusive lock
    flock(f, LOCK_EX)
    try:
        # Perform atomic write or read operations here
        f.seek(0)
        content = f.read()
        print(f"Locked content: {content}")
    finally:
        # Release lock
        flock(f, LOCK_UN)
```
