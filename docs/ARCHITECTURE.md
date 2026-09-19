# CodeBot Architecture

Last updated: 2026-09-18

## System Overview

CodeBot is a portable autonomous software engineering platform. It discovers work, plans implementation, executes changes through LLM-powered agents, verifies results through deterministic quality gates, and commits verified changes back to the target repository.

CodeBot operates as a standalone entity. It reads a project's `.codebot/project.yaml` contract to understand what to work on, resolves all paths through a `ProjectAdapter` interface, and writes results back via git. It contains zero project-specific knowledge in its core.

```
┌─────────────────────────────────────────────────────────────┐
│                      CodeBot Core                           │
│                                                             │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────┐   │
│  │Orchestrator│→│   Scheduler   │→│   API Runner       │   │
│  │(process   │  │(ticket store, │  │(LLM calls, tool   │   │
│  │ lifecycle)│  │ dep graph,   │  │ execution, claim)  │   │
│  └──────────┘  │ risk scoring)│  └────────┬───────────┘   │
│       ↑        └──────────────┘           │               │
│       │                                   ↓               │
│  ┌──────────┐  ┌──────────────┐  ┌────────────────────┐   │
│  │ Bootstrap │  │Role Registry │  │  Quality Gate      │   │
│  │(adapter  │  │(37 roles,    │  │  (YAML policy,     │   │
│  │ injection)│  │ legacy map)  │  │  gatekeeper)       │   │
│  └──────────┘  └──────────────┘  └────────────────────┘   │
│       ↑                                   │               │
│       │              ┌──────────────┐     │               │
│       └──────────────│ ProjectAdapter│←────┘               │
│                      │ (ABC)        │                     │
│                      └──────┬───────┘                     │
└─────────────────────────────┼─────────────────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │  .codebot/         │
                    │  project.yaml      │
                    │  constitution.md   │
                    │  quality_gates.yaml│
                    └───────────────────┘
                              │
                    ┌─────────┴─────────┐
                    │  Target Repository │
                    │  (Monitor, etc.)   │
                    └───────────────────┘
```

## Core Modules

| Module | Lines | Purpose |
|--------|-------|---------|
| `adaptive_scheduler.py` | ~700 | Core 30-slot adaptive concurrency scheduler control loop integrating all subsystems |
| `conflict_detector.py` | ~300 | File/module overlap detection between concurrent tickets, worktree isolation tracking |
| `discovery_manager.py` | ~435 | Discovery cooldown tracking, yield statistics per role, diversity allocation, saturation detection |
| `pipeline_state.py` | ~350 | Frozen dataclass snapshot of the entire engineering pipeline for scheduler decisions |
| `prompt_optimizer.py` | ~180 | Consumes RL alignment triggers to evolve agent prompts automatically |
| `queue_pressure.py` | ~470 | Calculates queue pressure ratios and detects bottlenecks for adaptive scheduling |
| `scheduler_config.py` | ~376 | Centralized configuration for the adaptive scheduler with YAML/JSON loading |
| `scheduler_metrics.py` | ~290 | Throughput metrics collector: utilization, cycle time, discovery yield, cost per ticket |
| `work_scorer.py` | ~392 | Utility scoring engine: priority + bottleneck relief + dependency unlock + aging - penalties |
| `orchestrator.py` | ~2900 | Process lifecycle: start, stop, health monitoring, heartbeat checking, drain management |
| `api_runner.py` | ~1450 | LLM execution loop: prompt assembly, tool dispatch, claim protocol, auto-commit |
| `ticket_engine.py` | ~390 | Normalized ticket schema v2, 14-state machine, SHA-256 dedup, persistent CRUD |
| `dependency_graph.py` | ~160 | DAG construction, cycle detection, topological sort, ready-ticket resolution |
| `risk_classifier.py` | ~100 | Deterministic risk scoring (0-100), constitution-aware autonomy decisions |
| `implementation_planner.py` | ~170 | Risk-scaled plan generation (summary/standard/full depth) |
| `quality_gate.py` | ~250 | YAML-driven gate engine, subprocess evaluation, conditional triggers |
| `gatekeeper.py` | ~120 | Central completion authority, max 3 rework cap, REWORK escalation |
| `role_registry.py` | ~400 | 25 role definitions with model profiles, tool policies, adversarial mappings |
| `role_prompt.py` | ~210 | Role template loading, project context injection, legacy name mapping |
| `prompt_gateway.py` | ~190 | Prompt compression, shared contract injection, spawn gating |
| `rl_engine.py` | ~880 | Epsilon-greedy bandit optimization, Q-value updates, reward shaping |
| `metrics_collector.py` | ~620 | Per-agent telemetry: execution, alignment, progress, liveness, quality, tokens |
| `token_budget.py` | ~140 | Fleet-wide token ledger, UTC-day accounting, budget enforcement |
| `cost_tracker.py` | ~200 | Per-ticket cost attribution, phase tracking, fleet summaries |
| `credentials.py` | ~120 | SSH/GitHub/API credential resolution from env vars or secret files |
| `control_server.py` | ~660 | HTTP control plane: agent status, logs, restart, drain, remote management |
| `control_client.py` | ~90 | Stdlib HTTP client for control server |
| `codebot_bootstrap.py` | ~150 | Adapter discovery, injection into core modules, project validation |
| `project_adapter.py` | ~130 | Abstract base class defining the 13-method adapter interface |
| `monitor_adapter.py` | ~180 | Concrete Monitor Platform adapter implementation |
| `migrate_queue.py` | ~230 | One-time QUEUE.md → TicketStore migration script |
| `codebot_adapter.py` | ~220 | Self-hosting adapter: CodeBot manages its own repo via 26-role registry |
| `model_router.py` | ~300 | Multi-provider model routing with automatic fallback and capability matching |
| `pricing_table.py` | ~170 | Model pricing table for monetary cost calculation (tokens to USD conversion) |
| `alignment_service.py` | ~250 | Decoupled alignment pipeline for reward scoring and prompt evolution |
| `integration_queue.py` | ~220 | Integration queue with dependency-aware merge ordering |
| `stale_branch_detector.py` | ~240 | Stale branch detection and cleanup mechanism |
| `adaptive_rate_limiter.py` | ~200 | Adaptive rate limiting based on API provider feedback |
| `alignment_events.py` | ~150 | Alignment event bus for reward scoring and prompt evolution triggers |
| `bot_metrics.py` | ~180 | Per-bot performance metrics collection, aggregation, and reporting |
| `checkpoint_manager.py` | ~200 | Agent checkpoint save/restore for crash recovery and session continuity |
| `file_lock.py` | ~120 | Cross-process file locking primitives for safe concurrent state access |

