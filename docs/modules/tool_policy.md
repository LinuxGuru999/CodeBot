# tool_policy.py

Provides path resolution and command parsing used by the API tool surface.

## Key Exports
- `resolve_workspace_path()`: Function
- `allowlisted_command()`: Function

## Invariants
- Resolved paths must remain below the supplied workspace root.
- Command parsing never permits shell metacharacters or non-allowlisted argv.
