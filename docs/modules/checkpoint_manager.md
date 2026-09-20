# Module: checkpoint_manager

## Summary
Provides atomic read/write of bot checkpoint files via tmp+replace. Checkpoints enable crash recovery and worker handoff between sessions.

## Why
Bots are autonomous agents that can timeout, crash, or exceed limits. Checkpoints capture progress (completed steps, current task, queue) so a replacement agent can resume without redoing work. Atomic writes prevent corruption during crashes.

## Invariants
- All writes are atomic (tmp file then os.replace) to prevent mid-write corruption
- Checkpoints stay under 4KB limit (warning logged if exceeded)
- Corrupt checkpoints fall back to .bak file for recovery
- Missing checkpoints return None (caller handles gracefully)
- Uses file locking (fcntl/msvcrt) for concurrent access safety

## Dependencies
- `json`, `logging`, `os`, `time`, `contextlib`, `pathlib`
- `fcntl` (Unix) or `msvcrt` (Windows) for file locking
- State directory: `.codebot/state/`

## Public API
- `is_manifest_error_disabled()`: Function
- `is_manifest_restart_budget_exceeded()`: Function
- `update_bot_state()`: Function

### `set_state_dir(state_dir: Path) -> None`
Configure the state directory for checkpoint operations.

**Args:**
- `state_dir`: Path to the state directory

### `checkpoint_path(bot_name: str) -> Path`
Return path to bot's checkpoint file.

**Args:**
- `bot_name`: Name of the bot

**Returns:**
Path object pointing to `{state_dir}/{bot_name}.checkpoint.json`

### `write_checkpoint_handoff(bot_name: str, payload: Dict[str, Any]) -> None`
Write checkpoint atomically with standard metadata fields.

**Args:**
- `bot_name`: Name of the bot (used for filename)
- `payload`: Checkpoint data (must be JSON-serializable)

The payload is augmented with:
- `bot`: bot_name (if not present)
- `updated_at`: current Unix timestamp
- `updated_at_human`: ISO format timestamp

**Example:**
```python
from codebot.checkpoint_manager import write_checkpoint_handoff

write_checkpoint_handoff("bug_hunter-1", {
    "scan_iteration": 5,
    "current_task": "review_auth_module",
    "completed": ["read_source", "analyze_deps"],
    "queue": ["check_api_contract", "verify_tests"],
    "findings_so_far": ["missing_docs"]
})
```

### `init_checkpoint(bot_name: str, scan_iteration: int = 0) -> None`
Initialize a new checkpoint file if it doesn't exist.

**Args:**
- `bot_name`: Name of the bot
- `scan_iteration`: Initial iteration counter (default 0)

**Example:**
```python
from codebot.checkpoint_manager import init_checkpoint

init_checkpoint("security_auditor-2", scan_iteration=0)
```

### `read_checkpoint(bot_name: str) -> Optional[Dict[str, Any]]`
Read bot's checkpoint with fallback to backup file.

**Args:**
- `bot_name`: Name of the bot

**Returns:**
Parsed checkpoint dict, or None if missing/corrupt. Falls back to .bak file if primary is corrupt.

**Example:**
```python
from codebot.checkpoint_manager import read_checkpoint

checkpoint = read_checkpoint("bug_hunter-1")
if checkpoint:
    print(f"Resuming at iteration {checkpoint.get('scan_iteration')}")
    current_task = checkpoint.get('current_task')
else:
    print("No checkpoint found, starting fresh")
```

## Checkpoint Data Structure
```json
{
  "bot": "bug_hunter-1",
  "updated_at": 1726848000.123,
  "updated_at_human": "2024-09-20T12:00:00Z",
  "scan_iteration": 5,
  "current_task": "review_auth_module",
  "completed": ["read_source", "analyze_deps"],
  "queue": ["check_api_contract", "verify_tests"],
  "findings_so_far": ["missing_docs"],
  "reason": "handoff"
}
```
