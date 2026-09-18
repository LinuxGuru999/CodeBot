# api_runner.py

Provides run_bot() — a minimal, stdlib-only bot executor that POSTs to the dialagram OpenAI-compatible endpoint, executes the four allowed tools via api_tools, and handles heartbeat / checkpoint / drain / retry semantics.

## Key Exports
- `set_project_adapter()`: Function
- `get_adapter()`: Function
- `run_batch()`: Function
- `run_bot()`: Function

## Invariants
- stdlib-only: urllib.request, json, time, os, pathlib (+ api_tools); no streaming
- Single POST timeout is 120 s; max 50 tool-call iterations per run
- Max total API-call retries is 5 per run (429 exponential backoff capped at 60 s,
- timeout/connection retries capped at 3 with backoff)
- All tool results are JSON-serialized before appending as tool messages
