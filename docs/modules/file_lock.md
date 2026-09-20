# Module: file_lock

## Summary
Cross-platform advisory file locking primitives for serializing access to shared state files.

## Purpose
Provides a portable `flock()` function that abstracts platform differences between Unix (`fcntl.flock`) and Windows (`msvcrt.locking`). Used by CodeBot modules that need to serialize concurrent access to shared state files such as leases, tickets, and ledgers.

## Why
The `fcntl` module is Unix-only. Hard-importing it breaks portability on Windows (violating Goal 1). Rather than scattering platform checks throughout the codebase, this module centralizes the abstraction behind a single API. On unsupported platforms, locking degrades gracefully to a no-op with a one-time warning, ensuring the system remains functional even if concurrent access protection is unavailable.

## Invariants
- stdlib-only: uses `fcntl` on Unix, `msvcrt` on Windows
- Fail-open on unsupported platforms: locking becomes a no-op with a single `RuntimeWarning`
- Lock constants (`LOCK_SH`, `LOCK_EX`, `LOCK_UN`, `LOCK_NB`) mirror `fcntl` values for API compatibility
- Thread-safe within a single process
- On Windows, `LOCK_SH` is treated as `LOCK_EX` because `msvcrt.locking` does not support shared locks
- On Windows, non-blocking semantics are implemented via catching `OSError` from `LK_LOCK`

## Dependencies
- `os`, `sys`, `warnings` (stdlib)
- `fcntl` (Unix only, optional import)
- `msvcrt` (Windows only, optional import)

## Exports

### Constants

| Constant | Value | Description |
|----------|-------|-------------|
| `LOCK_SH` | 0 | Shared lock |
| `LOCK_EX` | 1 | Exclusive lock |
| `LOCK_UN` | 8 | Unlock |
| `LOCK_NB` | 4 | Non-blocking flag (OR with lock type) |

### `flock(fd: int | TextIO, operation: int) -> None`
Apply or remove an advisory lock on a file descriptor.

**Args:**
- `fd`: File descriptor (int) or file object with `fileno()` method
- `operation`: Lock operation constant (`LOCK_SH`, `LOCK_EX`, `LOCK_UN`), optionally OR'd with `LOCK_NB`

**Raises:**
- `OSError`: If locking fails (e.g., would block when `LOCK_NB` is set, or invalid fd)
- `ValueError`: If fd is invalid

**Platform notes:**
- Unix: delegates directly to `fcntl.flock`
- Windows: uses `msvcrt.locking` with byte-range locking over a large region; saves/restores file position
- Unsupported: emits `RuntimeWarning` once, then proceeds without locking

## Usage Example

```python
from pathlib import Path
from codebot.file_lock import flock, LOCK_EX, LOCK_UN

state_file = Path(".codebot/state/tickets.json")

with state_file.open("a+") as f:
    # Acquire exclusive lock (blocks until available)
    flock(f, LOCK_EX)
    try:
        # Critical section: read-modify-write shared state
        f.seek(0)
        data = f.read()
        f.seek(0)
        f.truncate()
        f.write(updated_data)
    finally:
        # Always release the lock
        flock(f, LOCK_UN)
```

### Non-blocking example

```python
from codebot.file_lock import flock, LOCK_EX, LOCK_NB
import errno

try:
    flock(fd, LOCK_EX | LOCK_NB)
except OSError as e:
    if e.errno == errno.EAGAIN or e.errno == errno.EDEADLOCK:
        print("Lock held by another process, skipping")
    else:
        raise
```