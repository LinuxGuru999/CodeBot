# ticket-context-carrier - Work Plan

## TL;DR (For humans)

**What you'll get:** Tickets become self-contained context carriers. Every lifecycle stage (user agent, discovery, decomposition, planning, review) writes its decisions, rationale, and constraints into a namespaced section on the ticket itself. When an agent is dispatched for a ticket, it receives all prior context automatically in its prompt — no hunting for external plan files, scratchpads, or scattered documentation. The ticket carries its own history.

**Why this approach:** CODING_STANDARDS §22 says NO CONTEXT, NO DISPATCH. Currently tickets carry structural metadata (title, severity, modules) but not the *reasoning* from earlier stages. An implementer doesn't know why the planner chose a specific strategy. A reviewer doesn't know what the decomposer considered atomic. By adding a `lifecycle_context` dictionary to the Ticket, each stage deposits its authoritative context where the next stage can always find it. This implements the principle that the lifecycle is a context compiler.

**What it will NOT do:** It will not change the ticket state machine, bump the schema version, require migrations, modify the scratchpad system, or add context fields to DiscoveryFinding. Purely additive.

**Effort:** Medium
**Risk:** Low - additive optional field with empty default, backward-compatible with all existing tickets
**Decisions to sanity-check:** dict[str, Any] vs typed fields, 8KB size cap, namespace keys

Your next move: approve and run `$start-work`, or request high-accuracy review.

---

> TL;DR (machine): Medium/Low risk. 5 todos across 2 waves. Adds lifecycle_context dict field to Ticket dataclass + create_ticket, adds set_lifecycle_context() to TicketStore, updates _load_ticket_context to render namespaced context into prompt injection, wires stage writers into transition handlers, 5 architecture tests. Schema stays 3.0 (backward-compatible additive field).

## Normative Preamble

This plan implements the foundational doctrine: Context is the source. Code is the artifact. (ADR-008)

This plan implements the **CONTEXT IS THE CONTROL PLANE** invariant (CODING_STANDARDS.md §22) at the ticket level. The lifecycle is a context compiler: each stage produces durable context consumed by the next. Currently, `_load_ticket_context()` injects only structural metadata (title, class, severity, problem_statement, acceptance_criteria). It does not inject the *reasoning* produced by earlier stages.

Context ownership per lifecycle stage maps to namespaced keys within `lifecycle_context`:

| Namespace Key | Stage | Content |
|---|---|---|
| `user_intent` | User Agent | Original request, clarification history, architectural implications, doc changes made |
| `discovery` | Discovery/Triage | Evidence details, severity rationale, duplication analysis, scope assessment |
| `decomposition` | Decomposition | Atomic boundary rationale, dependency graph explanation, component impact analysis |
| `planning` | Planning | Implementation strategy, invariant list, target files/symbols, risk assessment, acceptance criteria rationale |
| `review` | Review | Findings summary, correctness verdicts, rework instructions, documentation drift notes |

A ticket may advance only when the producing stage has written its context namespace. The next stage's agent reads all prior namespaces before acting.

## Context Namespace Schemas

Each namespace is a `dict[str, Any]` with a defined shape. Agents write these; `_load_ticket_context` renders them.

```python
# user_intent (written by user_agent on REQUESTED→COMPLETE)
{
    "original_request": str,       # the raw human message
    "evaluation": str,             # user_agent's assessment
    "clarifications": list[str],   # questions asked and answers received
    "doc_changes": list[str],      # paths of docs updated
    "findings_emitted": list[str], # finding_ids produced
}

# discovery (written by triage/discovery on DISCOVERED→TRIAGED)
{
    "evidence_detail": str,        # expanded beyond the one-liner evidence field
    "severity_rationale": str,     # why this severity was chosen
    "duplication_analysis": str,   # dedup findings
    "scope_assessment": str,       # blast radius analysis
}

# decomposition (written by decomposer on DECOMP→PLANNING)
{
    "boundary_rationale": str,     # why these atomic boundaries
    "dependency_graph": str,       # textual dependency explanation
    "component_impact": list[str], # affected components with rationale
    "sub_tickets": list[str],      # IDs of child tickets if split
}

# planning (written by planner on PLANNING→IMPLEMENT)
{
    "strategy": str,               # implementation approach
    "invariants": list[str],       # MUST/MUST NOT rules for implementer
    "target_files": list[str],     # exact files to modify
    "target_symbols": list[str],   # exact functions/classes to change
    "risk_assessment": str,        # what could go wrong
    "test_strategy": str,          # how to verify
    "rollback_plan": str,          # how to undo
}

# review (written by reviewers on REVIEW→COMPLETE or REVIEW→REWORK)
{
    "verdict_summary": str,        # APPROVE/REWORK with rationale
    "findings": list[dict],        # structured findings
    "rework_instructions": str,    # what to fix if REWORK
    "doc_drift_notes": str,        # documentation inconsistencies found
}
```

