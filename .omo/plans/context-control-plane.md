# context-control-plane - Work Plan

## TL;DR (For humans)

**What you'll get:** A context control plane that turns documentation from passive reference material into the swarm's executable control surface. Every piece of knowledge in the system is classified (fact, decision, requirement, hypothesis, discovery, constraint, current-state, desired-state), attributed (who wrote it, when, under what authority, backed by what evidence), and scoped (each agent sees only the context layers relevant to its role and task). When an agent discovers that code contradicts documentation, it generates a conflict finding instead of silently rewriting the docs. When a reviewer evaluates code, they also verify that code, tests, requirements, architecture, and documentation all agree. The result: agents become replaceable because the project's accumulated knowledge — not any single agent's cleverness — is the competitive advantage.

**Why this approach:** The swarm must not operate from vague conversational memory or ad-hoc reconstruction. It operates from controlled, versioned, lifecycle-produced context. By classifying context kinds, enforcing authority hierarchies, scoping delivery per role, and requiring multi-dimensional agreement at completion, we prevent the most dangerous failure mode: agents confidently building on wrong assumptions that other agents then treat as truth. Documentation becomes the control plane; the lifecycle compiles it; dispatch delivers it; agents act on it.

**What it will NOT do:** It will not modify the ticket state machine, change dispatch mechanics, add external databases, or alter how discovery auditors produce findings. Pure context infrastructure layered on top of existing systems.

**Effort:** Large
**Risk:** Medium - touches prompt injection path which affects every dispatched agent
**Decisions to sanity-check:** Context kind taxonomy, authority hierarchy, 5-tier scoping model, conflict generation vs silent correction, review validation dimensions

Your next move: approve and run `$start-work`, or request high-accuracy review.

---

> TL;DR (machine): Large/Medium risk. 7 todos across 3 waves. Defines ContextItem schema with 8 kinds + provenance, builds 5-tier ContextPackage assembler, wires into _load_ticket_context, adds conflict detection protocol, adds review-stage context validation, amends constitution §5, 8 regression tests.

## Normative Preamble

This plan implements the foundational doctrine: Context is the source. Code is the artifact. (ADR-008)

This plan implements the principle: **the swarm should be replaceable; the context should be durable.**

CodeBot controls agent behavior primarily through authoritative context. The agent swarm must never depend on informal conversational memory, hidden assumptions, or ad-hoc reconstruction of project intent. Every important decision, requirement, architectural constraint, lifecycle result, rejection, decomposition, implementation plan, and review outcome must be converted into durable project context.

Documentation is therefore not passive reference material. It is part of the swarm control plane.

The lifecycle exists partly to create, refine, validate, and constrain that context in a controlled sequence:

```
REQUEST → EVALUATE → DOCUMENT → DECOMPOSE → PLAN → IMPLEMENT → REVIEW → COMPLETE
```

Each stage consumes the authoritative context produced by earlier stages and may only modify the portions of context that stage owns.

### Core Principles

1. **NO CONTEXT, NO DISPATCH.** Agents must not be expected to infer critical project intent that should have been produced by an earlier lifecycle stage.

2. **Context has classification.** Every durable fact is tagged: FACT, DECISION, REQUIREMENT, HYPOTHESIS, DISCOVERY, CONSTRAINT, CURRENT_STATE, or DESIRED_STATE. An agent inference is not a fact until validated.

3. **Context has provenance.** Every context item records: source, author_role, created_at, authority level, related_ticket, evidence.

4. **Context has scope.** Not every agent gets every document. Context is assembled in 5 tiers: GLOBAL → ROLE → WORKSTREAM → TICKET → LOCAL_CODE.

5. **Agents discover; lifecycle decides.** An agent may discover that architecture docs contradict code. It must NOT silently rewrite the docs. It generates a CONTEXT_CONFLICT finding that flows through the lifecycle for resolution.

6. **Completion is multi-dimensional agreement.** DONE means: CODE agrees with TESTS, TESTS agree with REQUIREMENTS, REQUIREMENTS agree with ARCHITECTURE, DOCUMENTATION agrees with REALITY.

