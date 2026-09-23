# CodeBot Discovery System Architecture

Last updated: 2026-09-20

## Overview

Discovery is the most important upstream stage of CodeBot. Its responsibility is to continuously determine what valuable engineering work exists in a repository, prove that the work is real, avoid rediscovering existing work, and create high-quality tickets that downstream stages can act upon.

Discovery answers:

```
WHAT is wrong, missing, risky, weak, inefficient, outdated, or worth improving?
WHY does it matter?
WHERE is the evidence?
IS it already known?
IS it worth creating work for?
```

## Core Philosophy

```
useful findings
────────────────────────
compute + tokens + noise
```

The system prefers **fewer high-confidence, actionable findings over large volumes of speculative work.** A discovery agent that correctly finds nothing worth doing may have performed excellent work.

## Architecture Components

### 1. DiscoveryFinding Schema (`codebot/discovery_finding.py`)

Structured finding representation that separates:

- **Observation**: What objectively exists (REQUIRED)
- **Interpretation**: Why the agent believes it is problematic
- **Impact**: What could happen because of it
- **Confidence**: HIGH / MEDIUM / LOW

Key types:
- `EvidenceItem` — one piece of evidence with observation/interpretation/impact separation
- `DiscoveryFinding` — full structured finding with all metadata
- `DuplicateCandidate` — reference to potentially duplicate existing ticket
- `FindingRelationship` — root-cause relationship between findings

The `finding_from_agent_output()` function repairs and validates raw model output, ensuring malformed JSON never produces garbage tickets.

Content fingerprinting (`fingerprint()`) enables semantic dedup: two findings about the same problem in the same files converge on the same fingerprint even when worded differently.

### 2. Evidence Validator (`codebot/evidence_validator.py`)

Pre-ticket evidence verification that runs BEFORE ticket creation:

- **File existence**: Confirms referenced files exist in the current repository
- **Symbol lookup**: Verifies function/class names exist (regex + AST for Python)
- **Generated/vendor detection**: Rejects evidence from `__pycache__`, `node_modules`, `.git/`, `dist/`, `vendor/`, etc.
- **Content matching**: Validates excerpts appear in the referenced file
- **Path traversal protection**: Rejects absolute paths and `..` traversal

The `revalidate_before_ticket_creation()` function is the final gate — it returns `(should_create, reason)` and blocks ticket creation when evidence is hallucinated, stale, or references generated code.

### 3. Ticket Engine Extensions (`codebot/ticket_engine.py`)

New fields on the Ticket dataclass (backward-compatible defaults):

| Field | Purpose |
|-------|---------|
| `confidence` | high/medium/low — evidence-backed certainty |
| `priority` | critical/high/medium/low — scheduling urgency (≠ severity) |
| `repo_revision` | git SHA at time of discovery |
| `atomicity` | atomic/compound/unknown — decomposition hint |
| `discovery_category` | normalized category from discovery role |
| `finding_id` | link to DiscoveryFinding that produced this ticket |
| `fingerprint` | content fingerprint for semantic dedup |

New TicketStore indexes:
- `_problem_index`: inverted index on problem-statement words for semantic similarity queries
- `_fingerprint_index`: exact-match fingerprint dedup

New TicketStore methods:
- `find_similar(problem_statement, affected_modules, threshold)` — semantic similarity search using Jaccard index over problem-statement words
- `discovery_history(fingerprint, evidence_hash, discovery_category)` — historical awareness lookup for previously completed/rejected/duplicate findings

The `add()` method now enforces fingerprint dedup: if a ticket with the same fingerprint exists in a non-terminal state, creation is rejected.

### 4. Discovery Manager (`codebot/discovery_manager.py`)

Enhanced with:

**Burst Detection (§52)**: `detect_burst()` flags when ticket creation rate in a 5-minute window exceeds both an absolute threshold (15 tickets) AND is 5× the baseline rate. When detected, `compute_allocation()` returns zero discovery slots.

**Coverage Tracking (§28)**: `get_coverage_summary()` returns which scopes have been scanned, by which role, at what revision, and when. Also identifies roles that have never scanned.

**Change-Triggered Discovery (§30)**: `roles_for_change(change_kind)` maps completed-change types to relevant discovery roles:
- `"security"` → `security_auditor`
- `"concurrency"` → `bug_hunter`
- `"dependency"` → `dependency_auditor`, `security_auditor`
- `"ui"` → `ux_auditor`, `test_gap_auditor`

**Backpressure (§31)**: `compute_allocation()` accepts `downstream_backlog` parameter. When backlog exceeds the high watermark (100), discovery slots are reduced to 25%. When above target (50), reduced to 50%.

**Extended RoleYieldStats**: Tracks `no_actionable_runs`, `completed_downstream`, `hallucinated_references`, `stale_findings_prevented`, and `hallucination_rate`. These feed role quality metrics for marginal value assessment.

**CHEAP_DISCOVERY_ROLES**: `test_gap_auditor`, `documentation_auditor`, `dependency_auditor` use cheaper models (lower reasoning requirements, metadata-driven checks).

### 5. Findings Log (`codebot/findings_log.py`)

Extended `append_finding()` with keyword-only arguments for new discovery fields: `confidence`, `priority`, `repo_revision`, `atomicity`, `discovery_category`, `finding_id`, `fingerprint`, `affected_files`, `affected_symbols`, `relationships`. All optional — backward compatible with existing callers.

### 6. API Runner (`codebot/api_runner.py`)

The `_create_ticket_tool` now:

