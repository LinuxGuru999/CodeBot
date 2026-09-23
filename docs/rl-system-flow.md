# CodeBot RL System — Architecture & Flow Reference

> 15 modules · Thompson Sampling bandits · factual event architecture · single-writer credit assignment · dual-mode prompt evolution

---

## 1. System Overview

The RL system is an **optional, non-mutating learning layer** that observes the CodeBot ticket lifecycle and uses reinforcement learning to optimize four decision axes: which model runs an agent, which discovery roles get scheduled, when tickets should proceed through goal alignment, and what instructions agents receive in their prompts.

It replaces the legacy `rl_engine.py` (epsilon-greedy Q-learning, scalar rewards, direct prompt mutation) with a modular architecture built on four principles:

1. **Factual events only**: The append-only event log records *what happened*, never *what it means*. Learning conclusions are derived by a versioned credit assigner, not baked into the source of truth. If attribution logic improves, wipe derived state and replay.
2. **Single-writer credit assignment**: All bandit posterior updates flow through one function (`assign_credits()`) that reads factual events and applies attribution rules. No production code mutates bandit state directly.
3. **Thompson Sampling over epsilon-greedy**: Beta-Bernoulli bandits naturally balance exploration/exploitation without tunable temperature parameters. High-risk tickets skip exploration entirely.
4. **Dual-mode prompt evolution**: Agents improve through append-only hint injection (closed taxonomy templates only, never raw reviewer text) and competitive tournament selection (natural selection across 3 forked variants).

Default policy mode is **OFF**. Four independent policies control behavior separately: `model_selector`, `discovery_allocator`, `goal_advisor`, `prompt_evolution`. An observation gate independently controls whether data collection runs at all.

---

## 2. Architecture Diagram

```mermaid
graph TB
    subgraph Producers["Event Producers"]
        TE[ticket_engine.py]
        MM[model_manager.py]
        HC[health_check_loop.py]
        DM[discovery_manager.py]
    end

    subgraph Ingestion["Factual Event Log"]
        EL[rl_event_log.py<br/>O_APPEND JSONL<br/>schema v2]<br/>
        DL[Dead letters<br/>rl_dead_letters.jsonl]
    end

    subgraph Processing["Pipeline Tick<br/>background thread, fcntl-locked,<br/>observation-gated"]
        OA[rl_outcome_aggregator.py<br/>incremental + semantic dedup]
        RE[rl_reward_engine.py<br/>9-dimension vectors]
        AG[rl_aggregator.py<br/>5 aggregation axes]
        CA[rl_credit_assigner.py<br/>single writer, versioned<br/>attribution rules]
    end

    subgraph Bandits["Thompson Sampling<br/>Beta-Bernoulli"]
        MB[rl_bandit.py<br/>model selection]
        DB[rl_discovery_bandit.py<br/>role allocation]
        GB[rl_goal_bandit.py<br/>NOW/LATER/NEVER]
    end

    subgraph Advisory["Advisory & Policy"]
        AD[rl_advisory.py<br/>delegates to bandit<br/>shadow recommendations]
        PP[rl_policy_promotion.py<br/>outcome-based promotion]
    end

    subgraph Evolution["Prompt Evolution<br/>dual mode, closed taxonomy"]
        PE[rl_prompt_evolution.py<br/>failure→pattern mapping<br/>no raw reviewer text]
        PO[prompt_optimizer.py<br/>consume_triggers<br/>PATTERN_HINTS only]
        RT[rl_tournament.py<br/>A/B/C fork → fitness → promote<br/>ACTIVE-only, versioned backup]
    end

    subgraph Monitoring["Monitoring & Safety"]
        RD[rl_diagnostics.py<br/>full_report + run_pipeline_tick<br/>error tracking]
        PM[rl_project_metrics.py<br/>gaming signal detection]
    end

    TE -->|factual events with attempt_id, transition_id| EL
    MM -->|MODEL_SELECTED| EL
    HC -->|GOAL_DECISION| EL
    DM -->|DISCOVERY_DOWNSTREAM| EL
    EL -->|unknown types| DL

    EL -->|incremental scan, dedup by transition_id| OA
    OA -->|TicketOutcome with 9 fields| RE
    RE -->|reward_vector + confidence| OA
    OA -->|outcomes| AG
    EL -->|events| AG

    EL -->|BANDIT_OUTIMAL, GOAL_DECISION, DISCOVERY_DOWNSTREAM| CA
    CA -->|_record_outcome_internal| MB
    CA -->|_record_goal_outcome_internal| GB
    CA -->|_record_discovery_outcome_internal| DB

    MB -->|select_model thompson_sampling| AD
    DB -->|select_discovery_role| DM
    GB -->|advise_goal_decision| HC

    AD -->|shadow recommendation log| PP
    PP -->|outcome-based evaluation| AD
    AD -->|get_model_recommendation| MM

    OA -->|failure attributions| PE
    RE -->|low reward dimensions| PE
    PE -->|.evolve.json q_values only, no raw text| PO
    PE -->|exclude active tournament roles| RT
    RT -->|fork A/B/C, promote winner| PO

    EL --> RD
    OA --> RD
    RE --> RD
    MB --> RD
    CA --> RD
    OA --> PM
    PM --> RD
```

