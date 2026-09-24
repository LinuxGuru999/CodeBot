# ADR 007: Authoritative Dispatch — Seven Strict Invariants

## Status

**Accepted** — 2026-09-24

Phase 0 normative scope (plan: `.omo/plans/authoritative-dispatch.md`). Implementation begins Phase 1 only after G0 green (`CODING_STANDARDS.md` + this ADR + 12 xfail skeletons + `pre-authoritative-dispatch` tag).

---

## Context

Today ~5 dispatchers race (`BucketDispatcher`, 4 legacy `ticket_dispatcher.*` functions, `dispatch_service`, `health_check_loop.start_eligible_bots`, `discovery_daemon`, plus a second `orchestrator_runtime._dispatch_loop` thread on the same gate), two claim formats (`{tid}.{bot}.json` vs `{tid}.claim.json` + `{tid}.{role}.claim.json`), four `MAX_CONCURRENT` aliases, three spawn staggers (adaptive 0.2–1.0 s, heartbeat-sliding, `0.3 s` discovery sleep), and three reconcilers that also schedule. The earlier “hundreds IMPLEMENTING, zero implementation claims” outage was the symptom: tickets transitioned before ownership existed, with no atomic transaction. Debugging required distributed archaeology across inconsistent ownership indexes.

Three additional requirements arrived late and are folded into this ADR's normative scope:

- **A. GOAL priority:** `testing → documentation → everything else` within `GOAL==NOW` (deterministic tier inside dispatcher, not LLM re-ranking).
- **B. Lifecycle `VERIFY`:** `REVIEW → VERIFY → COMPLETE` isolated-worktree + single integration lock + base-SHA CAS + rollback→REWORK.
- **C. One-time ticket reset:** archive-and-regenerate migration resetting all non-`COMPLETE` derived state to `DISCOVERED` (preserving discovery facts) and archiving `COMPLETE` tickets after a quiescent cutover.

Five modules exceed 2000 LOC (`api_runner.py` 3594, `ticket_engine.py` 2521, `ticket_dispatcher.py` 2579, plus `health_check_loop`/`orchestrator_runtime`/`process_manager` as control-plane participants).

---

## Decision

Enforce **7 strict invariants**, each with a single code owner, converging every path through `Scheduler.run_once()` with run-loop ordering `ingest exits → apply platform transitions → _reconcile → ingest findings (Finding→DISCOVERED→GOAL→…→TRIAGE) → dispatch (attach execution only) → drain`. Single high-level flag `DispatchMode={LEGACY|AUTHORITATIVE}` selects `LegacyDispatchController` vs `Scheduler`. ~15 call sites rewired; legacy deleted in one final phase.

### Invariants (one sentence each)

| # | Invariant | Single owner |
|---|-----------|--------------|
| 1 | One thread calling `tick()` — only `Scheduler.run_once()` ever calls `BucketDispatcher.tick()`; reentrancy-guarded | `Scheduler.run_once()` |
| 2 | One ticket authority — `get_ticket_store(state_dir)` keyed on canonical storage identity; one API, one backing store | `get_ticket_store(state_dir)` + backing `tickets.json`+WAL |
| 3 | One claim per WorkItem — exactly one `claims/{work_item_id}.claim.json` namespaced (`ticket--`/`discovery--`), `execution_id` correctness + `generation` diagnostic, exact-equality stale checks | `dispatch_gate.claim_ticket` flock+tmp+replace |
| 4 | One worker slot ledger — atomic `ConcurrencyController.reserve()` over WorkItem slots; `QUEUED+STARTING+RUNNING+ZOMBIE_PENDING_CLEANUP` hold slots | `ConcurrencyController` |
| 5 | One global spawn queue — fixed 5 s, FIFO, authoritative `last_process_start+5` gate, no cursor | `SpawnQueue.drain()` |
| 6 | One reconciler — `Scheduler._reconcile()` repairs facts only, never schedules | `Scheduler._reconcile()` |
| 7 | One-way ownership — `WorkItem → claim → agent → slot` always reconstructable | `WorkItem` claim+AgentRecord+ledger |

