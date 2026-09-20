# CodeBot Goals

Last updated: 2026-09-18
Source: CODEBOT-ROADMAP.md (43 sections) distilled into actionable targets.
Related: ROADMAP.md · .codebot/constitution.md · ARCHITECTURE.md

---

## Why This Exists

CodeBot is an autonomous software engineering platform. It takes a documented repository, discovers work, creates evidence-backed tickets, plans, implements, tests, reviews, and maintains code with minimal human intervention.

CodeBot's first managed project is **itself**. Before operating on external repositories at scale, CodeBot must prove it can maintain its own codebase under stricter safeguards than any normal project (§17 Self-Improvement).

The long-term purpose:

> Given a software repository plus a structured project contract and engineering constitution, autonomously understand the system, discover necessary work, create evidence-backed tickets, prioritize and plan changes, implement them, test them, attack them, review them, update documentation, verify acceptance criteria, and continuously maintain the application with minimal human engineering effort.
>
> The target is not merely functional code. The target is production-quality software that is intentionally designed, secure, reliable, maintainable, accessible, performant, documented, testable, operable, and pleasant to use. Software quality is multidimensional: correctness alone is insufficient. CodeBot must perform the work of an excellent product engineer, software architect, frontend engineer, backend engineer, database engineer, UX designer, UI designer, security engineer, QA engineer, accessibility specialist, performance engineer, DevOps engineer, SRE, technical writer, API designer, integration engineer, and release engineer — using coordinated autonomous agents, deterministic tools, structured project knowledge, and enforceable quality gates.
>
> "Bug-free" means: no known critical defects, strong automated regression protection, aggressive defect discovery, measurable reliability, and rapid detection/repair of escaped defects. Not literally zero bugs.

---

## What We're Building

A **portable, model-independent autonomous software factory** where:

```
CodeBot
   │
   ├── Project Profile: CodeBot (self-improvement, strongest safeguards)
   ├── Project Profile: Monitor Platform
   ├── Project Profile: Customer App A
   └── Project Profile: SaaS Experiment B
```

NOT:
```
Monitor
   └── Monitor-specific BotNet
```

CodeBot core contains zero project-specific business logic. All project knowledge flows through `.codebot/project.yaml` and the `ProjectAdapter` interface.

---

## Software Quality Definition

Software quality is multidimensional. Correctness alone is insufficient.

CodeBot's long-term goal explicitly includes the ability to create:

> Production-quality software that is not merely functional, but intentionally designed, secure, reliable, maintainable, accessible, performant, documented, testable, operable, and pleasant to use.

Quality dimensions:

```text
FUNCTION        — Does it do what requirements specify?
DESIGN          — Does it look deliberately designed?
USABILITY       — Is it intuitive and pleasant to use?
SECURITY        — Does it resist attack by default?
RELIABILITY     — Does it survive failures gracefully?
PERFORMANCE     — Is it fast within defined budgets?
ACCESSIBILITY   — Can everyone use it?
MAINTAINABILITY — Can a new engineer understand and extend it?
OPERABILITY     — Can operators diagnose and fix production issues?
TESTABILITY     — Is it comprehensively verified at every layer?
DOCUMENTATION   — Does documentation reflect actual system state?
```

"Bug-free" means: no known critical defects, strong automated regression protection, aggressive defect discovery, measurable reliability, and rapid detection/repair of escaped defects. Not literally zero bugs.

---

## Success Criteria

### For Autonomous Engineering
| Metric | Current | Target (v1.0) |
|--------|---------|---------------|
| Autonomous completion rate | ~0% (manual orchestration) | >80% |
| First-pass quality-gate success | N/A | >70% |
| Human intervention rate | 100% | 0% (QA recommendations only) |
| Escaped regression rate | Unknown | <5% |
| Mean accepted-ticket cost | Unknown | Measured & improving |
| Documentation drift rate | High | <10% |
| Test coverage (own codebase) | ~60% estimated | >85% |
| Tickets completed on self | 0 | 50+ meaningful tickets |

