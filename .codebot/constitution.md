# Project Constitution

Human-controlled architectural and engineering invariants for CodeBot-managed projects.
Agents may propose constitution changes but must never silently weaken requirements to satisfy a task.

Last updated: 2026-09-18
Schema version: 1.0

---

## 1. Product Purpose

This project is a self-hosted fleet monitoring, security, and management platform with AI-driven continuous improvement. It must remain deployable as a single-process manager with per-machine agents. The autonomous improvement system (CodeBot/BotNet) is a first-class component, not an afterthought.

Any change that fundamentally alters this purpose requires explicit human approval.

---

## 2. Security Boundaries

These are non-negotiable. No agent, ticket, or automated process may weaken them:

- **Stdlib-only policy**: No new third-party dependencies without a formal ADR demonstrating stdlib + lib-common cannot solve the problem.
- **SSRF protection**: All outbound requests must validate against blocklists. No exceptions.
- **Bearer token handling**: Tokens via Authorization header only. Never in query strings, logs, or error responses.
- **Input validation**: All user-supplied data validated at trust boundaries. No implicit trust.
- **TLS verification**: Enabled by default. Disabling requires explicit justification and human approval.
- **Secrets isolation**: No secrets in code, logs, or error messages. Token files must be 0600.
- **Rate limiting**: Auth endpoints must have rate limiting. Cannot be removed or weakened.
- **Security headers**: nosniff, DENY frame, no-referrer on all responses.

**Rule**: CodeBot must never silently lower a security requirement, disable a security test, remove validation, or weaken authorization merely to make a ticket pass.

---

## 3. Minimum Testing Standards

- All new public functions must have corresponding tests.
- `pytest -q` must pass before any ticket reaches COMPLETE state.
- No test deletion to achieve passing status. If a test fails, fix the code or formally deprecate the test with justification.
- Integration tests required for any cross-module change.
- Security-sensitive changes require adversarial test cases.

---

## 4. Architectural Invariants

- **Single-process manager**: Manager runs as one process. No multiprocessing shared state.
- **Thin router, fat service**: Router parses and dispatches; services contain business logic.
- **AuthZ seam**: Every route goes through `authorize()` before business logic. No bypasses.
- **Shared kernel**: `lib-common/` is canonical. Vendored copies in Monitor-Manager-Python and Monitor-Client-Python are never edited directly — edit canonical, re-vendor.
- **Bounded I/O**: All reads have timeout + size cap. No unbounded `resp.read()`.
- **Fail-open monitoring**: Monitoring/heartbeat paths catch exceptions, return partial data, never crash.
- **Atomic writes**: State files use temp + rename pattern. No partial writes.
- **RLock discipline**: Single RLock per Store instance. Lock ordering: shard lock before master lock. Never nest shard locks.

---

## 5. Supported Platforms

- **Python**: 3.11+ (currently 3.14.7)
- **OS**: Linux primary, macOS/Windows secondary (agent-side)
- **Deployment**: Self-hosted single machine, Docker, Fly.io
- **No cloud-provider lock-in**: Must run without AWS/GCP/Azure-specific services

---

## 6. Compatibility Requirements

- API contract (`API_CONTRACT.md`) is byte-identical across manager and client repos.
- Breaking API changes require versioning and migration path.
- Vendored `lib-common` copies must stay synchronized.
- Static file cache busting (`?v=N`) must be bumped together across all HTML files.

---

## 7. Dependency Policies

- **Default**: stdlib-only. No new dependencies.
- **Exception process**: File ADR in `docs/adr/NNNN-<slug>.md` with Context, Decision, Consequences, Alternatives considered, Why stdlib attempt failed.
- **Allowed existing**: `argon2-cffi` (manager password hashing), `pytest` + plugins (dev only).
- **Supply chain**: All dependencies pinned. No unpinned version ranges.

---

## 8. Human Approval Requirements

The following categories ALWAYS require human approval before implementation:

| Category | Examples |
|----------|----------|
| Authentication architecture | MFA flow changes, session model, token lifecycle |
| Authorization boundaries | RBAC model changes, permission definitions, scope rules |
| Cryptography | Algorithm selection, key management, hashing parameters |
| Destructive migrations | Data deletion, schema drops, irreversible transforms |
| Secrets management | Storage location, rotation policy, access patterns |
| Security policy relaxation | Weakening any rule in Section 2 |
| Constitution changes | Modifying this document |
| Major architecture changes | Process model, deployment topology, data store replacement |

Autonomous work (no human gate required):
- Documentation corrections
- Test additions
- Lint/formatting fixes
- Safe refactors (no behavior change)
- Known bug fixes with existing tests
- Low-risk UI corrections

---

## 9. Destructive Operation Policies

- No `DROP TABLE`, `DELETE FROM` without WHERE, or bulk destructive operations without human approval.
- File deletion requires confirmation the file is truly dead code (not referenced anywhere).
- Git force-push prohibited except in isolated feature branches with explicit approval.
- Rollback strategy must exist before any destructive operation.

---

## 10. Data Ownership Rules

- Agent-collected data belongs to the tenant (company) that registered the agent.
- Cross-tenant data access is forbidden without explicit authorization.
- Global actor scope uses strict equality (`("*",)`), not membership (`"*" in list`).
- Audit trails are immutable append-only records.
- GDPR right-to-erasure must be supportable (TIER 6.5).

---

## 11. CodeBot Self-Governance

- CodeBot may improve itself only under stronger controls than normal project work.
- Self-improvement changes require: isolated branch, extra reviewers, regression suite pass, security review, architecture review.
- CodeBot must never silently remove or weaken the controls governing its own behavior.
- Bootstrap recovery must always be possible (can revert to previous working state).

---

## 12. Amendment Process

Constitution changes require:
1. Proposal ticket with justification
2. Human review and explicit approval
3. Updated constitution committed with reference to approval ticket
4. All affected bots/agents notified of new constraints

Agents may propose amendments. Agents may NOT enact them autonomously.
