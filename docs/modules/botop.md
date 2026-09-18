# botop.py

Provides direct bot management operations (status, logs, restart, pause, resume, drain) without requiring the full orchestrator loop. Equivalent to Monitor's botop.py but portable across any CodeBot-managed project.  Live mode `botop live` renders a live dashboard: agents, tickets, throughput, claims/leases, budget, diagnostics. `botop term` drops into an interactive terminal for iterative inspection.

## Key Exports
- `cmd_status()`: Function
- `cmd_logs()`: Function
- `cmd_restart()`: Function
- `cmd_pause()`: Function
- `cmd_resume()`: Function

## Invariants
- stdlib-only
- Read-only operations by default (status, logs)
- Mutating operations (restart, pause, drain) require explicit flags
- Never modifies source code
- Fail-open per source: one corrupt file skips that source, not the bot