### Capability Modules (CAP Pipeline)

| Module | Purpose |
|--------|---------|
| `web_tools.py` | **SSRF-safe internet research**: provides `web_search()` (DuckDuckGo HTML lite) and `web_fetch()` (bounded HTTP GET with 1MB cap). Implements strict allowlisting of HTTP/HTTPS schemes, blocks private IP ranges (10.x.x.x, 172.16-31.x.x, 192.168.x.x, 127.x.x.x), enforces redirect limits, and validates DNS resolution to prevent server-side request forgery attacks. |
| `context_compactor.py` | Sliding window message summarization when approaching token budget |
| `scratchpad.py` | Ticket-scoped cross-agent context handoff via `ScratchpadState` and `AgentRecord` dataclasses; atomic JSON writes (tmp + os.replace) with 8KB hard cap; fail-open reads on corruption; enables seamless lifecycle handoffs between agents |
| `task_splitter.py` | Decomposes oversized tickets into ≤10 sub-tasks on agent failure |
| `coverage_runner.py` | Runs pytest-cov, parses output into per-module coverage reports |
| `coverage_bridge.py` | Generates tickets from coverage gaps, computes coverage delta scores |
| `botop.py` | Standalone CLI for agent operations (status, logs, restart, drain, tickets) |

### Infrastructure Modules

| Module | Purpose |
|--------|---------|
| `stats_collector.py` | Per-model API call statistics tracking (success, cost, tokens) |
| `telemetry.py` | HTTP ingestion endpoint for production telemetry signals to create tickets |
| `api_tools.py` | read/write/edit/bash/grep/glob tool implementations for LLM agents |
| `tool_policy.py` | Sandbox boundary: path resolution, command allowlisting |
| `readiness.py` | Readiness checks, noop detection, queue complexity parsing |
| `batch_scheduler.py` | Tier-based batch ordering, concurrency packing, capability caps |
| `manifest_schema.py` | Manifest loading and validation |
| `lease_state.py` | Distributed coordination via file-based leases |
| `event_log.py` | Append-only event persistence |
| `findings_log.py` | Findings persistence |
| `migrations/` | Database/schema migration scripts (migration_001, 003, 004) |
| `anomaly_alerts.py` | Anomaly detection on metrics |
| `auto_revert.py` | Automatic rollback on build gate failure |

