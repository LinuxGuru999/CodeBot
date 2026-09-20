# Adversarial Review & Independent Gatekeeping

This document describes CodeBot's evidence-driven review and gatekeeping system.

## Overview

CodeBot's review pipeline ensures that `COMPLETE` means:

> The implementation has survived serious independent attempts to prove it wrong.

The system is built on three principles:

1. **Review must be adversarial** — reviewers attempt to break implementations
2. **Review must be independent** — reviewers derive their own conclusions from repository state
3. **Evidence beats opinion** — "looks correct" is not sufficient; findings require location, evidence, and reproduction

## Review Lifecycle

```
IMPLEMENTING
    ↓
REVIEWING
    ├── Independent reviewers evaluate
    ├── Adversarial reviewers attack
    └── Findings collected with severity
    ↓
VERIFYING
    ├── Deterministic gates (build, tests, lint)
    ├── Gatekeeper independently verifies evidence
    └── Completion evidence assembled
    ↓
FINAL_GATE
    ├── COMPLETE (all evidence supports completion)
    ├── REWORK (materially deficient)
    ├── BLOCKED (external dependency)
    └── NEEDS_HUMAN (rare, high-risk ambiguity)
```

## Finding Severity Levels

| Severity | Definition | Blocks Completion? |
|----------|-----------|-------------------|
| BLOCKER | Build broken, requirement fundamentally unmet, destructive behavior, data corruption | Always |
| CRITICAL | High probability of serious production failure | Always |
| MAJOR | Material correctness, reliability, security, maintainability, or compatibility problem | By default (configurable) |
| MINOR | Real issue that should be corrected but does not invalidate the core implementation | Never |
| NIT | Style or low-impact quality improvement | Never |
| INFO | Observation only | Never |

A single valid BLOCKER or CRITICAL finding blocks completion regardless of how many reviewers approve.

## Finding Format

Every finding must contain ALL of these fields:

```json
{
  "severity": "MAJOR",
  "category": "concurrency",
  "finding": "Ticket can be claimed by two workers during concurrent dispatch",
  "file": "scheduler.py",
  "location": "claim_ticket() line 42",
  "evidence": "No lock acquired between check and claim",
  "reproduction": "Run two dispatchers concurrently against same ticket",
  "expected": "Exactly one dispatcher claims the ticket",
  "actual": "Both dispatchers claim the ticket",
  "recommended_fix": "Use file lock around check-and-claim sequence"
}
```

Vague rejections ("needs improvement", "could be cleaner") are not representable.

## Mandatory Review Checklist

Every substantive implementation must be evaluated against:

- requirement_satisfied
- acceptance_criteria_satisfied
- existing_behavior_preserved
- relevant_tests_pass
- new_behavior_has_tests
- error_paths_tested
- boundary_conditions_considered
- security_implications_considered
- performance_implications_considered
- concurrency_implications_considered
- architecture_consistent
- no_unnecessary_scope_expansion
- no_dead_code_introduced
- logging_error_handling_appropriate
- documentation_updated_when_needed
- dependency_changes_justified
- no_obvious_regressions

Each item is marked PASS, FAIL, NOT_APPLICABLE, or UNKNOWN.

**UNKNOWN never behaves like PASS.** Critical unknowns trigger rework or further investigation.

## Change-Risk Classification

Before final verification, each ticket is assigned a risk class based on a 0-100 score:

| Risk Class | Score Range | Reviewers Required | Specialized Reviewers |
|-----------|-------------|-------------------|----------------------|
| LOW | 0-19 | 1 | None |
| MEDIUM | 20-44 | 1 | None |
| HIGH | 45-69 | 2 | security_reviewer |
| CRITICAL | 70-100 | 2 | security_reviewer, architecture_reviewer |

Risk scoring considers: severity, ticket class, module count, security impact, blast radius, and dependency count.

## Configuration

All review behavior is configurable via `.codebot/review.yaml`.

### Key Settings

