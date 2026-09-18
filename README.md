# CodeBot

Portable autonomous software engineering platform.

CodeBot discovers, plans, implements, reviews, and verifies software changes across any sufficiently documented repository — without project-specific knowledge baked into its core.

## Architecture

```
.codebot/                  # Project contract (per-repository)
├── project.yaml           # Standardized project interface
├── constitution.md        # Protected invariants
└── quality_gates.yaml     # Verification policy

bots/                      # CodeBot core (portable, no project knowledge)
├── ticket_engine.py       # Normalized ticket schema + state machine
├── dependency_graph.py    # DAG ordering + cycle detection
├── risk_classifier.py     # Deterministic risk scoring → autonomy levels
├── implementation_planner.py  # Depth-scaled plan generation
├── quality_gate.py        # YAML-driven verification engine
├── gatekeeper.py          # Central completion authority
├── role_registry.py       # ROLE + TASK + MODEL + TOOL_POLICY abstraction
├── cost_tracker.py        # Per-ticket token economics
├── project_adapter.py     # Abstract interface (ABC)
├── codebot_bootstrap.py   # Adapter injection entry point
├── token_budget.py        # Fleet-wide budget ledger
├── tool_policy.py         # Sandbox boundary enforcement
├── prompt_gateway.py      # Prompt compression + spawn gating
├── api_tools.py           # read/write/edit/bash/grep/glob
├── metrics_collector.py   # Per-agent telemetry
├── rl_engine.py           # Epsilon-greedy bandit optimization
├── batch_scheduler.py     # Queue scheduling
├── readiness.py           # Readiness checks
├── auto_revert.py         # Rollback on failure
├── event_log.py           # Event persistence
├── findings_log.py        # Findings persistence
├── anomaly_alerts.py      # Anomaly detection
├── lease_state.py         # Distributed coordination
└── manifest_schema.py     # Schema validation
```

## Quick Start

```bash
# Install (stdlib-only, no dependencies)
pip install -e .

# Run tests
python3 -m pytest -q

# Bootstrap with a project
python3 -m bots.codebot_bootstrap --project-root /path/to/your/project
```

## How It Works

1. **Project Contract**: Every managed repository contains a `.codebot/` directory with `project.yaml` (structure, paths, testing, dependencies) and `constitution.md` (protected invariants agents cannot weaken).

2. **Adapter Injection**: `codebot_bootstrap.py` discovers the project's adapter and injects it into all core modules via `set_project_adapter()`. Core resolves paths/config through the adapter, never through hardcoded values.

3. **Ticket Lifecycle**: All work flows through normalized tickets: DISCOVERED → VALIDATING → TRIAGED → READY → PLANNING → IMPLEMENTING → REVIEWING → VERIFYING → COMPLETE. Deduplication via SHA-256 evidence hashing prevents duplicate work.

4. **Quality Gates**: YAML-defined verification policies enforce build, test, security, and documentation gates. Only the central gatekeeper may transition tickets to COMPLETE.

5. **Role-Based Agents**: 27 predefined roles across discovery/planning/implementation/review/control categories. Each role specifies required model capabilities, tool permissions, and adversarial relationships.

6. **Risk-Driven Autonomy**: Deterministic risk scoring (0-100) drives autonomy decisions. Constitution-protected categories always score 100 (human required). Low-risk documentation/test changes proceed autonomously.

## Adding a New Project

Create a `.codebot/project.yaml` and implement `ProjectAdapter`:

```python
from bots.project_adapter import ProjectAdapter

class MyProjectAdapter(ProjectAdapter):
    def project_name(self) -> str:
        return "my-project"
    # ... implement all abstract methods
```

Then bootstrap:
```python
from bots.codebot_bootstrap import bootstrap
adapter = bootstrap(Path("/path/to/my-project"))
```

## Version

0.2.0 — Portable core extracted from Monitor-BotNet.

## License

Proprietary.
