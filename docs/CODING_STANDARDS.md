# CodeBot Coding Standards — Authoritative Dispatch

> **Authority:** This document is normative. All code that touches scheduling, claims, slots, queues, tickets, or lifecycle MUST conform. Violations are invariant violations, not style nits.
>
> **Source of truth for dispatch:** §2–§8 define the 7 invariants. Grep-enforceable where noted.
> **Constitution anchor:** Constitution §4 — single-owner authority, bounded contexts, atomic ownership, one-way reconstructable mapping.
> **ADR:** `docs/adr/007-authoritative-dispatch.md` (Status: Accepted).

Last updated: 2026-09-24 — Phase 0.

---

## 1. Authority Model (overview)

> CodeBot is a context compiler. Context is the source; code is the artifact. The lifecycle transforms natural-language intent through parse, semantic analysis, decomposition, planning, code generation, and verification into durable project knowledge. Every invariant below serves this pipeline. See `docs/adr/008-context-is-source.md` (Status: Accepted).

```
                     Orchestrator Process — owns exactly ONE Scheduler
                                   │
                                   │ Scheduler.run_once()  ← ONE dispatch owner, reentrancy-guarded
                                   ▼
                      ┌──────────────────────────┐
                      │ Scheduler.run_once()      │
                      │ 1. ingest exits/results   │
                      │ 2. _reconcile()           │  ← ONE reconciler, never schedules
                      │ 3. ingest findings→DISCOVERED
                      │ 4. BucketDispatcher.tick()│  ← ONE mapper; maps WorkItem→request
                      │ 5. SpawnQueue.drain()     │
                      └────────────┬─────────────┘
                                   │ DispatchGate.try_dispatch() [reserve→claim→queue]
                                   ▼
                      ┌──────────────────────────┐
                      │ DispatchGate              │  ONE gate bundling:
                      │  ConcurrencyController    │  ONE slot ledger (reserve/release)
                      │  claim_ticket             │  ONE claim mechanism (flock+tmp+replace)
                      │  SpawnQueue (5 s, FIFO)   │  ONE spawn queue wiring
                      └────────────┬─────────────┘
                                   │ SpawnRequest (QUEUED)
                                   ▼
                      ┌──────────────────────────┐
                      │ RealProcessSpawner        │  ONE spawn site: _launch_bot_subprocess
                      └──────────────────────────┘

     TicketStore ← get_ticket_store(state_dir)  ← ONE ticket authority, one backing store
     Workers     ← Agents emit findings/verdicts/results; platform owns every store.transition()
     WorkItem    ← TicketWork(ticket_id) | DiscoveryWork(discovery_run_id) — same claim/slot mechanism
     Ownership   ← WorkItem → claim → agent → slot — one-way, always reconstructable
```

**Top-level mode switch (one flag, highest level only):**

```python
DispatchMode = Literal["LEGACY", "AUTHORITATIVE"]  # default AUTHORITATIVE after cutover
if dispatch_mode == "LEGACY":
    LegacyDispatchController.run_once(bots, store)
else:
    scheduler.run_once(store)
```

No `if USE_LEGACY_DISPATCH:` scattered through `dispatch_gate.py` / `ticket_dispatcher.py` / `process_manager.py`. Legacy wrappers during migration either `return scheduler.request_wake()` or are disabled — they never call `tick()` themselves.

---

## 2. Invariant 1 — ONE DISPATCH OWNER

> Only `Scheduler` calls `BucketDispatcher.tick()`. Only `BucketDispatcher` maps an actionable WorkItem → worker request.

- Exactly **one thread / one process** ever calls `BucketDispatcher.tick()` in production.
- That call site is `Scheduler.run_once()` (see §4.1 of the plan). Legacy wrappers during migration do **not** call `tick()` — they at most `request_wake()`.
- No other thread (notably the former `orchestrator_runtime._dispatch_loop`) may call `tick()`, `try_dispatch()`, or `reserve()` outside `run_once()`.

**Lock ordering (documented, enforced):** `ConcurrencyController._lock` → `claims/{work_item_id}.claim.lock` (flock, held briefly) → `SpawnQueue._lock`. Never nest `TicketStore` save-lock inside claim lock.

