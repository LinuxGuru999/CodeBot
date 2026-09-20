# Module: alignment_events

## Summary
Tracks and records bot exit events for RL (Reinforcement Learning) scoring and prompt optimization.

## Why
The orchestrator needs structured data about bot exits (exit code, duration, heartbeat age, log size) to evaluate alignment and trigger prompt evolution. This module provides a fail-open mechanism to record these events atomically.

## Invariants
- Events are written atomically via tmp+replace to prevent corruption.
- Events include all data needed for downstream scoring.
- Never raises exceptions; logs warnings on I/O errors to keep the health loop fail-open.

## Dependencies
- `json`, `logging`, `os`, `time`, `pathlib`
- Relies on state directory structure (`.codebot/state`) and logs directory (`logs/`).

## Exports
- `set_dirs(state_dir, logs_dir)`: Configure directories for event operations.
- `write_alignment_event(bot_name, exit_code, exit_reason, started_at)`: Record a bot exit event.
- `list_pending_events()`: List unprocessed alignment events.
- `mark_event_processed(event_file, event, score, reward, verdict)`: Mark an event as processed with scoring results.

## Usage Example

```python
from codebot.alignment_events import write_alignment_event

# Record a successful bot exit
write_alignment_event(
    bot_name="my_bot",
    exit_code=0,
    exit_reason="clean",
    started_at=1726848000.0
)

# Record a failed bot exit
write_alignment_event(
    bot_name="my_bot",
    exit_code=1,
    exit_reason="error",
    started_at=1726848000.0
)
```