---

## 3. Core Principle: Facts vs Interpretations

The most important architectural invariant separates **what happened** from **what it means**:

```
AUTHORITATIVE SYSTEM
    │
    ├── IMPLEMENT_STARTED fact (attempt_id, model, actor)
    ├── REVIEW_REWORK fact (failure_origin, failure_type)
    ├── TICKET_COMPLETE fact
    ├── TICKET_TERMINAL fact (terminal_state, failure_origin)
    ├── GOAL_DECISION fact (outcome: NOW/LATER/NEVER)
    ├── DISCOVERY_DOWNSTREAM fact (quality: completed/duplicate/rejected)
    └── REGRESSION_DISCOVERED fact
            │
            ▼
     APPEND-ONLY EVENTS (schema v2, no interpretations)
            │
            ▼
     OUTCOME AGGREGATOR (semantic dedup by transition_id)
            │
            ▼
      CREDIT ASSIGNER (versioned attribution rules)
        - COMPLETE + IMPLEMENTATION_ERROR → model failure
        - DEFERRED + DEPENDENCY_CHANGE → no signal
        - CANCELLED → no signal
        - REJECTED + REVIEW_ERROR → no model penalty
        - GOAL: NOW+COMPLETE=POSITIVE, LATER+pending=UNRESOLVED
            │
            ▼
       REWARD ENGINE (9 dimensions)
            │
            ▼
        BANDITS (single-writer posterior updates)
            │
            ▼
       RECOMMENDATIONS (per-policy mode gates)
```

If attribution logic improves in 6 months: bump `reward_schema_version`, delete derived state, replay the same factual events. The event log never needs migration.

---

## 4. Module Reference

### 4.1 rl_event_log.py
**Role**: Append-only factual event ingestion.

Writes timestamped JSON records to `.codebot/state/rl_events.jsonl` using `os.open(O_WRONLY | O_CREAT | O_APPEND)` for atomic concurrent writes. Schema version 2 includes causal identifiers:

- `attempt_id`: Generated when implementation starts (`{ticket_id}:impl:{uuid[:8]}`). Carried through all events for that attempt.
- `transition_id`: Deterministic `{ticket_id}:v{transition_version}`. Monotonically incrementing per ticket. Enables semantic deduplication.
- `policy_name`, `policy_version`: Which policy governed this event.
- `feature_schema_version`, `reward_schema_version`: For replay compatibility.

30 allowed event types including `TICKET_TERMINAL` (factual terminal state record for credit assignment) and `DISCOVERY_DOWNSTREAM` (factual quality record for discovery bandit). Unknown event types are written to `rl_dead_letters.jsonl` instead of silently dropped.

### 4.2 rl_failure_taxonomy.py
**Role**: Structured failure attribution from reviewer feedback.

