# module-design-docs - Work Plan

## TL;DR (For humans)

**What you'll get:** Every Python source file in the codebase gets a co-located Markdown design document explaining its purpose, public API, internal architecture, invariants, and dependencies. The root ENTRYPOINT.md becomes the authoritative file manifest that all agents read first to understand the system topology before touching any code. 100% documentation coverage across 115 files.

**Why this approach:** "Form follows function through context." Agents cannot make correct decisions without understanding the system. By placing design docs next to source files and indexing everything in ENTRYPOINT.md, every agent (discovery, implementation, review, user_agent) loads the relevant context before acting. This eliminates blind mutations — the class of bugs where an agent modifies a module without understanding its contracts.

**What it will NOT do:** It will not modify any Python source code, change runtime behavior, alter role prompt templates, or add dependencies. Pure documentation generation only.

**Effort:** XL
**Risk:** Low - documentation only, no behavioral changes. Risk is inconsistency if template isn't enforced.
**Decisions to sanity-check:** Template structure, subsystem grouping for parallel generation, ENTRYPOINT.md organization scheme

Your next move: approve and run `$start-work`, or request high-accuracy review.

---

> TL;DR (machine): XL/Low risk. 5 todos across 3 waves. Defines .md template spec, generates 115 co-located design docs via parallel deep agents grouped by subsystem (core-orchestration, core-ticket, core-dispatch, core-review, core-infrastructure, scheduler_v2, adapters, migrations, packages), rewrites ENTRYPOINT.md as authoritative file manifest, validates 100% coverage.

## Architecture

```
                    ENTRYPOINT.md
                    (authoritative index)
                          │
            ┌─────────────┼─────────────┐
            │             │             │
            ▼             ▼             ▼
      Subsystem       Subsystem      Subsystem
       Group A         Group B        Group C
            │             │             │
     ┌──────┴──────┐      │             │
     ▼             ▼      ▼             ▼
  foo.py        bar.py  baz.py       qux.py
  foo.md        bar.md  baz.md       qux.md
  (design)      (design) (design)     (design)

Each .md contains:
  - Purpose (WHY this module exists)
  - Public API (WHAT callers use)
  - Internal Architecture (HOW it works)
  - Invariants (MUST/MUST NOT rules)
  - Dependencies (WHAT it imports/exports)
  - Data Flow (WHERE information moves)
```

## Normative Preamble

This plan implements the foundational doctrine: Context is the source. Code is the artifact. (ADR-008)

This plan implements the **CONTEXT IS THE CONTROL PLANE** invariant (CODING_STANDARDS.md §22). The lifecycle is a context compiler: each stage produces durable context consumed by the next. Co-located `.md` design documents are not passive reference material — they are executable context for the swarm. Every agent reads the relevant `.md` before acting on a module. ENTRYPOINT.md is the authoritative index mapping every source file to its context document.

Context ownership per lifecycle stage:
- **User Agent** → intent, goals, constraints, roadmap/design docs
- **Discovery/Triage** → observed problem, evidence, severity, scope
- **Decomposition** → atomic work boundaries, dependencies, affected components
- **Planning** → implementation strategy, invariants, files/symbols, acceptance criteria
- **Implementation** → code changes, implementation-specific discoveries
- **Review** → correctness findings, regressions, documentation drift
- **Completion** → final authoritative state

A module's `.md` file is the durable artifact of these stages for that module's domain.

## Design Document Template

Every `.md` file MUST follow this structure exactly. No deviations.

