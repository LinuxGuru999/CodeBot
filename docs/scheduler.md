# Scheduler

Deterministic, bucket-triggered scheduling for CodeBot via `scheduler_v2`.

## Architecture

```
TicketStore
    │
    ▼
Scheduler.tick(store) — orchestrator_runtime run_main_loop every 30s
    │
    ├── _reconcile()  ── stale → ZOMBIE → DEAD, orphan claims, leaked slots
    │
    └── BucketDispatcher.tick(store)
            │
            ├── _snapshot_buckets()  ── group tickets by bucket (GOAL, DECOMP, PLANNING, REWORK, IMPLEMENT, REVIEW)
            │   └── DISCOVERED is intentionally absent — DISCOVERED → TRIAGED is platform code (triage), not a bucket
            ├── weighted round-robin  ── pick next bucket with actionable work
            ├── _select_ticket()  ── priority → age → id
            ├── ModelSelector.next_model()  ── persisted round-robin
            └── DispatchGate.try_dispatch()
                    │
                    ├── ConcurrencyController.reserve()  ── atomic, MAX_CONCURRENCY is sole limit
                    ├── claim_ticket()  ── flock-guarded, one owner per ticket
                    └── SpawnQueue.enqueue()  ── 0.5s stagger, heartbeat-aware
                    └── health_check_loop drains ready queue → start_bot() via process_manager
```

Discovery is **outside** the scheduler (see Discovery Daemon below).

```
orchestrator_runtime.run_main_loop
├── scheduler_v2 (90 slots: GOAL, DECOMP, PLANNING, REWORK, IMPLEMENT, REVIEW)
└── discovery_daemon thread (5 slots: 9 discovery roles, round-robin, 60s cooldown)
        ├── own pool: 5 concurrent, stagger 2s, tick 10s, next_run_at + _last_run per-role cooldown
        ├── dirty-first: CHANGED_FILES block from git diff --name-only, fallback git status --porcelain
        ├── cheap tier pinned: test_gap_auditor/documentation_auditor/dependency_auditor → xiaomi-mimo-2.5
        └── bypasses DispatchGate, never claims, read-only create_ticket
```

## Source files

| File | Types |
|------|-------|
| `codebot/scheduler_v2/lifecycle.py` | `AgentState`, `Clock`, `FakeClock`, `ProcessSpawner`, `AgentRecord` |
| `codebot/scheduler_v2/dispatch_gate.py` | `ConcurrencyController`, `ClaimRecord`, `SpawnQueue`, `DispatchGate` |
| `codebot/scheduler_v2/dispatcher.py` | `ModelSelector`, `BucketDispatcher`, `Scheduler`, `ReasonCode` |
| `codebot/discovery_daemon.py` | `DiscoveryDaemon`, `DISCOVERY_ROLES`, `DISCOVERY_MAX_CONCURRENT=5`, `_changed_files_block()` |
| `codebot/orchestrator_runtime.py` | `run_main_loop()` spawns daemon thread alongside health checks, joins on shutdown |
| `codebot/process_manager.py` | `_prepare_prompt_with_context(extra_block=)`, `_init_and_prepare_bot(extra_block=)` injects CHANGED_FILES |
| `codebot/codebot_adapter.py` | `bot_registry()` intervals: discovery 60s, planning 3600s, implementation 300s, review 600s, control 900s |

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

`scheduler_config.MAX_CONCURRENT_AGENTS` (default 90, env `CODEBOT_MAX_CONCURRENT`) is the sole scheduler limit. The discovery daemon adds 5 slots outside it. `DispatchGate` reserves one slot per scheduler agent; `SpawnQueue` entries also count. `free_slots == 0` means `why_not_running() == NO_GLOBAL_CAPACITY`. Discovery never consumes scheduler slots.

## Claims

`claim_ticket(state_dir, ticket_id, agent_id)` uses `flock` on `{ticket_id}.claim.lock` and writes `{ticket_id}.claim.json` atomically via `tmp+replace`. One ticket → one owner. `release_claim` verifies `agent_id` before deleting. Discovery agents never claim — they only `create_ticket`.

## Stagger

Global 0.5s minimum between scheduler spawns. `SpawnQueue` rules:
- Queue non-empty → next at `last_scheduled + 0.5s`.
- Queue empty → next at `max(now, latest_heartbeat + 0.5s)` reading disk `*.heartbeat` files.
- Once scheduled, timestamp is fixed; later heartbeats do not postpone.

Discovery daemon uses its own 2s stagger between discovery spawns.

## Buckets

`BUCKET_ORDER` maps ticket states to buckets. `BucketDispatcher.tick()` snapshots all buckets, applies starvation priority (actionable > 0 and active == 0), enforces per-bucket role caps, then round-robins with optional static weights.

| Bucket | States | Roles | Notes |
|--------|--------|-------|-------|
| GOAL | TRIAGED | goal_aligner | adapter queue depth, NOW/LATER/NEVER |
| DECOMP | DECOMP | decomposer | DAG via .decomp.json |
| PLANNING | PLANNING | planner | .plan.json → IMPLEMENT |
| REWORK | REWORK | implementer | failed review, prior feedback |
| IMPLEMENT | IMPLEMENT | implementer | primary build work |
| REVIEW | REVIEW | reviewer + specialists | adversarial swarm |
| DISCOVERED | — | — | not a bucket; triage is platform code. Discovery runs outside via discovery_daemon |

No discovery watermark inside scheduler; discovery is on its own pool.

## Model selection

`ModelSelector` persists `next_model_index` to `model_selector.json`. `mark_unavailable` / `mark_available` skip models without resetting the index. Index survives restarts. Cheap-tier discovery roles are pinned in daemon (xiaomi-mimo-2.5) and do not rotate.

## Diagnostics

`Scheduler.why_not_running(ticket_id, store)` returns `ReasonCode`: `NO_GLOBAL_CAPACITY`, `ALREADY_CLAIMED`, `BLOCKED`, `NO_ACTIONABLE_WORK`, `WAITING_FOR_STAGGER`, or `UNKNOWN`. Structured `SPAWN` / `EXIT` one-liners are emitted per lifecycle transition.

## Invariants

- `active_slots <= MAX_CONCURRENCY` always (scheduler). Discovery is separate.
- One ticket → at most one active scheduler claim. Discovery is claim-free.
- One scheduler agent → at most one ticket.
- `DEAD` owns nothing and consumes no slot.
- Spawn spacing >= 0.5s (scheduler), >= 2s (discovery).

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `MAX_CONCURRENT_AGENTS` | 90 | Scheduler global agent limit |
| `stagger_seconds` | 0.5 | Scheduler spawn spacing |
| `bucket_weights` | 1 per bucket | Static weighted round-robin |
| `role_caps` | none | Per-bucket concurrent cap |
| `DISCOVERY_MAX_CONCURRENT` | 5 | Discovery daemon slots (outside scheduler) |
| `DISCOVERY_INTERVAL_SECONDS` | 60 | Per-role cooldown |
| `DISCOVERY_TICK_SECONDS` | 10 | Daemon tick period |
| `DISCOVERY_STAGGER_SECONDS` | 2.0 | Discovery spawn spacing |

Discovery interval is also in `codebot_adapter` bot_registry (discovery=60s). Workers run outside both.

## Migration

`scheduler_v2` is the scheduler. `apply_agent_availability` no longer disables discovery roles — `DISCOVERY_ROLE_NAMES` are skipped and health_check_loop `start_eligible_bots` also skips them. `_count_api_runner_processes` delegates to `DispatchGate.count_active()` in V2.