### For Portability
| Metric | Current | Target |
|--------|---------|--------|
| Projects validated | 2 (Monitor, TaskBoard) | Any documented repo |
| Monitor-specific code in core | 0 (verified by grep audit) | 0 permanently |
| Adapter implementations | 2 (monitor, taskboard) | Community-contributed |
| Model providers supported | 1 (dialagram) | 3+ with fallback |

### For Self-Improvement (§17)
| Metric | Target |
|--------|--------|
| Self-modification safeguards | Stronger than any normal project |
| Bootstrap recovery | Always possible |
| Constitution weakening | Never without adversarial review swarm + QA recommendation resolution |
| Control removal | Impossible (hardcoded guard) |

---

## Strategic Goals (from CODEBOT-ROADMAP.md)

### Goal 1: Portable Project Understanding (§2.A)
CodeBot must enter any unrelated repository and understand it through inspection, documentation, configuration, architecture, API contracts, schemas, tests, source code, dependency graphs, and git history.

**Current state**: `.codebot/project.yaml` + `ProjectAdapter` ABC provides the interface. Validated against Monitor (5 components) and TaskBoard (2 components).
**Gap**: No automated repository inventory or architecture inference yet. Agents rely on static analysis prompts.

### Goal 2: Ticket-Centered Autonomous Development (§2.D, §E, §F)
All meaningful work flows through normalized structured tickets. Discovery is separated from authorization. Deduplication via SHA-256 evidence hashing prevents redundant work.

**Current state**: `ticket_engine.py` implements 14-state machine with dedup. `migrate_queue.py` bridges legacy QUEUE.md. Coverage bridge auto-generates test tickets from measured gaps.
**Gap**: Discovery agents run via prompts but aren't yet wired into the orchestrator's main loop as autonomous schedulers. Ticket lifecycle is manual-triggered.

### Goal 3: Planning Pipeline (§2.G)
Non-trivial tickets receive depth-scaled implementation plans before coding begins.

**Current state**: `implementation_planner.py` generates summary/standard/full plans based on risk score. `feature_decomposer` role breaks epics into atomic items.
**Gap**: Plans are generated but not yet enforced as prerequisites before IMPLEMENTING state transition.

### Goal 4: Specialized Agent Roles (§2.H)
Replace hardcoded bot names with composable ROLE + TASK + MODEL_PROFILE + TOOL_POLICY abstraction.

**Current state**: 29 registered roles across 5 categories in `role_registry.py`, with 40 prompt files in `codebot/roles/*.md`. `LEGACY_ROLE_MAP` bridges old bot names. Model profiles, tool policies, and adversarial mappings defined per role.
**Gap**: Orchestrator still uses `BOT_REGISTRY` from adapter rather than dynamically assembling agents from role definitions. Role-to-model routing is defined but not enforced. Some prompt files exist for roles not yet formally registered.

### Goal 5: Deterministic Verification (§4, §5)
AI opinion never serves as primary proof. Central quality gate is sole authority for COMPLETE transitions.

**Current state**: `quality_gate.py` runs YAML-defined gates via subprocess. `gatekeeper.py` enforces max 3 rework cap. Build/test/lint/security gates configured.
**Gap**: Gates execute but aren't yet blocking the ticket state machine. Implementers can still self-declare done without gatekeeper sign-off in the current orchestrator flow.

### Goal 6: Adversarial Multi-Agent Review (§6)
Review agents have intentionally conflicting incentives. Findings must be resolved before completion.

**Current state**: 7 review roles defined with adversarial_to mappings. `find_adversarial_reviewers()` resolves conflict graph.
**Gap**: Review dispatch is defined in roles but not wired into orchestrator scheduling. No automated REVIEWING → VERIFYING pipeline yet.

### Goal 7: Model Independence (§3)
Cheapest capable model selected per task. Multiple providers with fallback.

**Current state**: `ModelProfile` capability matching exists. `_resolve_api_url()` and `_get_api_key()` support env override + adapter config.
**Gap**: Only one provider (dialagram) configured. No automatic fallback chain between providers. No per-model success/cost statistics collected.

