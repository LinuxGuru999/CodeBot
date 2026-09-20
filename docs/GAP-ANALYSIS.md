# CodeBot Feature Gap Analysis

Last updated: 2026-09-18
Source: CODEBOT-ROADMAP.md §2.A through §27 vs current implementation

## Summary

| ROADMAP Section | Capability | Status | Gap |
|----------------|------------|--------|-----|
| §2.A | Portable Project Understanding | 🟡 Partial | Adapter interface exists, no auto-inventory |
| §2.B | Project Contract (.codebot/) | ✅ Implemented | project.yaml, constitution.md, quality_gates.yaml |
| §2.C | Project Constitution | ✅ Implemented | 11 sections including self-improvement safeguards |
| §2.D | Ticket-Centered Development | 🟡 Partial | Engine exists, not wired into orchestrator main loop |
| §2.E | Automatic Ticket Discovery | 🟡 Partial | Roles defined, not scheduled autonomously |
| §2.F | Deduplication & Validation | ✅ Implemented | SHA-256 evidence hashing in ticket_engine |
| §2.G | Planning Pipeline | 🟡 Partial | Plans generated but not enforced as prerequisites |
| §2.H | Specialized Agent Roles | ✅ Implemented | 29 registered roles, 40 prompt files, legacy map |
| §3 | Model Independence | 🟡 Partial | Profile matching exists, single provider only |
| §4 | Deterministic Verification | ✅ Implemented | quality_gate.py + gatekeeper.py |
| §5 | Central Quality Gate | 🟡 Partial | Gate exists, not blocking orchestrator flow |
| §6 | Adversarial Review | 🟡 Partial | Mappings defined, not wired into dispatch |
| §7 | Autonomous Rework Loop | ✅ Implemented | Max 3 rework, REWORK escalation |
| §8 | Documentation as State | 🟡 Partial | Same-PR rule in prompts, no drift detection automation |
| §9 | Artifact Provenance | ❌ Missing | No commit→ticket→agent traceability |
| §10 | Runtime State Separation | ✅ Implemented | .codebot/state/ separate from source |
| §11 | Concurrency & Parallel Dev | 🟡 Partial | Claims prevent double-work, no worktree isolation |
| §12 | Security Model | 🟢 Strong | Tool allowlisting, SSRF guard, bounded I/O, shell=False |
| §13 | Autonomy Levels | ✅ Implemented | 5 levels in autonomy_config, risk-based routing |
| §14 | Human Approval Model | ✅ Implemented | Constitution §7 defines categories |
| §15 | Economics Engine | 🟡 Partial | Cost tracking exists, no monetary conversion or prediction |
| §16 | Learning & Feedback | 🟡 Partial | RL engine exists, disconnected from exit events |
| §17 | Self-Improvement | ✅ Implemented | Constitution §10, prompt_optimizer guard |
| §18 | Portability Extraction | ✅ Complete | Extracted repo, zero Monitor refs in core |
| §19 | Second-Project Validation | ✅ Complete | TaskBoard validated end-to-end |
| §20 | Deep App Validation | ❌ Missing | Not attempted beyond TaskBoard |
| §21 | Greenfield Development | ❌ Missing | Maintenance only |
| §22 | Production Feedback Loop | ❌ Missing | Architecture designed, not implemented |
| §23 | Business-App Factory | ❌ Missing | Long-term goal |
| §24 | Customer/Agency Workflow | ❌ Missing | Long-term goal |
| §25 | Reliability | 🟢 Strong | Drain, checkpoint, heartbeat, crash recovery |
| §26 | Observability | 🟡 Partial | Metrics collector exists, no dashboard |
| §27 | Success Metrics | ❌ Missing | Defined in GOALS.md, not measured |

---

## Critical Gaps (Block v0.3.0)

### GAP-1: Orchestrator Not Wired to TicketStore
**Section**: §2.D
**Problem**: Orchestrator still reads QUEUE.md markdown. Ticket engine exists but isn't used for dispatch.
**Impact**: Tickets created by discovery agents sit in READY state forever. No autonomous pipeline.
**Effort**: High (refactor orchestrator main loop)
**Acceptance**: Orchestrator dispatches from TicketStore.list_ready(), QUEUE.md removed

