# context_compactor.py

Monitors message history token count and applies progressive compaction when approaching the model's context window limit. Three strategies applied in order: tail truncation, middle summarization, full compression.

## Key Exports
- `estimate_tokens()`: Function
- `estimate_messages_tokens()`: Function
- `needs_compaction()`: Function
- `compact_messages()`: Function
- `build_compaction_checkpoint()`: Function

## Invariants
- stdlib-only (no tiktoken — uses char/4 estimation)
- Never discards system prompt or last 4 messages
- Compaction is lossy by design — summary captures key facts, not verbatim text
- Token counting is approximate (char_count / 4) — conservative margin applied
- Idempotent: compacting already-compact history is safe
