# CodeBot Roles Reference

Last updated: 2026-09-25

CodeBot defines **24 registered roles** in `role_registry.py` (plus `implementer` is unified, legacy prompts remain). Current prompt files are in `codebot/roles/*.md` (24 registered, 1 unified implementation, plus planning/control prompt-only leftovers).

All prompts follow `docs/ROLE_PROMPT_STANDARDS.md`. Discovery scanners are lean 22–26 line prompts since Sep 2026. `feature_hunter` remains 201 lines (index-driven). All reviewer prompts include inline `--- REVIEW PACKET ---` fallback instructions so agents proceed if the file read fails.

Legacy bot names are mapped via `roles/__init__.py` and `role_registry`.

## Discovery Roles (9) — outside scheduler, discovery daemon 5 slots / 60s interval

Discovery agents scan continuously via `discovery_daemon.py` (round-robin, 60s per role, 5 concurrent). All are READ-ONLY — they never modify source files. They only call `batch_grep` → `read` hits → `create_ticket`. Each mission is injected with a `CHANGED_FILES` block (git diff HEAD + porcelain, 30 files, 60s cache) and is instructed to scan dirty files first.

| Role | Incentive | Model tier | Adversarial To |
|------|-----------|------------|----------------|
| `bug_hunter` | Maximize true positives, penalized for false reports | STANDARD | Implementers |
| `security_auditor` | Find exploitable vulnerabilities | PREMIUM/thinking | implementer |
| `architecture_auditor` | Find coupling violations and technical debt | PREMIUM/thinking | implementer |
| `performance_auditor` | Find scalability regressions | STANDARD | implementer |
| `test_gap_auditor` | Maximize coverage gap detection accuracy | **CHEAP** xiaomi-mimo-2.5 | — |
| `documentation_auditor` | Find claims that are no longer true | **CHEAP** xiaomi-mimo-2.5 | implementer |
| `dependency_auditor` | Find supply chain risks | **CHEAP** xiaomi-mimo-2.5 | — |
| `ux_auditor` | Find usability and accessibility issues | STANDARD | — |
| `feature_hunter` | Convert roadmap deliverables into tickets (index-driven) | CHEAP | — |

Cheap tier is pinned in `discovery_daemon._ensure_bot` + `_launch`, not via `model_manager.next_model_for_role`.

## Planning Roles (3)

Planning agents analyze, decompose, and align work. They produce tickets and plans but never modify source code.

| Role | Incentive | Tier |
|------|-----------|------|
| `decomposer` | Break tickets into atomic, implementable pieces | background |
| `planner` | Produce complete actionable plans preventing rework | background |
| `goal_aligner` | Classify tickets NOW/LATER/NEVER via goal alignment | background |

`scheduler_v2` buckets: `GOAL → goal_aligner`, `DECOMP → decomposer`, `PLANNING → planner`.

## Implementation Roles (1 unified)

| Role | Incentive | Tool Access | Git Write |
|------|-----------|-------------|-----------|
| `implementer` | Make the requested change work correctly and completely | read/write/edit/grep/glob/bash + batch_read/batch_grep | Yes |

All ticket classes (bug/feature/security/performance/architecture/test/documentation/dependency/infrastructure) route to `implementer` via `TICKET_CLASS_TO_IMPLEMENTER`. Former 6-role split (`general_implementer` etc.) is retired to `legacy` and not scheduled.

### Operational Protocols (all agents)

1. **Scheduler agents**: claim `claims/{ticket}.{role}.claim.json` via `DispatchGate`, 90-slot pool.
2. **Discovery agents**: no claim, `CHANGED_FILES` block, 5-slot daemon pool, 60s per role.
3. **Heartbeat**: bare Unix timestamp to `{STATE_DIR}/{agent}.heartbeat` every 60s.
4. **Checkpoint**: `state/{ticket}.scratchpad.json` via `scratchpad.py` (claim protocol).
5. **Noop cap**: 20 empty scans → exit cleanly. Zero tickets = success for discovery.

## Review Roles (11)

Review agents evaluate implementations. All are READ-ONLY. Their incentives intentionally conflict with implementers. Review packets (`review_packets/{ticket_id}.json`) are created by the implementer completion handler or the scheduler's pre-spawn gate before reviewers start.

| Role | Incentive | Adversarial To |
|------|-----------|----------------|
| `reviewer` | Default broad reviewer: correctness, acceptance, scope. Escalates to specialists | implementer |
| `correctness_reviewer` | Specialist: logic errors, spec violations | implementer |
| `security_reviewer` | Specialist: security question escalated from reviewer | implementer |
| `architecture_reviewer` | Specialist: architecture question | implementer |
| `performance_reviewer` | Specialist: performance question | implementer |
| `concurrency_reviewer` | Specialist: races, lock ordering, TOCTOU | implementer |
| `data_integrity_reviewer` | Specialist: migrations, schema, destructive writes | implementer |
| `test_reviewer` | Specialist: test adequacy, coverage gaps | implementer |
| `documentation_reviewer` | Specialist: doc accuracy, drift | implementer |
| `adversarial_reviewer` | Specialist: hostile edge-case analysis | implementer |
| `simplicity_reviewer` | Specialist: complexity reduction | implementer |

Plus `ux_reviewer` (evaluates browser/a11y, only if `scheduler_v2` REVIEW routes to it). Specialists only answer the escalated question.

### Verdicts

- **APPROVE** → VERIFY (not directly to COMPLETE)
- **REWORK** → REWORK with findings
- **Escalation** → reviewer escalates security/architecture/perf/concurrency/data to specialist, then re-evaluates

## Verification Role (1)

| Role | Purpose | Tool Access |
|------|---------|-------------|
| `verifier` | Static verification of implementation against acceptance criteria after reviewer approval | read/grep/glob only (no bash) |

The verifier reads `review_packets/{ticket_id}.json`, inspects changed files statically, and writes a verdict to `verification/{ticket_id}.json`. APPROVE → COMPLETE, REWORK → REWORK. VERIFY tickets older than 10 minutes with no live verifier are auto-approved by the scheduler's stale verify sweep.

## Control Roles (5)

| Role | Purpose | Tier |
|------|---------|------|
| `ticket_triager` | Validates DISCOVERED → TRIAGED: completeness, dedup, fingerprint | interactive CHEAP |
| `git_sync` | Batched push / vendor sync (commits from completion_commit) | background CHEAP |
| `github_mirror` | Mirror issue files to GitHub Issues via `gh` CLI | background CHEAP |
| `budget_controller` | Token spend tracking, budget enforcement (via token_budget) | interactive CHEAP |

Also: `quality_gate` gate evaluation is not a role but a module; alignment lives in `api_runner`/`rl_engine`.

## Role Resolution

```
"bug_hunter" → codebot/roles/bug_hunter.md (lean 26-line, CHANGED_FILES + batch_grep)
     → discovery_daemon._ensure_bot(BotConfig interval=60s, tier=11)
     → process_manager._prepare_prompt_with_context(extra_block=CHANGED_FILES)
```

Scheduler agents resolve via `scheduler_v2` BUCKET_ORDER → BUCKET_TO_ROLE_SETS → role.