7. **Context becomes progressively more precise.** Human intent ("make auth secure") is refined through each lifecycle stage into atomic, constrained, evidenced work items.

### Context Classification Taxonomy

| Kind | Definition | Who Produces | Authority |
|------|-----------|-------------|----------|
| FACT | Observed directly from repository/runtime | Any agent reading code | High (verifiable) |
| DECISION | Explicitly decided by human or authorized lifecycle stage | User agent, planner, human | Highest |
| REQUIREMENT | Desired behavior specification | User agent, human | High |
| HYPOTHESIS | Agent inference awaiting validation | Any agent | Low (must be confirmed) |
| DISCOVERY | Evidence found by an agent | Discovery auditors | Medium (needs triage) |
| CONSTRAINT | Rule implementation must obey | Constitution, ADRs, coding standards | Highest |
| CURRENT_STATE | What the software demonstrably does now | Implementation, review | High (observable) |
| DESIRED_STATE | What the software is intended to do | User agent, requirements | High (authoritative intent) |

### Authority Hierarchy (for conflict resolution)

```
Constitution (.codebot/constitution.md)     ← IMMUTABLE by agents
    ↓
ADRs (docs/adr/*.md)                        ← PROPOSABLE by agents, APPROVED by lifecycle
    ↓
Coding Standards (docs/CODING_STANDARDS.md) ← AMENDABLE via plan
    ↓
Architecture (docs/ARCHITECTURE.md)         ← AMENDABLE via plan
    ↓
Ticket context (lifecycle_context)          ← PRODUCED by lifecycle stages
    ↓
Agent inference (HYPOTHESIS)                ← LOWEST authority, must be validated
```

When conflict is detected between levels, the higher authority wins. Agents NEVER silently override higher authority.

### Context Scoping Tiers

```
┌─────────────────────────────────────────────────┐
│ TIER 1: GLOBAL CONTEXT (always included)        │
│   constitution, coding_standards, architecture  │
│   invariants, product purpose                   │
│   ~500 tokens max                               │
├─────────────────────────────────────────────────┤
│ TIER 2: ROLE CONTEXT (included per role)        │
│   implementer rules, reviewer rules,            │
│   planner rules, user_agent rules               │
│   ~300 tokens max                               │
├─────────────────────────────────────────────────┤
│ TIER 3: WORKSTREAM CONTEXT (per module/area)    │
│   module design doc (.md), relevant ADRs,       │
│   related tickets                               │
│   ~500 tokens max                               │
├─────────────────────────────────────────────────┤
│ TIER 4: TICKET CONTEXT (per ticket)             │
│   problem, dependencies, acceptance criteria,   │
│   evidence, lifecycle_context namespaces        │
│   ~800 tokens max                               │
├─────────────────────────────────────────────────┤
│ TIER 5: LOCAL CODE CONTEXT (per task)           │
│   files/symbols being modified,                 │
│   relevant test files                           │
│   ~variable, bounded by prompt budget           │
└─────────────────────────────────────────────────┘
```

Total context budget per agent: ~3000-4000 tokens. Prevents noise, cost explosion, and conflicting signals at 30+ concurrent agents.

## Architecture

```
  HUMAN INTENT
       │
       ▼
  USER AGENT (evaluates, classifies)
       │
       ├── writes DESIRED_STATE context
       ├── writes REQUIREMENT context
       ├── writes DECISION context
       └── emits Findings (classified DISCOVERY)
              │
              ▼
  PLATFORM INGESTION
       │
       ├── stores context with provenance
       ├── classifies each item by kind
       └── assigns authority level
              │
              ▼
  DISPATCH (builds ContextPackage)
       │
       ├── TIER 1: loads global docs
       ├── TIER 2: loads role-specific rules
       ├── TIER 3: loads module design + ADRs
       ├── TIER 4: loads ticket + lifecycle_context
       └── TIER 5: identifies affected code
              │
              ▼
  AGENT (receives scoped context package)
       │
       ├── acts within constraints
       ├── discovers facts
       ├── if contradiction found → CONTEXT_CONFLICT finding
       └── produces output + new context
              │
              ▼
  REVIEW (validates multi-dimensional agreement)
       │
       ├── CODE ↔ TESTS
       ├── TESTS ↔ REQUIREMENTS
       ├── REQUIREMENTS ↔ ARCHITECTURE
       └── DOCUMENTATION ↔ REALITY
              │
              ▼
  COMPLETION (context reconciled, durable)
       │
       ▼
  UPDATED AUTHORITATIVE CONTEXT
  (swarm is smarter than before)
```

