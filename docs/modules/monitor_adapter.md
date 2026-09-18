# monitor_adapter.py

Translates all Monitor-specific paths, configurations, bot registries, and model profiles into the standardized ProjectAdapter interface. This is the ONLY file in the CodeBot core that contains Monitor-specific knowledge.

## Key Exports
- `MonitorAdapter`: Class
- `project_name()`: Function
- `paths()`: Function
- `test_config()`: Function
- `dependency_policy()`: Function
- `autonomy_config()`: Function

## Invariants
- This file lives in the Monitor repository, NOT in the extracted CodeBot repo
- Contains all Monitor-specific domain knowledge
- If CodeBot core references anything Monitor-specific directly, it's a bug
