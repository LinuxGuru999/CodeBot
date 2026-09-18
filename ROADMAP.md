# Task: Build the Authoritative CodeBot Development Roadmap

<!--
ROADMAP_SCHEMA: v2.0
MACHINE_READABLE: true
PARSER_INSTRUCTIONS:
  - Each deliverable is marked with <!-- DELIVERABLE: id=X status=Y tier=Z -->
  - Status values: DONE, IN_PROGRESS, PLANNED, BLOCKED
  - Tier values: T0 (critical), T1 (core), T2 (important), T3 (enhancement), T4+ (future)
  - Feature decomposer agents: parse DELIVERABLE comments, skip DONE items, create tickets for IN_PROGRESS/PLANNED
  - Human readers: ignore HTML comments, read prose normally
LAST_AUDITED: 2026-09-18
TOTAL_DELIVERABLES: 52
DONE: 28
IN_PROGRESS: 14
PLANNED: 10
-->

Create or completely rewrite `ROADMAP.md` for **CodeBot**, an autonomous software-engineering platform.

The roadmap must be comparable in rigor, organization, dependency tracking, measurable exit criteria, and production-readiness focus to a mature infrastructure-software roadmap.

This is not a feature wishlist.

It must define the engineering path from the current CodeBot/BotNet implementation to a **portable, reliable, secure, model-independent autonomous software factory** capable of taking deeply structured web applications from documented requirements through implementation, testing, security review, documentation, deployment, maintenance, and continuous improvement.

---

# 1. Product Vision

CodeBot's long-term purpose is:

> Given a software repository plus a structured project contract and engineering constitution, autonomously understand the system, discover necessary work, create evidence-backed tickets, prioritize and plan changes, implement them, test them, attack them, review them, update documentation, verify acceptance criteria, and continuously maintain the application with minimal human engineering effort.

CodeBot should eventually be capable of working on almost any sufficiently documented web application or software project.

Monitor is currently the primary proving ground, but CodeBot must not remain architecturally dependent on Monitor.

The eventual relationship must be:

```text
CodeBot
   │
   ├── Project Profile: Monitor
   ├── Project Profile: Customer App A
   ├── Project Profile: Customer App B
   └── Project Profile: SaaS Experiment C
```

NOT:

```text
Monitor
   └── Monitor-specific BotNet
```

---

# 2. Primary Strategic Goals

The roadmap must lead CodeBot toward all of the following capabilities.

<!-- DELIVERABLE: id=2.A status=IN_PROGRESS tier=T1 modules=project_adapter.py,codebot_bootstrap.py -->
## A. Portable Project Understanding

CodeBot must be able to enter an unrelated repository and understand it through:

* repository inspection
* generated and curated documentation
* project configuration
* architecture documents
* module documentation
* API contracts
* schemas
* tests
* source code
* dependency graphs
* Git history where appropriate

Monitor-specific assumptions must be extracted into project profiles or adapters.

**Exit Criteria:**

- [x] ProjectAdapter ABC defined with standard interface methods
- [x] codebot_bootstrap discovers and injects adapter at runtime
- [x] Path resolution goes through adapter, never hardcoded
- [ ] Monitor-specific logic fully removed from core modules
- [ ] Second project adapter implemented and validated

---

<!-- DELIVERABLE: id=2.B status=DONE tier=T0 modules=.codebot/project.yaml -->
## B. Project Contract

Every CodeBot-managed project should eventually support a standardized `.codebot/` contract such as:

```text
.codebot/
├── project.yaml
├── constitution.md
├── goals.md
├── architecture.md
├── security.md
├── testing.md
├── capabilities.md
├── roadmap.md
├── bugs.md
├── improvements.md
├── repositories.yaml
├── policies.yaml
├── commands.yaml
├── protected_paths.yaml
├── api/
└── modules/
```

The exact design may evolve, but the roadmap must provide a migration path toward a standard portable project interface.

---

<!-- DELIVERABLE: id=2.C status=DONE tier=T0 modules=.codebot/constitution.md -->
## C. Project Constitution

Each project must have human-controlled architectural and engineering invariants.

Agents may propose constitution changes but must never silently weaken requirements in order to satisfy a task.

Protected areas should include concepts such as:

* product purpose
* security boundaries
* minimum testing standards
* architectural invariants
* supported platforms
* compatibility requirements
* dependency policies
* human-approval requirements
* destructive-operation policies
* secrets policies
* data ownership rules

---

<!-- DELIVERABLE: id=2.D status=IN_PROGRESS tier=T0 modules=ticket_engine.py,migrate_queue.py,gatekeeper.py -->
## D. Ticket-Centered Autonomous Development

All meaningful CodeBot work must eventually flow through normalized structured tickets.

Bots must not discover an issue and immediately modify code without an authorized work item.

The lifecycle should resemble:

```text
DISCOVERED
    ↓
VALIDATING
    ↓
TRIAGED
    ↓
READY
    ↓
PLANNING
    ↓
IMPLEMENTING
    ↓
REVIEWING
    ↓
VERIFYING
    ↓
COMPLETE
```

