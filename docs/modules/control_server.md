# control_server.py

Small ThreadingHTTPServer that exposes botnet control to an authenticated client. Runs alongside orchestrator.py on Fly.io (same VM, shared ~/Work/bots/state and ~/Work/bots/logs).

## Key Exports
- `ControlHandler`: Class
- `RateLimiter`: Class
- `validate_bot_name()`: Function
- `heartbeat_age()`: Function
- `log_tail()`: Function
- `bot_status()`: Function
- `scheduler_status()`: Function
- `retry_dead_letter()`: Function
- `main()`: Function

## Invariants
- stdlib-only