11 failure origins × 11 failure types. `extract_attribution_from_reviewer_feedback()` scans reviewer feedback dicts for explicit origin/type fields, falling back to keyword heuristics. Returns structured attribution embedded in event context for downstream credit assignment.

### 4.3 rl_outcome_aggregator.py
**Role**: Per-ticket lifecycle summaries with semantic deduplication.

`aggregate_ticket()` scans events for a ticket, tracking `seen_transitions` set. Events with duplicate `transition_id` values for state-changing event types are counted once. This prevents double-counting when the same authoritative transition emits multiple events.

`TicketOutcome` dataclass carries 9 new fields beyond the original schema: `severity`, `user_requested`, `quality_signals` (dict for deterministic evidence like tests_passed/build_passed), `reopen_count`, `human_overrides`, `exposure_signals` (dict for tests_executed/discovery_scanned/user_accepted).

Incremental rebuild via `.rl_outcome_offset` tracks byte position in the JSONL file.

### 4.4 rl_reward_engine.py
**Role**: Multi-dimensional fitness evaluation.

9 dimensions, each [0.0, 1.0]:

| Dimension | Signal Source | What It Measures |
|-----------|--------------|------------------|
| usefulness | Terminal status | Did the ticket deliver value? |
| correctness | `quality_signals` (tests, build, review, static checks) | Deterministic first-pass quality |
| first_pass_quality | Rework count + attempts | Execution efficiency |
| stability | Regressions + reopens | Post-completion durability |
| cost_efficiency | Total cost vs $5 reference | Token/cost efficiency |
| speed | Completion time vs 1800s reference | Time efficiency |
| implementation_efficiency | Rework + attempts penalty | Code churn reasonableness |
| human_override | `human_overrides` count | Actual manual rescues needed |
| goal_value | Severity(0.3) + goal_alignment(0.2) + user_requested(0.2) + base(0.3) | True business value delivered |

Confidence formula: base 0.3 + time exposure (capped at 0.35 total) + execution evidence (tests_executed +0.1, discovery_scanned +0.05, user_accepted +0.1) - regression penalty (×0.8). Attempts do NOT increase confidence.

### 4.5 rl_credit_assigner.py (NEW)
**Role**: Single writer for all bandit posteriors. Pure function over factual events.

This is the architectural keystone added during remediation. It replaces all direct `record_outcome()` calls from production code.

`assign_credits(state_dir)`:
1. Reads new events since `.rl_credit_offset`
2. Groups by ticket_id
3. Applies versioned attribution rules:
   - **Model bandit**: COMPLETE/RESOLVED → success. DEFERRED+DEPENDENCY_CHANGE → no signal. CANCELLED → no signal. REJECTED+IMPLEMENTATION_ERROR → failure. REJECTED+REVIEW_ERROR → no model penalty. Deduplicates by transition_id.
   - **Goal bandit**: Delayed ternary evaluation. NOW+COMPLETE=POSITIVE. NOW+REJECTED=NEGATIVE. LATER+pending=UNRESOLVED (skipped). NEVER+CANCELLED=POSITIVE. NEVER+COMPLETE=NEGATIVE. Only POSITIVE/NEGATIVE update posteriors.
   - **Discovery bandit**: Quality-aware. completed/actionable=success. duplicate/rejected/not_actionable=failure.
4. Calls `_record_*_internal()` functions (not the deprecated public APIs)
5. Advances offset

Idempotent: re-running produces zero additional credits.

### 4.6 rl_aggregator.py
**Role**: Pre-computed statistics for diagnostics and seeding.

5 aggregate files in `.codebot/state/rl_aggregates/`: model_stats, role_stats, discovery_stats, goal_calibration, planning_quality. Atomic writes. Idempotent.

### 4.7 rl_bandit.py
**Role**: Thompson Sampling model selection.

Beta-Bernoulli posteriors per (context_key, model) pair. Context key: `{role}|{ticket_class}`. `select_model()` samples from posteriors, returns highest sample. High-risk tickets always exploit. `record_outcome()` is deprecated (logs warning, delegates to `_record_outcome_internal()`). Credit assigner is the sole caller of internal functions.

