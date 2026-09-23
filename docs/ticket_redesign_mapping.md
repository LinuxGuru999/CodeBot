# Ticket Design Mapping: Current → New Lifecycle

Status: PLAN — updated to reflect executed backlog reset and delivered tooling.
Date: 2026-09-22
Last Updated: 2026-09-22

---

## Pre-Reset Baseline (for reference)

Before the backlog reset, diagnostic scripts revealed the following state of the legacy ticket store:

### State Distribution (pre-reset)

| State | Count | Status |
|-------|-------|--------|
| PLANNING | 1,004 | Stuck — no workers dispatching |
| DISCOVERED | 839 | Untriaged |
| IMPLEMENTATION_READY | 773 | Queued, no workers |
| COMPLETE | 720 | Done |
| REJECTED | 483 | Terminal failure |
| DUPLICATE | 278 | Terminal |
| REVIEWING | 57 | In review (all security class) |
| DECOMPOSE | 11 | Breaking down |
| VERIFYING | 1 | Gate check |
| TRIAGED | 1 | Ready for queue |

**Total**: 4,167 tickets. **0 in IMPLEMENTING**. 773 ready to implement with no workers picking them up.

### Diagnostic Findings (`scripts/diagnose_tickets.py`)

- **2,218 issues across 4 categories**: stuck tickets (~1,319 in PLANNING >24h), orphaned dependencies (sub-ticket IDs like `CB-...-sub1` never created), duplicate evidence hashes (65 entries sharing identical fingerprints across COMPLETE/REJECTED/PLANNING states)
- No invalid states or rework loops detected structurally

### Failure Analysis (`scripts/track_lifecycle.py`)

- **129 security tickets** with failure transitions (72 VERIFYING→REWORK, 57 REVIEWING→REWORK)
- **Root causes**: `contract_tests` exit code 2, `unit_tests` exit code 1/timeout
- **~60% show "No specific reason captured"** — reviewer_feedback not persisted to ticket objects (data gap)
- **Worst offenders**: CB-8800B2F83DE7 (87 reworks, 1154h, 99.2% in rework), CB-7954445-FCDB (54 reworks, 424h)
- **5,719 total REWORK transitions** across all classes
- Pipeline timelines showed infinite DECOMPOSE→PLANNING→IMPLEMENTING(0s) loops — agents decomposing, planning, implementing in under a second, getting rejected, repeating without meaningful work

### Backlog Reset Executed

All 3,257 non-COMPLETE tickets were reset to DISCOVERED via `scripts/reset_backlog.py --execute`. This bypassed the forward-only state machine by mutating TicketStore internals directly (_tickets dict + _state_index), since TRANSITIONS has no reverse paths to DISCOVERED from most states and terminal states (REJECTED, DUPLICATE) have zero exits.

**Post-reset distribution**:
| State | Count |
|-------|-------|
| DISCOVERED | 3,839 |
| COMPLETE | 727 |

Backup saved to `.codebot/state/tickets.backup.<timestamp>.json`. All 3,839 DISCOVERED tickets will flow through the new GOAL-first lifecycle when picked up by the new dispatcher.

---

## New Target Lifecycle

```
DISCOVERY
    |
    v
  TRIAGE (token-cheap structural filter)
    |
    +---- DUPLICATE
    +---- NOT_ACTIONABLE
    +---- REJECTED
    |
    v
   GOAL (semantic alignment, only sees validated tickets)
    |
    +---- NOW
    |       |
    |       v
    |     DECOMP
    |       |
    |       v
    |    PLANNING
    |       |
    |       v
    |    IMPLEMENT
    |       |
    |       v
    |     REVIEW
    |       |
    |       +---- COMPLETE
    |       |
    |       +---- REWORK
    |               |
    |               +----> IMPLEMENT
    |               |
    |               +----> DEFERRED
    |
    +---- LATER
    |
    +---- NEVER

UNIVERSAL EXITS (from any stage except COMPLETE):
    |
    +---- RESOLVED    (issue fixed incidentally by other work)
    +---- SUPERSEDED  (work covered by another ticket)
    +---- CANCELLED   (intentionally stopped, scope change, human decision)

BACK-FEEDING (RESOLVED/SUPERSEDED/CANCELLED → discovery suppression):
    Fingerprints + affected_modules indexed so future discovery skips resolved work.

FRONT-RUNNING (before any ticket enters TRIAGE):
    New findings checked against active pipeline + RESOLVED/SUPERSEDED/CANCELLED index.
```

---

## State-by-State Mapping

### DISCOVERED

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.DISCOVERED` | Same |
| Entry point | `TicketStore.add()` after discovery agent calls `create_ticket()` | Same |
| Exit transitions | `VALIDATING`, `TRIAGED`, `REJECTED`, `DUPLICATE` | `TRIAGED` only |
| What happens here | Raw finding, unvalidated | Raw finding, unvalidated |
| Change required | Remove transitions to VALIDATING/REJECTED/DUPLICATE. Only exit is TRIAGED. |

**Key difference**: Currently DISCOVERED can go directly to REJECTED or DUPLICATE. In the new design, DISCOVERED always flows to TRIAGE first. Triage is the structural gate before any semantic evaluation.

---

### TRIAGE (first gate — token-cheap)

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.TRIAGED` | Same (but entered directly from DISCOVERED) |
| Entry point | From `VALIDATING` or `DISCOVERED` | From `DISCOVERED` only |
| Exit transitions | `READY`, `DEFERRED`, `REJECTED` | `GOAL` (passed), `DUPLICATE`, `NOT_ACTIONABLE`, `REJECTED` |
| What happens here | Full LLM validation, severity scoring, risk calculation, role assignment | Structural checks only: fingerprint dedup, evidence file exists, not already fixed. NO severity scoring, NO risk calculation, NO role assignment |
| Agent role | `ticket_triager` (MEDIUM model, 300s timeout, reads source files) | `ticket_triager` rewritten as deterministic platform code or CHEAP model with minimal context |

**Token-cheap design**: Triage must handle 3,839 DISCOVERED tickets without bottlenecking. This means:

1. **Deterministic fast-paths first** (platform code, zero tokens):
   - Fingerprint match against existing tickets → DUPLICATE
   - Fingerprint match against NEVER decisions with same goal_revision → REJECTED
   - Evidence file doesn't exist (simple `Path.exists()` check) → REJECTED
   - Source referenced in evidence no longer contains the pattern → REJECTED

2. **CHEAP model only for ambiguous cases** (tickets that pass all deterministic checks):
   - Minimal context: ticket title + problem_statement + one evidence excerpt (~200 tokens input)
   - Structured output: `{"decision": "GOAL|REJECTED|NOT_ACTIONABLE", "reason": "..."}` (~50 tokens output)
   - Batch up to 20 tickets per call
   - No source file reading, no grep, no glob — only the ticket data itself
   - Timeout: 60s max (down from 300s)

3. **What triage NO LONGER does**:
   - Severity classification (GOAL handles priority)
   - Risk score calculation (removed)
   - Role assignment (DECOMP/PLANNING handles routing)
   - Constitution checking (GOAL handles scope)
   - Reading source files (deterministic checks use file existence only)

**Decision Matrix (new)**:

| Condition | Decision | Method |
|-----------|----------|--------|
| Fingerprint matches existing non-terminal ticket | DUPLICATE | Deterministic |
| Fingerprint matches NEVER decision (same goal_revision) | REJECTED | Deterministic |
| Evidence file missing | REJECTED | Deterministic |
| Pattern not found at evidence location | REJECTED | Deterministic |
| Valid, unique, structurally sound | GOAL | Pass to alignment |
| Valid but requires human decision outside autonomy | NOT_ACTIONABLE | CHEAP model |

**Current dispatch path** (`ticket_dispatcher.py:1082-1271`):
```
dispatch_triage_agents():
  DISCOVERED → spawn triager agent (MEDIUM model, reads source)
  VALIDATING → TRIAGED (line 1191)
  TRIAGED → READY (line 1186)
```

**New dispatch path**:
```
dispatch_triage_agents():
  DISCOVERED → deterministic fast-paths → CHEAP model batch → GOAL or terminal
  No VALIDATING state
  No READY state
```

---

### GOAL (NEW STATE — second gate)

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | Does not exist | `TicketState.GOAL` |
| Entry point | N/A | From `TRIAGED` (only tickets that passed structural validation) |
| Exit transitions | N/A | `DECOMP` (NOW), `LATER`, `NEVER` |
| What happens here | N/A | Goal alignment agent evaluates against project intent |
| Agent role | `goal_steering` exists but is portfolio-level P0-P3 signaling | New `goal_aligner` role: per-ticket NOW/LATER/NEVER classification |
| Disposition output | None | `goal_disposition` field on Ticket: `NOW`, `LATER`, `NEVER` |

**Key change from previous plan**: GOAL now receives only structurally validated tickets. Previously GOAL was first, meaning it wasted alignment calls on hallucinated/duplicate findings. Now triage filters those out cheaply before GOAL ever sees them.

**Relationship to existing goal_steering.md**: REMOVED. The legacy `goal_steering` role (portfolio-level P0-P3 signaling via `goal_steering.status.json`) is superseded by the new per-ticket GOAL alignment. The `goal_steering` role, its prompt file (`codebot/roles/goal_steering.md`), and its status/checkpoint/heartbeat files are deleted during Phase 1. The new `goal_aligner` role replaces it entirely — operating per-ticket rather than at portfolio level, with structured NOW/LATER/NEVER output instead of priority directives.

**Fast-path rules** (no LLM call needed):
- User-requested → NOW (transitions GOAL → DECOMP immediately)
- Critical security vulnerability → NOW
- Confirmed build-breaking bug → NOW
- Rework re-entry → bypass GOAL entirely (REWORK → IMPLEMENT directly)

---

