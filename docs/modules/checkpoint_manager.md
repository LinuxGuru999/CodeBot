# Module: checkpoint_manager

## Summary
Provides atomic read/write of bot checkpoint files for crash recovery and worker handoff.

## Why
Bots are autonomous agents that can timeout, crash, or exceed limits. Checkpoints capture progress (completed steps, current task, queue) so a replacement agent can resume without redoing work. This module ensures data integrity via atomic writes and provides fallback mechanisms for corrupt files.

## Invariants
- All writes are atomic (tmp file then `os.replace`) to prevent mid-write corruption.
- Checkpoints stay under 4KB limit (`_MAX_CHECKPOINT_BYTES`).
- Corrupt checkpoints fall back to `.bak` file if available.
- Missing checkpoints return `None` (caller handles gracefully).
- State files are locked during writes using `fcntl` (Unix) or no-op on unsupported platforms.

## Dependencies
- `json`, `logging`, `os`, `time`, `contextlib`, `pathlib`
- `fcntl` (Unix) or fallback no-op implementation.
- Relies on state directory structure (`.codebot/state`).

## Exports
- `set_state_dir(state_dir)`: Configure the state directory for checkpoint operations.
- `checkpoint_path(bot_name)`: Return path to bot's checkpoint file.
- `write_checkpoint_handoff(bot_name, payload)`: Write checkpoint atomically with standard metadata fields.
- `init_checkpoint(bot_name, scan_iteration)`: Initialize a new checkpoint file if it doesn't exist.
- `read_checkpoint(bot_name)`: Read bot's checkpoint with fallback to backup file.

## Usage Example

```python
from codebot.checkpoint_manager import init_checkpoint, write_checkpoint_handoff, read_checkpoint

# Initialize checkpoint for a new bot run
init_checkpoint("my_bot", scan_iteration=0)

# Update checkpoint with current progress
write_checkpoint_handoff("my_bot", {
    "current_task": "analyzing_module_x",
    "completed": ["step_1", "step_2"],
    "findings_so_far": ["issue_a"]
})

# Read checkpoint (e.g., after restart)
data = read_checkpoint("my_bot")
if data:
    print(f"Resuming from task: {data.get('current_task')}")
```
