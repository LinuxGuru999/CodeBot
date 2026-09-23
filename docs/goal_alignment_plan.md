# Implementation Plan: Goal Alignment in the CodeBot Ticket Lifecycle

Status: PLAN — no code modified.
Date: 2026-09-22
Author: Sisyphus

---

## A. Current Architecture

### Discovery Exit Path

1. Discovery agents (bug_hunter, security_auditor, architecture_auditor, performance_auditor, test_gap_auditor, documentation_auditor, dependency_auditor, ux_auditor, feature_hunter) produce structured `DiscoveryFinding` objects (`codebot/discovery_finding.py`, lines 246-465).
2. Findings are validated via `finding_from_agent_output()` (line 529), which repairs trivial malformations and enforces required fields.
3. Validated findings are written to `.codebot/state/findings.jsonl` via `findings_log.py` (append-only JSONL, line 36).
4. Discovery agents call `create_ticket()` → `TicketStore.add()` which inserts at `DISCOVERED` state (`ticket_engine.py`, line 949).

### Current Ticket States

```
DISCOVERED → VALIDATING → TRIAGED → READY → DECOMPOSE → PLANNING → IMPLEMENTATION_READY → IMPLEMENTING → REVIEWING → VERIFYING → COMPLETE
```

Side states: `BLOCKED`, `REWORK`, `REJECTED`, `DUPLICATE`, `DEFERRED`.
Terminal states: `COMPLETE`, `REJECTED`, `DUPLICATE` (no outgoing transitions, `ticket_engine.py` lines 197-199).

TRANSITIONS dict at `ticket_engine.py` lines 124-199 governs all valid moves.

### Decomposition Entry Point

`dispatch_triage_agents()` (`ticket_dispatcher.py`, line 1082) handles:
- `DISCOVERED` → spawns ticket_triager agent for validation/dedup/severity
- `VALIDATING` → transitions to `TRIAGED` (line 1191)
- `TRIAGED` → transitions to `READY` (line 1186)

`dispatch_decompose_agents()` (line 1274) picks up `READY` tickets and dispatches decomposer agents.

The critical handoff is: **TRIAGED → READY** (line 1186). After this, the ticket enters expensive downstream work (DECOMPOSE → PLANNING → IMPLEMENTING → REVIEWING). This is the insertion point.

### Scheduler Integration

`adaptive_scheduler.py` allocates 30 worker slots based on queue pressure. It calls dispatcher functions per role category. It does not reason about ticket content — only state counts and pressure metrics.

### Relevant Schemas/Classes/Functions/Files

| File | Role |
|------|------|
| `codebot/ticket_engine.py` | Ticket dataclass (line 203), TicketState enum (line 52), TRANSITIONS (line 124), TicketStore (line 406) |
| `codebot/discovery_finding.py` | DiscoveryFinding schema, validation, fingerprinting |
| `codebot/findings_log.py` | Append-only findings.jsonl |
| `codebot/ticket_dispatcher.py` | dispatch_triage_agents (1082), dispatch_decompose_agents (1274), advance_reviewed_tickets (1664), claim management |
| `codebot/role_registry.py` | AgentRole definitions (31 roles), ALL_ROLES list (line 461), ROLE_REGISTRY dict (line 462) |
| `codebot/roles/ticket_triager.md` | Triage agent prompt |
| `codebot/roles/goal_steering.md` | Existing portfolio-level priority signaling (NOT per-ticket alignment) |
| `codebot/adaptive_scheduler.py` | 30-slot worker allocation |
| `codebot/botop.py` | Status CLI |
| `.codebot/project.yaml` | Project contract (structure, paths, autonomy config) |
| `.codebot/constitution.md` | Protected invariants |
| `docs/GOALS.md` | Roadmap-derived goals |

---

## B. Proposed Architecture

### New Lifecycle

```
DISCOVERY
    ↓
DISCOVERED (existing)
    ↓
TRIAGED (existing — evidence validation + dedup)
    ↓
ALIGNED (NEW — goal alignment decision)
    ├── NOW → READY (immediate transition, enters existing pipeline)
    ├── LATER → parked (no downstream consumption)
    └── NEVER → parked (historical, auditable)

NOW path continues unchanged:
READY → DECOMPOSE → PLANNING → IMPLEMENTATION_READY → IMPLEMENTING → REVIEWING → VERIFYING → COMPLETE
```

### Where Goal Alignment Fits

Goal alignment runs **after triage validates the finding is real** but **before the ticket enters READY/DECOMPOSE**. Rationale:

- Triage answers: "Is this finding real, unique, and correctly classified?"
- Goal alignment answers: "Does this real finding deserve engineering resources now?"
- Running alignment before triage wastes LLM calls on hallucinated/duplicate findings.
- Running alignment after READY wastes decomposition/planning on work that should be deferred.

### Insertion Point

In `dispatch_triage_agents()` (`ticket_dispatcher.py`, line 1185-1187):

```python
# CURRENT:
if current_state_val == "TRIAGED":
    transitions.append((tid, TicketState.READY, None))

# PROPOSED:
if current_state_val == "TRIAGED":
    transitions.append((tid, TicketState.ALIGNED, None))
```

