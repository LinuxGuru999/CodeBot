# discovery_manager.py

Manages discovery role allocation, cooldown tracking, yield statistics, diversity enforcement, and project saturation detection. When the executable backlog is insufficient to occupy all slots, this module determines which discovery roles to spawn and how many of each.

## Key Exports
- `DiscoveryCooldown`: Class
- `RoleYieldStats`: Class
- `DiscoveryAllocation`: Class
- `DiscoveryManager`: Class
- `is_expired()`: Function
- `is_stale_commit()`: Function
- `yield_rate()`: Function
- `duplicate_rate()`: Function
- `cost_per_ticket()`: Function

## Invariants
- stdlib-only (dataclasses, time, json, pathlib)
- Discovery agents produce structured candidates; they NEVER modify code (§8)
- Cooldowns are tracked per (role, scope) pair keyed by commit SHA
- Yield stats decay over time so stale data doesn't permanently suppress a role
- Diversity allocation uses weighted round-robin, not random
