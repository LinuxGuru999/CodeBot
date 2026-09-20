# Module: checkpoint_manager

## Summary
Provides atomic read/write of bot checkpoint files via tmp+replace, enabling crash recovery and worker handoff between sessions.

## Purpose
Manages persistent state for autonomous bots that can timeout, crash, or exceed limits. Checkpoints capture progress (completed steps, current task, queue) so a replacement agent can resume without redoing work. Also manages bot state files for restart budget tracking and error-disabling logic.

## Why
Bots are ephemeral processes — they may be killed by the orchestrator, hit token limits, or crash mid-task. Without checkpoints, every restart begins from scratch, wasting tokens and time. The checkpoint system must be crash-safe (atomic writes), bounded in size (<4KB to prevent disk bloat), and resilient to corruption (fallback to .bak files). State management functions enforce restart budgets to prevent runaway bot loops.

## Invariants
- All writes are atomic (tmp file then os.replace) to prevent mid-write corruption
- Checkpoints stay under `_MAX_CHECKPOINT_BYTES` (4096 bytes); oversized checkpoints are logged but still returned
- Corrupt checkpoints fall back to `.checkpoint.bak` file automatically
- Missing checkpoints return `None` — callers must handle gracefully
- State file writes use `fcntl.flock` exclusive locks to serialize concurrent access
- Restart budgets are evaluated over a rolling 1-hour window

## Dependencies
- `json`, `time`, `os`, `contextlib`, `pathlib.Path` (stdlib)
- `fcntl` (Unix) with graceful fallback on unsupported platforms
- State directory: `.codebot/state/`

## Exports

### `set_state_dir(state_dir: Path) -> None`
Configure the state directory for checkpoint operations. Creates the directory if it doesn't exist. Call once at startup.

### `checkpoint_path(bot_name: str) -> Path`
Return the path to a bot's checkpoint file (`<state_dir>/<bot_name>.checkpoint.json`).

### `write_checkpoint_handoff(bot_name: str, payload: Dict[str, Any]) -> None`
Write checkpoint atomically with standard metadata fields. Augments payload with `bot`, `updated_at`, and `updated_at_human` if not present.

**Args:**
- `bot_name`: Name of the bot (used for filename)
- `payload`: Checkpoint data (must be JSON-serializable, <4KB recommended)

### `init_checkpoint(bot_name: str, scan_iteration: int = 0) -> None`
Initialize a new checkpoint file if it doesn't already exist. Sets up default structure with empty `completed`, `queue`, and `findings_so_far` lists.

**Args:**
- `bot_name`: Name of the bot
- `scan_iteration`: Initial iteration counter (default 0)

### `read_checkpoint(bot_name: str) -> Optional[Dict[str, Any]]`
Read bot's checkpoint with automatic fallback to backup file on corruption.

**Returns:** Parsed checkpoint dict, or `None` if missing/corrupt with no valid backup.

### `update_bot_state(bot_name: str, status: str, restart_count: int = 0, consecutive_errors: int = 0, next_run_at: float = 0.0) -> None`
Update bot's state file with current status. Uses exclusive file lock to serialize concurrent writes.

**Args:**
- `bot_name`: Name of the bot
- `status`: Current status string (e.g., 'running', 'waiting', 'disabled')
- `restart_count`: Number of restarts in current hour
- `consecutive_errors`: Count of consecutive error exits
- `next_run_at`: Unix timestamp for next scheduled run

### `is_manifest_restart_budget_exceeded(manifest: Dict[str, Any], now: float) -> bool`
Check if a bot has exceeded its restart budget within the last hour.

**Args:**
- `manifest`: Bot manifest dict with `name` and `max_restarts` keys
- `now`: Current Unix timestamp

**Returns:** `True` if restart count >= max_restarts in the past hour, or if state is corrupt.

### `is_manifest_error_disabled(manifest: Dict[str, Any], max_consecutive: int = 3) -> bool`
Check if a bot should be disabled due to consecutive errors.

**Args:**
- `manifest`: Bot manifest dict with `name` key
- `max_consecutive`: Maximum allowed consecutive errors (default 3)

**Returns:** `True` if consecutive_errors >= max_consecutive, or if state is corrupt.

## Usage Example

```python
from pathlib import Path
from codebot.checkpoint_manager import (
    set_state_dir,
    init_checkpoint,
    write_checkpoint_handoff,
    read_checkpoint,
)

# Configure at startup
set_state_dir(Path(".codebot/state"))

# Initialize checkpoint for a new bot
init_checkpoint("bug_hunter", scan_iteration=1)

# Update progress during work
write_checkpoint_handoff("bug_hunter", {
    "scan_iteration": 1,
    "current_task": "CB-123",
    "completed": ["CB-100", "CB-101"],
    "queue": ["CB-124", "CB-125"],
    "findings_so_far": [{"file": "auth.py", "line": 42}],
})

# On restart, resume from checkpoint
cp = read_checkpoint("bug_hunter")
if cp:
    print(f"Resuming from task: {cp.get('current_task')}")
    print(f"Already completed: {cp.get('completed')}")
else:
    print("No checkpoint found, starting fresh")
```