For ticket work Invariant 7 specializes to `ticket → claim → agent → slot`; the same mechanism covers `DiscoveryWork(discovery_run_id)` with a different root (no second claim/slot implementation).

### WorkItem model

```python
WorkItem = TicketWork(ticket_id) | DiscoveryWork(discovery_run_id)
# claim path: claims/{work_item_id}.claim.json
#   ticket--CB-xxx  /  discovery--{uuid}
# Claim/slot mechanism identical; only the ownership root differs.
```

`TicketWork` and `DiscoveryWork` share the same `MAX_CONCURRENCY` ledger; discovery has an admission **cap** (`discovery_max=3` inside `BucketDispatcher`) not a reservation — when no discovery is actionable, all 90 slots remain available to ticket work. Discovery pending state is **derived** from `SchedulerMetadata.last_discovery_run` cadence inside `BucketDispatcher.get_actionable_work()`, not a second persisted queue.

### Lock ordering

`ConcurrencyController._lock` → `claims/{work_item_id}.claim.lock` (flock, held briefly) → `SpawnQueue._lock`. Never nest `TicketStore` save-lock inside claim lock.

### Heartbeat role

Liveness only (`is_agent_stale` for reconciler). Never scheduling pressure. Spawn queue's `scheduled_at` never slid by heartbeat. `last_process_start` from `AgentRecord.started_at` only.

### TicketStore API shape

`get_ticket_store(state_dir: Path) -> TicketStore` — view behavior on method args (`list_tickets(include_workers=…)`) never on cache keys. Single backing file + WAL; single flock/version protocol across processes.

### Claim shape (canonical)

`execution_id` (uuid4, correctness token) authoritative; `generation` diagnostic non-authoritative; optional `ticket_revision` exact equality and `implementation_attempt_id` (`ticket--CB-xxx:impl:{hex8}`) where present; namespaced `work_item_id`; legacy `ticket_id`-only alias deprecated one release. `expires_at` never revokes a live owner (heartbeat/PID/lifecycle is authority). ClaimOutcome is typed (`CLAIMED`/`ALREADY_CLAIMED`/`STORAGE_ERROR`/`LOCK_UNAVAILABLE`/`INVALID_TICKET` — no `INVALID_GENERATION` while generation is diagnostic).

### Slot ledger

`MAX_CONCURRENCY` single env source (`CODEBOT_MAX_CONCURRENT=90`) → `DispatchGate(max_slots)`. Duplicates `prompt_gateway.MAX_CONCURRENT`, `bot_registry.GATEWAY_MAX_CONCURRENT`, `process_manager.GATEWAY_MAX_CONCURRENT` deleted. Provider/API capacity (`adaptive_rate_limiter`, RPM) kept but never gates `reserve()/try_dispatch()`. `token_budget.py` scheduling integration deleted (ledger remains as informational cost tracker).

### Spawn queue

`SPAWN_STAGGER_SECONDS=5.0`, FIFO, one clock, `next_allowed_at = last_process_start + 5.0` derived. Launch failure consumes head; agent `ZOMBIE→DEAD`, new `execution_id` on redispatch.

### Reconciler

`Scheduler._reconcile()` allowed: mark ZOMBIE/DEAD, release stale claim/slot/queued, sweep orphans/leaked slots. Forbidden: `choose_ticket`/`choose_bucket`/`create_worker`. Next `tick()` replaces what died.

### Atomic dispatch transaction (attach execution only — no `store.transition`)

```
choose bucket → choose WorkItem → reserve slot (SlotResult) → allocate execution_id+generation
  → flock claim → typed ClaimOutcome → persist AgentRecord(CREATED) [commit]
  → enqueue SpawnRequest → SpawnQueue.drain() staggered start
```

`SlotResult` vs `ClaimOutcome` are distinct types. Stale rejection is `execution_id` exact match (+ `ticket_revision` where present, + `implementation_attempt_id` where present); `generation` mismatch is `inconsistent_generation` diagnostic, never a gate.

### Debugging contract