### GAP-2: Gatekeeper Not Blocking Completion
**Section**: §5
**Problem**: Gatekeeper evaluates gates but orchestrator doesn't call it. Bots self-declare done.
**Impact**: Quality gates are advisory, not enforced. Unverified code can reach COMPLETE.
**Effort**: Medium (inject gatekeeper call into api_runner completion path)
**Acceptance**: No ticket reaches COMPLETE without gatekeeper PASS verdict

### GAP-3: No Worktree Isolation
**Section**: §11
**Problem**: All agents share one working directory. Concurrent edits cause conflicts.
**Impact**: Cannot safely run multiple implementers in parallel on overlapping files.
**Effort**: High (git worktree management, merge sequencing)
**Acceptance**: Each agent gets isolated worktree, sequential merge queue

### GAP-4: RL Engine Disconnected from Exit Events
**Section**: §16
**Problem**: rl_engine.py processes alignment events but orchestrator doesn't write them on agent exit.
**Impact**: Prompt optimization has no reward signal. RSI loop is dead.
**Effort**: Medium (wire orchestrator exit handler to write .exit.json)
**Acceptance**: Agent exits produce events, RL engine processes them, Q-values update

### GAP-5: No Artifact Provenance Trail
**Section**: §9
**Problem**: Cannot answer "why did this line change?" No link between commits, tickets, agents, models.
**Impact**: Auditing impossible. Debugging autonomous changes requires manual forensics.
**Effort**: Medium (append-only event log keyed by commit hash)
**Acceptance**: Given a commit hash, retrieve ticket, agent, model, reviewer, test results

---

## Moderate Gaps (v0.4.0)

### GAP-6: Single Model Provider
**Section**: §3
**Problem**: Only dialagram configured. No fallback chain, no capability-based routing in practice.
**Effort**: Medium

### GAP-7: No Monetary Cost Tracking
**Section**: §15
**Problem**: Tokens tracked but not converted to dollars. Can't optimize quality-per-dollar.
**Effort**: Low (pricing table + multiplication)

### GAP-8: Planning Not Enforced
**Section**: §2.G
**Problem**: Implementation plans generated but tickets can skip PLANNING state.
**Effort**: Low (state machine enforcement)

### GAP-9: Review Dispatch Not Automated
**Section**: §6
**Problem**: Adversarial reviewer mappings exist but orchestrator doesn't schedule review agents.
**Effort**: Medium

### GAP-10: No Telemetry Ingestion
**Section**: §22
**Problem**: Production feedback loop designed but no endpoint exists.
**Effort**: Medium

---

## Long-Term Gaps (v0.6.0+)

| Gap | Section | Description |
|-----|---------|-------------|
| Greenfield development | §21 | Cannot create apps from scratch |
| Business-app factory | §23 | No reusable component templates |
| Customer workflow | §24 | No scope tracking or acceptance flow |
| Deep app validation | §20 | Only tested on simple apps |
| Multi-language support | §6 | Python-only tool implementations |
| IDE integration | Non-goal | Explicitly deferred |

---

## What's Working Well

1. **Portability** (§18-19): Fully extracted, validated on 2 projects, zero cross-contamination
2. **Ticket schema** (§2.D-F): 14-state machine, dedup, serialization all solid
3. **Role abstraction** (§2.H): 29 registered roles with adversarial mappings, tool policies, model profiles, 40 prompt files
4. **Security** (§12): SSRF guard, shell=False, bounded I/O, tool allowlisting
5. **Self-improvement safeguards** (§17): Constitution §10 protects critical components
6. **Context compaction**: Sliding window prevents OOM on long sessions
7. **Scratchpad handoff**: Structured state persistence enables crash recovery
8. **Task splitting**: Oversized work decomposes into parallelizable sub-tasks
9. **Web research**: Agents can search and fetch internet resources
10. **Coverage pipeline**: Measured coverage drives test ticket generation
