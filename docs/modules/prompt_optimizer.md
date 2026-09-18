# prompt_optimizer

## Purpose
Closes the Reinforcement Learning (RL) feedback loop by consuming `.evolve.json` trigger files and automatically updating agent role prompts with high-weighted improvement patterns.

## Key Components
- `PATTERN_HINTS`: Dictionary of predefined improvement patterns (e.g., `add_file_paths`, `tighten_heartbeat_format`).
- `consume_triggers()`: Scans the triggers directory, selects the best pattern based on Q-values, and appends it to the agent's prompt file.
- `_append_evolution()`: Idempotently adds an evolution section to a prompt file, respecting max length and max evolution limits.

## Dependencies
- `rl_engine`: Produces the trigger files with Q-values.
- `role_registry`: Provides the base prompt files to be evolved.

## Invariants
- Never modifies its own prompt.
- Appends only; never deletes existing content.
- Deletes trigger files after successful consumption.
- Max 5 evolutions per prompt to prevent unbounded growth.