### LATER (NEW STATE)

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | Does not exist (DEFERRED used loosely) | `TicketState.LATER` |
| Entry point | N/A | From `GOAL` when disposition = LATER |
| Exit transitions | `DEFERRED` → `READY`, `TRIAGED`, `DECOMPOSE` | `GOAL` (reassessment only) |
| What happens here | N/A | Parked. Consumes zero downstream compute. |
| Scheduler visibility | DEFERRED tickets counted in some queries | LATER tickets excluded from all actionable queues |

**Promotion trigger**: When actionable NOW supply is depleted (DECOMP + PLANNING + IMPLEMENT all == 0), select bounded batch (default 10) of LATER candidates sorted by severity × goal_relevance × confidence, reassess through GOAL.

**Why not reuse DEFERRED**: DEFERRED currently has transitions to READY, TRIAGED, and DECOMPOSE (line 191-195). LATER is semantically different — it means "not aligned with current goals." Reusing DEFERRED would conflate "paused due to external blocker" with "deferred due to goal misalignment" and risk accidental promotion by the existing `recover_deferred_tickets()` function at `ticket_dispatcher.py:2231`.

---

### NEVER (NEW STATE)

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | Does not exist (REJECTED used) | `TicketState.NEVER` |
| Entry point | N/A | From `GOAL` when disposition = NEVER |
| Exit transitions | Terminal (like REJECTED) | Terminal. No outgoing transitions. |
| What happens here | N/A | Auditable historical record. Never deleted. |
| Duplicate suppression | Evidence hash dedup only | Fingerprint checked during TRIAGE (before GOAL); matching NEVER suppresses rediscovery |

**Why not reuse REJECTED**: REJECTED is a triage outcome (evidence invalid, false positive, duplicate). NEVER is a goal alignment outcome (valid finding, wrong time/scope). Conflating them makes metrics ambiguous. Separate states enable distinct reporting.

**Reconsideration**: When `goal_revision` increments, NEVER tickets with stale revision become eligible for inclusion in next LATER promotion batch. Not automatic promotion — they re-enter GOAL evaluation.

---

### DUPLICATE (terminal from TRIAGE)

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.DUPLICATE` | Same |
| Entry point | From `DISCOVERED` or `VALIDATING` | From `TRIAGED` only (deterministic fingerprint match) |
| Exit transitions | Terminal | Terminal |
| What happens here | Dedup via evidence_hash/fingerprint | Same — handled by deterministic fast-path in triage, no LLM call needed |

**Change**: Currently DUPLICATE is reachable from DISCOVERED and VALIDATING (lines 129, 134). In new design, only reachable from TRIAGED via deterministic fingerprint comparison. Zero token cost.

---

### NOT_ACTIONABLE (NEW STATE)

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | Does not exist | `TicketState.NOT_ACTIONABLE` |
| Entry point | N/A | From `TRIAGED` (CHEAP model determination) |
| Exit transitions | N/A | Terminal |
| What happens here | N/A | Finding is structurally valid but cannot be acted on (requires human decision outside autonomy level, blocked by constitution) |

**Distinction from REJECTED**: REJECTED = finding is invalid/false positive/duplicate. NOT_ACTIONABLE = finding is real and unique but the system cannot act on it within current constraints. High NOT_ACTIONABLE rate signals autonomy gaps; high REJECTED rate signals discovery quality problems.

**Distinction from NEVER**: NEVER = valid finding, wrong time/scope (goal alignment decision). NOT_ACTIONABLE = valid finding, impossible to implement (triage decision). NEVER is produced by GOAL; NOT_ACTIONABLE is produced by TRIAGE.

---

### DECOMP (maps to current DECOMPOSE)

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.DECOMPOSE` | Same (rename to DECOMP optional) |
| Entry point | From `READY` | From `GOAL` (disposition = NOW) |
| Exit transitions | `PLANNING`, `READY`, `BLOCKED` | `PLANNING` only |
| What happens here | Decomposer agent breaks compound tickets into atomic sub-tickets | Same |

**Key differences**:
1. **No more READY intermediary**. TRIAGED → DECOMP directly.
2. **No backward transition to READY**. Current DECOMPOSE → READY (line 149) creates loops. New design: DECOMP → PLANNING only.
3. **No BLOCKED exit**. If decomposition reveals a blocker, the sub-tickets carry the dependency info; the parent doesn't stall.

**Current dispatch**: `dispatch_decompose_agents()` at `ticket_dispatcher.py:1274` queries READY tickets. Must change to query TRIAGED tickets (those that passed triage with DECOMP needed) or DECOMP-ready tickets.

---

### PLANNING

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.PLANNING` | Same |
| Entry point | From `READY`, `DECOMPOSE` | From `DECOMP` only |
| Exit transitions | `IMPLEMENTATION_READY`, `DECOMPOSE`, `BLOCKED` | `IMPLEMENT` only |
| What happens here | Implementation planner generates step-by-step plan | Same |

**Key differences**:
1. **Single entry**: From DECOMP only. Currently reachable from READY (line 143) and DECOMPOSE (line 148).
2. **Single exit**: To IMPLEMENT. Currently has backward transitions to DECOMPOSE and BLOCKED.
3. **No IMPLEMENTATION_READY intermediary**. Current flow: PLANNING → IMPLEMENTATION_READY → IMPLEMENTING. New flow: PLANNING → IMPLEMENT. The IMPLEMENTATION_READY holding pen is eliminated.

**Impact on pre-reset stuck tickets**: Historical data showed 1,004 tickets stuck in PLANNING. After backlog reset, all non-COMPLETE tickets were returned to DISCOVERED. The simplified exit (PLANNING → IMPLEMENT only, no backward loops) prevents this accumulation pattern from recurring under the new lifecycle.

---

### IMPLEMENT (maps to current IMPLEMENTING)

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.IMPLEMENTING` | Rename to `IMPLEMENT` or keep `IMPLEMENTING` |
| Entry point | From `IMPLEMENTATION_READY` | From `PLANNING` or `REWORK` |
| Exit transitions | `REVIEWING`, `BLOCKED`, `REWORK`, `IMPLEMENTATION_READY` | `REVIEW` only |
| What happens here | Implementer agent writes code | Same |

**Key differences**:
1. **No IMPLEMENTATION_READY predecessor**. PLANNING → IMPLEMENT directly.
2. **Single forward exit**: To REVIEW. Currently has 4 exits including backward to IMPLEMENTATION_READY.
3. **REWORK enters directly**: REWORK → IMPLEMENT (not REWORK → IMPLEMENTATION_READY → IMPLEMENTING).
4. **No BLOCKED exit**. Dependencies handled at PLANNING/DECOMP level.

---

### REVIEW (maps to current REVIEWING)

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.REVIEWING` | Rename to `REVIEW` or keep `REVIEWING` |
| Entry point | From `IMPLEMENTING` | From `IMPLEMENT` |
| Exit transitions | `VERIFYING`, `REWORK` | `COMPLETE`, `REWORK` |
| What happens here | Adversarial reviewers evaluate implementation | Same |

**Key difference**: **No VERIFYING state**. Current flow: REVIEWING → VERIFYING → COMPLETE/REWORK. New flow: REVIEW → COMPLETE/REWORK. Verification (build, tests, lint) becomes part of the REVIEW stage rather than a separate state. The gatekeeper logic merges into review completion.

**Gatekeeper merge implementation**: Gatekeeper checks (deterministic gates: build, unit_tests, contract_tests, coverage) move into `advance_reviewed_tickets()` (`ticket_dispatcher.py:1664`). Before transitioning REVIEW → COMPLETE, the function calls gatekeeper verification inline. If gates fail, transition goes to REWORK instead. The gatekeeper module remains the authority for gate evaluation — it just gets called from the REVIEW exit path rather than requiring its own state. This preserves the existing gate enforcement that caught 5,646 VERIFYING→REWORK failures pre-reset, without the extra state transition hop.

**Rationale**: VERIFYING adds a full state transition cycle for what is essentially deterministic gate execution. Gates don't need their own state — they're a check within REVIEW completion. Eliminating VERIFYING removes the 5,646 VERIFYING→REWORK transitions seen in the lifecycle tracker data.

---

### COMPLETE

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.COMPLETE` | Same |
| Entry point | From `VERIFYING` (gatekeeper approval) | From `REVIEW` |
| Exit transitions | Terminal | Terminal |
| What happens here | Commit, close | Same |

**Change**: Entered directly from REVIEW instead of requiring VERIFYING → gatekeeper → COMPLETE.

---