### Goal 8: Economics Engine (§15)
Quality per dollar is the strategic advantage. Per-ticket cost tracking enables learning.

**Current state**: `cost_tracker.py` records per-ticket token attribution. `token_budget.py` enforces fleet-wide daily caps. RL engine shapes rewards based on efficiency.
**Gap**: No monetary cost calculation (tokens ≠ dollars without pricing table). No predictive cost estimation before execution.

### Goal 9: Learning & Self-Improvement (§16, §17)
Evidence-based optimization from historical outcomes. CodeBot improves itself under stronger controls.

**Current state**: `rl_engine.py` epsilon-greedy bandit with 11 Q-arms. `prompt_optimizer` role modifies other agents' prompts. `coverage_bridge.coverage_delta_score()` feeds reward signal.
**Gap**: Prompt optimizer is defined as a role but not scheduled by orchestrator. RL state persistence works but isn't connected to actual alignment events from agent exits.

### Goal 10: Concurrency & Safe Parallel Development (§11)
Many cheap agents operating concurrently without corrupting the repository.

**Current state**: Claim files (`state/claims/{ticket}.{agent}.json`) prevent double-work. `dependency_graph.py` topological sort ensures ordering. `task_splitter.py` decomposes oversized work.
**Gap**: No isolated worktrees/branches. All agents share one working directory. Merge conflict prevention is advisory (via dependency graph) not enforced (no file-level locking).

### Goal 11: Security Model (§12)
CodeBot is privileged infrastructure. Least-privilege, sandboxed, prompt-injection resistant.

**Current state**: `tool_policy.py` allowlists commands. SSRF guard blocks private IPs. Bounded I/O everywhere. `shell=False` in subprocess. Credentials from env only.
**Gap**: No filesystem sandboxing (chroot/bubblewrap). Prompt injection defense relies on system prompt instructions, not structural isolation. No signed artifacts.

### Goal 12: Production Feedback Loop (§22)
Deployed telemetry drives new tickets through normal pipeline.

**Current state**: Architecture designed (telemetry → structured evidence → ticket candidate → pipeline). Not implemented.
**Gap**: No telemetry ingestion endpoint. No production monitoring integration.

### Goal 13: Frontend Architecture and UI Engineering (ROADMAP §46)
CodeBot must build complete, production-quality frontend applications with component architecture, design system integration, responsive layouts, animation/motion design, and visual polish.

**Current state**: No frontend-specific roles, prompts, or quality gates exist. Discovery agents don't audit UI.
**Gap**: Need frontend_implementer role with CSS/HTML/JS tool policies, UX auditor integration into ticket pipeline, design token enforcement, responsive layout generation, and Core Web Vitals quality gates.

### Goal 14: Accessibility Engineering (ROADMAP §47)
Every web application CodeBot produces must meet WCAG 2.2 AA compliance. Semantic HTML, keyboard navigation, ARIA attributes, color contrast, and form accessibility are mandatory, not optional.

**Current state**: ux_auditor role exists but isn't wired into the quality gate pipeline. No automated a11y scanning in CI.
**Gap**: Wire ux_auditor findings into ticket creation, add axe-core or equivalent to quality gates, enforce contrast ratios and keyboard accessibility in review criteria.

### Goal 15: Web Security Hardening (ROADMAP §48)
Security must be structural. Input validation, auth/session management, authorization, HTTP security headers, CSRF protection, and injection prevention are baseline requirements for every web app.

**Current state**: security_auditor and security_reviewer roles exist. SSRF guard in web_tools.py. Tool policy sandboxing.
**Gap**: No CSP generation, no auth flow scaffolding, no automated OWASP scanning in quality gates, no session management templates. Security review is adversarial but not generative.

### Goal 16: API Design and Backend Engineering (ROADMAP §49)
RESTful API design, versioning, rate limiting, middleware pipelines, and WebSocket support must be first-class capabilities.