## Scope
### Must have
- `ContextItem` frozen dataclass with kind, provenance, content, authority fields
- `ContextKind` enum with 8 values: FACT, DECISION, REQUIREMENT, HYPOTHESIS, DISCOVERY, CONSTRAINT, CURRENT_STATE, DESIRED_STATE
- `AuthorityLevel` enum with hierarchical ordering
- `ContextPackage` builder function assembling 5 tiers per role
- Integration into `_load_ticket_context()` / `_prepare_prompt_with_context()`
- Conflict detection protocol: agents generate CONTEXT_CONFLICT findings, never silently rewrite
- Review-stage context validation: 4-dimension agreement check
- Constitution §5 amendment: Context Authority rules
- Role prompt updates: conflict detection instructions for all roles
- 8 regression tests

### Must NOT have (guardrails, anti-slop, scope boundaries)
- No modifications to ticket state machine or TRANSITIONS dict
- No changes to dispatch mechanics (BucketDispatcher, DispatchGate, SpawnQueue)
- No external database or storage beyond existing file-based system
- No changes to discovery_daemon.py finding production
- No modification of existing .py behavior beyond context injection path
- No context delivery exceeding 4000 tokens per agent (hard cap)
- No agent may modify files above its authority level without lifecycle approval

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after + pytest
- Evidence: .omo/evidence/task-<N>-context-control-plane.md

## Execution strategy
### Parallel execution waves
Wave 1: Schema (todo 1) — foundation, no dependencies
Wave 2: Builder + Injection + Conflict (todos 2, 3, 4) — 2 blocks 3, 4 parallel with 2
Wave 3: Validation + Constitution + Tests (todos 5, 6, 7) — 5,6 parallel, 7 blocked by all

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1 | none | 2, 3, 4, 5 | none |
| 2 | 1 | 3 | 4 |
| 3 | 2 | 7 | 4 |
| 4 | 1 | 5, 7 | 2 |
| 5 | 4 | 7 | 6 |
| 6 | none | 7 | 5 |
| 7 | 3, 4, 5, 6 | none | none |

## Todos
> Implementation + Test = ONE todo. Never separate.

- [ ] 1. Create ContextItem schema with classification and provenance
  What to do / Must NOT do: Create `codebot/context_item.py` containing: (A) `ContextKind(str, Enum)` with values: FACT, DECISION, REQUIREMENT, HYPOTHESIS, DISCOVERY, CONSTRAINT, CURRENT_STATE, DESIRED_STATE. (B) `AuthorityLevel(str, Enum)` with ordered values: CONSTITUTION="constitution", ADR="adr", CODING_STANDARD="coding_standard", ARCHITECTURE="architecture", TICKET="ticket", AGENT_INFERENCE="agent_inference". Add comparison methods so CONSTITUTION > ADR > ... > AGENT_INFERENCE. (C) Frozen `ContextItem` dataclass with fields: `kind: ContextKind`, `authority: AuthorityLevel`, `source: str` (file path or "user_request"), `author_role: str` (role that produced it), `created_at: float`, `related_ticket: str` (default ""), `evidence: str` (file:line or code snippet), `content: str` (the actual context text), `updated_at: float` (default same as created_at). (D) Methods: `to_dict() -> dict`, `from_dict(data) -> ContextItem`, `to_json() -> str`. (E) Validation: `from_dict` raises ValueError if kind not in ContextKind, if authority not in AuthorityLevel, if content is empty, if source is empty. (F) Helper function `classify_context(text: str, hint: str = "") -> ContextKind` that applies simple heuristics: text containing "MUST"/"SHALL"/"required" → CONSTRAINT or REQUIREMENT; text referencing a file path with observed behavior → FACT or CURRENT_STATE; text containing "we decided"/"ADR" → DECISION; text containing "I think"/"possibly"/"might" → HYPOTHESIS. This is a heuristic aid, not authoritative — lifecycle stages set the real kind. Must NOT import TicketStore. Must NOT use third-party deps. stdlib only.
  Parallelization: Wave 1 | Blocked by: none | Blocks: 2, 3, 4, 5
  References (executor has NO interview context - be exhaustive): codebot/discovery_finding.py:246-272 (frozen dataclass pattern), codebot/ticket_engine.py:87-105 (enum pattern), codebot/ticket_engine.py:360-370 (validation pattern)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.context_item import ContextItem, ContextKind, AuthorityLevel