## Architecture

```
  LIFECYCLE STAGE           WRITES TO TICKET              NEXT AGENT READS
  ───────────────          ──────────────────            ──────────────────

  User Agent         →  lifecycle_context["user_intent"]  → Discovery/Triage
  Discovery/Triage   →  lifecycle_context["discovery"]    → Decomposer
  Decomposer         →  lifecycle_context["decomposition"]→ Planner
  Planner            →  lifecycle_context["planning"]     → Implementer
  Implementer        →  (code changes, not context)       → Reviewer
  Reviewer           →  lifecycle_context["review"]       → Next cycle/Complete

  _load_ticket_context() renders ALL populated namespaces
  into the prompt injection block for ANY dispatched agent.
```

## Scope
### Must have
- `lifecycle_context: dict[str, Any]` field on Ticket dataclass (default `field(default_factory=dict)`)
- `lifecycle_context` parameter on `create_ticket()` function
- `set_lifecycle_context(namespace: str, data: dict)` method on TicketStore
- 8KB serialized size cap enforced in `set_lifecycle_context()`
- `_load_ticket_context()` renders populated namespaces into prompt injection
- Stage writers wired into transition handlers for each lifecycle stage
- 5 architecture tests verifying context propagation

### Must NOT have (guardrails, anti-slop, scope boundaries)
- No schema version bump (stays 3.0, additive optional field)
- No migration logic (old tickets deserialize with empty dict)
- No changes to Ticket.transition() state machine or TRANSITIONS dict
- No modifications to scratchpad system (complementary, not replaced)
- No context fields on DiscoveryFinding
- No changes to ticket JSON serialization format beyond the new field
- No third-party dependencies

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after + pytest
- Evidence: .omo/evidence/task-<N>-ticket-context-carrier.md

## Execution strategy
### Parallel execution waves
Wave 1: Schema + Store (todos 1, 2) — parallel, no dependencies
Wave 2: Injection + Writers + Tests (todos 3, 4, 5) — 3 depends on 1, 4 depends on 1+2, 5 depends on all

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1 | none | 3, 4, 5 | 2 |
| 2 | none | 4, 5 | 1 |
| 3 | 1 | 5 | 4 |
| 4 | 1, 2 | 5 | 3 |
| 5 | 3, 4 | none | none |

## Todos
> Implementation + Test = ONE todo. Never separate.

- [ ] 1. Add lifecycle_context field to Ticket dataclass and create_ticket()
  What to do / Must NOT do: Two changes in `codebot/ticket_engine.py`. (A) Add `lifecycle_context: dict[str, Any] = field(default_factory=dict)` to the Ticket dataclass after `current_attempt_id` (after line 354). This is an additive optional field — old tickets without it deserialize correctly because `field(default_factory=dict)` provides the default. (B) Add `lifecycle_context: dict[str, Any] | None = None` parameter to `create_ticket()` function signature (after `fingerprint` at line 460), and pass `lifecycle_context=lifecycle_context or {}` to the Ticket constructor call (around line 502). Must NOT change SCHEMA_VERSION. Must NOT modify Ticket.transition() — context is set via TicketStore method, not through transitions. Must NOT add migration logic.
  Parallelization: Wave 1 | Blocked by: none | Blocks: 3, 4, 5
  References (executor has NO interview context - be exhaustive): codebot/ticket_engine.py:297-354 (Ticket dataclass), codebot/ticket_engine.py:352-354 (last fields before insertion point), codebot/ticket_engine.py:434-460 (create_ticket signature), codebot/ticket_engine.py:472-503 (Ticket constructor call), codebot/ticket_engine.py:10 (SCHEMA_VERSION = "3.0" — do NOT change)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.ticket_engine import Ticket, TicketState, TicketClass, Severity, RiskLevel, create_ticket
