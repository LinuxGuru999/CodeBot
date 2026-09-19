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

## Success Criteria

### For Autonomous Engineering
| Metric | Current | Target (v1.0) |
|--------|---------|---------------|
| Autonomous completion rate | ~0% (manual orchestration) | >80% |
| First-pass quality-gate success | N/A | >70% |
| Human intervention rate | 100% | <20% |
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
| Constitution weakening | Never without explicit human approval |
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

**Current state**: 37 role prompts across 5 categories. `role_registry.py` defines 26 roles with tool policies and adversarial mappings. `LEGACY_ROLE_MAP` bridges old bot names.
**Gap**: Orchestrator still uses `BOT_REGISTRY` from adapter rather than dynamically assembling agents from role definitions. Role-to-model routing is defined but not enforced.

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
