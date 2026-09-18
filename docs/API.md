# CodeBot API Reference

Last updated: 2026-09-18

## CLI Interface

Entry point: `python -m codebot <command> --project <path>`

| Command | Description |
|---------|-------------|
| `serve` | Start the orchestrator main loop |
| `status` | Print agent status and exit |
| `drain` | Set drain flag (stop spawning new agents) |
| `clear-drain` | Clear drain flag (resume spawning) |
| `validate` | Validate project contract and print summary |
| `stop-all` | Stop all running agents |
| `start [agents...]` | Start specific agents or all if none specified |

### Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `CODEBOT_PROJECT_ROOT` | `.` | Path to project root containing `.codebot/` |
| `CODEBOT_API_KEY` | — | LLM API key (overrides config file) |
| `CODEBOT_API_URL` | dialagram URL | LLM API endpoint |
| `CODEBOT_MAX_CONCURRENT` | `10` | Maximum simultaneous agents |
| `CODEBOT_MIN_SPAWN_GAP` | `20` | Minimum seconds between agent spawns |
| `CODEBOT_MIN_MEMORY_MB` | `30` | Minimum available memory to spawn |
| `CODEBOT_MAX_THINKING_CONCURRENT` | `3` | Max concurrent reasoning-model agents |
| `CODEBOT_MANIFEST_SCHEDULER` | `0` | Enable manifest-based scheduling |
| `GH_TOKEN` / `GITHUB_TOKEN` | — | GitHub authentication for mirror/sync |
| `CONTROL_TOKEN` | — | Control server bearer token |
| `SSH_PRIVATE_KEY_PATH` | `~/.ssh/id_ecdsa` | SSH key for git push |
| `SSH_AUTH_SOCK` | — | SSH agent socket |
| `GITHUB_DRY_RUN` | `1` | If `1`, commit locally but don't push |

## Control Server HTTP API

Default port: 8081. Auth: `Authorization: Bearer <CONTROL_TOKEN>`.

### GET /health

Returns orchestrator health status.

**Response** `200`:
```json
{
  "status": "ok",
  "version": "0.2.0",
  "agents_running": 8,
  "drain_active": false,
  "uptime_seconds": 3600
}
```

### GET /bots

Returns status of all registered agents.

**Response** `200`:
```json
[
  {
    "name": "bug_hunter",
    "running": true,
    "pid": 12345,
    "model": "xiaomi-mimo-2.5",
    "heartbeat_age_seconds": 12.5,
    "effective_timeout": 450,
    "consecutive_errors": 0,
    "tier": 1,
    "risk": "low"
  }
]
```

### GET /bots/{name}/logs

Returns recent log output for a specific agent.

**Query params**: `lines` (default: 200)

**Response** `200`:
```json
{"tail": "...log output..."}
```

### POST /bots/{name}/restart

Kills and restarts a specific agent.

**Response** `200`: `{"status": "restarted", "name": "..."}`

### POST /bots/{name}/pause

Pauses an agent (prevents respawn until resumed).

**Response** `200`: `{"status": "paused", "name": "..."}`

### POST /bots/{name}/resume

Resumes a paused agent.

**Response** `200`: `{"status": "resumed", "name": "..."}`

### POST /control/drain

Sets global drain flag. No new agents will spawn until cleared.

**Response** `200`: `{"status": "draining"}`

### POST /control/clear-drain

Clears drain flag. Agents may resume spawning.

**Response** `200`: `{"status": "drain_cleared"}`

### POST /control/update

Triggers safe update sequence: drain → apply → verify → restart.

**Response** `200`: `{"status": "updating"}`

## Ticket Engine API

### Create Ticket

```python
from codebot.ticket_engine import create_ticket, TicketClass, Severity, RiskLevel

ticket = create_ticket(
    title="Fix auth bypass in router",
    ticket_class=TicketClass.SECURITY,
    severity=Severity.CRITICAL,
    source="security_auditor",
    evidence="Line 42: missing auth check",
    problem_statement="Router bypasses auth for admin endpoints",
    desired_state="All admin routes require valid bearer token",
    acceptance_criteria=["pytest passes", "auth check present on all admin routes"],
    risk=RiskLevel.HIGH,
    affected_modules=["lib/router.py"],
    dependencies=[],
)
```

### Ticket Store Operations

