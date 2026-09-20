# Module: alignment_events

## Summary
Tracks and records bot exit events for reinforcement learning (RL) scoring and prompt optimization.

## Why
The orchestrator needs structured data about bot lifecycles (exit codes, duration, heartbeat age, log size) to evaluate alignment and trigger prompt evolution. This module provides a fail-open mechanism to record these events atomically.

## Invariants
- Events are written atomically via tmp+replace to prevent corruption.
- Never raises exceptions; logs warnings on I/O errors to keep the health loop running.
- Events include all necessary context for the RL engine to score runs.

## Dependencies
- `json`, `logging`, `os`, `time`, `pathlib`
- Relies on bot state files (`.codebot/state/{bot}.state.json`) and heartbeat files (`.codebot/state/{bot}.heartbeat`) for context.

## Exports
- `set_dirs(state_dir, logs_dir)`: Configure directories for event operations.
- `write_alignment_event(bot_name, exit_code, exit_reason, started_at=None)`: Record a bot exit event.
- `list_pending_events()`: List unprocessed alignment events.
- `mark_event_processed(event_file, event, score, reward, verdict)`: Mark an event as processed with scoring results.

## Usage Example

```python
from codebot.alignment_events import write_alignment_event

# Record a successful bot exit
write_alignment_event(
    bot_name="bug_hunter-1",
    exit_code=0,
    exit_reason="clean",
    started_at=1726800000.0
)

# Record a failed bot exit
write_alignment_event(
    bot_name="security_auditor-2",
    exit_code=1,
    exit_reason="error",
    started_at=1726800000.0
)
```