with side states such as:

```text
BLOCKED
REWORK
REJECTED
DUPLICATE
DEFERRED
HUMAN_REQUIRED
```

Tickets should eventually contain structured fields including:

* ID
* title
* class
* priority/severity
* source
* evidence
* problem statement
* desired state
* acceptance criteria
* affected modules
* dependencies
* risk/blast radius
* security impact
* migration impact
* required reviewers
* required tests
* documentation requirements
* rollback strategy
* estimated model/resource cost
* final outcome

**Exit Criteria:**

- [x] Ticket schema normalized with all required fields
- [x] State machine enforces valid transitions
- [x] Discovery agents reliably create tickets
- [x] migrate_queue.py handles queue persistence
- [ ] Gatekeeper enforces all transition guards

---

<!-- DELIVERABLE: id=2.E status=IN_PROGRESS tier=T1 modules=role_registry.py,findings_log.py,discovery_manager.py -->
## E. Automatic Ticket Discovery

CodeBot must continuously discover actionable work from:

* failing tests
* source inspection
* static analysis
* security scans
* documentation drift
* TODO/FIXME markers
* architecture violations
* performance regressions
* dependency problems
* test coverage gaps
* production incidents
* user feedback
* monitoring telemetry
* support tickets
* roadmap gaps
* incomplete modules
* duplicated code
* API contract mismatches

Discovery must be separated from authorization.

Finding a possible improvement must never automatically authorize implementation.

**Exit Criteria:**

- [x] Cooldown system prevents duplicate discovery runs
- [x] Yield tracking counts findings per discovery pass
- [x] Diversity allocation rotates across discovery roles
- [x] Saturation detection identifies diminishing returns
- [ ] All 8 discovery roles produce findings
- [ ] Findings persist across restarts via findings_log

---

<!-- DELIVERABLE: id=2.F status=IN_PROGRESS tier=T1 modules=ticket_engine.py -->
## F. Ticket Deduplication and Evidence Validation

Before tickets enter the execution queue, CodeBot must:

* detect duplicate findings
* merge related evidence
* reject unsupported findings
* detect already-fixed problems
* associate related bugs
* identify dependencies
* determine affected modules
* classify severity
* calculate risk
* verify whether the issue actually exists

The system must aggressively prevent stale documentation from causing agents to reimplement already-completed work.

**Exit Criteria:**

- [x] SHA-256 evidence hashing for deduplication
- [ ] All duplicate patterns tested with synthetic cases
- [ ] Stale finding detection rejects already-fixed issues

---

<!-- DELIVERABLE: id=2.G status=IN_PROGRESS tier=T1 modules=implementation_planner.py,feature_decomposer -->
## G. Planning Pipeline

Before implementation, non-trivial tickets must receive an implementation plan containing:

* affected components
* architectural implications
* interfaces changed
* tests required
* security considerations
* backwards compatibility
* data migrations
* rollback path
* documentation updates
* dependency ordering
* expected artifacts

Planning depth should scale with risk.

**Exit Criteria:**

- [x] Feature decomposer processes ROADMAP.md
- [x] Implementation planner generates depth-scaled plans
- [ ] Plans include rollback strategies for medium+ risk
- [ ] Plan quality validated against synthetic tickets

---

<!-- DELIVERABLE: id=2.H status=DONE tier=T0 modules=role_registry.py,role_prompt.py -->
## H. Specialized Agent Roles

Move away from hardcoding behavior around individually named bots.

The architecture should support roles such as:

### Discovery

* Bug Hunter
* Security Auditor
* Architecture Auditor
* Performance Auditor
* Test Gap Auditor
* Documentation Auditor
* Dependency Auditor
* UX Auditor

### Planning

* Ticket Triager
* Dependency Planner
* Implementation Planner
* Architecture Planner

### Implementation

* General Implementer
* Backend Implementer
* Frontend Implementer
* Test Implementer
* Migration Implementer
* Documentation Implementer

### Review

* Correctness Reviewer
* Security Reviewer
* Architecture Reviewer
* Test Reviewer
* Performance Reviewer
* Simplicity Reviewer
* Documentation Reviewer

### Control

* Scheduler
* Quality Gate
* Conflict Resolver
* Budget Controller
* Human Escalation Controller
* Planning Coordinator

The core abstraction should eventually resemble:

```text
ROLE + TASK + MODEL PROFILE + TOOL POLICY
```

rather than:

```text
BOT NAME + FIXED PROMPT + FIXED MODEL
```

**Exit Criteria:**

- [x] Dynamic role assembly via role_registry.py
- [x] 28 roles defined with model requirements and tool policies
- [x] PLANNING_ROLES category added
- [ ] All roles tested with role_prompt.py generation

---

<!-- DELIVERABLE: id=3 status=IN_PROGRESS tier=T1 modules=role_registry.py,orchestrator.py -->
# 3. Model Independence

CodeBot must not depend on a single AI provider or model family.

