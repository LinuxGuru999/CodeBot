# evidence_validator.py

Pre-ticket evidence validation for discovery findings.

## Purpose

Validates that evidence referenced in a discovery finding actually exists in the current repository state BEFORE a ticket is created. Prevents stale findings, hallucinated file references, and phantom symbols from entering the downstream pipeline.

## Key Functions

### revalidate_before_ticket_creation()

The final gate before ticket creation:

```python
should_create, reason = revalidate_before_ticket_creation(
    project_root=Path("/path/to/repo"),
    evidence_items=[{"file_path": "codebot/foo.py", "symbol": "bar", "line_number": 42}],
    expected_revision="abc123",
)
if not should_create:
    # reason explains why: "hallucinated evidence", "generated file", etc.
```

Returns `(False, reason)` when:
- Evidence references non-existent files (hallucination)
- Evidence references generated/build output files
- Evidence references vendored/third-party code
- No evidence could be verified against current repository state

### validate_evidence_item()

Validates a single evidence reference:

```python
result = validate_evidence_item(
    project_root=Path("/path/to/repo"),
    file_path="codebot/ticket_engine.py",
    symbol="TicketStore",
    line_number=950,
    excerpt="def add(self, ticket)",
)
# result.is_valid, result.rejection_reasons
```

### Path Classification

```python
is_generated_path("codebot/__pycache__/foo.pyc")  # True
is_vendored_path("vendor/lib/foo.py")              # True
is_non_source_extension("foo.pyc")                 # True
```

Generated patterns: `__pycache__`, `.pyc`, `node_modules`, `.git/`, `dist/`, `build/`, `.tox/`, `.mypy_cache/`, `.pytest_cache/`, `*.egg-info`, `coverage/`, `.venv/`, `venv/`, `.codebot/state/`, `.omo/`, `.opencode/`, `logs/`.

Vendor indicators: `vendor/`, `third_party/`, `third-party/`, `external/`, `node_modules/`, `.bundle/`, `gems/`.

Non-source extensions: `.pyc`, `.pyo`, `.so`, `.dylib`, `.dll`, `.exe`, `.lock`, `.log`, `.jsonl`, `.png`, `.jpg`, `.zip`, `.pdf`, etc.

## Key Types

### EvidenceCheckResult

Result of validating one evidence reference. Properties: `is_valid`, `rejection_reasons`.

### EvidenceValidationResult

Aggregate result across all evidence items. Properties: `all_valid`, `has_rejections`, `rejection_summary`, `hallucination_detected`, `stale_evidence`, `generated_file_reference`, `vendor_reference`.

## Invariants

- stdlib-only (pathlib, re, ast, subprocess, logging)
- Read-only: never modifies files
- Bounded: each validation reads at most 500KB per file
- Fast: symbol lookup uses regex first, falls back to AST only for .py
- Fail-safe: I/O errors produce warnings, never crash discovery