### REWORK

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.REWORK` | Same |
| Entry point | From `REVIEWING`, `VERIFYING`, `IMPLEMENTING` | From `REVIEW` only |
| Exit transitions | `IMPLEMENTATION_READY`, `DECOMPOSE`, `PLANNING`, `REJECTED`, `DEFERRED` | `IMPLEMENT`, `DEFERRED` |
| What happens here | Implementer corrects defects from reviewer feedback | Same |

**Key differences**:
1. **Single entry**: From REVIEW only. Currently reachable from REVIEWING, VERIFYING, and IMPLEMENTING.
2. **Two exits only**: IMPLEMENT (retry) or DEFERRED (give up). Currently has 5 exits including backward to DECOMPOSE and PLANNING, creating the infinite loops visible in the pipeline timelines (CB-9067805-F528 cycling through DECOMPOSE hundreds of times).
3. **DEFERRED exit is new**: Allows parking a rework ticket that can't be fixed within current constraints, rather than looping endlessly.
4. **Bypasses GOAL**: Rework → IMPLEMENT skips goal alignment. System already committed to this work.

**Impact on rework loops**: Historical data showed tickets like CB-8800B2F83DE7 with 87 reworks spending 99.2% of lifecycle in rework. All such tickets were reset to DISCOVERED during the backlog reset. The constrained exits (IMPLEMENT or DEFERRED only, no backward to DECOMPOSE/PLANNING) prevent these spirals from recurring. After max_rework_cycles (3), the ticket should go to DEFERRED, not loop again.

---

### DEFERRED

| Aspect | Current | New |
|--------|---------|-----|
| Enum value | `TicketState.DEFERRED` | Same |
| Entry point | From `TRIAGED`, `READY`, `IMPLEMENTATION_READY`, `BLOCKED` | From `REWORK` only |
| Exit transitions | `READY`, `TRIAGED`, `DECOMPOSE` | Terminal (or back to `IMPLEMENT` on explicit human request) |
| What happens here | General-purpose pause | Specifically: rework that exceeded cycles or hit irrecoverable blocker |

**Semantic narrowing**: Currently DEFERRED is used as a general "pause" button reachable from 4 states with 3 exits. In the new design, DEFERRED is specifically for rework that cannot proceed. It becomes near-terminal. LATER serves the "parked for future consideration" purpose with its own promotion mechanism.

---

---

### RESOLVED (NEW — universal exit)

| Aspect | Definition |
|--------|------------|
| Enum value | `TicketState.RESOLVED` |
| Entry point | From ANY non-COMPLETE state (DISCOVERED, TRIAGED, GOAL, DECOMP, PLANNING, IMPLEMENT, REVIEW, REWORK, LATER, DEFERRED) |
| Exit transitions | Terminal |
| What happens here | The underlying issue was fixed incidentally by other work. Not implemented by this ticket, but the problem no longer exists. |

**Distinction from COMPLETE**: COMPLETE means this ticket's implementation resolved the issue. RESOLVED means something else resolved it (another ticket's work, external change, repository evolution).

**Distinction from REJECTED**: REJECTED means the finding was invalid/false positive. RESOLVED means the finding was valid but is now moot.

**Back-feeding**: When a ticket enters RESOLVED, its fingerprint and affected_modules are added to the resolved index. Future discovery agents check this index before creating tickets. If a new finding matches a RESOLVED fingerprint, it's suppressed unless evidence shows the issue regressed.

**Trigger examples**:
- Triage discovers the evidence file no longer contains the problematic pattern (fixed by another ticket)
- Implementation reveals the bug was already resolved in a dependency update
- Review finds the acceptance criteria are already met by existing code

---

### SUPERSEDED (NEW — universal exit)

| Aspect | Definition |
|--------|------------|
| Enum value | `TicketState.SUPERSEDED` |
| Entry point | From ANY non-COMPLETE state |
| Exit transitions | Terminal |
| Metadata | `superseded_by: str` — ticket ID that covers this work |
| What happens here | This ticket's intended work is now covered by a different, newer, or broader ticket. |

**Distinction from DUPLICATE**: DUPLICATE means two tickets describe the exact same finding discovered independently. SUPERSEDED means a ticket was valid when created but a later ticket covers the same ground more broadly or more correctly. DUPLICATE is detected at TRIAGE; SUPERSEDED can be detected at any stage.

**Back-feeding**: When a ticket enters SUPERSEDED, its fingerprint redirects to the superseding ticket. Discovery checks both the superseded fingerprint and the superseding ticket's scope before creating new work.

**Front-running**: Before a new ticket enters TRIAGE, its fingerprint is checked against active pipeline tickets. If an in-progress ticket already covers the same modules/problem, the new finding is marked SUPERSEDED immediately without consuming triage compute.

**Trigger examples**:
- DECOMP reveals that three tickets target overlapping code; two become SUPERSEDED by the broadest one
- A user request creates a comprehensive ticket that covers several smaller autonomous discoveries
- PLANNING identifies that a refactor ticket makes a separate bug-fix ticket unnecessary

---

### CANCELLED (NEW — universal exit)

| Aspect | Definition |
|--------|------------|
| Enum value | `TicketState.CANCELLED` |
| Entry point | From ANY non-COMPLETE state |
| Exit transitions | Terminal |
| Metadata | `cancelled_reason: str`, `cancelled_by: str` (agent/human/system) |
| What happens here | Work was intentionally stopped. Scope changed, human overrode, project direction shifted. Distinct from REJECTED (invalid), NEVER (goal misalignment), or RESOLVED (already fixed). |

**Distinction from REJECTED**: REJECTED = finding is false/invalid. CANCELLED = finding is valid but we chose not to pursue it for operational reasons.

**Distinction from NEVER**: NEVER = goal alignment determined this doesn't fit current objectives. CANCELLED = external decision (human override, scope reduction, budget constraint) stopped work that might otherwise be valid.

**Distinction from DEFERRED**: DEFERRED = pause due to rework exhaustion. CANCELLED = permanent stop.

**Back-feeding**: CANCELLED tickets' fingerprints are indexed with a `cancelled_reason`. Discovery checks this index. If the cancellation reason was scope-related (e.g., "feature removed from roadmap"), matching findings are suppressed. If the reason was transient (e.g., "budget paused"), they may be allowed through on reassessment.

**Who can cancel**:
- Human operator via `botop cancel <ticket_id> --reason "..."`
- Goal alignment agent if project_intent.yaml changes mid-flight
- System if max global rework budget is exceeded

---

### Back-Feeding Mechanism

Back-feeding ensures that when a ticket reaches RESOLVED, SUPERSEDED, or CANCELLED, that knowledge flows backward into the discovery pipeline to prevent redundant work.

**Implementation**: A new index file `.codebot/state/resolved_index.json` (append-only, compacted periodically):

```json
{
  "fingerprints": {
    "abc123def456": {"state": "RESOLVED", "ticket_id": "CB-xxx", "resolved_at": 0.0, "modules": ["codebot/auth.py"]},
    "789xyz012abc": {"state": "SUPERSEDED", "ticket_id": "CB-yyy", "superseded_by": "CB-zzz", "at": 0.0},
    "def456ghi789": {"state": "CANCELLED", "ticket_id": "CB-www", "reason": "scope_change", "by": "human", "at": 0.0}
  }
}
```

**Discovery integration**: Before `create_ticket()` is called (in the discovery-to-ticket path), the finding's fingerprint is checked against this index. Match behavior:

| Index State | Action |
|-------------|--------|
| RESOLVED | Suppress finding UNLESS evidence shows regression (pattern reappeared) |
| SUPERSEDED | Suppress finding, log reference to superseding ticket |
| CANCELLED (scope_change) | Suppress finding |
| CANCELLED (transient) | Allow finding through with annotation |

**Triage integration**: During deterministic fast-paths in `dispatch_triage_agents()`, the resolved index is checked alongside the NEVER index. This catches cases where a ticket was created before the resolution landed.

---

### Front-Running Mechanism

Front-running checks new findings against the ACTIVE pipeline (tickets currently in TRIAGED through REVIEW) before they enter the queue.

**Implementation**: In-memory or file-based active work index, updated as tickets transition:

```python
active_work_index: dict[str, set[str]]  # fingerprint → set of active ticket_ids
```

Updated atomically when tickets enter TRIAGED (added) and reach COMPLETE/RESOLVED/SUPERSEDED/CANCELLED/REJECTED/DUPLICATE/NEVER/LATER/DEFERRED (removed).

**Check point**: In `dispatch_triage_agents()`, before deterministic fast-paths run:

1. Compute finding fingerprint
2. Check `active_work_index` for match
3. If match found → new ticket is SUPERSEDED by the active ticket (transition DISCOVERED → TRIAGED → SUPERSEDED with `superseded_by` pointing to active ticket)
4. No LLM call needed — purely structural comparison

**Why this matters**: With 3,839 DISCOVERED tickets entering the pipeline after reset, many will overlap. Front-running prevents duplicate work from consuming even cheap triage tokens.

---

### States Eliminated

| Current State | Why Removed | Replacement |
|---------------|-------------|-------------|
| `VALIDATING` | Separate validation state adds latency; validation is part of triage | Merged into `TRIAGED` |
| `READY` | Holding pen between TRIAGED and DECOMPOSE with no processing | Direct `TRIAGED → DECOMP` |
| `IMPLEMENTATION_READY` | Holding pen between PLANNING and IMPLEMENTING with no processing | Direct `PLANNING → IMPLEMENT` |
| `VERIFYING` | Deterministic gates don't need a state; verification is part of review completion | Merged into `REVIEW` |
| `BLOCKED` | Used inconsistently across 5 states; dependencies handled at DECOMP/PLANNING level | Removed; use `DEFERRED` from REWORK or dependency metadata at PLANNING |

---

## Complete Transition Map

### Current TRANSITIONS (ticket_engine.py lines 124-199)

```
DISCOVERED → VALIDATING, TRIAGED, REJECTED, DUPLICATE
VALIDATING → TRIAGED, REJECTED, DUPLICATE
TRIAGED → READY, DEFERRED, REJECTED
READY → DECOMPOSE, PLANNING, IMPLEMENTATION_READY, DEFERRED
DECOMPOSE → PLANNING, READY, BLOCKED
PLANNING → IMPLEMENTATION_READY, DECOMPOSE, BLOCKED
IMPLEMENTATION_READY → IMPLEMENTING, DEFERRED, BLOCKED
IMPLEMENTING → REVIEWING, BLOCKED, REWORK, IMPLEMENTATION_READY
REVIEWING → VERIFYING, REWORK
VERIFYING → COMPLETE, REWORK, REJECTED
REWORK → IMPLEMENTATION_READY, DECOMPOSE, PLANNING, REJECTED, DEFERRED
BLOCKED → READY, DECOMPOSE, PLANNING, IMPLEMENTATION_READY, DEFERRED
DEFERRED → READY, TRIAGED, DECOMPOSE
COMPLETE → (terminal)
REJECTED → (terminal)
DUPLICATE → (terminal)
```

16 states, 42 transitions.

### New TRANSITIONS

```
DISCOVERED → TRIAGED
TRIAGED → GOAL, DUPLICATE, NOT_ACTIONABLE, REJECTED, RESOLVED, SUPERSEDED, CANCELLED
GOAL → DECOMP, LATER, NEVER, RESOLVED, SUPERSEDED, CANCELLED
DECOMP → PLANNING, RESOLVED, SUPERSEDED, CANCELLED
PLANNING → IMPLEMENT, RESOLVED, SUPERSEDED, CANCELLED
IMPLEMENT → REVIEW, RESOLVED, SUPERSEDED, CANCELLED
REVIEW → COMPLETE, REWORK, RESOLVED, SUPERSEDED, CANCELLED
REWORK → IMPLEMENT, DEFERRED, RESOLVED, SUPERSEDED, CANCELLED
LATER → GOAL, RESOLVED, SUPERSEDED, CANCELLED
DEFERRED → RESOLVED, SUPERSEDED, CANCELLED
RESOLVED → (terminal)
SUPERSEDED → (terminal)
CANCELLED → (terminal)
NEVER → (terminal)
DUPLICATE → (terminal)
NOT_ACTIONABLE → (terminal)
COMPLETE → (terminal)
REJECTED → (terminal)
```

18 states. Core forward path: 17 transitions. Universal exits: every non-COMPLETE, non-terminal state can reach RESOLVED/SUPERSEDED/CANCELLED.

**Universal exit rule**: Rather than hardcoding 26+ additional transitions in the TRANSITIONS dict, implement as a programmatic rule in `TicketStore.transition()`:

```python
UNIVERSAL_EXITS = frozenset({TicketState.RESOLVED, TicketState.SUPERSEDED, TicketState.CANCELLED})
# In transition validation:
if new_state in UNIVERSAL_EXITS and ticket.state != TicketState.COMPLETE:
    allow_transition()  # bypass normal TRANSITIONS lookup
