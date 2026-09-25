# Agent Lifecycle

## States (scheduler_v2 vs BotState)

Two lifecycles coexist. Most agents use `scheduler_v2/lifecycle.py` `AgentState`; orchestrator also tracks `process_manager.BotState`.

### scheduler_v2 AgentState (90 scheduler slots)

| State | Meaning |
|-------|---------|
| CREATED | Record exists, gate reserved, not yet spawned |
| STARTING | DispatchGate reserved, spawn queued, heartbeat not yet valid |
| RUNNING | Process alive, owns one ticket, heartbeat valid |
| ZOMBIE | Execution ended or stale, awaiting reconcile |
| DEAD | Finalized, owns nothing, consumes no slot |

Transitions:

```
CREATED → STARTING → RUNNING
  │          │          │
  └──────────┼──────────┼→ ZOMBIE → DEAD
```

- `CREATED → ZOMBIE` on spawn failure before process confirmation.
- `STARTING → ZOMBIE` on early crash or stale timeout.
- `RUNNING → ZOMBIE` on process exit, stale heartbeat (model-aware timeout), or explicit termination.
- `ZOMBIE → DEAD` after `Scheduler._reconcile` releases claim + slot and records `exit_reason`.
- `DEAD` is terminal; `InvalidTransitionError` on any attempt.

`AgentRecord` fields: `agent_id` (UUID), `ticket_id`, `role`, `model`, `pid`, `state`, `created_at`, `scheduled_at`, `started_at`, `last_heartbeat`, `exit_at`, `exit_reason`, `trace` (append-only `(AgentState, timestamp)`).

### Orchestrator BotState (daemon + legacy workers)

`BotState` wraps `BotConfig` + `process` + `last_heartbeat`, `next_run_at`, `restart_count`, `consecutive_errors`. `BotConfig` holds `name`, `prompt_file`, `interval_seconds`, `heartbeat_timeout`, `model`, `fallback_model`, `tier`, `max_restarts`, `clean_exit_wait`, `runner_mode`. `is_stuck()` + `effective_heartbeat_timeout()` come from `health_monitor`.

## Discovery daemon lifecycle (outside scheduler, 5 slots)

The discovery daemon does not use `AgentState`. It uses `BotState.next_run_at` for cooldown:

- `DiscoveryDaemon.tick()` (every 10s) picks up to 5 roles round-robin by `_next_index`.
- Skips any role whose `process` is alive or on cooldown (`next_run_at` future, or `_last_run` within 60s, or heartbeat-file mtime within 60s).
- For each candidate: `_ensure_bot()` creates `BotConfig` lazily (cheap tier pinned to xiaomi-mimo-2.5 for test_gap/documentation/dependency), `_build_checkpoint_block`, then `process_manager._launch_bot_subprocess` (no ticket context, with `CHANGED_FILES` `extra_block`).
- On exit: `health_check_loop.handle_exited_bots` moves the bot to `next_run_at = now + interval_seconds` so the next tick sees cooldown. `prune_stale_dynamic_bot_state` cleans retired dynamic workers. The daemon never holds a `DispatchGate` claim.

```
tick() → _changed_files_block() → _ensure_bot(role) → _init_and_prepare_bot(extra_block) → _launch_bot_subprocess → running
                                                          ↓
                                             on exit: next_run_at = now+60s → cooldown → round-robin rotates
```

## process_manager prompt injection

`process_manager._prepare_prompt_with_context(bot, extra_block="")` appends `extra_block` after ticket context / scratchpad / implementation packet stepping. `_init_and_prepare_bot(extra_block=)` threads it through. Currently only the discovery daemon injects `CHANGED_FILES` (dirty-first hint). `prompt_gateway.build_message()` is non-mutating and keeps `extra_block` order.

## Clock and process abstractions

### Clock

`Clock` protocol with `RealClock` (system `time.time()`) and `FakeClock(initial, advance(delta), set(value))`. All time-dependent logic takes an injected `Clock`; no `time.sleep(5)` in tests.

### Process

`ProcessSpawner` protocol with `RealProcessSpawner` (`subprocess.Popen`, env `PYTHONPATH`) and `FakeProcessSpawner(mode=SUCCESS|CRASH|TIMEOUT)`.

### Per-model stale timeouts