```python
from codebot.ticket_engine import TicketStore, TicketState
from pathlib import Path

store = TicketStore(Path("state/tickets.json"))

# Add ticket (auto-deduplicates via SHA-256 evidence hash)
store.add(ticket)

# Transition state (enforces valid transitions)
store.transition(ticket.id, TicketState.VALIDATING)
store.transition(ticket.id, TicketState.TRIAGED)
store.transition(ticket.id, TicketState.READY)

# Query
store.get(ticket.id)                    # Single ticket
store.list_by_state(TicketState.READY)  # Filter by state
store.list_ready()                      # READY tickets sorted by severity
store.count()                           # Total count
store.summary()                         # {"DISCOVERED": 3, "READY": 5, ...}
```

### Valid State Transitions

| From | Allowed To |
|------|-----------|
| DISCOVERED | VALIDATING, REJECTED, DUPLICATE |
| VALIDATING | TRIAGED, REJECTED, DUPLICATE |
| TRIAGED | READY, DEFERRED, REJECTED, HUMAN_REQUIRED |
| READY | PLANNING, IMPLEMENTING, DEFERRED |
| PLANNING | IMPLEMENTING, READY, BLOCKED |
| IMPLEMENTING | REVIEWING, REWORK, BLOCKED, HUMAN_REQUIRED |
| REVIEWING | VERIFYING, REWORK, HUMAN_REQUIRED |
| VERIFYING | COMPLETE, REWORK |
| REWORK | IMPLEMENTING, PLANNING, REJECTED |
| BLOCKED | READY, PLANNING, IMPLEMENTING, DEFERRED |
| DEFERRED | READY, TRIAGED |
| HUMAN_REQUIRED | READY, IMPLEMENTING, REJECTED |
| COMPLETE | (terminal) |
| REJECTED | (terminal) |
| DUPLICATE | (terminal) |

## Dependency Graph API

```python
from codebot.dependency_graph import DependencyGraph

graph = DependencyGraph()
graph.add_dependency("CB-2", "CB-1")  # CB-2 depends on CB-1
graph.add_dependency("CB-3", "CB-2")

order = graph.topological_sort()       # ["CB-1", "CB-2", "CB-3"]
ready = graph.ready_tickets(completed={"CB-1"}, all_tickets={"CB-1", "CB-2", "CB-3"})
# ["CB-2"]

graph.transitive_dependencies("CB-3")  # frozenset({"CB-1", "CB-2"})
```

Cycles raise `CyclicDependencyError` and the offending edge is rolled back.

## Risk Classifier API

```python
from codebot.risk_classifier import classify_risk, autonomy_level_for_risk

score, reason = classify_risk(
    ticket_class="security",
    severity="critical",
    affected_modules=["lib/auth.py", "lib/router.py"],
    security_impact="auth bypass",
)
# score=85, reason="critical: severity=critical; class=security; ..."

allowed, why = autonomy_level_for_risk(score, project_autonomy_level=2)
# allowed=False, why="human_required: risk score >= 70"
```

Constitution-protected categories always score 100 regardless of other inputs.

## Quality Gate API

```python
from codebot.quality_gate import load_policy, run_quality_gate, QualityGatePolicy
from pathlib import Path

policy = load_policy(Path(".codebot/quality_gates.yaml"))
passed, evaluations = run_quality_gate(
    policy=policy,
    workspace=Path("/path/to/project"),
    ticket_class="feature",
    changed_files=["lib/router.py"],
    test_dirs="tests/",
)
```

## Role Prompt Assembly API

```python
from codebot.role_prompt import assemble_prompt, resolve_role_name, format_ticket_context

# Map legacy bot name to CodeBot role
role = resolve_role_name("issues")  # "bug_hunter"

# Assemble full prompt with project context
prompt = assemble_prompt(
    role_name="bug_hunter",
    adapter=monitor_adapter,
    ticket_context=format_ticket_context(ticket),
)
```

## Migration Script

One-time migration from legacy QUEUE.md to TicketStore:

```bash
# Dry run
python3 -m codebot.migrate_queue --project ~/Work --queue bots/QUEUE.md --dry-run

# Execute
python3 -m codebot.migrate_queue --project ~/Work --queue bots/QUEUE.md
```

Parses `### [ID] Title` patterns, extracts severity/class/status fields, creates normalized tickets, transitions to READY state.