Create a roadmap toward a model routing layer based on capabilities such as:

```text
reasoning: low | medium | high
coding: basic | advanced
context: small | large
cost_class: cheap | standard | premium
latency: interactive | background
security_review: true/false
```

CodeBot should select the cheapest model capable of safely completing the task.

The system should support:

* multiple commercial providers
* local/open models where practical
* fallback models
* provider outages
* capability testing
* per-model success statistics
* per-model cost statistics

Model choice must become replaceable infrastructure.

---

<!-- DELIVERABLE: id=4 status=IN_PROGRESS tier=T0 modules=quality_gate.py -->
# 4. Deterministic Verification

AI opinion must never be the primary proof that work is correct.

CodeBot must integrate deterministic evidence from:

* unit tests
* integration tests
* E2E tests
* linters
* type checkers
* compilers
* formatters
* SAST
* dependency scanners
* fuzzing
* schema validation
* benchmarks
* migration tests
* browser automation
* API contract tests
* build systems
* container checks
* platform compatibility tests

Desired pattern:

```text
AI hypothesis
    ↓
deterministic verification
    ↓
AI interpretation
    ↓
deterministic acceptance criteria
```

---

<!-- DELIVERABLE: id=5 status=IN_PROGRESS tier=T0 modules=gatekeeper.py,quality_gate.py -->
# 5. Central Quality Gate

Implementers and reviewers must not directly declare tickets complete.

A central Quality Gate should make that decision.

The roadmap should lead toward policy-driven requirements such as:

```yaml
required:
  build: pass
  lint: pass
  unit_tests: pass
  regression_tests: pass

conditional:

  security_boundary:
    security_review: required
    adversarial_test: required

  api_change:
    contract_tests: required

  data_migration:
    migration_test: required
    rollback_test: required

  performance_sensitive:
    benchmark: required

  documentation_impact:
    documentation_review: required
```

A ticket is complete only when its acceptance criteria and required quality gates are verified.

**Exit Criteria:**

- [x] quality_gate.py loads and evaluates YAML gate policies
- [x] gatekeeper.py acts as central completion authority
- [ ] Gatekeeper blocks transitions when required checks fail
- [ ] All gate policies tested with synthetic ticket scenarios

---

<!-- DELIVERABLE: id=6 status=IN_PROGRESS tier=T1 modules=role_registry.py,adaptive_scheduler.py -->
# 6. Adversarial Multi-Agent Review

Review agents should intentionally have conflicting incentives.

Examples:

```text
IMPLEMENTER
"Make the requested change work."

SECURITY REVIEWER
"Find a way to exploit or abuse it."

TEST REVIEWER
"Find behavior the tests failed to cover."

ARCHITECTURE REVIEWER
"Find coupling, boundary violations, or technical debt."

PERFORMANCE REVIEWER
"Find scalability or resource regressions."

SIMPLICITY REVIEWER
"Find unnecessary complexity."

DOCUMENTATION REVIEWER
"Find claims that are no longer true."
```

CodeBot must resolve findings before completion.

The roadmap should eventually support independent reviewers using different model families where appropriate to reduce correlated mistakes.

---

<!-- DELIVERABLE: id=7 status=IN_PROGRESS tier=T1 modules=ticket_engine.py,adaptive_scheduler.py -->
# 7. Autonomous Rework Loop

Failed quality gates must automatically generate bounded rework.

Example:

```text
IMPLEMENT
   ↓
SECURITY REVIEW FAIL
   ↓
REWORK
   ↓
TEST
   ↓
SECURITY REVIEW
   ↓
VERIFY
```

Prevent infinite agent loops using:

* attempt limits
* cost limits
* failure classification
* escalation thresholds
* human-review thresholds

---

<!-- DELIVERABLE: id=8 status=IN_PROGRESS tier=T2 modules=docs/,documentation_auditor -->
# 8. Documentation as Operational Memory

Documentation must become part of system state, not an afterthought.

CodeBot should maintain:

* architecture docs
* module docs
* API contracts
* capability inventories
* roadmap status
* bug status
* security assumptions
* test strategy
* data models
* deployment instructions

But it must distinguish:

```text
AUTHORITATIVE HUMAN POLICY

GENERATED IMPLEMENTATION STATE

PLANNING DOCUMENTATION

HISTORICAL RECORDS
```

Avoid multiple documents independently claiming ownership of the same implementation status.

The roadmap should explicitly address documentation drift.

---

<!-- DELIVERABLE: id=9 status=IN_PROGRESS tier=T2 modules=event_log.py,cost_tracker.py -->
# 9. Artifact Provenance

Every autonomous change should eventually be traceable.

CodeBot should be able to answer:

> Why did this line of code change?

with evidence such as:

```text
Commit
Ticket
Discovery source
Evidence
Planner
Implementer
Model/provider
Reviewers
Test results
Security results
Documentation updates
Cost
Attempts
Outcome
Rollback status
```

Design an immutable or append-only event/provenance trail suitable for debugging and auditing CodeBot itself.

