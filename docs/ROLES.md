# CodeBot Roles Reference

Last updated: 2026-09-20

CodeBot defines **29 registered roles** across 5 categories in `role_registry.py`, with **40 prompt files** in `codebot/roles/*.md`. Each role specifies identity, incentives, tool constraints, operational protocols, and safety rules. Roles are loaded from `codebot/roles/*.md` and assembled with project context from the `ProjectAdapter` at runtime.

Some prompt files exist for roles not yet formally registered in `role_registry.py`. These are documented below with a note. They can be loaded by name but lack the full `AgentRole` definition (model profile, tool policy, adversarial mappings).

All prompts follow the hardened authoring standard in `docs/ROLE_PROMPT_STANDARDS.md`. Reference implementations per category: `bug_hunter.md` (discovery), `general_implementer.md` (implementation), `correctness_reviewer.md` (review), `scheduler.md` (control), `feature_decomposer.md` (planning).

Legacy Monitor bot names are mapped to CodeBot roles via `LEGACY_ROLE_MAP` in `role_prompt.py`.

## Discovery Roles (9)

Discovery agents scan source code for issues. All are READ-ONLY — they never modify source files.

| Role | Legacy Bot | Incentive | Adversarial To |
|------|-----------|-----------|----------------|
| `bug_hunter` | `issues` | Maximize true positives, penalized for false reports | Implementers |
| `security_auditor` | `security_auditor` | Find exploitable vulnerabilities | backend/frontend/general_implementer |
| `architecture_auditor` | `features` | Find coupling violations and technical debt | backend_implementer, simplicity_reviewer |
| `performance_auditor` | — | Find scalability regressions | backend/general_implementer |
| `test_gap_auditor` | `test_coverage` | Maximize coverage gap detection accuracy | — |
| `documentation_auditor` | — | Find claims that are no longer true | documentation_implementer |
| `dependency_auditor` | `dependency` | Find supply chain risks | — |
| `ux_auditor` | `ui_improve` | Find usability and accessibility issues | — |
| `feature_hunter` | — | Convert roadmap deliverables into tickets | — |

## Planning Roles (2 registered + 3 prompt-only)

Planning agents analyze, decompose, and order work items. They produce tickets and plans but never modify source code.

### Registered in `role_registry.py` (2)

| Role | Legacy Bot | Incentive |
|------|-----------|-----------|
| `decomposer` | `feature_decomposer` | Break tickets into atomic, implementable pieces |
| `implementation_planner` | — | Produce complete actionable plans preventing rework |

### Prompt files only (not in registry) (3)

These have `.md` prompt files but are not yet registered as `AgentRole` entries:

| Role | Legacy Bot | Incentive |
|------|-----------|-----------|
| `dependency_planner` | — | Ensure tickets execute in correct order |
| `architecture_planner` | — | Ensure changes align with architectural vision |
| `goal_steering` | `goal_steering` | Direct effort toward strategic priorities |

## Implementation Roles (6)

Implementation agents write code, tests, and documentation. They follow TDD (red-green-refactor) and the operational protocols (claim, heartbeat, checkpoint, noop cap). Commits happen at COMPLETE stage via completion_commit, not by agents.

| Role | Legacy Worker | Tool Access | Git Write |
|------|--------------|-------------|-----------|
| `general_implementer` | worker-1,2,3,11,12 | read/write/edit/grep/glob/bash | Yes |
| `backend_implementer` | worker-4,5,6 | read/write/edit/grep/glob/bash | Yes |
| `frontend_implementer` | worker-7 | read/write/edit/grep/glob/bash | Yes |
| `test_implementer` | worker-8 | read/write/edit/grep/glob/bash | Yes |
| `migration_implementer` | worker-9 | read/write/edit/grep/glob/bash | Yes |
| `documentation_implementer` | worker-10 | read/write/edit/grep/glob/bash | Yes |

### Operational Protocols (all implementers)

1. **Claim**: Write `{STATE_DIR}/claims/{ticket_id}.{agent_name}.json` before starting work. Delete on completion or failure.
2. **Heartbeat**: Write bare Unix timestamp to `{STATE_DIR}/{agent_name}.heartbeat` after every atomic task and at least every 60s.
3. **Checkpoint**: Write JSON to `{STATE_DIR}/{agent_name}.checkpoint.json` after every atomic task. Format per ROLE_PROMPT_STANDARDS.md §7.5.
4. **Auto-commit**: `git add -A → git commit -m "[{ticket_id}] {type}: {desc}" → git push`
5. **Noop cap**: Track consecutive empty scans. Exit cleanly at ≥ 20.

Where `{STATE_DIR}` = `{PROJECT_ROOT}/.codebot/state`.

## Review Roles (8)

Review agents evaluate implementations. All are READ-ONLY. Their incentives intentionally conflict with implementers.

| Role | Incentive | Adversarial To |
|------|-----------|----------------|
| `correctness_reviewer` | Find behavior tests missed | general/backend/migration_implementer |
| `security_reviewer` | Find a way to exploit the change | general/backend/frontend/migration_implementer |
| `architecture_reviewer` | Find coupling/boundary violations | general/backend_implementer |
| `test_reviewer` | Find behavior tests failed to cover | test_implementer |
| `performance_reviewer` | Find scalability regressions | general/backend_implementer |
| `simplicity_reviewer` | Find unnecessary complexity | general/backend/architecture_auditor |
| `documentation_reviewer` | Find claims no longer true | documentation_implementer |
| `ux_reviewer` | Find usability issues, accessibility violations, visual regressions | frontend/general_implementer |

### Verdicts

- **APPROVE** → transition to VERIFYING
- **REWORK** → document findings, transition to REWORK
- **ESCALATE/BLOCK** → transition to REWORK

## Control Roles (4 registered + 7 prompt-only)

Control agents manage infrastructure, scheduling, economics, and learning.

### Registered in `role_registry.py` (4)

| Role | Legacy Bot | Purpose |
|------|-----------|---------|
| `scheduler` | (orchestrator) | Agent scheduling, concurrency limits, model selection |
| `ticket_triager` | `bug_triage` | Validates incoming tickets: completeness, dedup, severity, routing |
| `budget_controller` | `prompt_opt` | Token spend tracking, budget enforcement |
| `conflict_resolver` | — | Merge conflict detection and resolution |

### Prompt files only (not in registry) (7)

These have `.md` prompt files but are not yet registered as `AgentRole` entries:

| Role | Legacy Bot | Purpose |
|------|-----------|---------|
| `quality_gate` | `build` | Central gate evaluation, COMPLETE authority |
| `github_mirror` | `github_bot` | Mirror issue files to GitHub Issues via `gh` CLI |
| `git_sync` | (implicit gitsync) | Auto-commit, push, vendor sync |
| `release_manager` | `release` | Staged rollout with gate-driven progression |
| `alignment_scorer` | `alignment` | Exit event processing, reward computation |
| `prompt_optimizer` | `prompt_opt` | Epsilon-greedy RSI with 11 Q-arms |
| `ticket_decomposer` | — | Break complex rework tickets into atomic sub-tickets |

## Role Resolution

When the orchestrator starts an agent named `issues`, the resolution chain is:

```
"issues" → LEGACY_ROLE_MAP["issues"] → "bug_hunter"
    → load_role_template("bug_hunter") → codebot/roles/bug_hunter.md
    → assemble_prompt("bug_hunter", adapter=...) → final prompt
```

If no mapping exists, the bot name is used directly as the role name.