**Current state**: backend_implementer role exists. API tools (read/write/edit/bash/grep/glob) available.
**Gap**: No API contract generation, no middleware pipeline scaffolding, no rate limiter implementation, no WebSocket support in tool surface.

### Goal 17: Comprehensive Testing Strategy (ROADMAP §50)
Unit, integration, E2E, performance, and security testing must be generated alongside every feature. Coverage thresholds enforced in CI.

**Current state**: test_implementer and test_gap_auditor roles exist. pytest configured. coverage_bridge generates tickets from gaps.
**Gap**: No E2E test generation (Playwright/Cypress), no performance test scaffolding, no security test automation, no visual regression testing.

### Goal 18: Performance Optimization (ROADMAP §51)
Core Web Vitals, backend caching, asset optimization, and database query performance must be engineered proactively.

**Current state**: performance_auditor and performance_reviewer roles exist.
**Gap**: No bundle analysis, no caching layer generation, no CDN configuration, no database query profiling in quality gates.

### Goal 19: State Management and Data Flow (ROADMAP §52)
Client-side state architecture, server-side caching, and form state management must follow established patterns.

**Current state**: No state management roles or templates.
**Gap**: Need state management scaffolding (Redux/Zustand/Pinia patterns), cache invalidation strategies, form state persistence.

### Goal 20: Error Handling and Resilience (ROADMAP §53)
Error boundaries, network resilience, graceful degradation, and circuit breakers must be standard patterns.

**Current state**: auto_revert.py handles build gate failures. Task splitter decomposes oversized work.
**Gap**: No error boundary generation, no retry/backoff scaffolding, no circuit breaker patterns, no offline-first support.

### Goal 21: i18n, Deployment, DX, SEO, and Data Engineering (ROADMAP §54-§58)
Internationalization, containerization, CI/CD pipelines, code quality enforcement, SEO optimization, and data layer engineering round out the full-stack capability.

**Current state**: Dockerfile exists. CI/CD partially configured (Fly.io). Linting configured per-project.
**Gap**: No i18n extraction, no CI/CD pipeline generation, no OpenAPI spec generation, no structured data/SEO scaffolding, no database migration safety patterns.

### Goal 22: Product Requirements Engineering (ROADMAP §59)
CodeBot must transform vague product ideas into structured, traceable requirements before implementation begins.

**Current state**: feature_decomposer role exists for breaking epics into tickets. No requirements extraction, no persona/story generation, no traceability system.
**Gap**: No requirements_engineer role. No requirement schema. No traceability links. No scope management. No conflict detection between requirements.

### Goal 23: UX Design and Information Architecture (ROADMAP §60)
Applications must be intuitive and pleasant to use. UX quality is a first-class engineering concern.

**Current state**: ux_auditor role exists with screenshot/a11y tools but isn't wired into pipeline. No information architecture design step.
**Gap**: No UX architect role. No user journey mapping. No state design enforcement (empty/loading/error/success). No onboarding flow generation. No progressive disclosure patterns.

### Goal 24: Professional UI Design and Design Systems (ROADMAP §61)
Generated interfaces must look deliberately designed with enforceable design systems.

**Current state**: frontend_implementer role exists. No design system engineering. No design token enforcement.
**Gap**: No design_system_engineer role. No token schema. No component library generation. No visual hierarchy enforcement. No data visualization patterns.

### Goal 25: UI Quality Review and Visual Regression (ROADMAP §62)
UI quality must be verified deterministically via screenshots and visual diffs.

**Current state**: ux_auditor has screenshot tool defined but no implementation. No visual regression infrastructure.
**Gap**: No UI reviewer role with browser rendering. No screenshot baseline system. No visual diff tooling. No viewport matrix testing.

### Goal 26: Browser E2E Testing (ROADMAP §63)
CodeBot must open the actual application and test it like a user through real browser interaction.

**Current state**: No Playwright integration. No E2E test infrastructure. No browser automation tools.
**Gap**: No Playwright/Cypress integration. No user journey test generation. No cross-browser testing. No mobile viewport testing. No flaky test detection for E2E.

