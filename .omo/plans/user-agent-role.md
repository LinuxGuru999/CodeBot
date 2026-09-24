# user-agent-role - Work Plan (v5)

## TL;DR (For humans)

**What you'll get:** A new agent role that serves as the authoritative human interface for CodeBot. Humans submit requests via API or CLI into a dedicated request inbox and receive an immediate acknowledgment that includes any similar existing tickets found via synchronous dedup search. The platform ingests each request into a REQUESTED ticket. The scheduler dispatches it to the user_agent, which evaluates the request first — pushing back if it's flawed, asking clarifying questions if it's ambiguous, updating desired-state documentation if it's valid — and only then decomposes it into properly structured findings that enter the same pipeline as every other discovery source. Each request has an explicit lifecycle state machine so nothing gets lost, duplicated, or dispatched in a loop. The lifecycle manufactures context; dispatch reads it; agents act on it.

**Why this approach:** Raw human requests are not validated findings. By giving them their own inbox and lifecycle, the user_agent can evaluate before emitting work. Context produced by each lifecycle stage becomes the authoritative input for the next. The reply endpoint updates durable context only — platform reconciliation observes the context change and drives the state transition, ensuring no HTTP endpoint directly manipulates swarm state. Fuzzy dedup enriches context rather than silently killing work. Exact dedup (finding_id/fingerprint match) suppresses deterministically. Every lifecycle stage must leave the project with equal or better authoritative context than it received.

**What it will NOT do:** It will not modify the concurrency controller, spawn queue, or legacy dispatchers. It will not implement goal_priority_tier. It will not change how discovery auditors produce findings. It will not let the user_agent modify current-state docs (API.md, schemas, operational docs) — only desired-state docs (ROADMAP, ADR proposals, requirements). It will not auto-cancel stale DEFERRED requests. It will not build a conversational chat UI beyond a request/response mailbox. It will not enforce lifecycle-wide context gates beyond the REQUESTED ingress boundary (that is a separate roadmap item).

**Effort:** Large
**Risk:** Medium - adds one TicketState value, one TicketClass value, two Ticket fields, and touches scheduler dispatch routing
**Decisions to sanity-check:** Context-only reply endpoint + reconciliation-driven transition, exact-vs-fuzzy dedup policy, DESIRED-STATE vs CURRENT-STATE doc ownership, tuple[frozen_dataclass] fields on frozen dataclass, crash-recovery reconciliation in request_ingestion, EvidenceItem as canonical related-ticket representation, §22 scoped to REQUESTED ingress only, atomic merge of state+routing

Your next move: approve and run `$start-work`, or request high-accuracy review.

---

> TL;DR (machine): Large/Medium risk. 10 todos across 4 waves. Adds UserRequest/DedupCandidate frozen dataclasses with sanctioned mutations, request_ingestion with crash-recovery and reply reconciliation (detects PENDING+response+DEFERRED→REQUESTED), API+CLI entry points with context-only reply and persisted dedup_candidates, REQUESTED TicketState + REQUEST TicketClass + origin_id/origin_type (atomic commit with bucket rewire, no CANCELLED transition), rewrites role prompt for evaluate-first flow with DESIRED-STATE/CURRENT-STATE doc ownership, defines request lifecycle (valid→COMPLETE, rejected→REJECTED, ambiguous→DEFERRED), hardens finding ingestion with exact dedup suppression + fuzzy EvidenceItem enrichment + quarantine, adds CODING_STANDARDS §20-22 (§22 scoped to REQUESTED ingress), updates Scheduler.tick ordering, 19 regression tests including triage-path machine DISCOVERED verification.

## Normative Preamble

This plan implements the foundational doctrine: Context is the source. Code is the artifact. (ADR-008)

This plan implements three principles from CODING_STANDARDS.md §20-22:

**§20 ONE INGRESS CONTRACT** — Raw external intent enters TicketStore only through REQUESTED. APIs/CLIs write request envelopes; platform ingestion creates tickets.

**§21 IDEMPOTENT INGESTION** — Same identifier never creates duplicate work. Exact matches suppress; fuzzy matches enrich context.

**§22 CONTEXT IS THE CONTROL PLANE** — The repository is the swarm's long-term memory. The lifecycle is the mechanism that writes and validates that memory. Dispatch is the mechanism that reads it and assigns intelligence to act on it. Prompts tell an agent how to behave. Context tells the agent what is true. The swarm is controlled primarily by authoritative context, not increasingly complicated prompts.

**Scope qualification:** The normative statement "A ticket MUST NOT advance if the durable context required by the destination stage does not exist" applies within this plan to the REQUESTED ingress boundary only. Lifecycle-wide context gate enforcement for GOAL/TRIAGE/DECOMP/PLANNING/IMPLEMENT/REVIEW/COMPLETE transitions is a separate roadmap item. This plan establishes the pattern; subsequent plans extend it.

**Core principle:** Every lifecycle stage must leave the project with equal or better authoritative context than it received. REVIEW and COMPLETE are not merely checking code — they are preventing context degradation.

The lifecycle is a context compiler:

```
REQUEST → EVALUATE → DOCUMENT → DECOMPOSE → PLAN → IMPLEMENT → REVIEW → COMPLETE
   │          │          │          │         │        │          │         │
   ▼          ▼          ▼          ▼         ▼        ▼          ▼         ▼
 intent    clarify    desired    atomic     strategy   code     verify    durable
 context   context    state      bounds     context    changes  context   knowledge
            docs
```

Context ownership per lifecycle stage:

| Stage | Produces | Authority Type |
|-------|----------|---------------|
| User Agent | Intent, constraints, architectural implications, desired-state docs | DESIRED-STATE |
| Discovery/Triage | Problem definition, evidence, severity, scope, duplication analysis | CURRENT-STATE observation |
| Decomposition | Atomic boundaries, dependencies, affected components | Structural |
| Planning | Strategy, invariants, target files/symbols, acceptance criteria, risks | Implementation constraint |
| Implementation | Code changes, implementation evidence, discovered constraints | CURRENT-STATE mutation |
| Review | Correctness verdicts, regressions, doc drift, context consistency check | Validation |
| Completion | Final authoritative state, promotion of desired→current docs | CURRENT-STATE authority |

A lifecycle transition is a context boundary. At the REQUESTED ingress boundary, a ticket MUST NOT advance if the durable context required by the destination stage does not exist.

## Architecture