import time
item = ContextItem(kind=ContextKind.DECISION, authority=AuthorityLevel.ADR, source='docs/adr/007.md', author_role='planner', created_at=time.time(), related_ticket='CB-123', evidence='docs/adr/007.md:42', content='Dispatch must use single authoritative scheduler')
d = item.to_dict()
assert d['kind'] == 'DECISION'
assert d['authority'] == 'adr'
item2 = ContextItem.from_dict(d)
assert item2.content == item.content
# Verify frozen
try:
    item.content = 'changed'
    assert False, 'Should be frozen'
except Exception:
    pass
# Verify authority ordering
assert AuthorityLevel.CONSTITUTION > AuthorityLevel.ADR
assert AuthorityLevel.ADR > AuthorityLevel.AGENT_INFERENCE
# Verify validation
try:
    ContextItem.from_dict({'kind': 'INVALID', 'authority': 'adr', 'source': 'x', 'author_role': 'y', 'created_at': 0, 'content': 'z', 'evidence': 'e'})
    assert False, 'Should reject invalid kind'
except ValueError:
    pass
print('ContextItem OK')
"`
  QA scenarios (name the exact tool + invocation): happy: create, serialize, deserialize, verify frozen, verify authority ordering. failure: invalid kind raises ValueError. failure: empty content raises ValueError. failure: attempt mutation raises exception. Evidence .omo/evidence/task-1-context-control-plane.md
  Commit: Y | feat(context): add ContextItem schema with classification and provenance

- [ ] 2. Build ContextPackage assembler with 5-tier scoping
  What to do / Must NOT do: Create `codebot/context_package.py` containing function `build_context_package(role: str, ticket_id: str, state_dir: Path, project_root: Path) -> str`. This function assembles the scoped context block injected into every agent prompt. Logic: (A) TIER 1 GLOBAL: Read first 30 lines of `.codebot/constitution.md` (purpose + security boundaries summary), first 20 lines of `docs/CODING_STANDARDS.md` (authority statement + invariant list), first 20 lines of `docs/ARCHITECTURE.md` (system overview). Cap at 500 tokens (~2000 chars). If file missing, skip silently. (B) TIER 2 ROLE: Map role to role-specific context. For implementer: read `codebot/roles/implementer.md` HARD CONSTRAINTS section. For reviewer: read reviewer role constraints. For user_agent: read evaluation directives. Cap at 300 tokens (~1200 chars). Use a `ROLE_CONTEXT_MAP: dict[str, str]` mapping base role names to key constraint excerpts. (C) TIER 3 WORKSTREAM: From ticket's `affected_modules`, find matching `codebot/{module}.md` design docs. Read first 40 lines (Purpose + Public API sections). Include relevant ADR titles from `docs/adr/` that reference affected modules. Cap at 500 tokens (~2000 chars). (D) TIER 4 TICKET: Call existing `_load_ticket_context(ticket_id)` logic OR read ticket fields directly. Include lifecycle_context namespaces if populated. Cap at 800 tokens (~3200 chars). (E) TIER 5 LOCAL_CODE: List affected files from ticket. Do NOT inline file contents here (that happens in the role prompt). Just list paths and symbols. Cap at 400 tokens (~1600 chars). (F) TOTAL CAP: Sum all tiers. If total exceeds 4000 tokens (~16000 chars), truncate lowest-priority tiers first (TIER 5 → TIER 3 → TIER 2, never TIER 1 or TIER 4). (G) Format output as structured markdown with clear section headers: `--- CONTEXT PACKAGE ---`, `## Global Context`, `## Role Context`, etc., `--- END CONTEXT PACKAGE ---`. Must NOT modify any source files. Must NOT call TicketStore constructor directly. Must handle missing files gracefully (skip tier, log debug).
  Parallelization: Wave 2 | Blocked by: 1 | Blocks: 3
  References (executor has NO interview context - be exhaustive): codebot/process_manager.py:1208-1241 (_load_ticket_context to integrate with), codebot/process_manager.py:778-838 (_prepare_prompt_with_context where package is used), .codebot/constitution.md (TIER 1 source), docs/CODING_STANDARDS.md (TIER 1 source), docs/ARCHITECTURE.md (TIER 1 source), codebot/roles/implementer.md:10-17 (example TIER 2 source), docs/adr/ (TIER 3 ADR sources)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.context_package import build_context_package
