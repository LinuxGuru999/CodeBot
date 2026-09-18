# project_adapter.py

Defines the abstract interface that every CodeBot-managed project must implement. The CodeBot core (orchestrator, runner, RL engine, ticket engine, quality gates) operates exclusively through this interface, never through hardcoded paths or project-specific knowledge.

## Key Exports
- `ProjectPaths`: Class
- `ProjectTestConfig`: Class
- `DependencyPolicy`: Class
- `AutonomyConfig`: Class
- `ComponentDef`: Class
- `project_name()`: Function
- `paths()`: Function
- `test_config()`: Function
- `dependency_policy()`: Function
- `autonomy_config()`: Function

## Invariants
- stdlib-only (abc, pathlib, dataclasses, json)
- Core modules MUST use ProjectAdapter for all path/config resolution
- Adapter implementations live in the project repository, not CodeBot core
- Changing projects means swapping the adapter, not modifying core
