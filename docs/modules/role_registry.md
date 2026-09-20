# role_registry.py

Replaces hardcoded bot names (issues-bot, worker-5, security_auditor) with a composable ROLE + TASK + MODEL_PROFILE + TOOL_POLICY abstraction. Each agent instance is assembled from these four dimensions rather than being a monolithic named entity.

## Key Exports
- `AgentRole`: Class
- `AgentTask`: Class
- `LatencyClass`: Class
- `ModelProfile`: Class
- `ToolPolicy`: Class
- `RoleCategory`: Class
- `ReasoningLevel`: Class
- `CodingLevel`: Class
- `ContextSize`: Class
- `CostClass`: Class
- `get_role()`: Function
- `roles_by_category()`: Function
- `find_adversarial_reviewers()`: Function
- `satisfies()`: Function
- `allows_tool()`: Function

## Invariants
- stdlib-only (dataclasses, enum, json)
- Role definitions are immutable once created
- Tool policies reference allowlists, not blocklists (fail-closed)
- Model profiles specify capability requirements, not provider names