### 4.8 rl_discovery_bandit.py
**Role**: Thompson Sampling over 9 discovery roles.

`record_discovery_outcome()` deprecated. Discovery outcomes flow through credit assigner via DISCOVERY_DOWNSTREAM events.

### 4.9 rl_goal_bandit.py
**Role**: Thompson Sampling over NOW/LATER/NEVER.

`record_goal_outcome()` deprecated. Goal outcomes evaluated with delay by credit assigner. Immediate recording removed from health_check_loop.

### 4.10 rl_advisory.py
**Role**: Central recommendation facade. Delegates to bandit.

The `_advisor` closure in `get_model_recommendation()` now calls `rl_bandit.select_model()` instead of computing `success_rate - regression_rate * 2` from aggregates. Advisory is a pure facade; the bandit is the single source of truth.

Four independent policy constants: `POLICY_MODEL_SELECTOR`, `POLICY_DISCOVERY_ALLOCATOR`, `POLICY_GOAL_ADVISOR`, `POLICY_PROMPT_EVOLUTION`.

Project-scoped observation flag: `set_observation_enabled(state_dir, bool)` / `is_observation_enabled(state_dir)`.

Shadow recommendations logged via `_log_shadow_recommendation()` (renamed from `_log_counterfactual`).

### 4.11 rl_policy_promotion.py
**Role**: Outcome-based promotion evaluation.

Promotion criteria changed from agreement rate to outcome fitness comparison:
- Reads counterfactual log entries with `ticket_id` fields
- Splits into followed cohort (agreed=True) and fallback cohort (agreed=False)
- Computes average fitness for each cohort from `rl_outcomes/` reward vectors
- SHADOW → ADVISORY: avg_confidence ≥ 0.3 AND improvement_ratio ≥ 1.0
- ADVISORY → ACTIVE: avg_confidence ≥ 0.6 AND improvement_ratio ≥ 1.05
- Agreement rate retained as diagnostic metric only, not used for promotion decisions

### 4.12 rl_prompt_evolution.py
**Role**: Failure-to-pattern trigger generation. Closed taxonomy only.

Maps failure types and low reward dimensions to predefined `PATTERN_HINTS` templates. Trigger payloads contain only `q_values` (pattern name → weight mappings). Raw reviewer text (`description`, `recommendation` fields) is never included in trigger payloads. `evaluate_triggers()` accepts `exclude_roles` parameter to prevent dual-evolution conflicts with active tournaments.

### 4.13 rl_tournament.py
**Role**: Competitive natural selection for prompt evolution.

Key parameters: `MIN_TICKETS_PER_VARIANT = 10`, `MIN_WINNING_MARGIN = 0.05`.

`assign_ticket_to_variant()` accepts `ticket_class` for stratified distribution across variants.