```yaml
review:
  blind_review_enabled: true
  adversarial_review_enabled: true
  multi_reviewer_enabled: true
  cross_model_preferred: true
  require_tests_for_behavior_changes: true
  random_audit_percentage: 10
  skip_adversarial_for_trivial: true

reviewers_per_risk:
  LOW: 1
  MEDIUM: 1
  HIGH: 2
  CRITICAL: 2

specialized_reviewers_per_risk:
  LOW:
  MEDIUM:
  HIGH: security_reviewer
  CRITICAL: security_reviewer,architecture_reviewer

gatekeeper:
  blocking_severities: BLOCKER,CRITICAL,MAJOR
  require_test_evidence: true
  require_build_success: true
  require_regression_check: true
  require_checklist_complete: true
  deterministic_gates_first: true

rework:
  max_cycles: 3
  repeated_finding_threshold: 2
```

### Environment Overrides

Environment variables override file values:

- `CODEBOT_REVIEW_BLIND` — enable/disable blind review
- `CODEBOT_REVIEW_ADVERSARIAL` — enable/disable adversarial pass
- `CODEBOT_REVIEW_MULTI` — enable/disable multi-reviewer
- `CODEBOT_REVIEW_AUDIT_PCT` — post-completion audit percentage
- `CODEBOT_REWORK_MAX_CYCLES` — max rework cycles before escalation

## Gatekeeper Behavior

The gatekeeper is the sole authority that transitions tickets from VERIFYING to COMPLETE.

### Decision Process

1. Run deterministic quality gates (build, tests, lint)
2. Load all structured review decisions (*_review.json files)
3. Collect all findings across all reviewers
4. Check for unresolved blocking findings (BLOCKER/CRITICAL/MAJOR)
5. Build CompletionEvidence from gate results and review decisions
6. Decide: COMPLETE, REWORK, BLOCKED, or NEEDS_HUMAN

### Completion Requirements

A ticket reaches COMPLETE only when ALL of:

- All deterministic gates pass
- No unresolved BLOCKER, CRITICAL, or MAJOR findings
- Checklist is complete (no UNKNOWN on critical items)
- Requirement verified by at least one reviewer
- No failed acceptance criteria
- No failed tests

### Completion Evidence Artifact

Every gatekeeper decision produces a machine-readable evidence artifact:

```json
{
  "requirement_verified": true,
  "acceptance_passed": 5,
  "acceptance_failed": 0,
  "acceptance_unknown": 0,
  "tests_executed": 143,
  "tests_passed": 143,
  "tests_failed": 0,
  "new_tests_added": 6,
  "finding_counts": {"MINOR": 2},
  "build_verified": true,
  "security_checked": true,
  "regression_checked": true,
  "deterministic_gates_passed": true,
  "checklist_complete": true,
  "completion_confidence": 0.0
}
```

Confidence is calculated from evidence, not from an LLM's self-reported certainty.

## Reviewer Roles

| Role | Codename | Focus |
|------|----------|-------|
| correctness_reviewer | Logic | Spec compliance, logic errors, edge cases |
| security_reviewer | Shield | Injection, auth bypass, data leaks, attack surface |
| architecture_reviewer | Structure | Coupling, boundaries, dependency direction |
| performance_reviewer | Speed | Complexity regressions, memory, I/O |
| simplicity_reviewer | Clarity | Over-engineering, unnecessary abstractions |
| test_reviewer | Coverage | Test adequacy, missing scenarios, weak assertions |
| documentation_reviewer | Truth | Doc accuracy, completeness, drift |
| adversarial_reviewer | Breaker | Dedicated attacker: challenge tests, malformed inputs, race conditions |

All reviewers share:
- The Core Mandate (anti-rubber-stamp language)
- The mandatory checklist
- Structured finding format
- Completion blocking rules
- Independence from implementer conclusions

## Rework Handling

When review rejects a ticket:

1. Ticket transitions to REWORK with structured findings
2. Findings are injected into the implementer's prompt as REVIEWER FEEDBACK
3. Implementer corrects the defect
4. Ticket returns to IMPLEMENTING → REVIEWING
5. Previously failed findings are explicitly rechecked

### Rework Loop Protection

After `max_rework_cycles` (default 3), the system escalates to an alternate/senior reviewer or NEEDS_HUMAN. It never force-completes.

## Adversarial Review Phase

After independent reviewers complete their evaluation, a dedicated **adversarial_reviewer** (codename: Breaker) runs a separate attack pass. Its objective is:

> Find a valid reason this change should not be merged.

