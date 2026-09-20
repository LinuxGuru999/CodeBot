# state_manager.py

Path configuration, drain/lock control, backup/restore. Single source of
truth for `PathConfig` (bots_dir, state_dir, logs_dir, backup_dir, drain,
locks, restart). Adapter injection via `set_project_adapter()` re-resolves
paths without mutating globals.

## Key Exports
- `PathConfig`: Dataclass
- `get_paths()`: Function
- `set_project_adapter()`: Function
- `get_adapter_instance()`: Function
- `is_draining()`: Function
- `set_drain()`: Function
- `clear_drain()`: Function
- `drain_status()`: Function
- `backup_botnet()`: Function
- `restore_botnet()`: Function
- `check_self_restart()`: Function
- `safe_stop_all()`: Function

## Invariants
- Paths always read dynamically; no cached module-level directory globals
- Drain/lock files live under state_dir