### Goal 27: Multi-Tenancy and Organization Management (ROADMAP §64)
Applications serving multiple organizations must enforce strict tenant isolation.

**Current state**: No multi-tenancy patterns. No tenant isolation testing.
**Gap**: No tenant architecture patterns. No tenant-scoped query enforcement. No cross-tenant attack testing. No tenant_isolation_reviewer role.

### Goal 28: Security Toolchain and Adversarial Review (ROADMAP §65)
Security verification must combine automated scanning with adversarial review roles.

**Current state**: security_auditor and security_reviewer roles exist. dependency_auditor checks CVEs. No SAST/DAST integration. No secret scanning.
**Gap**: No SAST integration. No secret scanning in quality gates. No adversarial auth reviewer. No input_validation_reviewer. No secrets_reviewer. Security review is adversarial but not generative.

### Goal 29: Advanced Testing Architecture (ROADMAP §66)
Testing must go beyond coverage to include property-based, fuzz, contract, and quality evaluation.

**Current state**: test_implementer and test_gap_auditor exist. pytest configured. coverage_bridge generates tickets.
**Gap**: No property-based testing. No fuzz testing. No contract testing. No test quality evaluation. No component testing infrastructure.

### Goal 30: Performance Budgets and Load Testing (ROADMAP §67)
Performance must be engineered proactively with measurable budgets enforced in CI.

**Current state**: performance_auditor and performance_reviewer roles exist. No budget enforcement.
**Gap**: No performance budget configuration. No budget enforcement in quality gates. No load testing infrastructure. No stress testing. No benchmark comparison for performance-sensitive tickets.

### Goal 31: Reliability Engineering (ROADMAP §68)
Applications must survive failures gracefully with retry, circuit breaker, and idempotency patterns.

**Current state**: auto_revert.py handles build gate failures. No reliability pattern library.
**Gap**: No retry/backoff scaffolding. No circuit breaker patterns. No idempotency key generation. No failure isolation testing. No graceful degradation patterns.

### Goal 32: Observability and Operational Diagnostics (ROADMAP §69)
Production applications must be diagnosable with structured logging, metrics, and tracing.

**Current state**: telemetry.py exists for signal ingestion. metrics_collector.py tracks agent telemetry. No application-level observability generation.
**Gap**: No structured logging generation. No correlation ID implementation. No metrics endpoint generation. No distributed tracing. No alerting rule generation.

### Goal 33: Release Engineering and Configuration Management (ROADMAP §70)
Releases must be reproducible, validated, and reversible with proper configuration management.

**Current state**: release_manager role prompt exists. No release pipeline. No configuration validation.
**Gap**: No semantic versioning enforcement. No changelog generation. No staged deployment. No rollback testing. No environment configuration validation.

### Goal 34: Secrets Management (ROADMAP §71)
Secrets must never be in source code, with injection, rotation, and scoping support.

**Current state**: credentials.py resolves secrets from env vars. No secret scanning. No rotation support.
**Gap**: No secret scanning in quality gates. No rotation automation. No scoped credential enforcement. No environment isolation verification.

### Goal 35: Third-Party Integrations (ROADMAP §72)
External service integrations must be resilient with retry, webhook verification, and contract tests.

**Current state**: No integration engineer role. No integration patterns.
**Gap**: No integration resilience patterns. No webhook signature verification. No integration contract tests. No sandbox/test mode support. No API version pinning.

### Goal 36: Domain Capabilities: Billing, Email, Search, Files, Time (ROADMAP §73)
Common application capabilities need reusable, well-tested patterns selected by project need.

**Current state**: No domain service patterns. No billing integration. No email infrastructure.
**Gap**: No payment provider integration patterns. No email template system. No search patterns. No file handling patterns. No timezone correctness testing.

### Goal 37: Privacy, Auditability, and Compliance (ROADMAP §74)
Applications handling user data must respect privacy and maintain audit trails.

**Current state**: No privacy patterns. No audit logging infrastructure.
**Gap**: No data classification. No retention policies. No deletion automation. No consent management. No audit log generation. No logging redaction.