Then a new `dispatch_alignment_agents()` function processes `ALIGNED` tickets, writing alignment decisions and transitioning to `READY` (NOW), or parking as `LATER`/`NEVER`.

### Relationship to Existing goal_steering.md

The existing `goal_steering` role writes portfolio-level P0-P3 directives to `goal_steering.status.json`. It steers **which categories of work get priority** among active tickets. It does NOT classify individual tickets as NOW/LATER/NEVER.

The new goal-alignment stage is complementary:
- **Goal alignment** (new): Per-ticket semantic gate — should this ticket enter the pipeline at all?
- **Goal steering** (existing): Portfolio-level signal — among active tickets, which categories matter most right now?

They share the same project intent source but operate at different granularities.

---

## C. State / Schema Changes

### Recommendation: Option C (Hybrid)

After evaluating all three options against the codebase:

| Option | Pros | Cons |
|--------|------|------|
| A: Full states (GOAL_ALIGNMENT, NOW, LATER, NEVER) | Explicit in state machine | Adds 4 states to TRANSITIONS, pollutes scheduler counting, NOW is transient |
| B: Metadata field only | No state machine changes | LATER/NEVER tickets still sit in DISCOVERED/TRIAGED, scheduler might pick them up |
| **C: Hybrid (ALIGNED state + disposition metadata)** | **One new state, clear semantics, minimal disruption** | Requires one migration |

**Decision: Option C.**

- One new state: `ALIGNED` (between TRIAGED and READY)
- One new metadata field: `goal_disposition` = `NOW | LATER | NEVER | PENDING`
- `NOW` is not a state — it's a disposition that immediately transitions `ALIGNED → READY`
- `LATER` and `NEVER` are dispositions that park the ticket at `ALIGNED` state permanently until reassessed
- The scheduler never sees LATER/NEVER because they don't reach READY

### New TicketState Enum Value

File: `codebot/ticket_engine.py`, line 52-69

```python
class TicketState(str, Enum):
    # ... existing ...
    ALIGNED = "ALIGNED"  # NEW: between TRIAGED and READY
```

### Updated TRANSITIONS

File: `codebot/ticket_engine.py`, lines 124-199

```python
TicketState.TRIAGED: frozenset({
    TicketState.ALIGNED,     # CHANGED: was READY
    TicketState.DEFERRED,
    TicketState.REJECTED,
}),
TicketState.ALIGNED: frozenset({   # NEW
    TicketState.READY,             # NOW disposition
    TicketState.REJECTED,          # NEVER disposition (auditable)
    TicketState.DEFERRED,          # LATER disposition
}),
TicketState.READY: frozenset({     # UNCHANGED
    TicketState.DECOMPOSE,
    TicketState.PLANNING,
    TicketState.IMPLEMENTATION_READY,
    TicketState.DEFERRED,
}),
```

LATER maps to `DEFERRED` (already exists, already has transitions back to READY/TRIAGED/DECOMPOSE at line 191-195). This avoids adding a new state while leveraging existing infrastructure.

NEVER maps to `REJECTED` (terminal state, already exists). The `goal_disposition` field distinguishes NEVER-rejected from other rejection reasons.

### New Ticket Fields

File: `codebot/ticket_engine.py`, lines 203-247

Add to the `Ticket` frozen dataclass:

```python
goal_disposition: str = ""          # "NOW" | "LATER" | "NEVER" | "PENDING" | ""
goal_reason: str = ""               # Brief justification from alignment agent
goal_relevant_to: str = ""          # Which goal/milestone this serves
goal_revision: int = 0              # Which version of project goals produced this decision
goal_aligned_at: float = 0.0        # Timestamp of alignment decision
goal_reconsider_when: str = ""      # For LATER: condition trigger for reassessment
```

All fields have defaults (empty string / 0 / 0.0) so existing tickets deserialize without migration errors — `Ticket.from_dict()` filters to valid dataclass fields (line 296-298).

### Schema Version Bump

File: `codebot/ticket_engine.py`, line 49

```python
SCHEMA_VERSION = "2.1"  # was "2.0"
```

### Migration Implications

- Existing tickets (schema 2.0) load fine: new fields default to empty/zero via `from_dict()` filtering (line 296-298).
- Tickets currently in TRIAGED state will flow to ALIGNED on next dispatch cycle.
- Tickets already past TRIAGED (in READY, DECOMPOSE, etc.) are unaffected — they bypass alignment.
- No bulk migration script needed for phase 1 rollout.

---

## D. Goal Context

### What Already Exists

| Source | Location | Content | Machine-readable? |
|--------|----------|---------|-------------------|
| Project contract | `.codebot/project.yaml` | Name, description, architecture, testing, dependencies, security, autonomy level | Yes (YAML) |
| Goals/Roadmap | `docs/GOALS.md` | Purpose, quality definition, success criteria, milestones | Prose (not parseable) |
| Constitution | `.codebot/constitution.md` | Protected invariants, security boundaries, human approval requirements | Prose |
| Product docs | `docs/PRODUCT.md` | Product vision | Prose |
| Goal steering status | `.codebot/state/goal_steering.status.json` | Current P0-P3 directive | Yes (JSON) |