---

<!-- DELIVERABLE: id=10 status=DONE tier=T0 modules=lease_state.py,event_log.py -->
# 10. Runtime State

Separate CodeBot operational state from project source.

CodeBot runtime state may include:

```text
runs
tickets
leases
workers
model calls
costs
artifacts
reviews
quality results
failures
outcomes
metrics
```

Target repositories should store accepted project artifacts and project configuration—not transient orchestration state.

---

<!-- DELIVERABLE: id=11 status=IN_PROGRESS tier=T1 modules=dependency_graph.py,conflict_detector.py,adaptive_scheduler.py -->
# 11. Concurrency and Safe Parallel Development

CodeBot must eventually support many cheap agents operating concurrently without corrupting the repository.

The roadmap should cover:

* isolated worktrees/branches
* file ownership or leases
* merge conflict prevention
* ticket dependency locking
* stale branch detection
* integration queues
* merge sequencing
* automatic rebasing
* conflicting ticket detection
* shared dependency coordination
* concurrency limits
* rollback

The goal is safe parallel work, not merely many active agents.

**Exit Criteria:**

- [x] Conflict detection identifies overlapping file modifications
- [x] dependency_graph.py orders tickets with cycle detection
- [x] lease_state.py provides distributed coordination
- [ ] Integration queue merges changes in correct order
- [ ] Stale branch detection and cleanup tested

---

<!-- DELIVERABLE: id=12 status=IN_PROGRESS tier=T0 modules=tool_policy.py,web_tools.py -->
# 12. Security Model

Treat CodeBot as privileged development infrastructure.

The roadmap must include:

* least-privilege tool access
* sandboxed execution
* filesystem restrictions
* command restrictions
* secret isolation
* network restrictions
* prompt-injection defense
* malicious repository content handling
* dependency supply-chain protection
* protected branches
* protected project constitution
* signed or auditable artifacts where justified
* immutable audit history
* human approval for dangerous actions
* environment isolation between projects

CodeBot should assume that source repositories may contain untrusted instructions or malicious dependencies.

---

<!-- DELIVERABLE: id=13 status=DONE tier=T1 modules=risk_classifier.py -->
# 13. Autonomy Levels

Implement project-selectable autonomy levels.

Example:

```text
LEVEL 0 — Analysis only

LEVEL 1 — Discover issues and create tickets

LEVEL 2 — Implement inside isolated branches

LEVEL 3 — Implement, review, and produce merge-ready changes

LEVEL 4 — Automatically merge low-risk verified changes

LEVEL 5 — Continuous autonomous maintenance
```

Define exactly what actions each level permits.

Risk policy must override autonomy level when necessary.

---

<!-- DELIVERABLE: id=14 status=PLANNED tier=T2 modules= -->
# 14. Human Approval Model

Do not require humans for ordinary low-risk work.

Use risk-based escalation.

Examples of potentially autonomous work:

* documentation corrections
* test additions
* lint fixes
* safe refactors
* known bug fixes
* low-risk UI corrections

Examples requiring stronger review or humans:

* authentication architecture
* authorization boundaries
* cryptography
* destructive migrations
* billing
* secrets
* licensing
* security-policy relaxation
* project constitution changes
* major architecture changes
* irreversible production actions

---

<!-- DELIVERABLE: id=15 status=IN_PROGRESS tier=T2 modules=cost_tracker.py,token_budget.py,scheduler_metrics.py -->
# 15. Economics Engine

CodeBot's strategic advantage should be measured in quality per dollar.

Track per ticket:

* model/provider
* tokens
* monetary cost
* wall-clock time
* attempts
* rework count
* reviewer findings
* tests added
* human intervention
* escaped regressions
* rollback
* final outcome

The platform should eventually learn:

* which model performs best for which task
* which tasks cheap models handle reliably
* when stronger reasoning is justified
* expected cost before execution
* expected rework probability
* optimal reviewer combinations

Primary optimization target:

> Reliable accepted engineering work per dollar.

**Exit Criteria:**

- [x] Budget config integrated in scheduler
- [x] cost_tracker.py records per-ticket economics
- [x] token_budget.py manages fleet-wide budget ledger
- [ ] scheduler_metrics.py reports cost-per-ticket trends
- [ ] Model routing uses cost history for selection

---

<!-- DELIVERABLE: id=16 status=IN_PROGRESS tier=T2 modules=rl_engine.py,metrics_collector.py,prompt_optimizer.py -->
# 16. Learning and Outcome Feedback

Do not assume online model retraining is necessary.

Initially build evidence-based optimization around historical outcomes.

Capture:

```text
task class
models used
plans
implementation attempts
review failures
quality results
production outcomes
human intervention
cost
time
rollback
```

Use this to improve:

* routing
* ticket planning
* reviewer selection
* risk classification
* confidence calibration
* estimated cost
* expected difficulty

Any future learning system must remain auditable and reversible.

**Exit Criteria:**