**Heartbeat role:** Heartbeats establish **liveness** (`is_agent_stale` for reconciler), never scheduling pressure. Spawn queue's `scheduled_at` is never slid by heartbeat.

**Reentrancy guard (defense-in-depth):** `Scheduler.run_once()` holds a non-blocking lock:

```python
def run_once(self, store):
    if not self._run_lock.acquire(blocking=False):
        self.request_wake()  # coalesce: one extra wake after current run
        return ALREADY_RUNNING
    try:
        # ingest exits → transitions → reconcile → ingest findings → dispatch → drain
    finally:
        self._run_lock.release()
```

**Measurable / grep:** `grep -R "BucketDispatcher.*tick\|DispatchGate.*try_dispatch\|ConcurrencyController.*reserve" codebot --include="*.py" | grep -v tests` hits exactly one production caller (`scheduler_v2/dispatcher.py:Scheduler.run_once`). No background thread calls `tick()`.

**Enforcement:** Arch test #1 (100 concurrent `run_once()` callers → exactly one proceeds, rest coalesce).

---

## 3. Invariant 2 — ONE TICKET AUTHORITY

> All ticket reads/writes go through one canonical `TicketStore` API with one locking/versioning protocol and one canonical backing store.

- Factory is `get_ticket_store(state_dir: Path) -> TicketStore`, keyed **exclusively on canonical storage identity** (resolved `state_dir` / `tickets.json` path), not on constructor flags.
- View behavior belongs on **method arguments**, not cache keys:

```python
# forbidden — two authorities for same file
get_ticket_store(path, workers_flag=True)   # A
get_ticket_store(path, workers_flag=False)  # B — violates invariant

# required
store = get_ticket_store(state_dir)          # one identity
store.list_tickets(include_workers=False)    # view, not identity
store.list_by_state(state, include_workers=False)
```

- “One Python object” ≠ “one authority”. Multiple processes may each have one in-memory object, but they converge through one backing `tickets.json`+WAL and one `flock`/revision protocol. Direct `TicketStore(...)` construction outside the factory is banned by arch test (single documented exemption `ticket_status.py` CLI helper — folded behind `get_ticket_store` in Phase 1).
- In-process split `store=None` → new instance vs shared `current_tick_store()` is forbidden after fix — every caller receives the injected `store` handle for its tick.

**Measurable / grep:** `grep -R "TicketStore(" codebot --include="*.py"` hits exactly one production line (the factory). Every `store.transition / get / list_by_state` caller receives `store` injected for its tick.

---

## 4. Invariant 3 — ONE CLAIM PER WORK ITEM

> Exactly one canonical claim record per WorkItem, atomic claim/release with ownership generation.

```python
WorkItem = TicketWork(ticket_id: str) | DiscoveryWork(discovery_run_id: str)
# claim file: claims/{work_item_id}.claim.json  where work_item_id is
#   namespaced: ticket--{ticket_id} for TicketWork, discovery--{discovery_run_id} for DiscoveryWork
```

- Precisely one file `claims/{work_item_id}.claim.json` per WorkItem, with shape:

```json
{
  "work_item_kind": "ticket",
  "work_item_id": "ticket--CB-123",
  "agent_id": "…",
  "role": "implementer",
  "execution_id": "uuid4",
  "generation": 7,
  "ticket_revision": 18,
  "implementation_attempt_id": "ticket--CB-123:impl:a1b2c3d4",
  "claimed_at": 1716120000.0,
  "expires_at": null
}
```
(`implementation_attempt_id` present only when `work_item_kind==ticket` and role in IMPLEMENT bucket; REVIEW references it. `work_item_id` for discovery is `discovery--{uuid}` with no `ticket_revision`/`implementation_attempt_id`. No `work_revision` field unless a concrete mutation problem for that kind is proven.)

- Invariant: `one WorkItem ≤ one active claim file`. No role-qualified sibling, no secondary index that can disagree with filesystem. `execution_id` correctness token + optional `ticket_revision` exact equality (+ `implementation_attempt_id` where present) make stale-result rejection mechanical; `generation` is **diagnostic only**, never correctness (see §9).