### What's Missing

No canonical, compact, machine-readable project intent artifact exists that an LLM can consume in a small context window. `GOALS.md` is 502 lines of prose. `project.yaml` has structure but no goals/non-goals/priorities.

### Proposed: `.codebot/project_intent.yaml`

A new file alongside `project.yaml`, containing only what goal alignment needs:

```yaml
schema_version: "1.0"
goal_revision: 1

purpose: >
  Autonomous software engineering platform managing its own codebase.

current_goals:
  - "Stabilize security audit pipeline (contract_tests passing)"
  - "Reduce rework rate below 30%"
  - "Achieve 100% test coverage on new implementations"

current_milestone: "v0.3.0-security-hardening"

current_priorities:
  - "Security vulnerability fixes"
  - "Test infrastructure reliability"
  - "Rework loop reduction"

non_goals:
  - "Multi-language support beyond Python"
  - "Cloud-provider-specific integrations"
  - "GUI/dashboard development"
  - "Third-party dependency expansion"

constraints:
  - "stdlib-only policy"
  - "Single-process manager"
  - "30-slot worker pool maximum"
  - "No external network access for implementation agents"

user_requested_outcomes: []
```

### Canonical Source Rules

- `project_intent.yaml` is the authoritative goal context for alignment decisions.
- `GOALS.md` remains the human-facing roadmap; `project_intent.yaml` is the machine-facing distillation.
- When goals change, both files update together. `goal_revision` increments.
- The alignment agent reads ONLY `project_intent.yaml`, never `GOALS.md` (keeps context small).
- User-requested outcomes are appended to `user_requested_outcomes` list when received.

### Cross-Project Compatibility

For CodeBot developing another application, the target project provides its own `.codebot/project_intent.yaml`. The mechanism is identical — no CodeBot-specific logic hardcoded. The file is project-specific configuration, like `project.yaml`.

---

## E. Goal-Alignment Agent

### Role Definition

- **Name**: `goal_aligner`
- **Category**: Planning (added to `PLANNING_ROLES` in `role_registry.py`)
- **Codename**: Compass
- **Model profile**: `ReasoningLevel.MEDIUM`, `CodingLevel.BASIC`, `ContextSize.SMALL`, `CostClass.CHEAP`, `LatencyClass.BACKGROUND`
  - Rationale: Semantic classification task, no code generation, small context. Cheap model sufficient.
- **Tool policy**: `READ_ONLY_TOOLS | frozenset({"write"})`, `READ_ONLY_COMMANDS`, `filesystem_scope="state_dir"`
  - Can read tickets, project_intent.yaml. Can write alignment records. Cannot touch source code.

### Prompt Inputs (Compact Packet)

The agent receives exactly this context per ticket (or batch of tickets):

```json
{
  "project_purpose": "...",
  "current_goals": ["..."],
  "current_milestone": "...",
  "non_goals": ["..."],
  "constraints": ["..."],
  "user_requested_outcomes": ["..."],
  "goal_revision": 1,
  "tickets": [
    {
      "ticket_id": "CB-xxx",
      "discovery_category": "security",
      "title": "...",
      "problem_statement": "...",
      "severity": "high",
      "confidence": "high",
      "affected_modules": ["..."],
      "scope_estimate": "small",
      "source": "security_auditor",
      "is_user_requested": false
    }
  ]
}
```

Total context: ~500-800 tokens per ticket. Batch up to 10 tickets per invocation.

### Structured Output

```json
{
  "decisions": [
    {
      "ticket_id": "CB-xxx",
      "decision": "NOW",
      "reason": "Directly addresses current milestone security hardening goal",
      "goal_relevant_to": "Stabilize security audit pipeline",
      "beneficiary": "security posture",
      "reconsider_when": null,
      "maintenance_cost_note": "Low — single file change, no new dependencies"
    }
  ]
}
```

### Central Prompt Instruction

> A finding may be technically correct, useful, and well-designed and still belong in LATER or NEVER. Technical merit alone does not justify current engineering expenditure. Your job is not to evaluate whether something is good — it is to evaluate whether it deserves engineering resources RIGHT NOW given the project's current goals, milestone, and constraints.

### Reasoning Framework (Qualitative, Not Numeric)

The prompt instructs the agent to answer these questions in order:

1. Does this advance a current goal listed in project intent?
2. Does this remove a current blocker or meaningful risk?
3. Does a current user or workflow need it?
4. Is it required for the current milestone?
5. What happens if this is not implemented now?
6. What permanent complexity would implementing it add?
7. Is this explicitly outside current scope (non-goals)?

Then classify:
- If 1-4 yield yes → NOW
- If 5 yields "nothing urgent" and 7 yields no → LATER
- If 7 yields yes → NEVER

### Fast-Path Decisions (Deterministic, No LLM Call)

