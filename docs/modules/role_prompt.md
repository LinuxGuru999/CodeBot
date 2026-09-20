# role_prompt.py

Reads a portable role prompt from codebot/roles/{role_name}.md and injects project-specific context from the ProjectAdapter, producing the final system prompt sent to the LLM. This replaces the old *_BOT.md files which had Monitor knowledge baked in.

## Key Exports
- `clear_role_prompt_cache()`: Function
- `resolve_role_name()`: Function
- `load_role_template()`: Function
- `build_project_context()`: Function
- `assemble_prompt()`: Function
- `format_ticket_context()`: Function

## Invariants
- stdlib-only
- Role templates are never modified at runtime
- Project context is appended, never interpolated into template text
- Missing role file degrades gracefully (returns minimal prompt)
