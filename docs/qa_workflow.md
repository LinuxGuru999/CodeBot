# QA Workflow

Last updated: 2026-09-20

## Overview

CodeBot supports both autonomous QA and user-driven QA. Both feed into the engineering pipeline for continuous improvement.

## QA Types

### Autonomous QA

CodeBot performs automated quality assurance:

```text
tests
behavior verification
UI checks
API checks
security checks
regression checks
performance checks
```

### User QA

Users provide product-level feedback:

```text
product correctness
workflow usefulness
appearance
business requirements
subjective product quality
```

## User QA Interface

Users can inspect the application and provide feedback:

```text
bug
incorrect behavior
poor UX
visual issue
missing feature
performance issue
workflow problem
incorrect requirement interpretation
```

The system translates QA feedback into appropriate tickets while preserving the original user context.

## QA Revision Loop

```
application version
      ↓
user QA
      ↓
feedback
      ↓
classification
      ↓
ticket(s)
      ↓
implementation
      ↓
verification
      ↓
new application version
      ↓
user QA
```

This allows iterative product development without requiring users to manually create technical tickets.

## User-Requested QA Revision

Support natural language feedback:

```text
"This works, but change the workflow so users select a company first."
"The feature is technically correct but too confusing."
"The report needs more detail."
"This doesn't match what I described."
```

The system retains the conversation/feedback context and turns it into structured revision work.

## QA Classification

CodeBot classifies QA feedback:

```text
BUG           → Incorrect behavior, crash, error
UX_ISSUE      → Poor usability, confusing workflow
VISUAL        → Layout, styling, appearance issue
MISSING       → Feature not implemented as expected
PERFORMANCE   → Slow response, resource usage
REQUIREMENT   → Doesn't match original description
CHANGE        → Request to modify existing behavior
```

## QA to Tickets

QA feedback becomes engineering work:

```
QA Feedback
    ↓
Classify feedback type
    ↓
Identify affected components
    ↓
Create ticket(s)
    ↓
Plan implementation
    ↓
Implement fix
    ↓
Verify fix
    ↓
Update documentation
    ↓
Report to user
```

## Autonomous QA Pipeline

### Test Execution

```text
Unit Tests → Integration Tests → E2E Tests → Security Tests
```

### Quality Gates

```yaml
required:
  build: pass
  lint: pass
  unit_tests: pass
conditional:
  security_boundary:
    security_review: required
  api_change:
    contract_tests: required
```

### Review Process

```text
Correctness Review → Security Review → Architecture Review
        ↓                  ↓                  ↓
   Find bugs          Find exploits      Find violations
```

## QA Metrics

Track QA effectiveness:

```text
test coverage
bug detection rate
false positive rate
mean time to fix
regression rate
user satisfaction
```

## Progress Visibility

Users can see:

```text
what QA has been performed
what issues were found
what issues were fixed
what issues remain
what is being tested
current quality status
```