1. Resolves the project root from the adapter
2. Validates evidence via `revalidate_before_ticket_creation()` when `evidence_file` is provided
3. Rejects tickets with hallucinated file references
4. Accepts new discovery fields: `confidence`, `priority`, `atomicity`, `evidence_file`, `evidence_symbol`, `evidence_line`, `finding_id`, `fingerprint`, `repo_revision`
5. Auto-detects current repo revision via `get_current_revision()`

The tool schema is updated to document `priority` (distinct from severity), `confidence`, `atomicity`, and evidence location fields.

### 7. Role Prompts (`codebot/roles/*.md`)

All 8 discovery role prompts updated:

| Role | Codename | Focus |
|------|----------|-------|
| `bug_hunter` | Tracker | Logic errors, race conditions, leaks |
| `security_auditor` | Sentinel | Injection, auth, crypto, exposure |
| `architecture_auditor` | Architect | Coupling, circular deps, god classes |
| `performance_auditor` | Profiler | O(n²), hot paths, memory, N+1 |
| `test_gap_auditor` | Coverage | Behavioral gaps, untested transitions |
| `documentation_auditor` | Scribe | Wrong/stale docs, API drift |
| `dependency_auditor` | Supply | CVEs, licenses, platform compat |
| `ux_auditor` | Eye | Accessibility, usability, workflow friction |

Key changes to all prompts:
- **Removed** "minimum 5 tickets" anti-pattern
- **Added** self-challenge: "What evidence would prove this finding wrong?"
- **Added** NO_ACTIONABLE_FINDINGS as a valid successful outcome
- **Added** evidence requirements (observation/interpretation/impact)
- **Added** severity vs priority distinction
- **Added** confidence and atomicity fields to create_ticket format
- **Added** stopping rule (exit when marginal value is low)
- **Added** generated/vendor file exclusion

## Discovery Lifecycle

```
Discovery Agent scans code
    │
    ▼
Candidate finding identified
    │
    ▼
Self-Challenge: "What would prove me wrong?"
    │
    ▼
Evidence validated against current repo state
    ├── File exists? Symbol exists? Content matches?
    ├── Not generated/vendored code?
    └── Not a hallucinated reference?
    │
    ▼
Duplicate check
    ├── Fingerprint match in non-terminal ticket? → REJECT
    ├── Evidence hash match? → REJECT
    └── Semantic similarity above threshold? → REVIEW
    │
    ▼
Historical awareness
    ├── Previously COMPLETE? → Check for regression
    ├── Previously REJECTED? → Skip unless new evidence
    └── Previously DUPLICATE? → Follow canonical ticket
    │
    ▼
Ticket created with structured fields
    │
    ▼
DiscoveryManager records completion
    ├── Cooldown updated
    ├── Yield stats updated
    ├── Burst detection checked
    └── Coverage summary updated
```

## Key Design Decisions

### Severity vs Priority (§13)

Severity measures **how bad the problem is**. Priority measures **how soon it should be worked on**. A severe issue in unreachable legacy code may have lower priority than a moderate bug currently crashing the scheduler.

### Confidence Levels (§14)

- **HIGH**: Failing test, confirmed vulnerability, reproducible exception
- **MEDIUM**: Strong static evidence, likely race condition, clear architectural duplication
- **LOW**: Speculative improvement, uncertain runtime behavior

HIGH confidence requires at least one concrete evidence kind (failing_test, runtime_behavior, benchmark, dependency_metadata, static_analysis, insecure_pattern).

### Atomicity Assessment (§43)

- **ATOMIC**: Single fix, e.g., "shlex.quote missing around repo_path"
- **COMPOUND**: Needs decomposition, e.g., "Agent lifecycle management is unreliable across restart, crash recovery, stale claims"
- **UNKNOWN**: Insufficient information to assess

### Backpressure Integration (§31)

Discovery allocation responds to downstream pipeline pressure:

```
downstream_backlog >= high_watermark (100) → 75% reduction
downstream_backlog >= target (50)          → 50% reduction
burst detected                             → 100% reduction (zero slots)
```

### Burst Detection (§52)

Prevents discovery storms by detecting when ticket creation rate in a 5-minute window exceeds both:
1. Absolute threshold: 15 tickets
2. Relative threshold: 5× the baseline rate

## Testing

Test coverage for the discovery system:

| Test File | Coverage |
|-----------|----------|
| `tests/test_discovery_finding.py` | Finding schema, validation, serialization, fingerprinting, agent output repair |
| `tests/test_evidence_validator.py` | File existence, symbol verification, generated/vendor detection, hallucination rejection |
| `tests/test_discovery_enhanced.py` | Burst detection, coverage tracking, NO_ACTIONABLE_FINDINGS, semantic dedup, backpressure, change-triggered discovery, persistence |
| `tests/test_ticket_engine.py` | New fields, fingerprint dedup, find_similar, discovery_history |
| `tests/test_discovery_manager.py` | CHEAP_DISCOVERY_ROLES, yield stats, allocation, cooldown |

## Module Index

| Module | Lines | Purpose |
|--------|-------|---------|
| `discovery_finding.py` | ~557 | Structured finding schema with evidence separation, fingerprinting, validation |
| `evidence_validator.py` | ~515 | Pre-ticket evidence verification: file existence, symbol lookup, hallucination detection |
| `discovery_manager.py` | ~734 | Cooldown, yield stats, burst detection, coverage, backpressure, change-triggered discovery |
| `findings_log.py` | ~179 | Extended findings JSONL with confidence, revision, atomicity, relationships |
| `ticket_engine.py` | ~1451 | Extended Ticket schema, semantic dedup indexes, find_similar, discovery_history |
| `api_runner.py` | ~2621 | Evidence validation in create_ticket tool, new finding fields |
