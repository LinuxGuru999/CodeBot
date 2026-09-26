# MANIFESTO.md

## Context Is the Source

Perfection comes from the source.
Code is the layer in-between.
CodeBot exists to continuously reduce the difference between authoritative intent and verified reality.

Software is not fundamentally code. Code is only the current implementation of a larger body of truth: requirements, constraints, decisions, architecture, security invariants, current state, desired state, tests, evidence, and observed behavior.

That body of truth is the source.
If the source is incomplete, contradictory, ambiguous, stale, or wrong, the software will inherit those defects.
Therefore:
**Perfect software cannot come from imperfect context.**

The system must improve the source, constrain the transformation from context into code, distrust the output until independently verified, and feed what reality teaches back into the source.

---

## 1. Context Is Authoritative

Context is the control plane of CodeBot.

Authoritative context includes:
- human intent;
- requirements;
- constraints;
- architecture and design decisions;
- security invariants;
- current state;
- desired state;
- dependencies;
- acceptance criteria;
- discoveries and evidence;
- review findings;
- verification results;
- unresolved conflicts and ambiguities.

Prompts tell an agent how to behave.
Context tells an agent what is true.

Important project truth MUST live in durable project context, not only in prompts, chat history, scratchpads, agent memory, or model state.
If destroying every running agent destroys important project knowledge, that knowledge was stored in the wrong place.

## 2. Code Is an Artifact

Code is not the ultimate authority.
Code is an artifact produced from context.

The intended relationship is:

```
AUTHORITATIVE CONTEXT
        ↓
IMPLEMENTATION
        ↓
EXECUTION
        ↓
OBSERVED REALITY
```

When code disagrees with authoritative context, the disagreement is a defect.
When observed reality disagrees with the code's claimed behavior, the disagreement is a defect.
When tests, documentation, architecture, security invariants, or implementation disagree with each other, the disagreement is a defect.
Completion means resolving those disagreements with evidence.

## 3. The Lifecycle Is a Context Compiler

The lifecycle does not merely move tickets between statuses.
It progressively transforms intent into increasingly precise, actionable, and verifiable context.

```
REQUEST
  ↓
DISCOVER
  ↓
GOAL
  ↓
DECOMPOSE
  ↓
PLAN
  ↓
IMPLEMENT
  ↓
REVIEW
  ↓
VERIFY
  ↓
COMPLETE
```

Every stage has one responsibility:
Receive authoritative context, perform one clearly owned transformation, and leave the project with equal or better authoritative context.

A lifecycle transition is therefore a context boundary.
**NO CONTEXT, NO ADVANCE.**
A stage is not complete because a worker exited successfully.
A stage is complete when the durable context required by the next stage exists and satisfies the transition contract.

## 4. One Authority for State

Lifecycle state MUST have one authoritative owner.
State changes MUST pass through the authoritative transition mechanism.
Direct state mutation outside that authority is forbidden.

Persistence, deserialization, new-record creation, and explicitly verified same-state field updates are not lifecycle transitions.
If `old.state != new.state`, the transition authority MUST own the change.
No scheduler, agent, reconciler, health checker, API handler, or recovery process may create an alternate state-transition path.

## 5. Agents Propose; The System Decides

Agents are disposable intelligence.
They may:
- discover;
- reason;
- propose;
- decompose;
- plan;
- implement;
- review;
- attack;
- verify assumptions.

Agents do not become authoritative merely because they produced an answer.

The governing rule is:
**Agents propose knowledge. The lifecycle validates knowledge. The platform persists authoritative knowledge.**

No agent may silently redefine existing authoritative context.
Conflicting context must be surfaced and reconciled explicitly.

## 6. Every Important Output Is Untrusted

Working code is not proof of correct code.
A passing local test is not proof of system correctness.
An implementation MUST be treated as potentially wrong until independently verified.

Different roles SHOULD have deliberately conflicting objectives:
- Implementer: make the requirement work.
- Reviewer: prove the implementation satisfies the requirement.
- Security auditor: find a way to break it.
- Fuzzer: search unexpected inputs and states.
- Architecture reviewer: detect structural disagreement.
- Verifier: prove the exact candidate integrates and passes deterministic checks.

Independent verification is preferred over self-review.
Trust comes from evidence, not authorship.

## 7. Security Is Correctness

Security is not an optional final-stage audit.
Security requirements are authoritative context.
Security invariants are correctness invariants.

Examples:
- untrusted input MUST NOT reach shell execution unsanitized;
- tenant-scoped data MUST require tenant authorization;
- secrets MUST NOT appear in logs, prompts, artifacts, or scratchpads;
- authentication failures MUST NOT expose protected information;
- privilege escalation MUST require explicit authorization;
- dependencies and external inputs MUST be treated as untrusted.

If an implementation violates a security invariant, the implementation is incorrect.
A security failure is a correctness failure.

## 8. Context Must Agree With the Transition

Presence alone is not always enough.
Context values MUST agree with the lifecycle transition they authorize.