The adversarial reviewer:
- Constructs challenge tests (regression, negative, boundary, malformed-input, concurrency)
- Probes for race conditions, state corruption, partial failures, resource leaks
- Writes its verdict to `{STATE_DIR}/adversarial_review.json` with `phase: ADVERSARIAL_REVIEW`
- Any failing challenge test forces the ticket back to REWORK

## Gate Tampering Detection

The gatekeeper detects when a ticket modifies CodeBot's own quality controls:

- `review.yaml`, `quality_gates.yaml`
- `codebot/gatekeeper.py`, `codebot/quality_gate.py`
- `codebot/review_config.py`, `codebot/review_types.py`, `codebot/review_metrics.py`

Any such modification triggers **elevated scrutiny**: the ticket is automatically routed to REWORK with reason "gate/review config modified", regardless of other findings. This prevents agents from improving their completion rate by weakening quality controls.

## Post-Completion Audits

A configurable percentage of completed tickets (default 10%) are re-reviewed by an independent auditor. Sampling is risk-weighted: higher-risk completions are more likely to be audited.

Audit selection is implemented in `review_metrics.select_tickets_for_audit()`. Results are recorded via `review_metrics.record_audit_result()` and aggregated by `review_metrics.get_audit_stats()`.

If an audit finds a major defect:
- Ticket is reopened (COMPLETE → REWORK)
- A false-completion event is recorded
- Quality health alert `HIGH_AUDIT_REOPEN_RATE` fires if reopen rate >= 15% over 20+ audits

## Metrics

### Reviewer Metrics

Tracked per reviewer in `reviewer_metrics.jsonl`:

- tickets_reviewed
- approval_rate
- rejection_rate
- findings_per_review
- blocking_findings
- avg_duration_s

### Gatekeeper Metrics

Tracked in `gatekeeper_metrics.jsonl`:

- total_decisions
- complete_count
- rework_count
- blocked_count
- complete_rate
- rework_rate

### Quality Health Alerts

The system generates warnings for suspicious conditions:

- **GK_HIGH_APPROVAL**: Gatekeeper complete_rate >= 98% over 50+ decisions
- **GK_HIGH_REWORK**: Gatekeeper rework_rate >= 90% over 50+ decisions
- **HIGH_ESCAPED_DEFECTS**: More than 10 escaped defects recorded

## Self-Grading Protection

Implementers cannot:
- Alter review criteria
- Weaken tests purely to pass
- Remove failing tests without justification
- Disable lint/type checking
- Modify quality thresholds
- Change gatekeeper policy
- Lower coverage thresholds
- Suppress security findings

Changes to CodeBot's own review/gatekeeping policy are automatically classified as high-risk.

## File Locations

| File | Purpose |
|------|---------|
| `codebot/review_types.py` | Core types: FindingSeverity, StructuredFinding, ReviewChecklist, CompletionEvidence, ReviewDecision |
| `codebot/review_config.py` | ReviewConfig dataclass, YAML loading, env overrides |
| `.codebot/review.yaml` | Operator-facing configuration |
| `codebot/review_metrics.py` | Reviewer/gatekeeper calibration metrics, quality health alerts |
| `codebot/gatekeeper.py` | Gatekeeper: independent final gate |
| `codebot/ticket_dispatcher.py` | advance_reviewed_tickets: severity-driven transitions |
| `codebot/roles/*_reviewer.md` | Reviewer role prompts with adversarial mandate |
| `tests/test_review_adversarial.py` | 27 unit tests for the review framework |

## Definition of Done

This system is complete when:

1. Broken implementations are reliably rejected
2. Valid implementations can still reach COMPLETE without unnecessary friction
3. Reviewers provide structured, evidence-based findings
4. Gatekeepers independently validate reviewer conclusions
5. High-risk changes receive stronger review than low-risk changes
6. Missing evidence cannot silently become a PASS
7. Rework routes correctly back through implementation and review
8. Previous findings are verified after rework
9. Review/gatekeeper policy tampering receives elevated scrutiny
10. Post-completion audits can detect false completions
11. Escaped defects and reopened tickets are measurable
12. Review metrics are visible and queryable
13. Tests demonstrate intentional bad implementations being rejected
14. Existing ticket processing continues functioning correctly
15. Documentation accurately reflects the implemented behavior
