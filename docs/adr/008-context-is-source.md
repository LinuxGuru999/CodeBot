# ADR 008: Context is Source — Foundational Doctrine

## Status

**Accepted** — 2026-09-24

---

## Context

AI coding systems are typically prompt-driven. A developer writes a prompt, the model generates code, and the model's understanding lives only in the ephemeral session window. When the session ends, the reasoning is gone. If the next session starts with a different model or a different prompt, the system has no memory of what it previously decided or why.

CodeBot needs to be context-driven instead. The repository's accumulated knowledge — requirements, constraints, architecture, decisions, invariants, evidence, dependencies, acceptance criteria, current-state — is the primary source material from which code is compiled. Agents are temporary workers. Context is persistent. If killing every agent destroys important knowledge, that knowledge was stored in the wrong place.

This ADR formalizes the foundational thesis that unifies all CodeBot plans and governs all future work.

---

## Decision

**Context is the source. Code is the artifact.**

Software development should begin with an authoritative model of intent, constraints, architecture, and reality — not with source code. CodeBot continuously converts human intent into structured context, structured context into implementation, and implementation results back into validated project knowledge.

### The Context Compiler Pipeline

The lifecycle is effectively a compiler pipeline:

```
Natural-language intent
        ↓
        Parse (user_agent evaluates, challenges ambiguity)
        ↓
Requirements / constraints (DESIRED-STATE context)
        ↓
Semantic analysis (discovery compares desired vs current reality)
        ↓
Architecture / conflict detection (ADR validation, contradiction findings)
        ↓
Intermediate representation (decomposition into atomic work boundaries)
        ↓
Findings / decomposition (DISCOVERED tickets, dependency graphs)
        ↓
Optimization / planning (implementation contracts, invariants, acceptance criteria)
        ↓
Implementation plan (PLANNING → IMPLEMENT transition)
        ↓
Code generation (implementer produces verified code)
        ↓
Source code (src/ is one output of the knowledge system)
        ↓
Verification (review validates CODE ↔ TESTS ↔ REQUIREMENTS ↔ ARCHITECTURE ↔ DOCS)
        ↓
Context reconciliation (CURRENT-STATE updated, durable knowledge improved)
```

Instead of asking "What code should we write?", the system asks: "Given everything we know about the desired state, current state, constraints, and history of this project, what change is justified?" Then: "What code realizes that change?"

### The Closed Loop

The pipeline is not linear. It is a closed loop:

```
CONTEXT → CODE → REALITY → VALIDATION → BETTER CONTEXT
```

Every completed cycle improves the repository's accumulated knowledge. Code that passes verification teaches the system something true about the project. Code that fails teaches the system something true about its current understanding.

### The Durability Test

> If killing every agent destroys important knowledge, that knowledge was stored in the wrong place.

Eventually you should be able to throw away every running agent, restart the entire swarm, use different models, move the project to another machine — and because the authoritative knowledge exists in durable context, the new swarm continues where the previous one stopped. That is the test.

---

## Alternatives Considered and Rejected

| Alternative | Why rejected |
|---|---|
| Prompt-driven development | Knowledge lives in ephemeral sessions. When the session ends, the reasoning is gone. The next session starts from scratch. No durable memory, no continuity. |
| Code-first development | Lacks an authoritative intent model. Code describes what exists, not why it exists or what should exist. Without intent context, agents cannot distinguish correct from incorrect behavior, only matching from non-matching syntax. |

---

## Consequences

### Positive

- Agents become replaceable execution resources. Any model, any role, any session can resume work because context is durable.
- Knowledge survives agent restarts, model swaps, and machine moves. The swarm is resilient.
- Every lifecycle stage produces durable context consumed by the next. No stage works in isolation.
- The system can be audited: every code change traces back through the pipeline to original human intent.
- Model swaps are transparent. The context substrate is model-agnostic.

### Negative / Costs

- Requires disciplined context production at every lifecycle stage. Each stage must write its decisions, rationale, and constraints into durable artifacts before the next stage can act.
- Context graph governance is ongoing effort. As the project grows, context must be pruned, reorganized, and kept consistent.
- The pipeline adds overhead compared to ad-hoc prompt-and-response. This overhead is the cost of durability.

---

## References

- Plan: `.omo/plans/context-is-source.md`
- Constitution preamble: `.codebot/constitution.md`
- Entrypoint: `ENTRYPOINT.md`
- Related ADR: `docs/adr/007-authoritative-dispatch.md`
- Active plans: `.omo/plans/user-agent-role.md`, `.omo/plans/module-design-docs.md`, `.omo/plans/ticket-context-carrier.md`, `.omo/plans/context-control-plane.md`