These bypass the alignment agent entirely, applied in platform code before dispatch:

| Condition | Disposition | Justification |
|-----------|-------------|---------------|
| `source` contains "user_request" or ticket referenced in `user_requested_outcomes` | NOW | Human intent has priority (§14) |
| `discovery_category` == "security" AND `severity` == "critical" | NOW | Critical security = always actionable (§16) |
| Ticket is a REWORK transition (from `advance_reviewed_tickets`) | BYPASS | Already committed to parent work (§15) |
| `discovery_category` == "bug" AND `severity` == "critical" AND evidence includes failing test | NOW | Build-breaking bugs block everything |
| Fingerprint matches a previous NEVER decision with same goal_revision | NEVER | Suppress rediscovery (§22) |

Conservative by design: only unambiguous cases fast-path. Everything else goes through the agent.

### User-Requested Work Protection

User requests are tagged with `source: "user_request"` at creation time. The fast-path rule above ensures they receive NOW immediately. The alignment agent is also instructed:

> User-requested work must receive NOW unless it is a duplicate, already completed, impossible under current constraints, or explicitly superseded by a newer user request. Do not demote user-requested work because autonomous priorities disagree.

### Failure Handling

If the alignment agent fails (timeout, invalid JSON, crash):

1. Retry once with the same input.
2. If retry fails, set `goal_disposition = "PENDING"` and leave ticket in `ALIGNED` state.
3. Do NOT default to NOW (creates bloat) or NEVER (loses work).
4. Log failure to `alignment_failures.jsonl` for operator visibility.
5. PENDING tickets are retried on the next dispatch cycle.

### Missing Goal Context

If `.codebot/project_intent.yaml` doesn't exist or is empty:

- `bugs` + `security` + `test` (for existing tests) → NOW
- `feature` + `refactor` + `architecture` + `documentation` → LATER
- Log warning: "project_intent.yaml missing — using conservative defaults"

This ensures the system degrades gracefully for new projects that haven't defined goals yet.

---

## F. LATER Behavior

### Storage

LATER tickets remain in the ticket store at `ALIGNED` state with `goal_disposition = "LATER"`. They are NOT moved to a separate file or database. The existing `TicketStore._state_index` groups them naturally under `ALIGNED`.

### Inactivity Guarantee

LATER tickets must not enter DECOMPOSE, PLANNING, IMPLEMENTING, or REVIEWING. This is enforced structurally:

- `ALIGNED` state has no transition to `DECOMPOSE` or `PLANNING` in the TRANSITIONS dict.
- The only forward transition from `ALIGNED` is to `READY` (for NOW promotion) or `REJECTED` (for NEVER).
- `dispatch_decompose_agents()` only queries `READY` tickets (line 1274+), never `ALIGNED`.
- The scheduler's slot allocation counts `ALIGNED` tickets separately from actionable work.

### Candidate Ordering for Promotion

When promotion is triggered, LATER candidates are sorted by:

1. `severity` (critical > high > medium > low)
2. `goal_relevant_to` match strength (direct goal match > indirect)
3. `confidence` (high > medium > low)
4. `ticket_id` (deterministic tiebreaker)

NOT FIFO. Age alone does not promote irrelevant work.

### Promotion Trigger

Promotion activates when **actionable NOW supply is depleted**, defined as:

```
count(IMPLEMENTATION_READY) == 0 AND count(READY) == 0 AND count(DECOMPOSE) == 0 AND count(PLANNING) == 0
```

This means no work is available for implementers, decomposers, or planners. It does NOT mean total ticket count is zero — there may be thousands of DISCOVERED or LATER tickets.

Checked in `adaptive_scheduler.py` during each tick, or in a new `promote_later_tickets()` function called by the orchestrator health loop.

### Small-Batch Promotion

When triggered:
1. Select top N LATER candidates (default: 10, configurable via `project_intent.yaml`).
2. Reassess each against CURRENT project intent (goals may have changed since original alignment).
3. Apply alignment decision again (fast-path or agent call).
4. Only tickets that receive NOW disposition transition `ALIGNED → READY`.
5. Remainder stay `ALIGNED` with `goal_disposition = "LATER"`.
6. Update `goal_revision` on reassessed tickets.

### Reassessment Before Promotion

Every LATER ticket is rechecked before promotion. The reassessment evaluates:

- Does the issue still exist in the current repository state?
- Has it been fixed by other work incidentally?
- Is it now a duplicate of an active ticket?
- Do current goals now support it?
- Has the non-goals list changed?

Possible outcomes: NOW, LATER (stays), NEVER (transitions to REJECTED), or DUPLICATE.

---

## G. NEVER Behavior

### Archival

NEVER tickets transition `ALIGNED → REJECTED` with `goal_disposition = "NEVER"`. They remain in `tickets.json` permanently. They are NOT deleted. The `goal_reason` field captures why.

### Duplicate Suppression

Before creating a new ticket from a discovery finding, the system checks:

