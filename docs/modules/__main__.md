# __main__.py

CLI entry (`python -m codebot ...`). Subcommands include `serve` (start the
orchestrator), `status`, `drain`, `clear-drain`, `stop-all`, `start`
(per-agent or all), plus project validation.

## Key Exports
- `main()`: Function

## Invariants
- Argument parsing only; all behavior lives in the target modules