- **TTL invariant:** `expires_at` may only clean a claim when no matching live `AgentRecord` exists or the owning record is independently stale/dead by heartbeat/PID/lifecycle. A `RUNNING` agent with healthy heartbeat + live PID owns the claim regardless of `expires_at`; TTL never independently revokes a live owner.

- Atomicity: `flock(claims/{work_item_id}.claim.lock)` → check existence → `tmp+replace` → unlock. `execution_id`/`ticket_revision`/`implementation_attempt_id` exact equality is the stale gate.

- `ClaimOutcome` is **typed**, never `None`:

```python
class ClaimOutcome:
    CLAIMED = "CLAIMED"
    ALREADY_CLAIMED = "ALREADY_CLAIMED"
    STORAGE_ERROR = "STORAGE_ERROR"
    LOCK_UNAVAILABLE = "LOCK_UNAVAILABLE"
    INVALID_TICKET = "INVALID_TICKET"
    # INVALID_GENERATION intentionally absent while generation is diagnostic-only
```

Filesystem-full and "already owned" are different operational states and must never produce the same `ReasonCode`.

**Measurable / grep:** `grep -R 'claims/.*\{tid\}\.\{bot\}\|_claim_index\|_claims_by_ticket_id' codebot --include="*.py"` zero hits; `ls state/claims` never shows two `*.claim.json` for same `work_item_id`; `claim_ticket` never returns bare `None` (typed enum required).

---

## 5. Invariant 4 — ONE WORKER SLOT LEDGER

> `MAX_CONCURRENCY` enforced by **atomic reservations over WorkItem slots** (ticket + discovery share the same ledger). `QUEUED + STARTING + RUNNING + ZOMBIE_PENDING_CLEANUP` consume slots. No `count_active()` gate.

```python
SPAWN_STAGGER_SECONDS = 5.0
MAX_CONCURRENCY  # single env source → DispatchGate(max_slots)

token = ledger.reserve()          # atomic: succeeds iff reserved < MAX_CONCURRENCY
if token is None:
    return NO_CAPACITY            # SlotResult, not ClaimOutcome — no type conflation

available = MAX_CONCURRENCY - ledger.reserved_slots
# Slot owned by: QUEUED_committed + STARTING + RUNNING + ZOMBIE_PENDING_CLEANUP
# Slot free only when: DEAD
```

- Single env read in `scheduler_config.MAX_CONCURRENT_AGENTS` (`CODEBOT_MAX_CONCURRENT=90`) → `orchestrator_scheduler.get_scheduler(max_slots=…) → DispatchGate(max_slots)`.
- Duplicate `GATEWAY_MAX_CONCURRENT` aliases collapsed. `token_budget.py` is an informational cost ledger only; it never gates existence of a worker.
- `prompt_gateway.MAX_CONCURRENT`, `bot_registry.GATEWAY_MAX_CONCURRENT`, `process_manager.GATEWAY_MAX_CONCURRENT` — deleted. Provider/API capacity (`adaptive_rate_limiter`, RPM, connection limit) remains as rate limiter only and never influences `reserve()`/`try_dispatch()`.

**WorkItem sharing:** `TicketWork` and `DiscoveryWork` both consume slots from the same `MAX_CONCURRENCY` via the same `ConcurrencyController.reserve()`. No second controller. Discovery `max=3` is an admission cap before `reserve()`, not a reservation — when no discovery is actionable, all 90 slots remain available to ticket work.

```python
# cap semantics (admission filter, not reservation)
if bucket == "DISCOVERY" and active_discovery >= discovery_max:
    skip_this_bucket_this_tick
else:
    try reserve+claim+queue    # global MAX_CONCURRENCY still authoritative
```

Dispatch uses only static `role_caps` + `BUCKET_WEIGHTS` + explicit starvation override. No adaptive `queue_pressure`/`work_scorer` headroom engine ported.

**Measurable / grep:** `grep -R "GATEWAY_MAX_CONCURRENT\|prompt_gateway.*MAX_CONCURRENT\|bot_registry.*GATEWAY_MAX_CONCURRENT" codebot --include="*.py"` zero hits; all capacity decisions go through `ConcurrencyController.reserve/release`.

---

