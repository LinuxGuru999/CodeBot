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
├── adaptive_rate_limiter.py   # Adaptive rate limiting based on API provider feedback
├── adaptive_scheduler.py      # Core 30-slot adaptive concurrency scheduler control loop
├── alignment_coordinator.py   # Coordinates alignment pipeline across agents
├── alignment_events.py        # Alignment event bus for reward scoring and prompt evolution
├── alignment_service.py       # Decoupled alignment pipeline for reward scoring
├── anomaly_alerts.py          # Anomaly detection on metrics
├── api_runner.py              # LLM execution loop: prompt assembly, tool dispatch, claim (commit happens at COMPLETE, not here)
├── api_tools.py               # read/write/edit/bash/grep/glob tool implementations
├── auto_revert.py             # Automatic rollback on build gate failure
├── batch_scheduler.py         # Tier-based batch ordering, concurrency packing
├── bot_metrics.py             # Per-bot performance metrics collection and reporting
├── botop.py                   # Standalone CLI for agent operations (status, logs, restart, drain)
├── checkpoint_manager.py      # Agent checkpoint save/restore for crash recovery
├── codebot_adapter.py         # Self-hosting adapter: CodeBot manages its own repo
├── codebot_bootstrap.py       # Adapter discovery, injection into core modules
├── completion_commit.py       # Per-ticket scoped git commit at COMPLETE + SHA traceability
├── config_reloader.py         # Hot-reloading for prompts, source code, and bot registry config
├── conflict_detector.py       # File/module overlap detection between concurrent tickets
├── context_compactor.py       # Sliding window message summarization
├── control_client.py          # Stdlib HTTP client for control server
├── control_server.py          # HTTP control plane: agent status, logs, restart, drain
├── cost_tracker.py            # Per-ticket cost attribution and fleet summaries
├── coverage_bridge.py         # Generates tickets from coverage gaps
├── coverage_runner.py         # Runs pytest-cov, parses per-module coverage reports
├── credentials.py             # SSH/GitHub/API credential resolution from env or secret files
├── dependency_graph.py        # DAG construction, cycle detection, topological sort
├── discovery_manager.py       # Discovery cooldown, yield stats, diversity allocation
├── dispatch_service.py        # Ticket dispatch orchestration service
├── event_log.py               # Append-only event persistence
├── file_lock.py               # Cross-process file locking primitives
├── findings_log.py            # Findings persistence
├── freeze_detector.py         # Detects and handles process freeze conditions
├── gatekeeper.py              # Central completion authority, max 3 rework cap; commits COMPLETE tickets' files
├── health_check.py            # Standalone health evaluation + eligible-bot startup
├── health_monitor.py          # Heartbeat monitoring, stuck/log-stall detection (BotState API)
├── implementation_planner.py  # Depth-scaled plan generation (summary/standard/full)
├── integration_queue.py       # Integration queue with dependency-aware merge ordering
├── lease_state.py             # Distributed coordination via file-based leases
├── lifecycle_scheduler.py     # Process lifecycle management and scheduling
├── locks.py                   # Cross-platform flock primitives (single source of truth)
├── manifest_schema.py         # Manifest loading and validation
├── metrics_collector.py       # Per-agent telemetry: execution, alignment, progress, quality
├── metrics_service.py         # Per-bot metrics snapshots for status views
├── migrate_queue.py           # One-time QUEUE.md → TicketStore migration script
├── model_router.py            # Multi-provider model routing with fallback
├── model_manager.py           # Model profiles: heartbeat multipliers, stall thresholds, lockup risk
├── monitor_adapter.py         # Concrete Monitor Platform adapter implementation
├── orchestrator.py            # Process lifecycle: start, stop, health, heartbeat, drain
├── orchestrator_services.py   # Supporting services for the orchestrator
├── pipeline_state.py          # Frozen dataclass snapshot of entire engineering pipeline
├── pricing_table.py           # Model pricing table for monetary cost calculation
├── process_manager.py         # OS process spawning, monitoring, and cleanup
├── project_adapter.py         # Abstract base class defining the 13-method adapter interface
├── prompt_gateway.py          # Prompt compression, shared contract injection, spawn gating
├── prompt_optimizer.py        # Consumes RL alignment triggers to evolve agent prompts
├── quality_gate.py            # YAML-driven gate engine, subprocess evaluation, file-hash pass cache
├── quality_metrics.py         # Quality-specific metrics tracking and reporting
├── queue_pressure.py          # Queue pressure ratios and bottleneck detection
├── readiness.py               # Readiness checks, noop detection, queue complexity parsing
├── review_config.py           # Blocking-severity policy + review config loading
├── review_metrics.py          # Review metrics aggregation
├── review_types.py            # ReviewDecision/StructuredFinding/CompletionEvidence types
├── risk_classifier.py         # Deterministic risk scoring (0-100), autonomy decisions
├── rl_engine.py               # Epsilon-greedy bandit optimization, Q-value updates
├── role_prompt.py             # Role template loading, project context injection
├── role_registry.py           # ROLE + TASK + MODEL + TOOL_POLICY abstraction (29 roles)
├── scheduler_config.py        # Centralized configuration for adaptive scheduler
├── scheduler_metrics.py       # Throughput metrics: utilization, cycle time, cost per ticket
├── scratchpad.py              # Ticket-scoped cross-agent context handoff
├── stale_branch_detector.py   # Stale branch detection and cleanup
├── state_manager.py           # Centralized state management for runtime data
├── stats_collector.py         # Per-model API call statistics tracking
├── task_splitter.py           # Decomposes oversized tickets into ≤10 sub-tasks
├── telemetry.py               # HTTP ingestion endpoint for production telemetry signals
├── ticket_dispatcher.py       # Dispatches tickets to appropriate agent roles; commits COMPLETE tickets' files
├── ticket_engine.py           # Normalized ticket schema v2, 14-state machine, SHA-256 dedup; records commit_sha
├── token_budget.py            # Fleet-wide token ledger, UTC-day accounting
├── tool_policy.py             # Sandbox boundary: path resolution, command allowlisting
├── web_tools.py               # SSRF-safe internet research (web_search, web_fetch)
├── worker_scaler.py           # Dynamic worker count scaling based on queue pressure
└── work_scorer.py             # Utility scoring: priority + bottleneck + dependency + aging
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

4. **Quality Gates**: YAML-defined verification policies enforce build, test, security, and documentation gates. Only the central gatekeeper may transition tickets to COMPLETE. Gate passes are cached by file hash (24h TTL) so unchanged re-runs skip subprocesses.

5. **Role-Based Agents**: 29 registered roles across discovery/planning/implementation/review/control categories, with 40 prompt files. Each role specifies required model capabilities, tool permissions, and adversarial relationships.

6. **Completion Commits**: After VERIFYING → COMPLETE, the ticket's own files are committed with a `[CB-xxx]` message and the SHA is recorded on the ticket (`commit_sha`). Commits are scoped (never `add -A`) and fail-open. No push — push stays batched/periodic.

7. **Risk-Driven Autonomy**: Deterministic risk scoring (0-100) drives autonomy decisions. Constitution-protected categories always score 100 (human required). Low-risk documentation/test changes proceed autonomously.

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