```

This keeps the TRANSITIONS dict clean while enforcing the "any stage except COMPLETE" constraint structurally.

**Reduction**: Core forward path reduced from 42 to 17 transitions. Universal exits add structured termination without creating loops. The graph is a DAG with only two intentional cycles: LATER → GOAL (promotion) and REWORK → IMPLEMENT (retry). RESOLVED/SUPERSEDED/CANCELLED are always terminal sinks.

**Key ordering change from previous plan**: TRIAGED now comes BEFORE GOAL in the transition chain (`DISCOVERED → TRIAGED → GOAL → DECOMP`). Triage is the cheap structural filter; GOAL is the semantic alignment gate that only sees validated tickets.

---

## Schema Changes

### New TicketState Enum Values

```python
class TicketState(str, Enum):
    DISCOVERED = "DISCOVERED"
    TRIAGED = "TRIAGED"
    GOAL = "GOAL"                    # NEW
    DECOMP = "DECOMP"                # RENAMED from DECOMPOSE (or alias)
    PLANNING = "PLANNING"
    IMPLEMENT = "IMPLEMENT"          # RENAMED from IMPLEMENTING (or alias)
    REVIEW = "REVIEW"                # RENAMED from REVIEWING (or alias)
    COMPLETE = "COMPLETE"
    REWORK = "REWORK"
    DEFERRED = "DEFERRED"
    LATER = "LATER"                  # NEW
    NEVER = "NEVER"                  # NEW
    DUPLICATE = "DUPLICATE"
    REJECTED = "REJECTED"
    NOT_ACTIONABLE = "NOT_ACTIONABLE" # NEW
    RESOLVED = "RESOLVED"            # NEW — universal exit
    SUPERSEDED = "SUPERSEDED"        # NEW — universal exit
    CANCELLED = "CANCELLED"          # NEW — universal exit