import inspect
# Verify field exists on dataclass
assert 'lifecycle_context' in Ticket.__dataclass_fields__
# Verify default is empty dict
t = Ticket(id='test', title='t', ticket_class=TicketClass.FEATURE, severity=Severity.LOW, state=TicketState.DISCOVERED, source='test', evidence='e', problem_statement='p', desired_state='d', acceptance_criteria=['a'], affected_modules=[], dependencies=[], risk=RiskLevel.LOW, blast_radius='', security_impact='', migration_impact='', required_reviewers=[], required_tests=[], documentation_requirements=[], rollback_strategy='', estimated_cost_tokens=0, created_at=0.0, updated_at=0.0)
assert t.lifecycle_context == {}
# Verify create_ticket accepts it
sig = inspect.signature(create_ticket)
assert 'lifecycle_context' in sig.parameters
t2 = create_ticket(title='t', ticket_class=TicketClass.FEATURE, severity=Severity.LOW, source='test', evidence='e', problem_statement='p', desired_state='d', acceptance_criteria=['a'], lifecycle_context={'planning': {'strategy': 'test'}})
assert t2.lifecycle_context == {'planning': {'strategy': 'test'}}
# Verify backward compat: create_ticket without lifecycle_context
t3 = create_ticket(title='t', ticket_class=TicketClass.FEATURE, severity=Severity.LOW, source='test', evidence='e', problem_statement='p', desired_state='d', acceptance_criteria=['a'])
assert t3.lifecycle_context == {}
print('Ticket field OK')
"`
  QA scenarios (name the exact tool + invocation): happy: field exists, defaults to {}, create_ticket accepts it, backward compatible without it. failure: old serialized ticket without lifecycle_context deserializes correctly (test via json round-trip). Evidence .omo/evidence/task-1-ticket-context-carrier.md
  Commit: Y | feat(engine): add lifecycle_context field to Ticket for context-as-control-plane

- [ ] 2. Add set_lifecycle_context() to TicketStore with 8KB size cap
  What to do / Must NOT do: Add method `set_lifecycle_context(self, ticket_id: str, namespace: str, data: dict[str, Any]) -> bool` to `TicketStore` class in `codebot/ticket_engine.py`. Valid namespaces: `frozenset({"user_intent", "discovery", "decomposition", "planning", "review"})`. Logic: (1) Validate namespace is in valid set, raise ValueError if not. (2) Get ticket by ID, return False if not found. (3) Build new lifecycle_context by copying existing dict and setting `namespace: data`. (4) Serialize the new lifecycle_context to JSON and check `len(json_bytes) <= 8192`. If exceeds, raise ValueError with message indicating which namespace caused overflow and current size. (5) Create new Ticket via `ticket.transition(ticket.state)` pattern (or direct reconstruction) with updated lifecycle_context. Since Ticket is frozen, use `Ticket(**{**asdict(ticket), "lifecycle_context": new_ctx, "updated_at": time.time()})`. (6) Save to store. Return True. Must NOT change ticket state. Must NOT trigger side effects. Must be atomic (use existing store lock).
  Parallelization: Wave 1 | Blocked by: none | Blocks: 4, 5
  References (executor has NO interview context - be exhaustive): codebot/ticket_engine.py:524+ (TicketStore class), codebot/ticket_engine.py:1524 (store.get method), codebot/ticket_engine.py:1436 (store.add method), codebot/ticket_engine.py:387-389 (to_dict pattern), codebot/ticket_engine.py:360-385 (Ticket.transition for frozen update pattern)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.ticket_engine import TicketStore, Ticket, TicketState, TicketClass, Severity, RiskLevel
