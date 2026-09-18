# prompt_gateway.py

Compresses per-bot prompt files and assembles the final LLM message, and gates spawns so the botnet cannot starve interactive sessions. The orchestrator calls build_message() instead of inlining prompt text, and checks spawn_allowed() before every start.

## Key Exports
- `set_project_adapter()`: Function
- `get_adapter()`: Function
- `compress_prompt()`: Function
- `build_message()`: Function
- `running_count()`: Function

## Invariants
- stdlib-only, no imports from orchestrator (duck-types BotState).
- compress_prompt() only removes headings in _STRIP_PREFIXES; mission kept.
- Spawn gate never kills running bots; it only defers new spawns.
