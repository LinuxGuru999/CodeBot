#!/usr/bin/env python3
"""Implementation planner for non-trivial CodeBot tickets.

Purpose
-------
Generates structured implementation plans for tickets in PLANNING state,
detailing affected components, required tests, security considerations,
backwards compatibility, data migrations, rollback path, documentation
updates, dependency ordering, and expected artifacts.

Why
---
CODEBOT-ROADMAP.md §2.G requires planning depth to scale with risk.
Without a plan, implementers make ad-hoc decisions about scope, miss
security implications, skip test requirements, and produce changes that
break downstream consumers. A deterministic plan template ensures every
non-trivial ticket receives consistent treatment regardless of which
agent executes it.

Invariants
----------
- stdlib-only (json, time, dataclasses, enum)
- Plans are immutable once generated; regeneration produces a new version
- Plan generation never modifies source code or repository state
- Risk level drives planning depth: low=summary, medium=standard, high/critical=full
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class PlanDepth(str, Enum):
    SUMMARY = "summary"
    STANDARD = "standard"
    FULL = "full"


RISK_TO_DEPTH: dict[str, PlanDepth] = {
    "low": PlanDepth.SUMMARY,
    "medium": PlanDepth.STANDARD,
    "high": PlanDepth.FULL,
    "critical": PlanDepth.FULL,
}


@dataclass(frozen=True)
class ImplementationPlan:
    ticket_id: str
    depth: PlanDepth
    affected_components: list[str]
    architectural_implications: list[str]
    interfaces_changed: list[str]
    tests_required: list[str]
    security_considerations: list[str]
    backwards_compatibility: str
    data_migrations: list[str]
    rollback_path: str
    documentation_updates: list[str]
    dependency_ordering: list[str]
    expected_artifacts: list[str]
    estimated_effort_tokens: int
    generated_at: float
    version: int = 1

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["depth"] = self.depth.value
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ImplementationPlan:
        data = dict(data)
        data["depth"] = PlanDepth(data["depth"])
        return cls(**data)


def determine_plan_depth(risk: str) -> PlanDepth:
    return RISK_TO_DEPTH.get(risk.lower(), PlanDepth.STANDARD)


def generate_plan(
    ticket_id: str,
    risk: str,
    affected_modules: list[str],
    dependencies: list[str],
    acceptance_criteria: list[str],
    ticket_class: str = "",
    security_impact: str = "none",
    migration_impact: str = "none",
    rollback_strategy: str = "revert commit",
    documentation_requirements: list[str] | None = None,
    required_tests: list[str] | None = None,
) -> ImplementationPlan:
    depth = determine_plan_depth(risk)
    components = list(affected_modules) if affected_modules else ["unknown"]
    arch_implications: list[str] = []
    interfaces: list[str] = []
    security: list[str] = []
    migrations: list[str] = []
    docs = list(documentation_requirements or [])
    tests = list(required_tests or [])
    compat = "No breaking changes expected"
    if depth in (PlanDepth.STANDARD, PlanDepth.FULL):
        if security_impact and security_impact != "none":
            security.append(f"Security impact declared: {security_impact}")
            security.append("Security review required before VERIFYING")
        if migration_impact and migration_impact != "none":
            migrations.append(f"Data migration declared: {migration_impact}")
            migrations.append("Migration test required")
            migrations.append("Rollback test required")
        if ticket_class in ("security", "architecture"):
            arch_implications.append(f"{ticket_class}-class change: review for coupling and boundary violations")
        if not tests and acceptance_criteria:
            tests = [f"test for: {criterion}" for criterion in acceptance_criteria[:5]]
        if not docs:
            docs = ["Update module docstring if public interface changed"]
    if depth == PlanDepth.FULL:
        arch_implications.append("Full architecture review required")
        interfaces.append("Document all changed function signatures")
        security.append("Adversarial review required")
        security.append("Fuzz testing recommended for input boundaries")
        if not rollback_strategy or rollback_strategy == "revert commit":
            rollback_strategy = "Revert commit + verify no partial state corruption via integration test"
        compat = "Verify no callers depend on removed/changed behavior"
    elif depth == PlanDepth.SUMMARY:
        if not tests and acceptance_criteria:
            tests = [f"verify: {acceptance_criteria[0]}"]
    effort_map = {PlanDepth.SUMMARY: 5000, PlanDepth.STANDARD: 20000, PlanDepth.FULL: 50000}
    return ImplementationPlan(
        ticket_id=ticket_id,
        depth=depth,
        affected_components=components,
        architectural_implications=arch_implications,
        interfaces_changed=interfaces,
        tests_required=tests,
        security_considerations=security,
        backwards_compatibility=compat,
        data_migrations=migrations,
        rollback_path=rollback_strategy,
        documentation_updates=docs,
        dependency_ordering=list(dependencies),
        expected_artifacts=[f"Modified files in: {', '.join(components)}"],
        estimated_effort_tokens=effort_map[depth],
        generated_at=time.time(),
    )


class PlanStore:
    def __init__(self, state_dir: Any) -> None:
        from pathlib import Path
        self._dir = Path(state_dir) / "plans"
        self._dir.mkdir(parents=True, exist_ok=True)

    def _path(self, ticket_id: str) -> Any:
        from pathlib import Path
        safe = ticket_id.replace("/", "_").replace("\\", "_")
        return self._dir / f"{safe}.json"

    def save(self, plan: ImplementationPlan) -> None:
        import os
        p = self._path(plan.ticket_id)
        tmp = p.with_suffix(".tmp")
        tmp.write_text(plan.to_json(), encoding="utf-8")
        os.replace(str(tmp), str(p))

    def load(self, ticket_id: str) -> ImplementationPlan | None:
        p = self._path(ticket_id)
        if not p.exists():
            return None
        try:
            return ImplementationPlan.from_dict(json.loads(p.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, KeyError, ValueError):
            return None

    def exists(self, ticket_id: str) -> bool:
        return self._path(ticket_id).exists()