from pathlib import Path
# Mock test with temp dirs
import tempfile
with tempfile.TemporaryDirectory() as td:
    root = Path(td)
    # Create minimal structure
    (root / '.codebot').mkdir()
    (root / '.codebot' / 'constitution.md').write_text('# Constitution\nInvariants here')
    (root / 'docs').mkdir()
    (root / 'docs' / 'CODING_STANDARDS.md').write_text('# Standards\nRules here')
    (root / 'docs' / 'ARCHITECTURE.md').write_text('# Arch\nOverview here')
    pkg = build_context_package('implementer', 'CB-TEST', root / '.codebot' / 'state', root)
    assert 'CONTEXT PACKAGE' in pkg
    assert 'Global Context' in pkg
    assert 'Role Context' in pkg
    assert len(pkg) < 16000, f'Exceeds token cap: {len(pkg)} chars'
    print(f'Package built: {len(pkg)} chars')
print('ContextPackage OK')
"`
  QA scenarios (name the exact tool + invocation): happy: all 5 tiers present, total under 16000 chars. happy: missing constitution → skips TIER 1 gracefully. failure: total exceeds cap → truncation works correctly. Evidence .omo/evidence/task-2-context-control-plane.md
  Commit: Y | feat(context): add 5-tier ContextPackage assembler with token budget

- [ ] 3. Wire ContextPackage into prompt injection path
  What to do / Must NOT do: Modify `_load_ticket_context()` at `codebot/process_manager.py:1208-1241` to delegate to `build_context_package()`. The current function builds a flat text block from ticket fields. Replace it: extract the role from the bot config name (available via caller context or passed as parameter), call `build_context_package(role, ticket_id, state_dir, project_root)`, return the result. If `build_context_package` fails for any reason, fall back to the current flat-text behavior (graceful degradation). Alternatively, modify `_prepare_prompt_with_context()` at line 778 to call `build_context_package` instead of `_load_ticket_context` since it has access to `bot.config.name` for role extraction. Choose whichever integration point has cleaner access to the role name. Must NOT break existing prompt injection for any role. Must NOT change the `--- ASSIGNED TICKET:` / `--- END TICKET CONTEXT ---` markers that role prompts reference — either preserve them within the package format or update all role prompts atomically.
  Parallelization: Wave 2 | Blocked by: 2 | Blocks: 7
  References (executor has NO interview context - be exhaustive): codebot/process_manager.py:1208-1241 (_load_ticket_context), codebot/process_manager.py:778-838 (_prepare_prompt_with_context), codebot/process_manager.py:834-841 (where ticket_ctx is appended to prompt_text)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.process_manager import _load_ticket_context
