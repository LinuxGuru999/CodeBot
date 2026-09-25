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
            ├── _snapshot_buckets()  ── group tickets by bucket (GOAL, DECOMP, PLANNING, REWORK, IMPLEMENT, REVIEW, VERIFY)
            │   └── DISCOVERED is intentionally absent — DISCOVERED → TRIAGED is platform code (triage), not a bucket
            ├── weighted round-robin  ── pick next bucket with actionable work
            ├── _select_ticket()  ── priority → age → id (GOAL tier takes precedence)
            ├── ModelSelector.next_model()  ── persisted round-robin
            └── DispatchGate.try_dispatch()
                    │
                    ├── ConcurrencyController.reserve()  ── atomic, MAX_CONCURRENCY is sole limit, per-role stagger dict
                    ├── claim_ticket()  ── flock-guarded, one owner per ticket
                    └── SpawnQueue.enqueue()  ── 5.0s per-role stagger, drain(role) enforces per-role spacing
                    └── health_check_loop drains ready queue → pre-spawn review packet gate → start_bot() → post-spawn survival check (0.5s) via process_manager
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
| `codebot/scheduler_v2/dispatcher.py` | `ModelSelector`, `Scheduler`, `ReasonCode`, `InvariantViolation`, reconciliation sweeps |
| `codebot/scheduler_v2/bucket_dispatcher.py` | `BucketDispatcher`, `BUCKET_ORDER`, `BUCKET_TO_ROLE_SETS` |
| `codebot/discovery_daemon.py` | `DiscoveryDaemon`, `DISCOVERY_ROLES`, `DISCOVERY_MAX_CONCURRENT=5`, `_changed_files_block()` |
| `codebot/orchestrator_runtime.py` | `run_main_loop()` spawns daemon thread alongside health checks, joins on shutdown |
| `codebot/process_manager.py` | `_prepare_prompt_with_context(extra_block=)`, `_init_and_prepare_bot(extra_block=)` injects CHANGED_FILES; REVIEWER_ROLE_NAMES imported for inline review packet injection |
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

Per-role 5.0s minimum between scheduler spawns (configurable via `SPAWN_STAGGER_SECONDS`). `SpawnQueue.drain(role)` enforces per-role spacing using `_last_process_start: dict[str, float]`. `record_spawn(ts, role)` updates the per-role timestamp after each dispatch.

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
| REVIEW | REVIEW | reviewer + 10 specialists | adversarial swarm, creates review_packets |
| VERIFY | VERIFY | verifier | read-only static analysis (no bash), auto-approve sweep >10min |
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
- Spawn spacing >= 5.0s per-role (scheduler), >= 2s (discovery).
- Review packets exist before reviewer spawn (pre-spawn gate + retry sweep).
- Empty-work approvals rejected (< 2 iterations, 0 files touched).
- Rate-limited agents transition to REWORK after 5 consecutive exit-code-3 events.
- VERIFY tickets auto-approved after 10 minutes with no live verifier.

## Configuration

| Key | Default | Meaning |
|-----|---------|---------|
| `MAX_CONCURRENT_AGENTS` | 90 | Scheduler global agent limit |
| `SPAWN_STAGGER_SECONDS` | 5.0 | Per-role scheduler spawn spacing |
| `BUCKET_WEIGHTS` | USER:1, GOAL:1, DECOMP:1, PLANNING:1, REWORK:2, IMPLEMENT:3, REVIEW:2, VERIFY:1 | Static weighted round-robin |
| `ROLE_CAPS` | triage:10, goal:10, verify:5, discovery:3 | Per-role concurrent cap |
| `DISCOVERY_MAX` | 3 | Discovery daemon slots (outside scheduler) |
| `SCHEDULER_INTERVAL_SECONDS` | 30 | Orchestrator tick period |

Discovery interval is also in `codebot_adapter` bot_registry (discovery=60s). Workers run outside both.

## Reconciliation Sweeps

`Scheduler._reconcile()` runs five sweeps each tick:
1. Stale agent detection → ZOMBIE → DEAD transitions
2. `_sweep_leaked_slots()` — releases gate entries for DEAD agents
3. `_sweep_orphan_claims()` — purges claim files with no live PID, cross-references `pgrep`
4. `_sweep_stale_verify_tickets()` — auto-approves VERIFY tickets >10min old with no live verifier
5. `_ensure_review_packet()` — retries review packet creation for all active reviewer claims every cycle

## Migration

`scheduler_v2` is the sole scheduler (ADR-007 Phase 2 complete). Legacy modules (`adaptive_scheduler.py`, `queue_pressure.py`, `work_scorer.py`, `discovery_manager.py`) deleted. `scheduler_config.py` flattened from 453 LOC to 36 LOC. `dispatcher.py` decomposed into `dispatcher.py` + `bucket_dispatcher.py`. Single top-level `DispatchMode` switch removed; always AUTHORITATIVE.
