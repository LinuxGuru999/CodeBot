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
- `collect_reviewer_feedback_for_trigger(bot_name)`: Collect reviewer feedback for tickets assigned to a bot. Used when triggering prompt evolution after consecutive failures (rework_count >= 3). Returns list of feedback dicts with ticket_id, reviewer, file, description, recommendation.

## Event Schema

Each exit event includes:
- `bot`: Bot name
- `exit_code`: Exit code (0=success, non-zero=error, None=stuck/killed)
- `exit_reason`: Human-readable reason ('clean', 'error', 'stuck')
- `exit_time`: Unix timestamp of exit
- `exit_time_human`: ISO format timestamp
- `run_duration`: Duration in seconds (if started_at available)
- `started_at`: Unix timestamp when bot started
- `log_path`, `stream_path`, `checkpoint_path`: Paths to related files
- `heartbeat_age_at_exit`: Seconds since last heartbeat
- `log_bytes_at_exit`: Log file size in bytes
- `processed`: Boolean flag for scoring pipeline
- `processed_at`: Timestamp when scored (None until processed)
- `version`: Schema version (currently 1)

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