For example:
- `GOAL → DECOMP` requires disposition `NOW`
- `GOAL → LATER` requires disposition `LATER`
- `GOAL → NEVER` requires disposition `NEVER`

A non-empty field containing the wrong value does not satisfy the contract.
Transition contracts SHOULD become more precise as the context model matures.

## 9. Fail Closed

Missing authority must never silently become permission.

If a legal transition has no explicit context contract:
**DO NOT ADVANCE.**

If required context is absent:
**DO NOT ADVANCE.**

If required context contradicts the requested transition:
**DO NOT ADVANCE.**

If verification cannot establish an important property:
**UNKNOWN IS NOT PASS.**

New lifecycle behavior must require explicit architectural intent.

## 10. Enforcement and Recovery Are Separate

The contract system answers:
*May this transition occur?*

Recovery answers:
*What should happen because it cannot occur?*

These are separate authorities.
A context gate may:
- allow the transition; or
- block the transition and persist a durable diagnostic.

It MUST NOT silently invent missing context.
It MUST NOT weaken the contract.
It MUST NOT recursively reroute lifecycle state unless that behavior belongs to an explicit recovery mechanism.

## 11. Failures Must Improve the Source

A discovered defect should produce more than a patch.
The desired loop is:

```
DEFECT
  ↓
ROOT CAUSE
  ↓
FIX
  ↓
REGRESSION EVIDENCE
  ↓
NEW OR IMPROVED INVARIANT
  ↓
SEARCH FOR RELATED FAILURES
  ↓
BETTER AUTHORITATIVE CONTEXT
```

The system should become harder to break each time it is broken.
Repeated failures without corresponding context improvement indicate a lifecycle failure.

## 12. Context Must Be Reconstructable

Durable truth is preferred over hidden memory.
Reconstructable state is preferred over caches.
Explicit state machines are preferred over implicit heuristics.
One owner is preferred over competing owners.
One queue is preferred over competing queues.
One clock is preferred over competing notions of time.
Boring beats clever.
Architecture should optimize for diagnosis, recovery, auditability, and correctness before novelty.

## 13. Scope Context, Do Not Dump It

Agents should not receive the entire project merely because context exists.
Each agent should receive the smallest complete context package necessary for its role.

A context package may include:
- global invariants;
- role-specific constraints;
- ticket intent;
- applicable requirements;
- relevant architecture decisions;
- dependencies;
- previous failures;
- acceptance criteria;
- local implementation context;
- relevant evidence.

The objective is not maximum context.
The objective is correct context.

## 14. Verification Must Bind to an Exact Artifact

Verification must prove something specific.
A verifier SHOULD operate on immutable identity such as:
- `ticket_id`
- `execution_id`
- `base_revision`
- `candidate_revision`

or an equivalently hashed patch/artifact.

Verification MUST NOT reconstruct implementation from ambiguous scratchpads or inferred diffs.
The thing verified must be the thing integrated.

## 15. Completion Means Evidence-Backed Agreement

A ticket is not complete because an agent stopped working.
Completion requires sufficient agreement between:

```
INTENT
REQUIREMENTS
CONSTRAINTS
ARCHITECTURE
IMPLEMENTATION
TESTS
SECURITY
DOCUMENTATION
OBSERVED REALITY
```

The exact evidence required depends on the work, but completion must represent convergence, not exhaustion.

## 16. Optimize for Correctness Before Throughput

Concurrency is useful only when it produces trustworthy progress.
Thirty agents producing ambiguous, conflicting, or unverifiable work are worse than five agents producing durable progress.

The swarm objective is not:
*Produce maximum code.*

It is:
*Reduce the distance between authoritative intent and demonstrably correct reality.*

Throughput must be measured in verified progress, not activity.

## 17. The Repository Is Long-Term Memory

The swarm is temporary.
Models are replaceable.
Agents are replaceable.
Processes are replaceable.
Authoritative project context must survive all of them.

The repository and durable project state form the long-term memory of the development system.
Anything important enough to influence future work is important enough to persist.

## 18. Perfection Is a Direction, Not a Claim

CodeBot must never confuse lack of discovered defects with proof that defects do not exist.

The operational goal is:
*Generate → challenge → verify → learn → improve until the available evidence cannot demonstrate a defect.*

Then continue monitoring reality.
Perfection is approached by continuously reducing disagreement between what should be true and what can be demonstrated to be true.

---

## North Star

```
AUTHORITATIVE CONTEXT
        ↓
SCOPED INTELLIGENCE
        ↓
IMPLEMENTATION
        ↓
ADVERSARIAL REVIEW
        ↓
DETERMINISTIC VERIFICATION
        ↓
OBSERVED REALITY
        ↓
CONTEXT RECONCILIATION
        ↓
BETTER AUTHORITATIVE CONTEXT
        ↓
REPEAT
```

The governing doctrine of CodeBot is:

**Context is the source.**
**Perfection comes from the source.**
**Code is the layer in-between.**

Improve the source.
Constrain the transformation.
Distrust the output.
Verify reality.
Feed what is learned back into the source.
Then build again.