```markdown
# {module_name}

## Purpose
One paragraph: WHY this module exists. What problem it solves. What would break without it.
What lifecycle stage(s) own this module's context.

## Public API
Table of every public function/class/method intended for external callers:
| Symbol | Signature | Purpose | Called by |
|--------|-----------|---------|----------|

## Internal Architecture
How the module works internally. Key data structures, algorithms, state machines.
Subsections as needed for complex modules.

## Invariants
Bulleted list of MUST/MUST NOT rules this module enforces.
These are the contracts that reviewers verify.
Reference CODING_STANDARDS.md §N where applicable.

## Dependencies
- **Imports from**: list of internal modules this depends on (link to their .md)
- **Imported by**: list of internal modules that depend on this
- **External**: stdlib modules used (no third-party deps)

## Data Flow
Where information enters, how it transforms, where it exits.
For stateful modules: the state machine diagram.
Map to lifecycle context flow where relevant.

## Context Ownership
Which lifecycle stage(s) produce and validate this module's authoritative context.
What durable artifacts (ticket fields, Finding JSON, plan files, this .md) hold that context.

## Related
Links to other .md files for closely related modules.
```

## Scope
### Must have
- Standardized template applied consistently to all 115 files
- 98 core module docs (`codebot/*.py` → `codebot/*.md`)
- 4 scheduler_v2 docs (`codebot/scheduler_v2/*.py` → `codebot/scheduler_v2/*.md`)
- 2 adapter docs (`codebot/adapters/*.py` → `codebot/adapters/*.md`)
- 7 migration docs (`codebot/migrations/*.py` → `codebot/migrations/*.md`)
- 4 package-level docs (`__init__.py` → `__init__.md` for each package)
- Rewritten ENTRYPOINT.md with full file manifest organized by subsystem
- Validation confirming 100% coverage (every .py has matching .md)

### Must NOT have (guardrails, anti-slop, scope boundaries)
- No modifications to any `.py` source files
- No modifications to role prompt `.md` files in `codebot/roles/`
- No creation of docs outside `codebot/` tree (except root ENTRYPOINT.md)
- No changes to runtime behavior, imports, or configuration
- No new dependencies
- No AI-generated filler content — every sentence must reference actual code
- No documenting private functions unless they encode critical invariants

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: validation script (not pytest — this is documentation, not behavior)
- Evidence: .omo/evidence/task-<N>-module-design-docs.md

## Execution strategy

### Subsystem groupings for parallel generation

Files are grouped into 9 batches for parallel deep agent delegation. Each batch shares architectural context so one agent can document the whole subsystem coherently.

