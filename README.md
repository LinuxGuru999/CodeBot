# CodeBot

**Autonomous Software Engineering Platform**

CodeBot is an autonomous software engineering environment capable of creating, understanding, documenting, maintaining, testing, reviewing, and continuously improving software projects.

It can begin with either an existing repository or a user description of a new application.

Once initialized, CodeBot manages the engineering lifecycle through discovery, decomposition, planning, implementation, review, verification, QA, documentation, and continuous improvement while providing users with an interface for requirements, feature requests, revisions, approvals, and progress monitoring.

---

## Two Entry Points, One Platform

```
                     CODEBOT
                        │
          ┌─────────────┴─────────────┐
          │                           │
          ▼                           ▼
 EXISTING SOFTWARE              NEW APPLICATION

 Point at repository            Describe application
          │                           │
          ▼                           │
 Understand                     Define requirements
 Document                       Design architecture
 Analyze                        Create repository
          │                           │
          └─────────────┬─────────────┘
                        ▼
              AUTONOMOUS ENGINEERING
                        │
      ┌─────────────────┼─────────────────┐
      │                 │                 │
      ▼                 ▼                 ▼
  Discover          User Requests        QA
      │                 │                 │
      └─────────────────┼─────────────────┘
                        ▼
                   Decompose
                        ↓
                      Plan
                        ↓
                   Implement
                        ↓
                     Review
                        ↓
                     Verify
                        ↓
                   Document
                        ↓
                     Release
                        ↓
               Continuous Improvement
                        ↑
                        │
                User monitors progress
                requests new features
                performs QA
                revises requirements
```

---

## What You Can Do

### For Existing Software

```text
"Here is my repository.
Understand it, document it, and make it better."
```

CodeBot will:
- Analyze repository structure, languages, frameworks, and dependencies
- Generate comprehensive project documentation
- Discover bugs, security issues, and improvement opportunities
- Create and implement fixes autonomously
- Add tests and improve coverage
- Continuously improve the codebase

### For New Software

```text
"Here is what I want to build.
Build it."
```

CodeBot will:
- Extract requirements from your description
- Design the architecture
- Create the repository and project structure
- Implement the application
- Test and verify functionality
- Generate documentation
- Accept revisions and new features

### After Initialization

```text
"Add this feature."
"Fix this bug."
"Change this behavior."
"This isn't working correctly."
"What's the current progress?"
```

CodeBot translates high-level human intentions into the engineering work necessary to evolve the software.

---

## Quick Start

```bash
# Install (stdlib-only, no dependencies)
pip install -e .

# Run tests
python3 -m pytest -q

# Bootstrap with an existing project
python3 -m codebot.codebot_bootstrap --project-root /path/to/your/project
```

---

## How It Works

### Autonomous Engineering Core

CodeBot's autonomous engineering pipeline handles the complete software development lifecycle:

1. **Discovery**: Continuously identifies work from source inspection, security scans, test gaps, documentation drift, and user feedback
2. **Decomposition**: Breaks work into manageable, independent engineering tasks
3. **Planning**: Creates implementation plans with risk assessment and dependency analysis
4. **Implementation**: Executes changes using specialized agent roles
5. **Review**: Adversarial multi-agent review ensures quality
6. **Verification**: Deterministic quality gates validate all changes
7. **Documentation**: Maintains project documentation as code evolves
8. **Continuous Improvement**: Proactively finds and implements improvements

### Project Model

CodeBot maintains a comprehensive project model that goes beyond the repository:

- **Intent**: What the software is trying to achieve
- **Requirements**: Functional and non-functional requirements
- **Features**: Implemented and planned features
- **Architecture**: System design decisions and constraints
- **Quality**: Test coverage, security posture, performance
- **Progress**: What's done, in progress, and planned
- **Decisions**: Key architectural and product decisions
- **History**: Major events and changes

### User Interface

CodeBot provides a user-facing interface for:

- **Requirements**: Define what you want to build
- **Feature Requests**: Add new capabilities at any time
- **Revisions**: Modify behavior without knowing the code
- **QA Feedback**: Report issues and provide feedback
- **Decisions**: Answer questions about product direction
- **Progress**: Monitor what CodeBot is doing
- **Approvals**: Control high-risk changes

---

## Architecture Overview

```
┌────────────────────────────────────────────────────────────┐
│                    USER INTERFACE                          │
│                                                            │
│  New Application · Existing Repository                    │
│  Feature Requests · Revisions · QA Feedback               │
│  Requirements · Progress · Decisions / Approvals          │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│               PROJECT INTENT MODEL                         │
│                                                            │
│  Goals · Requirements · Constraints · Features             │
│  Acceptance Criteria · Architecture Decisions              │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│             AUTONOMOUS ENGINEERING CORE                    │
│                                                            │
│  Discovery · Decomposition · Planning                      │
│  Implementation · Review · Verification                    │
│  QA · Documentation · Workforce Allocation                 │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│                  SOFTWARE PROJECT                          │
│                                                            │
│  Source Code · Tests · Documentation                       │
│  Infrastructure · Configuration · Builds                   │
└──────────────────────────────┬─────────────────────────────┘
                               │
                               ▼
┌────────────────────────────────────────────────────────────┐
│              CONTINUOUS IMPROVEMENT                        │
│                                                            │
│  Bugs · Security · Performance · UX                       │
│  Architecture · Dependencies · Tests · Docs                │
└────────────────────────────────────────────────────────────┘
```

---

## Core Capabilities

### Ticket Lifecycle

All work flows through normalized structured tickets:

```
DISCOVERED → VALIDATING → TRIAGED → READY → PLANNING → IMPLEMENTING → REVIEWING → VERIFYING → COMPLETE
```

With side states for exceptions:

```
BLOCKED · REWORK · REJECTED · DUPLICATE · DEFERRED · HUMAN_REQUIRED
```

### Role-Based Agents

29 registered roles across 5 categories:

- **Discovery**: Bug hunters, security auditors, architecture auditors, performance auditors
- **Planning**: Decomposers, implementation planners, dependency planners
- **Implementation**: General, backend, frontend, test, migration, documentation implementers
- **Review**: Correctness, security, architecture, test, performance, simplicity reviewers
- **Control**: Scheduler, quality gate, conflict resolver, budget controller

### Quality Gates

YAML-defined verification policies enforce:

- Build validation
- Test execution
- Security scanning
- Documentation review
- Architecture compliance

### Adaptive Workforce

Dynamic allocation of engineering capacity based on queue pressure, risk, and dependencies.

---

## Documentation

CodeBot generates and maintains documentation including:

- Project overview and README
- Architecture documentation
- API documentation
- Module documentation
- Data model documentation
- Deployment instructions
- Configuration reference
- Testing strategy
- Security model
- User documentation

Documentation is synchronized with code changes and maintained continuously.

---

## Security & Credentials

For details on how CodeBot manages secrets, SSH keys, and API tokens, see [docs/SECURITY.md](docs/SECURITY.md).

## Version

0.2.0 — Portable autonomous software engineering platform.

## License

Proprietary.