```

### Removed TicketState Enum Values

- `VALIDATING`
- `READY`
- `IMPLEMENTATION_READY`
- `VERIFYING`
- `BLOCKED`
- `DECOMPOSE` (if renamed to DECOMP)
- `IMPLEMENTING` (if renamed to IMPLEMENT)
- `REVIEWING` (if renamed to REVIEW)

### New Ticket Fields

```python
goal_disposition: str = ""           # NOW | LATER | NEVER | ""
goal_reason: str = ""                # Alignment justification
goal_relevant_to: str = ""           # Which goal/milestone
goal_revision: int = 0               # Project intent version at decision time
goal_aligned_at: float = 0.0         # Decision timestamp
goal_reconsider_when: str = ""       # LATER promotion trigger condition
superseded_by: str = ""              # Ticket ID that supersedes this one (for SUPERSEDED state)
cancelled_reason: str = ""           # Why work was stopped (for CANCELLED state)
cancelled_by: str = ""               # Who cancelled: "human", "goal_agent", "system"
resolved_by: str = ""                # How it was resolved incidentally (for RESOLVED state)
```

All fields have defaults (empty string / 0) for backward-compatible deserialization via `Ticket.from_dict()` filtering (line 296-298).

### Schema Version

Bump from `"2.0"` to `"3.0"` (major version due to state machine restructuring).

---

## Dispatcher Mapping

### Current Dispatch Functions → New Equivalents

| Current Function | File:Line | What It Does | New Equivalent |
|------------------|-----------|--------------|----------------|
| `dispatch_triage_agents()` | ticket_dispatcher.py:1082 | Processes DISCOVERED/VALIDATING/TRIAGED → spawns MEDIUM model triager, reads source files | Rewritten: processes DISCOVERED → front-running check → deterministic fast-paths + CHEAP model batch → GOAL or terminal. Integrates resolved_index and active_work_index. No source file reading. |
| `dispatch_goal_agents()` | NEW | Does not exist | Processes TRIAGED→GOAL tickets through semantic alignment → DECOMP (NOW), LATER, or NEVER |
| `dispatch_decompose_agents()` | ticket_dispatcher.py:1274 | Processes READY → spawns decomposer | Processes GOAL(NOW) → DECOMP tickets via `scheduler_v2.DispatchGate.try_dispatch()`. Scheduler V2 owns slot allocation and claim atomicity; dispatcher queries `list_by_state(TicketState.DECOMP)` and feeds candidates through the gate. |
| `dispatch_planning_agents()` | ticket_dispatcher.py:1466 | Processes DECOMPOSE → spawns planner | Processes DECOMP → spawns planner |
| `dispatch_implementation_agents()` | (in adaptive_scheduler) | Processes IMPLEMENTATION_READY → spawns implementer | Processes PLANNING → spawns implementer |
| `advance_reviewed_tickets()` | ticket_dispatcher.py:1664 | Processes REVIEWING → VERIFYING → COMPLETE/REWORK | Processes REVIEW → COMPLETE/REWORK |
| `recover_deferred_tickets()` | ticket_dispatcher.py:2231 | DEFERRED → READY/DECOMPOSE | Removed or scoped to explicit human override |

### Triage Dispatch: Option A — Platform Code + Scheduler V2 Gate

Triage does NOT use `BucketDispatcher.tick()` for its primary work. Deterministic checks run as synchronous platform code directly in the orchestrator health tick. Only tickets that survive all deterministic checks get dispatched as CHEAP model batches through `scheduler_v2.DispatchGate.try_dispatch()`.

**Why Option A**: Triage is primarily `Path.exists()`, fingerprint comparison, and index lookups — pure Python, zero tokens. Spawning agents for this wastes stagger slots and concurrency capacity. The CHEAP model batch (for ambiguous cases only) is the sole part needing agent dispatch.

**Phase 1: Deterministic processing (orchestrator tick, synchronous, zero tokens)**

New function: `run_triage_fast_paths(store, resolved_index, active_work_index) -> int`
Called directly by the orchestrator health loop, not through BucketDispatcher.

1. Query `store.list_by_state(TicketState.DISCOVERED)`
2. For each ticket, run deterministic checks. Each check that fails results in a **single direct transition** from DISCOVERED to the terminal state (not a two-hop through TRIAGED). This avoids atomicity gaps where another thread could claim the ticket mid-transition. Requires UNIVERSAL_EXITS rule to also cover SUPERSEDED/DUPLICATE/REJECTED from DISCOVERED state.
   a. **Front-running check**: compute fingerprint → check `active_work_index` → if match, single transition DISCOVERED → SUPERSEDED (set `superseded_by`). Continue to next ticket.
   b. **Resolved index check**: fingerprint → check `resolved_index` → if RESOLVED (no regression), SUPERSEDED, or CANCELLED(scope_change), single transition DISCOVERED → REJECTED. Continue.
   c. **Duplicate check**: fingerprint → check existing non-terminal tickets → if match, single transition DISCOVERED → DUPLICATE. Continue.
   d. **NEVER suppression**: fingerprint → check NEVER decisions with same goal_revision → if match, single transition DISCOVERED → REJECTED. Continue.
   e. **Evidence existence**: `Path(ticket.evidence_file).exists()` → if missing, single transition DISCOVERED → REJECTED. Continue.
   f. **Pattern check**: simple string-in-file check at evidence location → if not found, single transition DISCOVERED → REJECTED. Continue.
   g. If all checks pass → first transition DISCOVERED → TRIAGED, then add to `ambiguous_batch` list.
3. All transitions use `store.transition()` which emits lifecycle events and triggers back-feeding automatically. The single-transition approach ensures each lifecycle event is atomic — no intermediate TRIAGED state visible to other threads for rejected/superseded tickets.
4. Return count of processed tickets.

**Phase 2: CHEAP model dispatch (through scheduler_v2 gate)**

Tickets in `ambiguous_batch` from Phase 1 are the only ones dispatched as agents. Each ticket is dispatched individually through the gate — no synthetic batch-tickets.

1. For each ticket in `ambiguous_batch`, call `scheduler.request_agent_spawn(role="ticket_triager", ticket_id=tid, bucket="TRIAGE", model=<CHEAP>)`
2. This flows through `DispatchGate.try_dispatch()`: reserves concurrency slot → claims ticket → enqueues spawn with stagger. Each ticket gets its own claim, preventing double-dispatch.
3. The 5-second stagger between spawns naturally throttles CHEAP model calls. With 55 max slots, up to 55 triage agents can be queued concurrently.
4. Agent prompt receives compact context for ONE ticket: title + problem_statement + one evidence excerpt (~200 tokens input)
5. Structured output: `{"decision": "GOAL|NOT_ACTIONABLE", "reason": "..."}` (~50 tokens output)
6. Timeout: 60s max
7. On completion: transition TRIAGED → GOAL (passed) or TRIAGED → NOT_ACTIONABLE

**Why individual dispatch instead of batching**: `DispatchGate.try_dispatch()` claims exactly one ticket per call. Dispatching 20 tickets through one agent would leave 19 unclaimed and vulnerable to double-dispatch by concurrent scheduler ticks. Individual dispatch costs more stagger time but guarantees claim safety. The deterministic fast-paths already eliminate ~90% of tickets before this stage, so the ambiguous batch is small enough that individual dispatch doesn't bottleneck.

**Key implication for BUCKET_ORDER**: The TRIAGE bucket in scheduler_v2 only sees tickets that survived deterministic fast-paths. It does NOT process all DISCOVERED tickets. The bucket state list should be empty under normal operation (deterministic paths handle everything); it only activates when ambiguous tickets need the CHEAP model.

### New Dispatch Function: `dispatch_goal_agents()`

Processes TRIAGED tickets (those that reached GOAL state) through semantic alignment:

1. Query `store.list_by_state(TicketState.GOAL)`
2. Apply fast-path rules deterministically (user request → NOW, critical security → NOW)
3. For non-fast-path tickets: claim, spawn `goal_aligner` agent with compact context packet including project_intent.yaml
4. Agent writes structured decision → parse `goal_disposition`
5. Transition: GOAL → DECOMP (NOW), GOAL → LATER, or GOAL → NEVER
6. Any GOAL-stage ticket can also transition to RESOLVED/SUPERSEDED/CANCELLED via universal exit rule
7. Record alignment metrics
8. **Active work index update**: When ticket enters DECOMP, add to `active_work_index`. When it reaches any terminal state, remove from index.

### Back-Feeding Index Management

New module: `codebot/resolved_index.py`

- `ResolvedIndex` class backed by `.codebot/state/resolved_index.json`
- `add(ticket_id, fingerprint, state, metadata)` — called by `TicketStore.transition()` when target is RESOLVED/SUPERSEDED/CANCELLED
- `check(fingerprint)` → returns match or None
- `compact()` — periodic dedup/cleanup
- Thread-safe via file lock (same pattern as TicketStore)
- Discovery path calls `check()` before `create_ticket()`
- Triage dispatcher calls `check()` as first step in front-running

### Active Work Index Management

New module: `codebot/active_work_index.py`

- In-memory dict persisted to `.codebot/state/active_work.json` for crash recovery
- `add(ticket_id, fingerprint, modules)` — called when ticket enters GOAL state (not TRIAGED; under Option A deterministic fast-paths blow through TRIAGED instantly, so adding at TRIAGED would cause every ticket to briefly flash in the index even if immediately rejected)
- `remove(ticket_id)` — called when ticket reaches any terminal state (COMPLETE, REJECTED, DUPLICATE, NOT_ACTIONABLE, RESOLVED, SUPERSEDED, CANCELLED, NEVER) or parked state (LATER, DEFERRED)
- `check(fingerprint)` → returns active ticket_id or None
- Used by front-running in `run_triage_fast_paths()`
- **Cleanup on startup**: On orchestrator boot, rebuild active_work_index by scanning store for tickets in GOAL, DECOMP, PLANNING, IMPLEMENT, REVIEW, REWORK states. Prevents stale entries from crash recovery file persisting across restarts.

### Scheduler V2 Integration

`scheduler_v2/dispatcher.py` owns all agent dispatch through `BucketDispatcher.tick()`. The `BUCKET_ORDER` constant (line 80) must be rewritten to match the new lifecycle states.

**Current BUCKET_ORDER** (references eliminated states):
```python
BUCKET_ORDER = [
    ("DISCOVERY", ["DISCOVERED", "VALIDATING"]),
    ("TRIAGE", ["TRIAGED"]),
    ("READY", ["READY"]),
    ("DECOMPOSE", ["DECOMPOSE"]),
    ("PLANNING", ["PLANNING"]),
    ("IMPLEMENTATION", ["IMPLEMENTATION_READY"]),
    ("REVIEW", ["REVIEWING"]),
]
```

**New BUCKET_ORDER**:
```python
BUCKET_ORDER = [
    ("GOAL", ["TRIAGED"]),              # semantic alignment (triage-passed tickets land here)
    ("DECOMP", ["GOAL"]),               # decomposition (NOW-disposition tickets from GOAL)
    ("PLANNING", ["DECOMP"]),           # planning
    ("IMPLEMENT", ["PLANNING", "REWORK"]), # implementation + rework retry
    ("REVIEW", ["IMPLEMENT"]),          # review + gatekeeper merge
]
```

Note: DISCOVERED is intentionally absent from BUCKET_ORDER. Triage runs as platform code (Option A), not through BucketDispatcher. LATER, NEVER, RESOLVED, SUPERSEDED, CANCELLED, DEFERRED, DUPLICATE, REJECTED, NOT_ACTIONABLE, and COMPLETE are terminal/parked states — never dispatched.\n\n**Bucket name vs state name distinction**: The first element in each tuple is a label used for role routing and metrics. The second element is the list of TicketState enum values queried via `store.list_by_state()`. So `("GOAL", ["TRIAGED"])` means: query tickets in TRIAGED state, label them as the GOAL bucket, route to goal_aligner role. This is intentional — tickets land in TRIAGED state after passing deterministic triage, and the GOAL bucket picks them up for semantic alignment. Document this mapping explicitly in code comments to prevent confusion during maintenance.

**BUCKET_TO_ROLE_SETS updates**:
```python
BUCKET_TO_ROLE_SETS = {
    "GOAL": frozenset({"goal_aligner"}),
    "DECOMP": DECOMPOSER_ROLE_NAMES,
    "PLANNING": PLANNING_ROLE_NAMES,
    "IMPLEMENT": IMPLEMENTER_ROLE_NAMES,
    "REVIEW": REVIEWER_ROLE_NAMES,
}
```

**`why_not_running()` updates** (`dispatcher.py:437`): Add LATER, NEVER, NOT_ACTIONABLE, RESOLVED, SUPERSEDED, CANCELLED to the NO_ACTIONABLE_WORK check alongside existing COMPLETE, REJECTED, DUPLICATE.

**`_role_for_ticket()` updates** (`dispatcher.py:346`): GOAL bucket always routes to `goal_aligner` regardless of ticket_class. Other buckets continue using TICKET_CLASS_TO_IMPLEMENTER mapping.

**Discovery backpressure** (`dispatcher.py:252`): Current logic suppresses DISCOVERY bucket if downstream >= 1000 tickets. Under new design, also suppress discovery if GOAL queue depth exceeds threshold (prevent flooding alignment agent).

**Orchestrator integration**: Replace individual `dispatch_*_agents()` calls in the orchestrator health loop with:
1. `run_triage_fast_paths(store, resolved_index, active_work_index)` — synchronous, platform code
2. `scheduler_v2.Scheduler.tick(store)` — handles GOAL through REVIEW via BucketDispatcher
3. Legacy dispatch functions become dead code after verification

---

## Migration Strategy

### COMPLETED: Backlog Reset

All 3,257 non-COMPLETE tickets were bulk-reset to DISCOVERED using `scripts/reset_backlog.py --execute`. This used direct TicketStore internal mutation (_tickets dict + _state_index) because the existing TRANSITIONS graph has no reverse paths to DISCOVERED from most states, and terminal states (REJECTED, DUPLICATE) have empty frozensets with zero exits.

A timestamped backup was created automatically before mutation. Post-reset: 3,839 DISCOVERED + 727 COMPLETE.

### Phase 1: Infrastructure (current)

1. Add new states to `TicketState` enum (GOAL, LATER, NEVER, NOT_ACTIONABLE, RESOLVED, SUPERSEDED, CANCELLED)
2. Add new fields to `Ticket` dataclass with defaults (goal_disposition, goal_reason, goal_relevant_to, goal_revision, goal_aligned_at, goal_reconsider_when, superseded_by, cancelled_reason, cancelled_by, resolved_by)
3. Update TRANSITIONS dict: remove old transitions, add new forward-path transitions per the transition map above
4. Implement UNIVERSAL_EXITS rule in `TicketStore.transition()` — allow RESOLVED/SUPERSEDED/CANCELLED from any non-COMPLETE state
5. Bump SCHEMA_VERSION to "3.0"
6. Create `.codebot/project_intent.yaml` template
7. Implement `codebot/resolved_index.py` (ResolvedIndex class, `.codebot/state/resolved_index.json`)
8. Implement `codebot/active_work_index.py` (ActiveWorkIndex class, `.codebot/state/active_work.json`)
9. Delete `codebot/roles/goal_steering.md` and remove `goal_steering` from any role lists/configs. Superseded by per-ticket GOAL alignment.
10. Implement `run_triage_fast_paths()` as synchronous platform function in orchestrator tick (Option A). Front-running → deterministic checks → collect ambiguous batch. No agent spawning. Uses single direct transitions from DISCOVERED to terminal states (not two-hop through TRIAGED).
11. Rewrite `ticket_triager.md` prompt: CHEAP model only, ~200 token input per ticket, structured GOAL/NOT_ACTIONABLE output, 60s timeout. Only processes tickets that survived deterministic fast-paths. Each ticket dispatched individually through scheduler_v2 gate.
12. Update `BUCKET_ORDER` in `scheduler_v2/dispatcher.py` to new lifecycle states. Remove DISCOVERED/VALIDATING/READY/IMPLEMENTATION_READY/REVIEWING. Add GOAL. Rename DECOMPOSE→DECOMP, IMPLEMENTATION→IMPLEMENT, REVIEWING→REVIEW. Document that bucket names are labels while state lists are what gets queried.
13. Update `BUCKET_TO_ROLE_SETS` to include GOAL→goal_aligner mapping.
14. Update `why_not_running()` to recognize new terminal states (LATER, NEVER, NOT_ACTIONABLE, RESOLVED, SUPERSEDED, CANCELLED) as NO_ACTIONABLE_WORK alongside existing COMPLETE, REJECTED, DUPLICATE.
15. Rewrite `rework_target_state()` (`ticket_dispatcher.py:276-284`) to always return `TicketState.IMPLEMENT` (or `TicketState.DEFERRED` if max cycles exceeded). Currently returns DECOMPOSE, IMPLEMENTATION_READY, or PLANNING — all eliminated as rework targets. Without this fix, rework transitions will raise ValueError.
16. Expand `TicketStore.add()` duplicate exclusion list (`ticket_engine.py:955-970`) to include LATER, NEVER, RESOLVED, SUPERSEDED, CANCELLED, NOT_ACTIONABLE. Currently only excludes COMPLETE, REJECTED, DUPLICATE. Without this, discovery crashes with ValueError when creating a ticket whose fingerprint matches a LATER or NEVER ticket.
17. Audit all references to `TicketState.REVIEWING`, `TicketState.IMPLEMENTING`, `TicketState.DECOMPOSE`, `TicketState.VALIDATING`, `TicketState.READY`, `TicketState.IMPLEMENTATION_READY`, `TicketState.VERIFYING`, `TicketState.BLOCKED` across the entire codebase. Either rename enum values with backward-compatible aliases or update every reference. Key files: `ticket_dispatcher.py`, `adaptive_scheduler.py`, `botop.py`, `queue_pressure.py`, `work_scorer.py`, all role .md prompts.
18. Register `goal_aligner` role in `role_registry.py`
19. Write `codebot/roles/goal_aligner.md` prompt
20. Wire `scheduler_v2.Scheduler.tick(store)` into orchestrator health loop as single dispatch entry point. Remove or feature-flag ALL legacy `dispatch_*_agents()` calls to prevent double-dispatch.
21. Merge gatekeeper verification into `advance_reviewed_tickets()` REVIEW→COMPLETE path. Ensure `store.record_commit()` is called after successful gate pass (currently triggered by VERIFYING→COMPLETE path which is being eliminated).
22. Add size cap and TTL-based eviction to `resolved_index.json`. Add cleanup-on-startup to `active_work_index` to prevent stale entries from crash recovery.
23. Add `reward_from_lifecycle_outcome()` to `rl_engine.py`
24. Write full test suite per Test Strategy section above

### Phase 2: Fast-Path Triage + Alignment

1. Enable `run_triage_fast_paths()` in orchestrator tick — deterministic front-running + fast-paths process all DISCOVERED tickets synchronously, zero tokens
2. Ambiguous survivors dispatched as CHEAP model batches through `scheduler_v2.Scheduler.tick()` TRIAGE bucket
3. Enable back-feeding: wire `TicketStore.transition()` to call `resolved_index.add()` on RESOLVED/SUPERSEDED/CANCELLED transitions
4. Enable deterministic fast-paths in `dispatch_goal_agents()` via BucketDispatcher GOAL bucket (user request → NOW, critical security → NOW)
5. Non-fast-path GOAL tickets get `goal_disposition = "PENDING"` until LLM alignment is enabled
6. Verify 3,839 DISCOVERED tickets begin flowing through deterministic triage → GOAL → DECOMP pipeline with front-running preventing redundant work

### Phase 3: Full LLM Alignment

1. Enable `goal_aligner` agent dispatch for non-fast-path tickets
2. Implement LATER promotion trigger (empty NOW queue detection)
3. Implement small-batch reassessment (default 10 candidates)
4. Add NEVER fingerprint suppression to discovery pipeline

### Phase 4: Observability & Monitoring (partially delivered)

1. `scripts/monitor_failures.py` — persistent polling of lifecycle_events.jsonl ✅ DELIVERED
   - Update ALL_FAILURE_STATES to include RESOLVED, SUPERSEDED, CANCELLED when those buckets are activated
2. `botop failures` subcommand — failure-only dashboard ✅ DELIVERED
   - Update FAILURE_STATES frozenset to include RESOLVED, SUPERSEDED, CANCELLED when those buckets are activated
3. `scripts/track_lifecycle.py` — multi-view analysis tool ✅ DELIVERED
4. `scripts/diagnose_tickets.py` — structural health checks ✅ DELIVERED
   - Update valid states list to include all new states
5. Add alignment stats to botop live dashboard
6. Add alignment metrics to scheduler_metrics.py

Note: RESOLVED/SUPERSEDED bucket reporting deferred to future phase. Monitor and track_lifecycle will surface them once the states are actively used.

### Legacy Tickets

The 727 COMPLETE tickets remain untouched. The 3,839 DISCOVERED tickets are clean slate — they enter the new GOAL-first lifecycle on next dispatch cycle. No retroactive alignment needed.

---

## Observability & Monitoring

### Delivered Scripts

| Script | Purpose | Status |
|--------|---------|--------|
| `scripts/diagnose_tickets.py` | Structural health: stuck tickets, orphaned deps, duplicate evidence, schema drift, missing fields | ✅ Delivered |
| `scripts/track_lifecycle.py` | Multi-view analysis: failures, rework chains, stage timing, agent performance, bottlenecks, snapshot, pipeline timelines | ✅ Delivered |
| `scripts/monitor_failures.py` | Persistent poller watching lifecycle_events.jsonl for failure state transitions (REWORK, REJECTED, DEFERRED, DUPLICATE, BLOCKED, NOT_ACTIONABLE, NEVER). Incremental byte-offset tracking, stdout alerts + JSONL alert log | ✅ Delivered |
| `scripts/reset_backlog.py` | Bulk reset non-COMPLETE tickets to DISCOVERED via store-internal mutation. Auto-backup, dry-run default | ✅ Delivered |

### monitor_failures.py Usage

```bash
# Persistent monitoring (polls every 30s, reports new failures as they happen)
python3 scripts/monitor_failures.py

