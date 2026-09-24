## Core Thesis

> **Context is the source. Code is the artifact.**

Software development should begin with an authoritative model of intent, constraints, architecture, and reality — not with source code. The lifecycle is effectively a compiler pipeline:

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

> **If killing every agent destroys important knowledge, that knowledge was stored in the wrong place.**

---

# ENTRYPOINT — How to Enter the CodeBot Codebase

Start here. This file orients new engineers (human or agent) to the system in under five minutes.

## Run It

```bash
# Lifecycle management (preferred)
./codebotctl start | stop | restart | status | clear

# Or directly
python3 -m codebot serve --project .
```

Tests:

```bash
python3 -m pytest -q
```

Stdlib-only. No runtime dependencies beyond Python 3.14+ (pytest for dev).

## Read In This Order

1. **This file** — orientation
2. **README.md** — product vision and quick start
3. **docs/PRODUCT.md** — complete product definition
4. **docs/GOALS.md** — what we're building and why (49 goals)
5. **docs/ARCHITECTURE.md** — module inventory and data flow
6. **ROADMAP.md** — the full engineering path (86 sections)
7. **.codebot/constitution.md** — invariants you must not weaken

## The Core Loop

```
orchestrator.py          process lifecycle, spawn gating, health loop
    │
    ├── ticket_engine.py        tickets: 14-state machine, dedup, persistence
    ├── adaptive_scheduler.py   30-slot demand-driven worker allocation
    ├── api_runner.py           the agent: LLM loop, tools, claims (commit happens at COMPLETE, not here)
    ├── quality_gate.py         deterministic verification (YAML policy)
    └── gatekeeper.py           sole authority for COMPLETE transitions
```

Everything else supports these six modules.

## Key Directories

| Path | Contents |
|------|----------|
| `codebot/` | 75 portable core modules (no project knowledge) |
| `codebot/roles/` | 40 role prompt templates |
| `codebot/adapters/` | ProjectAdapter implementations |
| `tests/` | 109 test files |
| `docs/` | Architecture, API, goals, module docs, ADRs |
| `.codebot/` | Project contract: project.yaml, constitution.md, quality_gates.yaml, capability_matrix.yaml |
| `logs/` | Orchestrator and per-agent logs |

## Non-Obvious Facts

- **No human escalation.** Sensitive changes (auth, migrations, secrets) generate QA-stage recommendation tickets resolved autonomously (§14).
- **Claims are file-based**, created atomically with `open('x')`; TTL 1800s.
- **Quality gates scope to changed files** — py_compile skips non-Python diffs; pytest scopes to affected test modules.
- **The orchestrator drains** via `.codebot/state/.drain`; if it crashes on restart, `rm -f .codebot/state/.drain .codebot/state/.restart && ./codebotctl start`.
- **Capability honesty**: check `.codebot/capability_matrix.yaml` before claiming CodeBot can do something.