import inspect
src = inspect.getsource(_load_ticket_context)
assert 'build_context_package' in src or 'context_package' in src
print('Injection wired OK')
"`
  QA scenarios (name the exact tool + invocation): happy: dispatched agent receives structured context package with all tiers. failure: build_context_package crashes → falls back to flat text, no agent crash. Evidence .omo/evidence/task-3-context-control-plane.md
  Commit: Y | feat(prompt): wire ContextPackage into agent prompt injection

- [ ] 4. Implement conflict detection protocol and update role prompts
  What to do / Must NOT do: Two parts. (A) Create `codebot/context_conflict.py` with function `generate_conflict_finding(agent_role: str, authority_source: str, observed_reality: str, contradiction: str, affected_files: list[str]) -> dict`. Returns a DiscoveryFinding-shaped dict with: `discovery_role=agent_role`, `discovery_category="context_conflict"`, `title=f"CONTEXT CONFLICT: {authority_source} vs {observed_reality}"`, `problem_statement=contradiction`, `severity="high"`, `priority="high"`. This standardizes how agents report contradictions. (B) Update ALL role prompts in `codebot/roles/*.md` to include a new section "Context Conflict Protocol": "If you discover that authoritative documentation (constitution, ADRs, architecture docs, coding standards) contradicts what you observe in the code, you MUST NOT silently modify the documentation to match the code, nor modify the code to match incorrect documentation without flagging it. Instead: (1) Document the exact contradiction with file:line references for both sides. (2) Write a context conflict finding using the standard format to `state/findings/conflict-{uuid}.json`. (3) Continue your work using the HIGHER AUTHORITY as ground truth (Constitution > ADR > Coding Standards > Architecture > Ticket > Agent Inference). (4) Note the conflict in your scratchpad so downstream agents are aware." For implementer role specifically add: "If the architecture doc says X but the code requires Z, report the conflict and implement according to the higher authority." For reviewer role specifically add: "Check whether implementation matches documented requirements AND whether documentation matches implemented reality. Flag discrepancies as context conflicts." Must NOT change any role's core mission or tool policy.
  Parallelization: Wave 2 | Blocked by: 1 | Blocks: 5, 7
  References (executor has NO interview context - be exhaustive): codebot/discovery_finding.py:246-272 (DiscoveryFinding shape to match), codebot/roles/implementer.md (add conflict protocol), codebot/roles/reviewer.md (add conflict protocol), codebot/roles/architecture_reviewer.md (add conflict protocol), .codebot/constitution.md:3-4 (already states "Agents may propose constitution changes but must never silently weaken requirements")
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.context_conflict import generate_conflict_finding
finding = generate_conflict_finding('implementer', 'docs/ARCHITECTURE.md:42', 'codebot/auth.py:15', 'Architecture says use session auth, code uses JWT', ['codebot/auth.py'])
assert finding['discovery_category'] == 'context_conflict'
assert finding['severity'] == 'high'
assert 'CONTEXT CONFLICT' in finding['title']
# Verify all role prompts have conflict protocol
import os
for f in os.listdir('codebot/roles'):
    if f.endswith('.md'):
        content = open(f'codebot/roles/{f}').read()
        assert 'conflict' in content.lower() or 'CONTEXT CONFLICT' in content, f'{f} missing conflict protocol'
print('Conflict protocol OK')
"`
  QA scenarios (name the exact tool + invocation): happy: generate_conflict_finding returns valid structure. happy: all role prompts contain conflict protocol section. failure: missing conflict protocol in any role → fail. Evidence .omo/evidence/task-4-context-control-plane.md
  Commit: Y | feat(context): add conflict detection protocol and update all role prompts

