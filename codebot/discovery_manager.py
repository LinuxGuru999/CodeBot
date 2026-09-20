#!/usr/bin/env python3
"""Discovery manager for the adaptive scheduler.

Purpose
-------
Manages discovery role allocation, cooldown tracking, yield statistics,
diversity enforcement, and project saturation detection. When the executable
backlog is insufficient to occupy all slots, this module determines which
discovery roles to spawn and how many of each.

Why
---
Spec §8-12 require dynamic discovery capacity that adapts to backlog depth,
role diversity (§9), cooldown-based deduplication (§10), yield tracking (§11),
and saturation detection (§39). Without centralized management, 30 idle slots
would all become identical bug_hunters scanning the same unchanged files.

Invariants
----------
- stdlib-only (dataclasses, time, json, pathlib)
- Discovery agents produce structured candidates; they NEVER modify code (§8)
- Cooldowns are tracked per (role, scope) pair keyed by commit SHA
- Yield stats decay over time so stale data doesn't permanently suppress a role
- Diversity allocation uses weighted round-robin, not random
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


# All discovery roles from role_registry.py, ordered by default priority
DISCOVERY_ROLES: tuple[str, ...] = (
    "bug_hunter",
    "security_auditor",
    "test_gap_auditor",
    "architecture_auditor",
    "performance_auditor",
    "documentation_auditor",
    "dependency_auditor",
    "ux_auditor",
)

# Roles that use cheap models (§28)
CHEAP_DISCOVERY_ROLES: frozenset[str] = frozenset()

# Roles that need premium models
PREMIUM_DISCOVERY_ROLES: frozenset[str] = frozenset({
    "security_auditor",
    "architecture_auditor",
})


@dataclass(frozen=True)
class DiscoveryCooldown:
    """Tracks when a specific audit scope was last scanned (§10)."""
    role: str
    scope: str
    commit_sha: str
    completed_at: float
    findings_count: int
    duplicate_count: int

    def is_expired(self, now: float, cooldown_seconds: int) -> bool:
        return (now - self.completed_at) >= cooldown_seconds

    def is_stale_commit(self, current_sha: str) -> bool:
        return self.commit_sha != current_sha


@dataclass
class RoleYieldStats:
    """Productivity tracking for a single discovery role (§11)."""
    role: str
    total_scans: int = 0
    validated_tickets: int = 0
    duplicates: int = 0
    rejected: int = 0
    total_cost_tokens: int = 0
    last_scan_at: float = 0.0

    @property
    def yield_rate(self) -> float:
        if self.total_scans == 0:
            return 0.0
        return self.validated_tickets / self.total_scans

    @property
    def duplicate_rate(self) -> float:
        if self.total_scans == 0:
            return 0.0
        return self.duplicates / self.total_scans

    @property
    def cost_per_ticket(self) -> float:
        if self.validated_tickets == 0:
            return float("inf")
        return self.total_cost_tokens / self.validated_tickets

    def record_scan(
        self,
        validated: int,
        duplicates: int,
        rejected: int,
        cost_tokens: int,
        now: float | None = None,
    ) -> None:
        self.total_scans += 1
        self.validated_tickets += validated
        self.duplicates += duplicates
        self.rejected += rejected
        self.total_cost_tokens += cost_tokens
        self.last_scan_at = now or time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "total_scans": self.total_scans,
            "validated_tickets": self.validated_tickets,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "yield_rate": round(self.yield_rate, 4),
            "duplicate_rate": round(self.duplicate_rate, 4),
            "cost_per_ticket": round(self.cost_per_ticket, 1),
            "last_scan_at": self.last_scan_at,
        }


@dataclass(frozen=True)
class DiscoveryAllocation:
    """Output of the discovery planner: how many of each role to run."""
    allocations: dict[str, int]
    total_slots: int
    reason: str
    is_saturated: bool = False

    def to_list(self) -> list[dict[str, Any]]:
        result = []
        for role, count in self.allocations.items():
            if count > 0:
                result.append({
                    "role": role,
                    "count": count,
                    "cost_class": "cheap" if role in CHEAP_DISCOVERY_ROLES
                                  else "premium" if role in PREMIUM_DISCOVERY_ROLES
                                  else "standard",
                })
        return result


class DiscoveryManager:
    """Centralized discovery lifecycle management.

    Tracks cooldowns, yield statistics, and computes diverse allocations.
    State is persisted to disk as JSON for cross-restart continuity.
    
    Uses an O(1) index (_cooldown_index) mapping (role, scope) -> latest index
    in _cooldowns for efficient cooldown lookups.
    """

    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir
        self._cooldowns: list[DiscoveryCooldown] = []
        self._cooldown_index: dict[tuple[str, str], int] = {}  # (role, scope) -> index in _cooldowns
        self._yields: dict[str, RoleYieldStats] = {}
        self._load()

    def _state_path(self) -> Path | None:
        if self._state_dir is None:
            return None
        return self._state_dir / "discovery_state.json"

    def _load(self) -> None:
        path = self._state_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            for entry in data.get("cooldowns", []):
                idx = len(self._cooldowns)
                cd = DiscoveryCooldown(**entry)
                self._cooldowns.append(cd)
                # Rebuild index: latest entry for each (role, scope)
                self._cooldown_index[(cd.role, cd.scope)] = idx
            for role, stats in data.get("yields", {}).items():
                ys = RoleYieldStats(role=role)
                ys.total_scans = stats.get("total_scans", 0)
                ys.validated_tickets = stats.get("validated_tickets", 0)
                ys.duplicates = stats.get("duplicates", 0)
                ys.rejected = stats.get("rejected", 0)
                ys.total_cost_tokens = stats.get("total_cost_tokens", 0)
                ys.last_scan_at = stats.get("last_scan_at", 0.0)
                self._yields[role] = ys
        except Exception:
            pass

    def save(self) -> None:
        path = self._state_path()
        if path is None:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        # Only save the last 500 cooldowns, but rebuild index for those saved
        recent_cooldowns = self._cooldowns[-500:]
        data = {
            "cooldowns": [
                {
                    "role": c.role,
                    "scope": c.scope,
                    "commit_sha": c.commit_sha,
                    "completed_at": c.completed_at,
                    "findings_count": c.findings_count,
                    "duplicate_count": c.duplicate_count,
                }
                for c in recent_cooldowns
            ],
            "yields": {r: s.to_dict() for r, s in self._yields.items()},
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(path)

    def record_completion(
        self,
        role: str,
        scope: str,
        commit_sha: str,
        findings: int,
        duplicates: int,
        rejected: int,
        cost_tokens: int,
        now: float | None = None,
    ) -> None:
        """Record a completed discovery scan (§10, §11).
        
        Updates the O(1) cooldown index immediately after appending to ensure
        new cooldowns are visible to is_on_cooldown without delay.
        """
        now = now or time.time()
        idx = len(self._cooldowns)
        cd = DiscoveryCooldown(
            role=role,
            scope=scope,
            commit_sha=commit_sha,
            completed_at=now,
            findings_count=findings,
            duplicate_count=duplicates,
        )
        self._cooldowns.append(cd)
        # Update index: this is now the latest entry for (role, scope)
        self._cooldown_index[(role, scope)] = idx
        if role not in self._yields:
            self._yields[role] = RoleYieldStats(role=role)
        self._yields[role].record_scan(findings, duplicates, rejected, cost_tokens, now)
        # Prune old cooldowns (keep last 30 days) - must rebuild index after pruning
        cutoff = now - 30 * 86400
        old_cooldowns = self._cooldowns
        self._cooldowns = []
        self._cooldown_index.clear()
        for i, c in enumerate(old_cooldowns):
            if c.completed_at > cutoff:
                self._cooldowns.append(c)
                self._cooldown_index[(c.role, c.scope)] = i

    def is_on_cooldown(
        self,
        role: str,
        scope: str,
        current_commit_sha: str,
        cooldown_seconds: int,
        now: float | None = None,
    ) -> bool:
        """Check if a specific (role, scope) pair should be skipped (§10).

        Returns True if the same scope was scanned recently AND the
        commit hasn't changed since then.
        
        Uses O(1) index lookup for the latest (role, scope) entry.
        """
        now = now or time.time()
        idx = self._cooldown_index.get((role, scope))
        if idx is None:
            return False  # Never scanned
        cd = self._cooldowns[idx]
        if cd.is_stale_commit(current_commit_sha):
            return False  # Code changed, rescan allowed
        if not cd.is_expired(now, cooldown_seconds):
            return True  # Still on cooldown
        return False  # Expired

    def get_yield_stats(self, role: str) -> RoleYieldStats:
        if role not in self._yields:
            self._yields[role] = RoleYieldStats(role=role)
        return self._yields[role]

    def is_saturated(
        self,
        max_duplicate_rate: float = 0.5,
        min_yield: float = 0.05,
        min_scans: int = 5,
    ) -> bool:
        """Detect if the project has been fully audited (§39).

        Returns True when most discovery roles have high duplicate rates
        and low yield after sufficient scans.
        """
        active_roles = [r for r in DISCOVERY_ROLES if r in self._yields]
        if len(active_roles) < 3:
            return False

        saturated_count = 0
        for role in active_roles:
            stats = self._yields[role]
            if stats.total_scans < min_scans:
                continue
            if stats.duplicate_rate > max_duplicate_rate and stats.yield_rate < min_yield:
                saturated_count += 1

        return saturated_count >= len(active_roles) * 0.6

    def compute_allocation(
        self,
        available_slots: int,
        config: Any,
        current_commit_sha: str = "",
        project_signals: dict[str, float] | None = None,
        now: float | None = None,
    ) -> DiscoveryAllocation:
        """Determine how to distribute discovery slots across roles (§9).

        Uses weighted allocation based on:
        1. Base weights (all roles get some representation)
        2. Yield history (high-yield roles get more)
        3. Project signals (low test coverage → more test auditors)
        4. Cooldown filtering (skip recently-scanned scopes)

        Args:
            available_slots: number of free slots for discovery
            config: SchedulerConfig instance
            current_commit_sha: HEAD commit for cooldown checking
            project_signals: optional hints like {"test_coverage": 0.3}
            now: current timestamp

        Returns:
            DiscoveryAllocation with per-role counts
        """
        now = now or time.time()
        project_signals = project_signals or {}

        if available_slots <= 0:
            return DiscoveryAllocation(
                allocations={}, total_slots=0, reason="no slots available"
            )

        dup_threshold = getattr(config.discovery, "max_duplicate_rate_before_throttle",
                        getattr(config.discovery, "max_duplicate_rate", 0.5))
        min_yield = getattr(config.discovery, "min_yield_to_continue",
                    getattr(config.discovery, "min_yield", 0.05))
        if self.is_saturated(dup_threshold, min_yield):
            return DiscoveryAllocation(
                allocations={}, total_slots=0,
                reason="project saturated", is_saturated=True
            )

        cfg_max = getattr(config, "max_slots", 30)
        max_discovery = int(cfg_max * config.discovery.maximum_fraction)
        effective_slots = min(available_slots, max_discovery)

        # Ensure minimum floor
        effective_slots = max(effective_slots, config.discovery.minimum_slots)
        effective_slots = min(effective_slots, available_slots)

        # Compute base weights for each role
        weights: dict[str, float] = {}
        for role in DISCOVERY_ROLES:
            w = 1.0  # base weight

            # Adjust by historical yield
            stats = self.get_yield_stats(role)
            if stats.total_scans >= 3:
                if stats.yield_rate > 0.2:
                    w *= 1.5  # High yield: boost
                elif stats.duplicate_rate > config.discovery.max_duplicate_rate_before_throttle:
                    w *= 0.3  # Too many dupes: suppress

            # Adjust by project signals
            signal_map = {
                "test_gap_auditor": "test_coverage",
                "security_auditor": "security_changes",
                "performance_auditor": "performance_regression",
                "documentation_auditor": "doc_drift",
                "architecture_auditor": "new_architecture",
                "dependency_auditor": "dependency_age",
            }
            signal_key = signal_map.get(role)
            if signal_key and signal_key in project_signals:
                w *= 1.0 + project_signals[signal_key]

            # Check cooldown — skip entirely if on cooldown
            if current_commit_sha and self.is_on_cooldown(
                role, "full_project", current_commit_sha,
                config.discovery.cooldown_seconds, now
            ):
                w = 0.0

            weights[role] = max(0.0, w)

        # Distribute slots proportionally by weight
        total_weight = sum(weights.values())
        if total_weight <= 0:
            # All roles on cooldown or zero weight — assign evenly to non-cooldown
            eligible = [r for r in DISCOVERY_ROLES if weights.get(r, 0) >= 0]
            if not eligible:
                eligible = list(DISCOVERY_ROLES)
            per_role = max(1, effective_slots // len(eligible))
            alloc = {r: per_role for r in eligible[:effective_slots]}
            return DiscoveryAllocation(
                allocations=alloc, total_slots=sum(alloc.values()),
                reason="equal fallback distribution"
            )

        allocations: dict[str, int] = {}
        assigned = 0

        # First pass: proportional allocation
        for role in DISCOVERY_ROLES:
            w = weights[role]
            if w <= 0:
                allocations[role] = 0
                continue
            share = int(effective_slots * w / total_weight)
            allocations[role] = share
            assigned += share

        # Second pass: distribute remainder to highest-weight roles
        remainder = effective_slots - assigned
        sorted_roles = sorted(
            [r for r in DISCOVERY_ROLES if weights.get(r, 0) > 0],
            key=lambda r: weights[r],
            reverse=True,
        )
        idx = 0
        while remainder > 0 and sorted_roles:
            role = sorted_roles[idx % len(sorted_roles)]
            allocations[role] = allocations.get(role, 0) + 1
            remainder -= 1
            idx += 1

        # Remove zero-count entries
        allocations = {k: v for k, v in allocations.items() if v > 0}

        return DiscoveryAllocation(
            allocations=allocations,
            total_slots=sum(allocations.values()),
            reason=f"weighted distribution across {len(allocations)} roles",
        )