| Batch | Name | Files | Key Modules |
|-------|------|-------|-------------|
| 1 | Core Orchestration | 12 | orchestrator.py, orchestrator_runtime.py, orchestrator_services.py, orchestrator_cli.py, orchestrator_controller.py, orchestrator_compat.py, orchestrator_scheduler.py, orchestrator_status.py, health_check_loop.py, health_monitor.py, process_manager.py, process_supervisor.py |
| 2 | Core Ticket | 8 | ticket_engine.py, ticket_dispatcher.py, ticket_status.py, dispatch_service.py, pipeline_state.py, lifecycle_packet.py, lease_state.py, workforce_dispatch.py |
| 3 | Core Scheduler V2 | 5 | scheduler_v2/dispatcher.py, scheduler_v2/dispatch_gate.py, scheduler_v2/lifecycle.py, scheduler_v2/__init__.py, scheduler_config.py |
| 4 | Core Review | 10 | review_gate.py, review_store.py, review_config.py, review_types.py, review_metrics.py, review_learning_registry.py, quality_gate.py, gatekeeper.py, evidence_validator.py, completion_commit.py |
| 5 | Core Discovery | 8 | discovery_daemon.py, discovery_finding.py, discovery_manager.py, alignment_service.py, alignment_events.py, alignment_coordinator.py, conflict_detector.py, telemetry.py |
| 6 | Core Infrastructure | 20 | api_runner.py, api_tools.py, control_server.py, control_client.py, state_manager.py, file_lock.py, locks.py, scratchpad.py, checkpoint_manager.py, config_reloader.py, context_compactor.py, model_router.py, model_manager.py, model_registry.py, pricing_table.py, token_budget.py, credentials.py, botop.py, dashboard.py, readiness.py |
| 7 | Core RL | 12 | rl_engine.py, rl_event_log.py, rl_failure_taxonomy.py, rl_outcome_aggregator.py, rl_reward_engine.py, rl_review_dataset.py, rl_review_episode.py, rl_review_features.py, rl_review_linker.py, rl_review_metrics.py, rl_review_policy.py, rl_review_shadow.py, rl_review_train.py |
| 8 | Adapters + Migrations + Misc | 16 | adapters/flask_app_adapter.py, adapters/__init__.py, codebot_adapter.py, monitor_adapter.py, project_adapter.py, vcs_adapter.py, migrations/*.py (7), roles.py, role_registry.py, service_registry.py, runtime_invariants.py, dependency_graph.py |
| 9 | Package + Root | 6 | codebot/__init__.py, prompt_gateway.py, prompt_optimizer.py, tool_policy.py, web_tools.py, ui_components.py, worker_scaler.py, workforce_feedback.py, work_scorer.py, queue_pressure.py, adaptive_scheduler.py, adaptive_rate_limiter.py, active_work_index.py, auto_revert.py, bot_metrics.py, check_drain.py, cost_tracker.py, coverage_runner.py, documentation_ticket_generator.py, escalation_rules.py, event_log.py, implementation_planner.py, resolved_index.py, risk_classifier.py, task_splitter.py, __main__.py |

### Parallel execution waves
Wave 1: Template + Batch 1-3 (orchestration, ticket, scheduler) — 3 parallel deep agents
Wave 2: Batch 4-6 (review, discovery, infrastructure) — 3 parallel deep agents
Wave 3: Batch 7-9 (RL, adapters+migrations, remaining) + ENTRYPOINT rewrite — 3 parallel deep agents
Wave 4: Validation — 1 agent verifies 100% coverage

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1 | none | 2,3,4,5 | none |
| 2 | 1 | 5 | 3, 4 |
| 3 | 1 | 5 | 2, 4 |
| 4 | 1 | 5 | 2, 3 |
| 5 | 2,3,4 | none | none |

## Todos
> Implementation + Test = ONE todo. Never separate.

- [ ] 1. Define and validate design document template
  What to do / Must NOT do: Create `docs/MODULE_DOC_TEMPLATE.md` containing the standardized template specified in this plan's "Design Document Template" section. Include: Purpose, Public API table format, Internal Architecture, Invariants, Dependencies (imports from/imported by/external), Data Flow, Related sections. Add explicit instructions for the generating agent: (A) Read the entire .py file before writing. (B) Extract public symbols via AST inspection (functions/classes not prefixed with _). (C) Trace import statements to build dependency graph. (D) Identify state machines and data flow patterns. (E) Write invariants as MUST/MUST NOT bullet points derived from code assertions, guards, and comments. (F) Keep each .md under 400 lines. (G) Never fabricate APIs that don't exist in the source. Must NOT modify any .py files. Must NOT create role prompt docs.
  Parallelization: Wave 1 | Blocked by: none | Blocks: 2, 3, 4
  References (executor has NO interview context - be exhaustive): docs/ARCHITECTURE.md (existing architecture reference), README.md (product context), .codebot/constitution.md (invariants to reference), codebot/orchestrator.py (example of complex module to template against)
  Acceptance criteria (agent-executable): `test -f docs/MODULE_DOC_TEMPLATE.md && grep -q '## Purpose' docs/MODULE_DOC_TEMPLATE.md && grep -q '## Public API' docs/MODULE_DOC_TEMPLATE.md && grep -q '## Invariants' docs/MODULE_DOC_TEMPLATE.md && grep -q '## Dependencies' docs/MODULE_DOC_TEMPLATE.md && echo 'Template OK'`
  QA scenarios (name the exact tool + invocation): happy: template file exists with all required sections. failure: missing any required section header → fail. Evidence .omo/evidence/task-1-module-design-docs.md
  Commit: Y | docs(template): add MODULE_DOC_TEMPLATE.md for co-located design docs

- [ ] 2. Generate design docs for Core Orchestration + Core Ticket + Core Scheduler V2 (Batches 1-3, 25 files)
  What to do / Must NOT do: Delegate to 3 parallel `deep` agents, one per batch. Each agent receives: (A) the template from `docs/MODULE_DOC_TEMPLATE.md`, (B) the list of .py files in its batch, (C) instruction to read each .py file fully, extract public API via AST, trace imports, identify invariants, and write the co-located .md file. Batch 1 (Core Orchestration, 12 files): orchestrator.py, orchestrator_runtime.py, orchestrator_services.py, orchestrator_cli.py, orchestrator_controller.py, orchestrator_compat.py, orchestrator_scheduler.py, orchestrator_status.py, health_check_loop.py, health_monitor.py, process_manager.py, process_supervisor.py. Batch 2 (Core Ticket, 8 files): ticket_engine.py, ticket_dispatcher.py, ticket_status.py, dispatch_service.py, pipeline_state.py, lifecycle_packet.py, lease_state.py, workforce_dispatch.py. Batch 3 (Core Scheduler V2, 5 files): scheduler_v2/dispatcher.py, scheduler_v2/dispatch_gate.py, scheduler_v2/lifecycle.py, scheduler_v2/__init__.py, scheduler_config.py. Each agent writes `{module}.md` next to `{module}.py`. Must NOT modify .py files. Must NOT skip any file in the batch. Must follow template exactly.
  Parallelization: Wave 1 | Blocked by: 1 | Blocks: 5
  References (executor has NO interview context - be exhaustive): docs/MODULE_DOC_TEMPLATE.md (template from todo 1), codebot/orchestrator.py:1-374, codebot/ticket_engine.py:1-2309, codebot/scheduler_v2/dispatcher.py:1-766 (these are the largest files in each batch — agents must handle them)
  Acceptance criteria (agent-executable): `for f in codebot/orchestrator.py codebot/ticket_engine.py codebot/scheduler_v2/dispatcher.py codebot/process_manager.py codebot/dispatch_service.py codebot/health_check_loop.py codebot/scheduler_v2/dispatch_gate.py; do base="${f%.py}"; if [ ! -f "${base}.md" ]; then echo "MISSING: ${base}.md"; exit 1; fi; done && echo 'Batch 1-3 OK'`
  QA scenarios (name the exact tool + invocation): happy: all 25 .md files exist and contain ## Purpose, ## Public API, ## Invariants sections. failure: any .md file missing → fail. failure: any .md file has empty Public API table for a module with public functions → fail. Evidence .omo/evidence/task-2-module-design-docs.md
  Commit: Y | docs(core): add design docs for orchestration, ticket, and scheduler modules

- [ ] 3. Generate design docs for Core Review + Core Discovery + Core Infrastructure (Batches 4-6, 38 files)
  What to do / Must NOT do: Delegate to 3 parallel `deep` agents, one per batch. Same template and instructions as Todo 2. Batch 4 (Core Review, 10 files): review_gate.py, review_store.py, review_config.py, review_types.py, review_metrics.py, review_learning_registry.py, quality_gate.py, gatekeeper.py, evidence_validator.py, completion_commit.py. Batch 5 (Core Discovery, 8 files): discovery_daemon.py, discovery_finding.py, discovery_manager.py, alignment_service.py, alignment_events.py, alignment_coordinator.py, conflict_detector.py, telemetry.py. Batch 6 (Core Infrastructure, 20 files): api_runner.py, api_tools.py, control_server.py, control_client.py, state_manager.py, file_lock.py, locks.py, scratchpad.py, checkpoint_manager.py, config_reloader.py, context_compactor.py, model_router.py, model_manager.py, model_registry.py, pricing_table.py, token_budget.py, credentials.py, botop.py, dashboard.py, readiness.py. Note: api_runner.py (3509 LOC) and control_server.py (2484 LOC) are oversized — their docs must capture the full architecture including all endpoint handlers and tool dispatch logic. Must NOT modify .py files.
  Parallelization: Wave 2 | Blocked by: 1 | Blocks: 5
  References (executor has NO interview context - be exhaustive): docs/MODULE_DOC_TEMPLATE.md (template), codebot/api_runner.py:1-3509 (largest file in codebase), codebot/control_server.py:1-2484 (second largest), codebot/review_gate.py, codebot/discovery_finding.py:246-272 (DiscoveryFinding schema to document)
  Acceptance criteria (agent-executable): `count=0; for f in codebot/review_gate.py codebot/discovery_finding.py codebot/api_runner.py codebot/control_server.py codebot/quality_gate.py codebot/gatekeeper.py codebot/state_manager.py codebot/file_lock.py; do base="${f%.py}"; [ -f "${base}.md" ] && count=$((count+1)); done; [ $count -eq 8 ] && echo 'Batch 4-6 spot check OK' || echo "MISSING: $count/8"`
  QA scenarios (name the exact tool + invocation): happy: all 38 .md files exist with required sections. failure: api_runner.md missing endpoint documentation → fail. failure: control_server.md missing route table → fail. Evidence .omo/evidence/task-3-module-design-docs.md
  Commit: Y | docs(core): add design docs for review, discovery, and infrastructure modules

- [ ] 4. Generate design docs for Core RL + Adapters + Migrations + Remaining (Batches 7-9, 52 files)
  What to do / Must NOT do: Delegate to 3 parallel `deep` agents, one per batch. Same template and instructions as Todo 2. Batch 7 (Core RL, 12 files): rl_engine.py, rl_event_log.py, rl_failure_taxonomy.py, rl_outcome_aggregator.py, rl_reward_engine.py, rl_review_dataset.py, rl_review_episode.py, rl_review_features.py, rl_review_linker.py, rl_review_metrics.py, rl_review_policy.py, rl_review_shadow.py, rl_review_train.py. Batch 8 (Adapters + Migrations + Misc, 16 files): adapters/flask_app_adapter.py, adapters/__init__.py, codebot_adapter.py, monitor_adapter.py, project_adapter.py, vcs_adapter.py, migrations/migration_001.py through migration_008.py (7 files), roles.py, role_registry.py, service_registry.py, runtime_invariants.py, dependency_graph.py. Batch 9 (Package + Remaining, ~24 files): codebot/__init__.py, prompt_gateway.py, prompt_optimizer.py, tool_policy.py, web_tools.py, ui_components.py, worker_scaler.py, workforce_feedback.py, work_scorer.py, queue_pressure.py, adaptive_scheduler.py, adaptive_rate_limiter.py, active_work_index.py, auto_revert.py, bot_metrics.py, check_drain.py, cost_tracker.py, coverage_runner.py, documentation_ticket_generator.py, escalation_rules.py, event_log.py, implementation_planner.py, resolved_index.py, risk_classifier.py, task_splitter.py, __main__.py. Migration docs should explain WHAT changed and WHY, not just repeat the code. __init__.py docs describe the package contract. Must NOT modify .py files.
  Parallelization: Wave 3 | Blocked by: 1 | Blocks: 5
  References (executor has NO interview context - be exhaustive): docs/MODULE_DOC_TEMPLATE.md (template), codebot/rl_engine.py (RL system core), codebot/project_adapter.py (adapter interface), codebot/migrations/ (all migration files), codebot/role_registry.py:298-490 (role definitions to document)
  Acceptance criteria (agent-executable): `total_expected=115; total_found=$(find codebot/ -name '*.md' -not -path '*/roles/*' -not -path '*/__pycache__/*' | wc -l); echo "Found $total_found / expected $total_expected"; [ "$total_found" -ge 110 ] && echo 'Batch 7-9 OK' || echo 'INCOMPLETE'`
  QA scenarios (name the exact tool + invocation): happy: all 52 .md files exist. failure: migration docs missing rationale → fail. failure: __init__.md missing package contract → fail. Evidence .omo/evidence/task-4-module-design-docs.md
  Commit: Y | docs(core): add design docs for RL, adapters, migrations, and remaining modules

- [ ] 5. Rewrite ENTRYPOINT.md with full file manifest and validate 100% coverage
  What to do / Must NOT do: Two parts. (A) Rewrite `/home/kozuka/Work/CodeBot/ENTRYPOINT.md` preserving the existing "Run It" and "Non-Obvious Facts" sections but replacing the "Key Directories" and "Core Loop" sections with a comprehensive file manifest organized by subsystem. Structure: (1) "Run It" (preserve existing), (2) "Read In This Order" (update to reference new .md files), (3) "System Topology" (ASCII diagram showing subsystem relationships), (4) "File Manifest" — table with columns: Module Path | Design Doc | Purpose (one line) | Subsystem. Group entries by subsystem: Orchestration, Ticket Lifecycle, Dispatch & Scheduling, Review & Quality, Discovery & Alignment, Infrastructure & Control, RL & Learning, Adapters, Migrations, Utilities. Every .py file gets a row pointing to its .md. (5) "Non-Obvious Facts" (preserve existing). (B) Create validation script `scripts/validate_module_docs.py` that: finds all .py files under codebot/ (excluding roles/ and __pycache__/), checks each has a matching .md, verifies each .md contains required sections (Purpose, Public API, Invariants, Dependencies), reports missing files and incomplete docs, exits 0 if 100% coverage, exits 1 otherwise. Must NOT modify any .py source files except creating the validation script.
  Parallelization: Wave 4 | Blocked by: 2, 3, 4 | Blocks: none
  References (executor has NO interview context - be exhaustive): ENTRYPOINT.md:1-66 (current content to preserve parts of), docs/ARCHITECTURE.md (subsystem groupings reference), codebot/ (full directory listing for manifest)
  Acceptance criteria (agent-executable): `python3 scripts/validate_module_docs.py && echo 'VALIDATION PASSED' || echo 'VALIDATION FAILED'`
  QA scenarios (name the exact tool + invocation): happy: validation script exits 0, ENTRYPOINT.md contains all 115+ module references. failure: delete one .md file, run validation, assert exit 1 with specific missing file reported. failure: create .md without ## Invariants section, assert validation reports incomplete doc. Evidence .omo/evidence/task-5-module-design-docs.md
  Commit: Y | docs(entrypoint): rewrite with full file manifest; feat(scripts): add module doc validator

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Coverage audit — run validate_module_docs.py, confirm 100% .py → .md coverage
- [ ] F2. Template compliance — sample 10 random .md files, verify all required sections present and non-empty
- [ ] F3. Accuracy spot-check — pick 5 modules, verify documented Public API matches actual source exports
- [ ] F4. ENTRYPOINT completeness — verify every subsystem group has entries, ASCII topology diagram is accurate

## Commit strategy
5 commits total (one per todo). Todo 2, 3, 4 could be squashed into one "docs: add all module design docs" commit if preferred, but keeping separate enables per-subsystem review. No squash unless requested.

## Success criteria
- Every `.py` file under `codebot/` (excluding `roles/`) has a co-located `.md` design document
- Every `.md` follows the standardized template (Purpose, Public API, Internal Architecture, Invariants, Dependencies, Data Flow, Related)
- ENTRYPOINT.md contains a complete file manifest organized by subsystem with links to every design doc
- `scripts/validate_module_docs.py` passes with 100% coverage
- Zero `.py` files were modified
- All 4 final verifiers pass