1. Compute finding fingerprint (already done in `discovery_finding.py`, line 280).
2. Search existing tickets for matching fingerprint where `goal_disposition == "NEVER"`.
3. If found AND `goal_revision` hasn't changed since the NEVER decision → suppress the finding, don't create a ticket.
4. If found BUT `goal_revision` has changed → allow creation (goals shifted, reconsideration warranted).

This check happens in the discovery-to-ticket creation path, likely in `telemetry.py` (line 189: `store.add(ticket)`) or wherever findings are converted to tickets.

### Reconsideration After Major Goal Changes

When `goal_revision` increments (project goals materially change):

1. Query all NEVER tickets where `goal_revision < current_revision`.
2. Do NOT automatically promote any of them.
3. On next LATER promotion cycle, include a bounded sample of stale NEVER tickets (e.g., 5) in the reassessment batch.
4. If a NEVER ticket now aligns with new goals, it receives NOW and transitions to READY.
5. If still misaligned, update its `goal_revision` to current and keep as NEVER.

---

## H. Integration Points

Exact files and functions requiring modification:

| File | Function/Location | Change |
|------|-------------------|--------|
| `codebot/ticket_engine.py:52-69` | `TicketState` enum | Add `ALIGNED = "ALIGNED"` |
| `codebot/ticket_engine.py:124-199` | `TRANSITIONS` dict | Update TRIAGED→ALIGNED, add ALIGNED→{READY,REJECTED,DEFERRED} |
| `codebot/ticket_engine.py:203-247` | `Ticket` dataclass | Add 6 goal_* fields with defaults |
| `codebot/ticket_engine.py:49` | `SCHEMA_VERSION` | Bump to "2.1" |
| `codebot/ticket_dispatcher.py:1082` | `dispatch_triage_agents()` | Change TRIAGED→READY to TRIAGED→ALIGNED (line 1186) |
| `codebot/ticket_dispatcher.py:new` | `dispatch_alignment_agents()` | New function: process ALIGNED tickets, apply fast-paths, spawn aligner agents, write decisions, transition NOW→READY |
| `codebot/ticket_dispatcher.py:76-78` | `TRIAGER_ROLE_NAMES` | Add `ALIGNER_ROLE_NAMES` frozenset |
| `codebot/role_registry.py:441-458` | `PLANNING_ROLES` | Add `goal_aligner` AgentRole |
| `codebot/role_registry.py:461-462` | `ALL_ROLES`, `ROLE_REGISTRY` | Automatically includes new role |
| `codebot/roles/goal_aligner.md` | New file | Agent prompt template |
| `codebot/adaptive_scheduler.py:tick()` | Scheduler tick | Add alignment dispatch call, add LATER promotion trigger check |
| `codebot/discovery_manager.py` | Discovery pipeline | Add NEVER-suppression check before ticket creation |
| `codebot/botop.py` | Status reporting | Add ALIGNED/LATER/NEVER counts to dashboard |
| `codebot/findings_log.py` | Findings processing | Integrate NEVER fingerprint suppression |
| `.codebot/project_intent.yaml` | New file | Canonical project goal context |
| `codebot/ticket_dispatcher.py:2231` | DEFERRED recovery | Ensure LATER (stored as DEFERRED disposition) isn't accidentally recovered by existing DEFERRED→READY logic |

---

## I. Tests

### Unit Tests

| Test | Validates |
|------|-----------|
| `test_aligned_state_exists` | TicketState.ALIGNED is valid enum value |
| `test_transitions_triaged_to_aligned` | TRIAGED→ALIGNED is allowed |
| `test_transitions_aligned_to_ready` | ALIGNED→READY is allowed (NOW) |
| `test_transitions_aligned_to_rejected` | ALIGNED→REJECTED is allowed (NEVER) |
| `test_transitions_aligned_to_deferred` | ALIGNED→DEFERRED is allowed (LATER) |
| `test_no_transition_aligned_to_decompose` | ALIGNED→DECOMPOSE raises ValueError |
| `test_no_transition_aligned_to_planning` | ALIGNED→PLANNING raises ValueError |
| `test_goal_fields_default_empty` | New fields serialize/deserialize with defaults |
| `test_schema_version_2_1` | SCHEMA_VERSION updated |
| `test_fast_path_user_request` | User-requested ticket gets NOW without agent call |
| `test_fast_path_critical_security` | Critical security gets NOW without agent call |
| `test_fast_path_rework_bypass` | Rework tickets skip alignment entirely |
| `test_fast_path_never_suppression` | Fingerprint-matched NEVER ticket is suppressed |
| `test_alignment_decision_parse` | JSON output parsed into goal_* fields correctly |
| `test_alignment_failure_pending` | Failed alignment sets PENDING, not NOW or NEVER |

### Integration Tests

| Test | Validates |
|------|-----------|
| `test_discovery_to_aligned_flow` | Full path: finding → ticket → DISCOVERED → TRIAGED → ALIGNED |
| `test_now_enters_ready` | NOW disposition transitions ALIGNED → READY |
| `test_later_stays_aligned` | LATER disposition keeps ticket at ALIGNED, not dispatched |
| `test_never_reaches_rejected` | NEVER disposition transitions ALIGNED → REJECTED |
| `test_later_not_in_decompose_queue` | dispatch_decompose_agents() ignores ALIGNED tickets |
| `test_promotion_trigger` | Empty NOW queue triggers LATER reassessment |
| `test_promotion_batch_limit` | At most N tickets promoted per cycle |
| `test_reassessment_updates_revision` | Promoted tickets get current goal_revision |