# Custom interval
python3 scripts/monitor_failures.py --interval 10

# Single scan including pre-existing failures
python3 scripts/monitor_failures.py --once --scan-existing

# Watch specific failure states only
python3 scripts/monitor_failures.py --states REWORK,REJECTED

# Custom alert log location
python3 scripts/monitor_failures.py --alert-log /path/to/alerts.jsonl
```

Alerts are written to both stdout and `.codebot/state/failure_alerts.jsonl`. Byte offset tracked in `.codebot/state/.monitor_offset` for incremental reads. Handles file truncation/rotation gracefully. SIGINT/SIGTERM clean shutdown.

### track_lifecycle.py Views

```bash
python3 scripts/track_lifecycle.py --view failures --class-filter security
python3 scripts/track_lifecycle.py --view rework --top 20
python3 scripts/track_lifecycle.py --view timing
python3 scripts/track_lifecycle.py --view snapshot
python3 scripts/track_lifecycle.py --view pipeline --ticket-ids CB-xxx,CB-yyy
python3 scripts/track_lifecycle.py --view all --json
python3 scripts/track_lifecycle.py --view failures --since 24
```

Joins three data sources: lifecycle_events.jsonl (transition timeline), lifecycle_packets/*.json (per-ticket gate evidence), tickets.json (metadata + reviewer_feedback).

### botop.py Updates

New `failures` subcommand added to `codebot/botop.py`:

```bash
# Show all failed tickets
python3 -m codebot.botop failures

# Filter by specific failure state
python3 -m codebot.botop failures --state REWORK

# JSON output for programmatic consumption
python3 -m codebot.botop failures --json

# Limit output
python3 -m codebot.botop failures --limit 20
```

Output includes:
- Total failed count
- Breakdown by state (REWORK, REJECTED, DEFERRED, DUPLICATE, BLOCKED, NOT_ACTIONABLE, NEVER)
- Breakdown by ticket class
- Per-ticket table sorted by rework count descending: ticket ID, state, class, severity, reworks, attempts, title
- Diagnostic script pointers in header

Also wired into the interactive terminal loop (`botop term`).

### Future: Goal Alignment Observability

Once GOAL state is implemented, extend with:
- `--view goal`: GOAL decision distribution (NOW/LATER/NEVER rates, by class, by discovery role)
- `--view later`: Parked LATER tickets with promotion priority ordering
- `--view never`: NEVER decisions with reasons for audit
- botop status dashboard addition:
```
Goal Alignment:
  Pending: X
  NOW: X
  LATER: X
  NEVER: X
  Promoted: X
  Suppressed: X