- [ ] 5. Add context validation to review stage
  What to do / Must NOT do: Update reviewer role prompts (`codebot/roles/reviewer.md`, `codebot/roles/correctness_reviewer.md`, `codebot/roles/architecture_reviewer.md`) to include a mandatory "Context Agreement Validation" checklist in their verdict output. The checklist requires reviewers to verify 4 dimensions before issuing APPROVE: (1) CODE ↔ TESTS: Does the implementation behavior match what tests assert? (2) TESTS ↔ REQUIREMENTS: Do tests verify the acceptance criteria specified in the ticket/plan? (3) REQUIREMENTS ↔ ARCHITECTURE: Do the acceptance criteria align with architectural constraints in docs/ARCHITECTURE.md and relevant ADRs? (4) DOCUMENTATION ↔ REALITY: Do co-located .md design docs accurately describe the code as it exists after this change? If any dimension disagrees, the reviewer MUST issue REWORK with specific findings identifying which dimension failed and what the discrepancy is. Add a new verdict field `context_agreement: dict[str, str]` with keys `code_tests`, `tests_requirements`, `requirements_architecture`, `docs_reality` each valued PASS/FAIL/UNKNOWN. UNKNOWN is never PASS. Also update `codebot/review_types.py` if StructuredFinding or ReviewDecision types need extending to carry context_agreement data. Must NOT change review gate logic — just the reviewer's evaluation criteria.
  Parallelization: Wave 3 | Blocked by: 4 | Blocks: 7
  References (executor has NO interview context - be exhaustive): codebot/roles/reviewer.md (update verdict checklist), codebot/roles/correctness_reviewer.md (update), codebot/roles/architecture_reviewer.md (update), codebot/review_types.py (extend types if needed), codebot/review_gate.py (verify it doesn't block new field)
  Acceptance criteria (agent-executable): `python3 -c "
for role_file in ['codebot/roles/reviewer.md', 'codebot/roles/correctness_reviewer.md', 'codebot/roles/architecture_reviewer.md']:
    content = open(role_file).read()
    assert 'CODE' in content and 'TESTS' in content and 'REQUIREMENTS' in content and 'ARCHITECTURE' in content
    assert 'context_agreement' in content.lower() or 'Context Agreement' in content
    print(f'{role_file}: OK')
print('Review validation OK')
"`
  QA scenarios (name the exact tool + invocation): happy: all 3 reviewer prompts contain 4-dimension checklist. failure: any dimension missing from any reviewer → fail. Evidence .omo/evidence/task-5-context-control-plane.md
  Commit: Y | feat(review): add 4-dimension context agreement validation to reviewers

- [ ] 6. Amend constitution with Context Authority rules (§5)
  What to do / Must NOT do: Append a new section "## 5. Context Authority" to `.codebot/constitution.md` after the existing §4 (Architectural Invariants). Content: "Context is the control plane for the autonomous swarm. The following rules are non-negotiable: (1) Classification: All durable project knowledge must be classified as FACT, DECISION, REQUIREMENT, HYPOTHESIS, DISCOVERY, CONSTRAINT, CURRENT_STATE, or DESIRED_STATE. Unclassified assertions have no authority. (2) Provenance: Every context item must record its source, producing role, timestamp, authority level, and supporting evidence. Anonymous context has no authority. (3) Authority Hierarchy: Constitution > ADRs > Coding Standards > Architecture Docs > Ticket Context > Agent Inference. Lower-authority items must never silently override higher-authority items. (4) Conflict Resolution: When an agent discovers a contradiction between context sources, it must generate a CONTEXT_CONFLICT finding rather than silently resolving it. Conflicts flow through the lifecycle for authoritative resolution. (5) Progressive Precision: Human intent must be progressively refined through lifecycle stages. Each stage adds precision: requirements → constraints → decomposition → implementation strategy → code. Agents at later stages must not reinterpret earlier stages' decisions. (6) Completion Agreement: A ticket reaches COMPLETE only when CODE agrees with TESTS, TESTS agree with REQUIREMENTS, REQUIREMENTS agree with ARCHITECTURE, and DOCUMENTATION agrees with REALITY. (7) No Silent Redefinition: Agents may discover new context but may not silently redefine existing authoritative context. Documentation changes that contradict established decisions require lifecycle approval." Must NOT modify existing §1-§4. Must NOT weaken any existing constitutional rule.
  Parallelization: Wave 3 | Blocked by: none | Blocks: 7
  References (executor has NO interview context - be exhaustive): .codebot/constitution.md:1-40 (existing structure), .codebot/constitution.md:3-4 (existing principle about not weakening requirements — §5 extends this)
  Acceptance criteria (agent-executable): `python3 -c "
content = open('.codebot/constitution.md').read()
assert '## 5. Context Authority' in content
assert 'Classification' in content
assert 'Provenance' in content
assert 'Authority Hierarchy' in content
assert 'Conflict Resolution' in content
assert 'Progressive Precision' in content
assert 'Completion Agreement' in content
assert 'No Silent Redefinition' in content
# Verify existing sections untouched
assert '## 1. Product Purpose' in content
assert '## 2. Security Boundaries' in content
print('Constitution §5 OK')
"`
  QA scenarios (name the exact tool + invocation): happy: all 7 rules present under §5. failure: any existing §1-4 content modified → fail. Evidence .omo/evidence/task-6-context-control-plane.md
  Commit: Y | docs(constitution): add §5 Context Authority rules

- [ ] 7. Architecture and regression tests (8 tests)
  What to do / Must NOT do: Create `tests/test_context_control_plane.py` with 8 tests: (1) `test_context_item_classification_roundtrip` — create ContextItem for each of 8 kinds, serialize/deserialize, verify kind preserved. (2) `test_context_item_authority_ordering` — verify CONSTITUTION > ADR > CODING_STANDARD > ARCHITECTURE > TICKET > AGENT_INFERENCE. (3) `test_context_item_validation_rejects_invalid` — invalid kind, empty content, empty source all raise ValueError. (4) `test_context_item_frozen_immutability` — attempt to mutate any field raises FrozenInstanceError. (5) `test_context_package_five_tiers_present` — build package with mock filesystem, verify all 5 tier headers present in output. (6) `test_context_package_token_budget_enforced` — create oversized mock docs, verify package truncated under 16000 chars. (7) `test_conflict_finding_structure` — generate conflict finding, verify category="context_conflict", severity="high", required fields present. (8) `test_review_prompts_contain_context_agreement` — read all 3 reviewer role prompts, verify 4-dimension checklist present. All use tmp_path. Deterministic. No network. No running orchestrator.
  Parallelization: Wave 3 | Blocked by: 3, 4, 5, 6 | Blocks: none
  References (executor has NO interview context - be exhaustive): tests/test_arch_authoritative_dispatch.py (pattern reference), codebot/context_item.py (todo 1), codebot/context_package.py (todo 2), codebot/context_conflict.py (todo 4), codebot/roles/reviewer.md (todo 5)
  Acceptance criteria (agent-executable): `python3 -m pytest tests/test_context_control_plane.py -v --tb=short` passes all 8
  QA scenarios (name the exact tool + invocation): happy: all 8 pass. failure: revert any todo → corresponding test fails. Evidence .omo/evidence/task-7-context-control-plane.md
  Commit: Y | test(context): add 8 regression tests for context control plane

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Schema compliance — verify ContextItem, ContextKind, AuthorityLevel all properly defined, frozen, validated
- [ ] F2. Scoping verification — dispatch an implementer and a reviewer, verify they receive different TIER 2 content but identical TIER 1 content
- [ ] F3. Budget enforcement — verify no context package exceeds 16000 chars even with large docs
- [ ] F4. Conflict protocol E2E — simulate agent discovering contradiction, verify finding generated with correct structure, verify no silent doc modification

## Commit strategy
One commit per todo (7 commits). Ordered: 1 → 2,4 → 3,5,6 → 7. Atomic: each passes independently. No squash unless requested.

## Success criteria
- ContextItem schema supports all 8 kinds with provenance tracking
- ContextPackage builder produces 5-tier scoped output under 4000 tokens
- Prompt injection delivers context packages to all dispatched agents
- Conflict detection protocol present in all role prompts
- Review stage validates 4-dimension context agreement
- Constitution §5 documents all 7 context authority rules
- All 8 regression tests pass
- Zero modifications to ticket state machine, dispatch mechanics, or discovery daemon
- Existing pipeline (REQUEST→...→COMPLETE) continues working unchanged