```
                    EXTERNAL WORLD
                          │
                          ▼
                 POST /api/request
                 codebotctl request
                          │
                          ▼
              ┌───────────────────────┐
              │ SYNCHRONOUS ENRICHMENT│ ← immediate (~ms)
              │ store.find_similar()  │   returns {status: accepted,
              │ Jaccard ≥ 0.8         │            request_id,
              │                       │            dedup_candidates}
              └───────────┬───────────┘
                          │
                          ▼
                UserRequest JSON (frozen)
                status = PENDING
                dedup_candidates = (DedupCandidate(...),)
                state/requests/<request_id>.json
                          │
                          ▼
                 REQUEST INGESTION
                 (request_ingestion.py)
                 ─────────────────
                 parse → validate → dedup (by request_id)
                 malformed → requests/rejected/
                 crash recovery: PENDING + ticket exists → RUNNING
                          │
                          ▼
                 REQUESTED ticket
                 origin_type="user_request"
                 origin_id=<request_id>
                 ticket_class=REQUEST
                          │
                          ▼
                     USER bucket
                     BUCKET_ORDER[0]
                          │
                          ▼
                     user_agent
                    /     │      \
                   /      │       \
                  ▼       ▼        ▼
             ambiguous  invalid    valid
                 │         │         │
                 ▼         ▼         ▼
            req.with_   req.reject  update DESIRED-STATE
            clarif_     (reason)    docs only (ROADMAP,
            questions(              ADR proposals,
              [...])                requirements)
                 │         │         │
                 ▼         ▼         ▼
             set context   │      emit DiscoveryFindings
             on request    │      with related_ticket_candidates
                 │         │      as EvidenceItem(
                 │         │        kind="related_ticket_candidate",
                 │         │        observation=ticket_id,
                 │         │        interpretation=similarity
                 │         │      )
                 │         │      to state/findings/
                 │         │         │
                 ▼         ▼         ▼
           (no direct   REJECTED  FINDING INGESTION
            transition)  ticket   (finding_ingestion.py)
                 │         │      ─────────────────
                 │         │      exact finding_id match → suppress
                 │         │      exact fingerprint match → suppress
                 │         │      fuzzy Jaccard ≥ 0.8 → add
                 │         │        EvidenceItem candidates to
                 │         │        Finding.evidence list,
                 │         │        still create ticket
                 │         │         │
                 ▼         ▼         ▼
          RECONCILIATION  terminal  DISCOVERED tickets
          observes:               with related_ticket_candidates
          request.status=PENDING  in evidence + lifecycle_context
          request.response!=""
          ticket.state=DEFERRED
              │
              ▼
          store.transition(
            DEFERRED→REQUESTED)
          (platform-driven,
           not API-driven)

Original REQUESTED ticket lifecycle:
    valid (findings emitted) → COMPLETE
    rejected                 → REJECTED
    ambiguous                → DEFERRED
    human replies            → context updated → reconciliation → REQUESTED
    stale DEFERRED           → stays DEFERRED (no auto-cancel)

Machine-discovered DISCOVERED tickets:
    bug_hunter/security_auditor findings → DISCOVERED
    → platform triage (NOT in any bucket)
    → TRIAGED → GOAL bucket
    → goal_aligner role (not user_agent)
    → completely independent lifecycle
```

## Scope
### Must have
- `UserRequest` frozen dataclass with `UserRequestStatus` enum (PENDING, RUNNING, AWAITING_INPUT, REJECTED, COMPLETE) and explicit transition table
- `DedupCandidate` frozen dataclass with fields: ticket_id, title, state, similarity
- `UserRequest` fields using `tuple[frozen_dataclass, ...]` for immutable collections: request_id, source, message, context, created_at, updated_at, status, conversation_id, clarification_questions (tuple[str,...]), response, rejection_reason, findings_emitted (tuple[str,...]), dedup_candidates (tuple[DedupCandidate,...])
- Sanctioned mutation methods: `update_status()`, `with_response()`, `with_clarification_questions()`, `reject()`, `complete()`
- `codebot/request_ingestion.py` module owning parse, validate, dedup, quarantine, REQUESTED ticket creation, crash-recovery reconciliation, and reply reconciliation
- `state/requests/` and `state/requests/rejected/` directories
- `POST /api/request` with synchronous `find_similar()` dedup enrichment returning dedup_candidates immediately AND persisting them on UserRequest
- `GET /api/request/<id>` endpoint on control_server
- `POST /api/request/<id>/reply` endpoint that updates UserRequest context ONLY (sets PENDING + response, no direct store.transition call)
- Platform reconciliation in request_ingestion that detects `status==PENDING AND response!="" AND ticket.state==DEFERRED` and drives DEFERRED→REQUESTED transitions
- `codebotctl request`, `codebotctl request-status` (JSON + --human), `codebotctl request-reply` CLI commands
- `TicketState.REQUESTED` enum value with transitions: → COMPLETE, → REJECTED, → DEFERRED (NO → CANCELLED to prevent UserRequest/Ticket disagreement)
- `TicketClass.REQUEST` enum value for ingress tickets
- `Ticket.origin_id: str` and `Ticket.origin_type: str` fields (default "")
- `create_ticket()` extended with origin_id/origin_type parameters
- `DEFERRED → REQUESTED` added to TRANSITIONS dict for reconciliation-driven reply flow
- USER bucket in BUCKET_ORDER querying `["REQUESTED"]` state exclusively (deployed atomically with REQUESTED state in single commit)
- `_role_for_ticket` returning `"user_agent"` for REQUESTED state
- Rewritten `user_agent.md` prompt enforcing evaluate-first flow with DESIRED-STATE/CURRENT-STATE doc ownership boundaries, using sanctioned mutation methods, and representing related_ticket_candidates as EvidenceItem(kind="related_ticket_candidate")
- Request lifecycle transitions in `transition_ticket_on_success`: valid→COMPLETE, rejected→REJECTED, ambiguous→DEFERRED
- `codebot/finding_ingestion.py` with exact idempotent ingestion (finding_id/fingerprint match → suppress) + fuzzy candidate context enrichment (Jaccard ≥ 0.8 → add EvidenceItem(kind="related_ticket_candidate") to Finding.evidence, propagate to lifecycle_context["discovery"]["related_candidates"], still create ticket) + rejected/ quarantine
- `docs/CODING_STANDARDS.md` §20 (ONE INGRESS CONTRACT), §21 (IDEMPOTENT INGESTION), §22 (CONTEXT IS THE CONTROL PLANE, scoped to REQUESTED ingress)
- `Scheduler.tick()` ordering: exits → transitions → reconcile → ingest_requests → ingest_findings → dispatch → drain
- 19 regression tests covering full lifecycle including triage-path machine-DISCOVERED verification

### Must NOT have (guardrails, anti-slop, scope boundaries)
- No modifications to ConcurrencyController, DispatchGate, or SpawnQueue internals
- No changes to legacy dispatch paths (spawn_demand_agents, dispatch_triage_agents, etc.)
- No changes to discovery_daemon.py finding production
- No goal_priority_tier implementation
- No conversational UI beyond request/response mailbox
- No auto-cancellation of stale DEFERRED requests (DEFERRED stays until explicit human action)
- No product code edits beyond the files listed in each todo
- No direct TicketStore mutation from API/CLI reply endpoint — reply updates context only, reconciliation drives transitions
- No user_agent modification of CURRENT-STATE docs (API.md, current schemas, operational docs) — only DESIRED-STATE docs (ROADMAP, ADR proposals, requirements, architectural intent)
- No fuzzy dedup suppression — fuzzy matches enrich context via EvidenceItem, only exact finding_id/fingerprint matches suppress
- No list fields on frozen UserRequest/DedupCandidate dataclasses — use tuple[frozen_dataclass, ...] for true immutability
- No lifecycle-wide context gate enforcement beyond REQUESTED ingress (separate roadmap item)
- No REQUESTED → CANCELLED transition (UserRequestStatus has no CANCELLED; prevents ticket/request disagreement)

## Verification strategy
> Zero human intervention - all verification is agent-executed.
- Test decision: tests-after + pytest
- Evidence: .omo/evidence/task-<N>-user-agent-role.md

## Execution strategy
### Parallel execution waves
Wave 1: Foundation (todos 1, 5) — request envelope + state/routing (atomic), parallel
Wave 2: Ingestion + Entry (todos 2, 3, 4) — request_ingestion + API + CLI, sequential chain
Wave 3: Agent wiring (todos 6, 7, 8) — prompt + lifecycle + finding ingestion, 6 blocks 7, 8 parallel
Wave 4: Standards + Tests (todos 9, 10) — 9 parallel with wave 3 tail, 10 blocked by all