## 6. Invariant 5 — ONE GLOBAL SPAWN QUEUE

> Every dynamic worker process goes through one global queue. Fixed **5-second** process-start interval. One queue, one clock, one interval. FIFO — no `next_allowed_start` cursor.

```python
SPAWN_STAGGER_SECONDS = 5.0

def enqueue(self, request, now):
    request.scheduled_at = now  # diagnostic only
    self._queue.append(request)  # FIFO, no cursor

def drain(self, now):
    if not self._queue:
        return None
    if now < last_process_start + SPAWN_STAGGER_SECONDS:
        return WAITING_FOR_STAGGER  # not dequeued; retry next run_once()
    request = self._queue.pop(0)    # head consumed even on launch failure
    try:
        spawn(request)
    except Exception:
        # head already consumed; agent ZOMBIE→DEAD, release claim+slot
        raise
    last_process_start = now        # updated only on success
    return request

# Derived, not stored:
next_allowed_at = last_process_start + SPAWN_STAGGER_SECONDS
```

- No adaptive staggering, no role-specific staggering, no discovery sleep, no heartbeat sliding. `last_process_start` from `AgentRecord.started_at` only, never heartbeat mtime.
- Cancelling 29 of 30 queued workers does not leave the survivor with a far-future timestamp — once `now >= last_process_start+5`, the survivor drains. Heartbeats never move `scheduled_at`.
- `last_process_start` reconstruction on restart: from latest `AgentRecord.started_at`; if `started_at` missing while a live `STARTING/RUNNING` process exists: `last_process_start = now` (conservative: delays one spawn by 5 s, never violates spacing).

**Measurable / grep:** `grep -R "adaptive_stagger\|DEMAND_STAGGER\|discovery.*stagger\|_last_spawn\|next_allowed_start" codebot --include="*.py"` zero hits; with `FakeClock`, 10 sequential `drain()` calls satisfy `start[i] - start[i-1] >= 5.0`; 30 enqueues with 29 cancellations leave 1 drainable once gate passes.

---

## 7. Invariant 6 — ONE RECONCILER

> Repairs stale/dead/orphan state. **Never** selects work or creates workers.

**Allowed (repairs facts):**
- Mark `RUNNING`/`STARTING` → `ZOMBIE` when pid dead or heartbeat exceeds model-aware `STALE_TIMEOUT`
- `ZOMBIE` → `DEAD`, `release(claim)`, `release(slot)`, remove stale queued spawn
- Sweep orphan claims (no live owner / expired)
- Sweep leaked slots (token without live `AgentRecord`)
- Prune stale dynamic bot state (`*.heartbeat` older than threshold)
- `reject(stale_result)` when result `attempt_id`/`generation` mismatches current claim

**Forbidden (work decisions):**
- `choose_ticket` / `choose_bucket` / `create_worker` / `enqueue_replacement_worker` / `promote_backlog`

The next `tick()` replaces what died. No other code path invokes scheduling.

**Measurable / grep:** `grep -n "def _reconcile\|def reconcile" codebot/scheduler_v2/dispatcher.py` is the sole reconciler definition; static analysis that its transitive callees never reach `try_dispatch` / `reserve` / `start_bot`.

---

## 8. Invariant 7 — ONE-WAY OWNERSHIP

> `WorkItem → claim → agent → slot` is one-way, reconstructable, and internally consistent. For ticket work this specializes to `ticket → claim → agent → slot`.

- Every `QUEUED`/`STARTING`/`RUNNING` agent owns exactly one WorkItem, one claim (`{work_item_id}.claim.json` with matching `agent_id`), and one slot token. `DEAD` owns zero claims and zero slots.
- Every `QUEUED` commit also owns one claim and one slot; cancelling it releases both.
- `doctor scheduler` (or equivalent diagnostics) can enumerate the full mapping without consulting a second index; `work_item_id` prefix distinguishes ticket vs discovery.
- This is what turns `UNKNOWN` into a violation: every non-terminal WorkItem has an explainable `why_not_running` (precedence table, see §9).

---

## 9. Execution Identity & Stale-Rejection

Every worker execution gets:

