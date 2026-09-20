# Module: alignment_events

## Summary
Writes structured exit events for bots so the alignment pipeline can score runs and produce RL rewards for prompt evolution.

## Purpose
Captures bot exit context (exit code, duration, heartbeat age, log size) atomically, enabling the RL engine to evaluate alignment and trigger prompt optimization when needed.

## Why
The orchestrator needs to record bot exits with rich context so downstream scoring systems can assess performance and evolve prompts. This module centralizes that concern with fail-open semantics to keep the health loop running even on I/O errors.

## Invariants
- Events are written atomically via tmp+replace
- Events include all data needed for scoring (exit_code, run_duration, heartbeat_age_at_exit, log_bytes_at_exit)
- Never raises — logs at WARNING on I/O error (fail-open)

## Dependencies
- `json`, `logging`, `os`, `time`, `pathlib`, `typing`
- Reads bot state files (`.state.json`, `.heartbeat`) and log files

## Exports
- `set_dirs(state_dir, logs_dir)` — Configure directories for alignment event operations
- `write_alignment_event(bot_name, exit_code, exit_reason, started_at=None)` — Write atomic exit event
- `list_pending_events()` — List all unprocessed alignment events
- `mark_event_processed(event_file, event, score, reward, verdict)` — Mark event as processed with scoring results

## Usage Example

```python
from codebot.alignment_events import set_dirs, write_alignment_event
from pathlib import Path

# Configure directories
set_dirs(Path(".codebot/state"), Path("logs"))

# Record a bot exit
write_alignment_event(
    bot_name="implementation_planner-2",
    exit_code=0,
    exit_reason="clean",
    started_at=1726848000.0
)

# List pending events for scoring
from codebot.alignment_events import list_pending_events
pending = list_pending_events()
for event_file, event in pending:
    print(f"Pending: {event['bot']} - {event['exit_reason']}")
```

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
