# Agent Lifecycle

## States

| State | Meaning |
|-------|---------|
| CREATED | Record exists, process not yet spawned |
| STARTING | Spawn begun, not yet confirmed |
| RUNNING | Process alive, owns one ticket, heartbeat valid |
| ZOMBIE | Execution ended or unhealthy, awaiting cleanup |
| DEAD | Finalized, owns nothing, consumes no slot |

## Transitions

```
CREATED → STARTING → RUNNING
  │          │          │
  └──────────┼──────────┼→ ZOMBIE → DEAD
```

- `CREATED → ZOMBIE` on spawn failure before process confirmation.
- `STARTING → ZOMBIE` on early crash or stale timeout.
- `RUNNING → ZOMBIE` on process exit, stale heartbeat (model-aware timeout), or explicit termination.
- `ZOMBIE → DEAD` after `_reconcile` releases claim + concurrency slot and records `exit_reason`.
- `DEAD` has no outgoing transitions. `InvalidTransitionError` on any attempt.

## Record

`AgentRecord` fields: `agent_id` (UUID), `ticket_id`, `role`, `model`, `pid`, `state`, `created_at`, `scheduled_at`, `started_at`, `last_heartbeat`, `exit_at`, `exit_reason`, `trace` (append-only list of `(AgentState, timestamp)`).

`transition(new_state, timestamp?)` returns a new record or raises `InvalidTransitionError`. `with_heartbeat(timestamp?)` updates `last_heartbeat`. `with_exit(reason, timestamp?)` atomically does `ZOMBIE → DEAD` (or just `DEAD` if already `ZOMBIE`).

## Clock abstraction

`Clock` protocol with `RealClock` (system `time.time()`) and `FakeClock(initial, advance(delta), set(value))`. All time-dependent logic takes an injected `Clock`; no `time.sleep(5)` in tests.

## Process abstraction

`ProcessSpawner` protocol with `RealProcessSpawner` (`subprocess.Popen`, env `PYTHONPATH`) and `FakeProcessSpawner(mode=SUCCESS|CRASH|TIMEOUT)`.

## Per-model stale timeouts

`_MODEL_PROFILES` in `api_runner.py` defines `heartbeat_multiplier` per model (1.5–2.8). `get_stale_timeout(model)` returns `120 * multiplier`. Thinking models get up to 336s before being marked stale. `is_agent_stale(record, clock)` checks `STARTING`/`RUNNING` agents only; missing heartbeat is not stale.

## Retries

A failed agent goes `ZOMBIE → DEAD` and releases its claim + slot. Retry allocates a new `agent_id` from the bucket dispatcher on the next `tick()`. The old ticket is eligible again once its claim is released. The new agent reads the same ticket-scoped scratchpad (`state/{ticket_id}.scratchpad.json`) for context — no state is carried in the agent record itself.

## Cleanup (reconcile)

`Scheduler._reconcile()` runs at the start of each `tick()`:
1. `RUNNING` + stale heartbeat → `ZOMBIE`.
2. `ZOMBIE` → `DEAD` via `with_exit("zombie-cleanup")` + `gate.complete_dispatch()`.

Idempotent — running twice is safe. Double `finalize_agent(DEAD)` returns `False`.

## Lock ordering

1. `ConcurrencyController` (in-memory `threading.Lock`)
2. Claim file lock (`flock` on `claims/{ticket_id}.claim.lock`)
3. `SpawnQueue` (in-memory `threading.Lock`)

Never nest TicketStore save lock inside a claim lock.
