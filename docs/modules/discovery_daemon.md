# discovery_daemon.py

5-slot discovery outside the scheduler. Round-robin 9 roles, 60s per role, 5 concurrent, dirty-file injection.

## Key Exports
- `DISCOVERY_ROLES`: tuple[9 str] — `bug_hunter`, `security_auditor`, `architecture_auditor`, `performance_auditor`, `test_gap_auditor`, `documentation_auditor`, `dependency_auditor`, `ux_auditor`, `feature_hunter`
- `DISCOVERY_INTERVAL_SECONDS`: 60; `DISCOVERY_MAX_CONCURRENT`: 5; `DISCOVERY_STAGGER_SECONDS`: 2.0; `DISCOVERY_TICK_SECONDS`: 10; `DISCOVERY_HEARTBEAT_TIMEOUT`: 300
- `DiscoveryDaemon`: class — `tick()` (up to 5 spawns, cooldown-gated), `loop()` (10s tick checking `_shutdown_requested`), `start()`/`stop()`, `_ensure_bot()`, `_launch()`, `_is_on_cooldown()`, `_running_count()`, `_changed_files_block()`
- `_changed_files_block(state_dir, limit=30)`: cached (60s) `git diff --name-only HEAD` then `git diff --name-only`, fallback `git status --porcelain`, capped 30 lines

## Invariants
- Own 5-slot pool, not scheduler's 90 via `DispatchGate`. Uses `BotState.next_run_at` + in-memory `_last_run` + heartbeat-file mtime for 60s per-role cooldown.
- Read-only, `create_ticket` only, never `claim_ticket` / hold `DispatchGate` token.
- Respects `state_manager.is_draining()` and `orchestrator_runtime._shutdown_requested` each tick; 2s stagger between discovery spawns; no infinite daemon unbounded pool.
- `codebot_adapter.bot_registry()` discovery interval 60s survives restarts; cheap roles pinned to `xiaomi-mimo-2.5` at creation and per-launch in `_launch()`.
- `orchestrator_runtime.run_main_loop` starts the thread on `pid_path.parent` and joins on shutdown; `prune_stale_dynamic_bot_state` cleans after.
- `dispatch_service.apply_agent_availability` / `health_check_loop.start_eligible_bots` skip `DISCOVERY_ROLE_NAMES` so only daemon drives discovery.