`Scheduler.why_not_running(work_item_id, store)` returns precedence-ordered `ReasonCode` with exhaustive `else: raise InvariantViolation(UNKNOWN)`. `UNKNOWN` is itself a violation. Every `ReasonCode` maps through the same table; `generation` never participates.

### New normative requirements folded in

- **GOAL tier:** `goal_aligner` stays `NOW/LATER/NEVER`. Deterministic tier `(-1=safety-override, 0=test, 1=docs, 2=other) → priority → age → id` is applied inside `BucketDispatcher` selection of already-`NOW` work. Arch test: synthetic `NOW` set `{test, docs, feature}` selected in that order.
- **VERIFY:** `REVIEW → VERIFY → COMPLETE` with bucket `VERIFY` (`ROLE_CAPS["verify"]=1`), isolated worktree, candidate commit, full regression, single integration `flock` `state/.verify_integration.lock` + base-SHA CAS, artifact `verification/CB-xxx.json` journaling incremental phases. Crash recovery is idempotent via artifact.
- **Migration:** Quiescent `archive-and-regenerate` migration guard by `state/.authoritative_dispatch_migrated`, single-writer flock, `COMPLETE` tickets moved to `archive/completed/` (not deleted), non-`COMPLETE` tickets snapshot to `archive/pre-authoritative-reset/` then `migration_reset_to_discovered` preserving discovery facts while clearing derived downstream state, claims/derived files swept, ephemeral queue/claims cleared.

---

## Alternatives Considered and Rejected

| Alternative | Why rejected |
|---|---|
| Per-callsite `if USE_LEGACY_DISPATCH:` branches | Litters migration across 15 call sites; decays back into multi-owner. Rejected: one top-level `DispatchMode` switch. |
| Second dispatcher thread (`orchestrator_runtime._dispatch_loop`) + adaptive 0.2–1.0 s stagger | Adds exactly the race this ADR eliminates; "boring beats clever" 5 s FIFO is provable. Rejected. |
| Role-qualified claim siblings `{tid}.{role}.claim.json` + secondary index `_claim_index` | Two claim authorities that can disagree; filesystem is already authoritative. Rejected: one `claims/{work_item_id}.claim.json`. |
| Four `MAX_CONCURRENT` aliases + adaptive headroom engine (`queue_pressure`/`work_scorer`/`adaptive_scheduler`) | Duplicate WORKER CAPACITY + conflation with PROVIDER/API capacity. Rejected: one `MAX_CONCURRENCY` env → `DispatchGate`; provider limits stay but never dispatch-gate. |
| Heartbeat-sliding spawn queue / `next_allowed_start` cursor | AI-friendly bug: cancelling N-1 of N queued workers leaves survivor with far-future timestamp. Rejected: derived `last_process_start+5` gate, no cursor. |
| Goal-aligner ordered promotion of testing/docs (LLM re-ranking) | Contaminates GOAL benchmark with a second LLM ranking task; not testable. Rejected: deterministic dispatcher tier inside `BucketDispatcher`. |
| Destructive deletion migration (strip all but id/title/class/severity; delete COMPLETE) | Strips fingerprints needed for dedup → suppresses rediscovery with no provenance; loses RL/review-learning + historical audit. Rejected: archive-and-regenerate (preserve discovery facts, archive COMPLETE). |
| Dual-write `TicketStore.create` + findings inbox during migration | Exactly the dual-authority this ADR eliminates; merge ambiguity. Rejected: single-path inbox `state/findings/{finding_id}.json` behind same tool name under `AUTHORITATIVE`, legacy path deleted. |
| Keeping `INVALID_GENERATION` while calling `generation` diagnostic | Contradiction: diagnostic that also gates correctness. Rejected: remove `INVALID_GENERATION`; `generation` is logged inconsistency only. |
| Sharding oversized modules by LOC line-count | Arbitrary splits make debugging worse. Rejected: bounded-context seams; scheduler/control-plane strictly ≤400/≤250, others warned by cohesion. |

---

## Consequences

### Positive