- [x] metrics_collector.py gathers per-agent telemetry
- [x] rl_engine.py runs epsilon-greedy bandit optimization
- [ ] Prompt optimizer scheduled for periodic prompt improvement
- [ ] Outcome feedback influences model selection

---

<!-- DELIVERABLE: id=17 status=IN_PROGRESS tier=T1 modules=codebot_adapter.py -->
# 17. Self-Improvement

CodeBot may eventually work on CodeBot itself.

This must have stronger safeguards than ordinary project work.

Define a roadmap toward controlled self-improvement with:

* isolated branches
* extra reviewers
* regression suites
* security review
* architecture review
* bootstrap recovery
* version rollback
* independent validation
* protected orchestrator components

CodeBot must never be able to silently remove the controls governing its own behavior.

**Exit Criteria:**

- [x] CodeBot completes tickets on itself
- [x] codebot_adapter.py enables self-referential operation
- [ ] Extra reviewers applied to self-modification tickets
- [ ] Bootstrap recovery tested after self-modification

---

<!-- DELIVERABLE: id=18 status=DONE tier=T0 modules=project_adapter.py,monitor_adapter.py,codebot_adapter.py -->
# 18. Portability Extraction from Monitor

The roadmap must explicitly include a staged extraction strategy.

Do NOT start by moving files to another repository.

First:

1. Inventory current BotNet/CodeBot components.
2. Classify each component as:

   * CORE
   * PROJECT ADAPTER
   * MONITOR-SPECIFIC
   * LEGACY
   * UNKNOWN
3. Move Monitor-specific assumptions behind a Monitor project profile.
4. Make CodeBot operate Monitor exclusively through standardized project configuration and adapters.
5. Prove that the CodeBot core has no Monitor-specific domain knowledge.
6. Create a second unrelated test application.
7. Run CodeBot against the second application.
8. Complete real autonomous tickets.
9. Fix portability issues.
10. Only then move CodeBot into an independent repository.

The physical repository split should be the final step of portability, not the first.

---

<!-- DELIVERABLE: id=19 status=PLANNED tier=T3 modules= -->
# 19. Second-Project Validation

Before CodeBot is considered portable, it must successfully operate on an unrelated application.

Build or select a disposable but realistic test application containing:

* backend
* frontend
* authentication
* database
* API
* tests
* deployment
* security requirements

Require CodeBot to demonstrate:

* repository inventory
* architecture understanding
* documentation generation
* bug discovery
* ticket creation
* duplicate detection
* feature implementation
* regression repair
* security review
* test generation
* documentation updates
* quality-gate closure

No Monitor-specific logic may be required.

---

<!-- DELIVERABLE: id=20 status=PLANNED tier=T3 modules= -->
# 20. Deep Application Validation

After basic portability, progressively challenge CodeBot with applications containing:

* RBAC
* multi-tenancy
* payments
* queues
* background jobs
* third-party APIs
* migrations
* real-time updates
* file storage
* reporting
* complex workflows
* plugins
* multiple repositories
* infrastructure-as-code
* security boundaries

The roadmap should define benchmark projects representing increasing difficulty.

---

<!-- DELIVERABLE: id=21 status=PLANNED tier=T4 modules= -->
# 21. Application Bootstrap / Greenfield Development

CodeBot must eventually support creating applications, not only maintaining existing ones.

Long-term flow:

```text
BUSINESS IDEA
     ↓
REQUIREMENTS
     ↓
PROJECT CONSTITUTION
     ↓
ARCHITECTURE
     ↓
DATA MODEL
     ↓
API CONTRACT
     ↓
SECURITY MODEL
     ↓
ROADMAP
     ↓
TICKETS
     ↓
IMPLEMENTATION
     ↓
TESTING
     ↓
DEPLOYMENT
```

Human approval should occur at major architectural/product checkpoints while ordinary engineering proceeds autonomously.

---

<!-- DELIVERABLE: id=22 status=PLANNED tier=T3 modules=telemetry.py -->
# 22. Production Feedback Loop

Long term, CodeBot should consume evidence from deployed software:

```text
production error
monitoring alert
security finding
performance regression
support request
customer feedback
        ↓
structured evidence
        ↓
ticket candidate
        ↓
normal CodeBot pipeline
```

Do not permit production telemetry to directly trigger unsafe code changes.

All changes still go through tickets and quality gates.

---

<!-- DELIVERABLE: id=23 status=PLANNED tier=T4 modules= -->
# 23. Business-App Factory Capability

Once core autonomous engineering is reliable, roadmap toward rapidly producing and validating business applications.

Support reusable capabilities such as:

* authentication
* RBAC
* organizations/workspaces
* billing
* subscriptions
* transactional email
* dashboards
* reports
* audit logs
* notifications
* webhooks
* API keys
* onboarding
* admin interfaces
* monitoring
* backups
* deployment
* analytics

These should become reusable templates/components without forcing every application into one architecture.

---

<!-- DELIVERABLE: id=24 status=PLANNED tier=T4 modules= -->
# 24. Customer Development / Agency Workflow

