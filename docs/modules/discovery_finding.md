# discovery_finding.py

Structured discovery finding representation for CodeBot's discovery system.

## Purpose

Defines the v1 structured finding schema that every discovery role must produce before a ticket may be created. A finding separates OBSERVATION (what objectively exists) from INTERPRETATION (why the agent believes it is problematic), IMPACT (what could happen), and CONFIDENCE (how certain the agent is).

## Key Types

### EvidenceItem

One piece of evidence, separating observation from interpretation:

```python
EvidenceItem(
    observation="resp.read() has no cap",        # REQUIRED
    interpretation="Memory exhaustion risk",
    impact="Large response exhausts memory",
    file_path="codebot/web_tools.py",
    line_number=87,
    symbol="web_fetch",
    kind="code_reference",
)
```

Evidence kinds: `code_reference`, `failing_test`, `missing_test`, `static_analysis`, `dependency_metadata`, `runtime_behavior`, `benchmark`, `unsafe_path`, `state_transition`, `dead_code`, `api_inconsistency`, `doc_disagreement`, `architectural_duplication`, `insecure_pattern`, `configuration`, `ux_inconsistency`, `other`.

Concrete evidence kinds (support HIGH confidence): `failing_test`, `runtime_behavior`, `benchmark`, `dependency_metadata`, `static_analysis`, `insecure_pattern`.

### DiscoveryFinding

Full structured finding with all metadata:

```python
DiscoveryFinding(
    finding_id="DF-ABC123DEF456",
    discovery_role="bug_hunter",
    discovery_category="bug",
    title="Unbounded read in web_fetch",
    problem_statement="web_fetch calls resp.read() without limit",
    severity="high",
    priority="high",
    confidence="high",
    atomicity="atomic",
    repository="codebot",
    repository_revision="abc123",
    evidence=[EvidenceItem(...)],
    acceptance_outcome="resp.read() capped at 1MB",
)
```

Key methods:
- `fingerprint()` — stable content fingerprint for dedup (category + normalized problem statement + affected files)
- `normalized_problem_statement()` — canonical word-sorted form for semantic comparison
- `confidence_is_supported()` — HIGH confidence requires at least one concrete evidence kind
- `validate()` — raises FindingValidationError for structural issues

### finding_from_agent_output()

The ONLY sanctioned path from model output to finding object. Repairs trivial issues (case coercion, enum fallbacks, title truncation, legacy evidence string conversion) before validating.

```python
finding = finding_from_agent_output(
    raw_dict,
    role="bug_hunter",
    repository="codebot",
    repository_revision="abc123",
)
```

## Invariants

- stdlib-only (dataclasses, enum, hashlib, json, time, uuid)
- Findings are immutable once created (frozen dataclasses)
- `validate()` raises FindingValidationError for unrepairable problems
- Severity (how bad) and priority (how soon) are distinct axes
- Evidence items always carry an observation; repair() never fabricates interpretation