- `execution_id` — uuid4, **sole correctness token**. Every artifact carries it; ingestion matches on exact `execution_id`.
- `generation` — diagnostic, monotonic dispatch counter per WorkItem. Logged as `inconsistent_generation` if it disagrees, **never** used to gate `drop_as_stale`. Loss of generation history does not affect correctness.
- `ticket_revision` — TicketWork only: exact revision token at claim time for stale check (where present).
- `implementation_attempt_id` — `f"ticket--{ticket_id}:impl:{hex8}"` created **only** when a genuinely new implementation attempt begins (transition to new IMPLEMENT attempt). REVIEW references it, does not create it.

**Stale-rejection rule (authoritative):**

```python
if artifact.execution_id != claim.execution_id:
    drop_as_stale()          # sole ownership gate
elif claim.ticket_revision is not None and artifact.ticket_revision != claim.ticket_revision:
    drop_as_stale()
elif artifact.implementation_attempt_id is not None and claim.implementation_attempt_id is not None:
    if artifact.implementation_attempt_id != claim.implementation_attempt_id:
        drop_or_route_to_correct_attempt()
# diagnostic only — log, do not drop:
if artifact.generation != claim.generation:
    log_inconsistent_generation(artifact, claim)
```

No `ClaimOutcome.INVALID_GENERATION` while generation is diagnostic-only.

---

## 10. Dispatch Ordering — GOAL Priority Tier (deterministic, inside scheduler)

`goal_aligner` returns only `NOW / LATER / NEVER` per ticket. Deterministic tier is applied **inside `BucketDispatcher` ordering of already-`NOW` work**, not inside the GOAL LLM decision:

```python
if ticket.ticket_class in TEST_CLASSES:        # test, test_gap
    goal_priority_tier = 0
elif ticket.ticket_class in DOC_CLASSES:      # documentation
    goal_priority_tier = 1
else:
    goal_priority_tier = 2
if ticket.ticket_class in SAFETY_OVERRIDE and ticket.severity == "critical":
    goal_priority_tier = -1  # security / data-loss critical
```

Ordering key: `goal_priority_tier → priority(critical>high>medium>low) → age → id`. GOAL priority does **not** bypass dispatch ownership, slots, queue, or caps.

**Normative rule:** `BucketDispatcher` MUST order already-`NOW` work by `goal_priority_tier(0=test, 1=docs, 2=other, -1=safety-override)` before normal priority/age/id. GOAL itself remains `NOW/LATER/NEVER` only.

---

## 11. Lifecycle — Ticket Stages + Agent States

**Agent lifecycle (one agent = one WorkItem = one execution lifetime):**

```
CREATED → STARTING → RUNNING → ZOMBIE → DEAD   (DEAD never resurrected)
```

Retry → `new agent_id`, new `execution_id`, same `work_item_id`.

**Ticket stage chain:**

```
Finding → DISCOVERED → GOAL → (LATER|NEVER|NOW) → TRIAGE(ACCEPT/DUPLICATE/…) → DECOMP
  → PLANNING → IMPLEMENT → REVIEW → VERIFY → COMPLETE
                                          ↓ (VERIFY fail → REWORK)
```

- `Finding → DISCOVERED` is platform ingestion (finding inbox `state/findings/{finding_id}.json` → `TicketStore.create(DISCOVERED)`).
- `DISCOVERED → GOAL` is `goal_aligner`; `GOAL(NOW) → TRIAGE` is triager — distinct stages.
- `REVIEW → VERIFY` on gatekeeper-validated APPROVE; `VERIFY → COMPLETE` on verified integration; `VERIFY → REWORK` on candidate/regression/live/BASE_REVISION_CHANGED/crash.
- No direct `REVIEW → COMPLETE` after cutover.

**VERIFY (deterministic worker, not LLM):** Single integration lock `state/.verify_integration.lock`, isolated worktree `git worktree add <tmp>/verify-{ticket_id}-{execution_id} HEAD`, full regression, base-SHA CAS (`HEAD == base_sha` at integration), artifact `verification/CB-xxx.json` journaling incremental phases. `ROLE_CAPS["verify"]=1` serialized.

---

## 12. Atomic Dispatch Transaction (8 steps, attach execution only)