`model_manager.MODEL_PROFILES` / `_MODEL_PROFILES` in `api_runner.py` define `heartbeat_multiplier` per model (1.5–2.8). `get_stale_timeout(model)` returns `120 * multiplier`. Thinking models get up to 336s before being marked stale. `is_agent_stale(record, clock)` checks `STARTING`/`RUNNING` agents only; missing heartbeat is not stale. Discovery's effective are derived from `BotConfig`: `heartbeat_timeout = interval*2` (60s → 120s) so thinking auditors still survive long reads.

## Retries (scheduler_v2)

A failed scheduler agent goes `ZOMBIE → DEAD` and releases its claim + slot. Retry allocates a new `agent_id` from the bucket dispatcher on the next `tick()`. The old ticket is eligible again once its claim is released. The new agent reads the same ticket-scoped scratchpad (`state/{ticket_id}.scratchpad.json`) for context — no state is carried in the agent record.

### Rate-Limit Handling (exit code 3)

Implementers exiting with code 3 (429 rate limit) are requeued with exponential backoff instead of transitioning to REWORK immediately. However, after 5 consecutive rate-limit exits for the same ticket, the agent transitions to REWORK to break infinite requeue loops. Non-implementer roles use standard backoff + model rotation.

### Empty-Work Guard

Implementers that exit cleanly (code 0) but produced no work (< 2 tool iterations, 0 files touched) have their approval skipped. This prevents agents that make a single API call and return empty output from advancing tickets through the pipeline.

### Drain Loop Gates

The drain loop in `health_check_loop.run_dispatchers()` applies two gates around `start_bot_fn()`:
1. **Pre-spawn review packet gate**: For reviewer roles, verifies `review_packets/{ticket_id}.json` exists before spawning. If missing, creates it from the ticket store. If the ticket can't be found, cancels the dispatch.
2. **Post-spawn survival check**: After `start_bot_fn()` succeeds, waits 0.5s then checks `process.poll()`. If the agent died immediately, cancels the dispatch and clears the assignment instead of leaving an orphaned claim.

## Cleanup (reconcile)

`Scheduler._reconcile()` runs at the start of each `tick()` and executes five sweeps:
1. `RUNNING` + stale heartbeat → `ZOMBIE`.
2. `ZOMBIE` → `DEAD` via `with_exit("zombie-cleanup")` + `gate.complete_dispatch()`.
3. `_sweep_leaked_slots()` — releases gate entries for agents that are DEAD but still in `_gate._active`.
4. `_sweep_orphan_claims()` — scans `claims/*.claim.json`, cross-references live PIDs via `pgrep -f codebot.api_runner`, deletes claims for dead agents. For claims still in `gate_claimed_tickets`, verifies process liveness per-role+ticket before deleting. Fires every cycle for all active reviewer claims (not just newly registered ones) to recover from transient failures.
5. `_sweep_stale_verify_tickets()` — finds VERIFY tickets older than 10 minutes with no live verifier process, writes auto-approve verdict to `verification/{ticket_id}.json`, transitions to COMPLETE.

Additionally, `_register_dispatched_agents()` calls `_ensure_review_packet(ticket_id)` every cycle for all active reviewer claims, retrying packet creation if it failed on prior ticks.

Idempotent — running twice is safe. Double `finalize_agent(DEAD)` returns `False`.

Discovery has no reconcile; `orchestrator_services.prune_stale_dynamic_bot_state` plus the daemon's own `_is_on_cooldown` heartbeat check suffice.

## Lock ordering

1. `ConcurrencyController` (in-memory `threading.Lock`)
2. Claim file lock (`flock` on `claims/{ticket_id}.claim.lock`)
3. `SpawnQueue` (in-memory `threading.Lock`)

Never nest TicketStore save lock inside a claim lock. A daemon `flock` on `.orchestrator.pid` is separate and held briefly at startup.

## Roles vs lifecycle

| Subsystem | Lives via | Backed by |
|-----------|-----------|-----------|
| Scheduler (GOAL, DECOMP, PLANNING, REWORK, IMPLEMENT, REVIEW, VERIFY) | `AgentState` per `ticket_id` | `DispatchGate` + `ClaimRecord` |
| Discovery (9 auditors + feature_hunter) | `BotState.next_run_at` cooldown | `Beat + _last_run` (no claims) |
| Control (ticket_triager, git_sync, etc.) | `BotState` | heartbeat file |

