# CodeBot Context Map

Last updated: 2026-09-18

Shared vocabulary for the CodeBot domain. Agents and humans MUST use these terms consistently. See docs/ARCHITECTURE.md for module-level detail and docs/GOALS.md for strategy.

---

## Bounded Contexts

### 1. Work Orchestration
Owns process lifecycle and scheduling.

| Term | Meaning |
|------|---------|
| Orchestrator | Single process managing all agent lifecycles (`orchestrator.py`) |
| Slot | One unit of the 30-slot adaptive scheduler capacity |
| Spawn Gate | Preconditions (memory, gap, drain) checked before starting an agent |
| Drain | Flag file (`.codebot/state/.drain`) stopping new spawns for graceful shutdown |
| Heartbeat | Timestamp file each agent writes after atomic work; stale heartbeat = kill |
| Claim | File-based lease (`state/claims/{ticket}.{agent}.json`) preventing double-work |

### 2. Ticketing
Owns the unit of work and its lifecycle.

| Term | Meaning |
|------|---------|
| Ticket | Normalized work item; the ONLY authorized way to change code |
| State | One of 14 lifecycle positions (DISCOVERED … COMPLETE, plus REWORK/DEFERRED/etc.) |
| Evidence | SHA-256-hashed finding data used for deduplication |
| Rework | Reviewer/gate rejection sending a ticket back to IMPLEMENTING (max 3) |
| Recommendation Ticket | QA-stage recommendation generated for sensitive changes; resolved autonomously, never escalated to humans (§14) |

### 3. Verification
Owns proof that work is done.

| Term | Meaning |
|------|---------|
| Quality Gate | YAML-defined deterministic check (build, tests, conditional scans) |
| Gatekeeper | Central completion authority; ONLY path to COMPLETE |
| Adversarial Review | Reviewer role with incentive conflict against the implementer |
| QA-Stage Recommendation | Structured change recommendation surfaced during review for sensitive areas (auth, migrations, secrets); resolved through the review pipeline |

### 4. Agent Execution
Owns how roles become running processes.

| Term | Meaning |
|------|---------|
| Role | Capability definition: what an agent does (not a named bot) |
| Model Profile | Capability requirements (reasoning, coding, context, cost) for a role |
| Tool Policy | Allowlist of tools/commands/paths a role may use (fail-closed) |
| Mission | Prompt file assembled for one agent session |
| Contract | Shared behavioral rules injected into every agent prompt |

### 5. Portability
Owns project-independence.

| Term | Meaning |
|------|---------|
| Project Contract | `.codebot/project.yaml` — the standardized project interface |
| Constitution | `.codebot/constitution.md` — human-controlled invariants agents may not weaken |
| ProjectAdapter | ABC through which core resolves all paths/config; zero project knowledge in core |
| Capability Matrix | `.codebot/capability_matrix.yaml` — honest record of what CodeBot can do |

### 6. Economics & Learning
Owns cost and improvement signals.

| Term | Meaning |
|------|---------|
| Token Budget | Fleet-wide daily token cap |
| Cost Attribution | Per-ticket token/cost recording |
| Reward Signal | Outcome metric fed to the RL bandit after ticket completion |

---

## Cross-Context Rules

1. Discovery ≠ authorization: finding an issue creates a ticket candidate, never permission to change code.
2. Implementers cannot approve their own work; the Gatekeeper is the sole completion authority.
3. No human escalation: sensitive changes surface as QA-stage recommendations (§14, §84.C).
4. Deterministic evidence outranks model confidence.
5. Core contains no project-specific business logic; everything flows through ProjectAdapter.
