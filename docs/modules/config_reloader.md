# config_reloader.py

Configuration and code hot-reloading for the orchestrator. Watches prompt
files, source code mtimes, and bot registry config; triggers graceful
respawns when observed content drifts from the running state.

## Key Exports
- `check_prompt_changes()`: Function
- `get_code_mtimes()`: Function
- `check_code_changes()`: Function
- `check_config_changes()`: Function

## Invariants
- Read-only observation; the orchestrator decides respawn actions
- mtime comparison only — never hashes file contents on the hot path