### Goal 38: Backup, Disaster Recovery, and Data Lifecycle (ROADMAP §75)
Data durability is non-negotiable with tested backup/restore and import/export.

**Current state**: No backup patterns. No import/export infrastructure.
**Gap**: No backup schedule generation. No restore testing. No point-in-time recovery. No CSV/JSON import validation. No data lifecycle management.

### Goal 39: Admin Interfaces and Feature Flags (ROADMAP §76)
Operational tooling for application management with safe feature rollout.

**Current state**: No admin interface patterns. No feature flag system.
**Gap**: No admin UI generation. No feature flag infrastructure. No staged rollout. No flag cleanup detection.

### Goal 40: Framework Knowledge and Stack Selection (ROADMAP §77)
CodeBot must understand idiomatic framework patterns and select stacks based on requirements.

**Current state**: No framework-specific adapters. No stack selection reasoning. Generic patterns only.
**Gap**: No framework pattern libraries. No stack-specific quality gates. No structured stack selection. No framework convention enforcement.

### Goal 41: Existing Application Ingestion and Legacy Support (ROADMAP §78)
CodeBot must work with existing codebases through ingestion and incremental modernization.

**Current state**: project_adapter.py provides interface. No automated ingestion pipeline.
**Gap**: No repository inventory automation. No architecture reconstruction. No risk identification. No legacy pattern recognition. No safe modernization patterns.

### Goal 42: Browser/Device Matrix and Cross-Platform (ROADMAP §79)
Applications must work across target browsers, devices, and operating systems.

**Current state**: No browser matrix configuration. No cross-platform testing.
**Gap**: No browser compatibility matrix. No device testing. No touch behavior verification. No high-DPI testing. No cross-OS backend testing.

### Goal 43: Quality Gate Expansion (ROADMAP §80)
Quality gates must support conditional, change-type-aware verification for web applications.

**Current state**: quality_gate.py supports required + conditional gates with 5 conditions. Missing web-app-specific gates.
**Gap**: No frontend_change gate. No accessibility gate. No visual_regression gate. No e2e gate. No deployment_smoke gate. No ui_review gate. No tenant_isolation gate.

### Goal 44: Specialized Agent Roles for Web Applications (ROADMAP §81)
New roles required: product_analyst, ux_architect, ui_designer, design_system_engineer, database_engineer, api_architect, e2e_test_engineer, integration_engineer, devops_engineer, sre_reviewer, auth_reviewer, tenant_isolation_reviewer, accessibility_reviewer, performance_engineer.

**Current state**: 29 registered roles covering discovery, implementation, review, control, planning. 40 prompt files total.
**Gap**: 14 new roles needed for complete web application lifecycle coverage. Each must have tool policy, model profile, and adversarial mappings.

### Goal 45: Application Acceptance and Production Readiness (ROADMAP §82)
Complete application verification before release, including acceptance testing, polish pass, and readiness gate.

**Current state**: No acceptance testing pipeline. No polish pass. No production readiness gate.
**Gap**: No application-level acceptance checklist. No product polish review. No automated production readiness verification.

### Goal 46: Technical Debt, ADRs, and Decision Records (ROADMAP §83)
Structured debt tracking and architecture/product decision records that agents respect.

**Current state**: docs/adr/ directory configured. No ADR enforcement. No debt tracking.
**Gap**: No debt registry. No ADR template enforcement. No agent ADR respect mechanism. No product decision records.

### Goal 47: Review Swarm, Disagreement Resolution, and QA Recommendations (ROADMAP §84)
Coordinated multi-reviewer verification with explicit conflict resolution. No human escalation — all issues resolved through QA-stage recommendations.

**Current state**: Adversarial review mappings defined. No coordinated review orchestration. No disagreement resolution.
**Gap**: No review swarm orchestration. No disagreement resolution process. No QA-stage recommendation generation. No recommendation ticket type.

### Goal 48: Benchmark Applications and Capability Matrix (ROADMAP §85)
Measurable verification through progressively harder benchmark applications and machine-readable capability tracking.

