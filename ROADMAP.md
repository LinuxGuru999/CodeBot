# Task: Build the Authoritative CodeBot Development Roadmap

<!--
ROADMAP_SCHEMA: v2.1
MACHINE_READABLE: true
PARSER_INSTRUCTIONS:
  - Each deliverable is marked with <!-- DELIVERABLE: id=X status=Y tier=Z modules=... -->
  - Status values: DONE, IN_PROGRESS, PLANNED, BLOCKED
  - Tier values: T0 (critical), T1 (core), T2 (important), T3 (enhancement), T4+ (future)
  - Feature decomposer agents: parse DELIVERABLE comments, skip DONE items, create tickets for IN_PROGRESS/PLANNED
  - Section format: "# N. Title" with subsections "### §N.A — Subtitle"
  - Machine-readable index: .codebot/roadmap_index.json (compact, pre-parsed)
  - Human readers: ignore HTML comments, read prose normally
LAST_AUDITED: 2026-09-18
TOTAL_DELIVERABLES: 94
DONE: 8
IN_PROGRESS: 17
PLANNED: 68
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
- [x] 29 roles defined with model requirements and tool policies (40 prompt files)
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
* QA-stage recommendations for dangerous actions (resolved autonomously)
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

<!-- DELIVERABLE: id=14 status=PLANNED tier=T2 modules=quality_gate.py,gatekeeper.py -->
# 14. QA-Stage Recommendation Model

No human escalation. All changes flow through the autonomous pipeline. Sensitive changes generate structured QA-stage recommendations that agents resolve through the normal adversarial review and rework loop.

Use risk-based QA recommendations.

Examples of fully autonomous work:

* documentation corrections
* test additions
* lint fixes
* safe refactors
* known bug fixes
* low-risk UI corrections

Examples requiring QA-stage recommendations (resolved autonomously, never escalated to humans):

* authentication architecture → adversarial auth review recommendation
* authorization boundaries → access control verification recommendation
* cryptography → security hardening recommendation with threat assessment
* destructive migrations → migration safety recommendation with rollback plan
* billing → payment flow verification recommendation
* secrets → credential rotation recommendation
* licensing → dependency license audit recommendation
* security-policy relaxation → adversarial security review recommendation
* project constitution changes → architecture review swarm recommendation
* major architecture changes → ADR generation recommendation with alternatives
* irreversible production actions → staged rollout recommendation with health checks

QA recommendations are generated as tickets (type=recommendation) and processed through the normal pipeline. Resolution produces either: code change, ADR, or requirement clarification. No recommendation requires human intervention.

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
* QA-stage recommendation volume
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

---

<!-- DELIVERABLE: id=46 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py,quality_gate.py -->
# 46. Frontend Architecture and UI Engineering

CodeBot must be capable of building complete, production-quality frontend applications that are beautiful, elegant, and functional.

### §46.A — Component Architecture

- Generate framework-appropriate component hierarchies (React, Vue, Svelte, or vanilla web components based on project contract)
- Enforce single-responsibility components with clear prop interfaces
- Support compound component patterns, render props, and higher-order components where idiomatic
- Maintain a component registry mapping design system tokens to implementations

**Exit Criteria:**

- [ ] Agent can scaffold a multi-page component tree from a design specification
- [ ] Components pass isolated unit tests without mounting the full application
- [ ] Prop types/interfaces are strictly typed (TypeScript, Pydantic, or equivalent)

### §46.B — Design System Integration

- Parse and enforce design tokens (colors, spacing, typography, elevation, border radii)
- Generate CSS custom properties or theme provider configurations from token definitions
- Maintain visual consistency across all generated components via token references, never hardcoded values
- Support dark mode, high-contrast mode, and custom theme variants

**Exit Criteria:**

- [ ] All generated UI uses design tokens exclusively (zero hardcoded colors/spacing)
- [ ] Theme switching works without code changes
- [ ] Token drift detection alerts when generated code deviates from the design system

### §46.C — Responsive and Adaptive Layouts

- Generate mobile-first responsive layouts using CSS Grid, Flexbox, and container queries
- Support breakpoint systems defined in the project contract
- Handle touch targets, viewport units, and safe-area insets for mobile devices
- Generate adaptive layouts that restructure (not just resize) across breakpoints

**Exit Criteria:**

- [ ] Generated layouts pass responsive checks at 320px, 768px, 1024px, 1440px viewports
- [ ] No horizontal overflow at any supported breakpoint
- [ ] Touch targets meet minimum 44×44px requirement

### §46.D — Animation and Motion Design

- Generate CSS transitions and keyframe animations for micro-interactions
- Support orchestration libraries (Framer Motion, GSAP, or CSS-native) per project contract
- Respect `prefers-reduced-motion` media query automatically
- Implement skeleton loading states, optimistic UI updates, and transition choreography

**Exit Criteria:**

- [ ] All animations respect reduced-motion preferences
- [ ] Page transitions complete within 300ms perceived latency budget
- [ ] Loading states prevent layout shift (CLS < 0.1)

### §46.E — Visual Polish and Aesthetics

- Apply consistent spacing rhythm (4px/8px grid systems)
- Generate elegant typography scales with proper hierarchy
- Implement subtle shadows, gradients, and glassmorphism effects where appropriate
- Ensure visual harmony across all components and pages

**Exit Criteria:**

- [ ] Generated UI passes visual QA review against design references
- [ ] Typography scale follows modular ratio (major third, perfect fourth, etc.)
- [ ] Spacing is mathematically consistent throughout

---

<!-- DELIVERABLE: id=47 status=PLANNED tier=T0 modules=role_registry.py,quality_gate.py -->
# 47. Accessibility (a11y) Engineering

Web applications built by CodeBot must be usable by everyone, including people relying on assistive technologies.

### §47.A — WCAG Compliance

- Target WCAG 2.2 AA compliance as baseline, AAA where feasible
- Generate semantic HTML (proper heading hierarchy, landmark regions, list semantics)
- Ensure all interactive elements are keyboard-accessible with visible focus indicators
- Provide ARIA attributes only when native semantics are insufficient

**Exit Criteria:**

- [ ] Automated axe-core or equivalent scan reports zero critical/serious violations
- [ ] All pages navigable via keyboard alone (Tab, Enter, Escape, Arrow keys)
- [ ] Screen reader testing passes for all user flows

### §47.B — Color Contrast and Visual Accessibility

- Enforce minimum contrast ratios: 4.5:1 for normal text, 3:1 for large text and UI components
- Never rely solely on color to convey information (pair with icons, text, or patterns)
- Generate focus-visible styles that meet 3:1 contrast against adjacent colors
- Support forced-colors mode (Windows High Contrast)

**Exit Criteria:**

- [ ] All text/background combinations pass automated contrast checking
- [ ] Information conveyed by color has redundant non-color indicator
- [ ] Forced-colors mode renders all UI elements legibly

### §47.C — Form Accessibility

- Every input has a programmatically associated `<label>`
- Error messages are linked to inputs via `aria-describedby`
- Required fields marked with both `required` attribute and visual indicator
- Fieldsets and legends group related controls

**Exit Criteria:**

- [ ] All forms pass automated accessibility scanning
- [ ] Error recovery flow is screen-reader announced
- [ ] Autocomplete attributes correctly applied for browser autofill

---

<!-- DELIVERABLE: id=48 status=PLANNED tier=T0 modules=role_registry.py,api_runner.py,quality_gate.py -->
# 48. Web Security Hardening

Security must be structural, not bolted on. Every web application CodeBot produces must resist OWASP Top 10 attacks by default.

### §48.A — Input Validation and Sanitization

- Validate all input at the boundary (API routes, form handlers, webhook receivers)
- Use schema-driven validation (Zod, Pydantic, JSON Schema) — never manual string parsing
- Sanitize output contextually: HTML encoding for DOM insertion, URL encoding for hrefs, JavaScript encoding for inline scripts
- Reject invalid input early with descriptive error messages that don't leak internals

**Exit Criteria:**

- [ ] No endpoint accepts unvalidated input
- [ ] XSS payloads in all input fields are neutralized (reflected, stored, DOM-based)
- [ ] SQL injection payloads are rejected by parameterized queries or ORM

### §48.B — Authentication and Session Management

- Implement secure session handling: HttpOnly, Secure, SameSite cookies
- Support JWT with short expiry + refresh token rotation when stateless auth is required
- Hash passwords with bcrypt/scrypt/argon2 — never MD5/SHA1/SHA256 raw
- Implement account lockout, rate limiting on auth endpoints, and credential stuffing protection
- Support MFA/TOTP enrollment and verification flows