```
- Alignment metrics in `scheduler_metrics.py` and `alignment_metrics.jsonl`

---

## Test Strategy

### State Machine Tests

| Test | Validates |
|------|-----------|
| `test_discovered_to_triaged` | DISCOVERED → TRIAGED is allowed |
| `test_triaged_to_goal` | TRIAGED → GOAL is allowed |
| `test_triaged_to_duplicate` | TRIAGED → DUPLICATE is allowed |
| `test_triaged_to_rejected` | TRIAGED → REJECTED is allowed |
| `test_triaged_to_not_actionable` | TRIAGED → NOT_ACTIONABLE is allowed |
| `test_goal_to_decomp` | GOAL → DECOMP is allowed (NOW disposition) |
| `test_goal_to_later` | GOAL → LATER is allowed |
| `test_goal_to_never` | GOAL → NEVER is allowed |
| `test_decomp_to_planning` | DECOMP → PLANNING is allowed |
| `test_planning_to_implement` | PLANNING → IMPLEMENT is allowed |
| `test_implement_to_review` | IMPLEMENT → REVIEW is allowed |
| `test_review_to_complete` | REVIEW → COMPLETE is allowed |
| `test_review_to_rework` | REVIEW → REWORK is allowed |
| `test_rework_to_implement` | REWORK → IMPLEMENT is allowed |
| `test_rework_to_deferred` | REWORK → DEFERRED is allowed |
| `test_later_to_goal` | LATER → GOAL is allowed (promotion) |

### Invalid Transition Tests

| Test | Validates |
|------|-----------|
| `test_no_discovered_to_goal` | DISCOVERED → GOAL raises ValueError (must go through TRIAGED) |
| `test_no_discovered_to_rejected` | DISCOVERED → REJECTED raises ValueError (must go through TRIAGED) |
| `test_no_triaged_to_decomp` | TRIAGED → DECOMP raises ValueError (must go through GOAL) |
| `test_no_goal_to_implement` | GOAL → IMPLEMENT raises ValueError (must go through DECOMP→PLANNING) |
| `test_no_later_to_decomp` | LATER → DECOMP raises ValueError (must reassess through GOAL) |
| `test_no_rework_to_decomp` | REWORK → DECOMP raises ValueError (no backward loops) |
| `test_no_rework_to_planning` | REWORK → PLANNING raises ValueError (no backward loops) |
| `test_no_deferred_to_ready` | DEFERRED → READY raises ValueError (DEFERRED is terminal now) |
| `test_no_complete_to_anything` | COMPLETE → any state raises ValueError |

### Universal Exit Tests

| Test | Validates |
|------|-----------|
| `test_resolved_from_discovered` | DISCOVERED → RESOLVED allowed via UNIVERSAL_EXITS |
| `test_resolved_from_triaged` | TRIAGED → RESOLVED allowed |
| `test_resolved_from_goal` | GOAL → RESOLVED allowed |
| `test_resolved_from_decomp` | DECOMP → RESOLVED allowed |
| `test_resolved_from_planning` | PLANNING → RESOLVED allowed |
| `test_resolved_from_implement` | IMPLEMENT → RESOLVED allowed |
| `test_resolved_from_review` | REVIEW → RESOLVED allowed |
| `test_resolved_from_rework` | REWORK → RESOLVED allowed |
| `test_resolved_from_later` | LATER → RESOLVED allowed |
| `test_superseded_from_any_non_complete` | SUPERSEDED reachable from all non-COMPLETE states |
| `test_cancelled_from_any_non_complete` | CANCELLED reachable from all non-COMPLETE states |
| `test_universal_exit_blocked_from_complete` | COMPLETE → RESOLVED raises ValueError |
| `test_universal_exit_blocked_from_complete_superseded` | COMPLETE → SUPERSEDED raises ValueError |
| `test_universal_exit_blocked_from_complete_cancelled` | COMPLETE → CANCELLED raises ValueError |

### LATER/NEVER Isolation Tests (Core Invariant)

| Test | Validates |
|------|-----------|
| `test_later_cannot_reach_decomp` | No valid transition path from LATER to DECOMP except through GOAL |
| `test_later_cannot_reach_planning` | No valid transition path from LATER to PLANNING |
| `test_later_cannot_reach_implement` | No valid transition path from LATER to IMPLEMENT |
| `test_later_cannot_reach_review` | No valid transition path from LATER to REVIEW |
| `test_never_is_terminal` | NEVER has empty frozenset in TRANSITIONS |
| `test_never_cannot_reach_any_state` | NEVER → any state raises ValueError |
| `test_later_not_in_decompose_dispatch` | `dispatch_decompose_agents()` ignores LATER tickets |
| `test_later_not_in_planning_dispatch` | `dispatch_planning_agents()` ignores LATER tickets |
| `test_later_not_in_implementation_dispatch` | Implementation dispatch ignores LATER tickets |

### Schema Migration Tests

| Test | Validates |
|------|-----------|
| `test_legacy_ticket_loads_without_goal_fields` | Schema 2.0 tickets deserialize with empty defaults for all new fields |
| `test_new_fields_default_empty` | goal_disposition, superseded_by, cancelled_reason, resolved_by all default to "" |
| `test_schema_version_bumped` | SCHEMA_VERSION == "3.0" |
| `test_removed_states_still_deserialize` | Tickets with old state strings (VALIDATING, READY, etc.) handled gracefully during drain period |

### RL Reward Tests

| Test | Validates |
|------|-----------|
| `test_reward_complete_is_1` | `reward_from_lifecycle_outcome("COMPLETE")` returns 1.0 |
| `test_reward_rejected_is_0` | `reward_from_lifecycle_outcome("REJECTED")` returns 0.0 |
| `test_reward_goal_pass_is_0_3` | `reward_from_lifecycle_outcome("GOAL")` returns 0.3 |
| `test_reward_deferred_is_0_2` | `reward_from_lifecycle_outcome("DEFERRED")` returns 0.2 |

### Gatekeeper Merge Tests

| Test | Validates |
|------|-----------|
| `test_review_to_complete_runs_gates` | REVIEW → COMPLETE transition invokes gatekeeper checks |
| `test_gate_failure_goes_to_rework` | Failed gate during REVIEW exit transitions to REWORK, not COMPLETE |
| `test_all_gates_pass_completes` | Passing all gates allows REVIEW → COMPLETE |
| `test_record_commit_called_on_complete` | `store.record_commit()` fires after successful gate pass in merged flow |

### Edge Case Tests

| Test | Validates |
|------|-----------|
| `test_rework_target_returns_implement` | `rework_target_state()` returns IMPLEMENT, not DECOMPOSE/PLANNING/IMPLEMENTATION_READY |
| `test_rework_target_returns_deferred_after_max_cycles` | `rework_target_state()` returns DEFERRED when rework_count >= max |
| `test_add_excludes_later_fingerprint` | `TicketStore.add()` doesn't raise ValueError when fingerprint matches LATER ticket |
| `test_add_excludes_never_fingerprint` | `TicketStore.add()` doesn't raise ValueError when fingerprint matches NEVER ticket |
| `test_add_excludes_resolved_fingerprint` | `TicketStore.add()` doesn't raise ValueError when fingerprint matches RESOLVED ticket |
| `test_single_transition_atomicity` | Fast-path rejection uses one transition (DISCOVERED→REJECTED), not two hops through TRIAGED |
| `test_active_work_index_adds_at_goal` | Active work index adds ticket at GOAL state, not TRIAGED |
| `test_active_work_index_cleanup_on_startup` | Stale entries purged on orchestrator boot |
| `test_resolved_index_size_cap` | Resolved index evicts oldest entries when size exceeds cap |
| `test_no_double_dispatch` | Legacy dispatch functions disabled when scheduler_v2.tick() is active |

---

## Cross-Cutting Principle: Performance Is Key

Every agent role and lifecycle stage must be designed for speed and effectiveness. The existing RL engine (`codebot/rl_engine.py`) provides the optimization backbone — it must be extended from prompt-only optimization to lifecycle-wide performance steering.

### Existing RL Infrastructure

The codebase already has a multi-armed bandit system with:

| Component | Location | Function |
|-----------|----------|----------|
| `choose_pattern()` | rl_engine.py:690 | Epsilon-greedy action selection (explore vs exploit) |
| `update_q_value()` | rl_engine.py:718 | Q(a) ← Q(a) + α(R - Q(a)) learning rule |
| `decay_epsilon()` | rl_engine.py:736 | Adaptive exploration decay based on reward delta |
| `reward_from_score()` | rl_engine.py:278 | Differential scoring with bounded metrics shaping |
| `record_event_reward()` | rl_engine.py:758 | Per-bot EMA reward tracking, streak detection |
| `score_event()` | rl_engine.py:496 | Exit event → 0-100 score computation |
| `DEFAULT_Q_VALUES` | rl_engine.py:90 | Bootstrapped Q-values for 10 prompt patterns |
| `BOT_PROMPT_MAP` | rl_engine.py:103 | Maps all 26+ roles to prompt files |
| `alignment_service.py` | alignment_service.py:120 | Post-exit pipeline: score → reward → record → trigger |

Current wiring: `alignment_service.run_alignment_pipeline()` fires after bot exit. Computes reward, updates Q-values, writes optimization triggers when reward < 0.6. This only optimizes prompts — it doesn't steer discovery scope, triage strategy, model selection, or batch sizing.

### Extending RL to Lifecycle Stages

Each lifecycle stage should expose tunable "arms" (actions) to the bandit, with rewards derived from downstream outcomes:

| Stage | Arms (Actions) | Reward Signal |
|-------|----------------|---------------|
| Discovery | Scope breadth, file selection strategy, pattern focus areas | Finding yield rate (validated tickets / scans), duplicate rate, NOW-rate at GOAL |
| Triage | Fast-path threshold tuning, CHEAP model batch size, context window size | Tokens spent per ticket, false negative rate (valid findings rejected), throughput |
| Goal Alignment | Context packet composition, batch size, fast-path rules | NOW→COMPLETE rate, LATER promotion success rate, tokens per decision |
| Decomposition | Sub-ticket granularity, decomposition depth | Rework rate downstream, implementation success rate |
| Planning | Plan detail level, step count | Implementation attempts-to-success ratio |
| Implementation | Model selection, iteration budget | Review pass rate, rework count, gate pass rate |
| Review | Reviewer count, adversarial intensity | Escaped defect rate, completion confidence |

### RSI (Recursive Self-Improvement) Integration

RSI means the system uses its own RL signals to improve its own components. Concrete applications:

1. **Discovery yield optimization**: Track which discovery roles produce findings that survive TRIAGE → GOAL → DECOMP → COMPLETE. Roles with high end-to-end yield get more scheduler slots. Roles with high REJECTED/DUPLICATE rates get fewer. Use `choose_pattern()` over discovery strategies, not just prompt patterns.

2. **Triage cost minimization**: The token-cheap triage design is the first application. Track tokens-per-ticket and rejection-accuracy. If deterministic fast-paths catch 90% of invalid findings, the CHEAP model batch can shrink. RL tunes the threshold between deterministic and model-based checks.

3. **Model routing via RL**: Currently `WORKER_MODEL_CYCLE` in `ticket_dispatcher.py:286-315` uses a fixed rotation. Replace with `choose_pattern()` where arms are model IDs and reward is completion success rate per model. Exploitation favors models with high success; exploration tries cheaper models periodically.

4. **Prompt evolution per role**: Already partially implemented. Extend from `prompt_optimizer` triggers to continuous Q-value tracking per role. When a role's avg_reward drops below 0.6, automatically trigger prompt refinement.

5. **Batch size tuning**: For triage and goal alignment, batch size (tickets per LLM call) is an arm. Too small = overhead. Too large = context overflow or degraded quality. RL finds the optimum per role.

### RL Reward Signal: Acceptable Ticket Creation

The primary reward signal for discovery and triage stages is **whether a created ticket is acceptable** — meaning it survives the full pipeline without being rejected as duplicate, false positive, or misaligned.

**Reward definition**:

| Outcome | Reward |
|---------|--------|
| Ticket reaches COMPLETE | 1.0 |
| Ticket reaches REVIEW (entered pipeline, got implemented) | 0.7 |
| Ticket reaches DECOMP or PLANNING (accepted by GOAL) | 0.5 |
| Ticket reaches GOAL (passed triage) | 0.3 |
| Ticket REJECTED at TRIAGE (false positive, no evidence) | 0.0 |
| Ticket DUPLICATE at TRIAGE | 0.0 |
| Ticket NEVER at GOAL (valid but misaligned) | 0.1 |
| Ticket reaches DEFERRED (rework exhaustion) | 0.2 |

This gives dense intermediate rewards (triage pass = 0.3, goal pass = 0.5) so the bandit doesn't have to wait hours/days for COMPLETE to learn. The final outcome retroactively adjusts the Q-value when the ticket terminates.

**Implementation**: Add `reward_from_lifecycle_outcome(terminal_state: str) -> float` to `rl_engine.py`. Called by `alignment_service.py` when a ticket reaches any terminal state. Maps the terminal state to the reward table above, then calls `record_event_reward()` for the originating discovery role and triage agent.

### Good Discovery Design

Discovery is the most expensive stage because it feeds everything downstream. Bad discovery creates cascading waste. Principles:

1. **Yield-aware scanning**: Don't scan unchanged code. Use cooldown tracking (already in `discovery_manager.py:64-78`) keyed by commit SHA. RL tunes cooldown duration per role based on historical yield.

2. **Scope narrowing via RL**: Discovery roles currently scan broadly. Track which file patterns/modules produce actionable findings. Over time, `choose_pattern()` narrows scope to high-yield areas. Exploration occasionally broadens to prevent blind spots.

3. **Duplicate suppression upstream**: The resolved_index and active_work_index (front-running) prevent duplicate tickets from entering the pipeline. But discovery itself should check fingerprints BEFORE creating tickets. Currently `findings_log.py` records findings but doesn't suppress creation. Add fingerprint check against `resolved_index.json` and `active_work.json` in the discovery-to-ticket path.

4. **Role-specific Q-tables**: Each discovery role gets its own Q-table tracking which scanning strategies produce valid findings. bug_hunter's optimal strategy differs from documentation_auditor's. `rl_engine.ensure_bot()` already supports per-bot state.

5. **Cost-aware discovery**: Track tokens-per-finding per role. Architecture auditor using PREMIUM model with 0.02 yield rate is burning compute. RL should shift allocation toward roles with better cost-per-valid-finding ratios.

### Performance Constraints Per Stage

Hard limits that no agent may exceed:

| Stage | Max Tokens/Input | Max Timeout | Max Batch | Model Tier |
|-------|-----------------|-------------|-----------|------------|
| Triage (deterministic) | 0 | N/A | N/A | None (platform code) |
| Triage (CHEAP model) | ~200 | 60s | 20 | CHEAP |
| Goal Alignment | ~800 | 120s | 10 | CHEAP |
| Decomposition | ~2000 | 300s | 1 | STANDARD |
| Planning | ~3000 | 300s | 1 | STANDARD |
| Implementation | Full context | Role timeout | 1 | Per RL routing |
| Review | Full context | Role timeout | 1 | PREMIUM |

These aren't suggestions — they're enforced caps in the dispatch functions. Agents that hit limits are terminated and their exit events scored as failures by `reward_from_score()`, feeding back into RL.

### Integration Points for RL Extension

| File | Change |
|------|--------|
| `codebot/rl_engine.py` | Add lifecycle-stage arms to `DEFAULT_Q_VALUES`. Add `reward_from_lifecycle_outcome()` function that computes reward from ticket terminal state (COMPLETE=1.0, REWORK=0.3, REJECTED=0.0, etc.) |
| `codebot/alignment_service.py` | Extend `run_alignment_pipeline()` to record lifecycle-level rewards, not just prompt-level scores |
| `codebot/dispatch_triage_agents()` | Call `choose_pattern()` to select triage strategy (batch size, fast-path thresholds) |
| `codebot/dispatch_goal_agents()` | Call `choose_pattern()` to select alignment context composition |
| `codebot/ticket_dispatcher.py` | Replace `WORKER_MODEL_CYCLE` with RL-driven model selection |
| `codebot/discovery_manager.py` | Feed yield stats into `record_event_reward()` per discovery role |
| `codebot/adaptive_scheduler.py` | Use per-role avg_reward to weight slot allocation |

---

## Risk Assessment

| Risk | Mitigation |
|------|------------|
| Renaming states breaks external integrations | Keep old enum values as aliases during migration. External consumers read string values. |
| ~~4167 existing tickets in old states~~ | **RESOLVED**: All non-COMPLETE tickets bulk-reset to DISCOVERED. Clean slate. 727 COMPLETE untouched. |
| GOAL becomes bottleneck | Fast-paths handle majority. Cheap model, small context, batch evaluation for rest. |
| LATER accumulation | Promotion trigger + batch limit + periodic staleness pruning. |
| NEVER suppression blocks valid rediscovery | goal_revision check. Changed goals allow reconsideration. |
| Eliminating VERIFYING loses gate enforcement | Gates merge into `advance_reviewed_tickets()` REVIEW→COMPLETE path. Gatekeeper module still evaluates — called inline before COMPLETE transition. |
| Eliminating BLOCKED loses dependency tracking | Dependencies tracked as metadata on ticket. Handled at DECOMP/PLANNING level where they're most relevant. |
| Bulk reset corrupts store | Timestamped backup auto-created before mutation. Direct store-internal mutation preserves _state_index, _state_counts, WAL consistency. Verified: 0 errors on 3,257 tickets. |
| Monitor misses events between polls | Byte-offset tracking ensures no events skipped. File truncation handled (offset resets to 0). |
| Failure alerts flood operator | Configurable `--states` filter. Alert log persists to JSONL for post-hoc analysis. `--once` mode for manual checks. |
| Triage bottleneck on 3,839 DISCOVERED tickets | Deterministic fast-paths handle majority (fingerprint dedup, file existence). CHEAP model batch processes remainder at ~200 tokens input / ~50 tokens output per ticket. No source file reading. Target <2s per batch of 20. |
| Triage accuracy degrades without source reading | Deterministic checks (file exists, pattern present) catch obvious false positives. Ambiguous cases pass through to GOAL alignment which has richer context. Triage is a cheap filter, not a perfect gate. |
| CHEAP model misclassifies valid findings as REJECTED | GOAL alignment serves as second gate. Tickets that survive triage get semantic evaluation. False negatives at triage are acceptable if rate is low; false positives (letting garbage through to GOAL) waste alignment tokens. Optimize for precision over recall at triage. |
| Removing goal_steering creates priority vacuum | Per-ticket GOAL alignment replaces portfolio-level signaling. Scheduler V2 handles slot allocation via DispatchGate. No separate priority layer needed. |\n| RL reward latency for downstream outcomes | Dense intermediate rewards (triage pass = 0.3, goal pass = 0.5) provide fast feedback. Terminal outcome retroactively adjusts Q-value. |\n| Workers don't pick up tickets after state redesign | All dispatch functions wired through `scheduler_v2.DispatchGate.try_dispatch()`. Scheduler V2 owns concurrency, claims, and stagger. State machine redesign doesn't affect dispatch mechanics. |\n| `rework_target_state()` routes to eliminated states | Rewritten in Phase 1 step 15 to always return IMPLEMENT or DEFERRED. Tested by `test_rework_target_returns_implement`. |\n| `TicketStore.add()` crashes on new terminal fingerprints | Exclusion list expanded in Phase 1 step 16 to include all new terminal/parked states. |\n| Two-hop triage transitions create race conditions | Single direct transitions from DISCOVERED to terminal states. No intermediate TRIAGED state for rejected tickets. |\n| Legacy + new dispatch running simultaneously | Phase 1 step 20 requires removing or feature-flagging all legacy dispatch calls before enabling scheduler_v2.tick(). |\n| CHEAP model batch leaves 19 tickets unclaimed | Replaced with individual dispatch per ticket through the gate. Stagger throttles naturally. |\n| Active work index polluted by transient TRIAGED entries | Index adds at GOAL state, not TRIAGED. Cleanup on startup prevents stale crash-recovery entries. |\n| Resolved index grows unbounded | Size cap + TTL eviction added in Phase 1 step 22. |\n| State enum rename breaks dozens of references | Full codebase audit required in Phase 1 step 17. Backward-compatible aliases during drain period. |\n| `record_commit()` lost when VERIFYING eliminated | Gatekeeper merge in Phase 1 step 21 explicitly calls record_commit after gate pass. Tested by `test_record_commit_called_on_complete`. |