import tempfile, json
from pathlib import Path
with tempfile.TemporaryDirectory() as td:
    store = TicketStore(Path(td) / 'tickets.json')
    t = Ticket(id='CTX-1', title='t', ticket_class=TicketClass.FEATURE, severity=Severity.LOW, state=TicketState.DISCOVERED, source='test', evidence='e', problem_statement='p', desired_state='d', acceptance_criteria=['a'], affected_modules=[], dependencies=[], risk=RiskLevel.LOW, blast_radius='', security_impact='', migration_impact='', required_reviewers=[], required_tests=[], documentation_requirements=[], rollback_strategy='', estimated_cost_tokens=0, created_at=0.0, updated_at=0.0)
    store.add(t)
    # Happy path
    ok = store.set_lifecycle_context('CTX-1', 'planning', {'strategy': 'refactor X', 'invariants': ['must not break Y']})
    assert ok is True
    t2 = store.get('CTX-1')
    assert t2.lifecycle_context == {'planning': {'strategy': 'refactor X', 'invariants': ['must not break Y']}}
    # Invalid namespace
    try:
        store.set_lifecycle_context('CTX-1', 'invalid_ns', {})
        assert False, 'Should raise ValueError'
    except ValueError:
        pass
    # Size cap
    try:
        store.set_lifecycle_context('CTX-1', 'planning', {'x': 'A' * 10000})
        assert False, 'Should raise ValueError for size'
    except ValueError as e:
        assert '8192' in str(e) or 'size' in str(e).lower()
    print('set_lifecycle_context OK')
"`
  QA scenarios (name the exact tool + invocation): happy: set namespace, verify persisted. failure: invalid namespace raises ValueError. failure: oversized context raises ValueError. failure: nonexistent ticket returns False. Evidence .omo/evidence/task-2-ticket-context-carrier.md
  Commit: Y | feat(engine): add set_lifecycle_context() with namespace validation and 8KB cap

- [ ] 3. Update _load_ticket_context() to render lifecycle_context namespaces
  What to do / Must NOT do: Modify `_load_ticket_context()` at `codebot/process_manager.py:789-815` to append rendered lifecycle_context namespaces after the existing acceptance criteria block and before `--- END TICKET CONTEXT ---`. For each populated namespace in `ticket.lifecycle_context`, render a section:
  ```
  --- LIFECYCLE CONTEXT: {namespace} ---
  {key}: {value}
  {key}: {value}
  --- END {namespace} ---
  ```
  Skip empty namespaces. Render order matches lifecycle flow: user_intent → discovery → decomposition → planning → review. For nested dicts/lists, use `json.dumps(value, indent=2)` for readability. Must NOT change the existing context fields (title, class, severity, etc.). Must NOT exceed reasonable prompt size — if total context exceeds 4000 chars, truncate with a warning note.
  Parallelization: Wave 2 | Blocked by: 1 | Blocks: 5
  References (executor has NO interview context - be exhaustive): codebot/process_manager.py:789-815 (_load_ticket_context full method), codebot/process_manager.py:864-868 (where context is injected into prompt)
  Acceptance criteria (agent-executable): `python3 -c "
# Requires a ticket with lifecycle_context set
# Mock test: verify the rendering logic
ctx = {'planning': {'strategy': 'refactor X', 'invariants': ['must preserve Y']}, 'discovery': {'evidence_detail': 'found in Z'}}
rendered_parts = []
for ns in ['user_intent', 'discovery', 'decomposition', 'planning', 'review']:
    if ns in ctx:
        rendered_parts.append(f'--- LIFECYCLE CONTEXT: {ns} ---')
        for k, v in ctx[ns].items():
            rendered_parts.append(f'{k}: {v}')
        rendered_parts.append(f'--- END {ns} ---')
output = '\n'.join(rendered_parts)
assert 'LIFECYCLE CONTEXT: discovery' in output
assert 'LIFECYCLE CONTEXT: planning' in output
assert 'refactor X' in output
assert 'user_intent' not in output  # empty, should be skipped
print('Rendering OK')
"`
  QA scenarios (name the exact tool + invocation): happy: ticket with 3 namespaces renders all 3 in correct order. happy: ticket with empty lifecycle_context renders same as before (no extra output). failure: oversized context truncates gracefully. Evidence .omo/evidence/task-3-ticket-context-carrier.md
  Commit: Y | feat(prompt): render lifecycle_context namespaces in ticket context injection

