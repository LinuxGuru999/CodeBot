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
2. **README.md** — architecture overview and quick start
3. **docs/GOALS.md** — what we're building and why (49 goals)
4. **docs/ARCHITECTURE.md** — module inventory and data flow
5. **ROADMAP.md** — the full engineering path (86 sections)
6. **.codebot/constitution.md** — invariants you must not weaken

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