### Lifecycle Tests

| Test | Validates |
|------|-----------|
| `test_now_case_user_request` | User feature request → NOW |
| `test_now_case_milestone_blocker` | Bug blocking current milestone → NOW |
| `test_now_case_active_bug` | Confirmed correctness bug → NOW |
| `test_now_case_critical_security` | Exploitable vulnerability → NOW |
| `test_later_case_future_enhancement` | Useful but not current goal → LATER |
| `test_later_case_nonurgent_optimization` | Performance improvement with no current impact → LATER |
| `test_later_case_architecture_cleanup` | Refactor with no current cost → LATER |
| `test_never_case_explicit_non_goal` | Feature in non-goals list → NEVER |
| `test_never_case_out_of_scope` | Outside product purpose → NEVER |
| `test_never_case_conflicting_direction` | Contradicts architectural constraints → NEVER |
| `test_never_case_repeated_rejection` | Previously rejected idea resubmitted → NEVER |

### Goal Change Tests

| Test | Validates |
|------|-----------|
| `test_later_becomes_now_on_goal_change` | LATER ticket relevant to new goal → NOW on reassessment |
| `test_later_resolved_by_repo_change` | Issue fixed incidentally → DUPLICATE/REJECTED on reassessment |
| `test_later_becomes_never_on_scope_narrow` | Scope narrows → NEVER on reassessment |

### Empty-NOW Promotion Tests

| Test | Validates |
|------|-----------|
| `test_zero_now_triggers_promotion` | No actionable work → LATER batch selected |
| `test_only_small_batch_promoted` | 100 LATER tickets → only 10 reassessed |
| `test_invalid_items_stay_later` | Stale LATER tickets remain parked |
| `test_no_flood_into_decompose` | Promoted tickets enter READY, not directly DECOMPOSE |

### Non-Empty NOW Tests

| Test | Validates |
|------|-----------|
| `test_active_now_prevents_promotion` | NOW queue has items → LATER not touched |

### Cost Tests

| Test | Validates |
|------|-----------|
| `test_later_no_decomposer_call` | LATER ticket never invokes decomposer agent |
| `test_later_no_planner_call` | LATER ticket never invokes planner agent |
| `test_later_no_implementer_call` | LATER ticket never invokes implementer agent |
| `test_never_no_downstream_agents` | NEVER ticket consumes zero downstream compute |

### Migration Tests

| Test | Validates |
|------|-----------|
| `test_legacy_ticket_loads_without_goal_fields` | Schema 2.0 tickets deserialize with empty defaults |
| `test_legacy_triaged_flows_to_aligned` | Existing TRIAGED tickets transition to ALIGNED on next cycle |
| `test_legacy_ready_unaffected` | Tickets already past TRIAGED continue normally |

---

## J. Migration

### Rollout Strategy (Phased)

**Phase 1: Infrastructure (no behavioral change)**
1. Add `ALIGNED` state to `TicketState` enum and `TRANSITIONS`.
2. Add `goal_*` fields to `Ticket` dataclass with defaults.
3. Bump `SCHEMA_VERSION` to "2.1".
4. Create `.codebot/project_intent.yaml` template.
5. Register `goal_aligner` role in `role_registry.py`.
6. Write `codebot/roles/goal_aligner.md` prompt.
7. All existing tickets continue flowing unchanged (new fields are empty defaults).

**Phase 2: Routing (behavioral change, new tickets only)**
1. Modify `dispatch_triage_agents()` to route TRIAGED → ALIGNED instead of TRIAGED → READY.
2. Implement `dispatch_alignment_agents()` with fast-path rules only (no LLM calls yet).
3. Fast-paths handle: user requests → NOW, critical security → NOW, rework → BYPASS, everything else → NOW (temporary fallback).
4. Verify no regressions in ticket flow.

**Phase 3: LLM Alignment (full behavior)**
1. Replace "everything else → NOW" fallback with actual `goal_aligner` agent dispatch.
2. Implement LATER/NEVER disposition handling.
3. Ensure LATER/NEVER tickets cannot enter downstream buckets.
4. Add NEVER fingerprint suppression to discovery pipeline.

**Phase 4: LATER Promotion**
1. Implement promotion trigger (empty NOW queue detection).
2. Implement small-batch reassessment.
3. Add goal_revision tracking.

**Phase 5: Observability**
1. Add alignment stats to `botop.py` status output.
2. Create `scripts/track_lifecycle.py --view alignment` integration.
3. Add alignment metrics to `scheduler_metrics.py`.

### Legacy Ticket Handling

