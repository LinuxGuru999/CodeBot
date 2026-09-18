# lease_state.py

Provides owner-checked lease acquisition, renewal, release, retry, and dead-letter operations for manifest-scheduled work.

## Key Exports
- `acquire()`: Function
- `renew()`: Function
- `release()`: Function
- `fail()`: Function
- `dead_letters()`: Function

## Invariants
- One advisory lock serializes every lease state transition.
- Writes use a pid-unique temporary file and atomic replacement.
- Dead-letter records are unique by work-item id.
