# Progress Interface

Last updated: 2026-09-20

## Overview

CodeBot provides clear visibility into what is being done and why. Users should not need to interpret raw agent logs.

## What Users Can See

```text
what CodeBot is doing
what has been completed
what is currently being worked on
what is planned
what was discovered
what failed
what was rejected
what is waiting
what decisions require user input
what features exist
what features remain
current project health
```

## Multiple Views

### Product View

High-level feature status:

```text
Authentication        COMPLETE
Dashboard             COMPLETE
Reporting             IN PROGRESS
Remote Access         PLANNED
Billing               NOT STARTED
```

### Engineering View

Pipeline status:

```text
Discovery → Decomposition → Planning → Implementation → Review → Verification
     3            2              5              8              2            4
```

### Ticket View

Detailed engineering work:

```text
CB-101  Fix auth bypass     SECURITY   CRITICAL   IN_PROGRESS
CB-102  Add unit tests      TESTING    HIGH       READY
CB-103  Update docs         DOCS       MEDIUM     COMPLETE
```

### Agent View

Specialized agents and current activity:

```text
bug_hunter        scanning lib/auth.py
security_auditor  reviewing PR #42
general_impl      implementing CB-101
correctness_rev   reviewing CB-100
```

### Health View

Project health metrics:

```text
Tests:        85% passing
Coverage:     72%
Security:     3 findings
Performance:  Within budgets
Dependencies: 2 outdated
Build:        Passing
```

## Activity Feed

User-facing activity stream:

```text
14:20 — Security auditor discovered unsafe shell interpolation.
14:24 — Finding validated.
14:31 — Fix planned.
14:40 — Fix implemented.
14:44 — Tests passed.
14:47 — Review approved.
14:48 — Ticket completed.
```

This provides transparency without overwhelming the user.

## Progress Metrics

### Product Metrics

```text
features complete
requirements satisfied
user stories done
acceptance criteria met
```

### Engineering Metrics

```text
tickets complete
tickets remaining
tests passing
build health
security findings
known bugs
documentation health
current bottleneck
agent utilization
estimated work backlog
```

### Cost Metrics

```text
active agents
model usage
token usage
compute
API cost
cost by project
cost by feature
cost by ticket
```

## Dashboard Sections

### Overview

```text
Project Status: IN_PROGRESS
Mode: BUILD
Health: GOOD
Last Activity: 2 minutes ago
```

### Current Work

```text
IN PROGRESS:
  CB-101: Fix auth bypass (security)
  CB-102: Add unit tests (testing)

READY:
  CB-103: Update documentation
  CB-104: Refactor auth module

BLOCKED:
  CB-105: Database migration (waiting for approval)
```

### Recent Activity

```text
14:48 — CB-100 completed
14:47 — Review approved
14:44 — Tests passed
14:40 — Fix implemented
```

### Health Indicators

```text
Build:      ✅ Passing
Tests:      ✅ 85% passing
Security:   ⚠️ 3 findings
Coverage:   📊 72%
```

## User Questions

Users should be able to ask:

```text
What is currently blocking progress?
Why did you choose this architecture?
What are the biggest known risks?
What did CodeBot complete today?
What remains for the reporting feature?
Why was this ticket rejected?
What security issues remain?
What changed in the last release?
```

The project model provides answers grounded in current project state.

## Notifications

CodeBot should notify users of:

```text
important discoveries
decisions requiring input
milestones reached
issues requiring attention
completion of requested work
```

Notifications should be actionable and clear.