**Exit Criteria:**

- [ ] Session fixation attacks are prevented
- [ ] Auth tokens cannot be accessed via JavaScript (HttpOnly enforced)
- [ ] Password storage passes hashcat resistance benchmarks
- [ ] Brute force protection triggers within 5 failed attempts

### §48.C — Authorization and Access Control

- Implement server-side authorization on every protected route (never client-only)
- Support RBAC (Role-Based Access Control) and ABAC (Attribute-Based Access Control) patterns
- Enforce principle of least privilege: users see only what they're authorized to access
- Prevent IDOR (Insecure Direct Object Reference) via ownership checks on every resource access

**Exit Criteria:**

- [ ] Horizontal privilege escalation blocked (user A cannot access user B's resources)
- [ ] Vertical privilege escalation blocked (regular user cannot access admin routes)
- [ ] Missing authorization checks detected by automated scanning

### §48.D — HTTP Security Headers and Transport

- Set Content-Security-Policy (CSP) with strict-dynamic, nonce-based script allowlisting
- Configure X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy
- Enforce HTTPS via HSTS with preload directive
- Implement CORS policy matching the project's actual cross-origin requirements (never wildcard in production)
- Subresource Integrity (SRI) for all external scripts and stylesheets

**Exit Criteria:**

- [ ] Security headers score A+ on Mozilla Observatory or equivalent
- [ ] CSP blocks inline script execution without nonce
- [ ] Mixed content warnings: zero

### §48.E — CSRF, Clickjacking, and Injection Prevention

- Implement anti-CSRF tokens for all state-changing requests (or use SameSite cookies as defense-in-depth)
- Prevent clickjacking via X-Frame-Options: DENY and CSP frame-ancestors
- Parameterize all database queries; use prepared statements exclusively
- Escape all template output by default; require explicit opt-in for raw HTML

**Exit Criteria:**

- [ ] CSRF token validation fails closed on missing/invalid tokens
- [ ] Application cannot be embedded in iframes on unauthorized domains
- [ ] Zero SQL/NoSQL/LDAP/XML injection vectors in automated scanning

---

<!-- DELIVERABLE: id=49 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py -->
# 49. API Design and Backend Engineering

CodeBot must produce backend APIs that are consistent, versioned, documented, and resilient.

### §49.A — RESTful API Design

- Follow resource-oriented URL design (nouns, not verbs): `/users/{id}/orders`, not `/getOrders`
- Use correct HTTP methods: GET (read), POST (create), PUT/PATCH (update), DELETE (remove)
- Return appropriate status codes: 200, 201, 204, 400, 401, 403, 404, 409, 422, 429, 500
- Implement HATEOAS links for discoverability where appropriate
- Consistent error response format: `{error: {code, message, details}}`

**Exit Criteria:**

- [ ] All endpoints follow REST naming conventions
- [ ] Error responses are consistent across all routes
- [ ] Pagination implemented for all list endpoints (cursor-based preferred)

### §49.B — API Versioning and Evolution

- Support URL-path versioning (`/v1/users`) or header-based versioning per project contract
- Maintain backward compatibility within a major version
- Implement deprecation headers (`Deprecation`, `Sunset`) for retiring endpoints
- Generate changelog entries for API changes automatically

**Exit Criteria:**

- [ ] Breaking changes require a new major version
- [ ] Deprecated endpoints return Sunset headers with timeline
- [ ] v1 clients continue working after v2 deployment

### §49.C — Rate Limiting and Throttling

- Implement per-user, per-IP, and per-endpoint rate limiting
- Return 429 Too Many Requests with Retry-After header
- Support sliding window and token bucket algorithms
- Differentiate limits by plan tier or authentication level

**Exit Criteria:**

- [ ] Rate limit exceeded returns proper 429 with retry guidance
- [ ] Limits are configurable per project contract
- [ ] Rate limit state survives single-instance restart (Redis-backed or equivalent)

### §49.D — Middleware Pipeline

- Implement composable middleware chain: logging → auth → rate-limit → validation → handler → error-handling
- Support middleware ordering guarantees (security middleware always runs before business logic)
- Allow per-route middleware configuration
- Implement request/response transformation middleware (compression, serialization)

**Exit Criteria:**

- [ ] Middleware executes in deterministic order
- [ ] Unauthenticated requests are rejected before reaching business logic
- [ ] Request body parsing happens exactly once

### §49.E — WebSocket and Real-Time Communication

- Support WebSocket connections for real-time features (chat, notifications, live updates)
- Implement connection authentication and heartbeat/ping-pong keepalive
- Handle reconnection gracefully with exponential backoff on the client side
- Support channel/room-based pub-sub for targeted message delivery

**Exit Criteria:**

- [ ] WebSocket connections authenticate before accepting messages
- [ ] Disconnected clients auto-reconnect with message replay
- [ ] Server handles 10K concurrent WebSocket connections without degradation

---

<!-- DELIVERABLE: id=50 status=PLANNED tier=T1 modules=role_registry.py,quality_gate.py,api_runner.py -->
# 50. Testing Strategy for Web Applications

Comprehensive testing is non-negotiable. CodeBot must generate and maintain tests at every level.

### §50.A — Unit Testing

- Test every exported function, component, and utility in isolation
- Mock external dependencies (database, API calls, filesystem)
- Achieve >80% line coverage for business logic, >90% for security-critical paths
- Test edge cases: empty input, null values, boundary conditions, Unicode, extremely long strings

**Exit Criteria:**

- [ ] Unit test suite runs in <60 seconds
- [ ] Coverage thresholds enforced in CI (build fails below threshold)
- [ ] Zero flaky tests (tests pass deterministically regardless of execution order)

### §50.B — Integration Testing

- Test API endpoints end-to-end with real database (test containers or in-memory DB)
- Verify middleware pipeline behavior (auth rejection, rate limiting, validation errors)
- Test inter-service communication and event propagation
- Verify database migrations apply and rollback cleanly

**Exit Criteria:**

- [ ] All API routes have integration tests covering happy path and error cases
- [ ] Database state is isolated between tests (transaction rollback or fresh fixtures)
- [ ] External service failures are tested (timeout, 500, malformed response)

### §50.C — End-to-End (E2E) Testing

- Test critical user journeys through the full stack (browser → server → database → browser)
- Use Playwright, Cypress, or Selenium per project contract
- Test across browsers: Chrome, Firefox, Safari, Edge (latest two versions)
- Include visual regression testing for UI changes

**Exit Criteria:**

- [ ] Core user flows (signup, login, primary CRUD operations) have E2E coverage
- [ ] E2E tests run headlessly in CI
- [ ] Visual regression diffs reviewed before merge

### §50.D — Performance and Load Testing

- Benchmark API response times: p50 < 100ms, p95 < 500ms, p99 < 1s for standard endpoints
- Load test with expected peak concurrency (k6, Locust, or artillery)
- Profile memory usage and detect leaks under sustained load
- Test database query performance (N+1 detection, slow query alerts)

**Exit Criteria:**

- [ ] Performance budgets defined and enforced in CI
- [ ] No endpoint exceeds p95 budget under 2× expected load
- [ ] Memory growth flat over 1-hour sustained load test

### §50.E — Security Testing

- Run OWASP ZAP or equivalent DAST scanner against staging environment
- Perform dependency vulnerability scanning (npm audit, pip-audit, cargo audit)
- Execute fuzz testing on all input boundaries
- Verify security headers and TLS configuration programmatically

**Exit Criteria:**

- [ ] DAST scan reports zero high/critical findings
- [ ] Dependencies with known CVEs block deployment
- [ ] Fuzz testing runs for minimum 1 hour per release cycle

---

<!-- DELIVERABLE: id=51 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py,quality_gate.py -->
# 51. Performance Optimization

Web applications must be fast by default. CodeBot must engineer for performance at every layer.

### §51.A — Frontend Performance (Core Web Vitals)

- Target LCP < 2.5s, INP < 200ms, CLS < 0.1
- Implement code splitting and lazy loading for routes and heavy components
- Optimize images: WebP/AVIF format, responsive srcset, lazy loading below fold
- Minimize main-thread work: defer non-critical JS, use web workers for computation
- Preload critical resources, preconnect to third-party origins

**Exit Criteria:**

- [ ] Lighthouse performance score ≥ 90 on mobile and desktop
- [ ] Total bundle size tracked and budgeted per project contract
- [ ] Time to Interactive < 3.5s on 3G throttled connection

### §51.B — Backend Performance

- Implement caching layers: application cache (Redis/Memcached), HTTP cache (CDN, Cache-Control), database query cache
- Optimize database queries: proper indexing, query planning, N+1 prevention, connection pooling
- Use async/non-blocking I/O for network-bound operations
- Implement pagination at the database level (never load unbounded result sets into memory)

**Exit Criteria:**

- [ ] Database queries profiled; no full table scans on hot paths
- [ ] Cache hit ratio > 80% for repeated reads
- [ ] Connection pool sized appropriately (no connection starvation under load)

### §51.C — Asset Optimization and Delivery

- Minify CSS, JavaScript, and HTML in production builds
- Implement tree-shaking to eliminate dead code from bundles
- Generate and serve Brotli/gzip compressed assets
- Configure CDN distribution with appropriate cache-control and invalidation strategies
- Implement service workers for offline capability where specified

**Exit Criteria:**

- [ ] Production assets served compressed (Brotli preferred, gzip fallback)
- [ ] Unused code eliminated from production bundles (verified via bundle analyzer)
- [ ] CDN cache hit ratio > 95% for static assets

---

<!-- DELIVERABLE: id=52 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py -->
# 52. State Management and Data Flow

Complex web applications require disciplined state management to remain maintainable.

### §52.A — Client-Side State Architecture

- Choose state management approach appropriate to complexity: local state → context → store (Redux/Zustand/Pinia/Vuex)
- Normalize state shape to prevent duplication and inconsistency
- Implement optimistic updates with automatic rollback on server rejection
- Separate server state (React Query/SWR/RTK Query) from client-only state

**Exit Criteria:**

- [ ] State shape documented and enforced via types/schemas
- [ ] Optimistic updates revert correctly on failure
- [ ] No circular state dependencies

### §52.B — Server-Side State and Caching

- Implement stale-while-revalidate caching patterns
- Support cache invalidation via events, TTL, or tag-based purging
- Handle cache stampede protection (thundering herd) for popular resources
- Maintain cache coherence between application instances

**Exit Criteria:**

- [ ] Cache invalidation propagates within defined SLA (< 5s typical)
- [ ] Thundering herd scenarios handled via request coalescing
- [ ] Cache memory bounded with eviction policies (LRU/LFU)

### §52.C — Form State and Validation

- Implement progressive validation: inline on blur, comprehensive on submit
- Preserve form state across navigation (draft saving)
- Support complex form patterns: dynamic field arrays, conditional fields, multi-step wizards
- Debounce expensive validations (uniqueness checks, API lookups)

**Exit Criteria:**

- [ ] Form data persists across accidental navigation away
- [ ] Validation feedback appears within 300ms of relevant trigger
- [ ] Multi-step forms support forward/backward navigation without data loss

---

<!-- DELIVERABLE: id=53 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py -->
# 53. Error Handling and Resilience

Web applications must degrade gracefully. Every failure mode must be anticipated and handled.

### §53.A — Error Boundaries and Recovery

- Implement component-level error boundaries that prevent full-page crashes
- Display meaningful error UI with recovery actions (retry button, alternative navigation)
- Log errors with full context (component stack, user action, state snapshot) to monitoring
- Never show raw stack traces or internal error details to end users

**Exit Criteria:**

- [ ] Single component failure doesn't crash the entire page
- [ ] All errors logged with actionable debugging context
- [ ] User-facing error messages are helpful, not technical

### §53.B — Network Resilience

- Implement retry logic with exponential backoff and jitter for transient failures
- Detect offline state and queue mutations for later sync
- Handle partial failures gracefully (some items succeed, some fail in batch operations)
- Implement circuit breakers for downstream service dependencies

**Exit Criteria:**

- [ ] Transient network errors retried automatically (max 3 attempts with backoff)
- [ ] Offline mode queues writes and syncs on reconnection
- [ ] Circuit breaker opens after configurable failure threshold

### §53.C — Graceful Degradation

- Progressive enhancement: core functionality works without JavaScript where feasible
- Feature detection over browser sniffing
- Fallback content for unsupported features (video codecs, APIs, CSS features)
- Server-side rendering or static generation for SEO-critical and accessibility-critical pages

**Exit Criteria:**

- [ ] Core content accessible with JavaScript disabled
- [ ] Unsupported features show fallback rather than broken UI
- [ ] SSR/SSG pages render meaningful content before hydration

---

<!-- DELIVERABLE: id=54 status=PLANNED tier=T2 modules=role_registry.py,api_runner.py -->
# 54. Internationalization (i18n) and Localization (l10n)

Web applications targeting global audiences must support multiple languages and regional formats.

### §54.A — Translation Infrastructure

- Extract all user-facing strings to translation files (never hardcode display text)
- Support ICU MessageFormat for pluralization, gender, and selection
- Implement language negotiation: URL path, Accept-Language header, user preference, cookie
- Support right-to-left (RTL) layout mirroring

**Exit Criteria:**

- [ ] Zero hardcoded English strings in UI templates
- [ ] Pluralization rules correct for target locales
- [ ] RTL layout renders correctly without overlapping elements

### §54.B — Locale-Aware Formatting

- Format dates, times, numbers, and currencies according to user locale (Intl API)
- Handle timezone conversion: store UTC, display local
- Support locale-specific sorting and collation
- Handle name ordering, address formats, and phone number validation per region

**Exit Criteria:**

- [ ] Dates display correctly for all supported locales
- [ ] Currency amounts show correct symbol, decimal places, and grouping
- [ ] Timezone conversions accurate including DST transitions

---

<!-- DELIVERABLE: id=55 status=PLANNED tier=T2 modules=role_registry.py,api_runner.py -->
# 55. Deployment, CI/CD, and DevOps

CodeBot must produce applications that deploy reliably and recover automatically.

### §55.A — Containerization

- Generate Dockerfiles following best practices: multi-stage builds, minimal base images, non-root user
- Implement health check endpoints for container orchestration probes
- Configure graceful shutdown handling (SIGTERM → drain connections → exit)
- Support docker-compose for local development environments

**Exit Criteria:**

- [ ] Docker image size optimized (< 200MB for Node.js, < 100MB for Python/Go)
- [ ] Health check endpoint returns 200 within 5s of startup
- [ ] Container runs as non-root user

### §55.B — CI/CD Pipeline Generation

- Generate CI pipeline configurations (GitHub Actions, GitLab CI, or project-specified)
- Pipeline stages: lint → type-check → unit test → build → integration test → security scan → deploy
- Implement deployment strategies: blue-green, canary, rolling update per project contract
- Automate database migration execution during deployment

**Exit Criteria:**

- [ ] Full CI pipeline completes in < 15 minutes
- [ ] Failed security scan blocks deployment
- [ ] Rollback procedure tested and documented

### §55.C — Observability and Monitoring

- Implement structured logging (JSON) with correlation IDs spanning request lifecycle
- Expose Prometheus-compatible metrics endpoints (request count, latency histogram, error rate)
- Configure distributed tracing (OpenTelemetry) for multi-service architectures
- Set up alerting rules for error rate spikes, latency degradation, and availability drops

**Exit Criteria:**

- [ ] Every request traceable from ingress to database and back via correlation ID
- [ ] Alert fires within 1 minute of error rate exceeding threshold
- [ ] Dashboards auto-generated showing golden signals (latency, traffic, errors, saturation)

### §55.D — Database Migration Safety

- Generate reversible migrations for all schema changes
- Implement expand-and-contract pattern for zero-downtime column renames/removals
- Test migrations against production-scale data volumes before deployment
- Support seed data scripts for development and staging environments

**Exit Criteria:**

- [ ] Every migration has a tested rollback path
- [ ] Column removal takes minimum 2 deployment cycles (expand → migrate → contract)
- [ ] Migration execution time bounded and monitored

---

<!-- DELIVERABLE: id=56 status=PLANNED tier=T2 modules=role_registry.py,api_runner.py,quality_gate.py -->
# 56. Developer Experience (DX) and Code Quality

Code produced by CodeBot must be a pleasure for human developers to read, maintain, and extend.

### §56.A — Code Style and Consistency

- Enforce project-specific linting rules (ESLint, Ruff, Clippy) with zero warnings policy
- Apply consistent formatting via Prettier, Black, rustfmt, or gofmt
- Maintain import ordering conventions (stdlib → external → internal → relative)
- Enforce naming conventions: camelCase for JS/TS, snake_case for Python/Rust, PascalCase for types/components

**Exit Criteria:**

- [ ] `lint --fix` produces zero changes on generated code
- [ ] Formatter is idempotent (running twice produces identical output)
- [ ] Import ordering matches project convention

### §56.B — Type Safety and Contracts

- Use strict TypeScript (`strict: true`, no `any`) for frontend and Node.js backends
- Use Pydantic v2 or equivalent for Python API validation
- Generate OpenAPI/Swagger specs from route definitions automatically
- Maintain type synchronization between frontend and backend (shared types or code generation)

**Exit Criteria:**

- [ ] Zero `any` types in TypeScript codebase
- [ ] OpenAPI spec matches actual API behavior (verified by contract testing)
- [ ] Type changes in backend propagate to frontend types automatically

### §56.C — Documentation Generation

- Generate JSDoc/docstrings for all public functions, classes, and modules
- Maintain README with setup instructions, architecture overview, and contribution guide
- Auto-generate API reference documentation from route definitions and types
- Keep CHANGELOG updated with every merged change

**Exit Criteria:**

- [ ] All public APIs have documentation with examples
- [ ] README allows a new developer to run the project in < 5 minutes
- [ ] API docs stay synchronized with implementation (generated, not hand-written)

---

<!-- DELIVERABLE: id=57 status=PLANNED tier=T2 modules=role_registry.py,api_runner.py -->
# 57. SEO and Discoverability

Public-facing web applications must be discoverable by search engines and social platforms.

### §57.A — Technical SEO

- Generate semantic HTML with proper heading hierarchy (single H1, logical nesting)
- Implement canonical URLs to prevent duplicate content issues
- Generate XML sitemaps and robots.txt dynamically
- Support structured data (JSON-LD) for rich search results

**Exit Criteria:**

- [ ] Lighthouse SEO score ≥ 90
- [ ] All indexable pages have unique meta titles and descriptions
- [ ] Structured data validates against schema.org specifications

### §57.B — Social Sharing and Open Graph

- Generate Open Graph and Twitter Card meta tags for all shareable pages
- Support dynamic OG image generation for content-rich pages
- Implement proper canonical and alternate language link tags

**Exit Criteria:**

- [ ] Social preview cards render correctly on Facebook, Twitter, LinkedIn, Slack
- [ ] OG image dimensions meet platform requirements (1200×630)

### §57.C — Performance as SEO Signal

- Core Web Vitals directly impact search ranking; optimize accordingly
- Implement prerendering or SSR for crawler-facing content
- Minimize render-blocking resources in critical path

**Exit Criteria:**

- [ ] Core Web Vitals pass "Good" thresholds on real user data (CrUX)
- [ ] Search engine crawlers receive fully rendered content

---

<!-- DELIVERABLE: id=58 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py,quality_gate.py -->
# 58. Data Layer Engineering

Robust data modeling and persistence underpins every reliable web application.

### §58.A — Schema Design

- Normalize relational schemas to 3NF unless deliberate denormalization is justified
- Implement proper foreign keys, constraints, and indexes
- Use UUIDs or snowflake IDs for primary keys in distributed systems
- Design schemas for query patterns, not just storage patterns

**Exit Criteria:**

- [ ] All tables have primary keys, appropriate indexes, and foreign key constraints
- [ ] No N+1 query patterns in generated data access code
- [ ] Schema supports projected growth (partitioning strategy defined)

### §58.B — Data Validation and Integrity

- Validate at every layer: client form → API schema → database constraint
- Implement database-level constraints (NOT NULL, CHECK, UNIQUE) as final safety net
- Use transactions for multi-table mutations to prevent partial writes
- Implement soft deletes where audit trail is required

**Exit Criteria:**

- [ ] Invalid data cannot reach the database regardless of client behavior
- [ ] Multi-table operations are atomic (all succeed or all rollback)
- [ ] Deleted records preserved for audit period per project contract

### §58.C — Backup and Recovery

- Generate backup schedules appropriate to data criticality
- Test restore procedures regularly (backup without tested restore is not a backup)
- Implement point-in-time recovery for transactional databases
- Encrypt backups at rest

**Exit Criteria:**

- [ ] Restore from backup completes within RTO defined in project contract
- [ ] Backup integrity verified via automated checksum validation
- [ ] Point-in-time recovery tested quarterly

---

<!-- DELIVERABLE: id=59 status=PLANNED tier=T0 modules=role_registry.py,implementation_planner.py -->
# 59. Product Requirements Engineering

CodeBot must transform vague product ideas into structured, implementable requirements before any code is written.

### §59.A — Requirement Extraction and Structuring

- Extract functional requirements, non-functional requirements, and constraints from product descriptions
- Generate user personas, user stories with acceptance criteria, and use cases
- Model workflows, edge cases, failure cases, and assumptions explicitly
- Identify requirement conflicts and flag them for resolution before implementation
- Maintain requirement traceability: every ticket traces back to a requirement

**Exit Criteria:**

- [ ] Product idea input produces structured requirements document with zero ambiguity flags unresolved
- [ ] Every generated ticket has a traceable requirement ID
- [ ] Requirement conflicts detected and escalated before implementation begins

### §59.B — Scope Management

- Identify MVP boundaries: what ships first vs. what's deferred
- Separate future features from current scope explicitly
- Define out-of-scope items to prevent scope creep
- Version requirements: track changes to requirements over time
- Generate requirement dependency graphs for implementation ordering

**Exit Criteria:**

- [ ] MVP identification produces a shippable subset with clear deferral rationale
- [ ] Scope changes require explicit ticket creation with justification
- [ ] Requirement version history maintained for auditability

---

<!-- DELIVERABLE: id=60 status=PLANNED tier=T1 modules=role_registry.py,quality_gate.py -->
# 60. UX Design and Information Architecture

Applications must be easy and pleasant to use. UX quality is a first-class engineering concern, not an afterthought.

### §60.A — Information Architecture and Navigation

- Design navigation hierarchies appropriate to application complexity
- Map user journeys for primary workflows before implementation
- Apply progressive disclosure: show only what's needed at each step
- Design sensible defaults that work for 80% of users without configuration
- Support keyboard workflows for power users alongside mouse/touch

**Exit Criteria:**

- [ ] Primary user journeys documented before implementation begins
- [ ] Navigation depth ≤ 3 levels for primary workflows
- [ ] All destructive actions require confirmation with clear consequences

### §60.B — State Design: Empty, Loading, Error, Success

- Every view must handle: empty state, loading state, error state, success state
- Empty states guide users toward first action (not blank screens)
- Loading states communicate progress and prevent layout shift
- Error states are actionable: tell user what happened and what to do
- Success states confirm action completion with appropriate feedback

**Exit Criteria:**

- [ ] Zero views render blank content without guidance
- [ ] All async operations show loading indicators
- [ ] Error messages are actionable (not generic "something went wrong")
- [ ] Success feedback appears within 300ms of action completion

### §60.C — Forms, Validation UX, and Onboarding

- Forms use inline validation with clear, specific error messages
- Destructive actions visually differentiated (color, placement, confirmation)
- Onboarding flows guide new users to first value within 5 minutes
- Contextual help available where complexity demands it
- Search, filter, and sort UX follows established patterns

**Exit Criteria:**

- [ ] Form validation feedback appears within 300ms of relevant trigger
- [ ] Destructive actions require explicit confirmation with consequence description
- [ ] New user reaches primary value proposition within guided onboarding

---

<!-- DELIVERABLE: id=61 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py -->
# 61. Professional UI Design and Design Systems

CodeBot must produce interfaces that look deliberately designed, not merely generated.

### §61.A — Visual Hierarchy and Composition

- Apply consistent visual hierarchy: primary actions prominent, secondary actions subdued
- Use spacing systems (4px/8px grid) for mathematical consistency
- Implement proper typographic scales with clear heading hierarchy
- Compose layouts with intentional whitespace and visual breathing room
- Align all elements to a consistent grid system

**Exit Criteria:**

- [ ] Visual hierarchy review confirms primary actions are immediately identifiable
- [ ] Spacing audit shows zero arbitrary pixel values (all multiples of base unit)
- [ ] Typography scale follows modular ratio consistently

### §61.B — Design System Architecture

- Define design tokens: colors, spacing, typography, elevation, radii, shadows
- Generate CSS custom properties or theme configurations from token definitions
- Build component library with consistent interaction patterns and states
- Support dark/light themes via token swapping, not duplicated styles
- Document interaction patterns: hover, focus, active, disabled, loading, error

**Exit Criteria:**

- [ ] All UI references design tokens exclusively (zero hardcoded values)
- [ ] Component library covers: buttons, inputs, selects, cards, tables, dialogs, drawers, notifications, tooltips
- [ ] Theme switching works without component code changes
- [ ] Every component documents all supported states

### §61.C — Data Visualization and Complex UI

- Tables support sorting, filtering, pagination, column configuration
- Dashboards present key metrics with appropriate chart types
- Cards, dialogs, drawers used consistently per interaction pattern
- Charts are accessible (data tables as fallback, ARIA labels)
- Dense enterprise interfaces maintain readability at high information density

**Exit Criteria:**

- [ ] Tables handle 10K+ rows without degradation (virtualized or paginated)
- [ ] Chart types appropriate to data (not pie charts for 20 categories)
- [ ] Dense views maintain minimum touch targets and readable text sizes

---

<!-- DELIVERABLE: id=62 status=PLANNED tier=T1 modules=quality_gate.py,role_registry.py -->
# 62. UI Quality Review and Visual Regression Testing

UI quality must be verified deterministically, not just asserted.

### §62.A — UI Review Capability

- UI reviewer evaluates: visual hierarchy, alignment, spacing consistency, control clarity, information density
- Review uses browser rendering and screenshots where available, not text-only reasoning
- Review checks: loading/error/empty states present, tables usable, forms understandable
- Destructive actions visually differentiated from safe actions
- Responsive behavior verified at defined viewport matrix

**Exit Criteria:**

- [ ] UI review role defined with browser/screenshot tool access
- [ ] Review checklist covers all items in §7 of capability requirements
- [ ] UI review findings generate tickets with visual evidence

### §62.B — Visual Regression Testing

- Capture screenshot baselines for all pages/components at defined viewports
- Detect visual diffs between baseline and current rendering
- Viewport matrix: 320px, 768px, 1024px, 1440px minimum
- Detect: layout overflow, unexpected element movement, clipping, broken fonts, missing icons
- Dark/light theme regression detection

**Exit Criteria:**

- [ ] Screenshot baselines captured for all primary views
- [ ] Visual diff threshold configurable (pixel diff percentage)
- [ ] Theme regression tests pass for all supported themes
- [ ] Layout overflow detection catches horizontal scroll at all viewports

---

<!-- DELIVERABLE: id=63 status=PLANNED tier=T1 modules=quality_gate.py,role_registry.py -->
# 63. Browser E2E Testing

CodeBot must open the actual application and test it like a user.

### §63.A — E2E Test Infrastructure

- Integrate Playwright (or project-specified tool) for browser automation
- Test critical user journeys: register, login, logout, navigate, CRUD, search, upload, download
- Test across browsers: Chrome, Firefox, Safari, Edge (latest two versions)
- Support mobile layout testing via viewport emulation
- E2E tests run headlessly in CI

**Exit Criteria:**

- [ ] Core user flows (signup, login, primary CRUD) have E2E coverage
- [ ] E2E suite runs headlessly in < 5 minutes
- [ ] Tests pass across all supported browsers
- [ ] Mobile viewport tests catch responsive regressions

### §63.B — E2E Test Quality

- Tests verify user-visible outcomes, not implementation details
- Tests are resilient to minor UI changes (use data-testid, not CSS selectors)
- Failure screenshots captured automatically for debugging
- Tests isolated: no shared state between test cases
- Retry logic for flaky network conditions (max 2 retries)

**Exit Criteria:**

- [ ] Zero flaky E2E tests (deterministic pass/fail)
- [ ] Failure artifacts (screenshot + DOM snapshot) captured on every failure
- [ ] Test selectors use stable identifiers, not positional CSS

---

<!-- DELIVERABLE: id=64 status=PLANNED tier=T1 modules=role_registry.py,quality_gate.py -->
# 64. Multi-Tenancy and Organization Management

Applications serving multiple organizations/customers must enforce strict tenant isolation.

### §64.A — Tenant Architecture

- Implement tenant ownership: every resource belongs to exactly one tenant
- Tenant-scoped queries: all data access filtered by tenant context
- Tenant-specific settings, roles, and configuration
- User membership: users belong to one or more tenants with role per tenant
- Quota enforcement per tenant (storage, API calls, users)

**Exit Criteria:**

- [ ] Every database query includes tenant scope (no unscoped queries)
- [ ] Tenant isolation verified by automated cross-tenant access tests
- [ ] Tenant configuration changes don't affect other tenants

### §64.B — Tenant Isolation Testing

- Automated tests attempt cross-tenant data access (must fail)
- Test tenant-specific role permissions
- Verify audit trails are tenant-scoped
- Test billing ownership attribution
- Test tenant deletion/cleanup completeness

**Exit Criteria:**

- [ ] Cross-tenant access attempts return 403/404 (never data)
- [ ] Tenant isolation tests run on every deployment
- [ ] Tenant cleanup verified to remove all associated data

---

<!-- DELIVERABLE: id=65 status=PLANNED tier=T0 modules=role_registry.py,quality_gate.py -->
# 65. Security Toolchain and Adversarial Review

Security verification must combine automated tooling with adversarial human-equivalent review.

### §65.A — Automated Security Tooling

- Integrate SAST (Static Application Security Testing) into quality gates
- Dependency vulnerability scanning (npm audit, pip-audit, cargo audit) blocks deployment
- Secret scanning prevents credentials in source code
- License scanning flags incompatible licenses
- Container image scanning for known vulnerabilities
- No single scanner treated as proof of security; combine multiple signals

**Exit Criteria:**

- [ ] SAST runs on every PR; high/critical findings block merge
- [ ] Dependencies with known CVEs (CVSS ≥ 7) block deployment
- [ ] Secret scanning catches hardcoded credentials before commit
- [ ] Security tool results recorded in gate evaluation provenance

### §65.B — Adversarial Security Review Roles

- AUTH REVIEWER: attempts authentication bypass, session hijacking, token manipulation
- TENANT ISOLATION REVIEWER: attempts cross-tenant data access
- API SECURITY REVIEWER: attempts injection, IDOR, privilege escalation via API
- INPUT VALIDATION REVIEWER: attempts XSS, SQLi, command injection via all inputs
- SECRETS REVIEWER: searches for leaked credentials, weak crypto, insecure randomness
- High-risk changes (auth, payments, tenant boundaries) require independent security review

**Exit Criteria:**

- [ ] Adversarial review roles defined with explicit attack checklists
- [ ] High-risk tickets automatically assigned adversarial reviewers
- [ ] Review findings with severity ≥ HIGH block completion until resolved

---

<!-- DELIVERABLE: id=66 status=PLANNED tier=T1 modules=quality_gate.py,role_registry.py -->
# 66. Advanced Testing: Property-Based, Fuzz, Contract, and Test Quality

Testing must go beyond happy-path unit tests to catch subtle failures.

### §66.A — Property-Based and Fuzz Testing

- Generate property-based tests for parsers, serializers, and data transformations
- Fuzz all input boundaries: malformed data, oversized input, unusual Unicode, invalid schemas
- API fuzzing: unexpected methods, missing fields, wrong types, boundary values
- Concurrency race exploration for shared-state code

**Exit Criteria:**

- [ ] All parsers have property-based tests (roundtrip, idempotency)
- [ ] Fuzz testing runs minimum 1 hour per release cycle on input boundaries
- [ ] Zero crashes from malformed input (graceful error handling)

### §66.B — Contract Testing

- API contract tests verify request/response schemas match OpenAPI spec
- Consumer-driven contract tests for inter-service communication
- Database schema contract tests verify migrations match expected state
- Breaking change detection via contract diff

**Exit Criteria:**

- [ ] All API endpoints have contract tests matching OpenAPI spec
- [ ] Contract violations block deployment
- [ ] Breaking changes detected before merge (not after)

### §66.C — Test Quality Review

- Identify meaningless tests (assert true, no assertions, testing mocks)
- Detect implementation-coupled tests that break on refactor
- Flag missing boundary conditions, failure-path tests, permission tests
- Detect excessive mocking that hides real integration issues
- Track test flake rate and quarantine flaky tests

**Exit Criteria:**

- [ ] Test quality reviewer role identifies low-value tests
- [ ] Flaky test rate < 1% (quarantined tests don't count)
- [ ] Every test has meaningful assertions (no empty test bodies)

---

<!-- DELIVERABLE: id=67 status=PLANNED tier=T1 modules=quality_gate.py,role_registry.py -->
# 67. Performance Budgets and Load Testing

Performance must be engineered proactively with measurable budgets.

### §67.A — Performance Budget Definition

- Projects specify budgets: p95 API latency, page load time, bundle size, memory usage, query count
- Performance-sensitive tickets require benchmark comparison before/after
- Budget violations block completion (treated like test failures)
- Budgets versioned per project contract

**Exit Criteria:**

- [ ] Performance budgets defined in project contract (quality_gates.yaml)
- [ ] Performance regression > 10% on any budget metric blocks completion
- [ ] Benchmark results recorded in gate evaluation provenance

### §67.B — Load and Stress Testing

- Generate realistic load scenarios matching expected production traffic
- Concurrency testing: verify behavior under parallel requests
- Burst testing: verify behavior under sudden traffic spikes
- Saturation testing: find breaking point gracefully
- Rate-limit testing: verify 429 behavior under sustained overload
- Resource exhaustion testing: DB pool exhaustion, queue overload, memory pressure

**Exit Criteria:**

- [ ] Load tests simulate 2× expected peak traffic
- [ ] Graceful degradation verified (not hard crash) under overload
- [ ] Recovery after overload verified (system returns to normal)
- [ ] Rate limiting engages before resource exhaustion

---

<!-- DELIVERABLE: id=68 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py -->
# 68. Reliability Engineering

Applications must survive failures gracefully.

### §68.A — Retry, Timeout, and Circuit Breaker Patterns

- All external calls have explicit timeouts (never infinite wait)
- Retry with exponential backoff and jitter for transient failures
- Circuit breakers open after configurable failure threshold
- Idempotency keys for all state-changing operations
- Partial failure handling: some items succeed, some fail in batch operations

**Exit Criteria:**

- [ ] Zero unbounded waits in production code paths
- [ ] Circuit breaker opens within 5 consecutive failures
- [ ] Idempotent retries don't create duplicate resources

### §68.B — Failure Isolation and Recovery

- Single component failure doesn't cascade to full system failure
- Job recovery: failed background jobs retry with backoff, then alert
- Restart recovery: application starts cleanly after crash
- Cache failure: application degrades gracefully (slower, not broken)
- Database failure: read replicas or cached data served where possible
- Third-party API failure: fallback behavior defined per integration

**Exit Criteria:**

- [ ] Failure isolation verified: killing one service doesn't kill others
- [ ] Application recovers from crash within 30 seconds
- [ ] All third-party integrations have defined fallback behavior

---

<!-- DELIVERABLE: id=69 status=PLANNED tier=T1 modules=quality_gate.py,role_registry.py -->
# 69. Observability and Operational Diagnostics

Production applications must be diagnosable.

### §69.A — Structured Observability

- Structured logging (JSON) with correlation IDs spanning request lifecycle
- Metrics: request count, latency histogram, error rate, saturation indicators
- Distributed tracing for multi-service architectures (OpenTelemetry)
- Health endpoints: liveness, readiness, dependency health
- Alerting rules for error rate spikes, latency degradation, availability drops

**Exit Criteria:**

- [ ] Every request traceable via correlation ID from ingress to database
- [ ] Health endpoints respond within 5 seconds
- [ ] Alert fires within 1 minute of error rate exceeding threshold

### §69.B — Operational Diagnostics

- Every error path answers: "If this fails in production, how will the operator know why?"
- Diagnostics added during implementation, not after incidents
- Error messages include: what happened, what was attempted, what to check next
- Audit logging for security-relevant operations
- Structured error context: component stack, user action, state snapshot

**Exit Criteria:**

- [ ] Zero error paths that silently swallow failures
- [ ] All errors include actionable debugging context
- [ ] Audit log captures: actor, action, resource, timestamp, correlation ID

---

<!-- DELIVERABLE: id=70 status=PLANNED tier=T1 modules=role_registry.py,quality_gate.py -->
# 70. Release Engineering and Configuration Management

Releases must be reproducible, validated, and reversible.

### §70.A — Release Process

- Semantic versioning policy enforced
- Changelogs generated from ticket/commit history
- Release candidates tested before production deployment
- Staged deployment: canary → percentage → full rollout
- Rollback procedure documented and tested
- Release provenance: every artifact traceable to source commit

**Exit Criteria:**

- [ ] Release process completes in < 30 minutes
- [ ] Rollback tested and completes in < 5 minutes
- [ ] Changelog accurate and complete for every release

### §70.B — Configuration Management

- Environment-specific configuration: development, testing, staging, production
- Configuration validated at startup (fail fast on invalid config)
- Safe defaults that work for development without configuration
- Secrets never in configuration files (injected via environment or secret manager)
- Configuration documentation: what each setting does, valid values, defaults

**Exit Criteria:**

- [ ] Application starts with zero configuration in development mode
- [ ] Invalid configuration produces clear error at startup (not runtime)
- [ ] Zero secrets in source code or configuration files

---

<!-- DELIVERABLE: id=71 status=PLANNED tier=T0 modules=tool_policy.py,quality_gate.py -->
# 71. Secrets Management

Secrets are security-critical infrastructure.

### §71.A — Secret Handling

- No secrets in source code (enforced by secret scanning)
- Secret injection via environment variables or secret manager
- Secret rotation supported without code changes
- Scoped credentials: minimum permissions per service
- Environment isolation: dev/test/staging/prod secrets never shared

**Exit Criteria:**

- [ ] Secret scanning blocks commits containing credentials
- [ ] Secret rotation completes without application code changes
- [ ] Each environment uses isolated credentials

---

<!-- DELIVERABLE: id=72 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py -->
# 72. Third-Party Integrations

External service integrations must be resilient and well-tested.

### §72.A — Integration Patterns

- API clients with retry, timeout, and circuit breaker patterns
- Webhook receivers with signature verification and idempotency
- Rate limit awareness: respect provider rate limits, queue excess requests
- Pagination handling for large result sets
- API version pinning: don't break when provider updates
- Sandbox/test mode support for development

**Exit Criteria:**

- [ ] All integrations have retry + timeout + circuit breaker
- [ ] Webhook signatures verified before processing
- [ ] Integration tests use sandbox/mock mode (not production APIs)

### §72.B — Integration Contract Tests

- Mock external services in tests (never call real APIs in CI)
- Contract tests verify request/response format matches provider docs
- Failure mode tests: timeout, 500, malformed response, rate limit
- Integration health monitoring in production

**Exit Criteria:**

- [ ] Zero real API calls in CI test suite
- [ ] All integration failure modes tested
- [ ] Integration health dashboard shows dependency status

---

<!-- DELIVERABLE: id=73 status=PLANNED tier=T2 modules=role_registry.py,api_runner.py -->
# 73. Domain Capabilities: Billing, Email, Search, Files, Time

Common application capabilities need reusable, well-tested patterns.

### §73.A — Billing and Payments

- Prefer established payment providers (Stripe, etc.) with hosted/tokenized flows
- Never implement raw card handling
- Support: subscriptions, one-time payments, trials, refunds, failed payments
- Webhook-driven state updates for payment events
- Entitlement enforcement: features gated by plan/subscription status

**Exit Criteria:**

- [ ] Payment handling uses provider-hosted flows (no raw card data)
- [ ] Payment webhooks verified and idempotent
- [ ] Entitlement checks enforced server-side on every protected resource

### §73.B — Email and Notifications

- Transactional email: verification, recovery, notifications
- Email templates with proper HTML/text variants
- Delivery failure handling with retry
- Notification preferences: user controls what they receive
- Unsubscribe compliance where required

**Exit Criteria:**

- [ ] Email delivery failures retried with backoff
- [ ] Unsubscribe links functional and compliant
- [ ] Notification preferences respected in all sends

### §73.C — Search, Files, and Time

- Search: database full-text or dedicated search service per project needs
- File handling: upload validation, type checking, size limits, secure download
- Time handling: UTC storage, local display, DST-aware scheduling, timezone testing

**Exit Criteria:**

- [ ] Search returns permission-filtered results
- [ ] File uploads validated (type, size) before storage
- [ ] Time-related bugs have explicit test coverage (DST transitions)

---

<!-- DELIVERABLE: id=74 status=PLANNED tier=T1 modules=quality_gate.py,role_registry.py -->
# 74. Privacy, Auditability, and Compliance

Applications handling user data must respect privacy and maintain audit trails.

### §74.A — Privacy by Design

- Minimum data collection: only collect what's needed
- Sensitive data classification and appropriate protection
- Retention policies: data deleted after defined period
- Deletion: user data fully removed on request (including backups where feasible)
- Consent tracking where applicable
- Logging redaction: sensitive data never in logs

**Exit Criteria:**

- [ ] Data collection justified per field (documented purpose)
- [ ] User deletion request removes data within defined SLA
- [ ] Zero sensitive data in application logs

### §74.B — Auditability

- Audit log captures: actor, action, resource, timestamp, origin, correlation ID
- Before/after state captured for data modifications where required
- Audit logs immutable (append-only, tamper-evident)
- Audit log access itself is logged

**Exit Criteria:**

- [ ] All security-relevant operations produce audit entries
- [ ] Audit log entries cannot be modified or deleted
- [ ] Audit trail supports forensic reconstruction of events

---

<!-- DELIVERABLE: id=75 status=PLANNED tier=T1 modules=quality_gate.py -->
# 75. Backup, Disaster Recovery, and Data Lifecycle

Data durability is non-negotiable for production applications.

### §75.A — Backup and Restore

- Backup schedules appropriate to data criticality
- Backup verification: automated restore tests (backup without tested restore is not a backup)
- Point-in-time recovery for transactional databases
- RPO/RTO defined per project contract
- Encrypted backups at rest

**Exit Criteria:**

- [ ] Restore from backup tested quarterly (automated)
- [ ] RPO/RTO targets met in restore tests
- [ ] Backup encryption verified

### §75.B — Data Import/Export

- CSV/JSON import with validation, preview, duplicate handling
- Export with appropriate format and filtering
- Migration tools for data format changes
- Import reports: what succeeded, what failed, why

**Exit Criteria:**

- [ ] Import validates before committing (preview mode)
- [ ] Failed imports don't partially commit
- [ ] Export includes all user-accessible data

---

<!-- DELIVERABLE: id=76 status=PLANNED tier=T2 modules=role_registry.py,api_runner.py -->
# 76. Admin Interfaces and Feature Flags

Operational tooling for application management.

### §76.A — Admin Interfaces

- User management, organization management, permission management
- System status and health dashboards
- Audit log viewing and search
- Support tools: user impersonation (where securely justified), data inspection
- Feature controls: enable/disable features without deployment

**Exit Criteria:**

- [ ] Admin actions require elevated permissions (never accessible to regular users)
- [ ] Admin actions produce audit log entries
- [ ] Impersonation (if supported) clearly indicated and time-limited

### §76.B — Feature Flags

- Safe rollout: per-user, per-organization, staged percentage
- Rollback: disable feature without deployment
- Cleanup: expired flags identified and removed
- Flag state consistent across application instances

**Exit Criteria:**

- [ ] Feature can be disabled in production within 60 seconds
- [ ] Expired flags (> 30 days) flagged for cleanup
- [ ] Flag evaluation consistent across all instances

---

<!-- DELIVERABLE: id=77 status=PLANNED tier=T1 modules=role_registry.py,project_adapter.py -->
# 77. Framework Knowledge and Stack Selection

CodeBot must understand idiomatic patterns for supported stacks, not generate generic pseudo-framework architecture.

### §77.A — Framework-Specific Expertise

- Understand conventions for major frameworks: React, Vue, Svelte, Django, Flask, FastAPI, Rails, Express, Next.js
- Generate code following framework idioms, not generic patterns
- Framework-specific testing approaches (React Testing Library, Django test client, etc.)
- Framework-specific security patterns (Django CSRF, Rails strong params, etc.)

**Exit Criteria:**

- [ ] Generated code passes framework-specific linting rules
- [ ] Code follows framework directory structure conventions
- [ ] Framework-specific security features used (not reinvented)

### §77.B — Structured Stack Selection

- Stack selection based on: requirements, scale, team constraints, deployment target, ecosystem maturity
- Not the same stack for every project
- Justification documented: why this stack for this application
- Migration path considered: can we change later if needed?

**Exit Criteria:**

- [ ] Stack selection produces documented rationale
- [ ] Selection considers operational complexity (not just developer preference)
- [ ] At least 2 alternative stacks evaluated before selection

---

<!-- DELIVERABLE: id=78 status=PLANNED tier=T2 modules=role_registry.py,project_adapter.py -->
# 78. Existing Application Ingestion and Legacy Support

CodeBot must work with existing codebases, not just greenfield.

### §78.A — Repository Ingestion

- Automated repo inventory: files, dependencies, entry points, test locations
- Architecture reconstruction from code structure
- Documentation reconstruction where missing
- Risk identification: technical debt, security issues, missing tests
- Safe incremental modernization (not big-bang rewrites)

**Exit Criteria:**

- [ ] Ingestion produces architecture overview within 1 hour
- [ ] Dependency graph generated automatically
- [ ] Risk assessment identifies top 10 issues by severity

### §78.B — Legacy Application Support

- Handle: weak tests, old frameworks, missing docs, inconsistent architecture
- Progressive improvement: each change leaves code slightly better
- Compatibility preservation: don't break existing behavior
- Migration path: old → new patterns incrementally

**Exit Criteria:**

- [ ] Legacy code changes preserve existing behavior (verified by tests)
- [ ] Each legacy ticket improves at least one quality metric
- [ ] Zero regressions from modernization changes

---

<!-- DELIVERABLE: id=79 status=PLANNED tier=T1 modules=quality_gate.py -->
# 79. Browser/Device Matrix and Cross-Platform

Applications must work across the target platform matrix.

### §79.A — Browser and Device Testing

- Desktop, tablet, mobile viewports tested
- Modern browser support: Chrome, Firefox, Safari, Edge (latest 2 versions)
- Touch behavior verified on mobile viewports
- Keyboard behavior verified on desktop
- High DPI rendering verified
- Reduced-motion preferences respected

**Exit Criteria:**

- [ ] Primary workflows pass on all supported browsers
- [ ] Touch targets meet 44×44px on mobile
- [ ] Keyboard navigation complete on desktop

### §79.B — Cross-Platform Backend

- Linux, Windows, macOS compatibility where required
- Container environment compatibility
- Don't assume POSIX behavior on Windows
- Path handling, line endings, process management platform-aware

**Exit Criteria:**

- [ ] Tests pass on Linux and at least one other platform (if cross-platform required)
- [ ] Zero platform-specific path assumptions in portable code

---

<!-- DELIVERABLE: id=80 status=PLANNED tier=T0 modules=quality_gate.py,gatekeeper.py -->
# 80. Quality Gate Expansion

The quality gate system must support conditional gates appropriate to change type.

### §80.A — Conditional Gate Definitions

```yaml
frontend_change:
  ui_review: required
  responsive_test: required
  visual_regression: required

public_ui:
  accessibility: required
  lighthouse_performance: required

security_boundary:
  security_review: required
  adversarial_tests: required
  sast_scan: required

database_change:
  migration_test: required
  rollback_test: required

api_change:
  contract_test: required
  openapi_validation: required

performance_sensitive:
  benchmark: required
  budget_check: required

authentication_change:
  auth_security_review: required

tenant_scope_change:
  isolation_tests: required

deployment_change:
  deployment_smoke_test: required
```

Do not require irrelevant gates for every ticket.

**Exit Criteria:**

- [ ] Gate conditions correctly triggered by changed file patterns
- [ ] Irrelevant gates skipped (documentation-only changes don't trigger security review)
- [ ] All gate results recorded in provenance

---

<!-- DELIVERABLE: id=81 status=PLANNED tier=T1 modules=role_registry.py -->
# 81. Specialized Agent Roles for Web Applications

New roles required to cover the full web application lifecycle.

### §81.A — Design and UX Roles

- `product_analyst`: Extracts and structures requirements from product descriptions
- `ux_architect`: Designs information architecture, user flows, interaction patterns
- `ui_designer`: Creates visual designs, selects components, ensures aesthetic quality
- `design_system_engineer`: Maintains design tokens, component library, theme system
- `ux_reviewer`: Evaluates usability, workflow friction, accessibility (exists: ux_auditor, needs expansion)

### §81.B — Engineering Roles

- `database_engineer`: Schema design, query optimization, migration safety
- `api_architect`: API contract design, versioning strategy, documentation
- `e2e_test_engineer`: Browser automation, user journey testing, visual regression
- `integration_engineer`: Third-party service integration, webhook handling
- `devops_engineer`: CI/CD, containerization, deployment automation
- `sre_reviewer`: Reliability, observability, incident readiness review

### §81.C — Security and Quality Roles

- `auth_reviewer`: Authentication-specific security review
- `tenant_isolation_reviewer`: Multi-tenancy isolation verification
- `accessibility_reviewer`: WCAG compliance verification (separate from ux_auditor)
- `performance_engineer`: Performance budget enforcement, optimization

**Exit Criteria:**

- [ ] Each role has: description, model profile, tool policy, incentive, adversarial mappings
- [ ] Roles integrated into adaptive scheduler slot allocation
- [ ] Role prompts generated with project-specific context

---

<!-- DELIVERABLE: id=82 status=PLANNED tier=T1 modules=quality_gate.py,gatekeeper.py -->
# 82. Application Acceptance and Production Readiness

Beyond individual tickets, complete application verification before release.

### §82.A — Application Acceptance Testing

Before major release, verify:

- Can a new user sign up and reach primary value?
- Do permissions work correctly across all roles?
- Can an administrator manage the application?
- Does mobile work?
- Are errors understandable and recoverable?
- Can operators diagnose failures?
- Can the system be backed up and restored?
- Can it be upgraded without data loss?

**Exit Criteria:**

- [ ] Acceptance checklist defined per project contract
- [ ] All acceptance criteria verified before release
- [ ] Acceptance failures block release

### §82.B — Product Polish Pass

Distinct release-stage review for:

- Inconsistent copy or labels
- Missing states (empty, loading, error)
- Broken responsiveness
- Unclear forms or confusing flows
- Inconsistent icons or spacing
- Animation misuse or missing feedback
- Unnecessary complexity visible to users

**Exit Criteria:**

- [ ] Polish pass completes before every major release
- [ ] Polish findings generate tickets (not ignored)
- [ ] Zero "obviously broken" UI in release

### §82.C — Production Readiness Gate

```text
build passes
tests pass
security gate passes
primary E2E workflows pass
database migrations verified
backup/restore defined
health checks functional
observability functional
error handling verified
accessibility requirements satisfied
performance budgets satisfied
deployment reproducible
rollback documented
documentation current
```

**Exit Criteria:**

- [ ] All readiness criteria verified before production deployment
- [ ] Readiness gate is automated (not manual checklist)
- [ ] Failed readiness blocks deployment

---

<!-- DELIVERABLE: id=83 status=PLANNED tier=T2 modules= -->
# 83. Technical Debt, ADRs, and Decision Records

Structured management of long-term codebase health.

### §83.A — Technical Debt Management

- Debt items tracked with: source, affected area, impact, risk, estimated cost, reason deferred
- Debt competes rationally with features (not ignored, not automatically prioritized)
- Debt accumulation rate monitored
- High-risk debt escalated

**Exit Criteria:**

- [ ] Debt items have structured metadata (not just "TODO" comments)
- [ ] Debt review happens quarterly (at minimum)
- [ ] High-risk debt (security, data integrity) prioritized over features

### §83.B — Architecture Decision Records (ADRs)

- Major technical decisions documented: context, decision, alternatives, tradeoffs, consequences
- Agents respect accepted ADRs until authorized replacement exists
- ADR status: proposed → accepted → deprecated → replaced
- Product decisions documented separately from technical ADRs where useful

**Exit Criteria:**

- [ ] All major architecture decisions have ADRs
- [ ] Agents cannot contradict accepted ADRs without proposing replacement
- [ ] ADR directory maintained and current

---

<!-- DELIVERABLE: id=84 status=PLANNED tier=T1 modules=role_registry.py,adaptive_scheduler.py,quality_gate.py -->
# 84. Review Swarm, Disagreement Resolution, and QA-Stage Recommendations

Coordinated multi-reviewer verification with explicit conflict resolution. No human escalation — all issues surface as QA-stage recommendations resolved through the normal pipeline.

### §84.A — Full Application Review Swarm

For releases, coordinate relevant reviewers:

- Correctness, Security, UX, UI, Accessibility, Architecture, Performance, Database, API, Operations, Documentation, Simplicity
- Only invoke reviewers relevant to the change or release
- Reviewers operate in parallel where independent

**Exit Criteria:**

- [ ] Review swarm composition determined by change scope
- [ ] Parallel review completes within defined time budget
- [ ] All reviewer findings aggregated before completion decision

### §84.B — Review Disagreement Resolution

- Store: reviewer, finding, severity, evidence, disagreement, resolution
- Conflicting reviews explicitly resolved (not silently dropped)
- Resolution authority: adversarial review swarm votes; senior reviewer role breaks ties
- Disagreement history informs reviewer calibration via RL reward shaping
- No human arbitration; conflicts resolve through structured adversarial process

**Exit Criteria:**

- [ ] Zero unresolved review conflicts at completion time
- [ ] Disagreement resolution recorded with rationale
- [ ] Repeated false-positive reviewers have findings deprioritized
- [ ] Conflict resolution completes within review cycle (no indefinite blocking)

### §84.C — QA-Stage Recommendations

Sensitive or ambiguous issues surface as structured recommendations during QA review, not as human escalations.

Recommendation triggers:

- Unresolved requirement ambiguity → recommendation ticket with options and tradeoff analysis
- Conflicting business rules → recommendation ticket with conflict evidence and proposed resolution
- Major architectural tradeoffs → ADR recommendation with alternatives evaluated
- Irreversible migrations → migration safety recommendation with rollback plan requirement
- High-risk security design → adversarial security review recommendation with threat assessment
- Unclear product direction → requirement clarification recommendation with evidence from existing patterns

Recommendations are processed through the normal ticket pipeline:

1. QA stage generates recommendation as a ticket (type=recommendation)
2. Recommendation assigned to appropriate reviewer/planning role
3. Resolution produces either: code change, ADR, or requirement clarification
4. Original ticket unblocked after recommendation resolved
5. All recommendations tracked with resolution status

**Exit Criteria:**

- [ ] Recommendation tickets generated automatically by QA stage
- [ ] Zero recommendations require human intervention to resolve
- [ ] Recommendation resolution time tracked and trending downward
- [ ] All sensitive-area changes produce recommendations (never silently passed)

---

<!-- DELIVERABLE: id=85 status=PLANNED tier=T2 modules= -->
# 85. Benchmark Applications and Capability Matrix

Measurable verification of CodeBot's web application capabilities.

### §85.A — Benchmark Application Levels

```text
Level 1 — CRUD: Users, records, forms, search, database
Level 2 — Business App: RBAC, reporting, workflows, notifications
Level 3 — SaaS: Organizations, subscriptions, billing, tenant isolation
Level 4 — Real-Time: WebSockets/SSE, background processing, queues
Level 5 — Complex Business: Multiple modules, integrations, advanced reporting
Level 6 — Security-Sensitive: Sensitive data, strong authorization, auditability
Level 7 — Large Existing: Legacy code, migrations, incomplete documentation
```

Track autonomous success rate separately for each level.

### §85.B — Capability Matrix

Machine-readable matrix recording for each capability:

- capability name
- status (exists / partial / planned / missing)
- supported stacks
- required tools
- required agents
- quality gates
- limitations
- tests
- roadmap ID

**Exit Criteria:**

- [ ] Benchmark levels 1-3 have reference implementations
- [ ] Capability matrix updated with every capability change
- [ ] CodeBot never claims capabilities not in the matrix

---

<!-- DELIVERABLE: id=86 status=PLANNED tier=T1 modules= -->
# 86. Maintainability After CodeBot and Architecture Diversity

CodeBot-generated software must outlive CodeBot itself.

### §86.A — Maintainability Without CodeBot

- Conventional structures: standard framework layouts, normal git history
- Human-readable documentation: README, architecture, setup, operations
- Standard frameworks: no custom frameworks or DSLs
- Reasonable dependencies: well-known, maintained packages
- Reproducible builds: any engineer can build from source
- Clear configuration: documented, validated, safe defaults
- Understandable schemas: documented, conventional naming
- Explicit contracts: OpenAPI, type definitions, interface docs

**Exit Criteria:**

- [ ] New engineer can run project in < 5 minutes from README alone
- [ ] Zero CodeBot-specific constructs in generated application code
- [ ] Architecture understandable without CodeBot context

### §86.B — Architecture Diversity (No Monoculture)

- Select architecture based on requirements, not habit
- Don't force: same framework, same database, same cloud, same frontend everywhere
- Reuse validated concepts and components without rigid templates
- Simplest architecture satisfying requirements wins
- Microservices not mandatory; Kubernetes not mandatory; GraphQL not mandatory; Redis not mandatory

**Exit Criteria:**

- [ ] Stack selection rationale documented per project
- [ ] At least 2 different stacks used across benchmark applications
- [ ] No capability requires a specific infrastructure choice

---

<!-- DELIVERABLE: id=59 status=PLANNED tier=T1 modules=role_registry.py,api_runner.py,ticket_engine.py -->
