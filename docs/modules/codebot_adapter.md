# codebot_adapter.py

Allows CodeBot to manage its own codebase by providing the ProjectAdapter interface with CodeBot-specific paths, components, roles, and model profiles. This replaces the 3-bot fallback registry with the full 26-role system.

## Key Exports
- `CodeBotAdapter`: Class
- `project_name()`: Function
- `paths()`: Function
- `test_config()`: Function
- `dependency_policy()`: Function
- `autonomy_config()`: Function

## Invariants
- stdlib-only
- Never modifies source code
- Protected paths include orchestrator, gatekeeper, tool_policy, constitution
- Self-improvement autonomy level is 2 (lower than normal projects)
