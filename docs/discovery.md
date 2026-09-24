# CodeBot Discovery System Architecture

Last updated: 2026-09-23

## Overview

Discovery is the upstream stage of CodeBot. It continuously determines what valuable engineering work exists, proves it is real, avoids rediscovering existing work, and creates high-quality tickets that downstream stages can act upon.

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

Fewer high-confidence, actionable findings over speculative volume. A discovery agent that correctly finds nothing worth doing performed excellent work.

## Runtime: Discovery Daemon (outside scheduler)

Since Sep 2026 discovery runs **outside** `scheduler_v2` in its own daemon so it does not compete for the 90 scheduler slots.

- **File**: `codebot/discovery_daemon.py`
- **Roles**: 9 (`bug_hunter`, `security_auditor`, `architecture_auditor`, `performance_auditor`, `test_gap_auditor`, `documentation_auditor`, `dependency_auditor`, `ux_auditor`, `feature_hunter`)
- **Concurrency**: `DISCOVERY_MAX_CONCURRENT = 5` own pool; scheduler uses 90, daemon uses 5 → total 95.
- **Cooldown**: `DISCOVERY_INTERVAL_SECONDS = 60` per role. `next_run_at` (set by `process_manager` after launch) plus in-memory `_last_run` plus heartbeat-file mtime.
- **Tick**: `DISCOVERY_TICK_SECONDS = 10`, own 2s stagger (`DISCOVERY_STAGGER_SECONDS`).
- **Scheduling**: Round-robin from `_next_index`, skip any role that is alive or on cooldown, `_ensure_bot()` creates `BotConfig` lazily.
- **Dirty-first**: Each spawn injects a `CHANGED_FILES` block from `git diff --name-only HEAD` (fallback `git status --porcelain`, 60s cache, 30-file cap) via `process_manager._prepare_prompt_with_context(extra_block=)`. Prompts are instructed to scan dirty files first.
- **Cheap tier pinned**: `test_gap_auditor`, `documentation_auditor`, `dependency_auditor` pinned to `xiaomi-mimo-2.5` in both `_ensure_bot()` and `_launch()`, not rotated.
- **Excluded from scheduler**: `dispatch_service.apply_agent_availability` and `orchestrator_services.apply_agent_availability` skip `DISCOVERY_ROLE_NAMES`; `health_check_loop.start_eligible_bots` also skips them. `scheduler_v2` has no DISCOVERY bucket by design (DISCOVERED → TRIAGED is platform code).
- **Lifecycle**: `orchestrator_runtime.run_main_loop` starts the daemon thread on `pid_path.parent` and joins on shutdown. Respects `is_draining()` and `orchestrator_runtime._shutdown_requested`.

Intervals are also in `codebot_adapter.bot_registry()`: discovery 60s, planning 3600s, implementation 300s, review 600s, control 900s. The 60s value survives restarts via heartbeat file.

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

### 4. Process Manager Wiring

- `process_manager._prepare_prompt_with_context(bot, extra_block="")` — appends `extra_block` after implementation packet. Discovery daemon passes the CHANGED_FILES block here.
- `_init_and_prepare_bot(bot, resume_checkpoint, extra_block="")` — threads `extra_block` through.
- Scheduler's `apply_agent_availability` and `health_check_loop.start_eligible_bots` both skip discovery roles.

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

8 scanner roles are **lean 22-26 line prompts** (was ~165 lines). Each follows:

| Prompt | Codename | Focus | Model tier |
|--------|----------|-------|------------|
| `bug_hunter` | Tracker | Logic errors, race conditions, leaks | standard |
| `security_auditor` | Sentinel | Injection, auth, crypto, exposure | premium/thinking |
| `architecture_auditor` | Architect | Coupling, circular deps, god classes | premium/thinking |
| `performance_auditor` | Profiler | O(n²), hot paths, memory, N+1 | standard |
| `test_gap_auditor` | Coverage | Behavioral gaps, untested transitions | **cheap** xiaomi-mimo-2.5 |
| `documentation_auditor` | Scribe | Wrong/stale docs, API drift | **cheap** xiaomi-mimo-2.5 |
| `dependency_auditor` | Supply | CVEs, licenses, platform compat | **cheap** xiaomi-mimo-2.5 |
| `ux_auditor` | Eye | Accessibility, usability, workflow friction | standard |

`feature_hunter` (Scout, 201 lines) is unchanged — index-driven, not a scanner.

Lean prompt pattern (all 8 scanners):
1. `read checkpoint` + `grep tickets.json` ONCE for dedup
2. **Dirty-first**: read `CHANGED_FILES` block, `batch_grep` 5 patterns on dirty files → `read` hits only; fallback `batch_grep` on `codebot/**/*.py`
3. Self-challenge before every ticket, `create_ticket` JSON, checkpoint+heartbeat

Tools allowed: `read`, `write`, `grep`, `glob`, `bash`, `create_ticket`, `batch_grep`, `batch_read`. Cheap-tier roles are told "be terse" (xiaomi-mimo-2.5 has weaker instruction-following).

Key invariants in lean prompts:
- JSON args only, no YAML
- `source` always equals own role name
- `evidence` file:line never empty, fabricate path = violation → exit cleanly
- Write scope strictly checkpoint + heartbeat under `.codebot/state/`

## Discovery Lifecycle

```
Discovery daemon tick every 10s (round-robin)
    │
    ▼ round-robin _next_index, skip alive/on-cooldown, up to 5 slots
Discovery agent launched via process_manager._launch_bot_subprocess
    │  (prompt + CHANGED_FILES block + contract)
    ▼
Agent scans: batch_grep 5 patterns on dirty files → read hits
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
Ticket created with structured fields (DISCOVERED)
    │
    ▼
Cooldown applied (60s next_run_at + _last_run + heartbeat mtime)
    ├── yield recorded via metrics
    └── agent exits cleanly, daemon rotates to next role
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

### Dirty-First Injection

Dirty files are injected as a text block (git diff HEAD + porcelain status, 30 files, 60s cache) via `extra_block`. This is a hint, not a hard gate — prompts are instructed to scan dirty first but fall back to full scan if none.

### Cheap Tier Pinning

Cheap discovery roles (`test_gap`, `documentation`, `dependency`) are pinned at the daemon layer to `xiaomi-mimo-2.5` so `model_manager.next_model_for_role()` rotation does not elevate them.

## Testing

Test coverage for the discovery system:

| Test File | Coverage |
|-----------|----------|
| `tests/test_discovery_finding.py` | Finding schema, validation, serialization, fingerprinting, agent output repair |
| `tests/test_evidence_validator.py` | File existence, symbol verification, generated/vendor detection, hallucination rejection |
| `tests/test_discovery_enhanced.py` | Coverage tracking, NO_ACTIONABLE_FINDINGS, semantic dedup |
| `tests/test_ticket_engine.py` | New fields, fingerprint dedup, find_similar, discovery_history |
| `tests/test_discovery_daemon.py` | 5-slot daemon, round-robin, cooldown, dirty-file injection, cheap tier (when present) |

## Module Index

| Module | Lines | Purpose |
|--------|-------|---------|
| `discovery_daemon.py` | ~385 | 5-slot daemon outside scheduler, round-robin, dirty-file injection, cheap-tier pin |
| `discovery_finding.py` | ~557 | Structured finding schema with evidence separation, fingerprinting, validation |
| `evidence_validator.py` | ~515 | Pre-ticket evidence verification: file existence, symbol lookup, hallucination detection |
| `process_manager.py` | +extra_block wiring | CHANGED_FILES injection for discovery |

