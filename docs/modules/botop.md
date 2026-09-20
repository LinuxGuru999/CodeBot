# botop.py

Standalone bot operations CLI for CodeBot (`python -m codebot.botop ...`).
Direct bot management (status, logs, restart, pause, resume, drain) without
requiring the full orchestrator loop. Live mode renders a dashboard (agents,
tickets, throughput, claims/leases, budget, diagnostics); term mode is an
interactive terminal for iterative inspection.

## Key Exports
- `cmd_status()`: Function
- `cmd_logs()`: Function
- `cmd_restart()`: Function
- `cmd_pause()`: Function
- `cmd_resume()`: Function
- `cmd_drain()`: Function
- `cmd_clear_drain()`: Function
- `cmd_claims()`: Function
- `cmd_tickets()`: Function
- `cmd_throughput()`: Function
- `cmd_metrics()`: Function
- `cmd_budget()`: Function
- `cmd_events()`: Function
- `cmd_findings()`: Function
- `cmd_leases()`: Function
- `cmd_deadletters()`: Function
- `cmd_gatekeeper()`: Function
- `cmd_health()`: Function
- `cmd_live()`: Function
- `cmd_term()`: Function
- `main()`: Function

## Invariants
- stdlib-only
- Read-only operations by default (status, logs)
- Mutating operations (restart, pause, drain) require explicit flags
- Never modifies source code
- Fail-open per source: one corrupt file skips that source, not the bot
