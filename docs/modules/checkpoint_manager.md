# Module: checkpoint_manager

## Summary
Provides atomic state persistence for bot recovery via tmp+replace, enabling crash recovery and worker handoff between sessions.

## Why
Bots are autonomous agents that can timeout, crash, or exceed limits. Checkpoints capture progress (completed steps, current task, queue) so a replacement agent can resume without redoing work. This module ensures data integrity during writes and provides fallback mechanisms for corrupt files.

## Invariants
- All writes are atomic (tmp file then `os.replace`) to prevent mid-write corruption.
- Checkpoints stay under 4KB limit; larger checkpoints are truncated with warning.
- Corrupt checkpoints fall back to `.bak` file if available.
- Missing checkpoints return `None` (caller handles gracefully).
- Uses file locking (`fcntl` on Unix, `msvcrt` on Windows) for exclusive access during writes.

## Dependencies
- `json`, `logging`, `os`, `time`, `pathlib`, `contextlib`
- Platform-specific locking: `fcntl` (Unix) or `msvcrt` (Windows)

## Exports
- `set_state_dir(state_dir)`: Configure the state directory for checkpoint operations.
- `checkpoint_path(bot_name)`: Return path to bot's checkpoint file.
- `write_checkpoint_handoff(bot_name, payload)`: Write checkpoint atomically with standard metadata fields.
- `init_checkpoint(bot_name, scan_iteration=0)`: Initialize a new checkpoint file if it doesn't exist.
- `read_checkpoint(bot_name)`: Read bot's checkpoint with fallback to backup file.

## Usage Example

```python
from codebot.checkpoint_manager import init_checkpoint, write_checkpoint_handoff, read_checkpoint

# Initialize checkpoint for a new bot
init_checkpoint("bug_hunter-1", scan_iteration=0)

# Update checkpoint with current progress
write_checkpoint_handoff(
    bot_name="bug_hunter-1",
    payload={
        "current_task": "analyzing_auth_module",
        "completed": ["scan_deps", "check_drain"],
        "queue": ["review_api", "update_docs"]
    }
)

# Read checkpoint on restart
data = read_checkpoint("bug_hunter-1")
if data:
    print(f"Resuming from: {data['current_task']}")
```
