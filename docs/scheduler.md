# Scheduler

Simple, deterministic, bucket-triggered scheduling for CodeBot.

## Architecture

```
TicketStore
    │
    ▼
Scheduler.tick()
    │
    ├── _reconcile()  ── stale → ZOMBIE → DEAD, orphan claims, leaked slots
    │
    └── BucketDispatcher.tick()
            │
            ├── _snapshot_buckets()  ── group tickets by DISCOVERY/TRIAGE/READY/DECOMPOSE/PLANNING/IMPLEMENTATION/REVIEW
            ├── discovery backpressure  ── if downstream >= watermark, suppress DISCOVERY
            ├── weighted round-robin  ── pick next bucket with actionable work
            ├── _select_ticket()  ── priority → age → id
            ├── ModelSelector.next_model()  ── persisted round-robin
            └── DispatchGate.try_dispatch()
                    │
                    ├── ConcurrencyController.reserve()  ── atomic, MAX_CONCURRENCY is sole limit
                    ├── claim_ticket()  ── flock-guarded, one owner per ticket
                    └── SpawnQueue.enqueue()  ── 5s stagger, heartbeat-aware
```

## Source files

| File | Types |
|------|-------|
| `codebot/scheduler_v2/lifecycle.py` | `AgentState`, `Clock`, `FakeClock`, `ProcessSpawner`, `AgentRecord` |
| `codebot/scheduler_v2/dispatch_gate.py` | `ConcurrencyController`, `ClaimRecord`, `SpawnQueue`, `DispatchGate` |
| `codebot/scheduler_v2/dispatcher.py` | `ModelSelector`, `BucketDispatcher`, `Scheduler`, `ReasonCode` |

Any diagnostic path touches at most 3 files.

## Agent lifecycle

```
CREATED → STARTING → RUNNING → ZOMBIE → DEAD
  │          │                      ▲
  └──────────┴──────────────────────┘
         (failure paths go ZOMBIE)
```

DEAD is terminal. Retries create a new `agent_id`.

## Concurrency

`MAX_CONCURRENCY` (default 200, env `CODEBOT_MAX_CONCURRENT`) is the sole capacity limit. No token budgets. `DispatchGate` reserves one slot per agent; `SpawnQueue` entries also count. `free_slots == 0` means `why_not_running() == NO_GLOBAL_CAPACITY`.

## Claims

`claim_ticket(state_dir, ticket_id, agent_id)` uses `flock` on `{ticket_id}.claim.lock` and writes `{ticket_id}.claim.json` atomically via `tmp+replace`. One ticket → one owner. `release_claim` verifies `agent_id` before deleting.

## Stagger

Global 0.5-second minimum between spawns. `SpawnQueue` rules:
- Queue non-empty → next at `last_scheduled + 0.5s`.
- Queue empty → next at `max(now, latest_heartbeat + 0.5s)` reading disk `*.heartbeat` files.
- Once scheduled, timestamp is fixed; later heartbeats do not postpone.

## Buckets

`BUCKET_ORDER` maps ticket states to buckets. `BucketDispatcher.tick()` snapshots all buckets, applies starvation priority (actionable > 0 and active == 0), enforces discovery watermark and per-bucket role caps, then round-robins with optional static weights.

## Model selection

`ModelSelector` persists `next_model_index` to `model_selector.json`. `mark_unavailable` / `mark_available` skip models without resetting the index. Index survives restarts.

## Diagnostics

`Scheduler.why_not_running(ticket_id, store)` returns `ReasonCode`: `NO_GLOBAL_CAPACITY`, `ALREADY_CLAIMED`, `BLOCKED`, `NO_ACTIONABLE_WORK`, `WAITING_FOR_STAGGER`, or `UNKNOWN`. Structured `SPAWN` / `EXIT` one-liners are emitted per lifecycle transition.

## Invariants

- `active_slots <= MAX_CONCURRENCY` always.
- One ticket → at most one active claim.
- One agent → at most one ticket.
- `DEAD` owns nothing and consumes no slot.
- Spawn spacing >= 0.5s.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `MAX_CONCURRENCY` | 200 | Global agent limit |
| `stagger_seconds` | 0.5 | Spawn spacing |
| `discovery_watermark` | 1000 | Suppress discovery when downstream >= watermark |
| `bucket_weights` | 1 per bucket | Static weighted round-robin |
| `role_caps` | none | Per-bucket concurrent cap |

## Migration

When `CODEBOT_V2_SCHEDULER=1`, `orchestrator.py` routes `check_all_bots` through `Scheduler.tick()`. Old `apply_agent_availability` is bypassed for V2-managed roles to avoid enable/disable fighting. `_count_api_runner_processes` should delegate to `DispatchGate.count_active()` once cut over.