**Current state**: §39 defines benchmark levels A-G (ticket difficulty). No web-app-specific benchmarks. No capability matrix.
**Gap**: No 7-level web application benchmark classes. No capability matrix storage. No autonomous success tracking per level. No metric collection infrastructure.

### Goal 49: Maintainability After CodeBot and Architecture Diversity (ROADMAP §86)
Generated software must be maintainable without CodeBot and avoid architecture monoculture.

**Current state**: No maintainability verification. No architecture diversity enforcement.
**Gap**: No maintainability checklist. No CodeBot-specific construct detection. No stack diversity tracking. No "simplest architecture" enforcement.

---

## Version Milestones

| Version | Theme | Tiers | Must-Have | Status |
|---------|-------|-------|-----------|--------|
| **0.2.0** | Portable Core | T0–T4 | Ticket engine, quality gates, role registry, adapter interface, extraction | ✅ Shipped |
| **0.3.0** | Self-Hosting | T5–T6 | CodeBot manages itself, autonomous discovery→ticket→plan→implement loop, coverage-driven test generation | 🔄 In Progress |
| **0.4.0** | Multi-Provider | T7 | Model routing layer, fallback chains, per-model statistics, cost prediction | ⏳ Planned |
| **0.5.0** | Parallel Execution | T8 | Worktree isolation, file leases, merge sequencing, concurrent safe development | ⏳ Planned |
| **0.6.0** | Production Ready | T9–T11 | 100 consecutive tickets without human code modification, provenance trail, production feedback | ⏳ Planned |
| **1.0.0** | Software Factory | T12–T13 | Greenfield development, business-app templates, customer workflow, controlled self-improvement maturity | ⏳ Planned |

---

## Dependency Graph

```
Ticket Schema (ticket_engine.py)
     ↓
Discovery Agents (roles/*.md)
     ↓
Planning Pipeline (implementation_planner.py, feature_decomposer)
     ↓
Quality Gates (quality_gate.py, gatekeeper.py)
     ↓
Adversarial Review (role_registry.py adversarial_to)
     ↓
Autonomous Pipeline (orchestrator.py main loop)
     ↓
Portable Project Contract (.codebot/, ProjectAdapter)
     ↓
Self-Hosting (CodeBot manages CodeBot)
     ↓
Multi-Provider Model Routing
     ↓
Parallel Safe Development
     ↓
Production Feedback Loop
     ↓
Software Factory
```

---

## Non-Goals (Until Core is Mature)

| Feature | Reason |
|---------|--------|
| Consumer no-code UI | Engineering reliability first |
| Visual drag-and-drop builder | Out of scope for autonomous backend |
| Mobile CodeBot app | Agents run headless on servers |
| Proprietary foundational model | Models are replaceable infrastructure |
| Full IDE replacement | Agents use tools, not IDEs |
| Massive plugin marketplace | Core stability before extensibility |
| Enterprise billing platform | Business features after engineering maturity |
| Autonomous production deploy before gates | Quality gates are prerequisite |
| Automatic constitution modification | Humans control invariants (§36) |

---

## Architectural Principles

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
19. No human escalation: sensitive changes surface as QA-stage recommendations resolved autonomously through the adversarial review pipeline.

---

## Security-Sensitive Rule

> CodeBot must never silently lower a security requirement, disable a test, delete a failing test, loosen an acceptance criterion, remove validation, weaken authorization, suppress an error, or modify project policy merely to make a ticket pass.

Such actions require explicit justification and appropriate review.

---

## How to Use This Document

- **For developers**: This defines what we're building and why. Implementation details live in ARCHITECTURE.md and API.md.
- **For autonomous agents**: Read this at startup to understand strategic priorities. Your work should advance these goals. If a task conflicts with these goals, escalate.
- **For the goal_steering role**: This document is your primary input. Analyze progress against each goal, identify blockers, and inject steering directives into the ticket queue.
- **For reviewers**: Verify that changes advance these goals without violating the principles or security rule above.