## Data Flow

```
Discovery Agent (bug_hunter, security_auditor, etc.)
    │
    ▼
ticket_engine.create_ticket() → DISCOVERED state
    │
    ▼
ticket_triager validates → TRIAGED state
    │
    ▼
dependency_planner builds DAG → READY state (when deps satisfied)
    │
    ▼
implementation_planner generates plan → PLANNING state
    │
    ▼
scheduler picks ticket, assigns role + model → IMPLEMENTING state
    │
    ▼
api_runner executes via LLM with tools
    ├── Claims ticket (state/claims/{id}.{agent}.json)
    ├── Writes failing test (TDD red)
    ├── Implements change (TDD green)
    ├── Runs pytest (verify)
    ├── Auto-commits with ticket ID
    ├── Releases claim
    └── Transitions → REVIEWING state
    │
    ▼
Reviewers (correctness, security, architecture, etc.)
    ├── APPROVE → VERIFYING state
    └── REWORK → IMPLEMENTING state (increment rework_count)
    │
    ▼
gatekeeper runs quality gates
    ├── ALL PASS → COMPLETE state
    ├── ANY FAIL → REWORK (if rework_count < 3)
    └── rework_count >= 3 → REWORK state
    │
    ▼
git_sync commits + pushes verified changes
    │
    ▼
alignment_scorer processes exit event → reward signal
    │
    ▼
rl_engine updates Q-values → prompt_optimizer refines prompts
```

## State Machine

Tickets flow through 14 states:

```
DISCOVERED → VALIDATING → TRIAGED → READY → PLANNING → IMPLEMENTING → REVIEWING → VERIFYING → COMPLETE
    ↓            ↓           ↓         ↓         ↓           ↓             ↓            ↓
 REJECTED    DUPLICATE   DEFERRED   DEFERRED   BLOCKED     REWORK       REWORK       REWORK
                                            HUMAN_REQ   HUMAN_REQ    HUMAN_REQ
```

Valid transitions are enforced by `TRANSITIONS` dict in `ticket_engine.py`. Invalid transitions raise `ValueError`.

## Concurrency Model

- **Single process orchestrator** manages all agent lifecycles
- **File-based claims** (`state/claims/{ticket}.{agent}.json`) prevent double-work via atomic file creation (`open('x')` mode)
- **Claim TTL**: 2-hour expiry; stale claims can be stolen by other workers
- **Heartbeat monitoring**: each agent writes timestamp to `state/{name}.heartbeat` after every atomic task; orchestrator kills agents exceeding `effective_timeout`
- **TicketStore locking**: `threading.Lock()` protects `self._tickets` mutations; serialization happens under lock, disk I/O outside lock
- **Drain flag**: `state/.drain` file prevents new agent spawns for graceful shutdown

## Security Properties

| Property | Implementation |
|----------|---------------|
| Bounded I/O | All reads capped at 1MB (`resp.read(1_000_000)`) |
| No shell injection | `subprocess.run(shell=False)` with argv splitting |
| No path traversal | `resolve_workspace_path()` validates against workspace root |
| Command allowlisting | `tool_policy.allowlisted_command()` rejects unknown commands |
| Credential isolation | Secrets resolved from env vars only, never hardcoded |
| Atomic writes | `tmp.write_text()` + `os.replace()` prevents partial writes |
| No type suppression | Zero `as any`, `@ts-ignore`, or equivalent patterns |

## Performance Characteristics

| Operation | Complexity | Notes |
|-----------|-----------|-------|
| Ticket CRUD | O(1) dict lookup | `self._tickets[id]` |
| Evidence dedup | O(1) hash lookup | `self._evidence_index[hash]` |
| State serialization | O(n) tickets | Under lock, then I/O outside lock |
| Dependency sort | O(V + E) | Kahn's algorithm |
| Ready tickets | O(V) scan | Filter by satisfied dependencies |
| Risk scoring | O(1) | Deterministic formula |
| Gate evaluation | O(g) gates | Each gate is independent subprocess |
| Role resolution | O(1) dict lookup | `LEGACY_ROLE_MAP[name]` |

## Adapter Interface

Every managed project implements `ProjectAdapter` (13 abstract methods):

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

CodeBot core resolves all paths, configs, and registries through this interface. Adding support for a new project means implementing this ABC — zero changes to CodeBot core required.