Dispatcher never performs semantic lifecycle transitions. A ticket is already in an eligible stage when dispatched. Dispatch only attaches an execution.

```
1. choose actionable bucket              (non-empty bucket, cap not hit)
2. choose actionable WorkItem            (priority→age→id; WorkItem is TicketWork or DiscoveryWork)
3. reserve global slot                   — ConcurrencyController.reserve() → Token | SlotResult.NO_CAPACITY → WAITING_FOR_SLOT
4. allocate execution identity            — execution_id=uuid4 (correctness), generation++ (diagnostic)
5. atomically claim WorkItem              — flock+tmp+replace → ClaimOutcome
      on non-CLAIMED → release Token, map ClaimOutcome → ReasonCode, no queue entry
6. commit dispatch                        — persist AgentRecord(CREATED) atomically (THIS is the commit)
      on failure → release Claim + Slot, no queue entry
7. enqueue spawn request                  — SpawnQueue.enqueue() (FIFO, advisory scheduled_at)
      on exception → ZOMBIE→DEAD lifecycle, release Claim+Slot
8. (later) SpawnQueue.drain()             — one worker per call if now >= last_process_start+5 → pop_head → spawn → last_process_start=now
```

Crash-consistency: `claims/{work_item_id}.claim.json` + `AgentRecords` persist; `SpawnQueue` is disposable in-memory; slot ledger is derived reconstructable. Restart loads claims+records, validates pid, rebuilds ledger, marks unrecoverable QUEUED→ZOMBIE→DEAD.

---

## 13. Agent Purity

> **Agents produce semantic results. Platform code owns every persistent state transition.**

- Discovery agent → Finding artifact → `PlatformIngest.tick()` → `TicketStore.create(DISCOVERED)`
- Review agent → Verdict artifact → gatekeeper ingestion
- Implement agent → Workspace diff + scratchpad → exit handler + gatekeeper

Even discovery `create_ticket` goes through findings inbox `state/findings/{finding_id}.json` under `AUTHORITATIVE` mode (single-path: same tool name, backend switched to inbox; `fingerprint` dedup remains idempotent).

---

## 14. Debugging Contract

> **Every non-terminal WorkItem has exactly one explainable reason it is not progressing.** `UNKNOWN` is itself an invariant violation.

`Scheduler.why_not_running(work_item_id, store)` returns enumerated `ReasonCode` with canonical **precedence** (first match wins):

```
1  RUNNING
2  STARTING
3  WAITING_FOR_STAGGER(until=…)  # queued, not yet eligible by 5 s gate
4  CLAIMED(agent=…)              # owned, not yet in above states (QUEUED committed)
5  BLOCKED_ON_DEPENDENCY(depends_on=…)
6  ROLE_CAP_REACHED(which=discovery/…)
7  WAITING_FOR_SLOT              # MAX_CONCURRENCY full (SlotResult, not ClaimOutcome)
8  CLAIM_FAILURE(cause=STORAGE_ERROR|LOCK_UNAVAILABLE)  # last attempted claim failed — from event log, not WorkItem-persisted state
9  NO_COMPATIBLE_MODEL           # structurally incompatible config (provider throttling is worker-side wait, never this)
10 PLAN_GATE                     # risk ≥ threshold but no PLAN artifact
11 NEEDS_TRIAGE                  # GOAL==NOW but not yet TRIAGE→ACCEPT
12 NEEDS_GOAL                    # DISCOVERED but not yet DISCOVERED→GOAL
13 READY / ELIGIBLE              # eligible for selection this tick (weighted round-robin may choose another first)
```

`READY` means eligible, not "will be chosen next tick". `CLAIM_FAILURE` reasons are **not** WorkItem-persisted state — after `claim → STORAGE_ERROR` the slot is released and the WorkItem has no claim/agent/queue entry; `why_not_running` returns current structural reason, while `last_dispatch_outcome(work_item_id)` in the scheduler event log records the last attempted outcome.

Implemented as precedence-ordered `if/elif` with exhaustive `else: raise InvariantViolation(UNKNOWN)`.

---

## 15. Configuration

**SchedulerConfig (flat, ≤100 LOC, Phase 3):**