- [ ] 4. Wire lifecycle context writes into transition handlers
  What to do / Must NOT do: Add context-writing calls at each lifecycle transition point. (A) In `dispatch_service.py:transition_ticket_on_success()`, after the `user_agent` branch transitions REQUESTED→COMPLETE, write `user_intent` context from the UserRequest data. (B) In the triage fast-path or goal_aligner completion handler, write `discovery` context when transitioning TRIAGED→GOAL or GOAL→DECOMP. (C) In the decomposer completion handler (PLANNING transition), write `decomposition` context. (D) In the planner completion handler (IMPLEMENT transition), write `planning` context from the plan data. (E) In `advance_reviewed_tickets()` or reviewer completion, write `review` context. Each write calls `store.set_lifecycle_context(ticket_id, namespace, data)`. Wrap each in try/except so context write failures do NOT block the state transition. Log warnings on failure. Must NOT change transition logic. Must NOT prevent transitions if context write fails.
  Parallelization: Wave 2 | Blocked by: 1, 2 | Blocks: 5
  References (executor has NO interview context - be exhaustive): codebot/dispatch_service.py:241-385 (transition_ticket_on_success with all role branches), codebot/ticket_dispatcher.py (advance_reviewed_tickets, dispatch_decompose_agents, dispatch_planning_agents), codebot/ticket_engine.py:set_lifecycle_context (todo 2)
  Acceptance criteria (agent-executable): `python3 -c "
import ast
src = open('codebot/dispatch_service.py').read()
assert 'set_lifecycle_context' in src
assert 'user_intent' in src or 'planning' in src
print('Transition wiring present')
"`
  QA scenarios (name the exact tool + invocation): happy: planner completes, planning context appears on ticket. failure: context write fails, transition still succeeds (graceful degradation). Evidence .omo/evidence/task-4-ticket-context-carrier.md
  Commit: Y | feat(dispatch): wire lifecycle context writes into transition handlers

- [ ] 5. Architecture tests for context propagation
  What to do / Must NOT do: Create `tests/test_ticket_context_carrier.py` with 5 tests: (1) `test_lifecycle_context_default_empty` — new tickets have empty lifecycle_context. (2) `test_set_lifecycle_context_persists` — write namespace, reload ticket, verify data. (3) `test_invalid_namespace_rejected` — ValueError for unknown namespace. (4) `test_size_cap_enforced` — ValueError when context exceeds 8KB. (5) `test_load_ticket_context_renders_namespaces` — mock ticket with populated lifecycle_context, call _load_ticket_context, verify rendered output contains all namespace headers and values. All use tmp_path. Deterministic. No network. No running orchestrator.
  Parallelization: Wave 2 | Blocked by: 3, 4 | Blocks: none
  References (executor has NO interview context - be exhaustive): tests/test_arch_authoritative_dispatch.py (test patterns), codebot/ticket_engine.py (Ticket, TicketStore, set_lifecycle_context), codebot/process_manager.py (_load_ticket_context)
  Acceptance criteria (agent-executable): `python3 -m pytest tests/test_ticket_context_carrier.py -v --tb=short` passes all 5
  QA scenarios (name the exact tool + invocation): happy: all 5 pass. failure: remove lifecycle_context field → test 1 fails. failure: remove size cap → test 4 fails. Evidence .omo/evidence/task-5-ticket-context-carrier.md
  Commit: Y | test(context): add 5 architecture tests for lifecycle context propagation

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Schema compatibility — serialize old ticket (without lifecycle_context), deserialize with new code, verify no crash and empty dict default
- [ ] F2. Context propagation E2E — create ticket, simulate planner writing planning context, verify _load_ticket_context renders it
- [ ] F3. Size enforcement — attempt to write 10KB context, verify rejection
- [ ] F4. Graceful degradation — mock set_lifecycle_context failure, verify transition still succeeds

## Commit strategy
One commit per todo (5 commits). Ordered: 1, 2 → 3, 4 → 5. Atomic: each passes independently. No squash unless requested.

## Success criteria
- Ticket dataclass has `lifecycle_context` field defaulting to empty dict
- `create_ticket()` accepts optional `lifecycle_context` parameter
- `TicketStore.set_lifecycle_context()` writes namespaced context with validation and 8KB cap
- `_load_ticket_context()` renders populated namespaces into agent prompt injection
- Transition handlers write context at each lifecycle stage boundary
- Old tickets without lifecycle_context deserialize without errors
- All 5 architecture tests pass
- Zero changes to state machine, schema version, or scratchpad system