CodeBot may eventually power a custom-software development business.

Plan for:

```text
customer requirement
     ↓
structured scope
     ↓
acceptance criteria
     ↓
ticket graph
     ↓
implementation
     ↓
customer acceptance
     ↓
maintenance
```

Useful future capabilities include:

* scope tracking
* requirement traceability
* change-request detection
* customer acceptance criteria
* release notes
* maintenance plans
* support ticket ingestion
* effort/cost estimation

Do not let business-management features distract from autonomous engineering until the core is mature.

---

<!-- DELIVERABLE: id=25 status=DONE tier=T0 modules=adaptive_scheduler.py,scheduler_config.py,pipeline_state.py,queue_pressure.py,work_scorer.py,discovery_manager.py,conflict_detector.py,scheduler_metrics.py -->
# 25. Adaptive Concurrency Scheduling

A 30-slot shared worker pool scheduler dynamically allocates roles based on queue pressure. Rather than running a fixed number of workers per role, the scheduler continuously evaluates demand across all ticket queues and redistributes capacity in real time.

Core components:

```text
adaptive_scheduler.py    — Main scheduling loop, slot allocation
scheduler_config.py      — Tunable parameters (max slots, pressure thresholds)
pipeline_state.py        — Per-ticket state tracking across pipeline stages
queue_pressure.py        — Measures demand per role and priority tier
work_scorer.py           — Scores pending work by urgency, risk, and dependencies
discovery_manager.py     — Coordinates discovery passes with cooldown
conflict_detector.py     — Prevents overlapping modifications
scheduler_metrics.py     — Emits throughput and utilization telemetry
```

The scheduler references spec sections §1-54 for the complete concurrency model, including worktree isolation, lease management, dependency ordering, and merge sequencing.

Key behaviors:

* Slots shift from implementation to review when review queues grow
* Discovery roles yield to implementation during high-pressure periods
* Conflict detection prevents duplicate effort across concurrent workers
* Queue pressure metrics feed back into slot allocation decisions
* Pipeline state persists across scheduler restarts

This replaces a static per-role worker model with a demand-driven shared pool.

---

<!-- DELIVERABLE: id=26 status=DONE tier=T1 modules=codebotctl -->
# 26. Lifecycle Management

The `codebotctl` script provides safe operational control over CodeBot processes.

Supported operations:

```text
codebotctl start     — Launch CodeBot with proper initialization
codebotctl stop      — Graceful shutdown with state preservation
codebotctl restart   — Stop then start with clean state
codebotctl clear     — Remove runtime state without touching project files
codebotctl status    — Report current operational state
```

Lifecycle management handles:

* process spawning and monitoring
* graceful shutdown (finish current work, save state)
* orphan process detection
* state directory cleanup
* startup readiness checks
* signal handling (SIGTERM, SIGINT)
* log rotation coordination

The script must never delete project artifacts, tickets, or configuration. Only transient runtime state (leases, worker assignments, in-progress runs) is affected by clear operations.

---

<!-- DELIVERABLE: id=27 status=PLANNED tier=T2 modules= -->
# 27. Reliability Requirements

CodeBot must fail safely.

Address:

* agent crashes
* provider outages
* malformed model responses
* partial work
* interrupted runs
* corrupted state
* stale leases
* Git conflicts
* failing tests
* missing tools
* unavailable dependencies
* cost exhaustion
* rate limits
* context exhaustion

Runs should be resumable and auditable.

---

<!-- DELIVERABLE: id=28 status=PLANNED tier=T2 modules= -->
# 28. Observability

CodeBot itself must be measurable.

Provide metrics for:

* active agents
* queue depth
* ticket throughput
* ticket age
* attempts
* failures
* model latency
* provider failures
* cost
* quality-gate failures
* human escalations
* merge conflicts
* review findings
* regression rate

Add dashboards only after useful metrics exist.

---

<!-- DELIVERABLE: id=29 status=PLANNED tier=T2 modules= -->
# 29. Success Metrics

Do NOT optimize around:

* lines of code
* commits
* raw ticket count
* number of agents
* tokens consumed

Primary metrics should include:

```text
Autonomous completion rate

First-pass quality-gate success

Human intervention rate

Human engineering minutes per accepted ticket

Escaped regression rate

Security regression rate

Rework rate

Rollback rate

Mean accepted-ticket cost

Mean completion time

Documentation drift rate

Architecture violation rate

Cost per validated application/business experiment
```

The ultimate metric is:

> How much verified, maintainable engineering output can CodeBot produce with minimal human engineering intervention?

---

<!-- DELIVERABLE: id=30 status=PLANNED tier=T2 modules= -->
# 30. Roadmap Structure

Organize the roadmap into dependency-driven tiers similar to:

```text
TIER 1 — Stabilize Existing CodeBot
TIER 2 — State, Ticketing & Evidence
TIER 3 — Quality Gates & Deterministic Verification
TIER 4 — Agent Roles & Adversarial Review
TIER 5 — Project Contract & Monitor Decoupling
TIER 6 — Portable Tool/Language Adapters
TIER 7 — Model Routing & Cost Optimization
TIER 8 — Autonomous Pipeline Integration
TIER 9 — Independent Project Validation
TIER 10 — Deep Application / Greenfield Development
TIER 11 — Production Feedback & Continuous Maintenance
TIER 12 — Software Factory / Customer Development
TIER 13 — Controlled Self-Improvement & Maturity
```

These are suggested themes, not mandatory exact boundaries.

Modify them if repository evidence supports a better dependency structure.

---

<!-- DELIVERABLE: id=31 status=PLANNED tier=T2 modules= -->
# 31. Every Roadmap Item Must Include

For each feature/task include:

| Field      | Requirement                    |
| ---------- | ------------------------------ |
| ID         | Stable identifier              |
| Feature    | Clear name                     |
| Problem    | Why it exists                  |
| Depends On | Explicit dependencies          |
| Risk       | Low / Medium / High / Critical |
| Status     | Standardized lifecycle state   |
| Acceptance | Objective completion criteria  |
| Evidence   | What proves completion         |

Do not use vague completion criteria such as:

> "system works"

Instead use measurable tests.

---

<!-- DELIVERABLE: id=32 status=PLANNED tier=T2 modules= -->
# 32. Standard Status Vocabulary

Use only clearly defined statuses.

Recommended:

```text
PLANNED
DESIGNING
DESIGN COMPLETE
IMPLEMENTING
IMPLEMENTED
VERIFYING
VERIFIED
PRODUCTION VALIDATED
BLOCKED
DEFERRED
```

`DONE` should mean at minimum:

> VERIFIED against documented acceptance criteria.

Do not mark an item complete merely because an ADR, skeleton, API, module, or partial implementation exists.

---

<!-- DELIVERABLE: id=33 status=PLANNED tier=T2 modules= -->
# 33. Tier Exit Criteria

Every tier must have measurable exit criteria.

Examples:

```text
Tier X Exit Criteria:

- 100% tickets use normalized schema
- duplicate detection tested
- queue recovers after crash
- no ticket can skip quality gate
- security-boundary changes require security review
- 95% of synthetic tickets survive pipeline without human intervention
```

Do not allow tier completion when mandatory items remain incomplete.

---

<!-- DELIVERABLE: id=34 status=PLANNED tier=T2 modules= -->
# 34. Version Milestones

Include a version plan.

Example:

```text
0.1 — Existing BotNet baseline
0.2 — Reliable tickets + orchestration
0.3 — Quality gates + review swarm
0.4 — Project contract + Monitor decoupling
0.5 — Independent repository portability
0.6 — Multi-language project support
0.7 — Autonomous deep web-app development
0.8 — Production feedback + continuous maintenance
0.9 — Software factory
1.0 — Production autonomous engineering platform
```

Adjust according to actual repository maturity.

Every version must identify:

* theme
* included tiers
* must-have features
* measurable release criteria
* current status

---

<!-- DELIVERABLE: id=35 status=PLANNED tier=T2 modules= -->
# 35. Dependency Graph

At the end of the roadmap, include a dependency graph showing tier dependencies and major feature dependencies.

Example:

```text
Ticket Schema
     ↓
Ticket Engine
     ↓
Quality Gate
     ↓
Review System
     ↓
Autonomous Pipeline
     ↓
Portable Project Contract
     ↓
Independent Project Validation
```

Prevent roadmap ordering based purely on feature excitement.

---

<!-- DELIVERABLE: id=36 status=PLANNED tier=T2 modules= -->
# 36. Explicit Non-Goals

Add a section defining what must NOT distract the project yet.

Potential examples:

* consumer no-code UI
* visual drag-and-drop app builder
* mobile CodeBot app
* proprietary foundational model
* full IDE replacement
* massive plugin marketplace
* enterprise billing platform
* autonomous production deployment before quality gates
* automatic constitution modification

Keep CodeBot focused on engineering reliability first.

---

<!-- DELIVERABLE: id=37 status=PLANNED tier=T2 modules= -->
# 37. Architectural Principles

Create a permanent principles section.

At minimum include principles such as:

1. Models are replaceable.
2. Deterministic evidence outranks model confidence.
3. Agents cannot weaken acceptance criteria to finish work.
4. Discovery does not equal authorization.
5. Implementers cannot approve their own work.
6. High-risk changes require independent review.
7. Every accepted change must be traceable to a ticket.
8. Every ticket must have objective acceptance criteria.
9. Project policy outranks agent instructions.
10. Repository content is potentially untrusted.
11. CodeBot core contains no project-specific business logic.
12. Failed work must be recoverable.
13. Autonomous work must remain auditable.
14. Cost is a first-class engineering metric.
15. Human intervention should decrease without decreasing quality.
16. Documentation must describe actual system state.
17. CodeBot must remain functional when individual AI providers are unavailable.
18. CodeBot may improve itself only under stronger controls than normal projects.

