# CodeBot API Contract

Last updated: 2026-09-18

This is the authoritative API contract for CodeBot's public interfaces. Full endpoint documentation lives in `docs/API.md`. This contract defines what is STABLE vs what may change.

---

## Stability Guarantees

| Interface | Stability | Contract |
|-----------|-----------|----------|
| CLI commands (`serve`, `status`, `drain`, `clear-drain`, `validate`, `stop-all`, `start`) | **STABLE** | Will not be removed without major version bump |
| Control Server HTTP API (port 8081) | **STABLE** | Endpoints documented below; breaking changes require new API version |
| `ProjectAdapter` ABC (13 methods) | **STABLE** | All adapters implement this; changes require all adapters updated |
| Ticket states (14-state machine) | **STABLE** | State names and transitions enforced by `ticket_engine.py` |
| `.codebot/project.yaml` schema | **STABLE** | Schema version tracked in `schema_version` field |
| `.codebot/quality_gates.yaml` schema | **STABLE** | `required` + `conditional` structure; new conditions may be added |
| `.codebot/capability_matrix.yaml` schema | **STABLE** | Status vocabulary: EXISTS, PARTIALLY_EXISTS, PLANNED, MISSING |
| Role prompt templates (`codebot/roles/*.md`) | **EVOLVING** | Content may change; template structure (sections) stable |
| Environment variables (`CODEBOT_*`) | **EVOLVING** | New variables may be added; existing ones documented in docs/API.md |
| Agent mission file format | **INTERNAL** | Not a public contract; may change between versions |

---

## Control Server Endpoints (Stable)

Base URL: `http://localhost:8081`
Auth: `Authorization: Bearer <CONTROL_TOKEN>`

| Method | Path | Purpose | Auth Required |
|--------|------|---------|---------------|
| GET | `/health` | Orchestrator health status | No |
| GET | `/bots` | All agent statuses | Yes |
| GET | `/bot/{name}` | Single agent status | Yes |
| POST | `/bot/{name}/restart` | Restart a specific agent | Yes |
| POST | `/bot/{name}/stop` | Stop a specific agent | Yes |
| POST | `/drain` | Set drain flag | Yes |
| POST | `/clear-drain` | Clear drain flag | Yes |
| GET | `/tickets` | Ticket queue summary | Yes |
| GET | `/logs/{name}` | Recent agent logs | Yes |

Rate limiting: 60 requests/minute per token. Excess returns 429 with `Retry-After`.

---

## CLI Interface (Stable)

```bash
python3 -m codebot <command> [--project <path>] [args...]
```

| Command | Args | Exit Codes |
|---------|------|------------|
| `serve` | — | 0=clean shutdown, 1=error |
| `status` | — | 0=running, 1=stopped |
| `drain` | — | 0=drain set |
| `clear-drain` | — | 0=drain cleared |
| `validate` | — | 0=valid, 1=validation errors |
| `stop-all` | — | 0=all stopped |
| `start` | `[agents...]` | 0=started |

---

## ProjectAdapter Interface (Stable)

Every managed project implements this ABC. Core resolves everything through it.

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

---

## Ticket State Machine (Stable)

```
DISCOVERED → VALIDATING → TRIAGED → READY → PLANNING → IMPLEMENTING
    → REVIEWING → VERIFYING → COMPLETE

Terminal/exception states: REJECTED, DUPLICATE, DEFERRED, BLOCKED,
HUMAN_REQUIRED (deprecated — see §14 QA-recommendation model), REWORK
```

Valid transitions enforced by `TRANSITIONS` dict in `ticket_engine.py`. Invalid transitions raise `ValueError`.

---

## Escalation Model (Stable since 2026-09-18)

**No human escalation.** All sensitive changes surface as QA-stage recommendations:

- Recommendations generated as tickets (`type=recommendation`)
- Resolved through adversarial review pipeline
- Zero human intervention required
- `escalation_model: "qa_recommendations"` in project.yaml

---

## Versioning

- API version: 1.0 (embedded in Control Server responses)
- Breaking changes require: new version prefix, deprecation notice, 2-release migration window
- Schema version tracked in `.codebot/project.yaml` (`schema_version`)