### Dependency matrix
| Todo | Depends on | Blocks | Can parallelize with |
| --- | --- | --- | --- |
| 1 | none | 2 | 5 |
| 2 | 1 | 3 | 5 |
| 3 | 2 | 4 | 5 |
| 4 | 3 | none | 5 |
| 5 | none | 6, 7 | 1 |
| 6 | 5 | 7, 10 | 8 |
| 7 | 5, 6 | 10 | 8 |
| 8 | none | 10 | 6, 7 |
| 9 | 2, 8 | 10 | 6, 7 |
| 10 | 6, 7, 8, 9 | none | none |

## Todos
> Implementation + Test = ONE todo. Never separate.

- [ ] 1. Create UserRequest, DedupCandidate, UserRequestStatus with sanctioned mutations
  What to do / Must NOT do: Create `codebot/user_request.py` containing: (A) `UserRequestStatus(str, Enum)` with values PENDING, RUNNING, AWAITING_INPUT, REJECTED, COMPLETE. No CANCELLED. (B) Transition table as a dict mapping each status to its allowed targets: PENDING→{RUNNING}, RUNNING→{AWAITING_INPUT, REJECTED, COMPLETE}, AWAITING_INPUT→{PENDING}, REJECTED→{}, COMPLETE→{}. (C) Frozen `DedupCandidate` dataclass with fields: `ticket_id: str`, `title: str`, `state: str`, `similarity: float`. Methods: `to_dict() -> dict`, `from_dict(data) -> DedupCandidate`. (D) Frozen `UserRequest` dataclass with ALL collection fields as `tuple[...]` (NOT `list[...]`) to prevent mutation via `.append()` on a frozen dataclass. Fields: `request_id: str`, `source: str` (always "user"), `message: str`, `context: str` (default ""), `created_at: float`, `updated_at: float`, `status: UserRequestStatus`, `conversation_id: str` (defaults to request_id), `clarification_questions: tuple[str, ...]` (default empty tuple), `response: str` (default ""), `rejection_reason: str` (default ""), `findings_emitted: tuple[str, ...]` (default empty tuple), `dedup_candidates: tuple[DedupCandidate, ...]` (default empty tuple — populated by API's find_similar enrichment, contains frozen DedupCandidate instances). (E) Sanctioned mutation methods, each returning a new frozen instance with updated_at=time.time(): `update_status(new_status: UserRequestStatus) -> UserRequest` validates against transition table (raises ValueError for illegal transitions). `with_response(response: str) -> UserRequest` returns new instance with response set and clarification_questions cleared to empty tuple. `with_clarification_questions(questions: tuple[str, ...]) -> UserRequest` returns new instance with questions set. `reject(reason: str) -> UserRequest` returns new instance with status=REJECTED and rejection_reason=reason. `complete(findings: tuple[str, ...]) -> UserRequest` returns new instance with status=COMPLETE and findings_emitted=findings. (F) Serialization: `to_dict() -> dict`, `from_dict(data) -> UserRequest` (reconstructs DedupCandidate tuples), `to_json(pretty=False) -> str`. (G) `ensure_directories(state_dir: Path) -> None` creating `requests/` and `requests/rejected/`. Must NOT import TicketStore. Must NOT mutate tickets.json. stdlib only.
  Parallelization: Wave 1 | Blocked by: none | Blocks: 2
  References (executor has NO interview context - be exhaustive): codebot/discovery_finding.py:141-156 (EvidenceItem frozen dataclass pattern), codebot/discovery_finding.py:246-272 (DiscoveryFinding pattern), codebot/ticket_engine.py:87-105 (enum pattern), codebot/ticket_engine.py:360-370 (transition validation pattern)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.user_request import UserRequest, UserRequestStatus, DedupCandidate
import time
dc = DedupCandidate(ticket_id='CB-1', title='Test', state='IMPLEMENT', similarity=0.85)
assert dc.to_dict()['similarity'] == 0.85
req = UserRequest(request_id='test123', source='user', message='Add rate limiting', context='Auth', created_at=time.time(), updated_at=time.time(), status=UserRequestStatus.PENDING, conversation_id='test123', dedup_candidates=(dc,))
d = req.to_dict()
assert d['status'] == 'pending'
assert isinstance(req.clarification_questions, tuple)
assert isinstance(req.findings_emitted, tuple)
assert isinstance(req.dedup_candidates, tuple)
assert isinstance(req.dedup_candidates[0], DedupCandidate)
req2 = UserRequest.from_dict(d)
assert req2.message == 'Add rate limiting'
assert len(req2.dedup_candidates) == 1
# Valid transition
req3 = req2.update_status(UserRequestStatus.RUNNING)
assert req3.status == UserRequestStatus.RUNNING
# Invalid transition
try:
    req2.update_status(UserRequestStatus.COMPLETE)
    assert False, 'Should have raised ValueError'
except ValueError:
    pass
# Tuple immutability
try:
    req.clarification_questions.append('x')
    assert False, 'tuple should not support append'
except AttributeError:
    pass
try:
    req.dedup_candidates[0].similarity = 0.0
    assert False, 'frozen dataclass element should not be mutable'
except Exception:
    pass
# Sanctioned mutations
req4 = req3.with_clarification_questions(('Which auth?', 'Session or token?'))
assert len(req4.clarification_questions) == 2
req5 = req4.with_response('Use JWT tokens')
assert req5.response == 'Use JWT tokens'
assert req5.clarification_questions == ()
req6 = req3.reject('Duplicate of CB-99')
assert req6.status == UserRequestStatus.REJECTED
assert req6.rejection_reason == 'Duplicate of CB-99'
req7 = req3.complete(('DF-1', 'DF-2'))
assert req7.status == UserRequestStatus.COMPLETE
assert req7.findings_emitted == ('DF-1', 'DF-2')
print('UserRequest OK')
"`
  QA scenarios (name the exact tool + invocation): happy: create, serialize, deserialize, valid transition, verify tuple+frozen immutability, verify all sanctioned mutations. failure: illegal transition raises ValueError. failure: attempt list.append on tuple field raises AttributeError. failure: attempt mutation of DedupCandidate element raises exception. Evidence .omo/evidence/task-1-user-agent-role.md
  Commit: Y | feat(user-agent): add UserRequest, DedupCandidate, UserRequestStatus with sanctioned mutations

- [ ] 2. Create request_ingestion.py module with crash-recovery and reply reconciliation
  What to do / Must NOT do: Create `codebot/request_ingestion.py` with function `ingest_requests(store, state_dir: Path) -> int`. Logic: (1) Call `UserRequest.ensure_directories(state_dir)`. (2) Glob `state_dir / "requests" / "*.json"`. (3) For each file: try parse as UserRequest via `from_dict()`. If parse fails → move file to `state_dir / "requests" / "rejected" / f"{filename}.malformed"`, log warning, continue. Quarantine is owned by this module, not by UserRequest. (4) Check idempotency: iterate store tickets looking for `origin_type == "user_request"` and `origin_id == req.request_id`. (5) **Crash-recovery reconciliation**: If match found AND req.status == PENDING → this means store.add() succeeded but the status update/move didn't (crash between steps). Reconcile: update UserRequest status to RUNNING via `update_status()`, write updated JSON, move file to `processed/`, log info "reconciled crashed ingestion", skip creation. If match found AND req.status == RUNNING → already processed, move to processed/ if not there, skip. (6) If no match and status == PENDING → call `store.add(create_ticket(...))` mapping: title=req.message[:80], problem_statement=req.message, source="user_agent", ticket_class=TicketClass.REQUEST (the dedicated ingress class added in Todo 5, NOT FEATURE), severity=Severity.MEDIUM, evidence=f"user-request:{req.request_id}", acceptance_criteria=["user_agent evaluates this request"], affected_modules=[], desired_state="Evaluated and decomposed by user_agent", origin_id=req.request_id, origin_type="user_request". (7) Update UserRequest status to RUNNING via `update_status()`, write updated JSON back to inbox. (8) Move original file to `state_dir / "requests" / "processed" /`. (9) **Reply reconciliation**: Also glob `state_dir / "requests" / "processed" / "*.json"`. For each, parse as UserRequest. If `req.status == PENDING and req.response != ""` → find corresponding ticket via `origin_type == "user_request"` and `origin_id == req.request_id`. If ticket found AND `ticket.state == TicketState.DEFERRED` → call `store.transition(ticket.id, TicketState.REQUESTED)`. Log info "reconciled reply: DEFERRED→REQUESTED". Write updated JSON. This is the ONLY place that drives the DEFERRED→REQUESTED transition — never the API endpoint directly. The condition is naturally idempotent: once the ticket transitions away from DEFERRED, subsequent scans skip it. No `updated_at > last_check` comparison needed. (10) Return count of newly created tickets. Must NOT call TicketStore() constructor directly — use injected store. Must handle empty directory gracefully (return 0).
  Parallelization: Wave 2 | Blocked by: 1 | Blocks: 3
  References (executor has NO interview context - be exhaustive): codebot/user_request.py (todo 1 output), codebot/ticket_engine.py:440-503 (create_ticket signature), codebot/ticket_engine.py:297-354 (Ticket dataclass showing origin_id/origin_type fields from todo 5), codebot/ticket_engine.py:360-370 (store.transition for DEFERRED→REQUESTED)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.request_ingestion import ingest_requests
from codebot.ticket_dispatcher import get_ticket_store
from codebot.user_request import UserRequest, UserRequestStatus
import tempfile, json, time
from pathlib import Path
with tempfile.TemporaryDirectory() as td:
    sd = Path(td)
    UserRequest.ensure_directories(sd)
    req = UserRequest(request_id='test-ingest', source='user', message='Test feature', context='', created_at=time.time(), updated_at=time.time(), status=UserRequestStatus.PENDING, conversation_id='test-ingest')
    (sd / 'requests' / 'test-ingest.json').write_text(req.to_json())
    store = get_ticket_store(sd / 'tickets.json')
    n1 = ingest_requests(store, sd)
    assert n1 == 1, f'Expected 1, got {n1}'
    # Crash recovery: write PENDING request for already-existing ticket
    req_crash = UserRequest(request_id='test-ingest', source='user', message='Test feature', context='', created_at=time.time(), updated_at=time.time(), status=UserRequestStatus.PENDING, conversation_id='test-ingest')
    (sd / 'requests' / 'test-ingest.json').write_text(req_crash.to_json())
    n2 = ingest_requests(store, sd)
    assert n2 == 0, f'Expected 0 (reconciled), got {n2}'
    reconciled = json.loads((sd / 'requests' / 'processed' / 'test-ingest.json').read_text())
    assert reconciled['status'] == 'running', f'Expected running, got {reconciled[\"status\"]}'
    # Malformed
    (sd / 'requests' / 'bad.json').write_text('not json')
    n3 = ingest_requests(store, sd)
    assert n3 == 0
    assert any((sd / 'requests' / 'rejected').glob('bad*'))
print('Request ingestion OK')
"`
  QA scenarios (name the exact tool + invocation): happy: valid request creates REQUESTED ticket with TicketClass.REQUEST, file moved to processed/. happy: crash recovery reconciles PENDING+existing-ticket to RUNNING. happy: reply reconciliation drives DEFERRED→REQUESTED when status==PENDING and response!="" and ticket==DEFERRED. failure: malformed JSON quarantined to rejected/. failure: store=None returns 0 gracefully. Evidence .omo/evidence/task-2-user-agent-role.md
  Commit: Y | feat(platform): add request_ingestion with crash-recovery and reply reconciliation

- [ ] 3. Add API endpoints with context-only reply and persisted dedup_candidates
  What to do / Must NOT do: Add three HTTP handlers to `codebot/control_server.py` in the existing `do_GET` and `do_POST` methods using path matching. (A) `POST /api/request`: parse JSON body `{"message": str, "context": str (optional), "conversation_id": str (optional)}`. Validate message non-empty (400 if missing). BEFORE writing the UserRequest file, perform synchronous dedup enrichment: call `get_ticket_store(state_dir).find_similar(problem_statement=message, limit=3)` wrapped in try/except so store unavailability degrades gracefully. Format matches as tuple of DedupCandidate instances: `tuple(DedupCandidate(ticket_id=t.id, title=t.title, state=t.state.value, similarity=float(sim)) for t, sim in results)`. Construct UserRequest with status=PENDING and `dedup_candidates=candidates_tuple`. Write atomically to `state_dir / "requests" / f"{request_id}.json"`. Return enriched response: `{"status": "accepted", "request_id": id, "dedup_candidates": [c.to_dict() for c in candidates_tuple]}`. (B) `GET /api/request/<request_id>`: read the request JSON from `state/requests/` (or processed/rejected subdirs). Return the full UserRequest dict as JSON. 404 if not found. (C) `POST /api/request/<request_id>/reply`: parse JSON body `{"response": str}`. Read existing UserRequest from processed/ directory. Verify status==AWAITING_INPUT (400 if not). **CRITICAL: Do NOT call store.transition() here.** Only update the UserRequest context: call `req.with_response(response_text)` to get new frozen instance with response set and clarification_questions cleared, then `new_req.update_status(UserRequestStatus.PENDING)`. Write updated JSON back to `processed/` directory atomically. Return `{"status": "replied", "request_id": id}`. The actual DEFERRED→REQUESTED transition happens in request_ingestion's reply reconciliation (Todo 2), which runs during the next Scheduler.tick(). This ensures no HTTP endpoint directly manipulates swarm state. All endpoints use existing CONTROL_TOKEN bearer auth. Import UserRequest, DedupCandidate, and get_ticket_store lazily inside handlers. Must NOT construct DiscoveryFinding. Must NOT call store.add() or store.transition() directly in any handler.
  Parallelization: Wave 2 | Blocked by: 2 | Blocks: 4
  References (executor has NO interview context - be exhaustive): codebot/control_server.py:1718-1743 (do_GET path matching pattern), codebot/control_server.py:1992-2021 (do_POST auth + handler pattern), codebot/ticket_engine.py:2085-2121 (find_similar method signature and return type list[tuple[Ticket, float]]), codebot/user_request.py (todo 1, with_response method, DedupCandidate), codebot/request_ingestion.py (todo 2, reply reconciliation logic)
  Acceptance criteria (agent-executable): `python3 -c "
import ast
src = open('codebot/control_server.py').read()
assert '/api/request' in src
assert 'reply' in src.lower()
assert 'find_similar' in src
assert 'dedup_candidates' in src
assert 'DedupCandidate' in src
# CRITICAL: reply handler must NOT contain store.transition
print('API endpoints present')
"`
  QA scenarios (name the exact tool + invocation): happy: POST valid request when similar ticket exists, assert response contains dedup_candidates with ticket_id/title/state/similarity. happy: POST valid request persists dedup_candidates as frozen DedupCandidate tuple on UserRequest file. happy: GET status returns correct UserRequest with dedup_candidates. happy: POST reply updates UserRequest context (response set, questions cleared, status PENDING) but does NOT call store.transition. failure: POST empty message → 400. failure: reply to non-AWAITING_INPUT request → 400. failure: no auth token when CONTROL_TOKEN set → 401. failure: store unavailable during find_similar → request still accepted with empty dedup_candidates (graceful degradation). Evidence .omo/evidence/task-3-user-agent-role.md
  Commit: Y | feat(api): add request, status, and context-only reply endpoints with persisted dedup enrichment

- [ ] 4. Add CLI commands for request, request-status, request-reply
  What to do / Must NOT do: Add three subcommands to `codebotctl` script at project root following the existing sys.argv dispatch pattern. (A) `./codebotctl request "<message>" [--context "..."] [--conversation-id "..."]`: POST to `/api/request`, print response JSON (request_id + status + dedup_candidates), exit 0/1. (B) `./codebotctl request-status <request_id>`: GET `/api/request/<id>`, output formatted JSON to stdout (machine-parseable). Include fields: status, created_at, updated_at, clarification_questions, rejection_reason, findings_emitted, dedup_candidates. Support optional `--human` flag that renders a human-readable table instead of raw JSON. (C) `./codebotctl request-reply <request_id> "<response>"`: POST to `/api/request/<id>/reply`, print result. All use stdlib urllib.request. Include CONTROL_TOKEN auth header if env var set. Must NOT duplicate UserRequest construction. Output references request_id, not finding_id.
  Parallelization: Wave 2 | Blocked by: 3 | Blocks: none
  References (executor has NO interview context - be exhaustive): codebotctl (read entire file for subcommand dispatch pattern)
  Acceptance criteria (agent-executable): `./codebotctl request --help 2>&1 | grep -q 'message' && ./codebotctl request-status --help 2>&1 | grep -q 'request_id' && ./codebotctl request-reply --help 2>&1 | grep -q 'response' && echo 'CLI OK'`
  QA scenarios (name the exact tool + invocation): happy: request + status + reply round-trip against running server. failure: request when server down → non-zero exit + error to stderr. Evidence .omo/evidence/task-4-user-agent-role.md
  Commit: Y | feat(cli): add request, request-status, request-reply commands

- [ ] 5. Add TicketState.REQUESTED, TicketClass.REQUEST, origin fields, rewire USER bucket, and _role_for_ticket (ATOMIC)
  What to do / Must NOT do: This todo combines what were previously separate state and routing changes into a SINGLE ATOMIC COMMIT to prevent broken intermediate states. Five changes in `codebot/ticket_engine.py` and two in `codebot/scheduler_v2/dispatcher.py`. (A) Add `REQUESTED = "REQUESTED"` to TicketState enum (after DISCOVERED, around line 88). (B) Add to TRANSITIONS dict: `TicketState.REQUESTED: frozenset({TicketState.COMPLETE, TicketState.REJECTED, TicketState.DEFERRED})`. NOTE: NO CANCELLED transition — UserRequestStatus has no CANCELLED state, and allowing ticket CANCELLED without request CANCELLED creates disagreement. Add `TicketState.REQUESTED` to DEFERRED's allowed transitions: `TicketState.DEFERRED: frozenset({...existing..., TicketState.REQUESTED})`. (C) Add `REQUEST = "request"` to TicketClass enum (after INFRASTRUCTURE at line 133). (D) Add `origin_id: str = ""` and `origin_type: str = ""` fields to the Ticket dataclass (after finding_id at line 340). (E) Update `create_ticket()` function signature (line 434-460) to accept `origin_id: str = ""` and `origin_type: str = ""` parameters after the existing `fingerprint` parameter, and pass them through to the Ticket constructor in the return statement (around line 472-503). (F) In `codebot/scheduler_v2/dispatcher.py`: Change BUCKET_ORDER at line 79 from `("USER", ["DISCOVERED"])` to `("USER", ["REQUESTED"])`. Machine-discovered DISCOVERED tickets continue flowing through platform triage (DISCOVERED is intentionally absent from BUCKET_ORDER per line 70-71 comment). (G) In `codebot/scheduler_v2/dispatcher.py`: Add branch at top of `_role_for_ticket()` (before line 552) that returns `"user_agent"` when `state_val == "REQUESTED"`. Must NOT change any other state transitions. Must NOT modify DISCOVERED handling. **This entire todo is ONE commit.**
  Parallelization: Wave 1 | Blocked by: none | Blocks: 6, 7
  References (executor has NO interview context - be exhaustive): codebot/ticket_engine.py:87-105 (TicketState enum), codebot/ticket_engine.py:123-133 (TicketClass enum), codebot/ticket_engine.py:197-288 (TRANSITIONS dict), codebot/ticket_engine.py:267-274 (DEFERRED transitions to extend), codebot/ticket_engine.py:297-354 (Ticket dataclass), codebot/ticket_engine.py:434-460 (create_ticket signature), codebot/ticket_engine.py:472-503 (constructor), codebot/scheduler_v2/dispatcher.py:78-96 (BUCKET_ORDER and BUCKET_TO_ROLE_SETS), codebot/scheduler_v2/dispatcher.py:545-572 (_role_for_ticket), codebot/scheduler_v2/dispatcher.py:70-71 (DISCOVERED intentionally absent comment)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.ticket_engine import TicketState, TicketClass, TRANSITIONS, Ticket, create_ticket
from codebot.scheduler_v2.dispatcher import BUCKET_ORDER, BucketDispatcher
from types import SimpleNamespace
assert hasattr(TicketState, 'REQUESTED')
assert TicketState.REQUESTED.value == 'REQUESTED'
assert hasattr(TicketClass, 'REQUEST')
assert TicketClass.REQUEST.value == 'request'
assert TicketState.COMPLETE in TRANSITIONS[TicketState.REQUESTED]
assert TicketState.REJECTED in TRANSITIONS[TicketState.REQUESTED]
assert TicketState.DEFERRED in TRANSITIONS[TicketState.REQUESTED]
assert TicketState.CANCELLED not in TRANSITIONS[TicketState.REQUESTED]
assert TicketState.REQUESTED in TRANSITIONS[TicketState.DEFERRED]
t = Ticket.__dataclass_fields__
assert 'origin_id' in t
assert 'origin_type' in t
import inspect
sig = inspect.signature(create_ticket)
assert 'origin_id' in sig.parameters
assert 'origin_type' in sig.parameters
user_bucket = [b for b in BUCKET_ORDER if b[0] == 'USER'][0]
assert user_bucket[1] == ['REQUESTED'], f'Expected REQUESTED, got {user_bucket[1]}'
bd = BucketDispatcher.__new__(BucketDispatcher)
t_req = SimpleNamespace(state=TicketState.REQUESTED, source='user', ticket_class='feature')
assert bd._role_for_ticket(t_req) == 'user_agent'
t_disc = SimpleNamespace(state=TicketState.DISCOVERED, source='bug_hunter', ticket_class='bug')
assert bd._role_for_ticket(t_disc) != 'user_agent'
discovered_buckets = [b[0] for b in BUCKET_ORDER if 'DISCOVERED' in b[1]]
assert discovered_buckets == [], f'DISCOVERED should not be in any bucket, found in: {discovered_buckets}'
print('State + class + fields + routing OK')
"`
  QA scenarios (name the exact tool + invocation): happy: REQUESTED exists, REQUEST class exists, all transitions valid, no CANCELLED transition, origin fields present, create_ticket accepts origin params, USER bucket queries REQUESTED, routes to user_agent, DISCOVERED not in any bucket. failure: REQUESTED→IMPLEMENT raises ValueError. failure: REQUESTED→CANCELLED raises ValueError. failure: DEFERRED→REQUESTED succeeds (new transition). Evidence .omo/evidence/task-5-user-agent-role.md
  Commit: Y | feat(engine+scheduler): add REQUESTED state, REQUEST class, origin fields, wire USER bucket and routing (atomic)

- [ ] 6. Rewrite user_agent.md role prompt for evaluate-first flow with sanctioned mutations and EvidenceItem candidates
  What to do / Must NOT do: Rewrite `codebot/roles/user_agent.md` entirely. The prompt must enforce this linear process: (1) Read the injected UserRequest from the ticket context (use origin_id to locate request file if needed). Read dedup_candidates from the UserRequest for prior context. (2) Evaluate: is this request clear, valid, non-duplicate, architecturally sound? (3) If ambiguous → use `req.with_clarification_questions(('question1', 'question2'))` to produce updated request, write to scratchpad, set status to AWAITING_INPUT, exit 0. The lifecycle handler will transition ticket to DEFERRED. (4) If flawed/duplicate/violates constitution → argue why with specific citations (file:line), use `req.reject('reason')` to produce updated request, exit 0. Lifecycle handler transitions to REJECTED. (5) If valid → update **DESIRED-STATE documentation ONLY**: ROADMAP.md, docs/adr/ (proposals), requirements docs, architectural intent docs. **MUST NOT modify CURRENT-STATE docs**: docs/API.md, current schemas, operational docs, implemented architecture descriptions. The distinction: desired-state describes what SHOULD be; current-state describes what IS. Implementation and review own the promotion of desired→current. (6) Decompose into atomic units. For each unit, construct a `DiscoveryFinding` with ALL required fields from `codebot/discovery_finding.py:246-272`. For related ticket candidates (from dedup_candidates or new find_similar calls), represent each as an `EvidenceItem(kind="related_ticket_candidate", observation=ticket_id, interpretation=f"Jaccard similarity: {similarity}", impact="Potential duplicate or related work")` and include it in the Finding's `evidence` list. This is the single canonical representation — do NOT use custom metadata fields. Write each finding atomically to `{STATE_DIR}/findings/{finding_id}.json`. (7) Use `req.complete(tuple_of_finding_ids)` to produce final request state, exit 0. Lifecycle handler transitions REQUESTED→COMPLETE. Tool Usage section: read, grep, glob, write, edit, bash, web_search, web_fetch. MUST NOT list create_ticket. Anti-patterns: creating finding without evaluation, assuming meaning, modifying CURRENT-STATE docs, calling create_ticket, using list.append on frozen fields instead of sanctioned mutations.
  Parallelization: Wave 3 | Blocked by: 5 | Blocks: 7, 10
  References (executor has NO interview context - be exhaustive): codebot/roles/user_agent.md:1-111 (current prompt to replace), codebot/discovery_finding.py:246-272 (DiscoveryFinding required fields), codebot/discovery_finding.py:141-156 (EvidenceItem dataclass with kind field), codebot/user_request.py (sanctioned mutations: with_clarification_questions, reject, complete), codebot/roles/implementer.md:1-60 (reference format for role prompts)
  Acceptance criteria (agent-executable): `python3 -c "
prompt = open('codebot/roles/user_agent.md').read()
for field in ['finding_id','discovery_role','discovery_category','problem_statement','severity','priority','confidence','atomicity','repository','repository_revision','evidence','acceptance_outcome']:
    assert field in prompt, f'Missing: {field}'
assert 'create_ticket' not in prompt
assert 'AWAITING_INPUT' in prompt or 'awaiting_input' in prompt
assert 'REJECTED' in prompt or 'rejected' in prompt
assert 'COMPLETE' in prompt or 'complete' in prompt
assert 'DESIRED-STATE' in prompt or 'desired-state' in prompt or 'desired state' in prompt.lower()
assert 'CURRENT-STATE' in prompt or 'current-state' in prompt or 'current state' in prompt.lower()
assert 'ROADMAP' in prompt
assert 'API.md' in prompt
assert 'with_clarification_questions' in prompt
assert 'reject(' in prompt or '.reject(' in prompt
assert 'complete(' in prompt or '.complete(' in prompt
assert 'related_ticket_candidate' in prompt
assert 'EvidenceItem' in prompt
print('Prompt OK')
"`
  QA scenarios (name the exact tool + invocation): happy: all DiscoveryFinding fields mentioned, evaluation flow documented, DESIRED-STATE/CURRENT-STATE ownership stated, API.md listed as forbidden, sanctioned mutations referenced, EvidenceItem candidate representation specified. failure: create_ticket referenced → fail. failure: list.append pattern suggested → fail. Evidence .omo/evidence/task-6-user-agent-role.md
  Commit: Y | docs(roles): rewrite user_agent.md for evaluate-first flow with sanctioned mutations

- [ ] 7. Implement request lifecycle transitions in dispatch_service.py
  What to do / Must NOT do: Modify `transition_ticket_on_success()` in `codebot/dispatch_service.py`. Add `elif base_role == "user_agent":` branch after the IMPLEMENTER_ROLE_NAMES branch. When user_agent exits with code 0: (A) Read the ticket's origin_id field. (B) Load UserRequest from `state_dir / "requests" / "processed" / f"{origin_id}.json"` (it was moved there by request_ingestion). (C) Based on request status: if COMPLETE (findings emitted) → `ts.transition(assigned_tid, TicketState.COMPLETE)`. If REJECTED → `ts.transition(assigned_tid, TicketState.REJECTED)`. If AWAITING_INPUT → `ts.transition(assigned_tid, TicketState.DEFERRED)`. (D) Log the transition. (E) Handle missing request file gracefully (log warning, skip transition, do NOT crash). Must NOT transition to DECOMP (user_agent already decomposed). Must handle ValueError from invalid transitions gracefully.
  Parallelization: Wave 3 | Blocked by: 5, 6 | Blocks: 10
  References (executor has NO interview context - be exhaustive): codebot/dispatch_service.py:375-385 (implementer transition branch pattern), codebot/dispatch_service.py:241-315 (full function), codebot/ticket_engine.py:197-288 (TRANSITIONS), codebot/user_request.py (status field)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.dispatch_service import transition_ticket_on_success
import inspect
src = inspect.getsource(transition_ticket_on_success)
assert 'user_agent' in src
assert 'COMPLETE' in src
assert 'DEFERRED' in src
assert 'REJECTED' in src
user_agent_section = src.split('user_agent')[1].split('elif')[0] if 'user_agent' in src else ''
assert 'DECOMP' not in user_agent_section
print('Lifecycle OK')
"`
  QA scenarios (name the exact tool + invocation): happy: COMPLETE status → ticket transitions to COMPLETE. happy: REJECTED → REJECTED. happy: AWAITING_INPUT → DEFERRED. failure: missing request file → graceful skip with warning. Evidence .omo/evidence/task-7-user-agent-role.md
  Commit: Y | feat(dispatch): add user_agent lifecycle transitions

- [ ] 8. Create finding_ingestion.py with exact dedup suppression and fuzzy EvidenceItem enrichment
  What to do / Must NOT do: Create `codebot/finding_ingestion.py` with `ingest_findings(store, state_dir: Path) -> int`. Logic: (1) Ensure `state_dir / "findings"`, `findings/processed/`, `findings/rejected/` exist. (2) Glob `findings/*.json`. (3) Parse each as DiscoveryFinding via from_dict(). If parse fails → move to `findings/rejected/`, log warning, continue. (4) **Exact dedup (suppress)**: Check if store already has a ticket with matching `finding_id`. If yes → move to processed/, skip creation, log info "suppressed duplicate finding_id". Also check `store._fingerprint_index` for matching fingerprint. If match → move to processed/, skip, log info "suppressed duplicate fingerprint". (5) **Fuzzy dedup (enrich context, do NOT suppress)**: Call `store.find_similar(problem_statement=finding.problem_statement, limit=3)`. If results have similarity ≥ 0.8 AND are not in terminal states → build `related_ticket_candidates` list. For each candidate, create an `EvidenceItem(kind="related_ticket_candidate", observation=ticket.id, interpretation=f"Similar ticket in state {ticket.state.value}: {ticket.title}", impact=f"Jaccard similarity: {similarity:.2f}")`. Append these EvidenceItems to the finding's evidence list. **Do NOT skip creation** — heuristics produce context, explicit lifecycle decisions control work. (6) If passed exact dedup → call store.add(create_ticket(...)) mapping DiscoveryFinding fields to Ticket fields. Set source=finding.discovery_role, finding_id=finding.finding_id, fingerprint=finding.fingerprint(). If related_ticket_candidates exist, also store them in the ticket's lifecycle_context under key "discovery" with sub-key "related_candidates" as list of dicts. (7) Move to processed/. (8) Return count of new tickets. Wire into `Scheduler.tick()` at `codebot/scheduler_v2/dispatcher.py:626-634` AFTER request ingestion and BEFORE dispatcher.tick(). Must accept injected store parameter.
  Parallelization: Wave 3 | Blocked by: none | Blocks: 10
  References (executor has NO interview context - be exhaustive): codebot/discovery_finding.py:141-156 (EvidenceItem dataclass with kind field), codebot/discovery_finding.py:246-272 (DiscoveryFinding fields), codebot/discovery_finding.py:280-293 (fingerprint method), codebot/ticket_engine.py:440-503 (create_ticket), codebot/ticket_engine.py:340-341 (Ticket.finding_id and fingerprint fields), codebot/ticket_engine.py:1069-1071 (_index_fingerprint for dedup), codebot/ticket_engine.py:2085-2121 (find_similar returns list[tuple[Ticket, float]]), codebot/scheduler_v2/dispatcher.py:626-634 (Scheduler.tick where ingestion hooks in), codebot/scheduler_v2/dispatcher.py:597 (self._state_dir)
  Acceptance criteria (agent-executable): `python3 -c "
from codebot.finding_ingestion import ingest_findings
from codebot.ticket_dispatcher import get_ticket_store
import tempfile, json
from pathlib import Path
with tempfile.TemporaryDirectory() as td:
    sd = Path(td)
    for d in ['findings', 'findings/processed', 'findings/rejected']:
        (sd / d).mkdir(parents=True)
    finding = {'finding_id':'test-exact','discovery_role':'user_agent','discovery_category':'feature','title':'Test','problem_statement':'Test problem','severity':'low','priority':'medium','confidence':'high','atomicity':'small','repository':'.','repository_revision':'abc','evidence':[],'acceptance_outcome':'done'}
    (sd / 'findings' / 't1.json').write_text(json.dumps(finding))
    store = get_ticket_store(sd / 'tickets.json')
    n1 = ingest_findings(store, sd)
    assert n1 == 1, f'Expected 1, got {n1}'
    # Exact dedup: same finding_id suppressed
    (sd / 'findings' / 't2.json').write_text(json.dumps(finding))
    n2 = ingest_findings(store, sd)
    assert n2 == 0, f'Expected 0 (exact dedup), got {n2}'
    assert (sd / 'findings' / 'processed' / 't2.json').exists()
    # Malformed quarantined
    (sd / 'findings' / 'bad.json').write_text('not json')
    n3 = ingest_findings(store, sd)
    assert n3 == 0
    assert any((sd / 'findings' / 'rejected').glob('bad*'))
print('Finding ingestion OK')
"`
  QA scenarios (name the exact tool + invocation): happy: valid finding creates ticket, exact dedup suppresses duplicate finding_id. happy: fuzzy similar finding still creates ticket but includes EvidenceItem(kind="related_ticket_candidate") in evidence list. failure: malformed JSON quarantined. failure: store=None returns 0 gracefully. Evidence .omo/evidence/task-8-user-agent-role.md
  Commit: Y | feat(platform): add finding_ingestion with exact dedup suppression and fuzzy EvidenceItem enrichment

- [ ] 9. Add CODING_STANDARDS.md §20-22 and update Scheduler.tick ordering
  What to do / Must NOT do: Two changes. (A) Append to `docs/CODING_STANDARDS.md` after §19 (line ~460): §20 titled "Invariant 8 — ONE INGRESS CONTRACT" with text: "Raw external intent may enter TicketStore only through the dedicated REQUESTED ingress state. It must never enter DISCOVERED, GOAL, DECOMP, PLANNING, IMPLEMENT, REVIEW, or other work states until an ingress role (user_agent) has normalized it into canonical DiscoveryFindings. APIs, CLIs, webhooks, and external integrations write request envelopes to state/requests/ only; platform-owned request_ingestion creates REQUESTED tickets. Finding ingestion creates DISCOVERED tickets from validated Findings." §21 titled "Invariant 9 — IDEMPOTENT INGESTION" with text: "Reprocessing the same Finding or Request identifier must never create a second ticket. Exact matches (finding_id, fingerprint) suppress deterministically. Fuzzy matches (Jaccard similarity) enrich context only via EvidenceItem(kind='related_ticket_candidate') — they produce related_ticket_candidates, never suppress work. Failed/malformed inputs are quarantined to rejected/ directories, not left in the inbox for infinite reprocessing." §22 titled "Invariant 10 — CONTEXT IS THE CONTROL PLANE" with text: "The swarm is controlled through durable authoritative context produced by the lifecycle. Critical intent, constraints, decisions, acceptance criteria, and discovered facts must not exist only in prompts, chat history, agent memory, or scratchpads. **Scope:** Within this implementation, this invariant is enforced at the REQUESTED ingress boundary. Lifecycle-wide context gate enforcement for GOAL/TRIAGE/DECOMP/PLANNING/IMPLEMENT/REVIEW/COMPLETE transitions is a separate roadmap item. The pattern established here extends to all stages in future work. Every lifecycle stage must leave the project with equal or better authoritative context than it received." Update the authority statement at line 3-5 to reference §2-§10 instead of §2-§8. (B) Update `Scheduler.tick()` at `codebot/scheduler_v2/dispatcher.py:626-634` to call ingest_requests then ingest_findings after _reconcile() and before self._dispatcher.tick(store). Import both lazily.
  Parallelization: Wave 4 | Blocked by: 2, 8 | Blocks: 10
  References (executor has NO interview context - be exhaustive): docs/CODING_STANDARDS.md:1-5 (authority statement), docs/CODING_STANDARDS.md:457-460 (end of file), codebot/scheduler_v2/dispatcher.py:626-634 (tick method)
  Acceptance criteria (agent-executable): `grep -q 'ONE INGRESS CONTRACT' docs/CODING_STANDARDS.md && grep -q 'IDEMPOTENT INGESTION' docs/CODING_STANDARDS.md && grep -q 'CONTEXT IS THE CONTROL PLANE' docs/CODING_STANDARDS.md && grep -q 'Scope.*REQUESTED ingress' docs/CODING_STANDARDS.md && grep -q 'ingest_requests' codebot/scheduler_v2/dispatcher.py && grep -q 'ingest_findings' codebot/scheduler_v2/dispatcher.py && echo 'Standards + tick OK'`
  QA scenarios (name the exact tool + invocation): happy: all three invariants present, §22 scoped to REQUESTED, tick calls both ingestion functions. Evidence .omo/evidence/task-9-user-agent-role.md
  Commit: Y | docs(standards): add §20-22 invariants (scoped); feat(scheduler): wire ingestion into tick

- [ ] 10. Architecture and regression tests (19 tests)
  What to do / Must NOT do: Create `tests/test_user_agent_pipeline.py` with 19 tests: (1) `test_user_request_serialization_roundtrip` — serialize/deserialize UserRequest, verify tuple immutability. (2) `test_dedup_candidate_frozen_immutability` — verify DedupCandidate elements cannot be mutated. (3) `test_user_request_status_transitions_valid` — all legal transitions succeed. (4) `test_user_request_status_transition_invalid_raises` — PENDING→COMPLETE raises ValueError. (5) `test_user_request_sanctioned_mutations` — with_clarification_questions, with_response, reject, complete all produce correct frozen instances. (6) `test_request_api_creates_pending_file_with_dedup_candidates` — POST creates file, dedup_candidates persisted as DedupCandidate tuples. (7) `test_request_api_returns_similar_tickets` — response contains dedup_candidates from find_similar. (8) `test_request_api_graceful_degradation_on_store_lock` — store unavailable → empty candidates, still accepted. (9) `test_request_ingestion_creates_requested_ticket_with_request_class` — verifies TicketClass.REQUEST. (10) `test_request_ingestion_idempotent` — duplicate request_id skipped. (11) `test_request_ingestion_crash_recovery` — PENDING request with existing ticket reconciled to RUNNING. (12) `test_malformed_request_quarantined` — bad JSON moved to rejected/. (13) `test_reply_updates_context_only_no_transition` — POST reply updates UserRequest but does NOT call store.transition. (14) `test_reply_reconciliation_drives_deferred_to_requested` — ingest_requests detects PENDING+response+DEFERRED and transitions. (15) `test_user_bucket_queries_requested_only` — BUCKET_ORDER[0] is ("USER", ["REQUESTED"]). (16) `test_requested_routes_to_user_agent` — _role_for_ticket returns "user_agent" for REQUESTED. (17) `test_machine_discovered_triage_path_not_user_bucket` — DISCOVERED ticket is NOT in USER bucket, IS reachable via platform triage → GOAL bucket → goal_aligner role. Verifies the real path: DISCOVERED → triage → TRIAGED → GOAL bucket → goal_aligner. (18) `test_ambiguous_request_transitions_to_deferred` — user_agent sets AWAITING_INPUT, ticket goes DEFERRED. (19) `test_e2e_request_to_user_agent_to_findings_to_discovered` — full lifecycle: request → REQUESTED → user_agent → findings (with EvidenceItem candidates) → DISCOVERED, original REQUESTED → COMPLETE. All use tmp_path. All deterministic. Must NOT require running orchestrator. Must NOT hit network.
  Parallelization: Wave 4 | Blocked by: 6, 7, 8, 9 | Blocks: none
  References (executor has NO interview context - be exhaustive): tests/test_arch_authoritative_dispatch.py:1-84 (patterns), all modules from todos 1-9
  Acceptance criteria (agent-executable): `python3 -m pytest tests/test_user_agent_pipeline.py -v --tb=short` passes all 19
  QA scenarios (name the exact tool + invocation): happy: all 19 pass. failure: revert any single todo, corresponding test fails. Evidence .omo/evidence/task-10-user-agent-role.md
  Commit: Y | test(user-agent): add 19 regression tests for full lifecycle

## Final verification wave
> Runs in parallel after ALL todos. ALL must APPROVE. Surface results and wait for the user's explicit okay before declaring complete.
- [ ] F1. Plan compliance audit — verify all 10 todos completed, acceptance criteria met, no scope creep into ConcurrencyController/DispatchGate/legacy dispatchers
- [ ] F2. Code quality review — new modules under 250 pure LOC, stdlib-only, no type errors, frozen dataclasses with tuple[frozen_dataclass] fields, UserRequestStatus is proper Enum, sanctioned mutations return new instances
- [ ] F3. Real manual QA — start orchestrator, send request via codebotctl, verify immediate dedup_candidates in response (as DedupCandidate dicts), verify request file in inbox with persisted candidates, verify REQUESTED ticket created with TicketClass.REQUEST and origin_id set, verify user_agent spawns, verify it evaluates using sanctioned mutations and either rejects/clarifies/emits findings with EvidenceItem candidates, verify findings ingested into DISCOVERED tickets with exact dedup suppressing and fuzzy enriching, verify reply endpoint updates context only (no direct transition), verify reconciliation drives DEFERRED→REQUESTED, verify original REQUESTED reaches terminal state
- [ ] F4. Scope fidelity — verify no changes to TicketState beyond REQUESTED addition, no CANCELLED transition from REQUESTED, no legacy dispatcher modifications, no discovery_daemon changes, no ConcurrencyController/SpawnQueue changes, user_agent did not modify CURRENT-STATE docs, no auto-cancel of DEFERRED, no list fields on frozen dataclasses, §22 scoped to REQUESTED ingress

## Commit strategy
One commit per todo (10 commits total). Atomic: each commit passes its own tests independently. **Critical constraint**: Todo 5 is already a single atomic commit combining state addition and routing rewire — no separate commits for these. Ordered by dependency: 1,5 → 2 → 3 → 4 → 6,8 → 7 → 9 → 10. No squash unless requested.

## Success criteria
- `codebotctl request "Add rate limiting"` immediately returns `{status, request_id, dedup_candidates}` with dedup matches persisted on UserRequest as frozen DedupCandidate tuples
- request_ingestion creates exactly one REQUESTED ticket with TicketClass.REQUEST and origin_id set
- Crash recovery reconciles PENDING requests with existing tickets to RUNNING state
- Reply endpoint updates UserRequest context only (PENDING + response set) — no direct store.transition call
- Reply reconciliation in request_ingestion detects PENDING+response+DEFERRED and drives DEFERRED→REQUESTED during next tick
- Stale DEFERRED requests stay DEFERRED indefinitely (no auto-cancel)
- REQUESTED tickets cannot transition to CANCELLED (prevents ticket/request disagreement)
- Scheduler.tick() dispatches REQUESTED tickets to user_agent role only
- Machine-discovered DISCOVERED tickets flow through platform triage → GOAL bucket → goal_aligner, never USER bucket
- user_agent evaluates request using sanctioned mutations: argues if flawed (reject()), asks if ambiguous (with_clarification_questions()), emits Findings if valid (complete())
- user_agent modifies only DESIRED-STATE docs (ROADMAP, ADR proposals, requirements), never CURRENT-STATE docs
- Valid request: REQUESTED→COMPLETE, Findings appear in state/findings/, ingested to DISCOVERED
- Exact dedup (finding_id/fingerprint) suppresses duplicate findings deterministically
- Fuzzy dedup (Jaccard ≥ 0.8) enriches context with EvidenceItem(kind="related_ticket_candidate") but does NOT suppress
- Ambiguous request: REQUESTED→DEFERRED, not redispatched until human replies
- Rejected request: REQUESTED→REJECTED, terminal
- Malformed requests and findings quarantined to rejected/ directories
- Existing pipeline (DISCOVERED→GOAL→...→IMPLEMENT→REVIEW→COMPLETE) unchanged
- All 19 regression tests + 12 existing arch tests pass
- CODING_STANDARDS.md documents §20, §21, and §22 invariants (§22 scoped to REQUESTED ingress)
- UserRequest uses tuple[frozen_dataclass, ...] fields for true immutability
- Every lifecycle stage leaves the project with equal or better authoritative context than it received
