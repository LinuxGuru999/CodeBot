# Existing Repository Workflow

Last updated: 2026-09-20

## Overview

CodeBot can point at an existing repository and begin understanding, documenting, and improving it. This is one of the two primary entry points into the autonomous engineering system.

## Workflow

```
1. Import/open repository
         ↓
2. Inventory repository structure
         ↓
3. Identify languages, frameworks, build systems,
   services, tests, dependencies, APIs, databases,
   deployment/configuration
         ↓
4. Build internal project model
         ↓
5. Read existing documentation
         ↓
6. Compare documentation against actual implementation
         ↓
7. Generate missing documentation
         ↓
8. Repair inaccurate or stale documentation
         ↓
9. Establish baseline repository health
         ↓
10. Begin autonomous discovery
         ↓
11. Generate engineering work
         ↓
12. Improve the project continuously
         ↓
13. Accept direct user requests alongside autonomous work
         ↓
14. Expose progress and repository health through UI
```

## Step 1: Import Repository

The user provides a repository path, URL, or selects from available repositories.

```text
Select repository / path / URL

Optional:
Describe what you want CodeBot to accomplish.
```

## Step 2: Repository Inventory

CodeBot analyzes the repository to understand:

- **Languages**: Python, TypeScript, Go, Rust, etc.
- **Frameworks**: Django, React, FastAPI, etc.
- **Build Systems**: pip, npm, cargo, make, etc.
- **Services**: Web servers, workers, queues, etc.
- **Tests**: Unit tests, integration tests, E2E tests
- **Dependencies**: Package managers, lock files
- **APIs**: REST, GraphQL, gRPC, etc.
- **Databases**: SQL, NoSQL, migrations
- **Deployment**: Docker, CI/CD, cloud config

## Step 3: Project Model

CodeBot builds an internal model:

```text
Project Name: ...
Primary Language: ...
Framework: ...
Architecture Pattern: ...
Entry Points: ...
Test Framework: ...
Build System: ...
Deployment Target: ...
```

## Step 4: Documentation Analysis

CodeBot compares existing documentation against implementation:

- Documentation exists but code has changed → Mark stale
- Code exists but documentation missing → Mark for generation
- Documentation contradicts implementation → Mark for repair

## Step 5: Documentation Generation

CodeBot generates missing documentation:

- Project overview
- Architecture overview
- Module documentation
- API documentation
- Data model documentation
- Deployment instructions
- Configuration reference
- Developer setup
- Testing strategy
- Security model

## Step 6: Repository Health

Establishes baseline metrics:

- Test coverage
- Dependency freshness
- Security vulnerabilities
- Code quality indicators
- Documentation coverage

## Step 7: Autonomous Discovery

Begins continuous scanning for:

- Failing tests
- Security vulnerabilities
- Performance issues
- Documentation drift
- Dependency problems
- Test coverage gaps
- Architecture violations
- Code quality issues

## Step 8: User Requests

Accepts direct user input:

- "Add OAuth login"
- "Fix the memory leak"
- "Improve test coverage"
- "Update dependencies"
- "Refactor the authentication module"

## Step 9: Progress Monitoring

Exposes visibility into:

- What CodeBot is doing
- What has been completed
- What is currently being worked on
- What is planned
- Repository health metrics

## Initial Actions

After import, the user may choose goals:

```text
Understand and document this repository.
Improve this repository continuously.
Find and fix bugs.
Improve security.
Improve test coverage.
Modernize the project.
Add requested features.
Perform a full engineering assessment.
```

These guide initial prioritization without disabling general project understanding.

## Example

User:
```text
Point CodeBot at my Python web application repository.
```

CodeBot:
1. Analyzes repository structure
2. Identifies Django framework, PostgreSQL database
3. Generates architecture documentation
4. Discovers 3 security vulnerabilities
5. Creates tickets for each
6. Begins implementing fixes
7. Reports progress through dashboard
