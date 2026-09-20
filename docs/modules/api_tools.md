# api_tools.py

Provides synchronous, fail-open tools (read, write, edit, bash, grep, glob, a11y_snapshot) for the CodeBot API runner. Each tool returns a uniform dict {success, output, error} so the runner loop never needs try/except. The a11y_snapshot tool extracts Playwright accessibility trees as structured text for UI verification.

## Key Exports
- `read()`: Function
- `write()`: Function
- `bash()`: Function
- `grep()`: Function
- `edit()`: Function
- `glob()`: Function
- `a11y_snapshot()`: Function
- `batch_read()`: Function
- `batch_grep()`: Function

## Invariants
- stdlib-only: pathlib, subprocess, re, json, logging, tempfile, base64
- Every tool returns exactly {success: bool, output: str, error: str|None} and never raises
- a11y_snapshot returns structured JSON text for UI verification via standard tool results
- All I/O is bounded: read 1 MB, bash 100 KB + timeout, grep 10 KB, screenshot 2 MB
- write is atomic via tmp+replace
- edit fails if old_string not found or found multiple times (no ambiguity)
- No async, no LSP/Git/web tools beyond the seven listed
