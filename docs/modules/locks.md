# locks.py

Cross-platform file locking primitives. Single source of truth for flock
semantics; `checkpoint_manager` and `process_manager` route through it
instead of private fcntl/msvcrt/no-op duplicates (migration 007).

## Key Exports
- `flock()`: Function
- `LOCK_EX`: Constant
- `LOCK_UN`: Constant
- `LOCK_SH`: Constant
- `LOCK_NB`: Constant

## Platform Limitations
- Linux/Unix (fcntl.flock): whole-file advisory locks
- Windows (msvcrt.locking): byte-range proxy, LOCK_SH treated as LOCK_EX
- Other platforms: fail-open no-op with one-time RuntimeWarning