- **Tickets already past TRIAGED** (in READY, DECOMPOSE, PLANNING, etc.): Unaffected. They continue through existing pipeline. No retroactive alignment.
- **Tickets currently in TRIAGED**: Will flow to ALIGNED on next dispatch cycle. Safe — they haven't entered expensive stages yet.
- **Tickets in DISCOVERED/VALIDATING**: Flow normally through triage, then hit ALIGNED.
- **Bulk backlog (1004 in PLANNING, 839 in DISCOVERED)**: Do NOT push through alignment retroactively. That would generate thousands of LLM calls for tickets already queued. Let them drain naturally. Only newly discovered tickets go through alignment.

---

## K. Observability

### botop.py Additions

Add to the status dashboard:

```
Goal Alignment:
  Pending alignment: 12
  NOW (this cycle): 8
  LATER (parked): 342
  NEVER (rejected): 47
  Promoted from LATER: 3
  Alignment failures: 1
```

### Metrics (New File: `codebot/alignment_metrics.py`)

Tracked in `.codebot/state/alignment_metrics.jsonl`:

```json
{"timestamp": 0, "total_evaluated": 10, "now_count": 3, "later_count": 5, "never_count": 2, "fast_path_count": 4, "agent_path_count": 6, "by_discovery_role": {"bug_hunter": {"evaluated": 4, "now": 3, "later": 1, "never": 0}, "feature_hunter": {"evaluated": 3, "now": 0, "later": 1, "never": 2}}, "promotions": 0, "suppressions": 1, "failures": 0, "goal_revision": 1}
```

### Diagnostic Queries

For any ticket, operators can answer:

```bash
python3 scripts/track_lifecycle.py --view pipeline --ticket-ids CB-xxx
```

Plus direct inspection:
```python
ticket.goal_disposition  # NOW / LATER / NEVER
ticket.goal_reason       # Why
ticket.goal_relevant_to  # Which goal
ticket.goal_revision     # Which goal version
ticket.goal_aligned_at   # When
ticket.goal_reconsider_when  # Reassessment trigger
```

### track_lifecycle.py Integration

Extend the existing `scripts/track_lifecycle.py` with:
- `--view alignment`: Show alignment decision distribution, by class, by discovery role
- `--view later`: Show parked LATER tickets ordered by promotion priority
- `--view never`: Show NEVER decisions with reasons for audit

---

## L. Risks

| Risk | Mitigation |
|------|------------|
| **New bottleneck**: Alignment agent adds latency between TRIAGED and READY | Use cheap model, small context, batch evaluation (10 tickets/call), deterministic fast-paths for common cases. Target <5s per batch. |
| **Incorrect suppression**: Valid work classified NEVER incorrectly | NEVER requires explicit reasoning. NEVER tickets remain auditable. Goal revision changes trigger reconsideration. Human override always possible. |
| **Goal-context drift**: `project_intent.yaml` becomes stale | `goal_revision` tracks staleness. Alignment decisions record which revision they used. Operator alert when revision age exceeds threshold. |
| **Rediscovery loops**: Discovery agents regenerate NEVER findings | Fingerprint-based suppression checked before ticket creation. Only bypassed when goal_revision changes. |
| **LATER accumulation**: Thousands of LATER tickets pile up | Promotion batch limit (10). Reassessment can move stale LATER → NEVER. Periodic pruning of LATER tickets older than N goal revisions. |
| **Race conditions**: Concurrent alignment + triage dispatch | Claim system already prevents double-processing. Alignment uses same claim pattern as triage. |
| **Scheduler confusion**: New ALIGNED state breaks slot counting | Scheduler counts ALIGNED separately from actionable work. ALIGNED tickets don't appear in READY/DECOMPOSE/PLANNING queues. |
| **Cost overhead**: LLM calls for every discovered ticket | Fast-paths eliminate LLM calls for user requests, critical security, rework, and NEVER suppressions. Batch evaluation reduces per-ticket cost. Cheap model profile. |

---

## M. Simplification Check

### Why This Is Minimum Complexity

1. **One new state** (`ALIGNED`), not four. NOW/LATER/NEVER are metadata dispositions, not states. This minimizes TRANSITIONS changes and scheduler impact.

2. **One new role** (`goal_aligner`), one prompt file, one dispatch function. No new managers, controllers, or scheduling layers.

3. **Leverages existing DEFERRED/REJECTED states** for LATER/NEVER storage rather than inventing parallel infrastructure.

4. **Fast-paths eliminate most LLM calls**. Only genuinely ambiguous tickets reach the agent. User requests, critical security, and rework — the highest-volume categories — are handled deterministically.

5. **No scheduler modifications** beyond adding one dispatch call. The scheduler remains ignorant of product strategy. It counts states and allocates slots. Goal alignment decides WHAT enters those slots.

6. **One config file** (`project_intent.yaml`) for goal context. Not a database, not a version control system, not a complex hierarchy. YAML, versioned by integer revision.

7. **Batch evaluation** (10 tickets per agent call) rather than 1:1 ratio. Reduces LLM cost by 10x for alignment.

8. **Phased rollout** means Phase 1-2 add zero runtime cost. Infrastructure lands first, behavior changes incrementally.