- Scheduler debugging becomes a check of deterministic invariants rather than distributed archaeology.
- Every non-terminal WorkItem has an explainable `why_not_running`; `UNKNOWN` becomes a test failure.
- Stale `execution_id`/`ticket_revision`/`implementation_attempt_id` results are mechanically rejected; `generation` no longer creates a spurious correctness gate.
- Spawn spacing is provably ≥5 s; discovery shares the same ledger with a cap, not a reservation.
- Crash recovery is deterministic (persisted claim+AgentRecord is source of truth; in-memory queue is disposable).

### Negative / Costs

- **Very large effort** — 4 phases, 11+ todos, sequential; 5 oversized modules (>2000 LOC) decomposed.
- **High risk** — every spawn edge touched. Mitigated by Phase 0 skeletons + annotated tag `pre-authoritative-dispatch`, route→observe→delete layering, a single dispatch-mode switch, atomically reversible transactions, and a **10 k-cycle deterministic simulation gate before any deletion** (required for Phase 1→2 flip).
- `VERIFY` serializes integration (`ROLE_CAPS["verify"]=1` + single flock) — at most one verified commit at a time.
- Migration is one-time and quiescent (must drain live workers, prove no live ownership, snapshot `tickets.json`, guard with marker file); rerunning is a no-op but the first run must be fully quiescent.

### What is deleted

`adaptive_scheduler.py`, `discovery_manager.py`, `queue_pressure.py`, `work_scorer.py` scheduling integration; `token_budget` scheduling gates; legacy dispatchers `spawn_demand_agents`/`dispatch_triage_agents`/`dispatch_decompose_agents`/`dispatch_planning_agents`/`dispatch_ready_tickets`/`run_workforce_dispatchers`/`start_eligible_bots` worker branch; `discovery_daemon._launch/_running_count` private capacity + its 0.3 s sleep; `process_manager._last_spawn_time` + `.last_spawn` + `prompt_gateway.note_spawn()`; `GATEWAY_MAX_CONCURRENT` aliases; `_claim_index`/`_claims_by_ticket_id`; `DEMAND_STAGGER_SECONDS`; `orchestrator_runtime._dispatch_loop` thread; `DispatchMode` flag itself after flip (always `AUTHORITATIVE`). No `queue_pressure`/`work_scorer` headroom ported as renamed engine inside `BucketDispatcher`.

### What is kept

`token_budget.py` as informational cost ledger (never dispatch-gating); `adaptive_rate_limiter`/provider RPM as HTTP concurrency limiter only (worker-side wait, never scheduler policy); discovery as cap not reservation.

### Rollback

Tag `pre-authoritative-dispatch` is the rollback point. Flip tag + revert runbook in plan §5.1 (stop scheduler → drain workers → clear authoritative claims → `verify-no-live-authoritative-ownership` → `git revert 8a/8b` or `reset --hard`). `tickets.json`+WAL remain schema-compatible; only ephemeral state (claims/AgentRecords/queue/ledger) needs clearing. New tickets after migration that are re-processed post-cutover are not double-reset (guard marker).

### Enforcement

`docs/CODING_STANDARDS.md` is the grep-enforceable constitution; 12 architecture tests (`tests/test_arch_authoritative_dispatch.py`) plus 10 k-cycle deterministic simulation (`FakeClock`+`FakeProcessSpawner`+isolated `TicketStore`) required before Phase 1→2 flip and before Phase 2 deletion. Docs/scheduler.md, docs/agent_lifecycle.md, docs/ARCHITECTURE.md, ENTRYPOINT.md updated against final implementation symbols (not line numbers). All 15 relevant `codebot/roles/*.md` + `docs/ROLE_PROMPT_STANDARDS.md §1` reference `docs/CODING_STANDARDS.md`.

---

## References

- Plan: `.omo/plans/authoritative-dispatch.md` (normative)
- Standards: `docs/CODING_STANDARDS.md` (§2–§8 invariants)
- Prior ADR: `docs/adr/006-ui-component-architecture.md` (pattern reference)
- Constitution §4 (single-owner authority)

