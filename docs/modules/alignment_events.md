# Module: alignment_events

## Summary
Tracks and records bot exit events for RL (Reinforcement Learning) scoring and prompt optimization.

## Why
The orchestrator needs structured exit data (exit code, duration, heartbeat age, log size) so the alignment pipeline can evaluate bot behavior and produce RL rewards for prompt evolution. This enables automated detection of misaligned or failing bots.

## Invariants
- Events are written atomically via tmp+replace to prevent corruption
- Events include all data needed for downstream scoring
- Never raises exceptions — logs at WARNING on I/O error (fail-open design)
- Events are marked `processed=False` until consumed by the alignment scorer

## Dependencies
- `json`, `logging`, `os`, `time`, `pathlib`
- State directory structure: `.codebot/state/alignment_events/`
- Bot state files: `.codebot/state/{bot_name}.state.json`
- Heartbeat files: `.codebot/state/{bot_name}.heartbeat`
- Log files: `logs/{bot_name}.log`

## Public API
- `collect_reviewer_feedback_for_trigger()`: Function

### `set_dirs(state_dir: Path, logs_dir: Path) -> None`
Configure directories for alignment event operations. Must be called before other functions if using non-default paths.

**Args:**
- `state_dir`: Path to the state directory
- `logs_dir`: Path to the logs directory

### `write_alignment_event(bot_name: str, exit_code: Optional[int], exit_reason: str, started_at: Optional[float] = None) -> None`
Write a bot exit event atomically. Called immediately after detecting a bot exit.

**Args:**
- `bot_name`: Name of the bot
- `exit_code`: Exit code (0=success, non-zero=error, None=stuck/killed)
- `exit_reason`: Human-readable reason (e.g., 'clean', 'error', 'stuck')
- `started_at`: Unix timestamp when bot started (optional; auto-detected from state file if omitted)

**Example:**
```python
from codebot.alignment_events import write_alignment_event

# Record a successful bot exit
write_alignment_event(
    bot_name="bug_hunter-1",
    exit_code=0,
    exit_reason="clean",
    started_at=1726848000.0
)

# Record a stuck bot (no exit code)
write_alignment_event(
    bot_name="security_auditor-3",
    exit_code=None,
    exit_reason="stuck"
)
```

### `list_pending_events() -> List[tuple]`
List all unprocessed alignment events.

**Returns:**
List of `(event_path, event_data)` tuples for events with `processed=False`.

**Example:**
```python
from codebot.alignment_events import list_pending_events

pending = list_pending_events()
for path, event in pending:
    print(f"{event['bot']}: exit_code={event['exit_code']}, reason={event['exit_reason']}")
```

### `mark_event_processed(event_file: Path, event: Dict[str, Any], score: float, reward: float, verdict: str) -> None`
Mark an alignment event as processed with scoring results.

**Args:**
- `event_file`: Path to the event file
- `event`: Event data dict
- `score`: Numerical alignment score
- `reward`: RL reward value
- `verdict`: Human-readable verdict (e.g., 'aligned', 'misaligned', 'inconclusive')

## Event Payload Structure
```json
{
  "bot": "bug_hunter-1",
  "exit_code": 0,
  "exit_reason": "clean",
  "exit_time": 1726848000.123,
  "exit_time_human": "2024-09-20T12:00:00Z",
  "run_duration": 300.5,
  "started_at": 1726847700.0,
  "log_path": "logs/bug_hunter-1.log",
  "stream_path": "logs/bug_hunter-1.stream.json",
  "checkpoint_path": "state/bug_hunter-1.checkpoint.json",
  "heartbeat_age_at_exit": 5.2,
  "log_bytes_at_exit": 15234,
  "processed": false,
  "processed_at": null,
  "version": 1
}
```