9. **Frozen dataclass with defaults** means zero migration scripts. Old tickets load fine. New fields are empty until populated.

10. **Separation of concerns maintained**: Discovery finds. Triage validates. Alignment steers. Scheduler allocates. Agents implement. Each layer knows nothing about the others' internal logic.

---

## Answers to Final Questions (§60)

1. **Where does discovery hand off?** Discovery agents write findings to `findings.jsonl` and create tickets at `DISCOVERED` state via `TicketStore.add()`. `dispatch_triage_agents()` picks them up.

2. **Smallest insertion change?** Modify line 1186 of `ticket_dispatcher.py`: `TRIAGED → ALIGNED` instead of `TRIAGED → READY`. Add `dispatch_alignment_agents()` to process ALIGNED tickets.

3. **States vs metadata?** Hybrid. One new state (`ALIGNED`) plus metadata fields (`goal_disposition`, etc.). NOW/LATER/NEVER are dispositions, not states. Minimizes lifecycle complexity.

4. **How do LATER/NEVER avoid downstream agents?** Structural enforcement: ALIGNED has no transition to DECOMPOSE or PLANNING. `dispatch_decompose_agents()` only queries READY. LATER/NEVER tickets never reach READY.

5. **What goal data exists?** `docs/GOALS.md` (prose), `.codebot/project.yaml` (structure), `.codebot/constitution.md` (invariants), `goal_steering.status.json` (priority signals). No compact machine-readable goal context.

6. **What additional goal data is required?** `project_intent.yaml` with: purpose, current_goals, current_milestone, non_goals, constraints, user_requested_outcomes, goal_revision.

7. **How is it kept authoritative?** Single file, versioned by `goal_revision`. Updated alongside `GOALS.md`. Alignment agent reads only this file.

8. **Cross-project compatibility?** `project_intent.yaml` is project-specific config in `.codebot/`. Same mechanism works for CodeBot self-improvement and external projects.

9. **Which categories bypass alignment?** User requests, critical security vulnerabilities, confirmed build-breaking bugs, rework tickets, NEVER-fingerprint suppressions.

10. **How are user requests protected?** Tagged at creation (`source: "user_request"`). Fast-path rule assigns NOW immediately. Agent prompt explicitly forbids demotion.

11. **How is rework protected?** Rework transitions (REVIEWING → REWORK → IMPLEMENTATION_READY) bypass alignment entirely. System already committed to parent work.

12. **Alignment failure handling?** Retry once → set PENDING → retry next cycle. Never default to NOW or NEVER. Logged to `alignment_failures.jsonl`.

13. **LATER reassessment trigger?** Actionable NOW supply depleted: IMPLEMENTATION_READY + READY + DECOMPOSE + PLANNING all equal zero.

14. **How many LATER tickets reconsidered?** Configurable batch (default: 10). Sorted by severity × goal relevance × confidence. Never the entire backlog.

15. **LATER validation before promotion?** Re-run alignment against current goals + check repository state (still exists? already fixed? duplicate?). Full reassessment, not blind promotion.

16. **Stale LATER removal?** During reassessment, stale tickets transition to NEVER or DUPLICATE. Tickets older than N goal revisions without relevance are candidates for NEVER.

17. **NEVER remembered by discovery?** Fingerprint index checked before ticket creation. Matching NEVER fingerprint with same goal_revision suppresses the finding.

18. **Reconsideration after goal changes?** When `goal_revision` increments, stale NEVER tickets become eligible for inclusion in next LATER promotion batch (bounded sample of 5).

19. **Scheduler stays ignorant?** Scheduler counts states and allocates slots. It never reads `goal_disposition` or `project_intent.yaml`. Alignment controls what reaches READY; scheduler controls who works on READY.

20. **Proof of reduced compute?** Metrics track: alignment decisions by role, NOW/LATER/NEVER rates, downstream agent invocations avoided. Compare pre/post: decomposer/planner/implementer calls should decrease proportional to LATER+NEVER rate.

21. **Avoid bottleneck?** Cheap model, small context (~800 tokens), batch evaluation (10/call), deterministic fast-paths for majority. Target <5s per batch.

22. **Safe migration?** Phased rollout. Phase 1 adds fields/states with no behavioral change. Phase 2 routes new tickets only. Legacy backlog drains naturally without retroactive alignment.

23. **Diagnose any ticket's classification?** `goal_disposition`, `goal_reason`, `goal_relevant_to`, `goal_revision`, `goal_aligned_at` stored on ticket. Visible via `track_lifecycle.py` or direct JSON inspection.

24. **Tests preventing LATER/NEVER reaching implementation?** `test_no_transition_aligned_to_decompose`, `test_later_not_in_decompose_queue`, `test_later_no_decomposer_call`, `test_later_no_planner_call`, `test_later_no_implementer_call`. Structural enforcement via TRANSITIONS dict.

25. **Understandable without tracing multiple layers?** Yes. One state, one role, one config file, one dispatch function. The conceptual model fits in the ASCII diagram at the top of this document.
