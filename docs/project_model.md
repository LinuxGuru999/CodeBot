# Project Model

Last updated: 2026-09-20

## Overview

A CodeBot project is more than a repository. The project model contains the complete knowledge of what the software is, what it should do, and how it should evolve.

## Project ≠ Repository

```
PROJECT = REPOSITORY + INTENT + HISTORY + STATE
```

The repository stores implementation. The CodeBot project also contains:

- **Intent**: What the software is trying to achieve
- **Requirements**: Functional and non-functional requirements
- **History**: Major events and changes
- **Decisions**: Key architectural and product decisions
- **Feature State**: What features exist and their status
- **Engineering State**: Tickets, plans, and implementation progress
- **QA**: Quality assurance sessions and feedback
- **Metrics**: Project health and progress metrics
- **Documentation Model**: Documentation inventory and status

## Project Entity

A persistent project entity contains:

```text
repository
product specification
requirements
features
tickets
artifacts
documentation
agents
metrics
decisions
user feedback
QA sessions
history
settings
```

CodeBot understands a project across multiple sessions and development cycles.

## Project Knowledge Model

The project model answers:

```text
What is this product?
Who is it for?
What does it do?
What architecture does it use?
What features exist?
What features are planned?
What constraints exist?
What important decisions have been made?
What requirements remain unsatisfied?
What areas are risky?
What documentation exists?
```

## Project State Model

### Project Modes

CodeBot supports different operating modes based on project needs:

- **BUILD**: Focus on creating required functionality
- **MAINTAIN**: Focus on correctness, dependencies, testing, and stability
- **IMPROVE**: Actively search for worthwhile improvements
- **STABILIZE**: Prioritize bugs, tests, regressions, security, and reliability
- **QA**: Prioritize verification and user feedback
- **DOCUMENT**: Prioritize repository understanding and documentation

### Project Lifecycle

```
INITIALIZE
    ↓
UNDERSTAND (existing) or DESIGN (new)
    ↓
IMPLEMENT
    ↓
VERIFY
    ↓
IMPROVE (continuous)
```

## Project Configuration

### .codebot/project.yaml

Standardized project interface:

```yaml
name: my-project
type: application  # or library, service, etc.
languages:
  - python
  - typescript
frameworks:
  - react
  - fastapi
testing:
  framework: pytest
  coverage_threshold: 80
dependencies:
  policy: conservative
autonomy:
  level: 2
```

### .codebot/constitution.md

Protected invariants that agents cannot weaken:

```text
Product Purpose: ...
Security Boundaries: ...
Minimum Testing Standards: ...
Architectural Invariants: ...
Supported Platforms: ...
Compatibility Requirements: ...
Dependency Policies: ...
Human-Approval Requirements: ...
```

### .codebot/quality_gates.yaml

Verification policies:

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

## Project Adapter

Every managed project implements `ProjectAdapter`:

```python
class ProjectAdapter(ABC):
    def project_name(self) -> str
    def paths(self) -> ProjectPaths
    def test_config(self) -> ProjectTestConfig
    def dependency_policy(self) -> DependencyPolicy
    def autonomy_config(self) -> AutonomyConfig
    def components(self) -> list[ComponentDef]
    def bot_registry(self) -> list[dict]
    def model_profiles(self) -> dict[str, dict]
    def tier_priority(self) -> dict[str, int]
    def prompt_directory(self) -> Path
    def api_runner_command(self, bot_name, prompt_file) -> list[str]
    def is_protected_path(self, path) -> bool
    def validate_project(self) -> list[str]
```

CodeBot core resolves all paths, configs, and registries through this interface.

## Traceability

Every change traces back to its origin:

```
USER INTENT
    ↓
REQUIREMENT
    ↓
FEATURE
    ↓
ENGINEERING TICKETS
    ↓
CODE CHANGES
    ↓
TESTS
    ↓
VERIFICATION
    ↓
DOCUMENTATION
```

The system allows a user to inspect this chain.

## Project History

Major events preserved:

```text
requirements changed
feature requested
feature completed
architecture changed
QA revision submitted
release produced
critical bug discovered
critical bug resolved
```

This sits above raw Git history.

## Release Awareness

CodeBot understands:

```text
development state
release candidate
release
version
changelog
```

A release may trigger:

```text
full tests
security checks
documentation validation
QA
release notes
```
