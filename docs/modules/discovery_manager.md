# discovery_manager.py — DEPRECATED

Archived/removed. Prior classic cooldown/yield/diversity/saturation manager for the pre-scheduler_v2 adaptive scheduler.

Current discovery uses `codebot/discovery_daemon.py` (5-slot daemon outside scheduler, round-robin 9 roles, 60s per role, 5 concurrent, dirty-file injection, cheap-tier pin). Related wiring lives in `process_manager.extra_block` and `codebot_adapter` discovery interval.

## Key Exports
- No longer imported by orchestrator. `codebot_adapter` still defines `discovery: 60` via `bot_registry()` intervals.
- Prior exports: `DiscoveryCooldown`, `RoleYieldStats`, `DiscoveryAllocation`, `DiscoveryManager`, `CHEAP_DISCOVERY_ROLES` → now `discovery_daemon` cheap pin.

## Invariants
- stdlib-only (dataclasses, time, json, pathlib)
- Discovery agents produce structured candidates; they NEVER modify code (§8)
- Current cooldowns are tracked in the daemon via `BotState.next_run_at` + `_last_run` + heartbeat mtime (60s per role), not per `(role, scope)` commit-SHA pairs
- Prior yield/diversity/saturation logic archived in git history; roadmaps now reference daemon metrics
