#!/usr/bin/env python3
"""Implementation Planner — Manages implementation plans for tickets.

Purpose
-------
Provides a storage mechanism for implementation plans associated with tickets.
Plans are required for medium+ risk tickets before they can enter the IMPLEMENTING state.
Also generates structured implementation plans for tickets in PLANNING state,
detailing affected components, required tests, security considerations,
backwards compatibility, data migrations, rollback path, documentation
updates, dependency ordering, and expected artifacts.

Why
---
To enforce the "Planning Prerequisite" invariant defined in the roadmap.
Agents generate plans during the PLANNING state, which are then validated
before allowing transition to IMPLEMENTING.  CODEBOT-ROADMAP.md §2.G
requires planning depth to scale with risk. Without a plan, implementers
make ad-hoc decisions about scope, miss security implications, skip test
requirements, and produce changes that break downstream consumers.

Invariants
----------
- Plans are stored as JSON files in .codebot/state/plans/
- Plan filename matches ticket ID: <ticket_id>.plan.json (also supports legacy <ticket_id>.json)
- Atomic writes via tmp+replace
- stdlib-only (json, time, dataclasses, enum)
- Plans are immutable once generated; regeneration produces a new version
- Plan generation never modifies source code or repository state
- Risk level drives planning depth: low=summary, medium=standard, high/critical=full
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field, asdict
from enum import Enum
from pathlib import Path
from typing import Any, Optional


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
    """Persistent store for implementation plans.

    Supports both legacy and current file naming conventions and both
    save signatures for backward compatibility:
      - save(plan: ImplementationPlan)
      - save(ticket_id: str, plan: dict[str, Any] | ImplementationPlan)
    """

    def __init__(self, state_dir: Path | str | Any) -> None:
        self._dir = Path(state_dir) / "plans"
        self._dir.mkdir(parents=True, exist_ok=True)
        # Legacy compat: also ensure .plan.json naming works via same dir
        self._plans_dir = self._dir

    def _safe(self, ticket_id: str) -> str:
        return ticket_id.replace("/", "_").replace("\\", "_")

    def _path_candidates(self, ticket_id: str) -> list[Path]:
        safe = self._safe(ticket_id)
        # Prefer new naming <ticket_id>.plan.json, fallback to legacy <ticket_id>.json
        return [
            self._dir / f"{safe}.plan.json",
            self._dir / f"{safe}.json",
        ]

    def _primary_path(self, ticket_id: str) -> Path:
        # New naming is canonical
        return self._dir / f"{self._safe(ticket_id)}.plan.json"

    def _legacy_path(self, ticket_id: str) -> Path:
        return self._dir / f"{self._safe(ticket_id)}.json"

    def _plan_path(self, ticket_id: str) -> Path:
        """Return primary path for new code calling internally."""
        return self._primary_path(ticket_id)

    def _path(self, ticket_id: str) -> Path:
        """Legacy alias for _plan_path / _primary_path."""
        return self._primary_path(ticket_id)

    def save(self, ticket_id_or_plan: str | ImplementationPlan | dict[str, Any], plan: dict[str, Any] | ImplementationPlan | None = None) -> None:
        """Save an implementation plan.

        Supported signatures:
          save(plan: ImplementationPlan)
          save(plan_dict: dict)
          save(ticket_id: str, plan: dict)
          save(ticket_id: str, plan: ImplementationPlan)
        """
        # Signature: save(plan)
        if plan is None:
            # Single arg
            if isinstance(ticket_id_or_plan, ImplementationPlan):
                impl_plan = ticket_id_or_plan
                ticket_id = impl_plan.ticket_id
                payload_dict = json.loads(impl_plan.to_json())
                # Store directly as ImplementationPlan json (readable by load)
                # Also support wrapper style? Use direct for simplicity but ensure load handles both.
                # We'll write the ImplementationPlan dict directly (original behavior)
                p = self._primary_path(ticket_id)
                # Also keep legacy path in sync for callers expecting legacy
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload_dict, indent=2), encoding="utf-8")
                os.replace(str(tmp), str(p))
                # Also write legacy file for compatibility if someone looks for .json
                legacy = self._legacy_path(ticket_id)
                # Keep legacy in sync (optional)
                try:
                    tmp2 = legacy.with_suffix(".tmp")
                    tmp2.write_text(json.dumps(payload_dict, indent=2), encoding="utf-8")
                    os.replace(str(tmp2), str(legacy))
                except OSError:
                    pass
                return
            elif isinstance(ticket_id_or_plan, dict):
                # Dict assumed to be ImplementationPlan dict or wrapper
                d = ticket_id_or_plan
                # Try to extract ticket_id
                ticket_id = d.get("ticket_id") or d.get("plan", {}).get("ticket_id") or "unknown"
                if "ticket_id" in d and "depth" in d:
                    # It's an ImplementationPlan dict
                    p = self._primary_path(ticket_id)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
                    os.replace(str(tmp), str(p))
                    return
                elif "plan" in d and isinstance(d["plan"], dict):
                    # wrapper style {"ticket_id":..., "plan": {...}}
                    plan_inner = d["plan"]
                    ticket_id = d.get("ticket_id", plan_inner.get("ticket_id", ticket_id))
                    p = self._primary_path(ticket_id)
                    tmp = p.with_suffix(".tmp")
                    # Store inner plan as ImplementationPlan-like if possible, else wrapper
                    # For load compatibility, store inner if it looks like ImplementationPlan
                    if "depth" in plan_inner:
                        tmp.write_text(json.dumps(plan_inner, indent=2), encoding="utf-8")
                    else:
                        tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
                    os.replace(str(tmp), str(p))
                    return
                else:
                    # Fallback: store dict as is
                    ticket_id = d.get("ticket_id", "unknown")
                    p = self._primary_path(ticket_id)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps(d, indent=2), encoding="utf-8")
                    os.replace(str(tmp), str(p))
                    return
            else:
                raise TypeError(f"unsupported save signature: {type(ticket_id_or_plan)}")
        else:
            # Two-arg form: save(ticket_id, plan)
            ticket_id = str(ticket_id_or_plan)
            plan_data = plan
            if isinstance(plan_data, ImplementationPlan):
                payload_dict = json.loads(plan_data.to_json())
                p = self._primary_path(ticket_id)
                tmp = p.with_suffix(".tmp")
                tmp.write_text(json.dumps(payload_dict, indent=2), encoding="utf-8")
                os.replace(str(tmp), str(p))
                # sync legacy
                try:
                    legacy = self._legacy_path(ticket_id)
                    tmp2 = legacy.with_suffix(".tmp")
                    tmp2.write_text(json.dumps(payload_dict, indent=2), encoding="utf-8")
                    os.replace(str(tmp2), str(legacy))
                except OSError:
                    pass
                return
            elif isinstance(plan_data, dict):
                # Could be ImplementationPlan dict or arbitrary plan dict
                # New code path expects: payload = {"ticket_id": ticket_id, "plan": plan_dict}
                # But for load compatibility we store either wrapper or direct
                # If dict has "depth", treat as ImplementationPlan dict directly
                if "depth" in plan_data and "ticket_id" in plan_data:
                    p = self._primary_path(ticket_id)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps(plan_data, indent=2), encoding="utf-8")
                    os.replace(str(tmp), str(p))
                    # sync legacy
                    try:
                        legacy = self._legacy_path(ticket_id)
                        tmp2 = legacy.with_suffix(".tmp")
                        tmp2.write_text(json.dumps(plan_data, indent=2), encoding="utf-8")
                        os.replace(str(tmp2), str(legacy))
                    except OSError:
                        pass
                    return
                else:
                    # Wrap as new API did: {"ticket_id": ticket_id, "created_at":..., "plan": plan_data}
                    payload = {
                        "ticket_id": ticket_id,
                        "created_at": time.time(),
                        "updated_at": time.time(),
                        "plan": plan_data,
                    }
                    # For load to return ImplementationPlan, we need to handle wrapper.
                    # Write wrapper to primary path (new style)
                    p = self._primary_path(ticket_id)
                    tmp = p.with_suffix(".tmp")
                    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
                    os.replace(str(tmp), str(p))
                    return
            else:
                raise TypeError(f"unsupported plan type: {type(plan_data)}")

    def load(self, ticket_id: str) -> Optional[ImplementationPlan | dict[str, Any]]:
        """Load an implementation plan for a ticket.

        Returns ImplementationPlan if stored data can be parsed as such,
        otherwise returns raw dict for backward compatibility.
        Tries both naming conventions and both wrapper styles.
        """
        for p in self._path_candidates(ticket_id):
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    # Case 1: wrapper {"plan": {...}}
                    if isinstance(raw, dict) and "plan" in raw and isinstance(raw["plan"], dict):
                        inner = raw["plan"]
                        # inner might be ImplementationPlan dict or arbitrary dict
                        if "depth" in inner and "ticket_id" in inner:
                            try:
                                return ImplementationPlan.from_dict(inner)
                            except Exception:
                                return inner
                        # If inner is arbitrary plan dict, return ImplementationPlan if possible?
                        # For two-arg dict save, inner may be arbitrary dict without depth; return inner dict
                        return inner
                    # Case 2: direct ImplementationPlan dict
                    if isinstance(raw, dict) and "depth" in raw and "ticket_id" in raw:
                        try:
                            return ImplementationPlan.from_dict(raw)
                        except Exception:
                            return raw
                    # Case 3: wrapper with top-level ticket_id but no plan key? fallback
                    return raw
                except (json.JSONDecodeError, OSError, KeyError, ValueError):
                    continue
        return None

    def exists(self, ticket_id: str) -> bool:
        """Check if a plan exists for a ticket (either naming convention)."""
        return any(p.exists() for p in self._path_candidates(ticket_id))

    def delete(self, ticket_id: str) -> None:
        """Delete an implementation plan for a ticket."""
        for p in self._path_candidates(ticket_id):
            if p.exists():
                try:
                    p.unlink()
                except OSError:
                    pass