```python
MAX_CONCURRENCY: int = 90          # int(os.getenv("CODEBOT_MAX_CONCURRENT", "90"))
SPAWN_STAGGER_SECONDS: float = 5.0
DISCOVERY_MAX: int = 3             # cap, not reservation
BUCKET_WEIGHTS: dict = {…}
ROLE_CAPS: dict = {"verify": 1, "discovery": 3, …}
```

No `BacklogConfig` / `DiscoveryConfig` / `WorkforceConfig` / `Hysteresis` hierarchy. No `adaptive_rate_limiter` gating `try_dispatch`. Provider throttling is worker-side.

---

## 16. Bounded-Context Simplicity (Phase 3 gate)

Strict `>400 total / >250 pure LOC` is normative for **scheduler/control-plane modules** (`scheduler_v2/*`, `orchestrator*`, `health_check_loop`, `process_manager`, `discovery_daemon`, `dispatch_gate`). Elsewhere it is a strong warning; decomposition is driven by responsibility/cohesion, not line-count sharding. Each split preserves single-owner invariant — no new concurrency owners introduced during decomposition. Gate G5 checks scheduler/control-plane strictly, others as warnings (report, not block) unless subsystem collectively >600 LOC.

---

## 17. Grep-Enforceable Rules (CI)

| Forbidden pattern | Why | Replacement |
|---|---|---|
| `TicketStore(` outside `ticket_dispatcher.get_ticket_store` | Violates Inv. 2 | `get_ticket_store(state_dir)` |
| `_claim_index\|_claims_by_ticket_id` | Two claim formats (§3) | `claims/{work_item_id}.claim.json` |
| `GATEWAY_MAX_CONCURRENT\|prompt_gateway.*MAX_CONCURRENT` | Duplicate WORKER CAPACITY (§4) | `MAX_CONCURRENCY` via `DispatchGate` |
| `adaptive_stagger\|DEMAND_STAGGER\|_last_spawn\|next_allowed_start` | Three stagger mechanisms (§5) | `SPAWN_STAGGER_SECONDS=5.0` + FIFO gate |
| `queue_pressure\|work_scorer\|QueuePressure\|compute_implementation_cap` | Replaced headroom engine | Static `role_caps` + `BUCKET_WEIGHTS` |
| `LegacyDispatchController\|DispatchMode.*LEGACY` (post-Phase 2) | Deleted legacy | Always AUTHORITATIVE |
| `INVALID_GENERATION` | Generation diagnostic-only | `execution_id` authoritative |
| `TicketStore.*start_background_workers` | Flags as cache key | `include_workers` view arg |

---

## 18. Rollback

Tag `pre-authoritative-dispatch` is the rollback point before any dispatch change. Revert preamble (stop scheduler → drain workers → clear authoritative claims → `verify-no-live-authoritative-ownership`) is required before any revert after authoritative mode has run. See `MIGRATION-RUNBOOK.md` / plan §5.1.

---

## 19. Bounded Exceptions (documented, not violations)

- `ticket_status.py` CLI offline helper — single documented exemption for direct `TicketStore(tickets_file)`, or folded behind `get_ticket_store` (Phase 1 Todo 1 decides; leans to fold).
- `tests/*` — `TicketStore(tmp_path)` via fixture, allowed; arch test enforces no production direct construction outside factory.

---

## 23. Durability Test Principle

> If killing every agent destroys important knowledge, that knowledge was stored in the wrong place.

All critical project knowledge MUST exist in durable artifacts (tickets, Findings, ADRs, design docs, constitution, coding standards, co-located module docs) before any agent session terminates. Agent memory, scratchpads, and conversation history are transient working state, not authoritative storage.

**Measurable:** After terminating all running agent processes, a fresh swarm spawned against the same repository MUST be able to resume work without loss of requirements, constraints, decisions, acceptance criteria, or discovered facts. No TODO/FIXME referencing lost context or undocumented decisions in the codebase.

**Enforcement:** Any ticket reaching COMPLETE that left critical context only in agent memory (not written to a durable artifact) is a standards violation. Reviewers MUST verify that decisions, rationale, and constraints from prior lifecycle stages are persisted in durable context (lifecycle_context on tickets, ADRs, design docs, constitution amendments) before approving.