`promote_winner()` requires:
1. All variants have ≥ 10 completed tickets
2. Winner fitness exceeds runner-up by ≥ 0.05 margin
3. `POLICY_PROMPT_EVOLUTION == ACTIVE` (ADVISORY logs winner but doesn't replace canonical)
4. Creates `.gen{N}` backup before replacement
5. `rollback_prompt(role, roles_dir, state_dir, generation)` restores any previous generation

### 4.14 rl_project_metrics.py
**Role**: Gaming signal detection. Requires ≥ 10 observations before signaling.

### 4.15 rl_diagnostics.py
**Role**: Unified health reporting and pipeline orchestration.

`run_pipeline_tick()`:
- Gated by `is_observation_enabled(state_dir)` — returns skipped if False
- Protected by `fcntl.flock` on `.rl_pipeline.lock` per state_dir — second concurrent call returns skipped
- Stages: outcomes → rewards → aggregates → credit assignment → policy promotion → prompt evolution (gated by POLICY_PROMPT_EVOLUTION, excludes active tournament roles) → tournament status → tournament evolve (only if ACTIVE)

Centralized error tracking: `record_rl_error(state_dir, operation, error)` logs warnings and increments counters scoped per project. `get_error_counts(state_dir)` returns dict. Error counts included in `full_report()["errors"]`.

---

## 5. Production Integration Points

### 5.1 ticket_engine.py

`_STATE_TO_RL_EVENT` maps 18 TicketState values to event types. Semantically corrected: DECOMP→DECOMP_STARTED (not COMPLETED), PLANNING→PLAN_STARTED. DUPLICATE, NOT_ACTIONABLE mapped. BLOCKED intentionally unmapped.

`Ticket` dataclass has `transition_version: int` (incremented on every transition) and `current_attempt_id: str` (generated on IMPLEMENT transitions).

`_emit_rl_event()` passes `attempt_id`, `transition_id`, `model` on every event. On terminal states, emits both the mapped event type AND a `TICKET_TERMINAL` factual event carrying `failure_origin` and `failure_type` for credit assignment. No direct bandit mutation.

### 5.2 model_manager.py

`next_model_for_role(role, *, ticket_class, severity, risk)` accepts optional context kwargs. Passes them through to advisory → bandit for contextual Thompson Sampling. After selection, calls `record_actual_decision()` to close the shadow recommendation loop. All RL calls wrapped in `record_rl_error()` instead of silent `except: pass`.

### 5.3 process_manager.py

`_prepare_prompt_with_context()` build order:
1. Check for active tournament → if so, read variant prompt as base
2. Fall back to canonical prompt if no variant
3. Append ticket context once
4. Append scratchpad handoff once

Variant replaces the base entirely rather than appending to an already-built prompt.

### 5.4 health_check_loop.py

Goal alignment consumer queries `advise_goal_decision()` and logs advice in event context. Immediate `record_goal_outcome()` call removed — delayed evaluation handled by credit assigner. Alignment service import guarded with `ModuleNotFoundError` (not broad `ImportError`).

### 5.5 discovery_manager.py

`compute_allocation()` queries `select_discovery_role()` and boosts recommended role's weight by `(1 + confidence)` when confidence > 0.1. `record_downstream_completion()` emits `DISCOVERY_DOWNSTREAM` factual events with quality field instead of calling bandit directly. Accepts `quality` parameter (completed/actionable vs duplicate/rejected).

### 5.6 orchestrator.py / orchestrator_runtime.py

`_periodic_rl_pipeline_tick()` combines alignment no-op + pipeline tick. Pipeline tick dispatched via `threading.Thread(daemon=True)`. Internal locking prevents concurrent execution.

### 5.7 codebot_bootstrap.py

All 7 new RL modules registered in `_ADAPTER_MODULES` for adapter injection at startup.

### 5.8 prompt_optimizer.py

`_generate_feedback_hint()` uses closed taxonomy only: looks up `category` field against `PATTERN_HINTS` keys. Never embeds raw `description` or `recommendation` text from reviewer feedback. Prevents prompt injection via repository-controlled content flowing through reviewer → trigger → permanent prompt.

---

## 6. Complete Data Flow

### Phase A: Real-time Event Emission

```
ticket_engine.transition()
  → Ticket.transition() increments transition_version, generates attempt_id on IMPLEMENT
  → _emit_rl_event() maps state to event type
  → append_event() with attempt_id, transition_id, model, actor_role
  → On terminal state: also emits TICKET_TERMINAL with failure_origin/type

model_manager.next_model_for_role()
  → get_model_recommendation() → select_model() (Thompson Sampling)
  → append_event(MODEL_SELECTED)
  → record_actual_decision() → shadow recommendation log

health_check_loop goal consumer
  → advise_goal_decision() → logs advice in event context
  → append_event(GOAL_DECISION)
  → NO immediate record_goal_outcome()

discovery_manager.compute_allocation()
  → select_discovery_role() → boosts weight

discovery_manager.record_downstream_completion()
  → append_event(DISCOVERY_DOWNSTREAM, quality=...)
```

### Phase B: Periodic Pipeline Tick (every 30 min, background thread, fcntl-locked)

```
orchestrator_runtime main loop
  → threading.Thread(target=_periodic_rl_pipeline_tick, daemon=True)
    → fcntl.flock(.rl_pipeline.lock, LOCK_EX | LOCK_NB)
    → if locked: return {skipped: pipeline_already_running}
    → if not observation_enabled: return {skipped: observation_disabled}

    Stage 1: rebuild_all_outcomes(incremental=True)
      → Read .rl_outcome_offset, seek to byte position
      → Scan new lines, dedup by transition_id
      → Produce TicketOutcome per ticket
      → Write new offset

    Stage 2: update_outcome_reward() per outcome
      → 9 dimensions computed
      → Confidence from time + exposure signals
      → Save updated outcome

    Stage 3: rebuild_all_aggregates()
      → 5 aggregate files

    Stage 4: assign_credits()
      → Read .rl_credit_offset
      → Scan new events
      → Apply attribution rules per schema version
      → Model: terminal_state + failure_origin → success/failure/no-signal
      → Goal: delayed ternary (POSITIVE/NEGATIVE/UNRESOLVED)
      → Discovery: quality field → success/failure
      → Call _record_*_internal() for each credit
      → Write new offset

    Stage 5: evaluate_all_policies()
      → Split counterfactuals into followed/fallback cohorts
      → Compute average fitness per cohort from outcomes
      → Compare improvement_ratio against thresholds

    Stage 6: evaluate_triggers(exclude_roles=active_tournaments)
      → Map failures to patterns (closed taxonomy)
      → Write .evolve.json with q_values only (no raw text)

    Stage 7: tournament_evolve() (only if POLICY_PROMPT_EVOLUTION == ACTIVE)
      → maybe_start_tournament() for eligible roles
      → evaluate_fitness() for active tournaments
      → promote_winner() if margin ≥ 0.05 and ACTIVE

    → Release flock
```

### Phase C: Agent Startup

```
process_manager._prepare_prompt_with_context()
  → Check tournament state for role
  → If active: assign_ticket_to_variant(role, ticket_id, ticket_class)
    → Read variant prompt as base
  → Else: read canonical prompt as base
  → Append ticket context once
  → Append scratchpad handoff once
  → Return combined prompt

prompt_optimizer.consume_triggers()
  → Read .evolve.json triggers
  → Select highest-weighted pattern from q_values
  → If reviewer_feedback present: look up category in PATTERN_HINTS only
  → Append <!-- CODEBOT EVOLUTION --> section to role prompt
  → Max 5 evolutions, max 15000 chars
  → Delete trigger after consumption
```

---

## 7. Policy Mode Progression

```
OBSERVATION_ENABLED (independent gate)
  True: events collected, outcomes aggregated, rewards computed
  False: pipeline tick returns immediately, no data collection

POLICY_MODE (per-policy, 4 independent instances)
  OFF: no recommendations computed, zero overhead
  SHADOW: recommendations computed + logged, fallback always used
  ADVISORY: recommendations returned to caller, caller may use them
    Tournament: proposes winner but does NOT replace canonical prompt
  ACTIVE: recommendations should be followed
    Tournament: promotes winner, replaces canonical prompt with backup

Promotion criteria (outcome-based):
  SHADOW → ADVISORY: observations ≥ 50, confidence ≥ 0.3, improvement_ratio ≥ 1.0
  ADVISORY → ACTIVE: observations ≥ 50, confidence ≥ 0.6, improvement_ratio ≥ 1.05
  Improvement ratio = avg_fitness(followed_cohort) / avg_fitness(fallback_cohort)
  Agreement rate is diagnostic only, never gates promotion
```

---

## 8. File System Layout

```
.codebot/state/
├── rl_events.jsonl              # Append-only factual events (schema v2)
├── rl_dead_letters.jsonl         # Unknown event types for debugging
├── rl_bandit.json                # Model selection posteriors
├── .rl_outcome_offset            # Byte offset for incremental outcome rebuild
├── .rl_credit_offset             # Byte offset for incremental credit assignment
├── .rl_pipeline.lock             # fcntl single-flight guard
├── rl_outcomes/
│   ├── CB-0001.json              # Per-ticket outcomes (9-dim rewards)
│   └── ...
├── rl_aggregates/
│   ├── model_stats.json
│   ├── role_stats.json
│   ├── discovery_stats.json
│   ├── goal_calibration.json
│   └── planning_quality.json
├── rl_counterfactuals/
│   └── counterfactuals.jsonl     # Shadow recommendation log
├── alignment_triggers/
│   ├── implementer.evolve.json   # q_values only, no raw text
│   └── ...
└── tournament/
    ├── implementer_state.json
    ├── implementer_A.md          # Variant A (baseline)
    ├── implementer_B.md          # Variant B (failure fix)
    ├── implementer_C.md          # Variant C (reward fix)
    └── ...
codebot/roles/
├── implementer.md                # Canonical prompt
├── implementer.md.gen0           # Backup before tournament promotion
├── implementer.md.gen1           # Previous generation backup
└── ...
```

---

## 9. Key Invariants

1. **Facts only in event log**: Events record what happened. Credit assignment derives what it means. Attribution logic is versioned and replayable.
2. **Single writer for bandits**: `rl_credit_assigner.assign_credits()` is the only code path that updates bandit posteriors. Public `record_outcome()` functions are deprecated and log warnings.
3. **No mutation of authoritative state**: RL modules never call `transition()`, `spawn()`, or modify ticket objects. All writes go to RL-owned files.
4. **Append-only event log**: `os.O_APPEND` guarantees atomic concurrent writes.
5. **Deterministic fallback everywhere**: OFF mode skips RL entirely. Insufficient data returns defaults. Failed imports caught and reported.
6. **Exception-safe with visibility**: Every RL call site uses `record_rl_error()` instead of silent `except: pass`. Errors visible in `full_report()["errors"]`.
7. **Independent policy gates**: 4 separate policies control different behaviors. Model selection ACTIVE doesn't enable prompt mutation.
8. **Observation gate**: Separate from policy modes. Controls whether data collection runs at all.
9. **Risk-aware exploration**: High-risk tickets bypass Thompson Sampling exploration.
10. **Gaming-resistant thresholds**: Minimum observation counts before signals/promotions fire.
11. **Self-target protection**: Neither triggers nor tournaments target prompt_optimizer.
12. **Closed taxonomy for prompts**: Only predefined PATTERN_HINTS templates enter prompts. Raw reviewer text never flows through the trigger → prompt pipeline.
13. **Atomic file operations**: All state writes use tmp + `os.replace()`.
14. **Semantic deduplication**: `transition_id` prevents double-counting when the same authoritative transition emits multiple events.
15. **Delayed goal evaluation**: Goal bandit outcomes evaluated only after downstream evidence arrives. UNRESOLVED outcomes don't update posteriors.
16. **Outcome-based promotion**: Policy promotion requires demonstrated fitness improvement, not just agreement between RL and fallback.
17. **Tournament safety**: 10-ticket minimum, 5% winning margin, ACTIVE-only promotion, versioned backups, rollback capability, dual-evolution lockout.
18. **Single-flight pipeline**: `fcntl.flock` prevents concurrent pipeline ticks per project.
19. **stdlib-only**: No external dependencies beyond Python standard library.

---

## 10. Remediation History

This document reflects the architecture after 7 remediation phases addressing 20 review findings:

| Phase | Focus | Key Change |
|-------|-------|------------|
| 0 | Foundation | Renamed dimensions, semantic state map, per-policy gates, file-based lock |
| 1 | Factual Events | Schema v2, attempt/transition IDs, TICKET_TERMINAL events, deprecated direct mutation |
| 2 | Credit Assignment | New `rl_credit_assigner.py`, delayed goal evaluation, 9-dim rewards, exposure confidence |
| 3 | Bandit Architecture | Deprecation tests, semantic dedup, single-writer enforcement |
| 4 | Unify Selection | Advisory delegates to bandit Thompson Sampling |
| 5 | Pipeline Safety | Centralized error tracking, dead-letter logging, observable failures |
| 6 | Tournament Fixes | Correct build order, 10-ticket minimum, margin requirement, ACTIVE-only, versioned backup |
| 7 | Policy & Security | Outcome-based promotion, closed taxonomy prompt injection protection |
