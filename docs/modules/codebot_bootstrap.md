# codebot_bootstrap.py

Entry point for adapter injection. Called once at startup before the orchestrator begins scheduling bots. Discovers the ProjectAdapter from the .codebot/project.yaml config, instantiates the correct adapter class, and injects it into every core module that has a set_project_adapter() seam.

## Key Exports
- `discover_adapter_class()`: Function
- `wire_adapter()`: Function
- `bootstrap()`: Function

## Invariants
- stdlib-only (importlib, pathlib, json)
- Idempotent: calling wire_adapter() multiple times is safe
- Fails open: if no adapter found, core modules use default paths
- Never modifies source code; only mutates runtime module globals
