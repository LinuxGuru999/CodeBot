# CodeBot

Portable autonomous software engineering platform.

CodeBot discovers, plans, implements, reviews, and verifies software changes across any sufficiently documented repository — without project-specific knowledge baked into its core.

## Architecture

```
.codebot/                  # Project contract (per-repository)
├── project.yaml           # Standardized project interface
├── constitution.md        # Protected invariants
└── quality_gates.yaml     # Verification policy

codebot/                   # CodeBot core (portable, no project knowledge)
── __init__.py
├── __main__.py
├── adaptive_rate_limiter.py
├── adaptive_scheduler.py
├── alignment_events.py
├── alignment_service.py
├── anomaly_alerts.py
├── api_runner.py
├── api_tools.py           # read/write/edit/bash/grep/glob
├── auto_revert.py         # Rollback on failure
├── batch_scheduler.py     # Queue scheduling
── bot_metrics.py
├── botop.py
├── checkpoint_manager.py
├── codebot_adapter.py
├── codebot_bootstrap.py   # Adapter injection entry point
├── conflict_detector.py
├── context_compactor.py
── control_client.py
├── control_server.py
├── cost_tracker.py        # Per-ticket token economics
├── coverage_bridge.py
├── coverage_runner.py
├── credentials.py
├── dependency_graph.py    # DAG ordering + cycle detection
├── discovery_manager.py
├── event_log.py           # Event persistence
├── file_lock.py
├── findings_log.py        # Findings persistence
├── gatekeeper.py          # Central completion authority
├── implementation_planner.py  # Depth-scaled plan generation
├── integration_queue.py
├── lease_state.py         # Distributed coordination
├── manifest_schema.py     # Schema validation
├── metrics_collector.py   # Per-agent telemetry
├── migrate_queue.py
├── model_router.py
├── monitor_adapter.py
├── orchestrator.py
├── pipeline_state.py
├── pricing_table.py
├── project_adapter.py     # Abstract interface (ABC)
├── prompt_gateway.py      # Prompt compression + spawn gating
── prompt_optimizer.py
├── quality_gate.py        # YAML-driven verification engine
├── queue_pressure.py
├── readiness.py           # Readiness checks
├── risk_classifier.py     # Deterministic risk scoring → autonomy levels
├── rl_engine.py           # Epsilon-greedy bandit optimization
├── role_prompt.py
├── role_registry.py       # ROLE + TASK + MODEL + TOOL_POLICY abstraction
├── scheduler_config.py
├── scheduler_metrics.py
├── scratchpad.py
├── stale_branch_detector.py
├── stats_collector.py
├── task_splitter.py
├── telemetry.py
├── ticket_engine.py       # Normalized ticket schema + state machine
├── token_budget.py        # Fleet-wide budget ledger
├── tool_policy.py         # Sandbox boundary enforcement
├── web_tools.py
└── work_scorer.py
```

## Quick Start

```bash
# Install (stdlib-only, no dependencies)
pip install -e .

# Run tests
python3 -m pytest -q

# Bootstrap with a project
python3 -m codebot.codebot_bootstrap --project-root /path/to/your/project
```

## How It Works

1. **Project Contract**: Every managed repository contains a `.codebot/` directory with `project.yaml` (structure, paths, testing, dependencies) and `constitution.md` (protected invariants agents cannot weaken).

2. **Adapter Injection**: `codebot_bootstrap.py` discovers the project's adapter and injects it into all core modules via `set_project_adapter()`. Core resolves paths/config through the adapter, never through hardcoded values.

3. **Ticket Lifecycle**: All work flows through normalized tickets: DISCOVERED → VALIDATING → TRIAGED → READY → PLANNING → IMPLEMENTING → REVIEWING → VERIFYING → COMPLETE. Deduplication via SHA-256 evidence hashing prevents duplicate work.

4. **Quality Gates**: YAML-defined verification policies enforce build, test, security, and documentation gates. Only the central gatekeeper may transition tickets to COMPLETE.

5. **Role-Based Agents**: 29 registered roles across discovery/planning/implementation/review/control categories, with 40 prompt files. Each role specifies required model capabilities, tool permissions, and adversarial relationships.

6. **Risk-Driven Autonomy**: Deterministic risk scoring (0-100) drives autonomy decisions. Constitution-protected categories always score 100 (human required). Low-risk documentation/test changes proceed autonomously.

## Adding a New Project

Create a `.codebot/project.yaml` and implement `ProjectAdapter`:

```python
from codebot.project_adapter import ProjectAdapter

class MyProjectAdapter(ProjectAdapter):
    def project_name(self) -> str:
        return "my-project"
    # ... implement all abstract methods
```

Then bootstrap:
```python
from codebot.codebot_bootstrap import bootstrap
adapter = bootstrap(Path("/path/to/my-project"))
```

## Version

0.2.0 — Portable core extracted from Monitor-BotNet.

## License

Proprietary.