---

<!-- DELIVERABLE: id=38 status=PLANNED tier=T2 modules= -->
# 38. Security-Sensitive Rule

Explicitly state:

> CodeBot must never silently lower a security requirement, disable a test, delete a failing test, loosen an acceptance criterion, remove validation, weaken authorization, suppress an error, or modify project policy merely to make a ticket pass.

Such actions require explicit justification and appropriate review.

---

<!-- DELIVERABLE: id=39 status=PLANNED tier=T3 modules= -->
# 39. Autonomous Development Benchmark

Define a benchmark suite measuring CodeBot over increasingly difficult tickets.

Example levels:

```text
LEVEL A
Documentation/test-only changes

LEVEL B
Localized bug fixes

LEVEL C
Cross-module features

LEVEL D
Database/API changes

LEVEL E
Authentication/security-boundary work

LEVEL F
Large architectural refactors

LEVEL G
Greenfield application implementation
```

Track autonomous success rate and human intervention for each category.

---

<!-- DELIVERABLE: id=40 status=PLANNED tier=T3 modules= -->
# 40. Major Milestone Before Extraction

Do not recommend physically separating CodeBot from Monitor until:

* Monitor operates through a standard CodeBot project profile
* no Monitor-specific behavior exists in CodeBot core
* standardized project configuration works
* ticket pipeline works end-to-end
* quality gates work
* deterministic tests are mandatory
* at least one unrelated repository works successfully

Then extraction should primarily become repository/package movement rather than architectural redesign.

---

<!-- DELIVERABLE: id=41 status=PLANNED tier=T3 modules= -->
# 41. Major Milestone for Portable CodeBot

CodeBot is not considered genuinely portable until it can autonomously complete at least:

> 20–50 meaningful tickets against a second unrelated application

with:

* correct implementation
* tests
* security review
* documentation
* quality-gate verification
* no Monitor-specific modifications to core

---

<!-- DELIVERABLE: id=42 status=PLANNED tier=T3 modules= -->
# 42. Major Milestone for Production CodeBot

Define a long-term milestone approximately like:

> CodeBot completes 100 consecutive non-trivial accepted engineering tickets across multiple projects without human code modification, without security regression, without architectural-policy violations, with complete provenance and measurable evidence for every accepted ticket.

Do not require zero human product decisions.

The goal is eliminating routine engineering intervention, not eliminating human ownership.

---

<!-- DELIVERABLE: id=43 status=PLANNED tier=T4 modules= -->
# 43. Software Factory End State

The eventual system should support:

```text
IDEA / REQUIREMENT
       ↓
PROJECT CONTRACT
       ↓
ARCHITECTURE
       ↓
ROADMAP
       ↓
AUTOMATIC TICKET GRAPH
       ↓
IMPLEMENTATION SWARM
       ↓
TEST / SECURITY / REVIEW SWARM
       ↓
QUALITY GATE
       ↓
DEPLOYMENT
       ↓
PRODUCTION OBSERVATION
       ↓
NEW TICKETS
       ↓
CONTINUOUS MAINTENANCE
```

At maturity, a human should spend most of their time on:

* product direction
* customer needs
* business decisions
* architecture exceptions
* security exceptions
* high-risk approvals

rather than routine coding.

---

<!-- DELIVERABLE: id=44 status=PLANNED tier=T2 modules= -->
# 44. Required Output

Produce the complete `ROADMAP.md`.

It must include:

1. What production-ready CodeBot means
2. Current-state gap analysis
3. Priority/dependency tiers
4. Detailed feature tables
5. Objective acceptance criteria
6. Tier exit criteria
7. Version milestones
8. Dependency graph
9. Portability/extraction milestones
10. Autonomous-development benchmark
11. Security/governance requirements
12. Explicit non-goals
13. Success metrics
14. How developers and autonomous agents should use the roadmap

Use repository evidence wherever possible.

Do not invent completed functionality.

If implementation state is uncertain, mark it:

```text
NEEDS VERIFICATION
```

Do not treat documentation claims as proof of implementation.

Inspect source and tests before marking major capabilities verified.

---

<!-- DELIVERABLE: id=45 status=PLANNED tier=T2 modules= -->
# 45. Final Roadmap Philosophy

The roadmap must optimize toward this outcome:

> CodeBot becomes an autonomous engineering organization, not merely a collection of coding agents.

The core competitive advantage should eventually be:

```text
cheap interchangeable AI labor
        +
structured project knowledge
        +
evidence-backed ticketing
        +
specialized adversarial roles
        +
deterministic quality gates
        +
security governance
        +
continuous documentation
        +
cost-aware model routing
        +
production feedback
        =
reliable autonomous software engineering
```

Prioritize reliability, portability, maintainability, security, auditability, and cost-efficiency over raw code-generation speed.

The long-term measure of success is not how much code CodeBot writes.

It is:

> **How much production-quality software CodeBot can create and maintain, across unrelated projects, per dollar and per minute of human engineering attention.**
