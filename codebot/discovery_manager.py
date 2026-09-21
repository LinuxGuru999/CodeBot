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

import html as html_module
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

# Roles that use cheap models (§28): lower reasoning requirements,
# metadata-driven checks rather than deep semantic analysis
CHEAP_DISCOVERY_ROLES: frozenset[str] = frozenset({
    "test_gap_auditor",
    "documentation_auditor",
    "dependency_auditor",
})

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
    """Productivity tracking for a single discovery role (§11, §32, §34)."""
    role: str
    total_scans: int = 0
    validated_tickets: int = 0
    duplicates: int = 0
    rejected: int = 0
    no_actionable_runs: int = 0
    completed_downstream: int = 0
    hallucinated_references: int = 0
    stale_findings_prevented: int = 0
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

    @property
    def hallucination_rate(self) -> float:
        if self.total_scans == 0:
            return 0.0
        return self.hallucinated_references / self.total_scans

    def record_scan(
        self,
        validated: int,
        duplicates: int,
        rejected: int,
        cost_tokens: int,
        now: float | None = None,
        no_actionable: bool = False,
        hallucinated: int = 0,
        stale_prevented: int = 0,
    ) -> None:
        self.total_scans += 1
        self.validated_tickets += validated
        self.duplicates += duplicates
        self.rejected += rejected
        if no_actionable:
            self.no_actionable_runs += 1
        self.hallucinated_references += hallucinated
        self.stale_findings_prevented += stale_prevented
        self.total_cost_tokens += cost_tokens
        self.last_scan_at = now or time.time()

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "total_scans": self.total_scans,
            "validated_tickets": self.validated_tickets,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "no_actionable_runs": self.no_actionable_runs,
            "completed_downstream": self.completed_downstream,
            "hallucinated_references": self.hallucinated_references,
            "stale_findings_prevented": self.stale_findings_prevented,
            "yield_rate": round(self.yield_rate, 4),
            "duplicate_rate": round(self.duplicate_rate, 4),
            "cost_per_ticket": round(self.cost_per_ticket, 1),
            "hallucination_rate": round(self.hallucination_rate, 4),
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

    BURST_WINDOW_SECONDS = 300.0
    BURST_THRESHOLD = 15
    BURST_MULTIPLIER = 5.0

    def __init__(self, state_dir: Path | None = None) -> None:
        self._state_dir = state_dir
        self._cooldowns: list[DiscoveryCooldown] = []
        self._cooldown_index: dict[tuple[str, str], int] = {}  # (role, scope) -> index in _cooldowns
        self._yields: dict[str, RoleYieldStats] = {}
        self._last_prune_at: float = 0.0
        self._ticket_creation_times: list[float] = []
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
                cd = DiscoveryCooldown(**entry)
                self._cooldowns.append(cd)
            # Cap to last 500 entries (handles legacy state with >500)
            if len(self._cooldowns) > 500:
                self._cooldowns = self._cooldowns[-500:]
            # Rebuild index: latest entry for each (role, scope)
            self._cooldown_index.clear()
            for idx, cd in enumerate(self._cooldowns):
                self._cooldown_index[(cd.role, cd.scope)] = idx
            for role, stats in data.get("yields", {}).items():
                ys = RoleYieldStats(role=role)
                ys.total_scans = stats.get("total_scans", 0)
                ys.validated_tickets = stats.get("validated_tickets", 0)
                ys.duplicates = stats.get("duplicates", 0)
                ys.rejected = stats.get("rejected", 0)
                ys.no_actionable_runs = stats.get("no_actionable_runs", 0)
                ys.completed_downstream = stats.get("completed_downstream", 0)
                ys.hallucinated_references = stats.get("hallucinated_references", 0)
                ys.stale_findings_prevented = stats.get("stale_findings_prevented", 0)
                ys.total_cost_tokens = stats.get("total_cost_tokens", 0)
                ys.last_scan_at = stats.get("last_scan_at", 0.0)
                self._yields[role] = ys
            for ts in data.get("ticket_creation_times", []):
                self._ticket_creation_times.append(float(ts))
            # Set _last_prune_at to now so we don't immediately re-prune
            self._last_prune_at = time.time()
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
            "ticket_creation_times": list(self._ticket_creation_times[-500:]),
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
        *,
        no_actionable: bool = False,
        hallucinated: int = 0,
        stale_prevented: int = 0,
        tickets_created: int = 0,
    ) -> None:
        """Record a completed discovery scan (§10, §11, §34, §52)."""
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
        self._cooldown_index[(role, scope)] = idx
        if role not in self._yields:
            self._yields[role] = RoleYieldStats(role=role)
        self._yields[role].record_scan(
            findings, duplicates, rejected, cost_tokens, now,
            no_actionable=no_actionable,
            hallucinated=hallucinated,
            stale_prevented=stale_prevented,
        )
        for _ in range(max(0, tickets_created)):
            self._ticket_creation_times.append(now)
        if len(self._ticket_creation_times) > 500:
            self._ticket_creation_times = self._ticket_creation_times[-500:]
        # Amortized pruning: skip if list is small and recently pruned
        needs_prune = len(self._cooldowns) > 500 or (now - self._last_prune_at) > 86400
        if not needs_prune:
            return
        # Prune old cooldowns (keep last 30 days) and enforce 500-entry cap
        cutoff = now - 30 * 86400
        old_cooldowns = self._cooldowns
        self._cooldowns = []
        self._cooldown_index.clear()
        for c in old_cooldowns:
            if c.completed_at > cutoff:
                self._cooldowns.append(c)
                self._cooldown_index[(c.role, c.scope)] = len(self._cooldowns) - 1
        # Enforce hard cap: keep only the 500 most recent
        if len(self._cooldowns) > 500:
            self._cooldowns = self._cooldowns[-500:]
            self._cooldown_index.clear()
            for idx, c in enumerate(self._cooldowns):
                self._cooldown_index[(c.role, c.scope)] = idx
        self._last_prune_at = now

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

    def detect_burst(self, now: float | None = None) -> bool:
        now = now or time.time()
        cutoff = now - self.BURST_WINDOW_SECONDS
        recent = [t for t in self._ticket_creation_times if t >= cutoff]
        if len(recent) < self.BURST_THRESHOLD:
            return False
        older = [t for t in self._ticket_creation_times if t < cutoff]
        if not older:
            return len(recent) >= self.BURST_THRESHOLD
        baseline_rate = len(older) / self.BURST_WINDOW_SECONDS
        return len(recent) >= baseline_rate * self.BURST_MULTIPLIER

    def record_ticket_created(self, now: float | None = None) -> None:
        now = now or time.time()
        self._ticket_creation_times.append(now)
        if len(self._ticket_creation_times) > 500:
            self._ticket_creation_times = self._ticket_creation_times[-500:]

    def get_coverage_summary(self, now: float | None = None) -> dict[str, Any]:
        now = now or time.time()
        coverage: dict[str, Any] = {}
        scanned_roles: set[str] = set()
        for cd in self._cooldowns:
            scanned_roles.add(cd.role)
            key = cd.scope
            if key not in coverage:
                coverage[key] = {
                    "scope": cd.scope,
                    "last_role": cd.role,
                    "last_commit": cd.commit_sha,
                    "last_scanned_at": cd.completed_at,
                    "findings": cd.findings_count,
                    "duplicates": cd.duplicate_count,
                }
            elif cd.completed_at > coverage[key]["last_scanned_at"]:
                coverage[key] = {
                    "scope": cd.scope,
                    "last_role": cd.role,
                    "last_commit": cd.commit_sha,
                    "last_scanned_at": cd.completed_at,
                    "findings": cd.findings_count,
                    "duplicates": cd.duplicate_count,
                }
        never_scanned = [r for r in DISCOVERY_ROLES if r not in scanned_roles]
        return {
            "scopes": coverage,
            "never_scanned_roles": never_scanned,
            "total_scans": len(self._cooldowns),
            "as_of": now,
        }

    @staticmethod
    def roles_for_change(change_kind: str) -> tuple[str, ...]:
        triggers: dict[str, tuple[str, ...]] = {
            "security": ("security_auditor",),
            "concurrency": ("bug_hunter",),
            "scheduler": ("bug_hunter", "performance_auditor"),
            "dependency": ("dependency_auditor", "security_auditor"),
            "ui": ("ux_auditor", "test_gap_auditor"),
            "api": ("documentation_auditor", "test_gap_auditor"),
            "architecture": ("architecture_auditor",),
            "performance": ("performance_auditor",),
            "documentation": (),
            "test": (),
        }
        return triggers.get(change_kind, ())

    def record_downstream_completion(self, role: str, count: int = 1) -> None:
        if role not in self._yields:
            self._yields[role] = RoleYieldStats(role=role)
        self._yields[role].completed_downstream += count

    def compute_allocation(
        self,
        available_slots: int,
        config: Any,
        current_commit_sha: str = "",
        project_signals: dict[str, float] | None = None,
        now: float | None = None,
        downstream_backlog: int = 0,
    ) -> DiscoveryAllocation:
        """Determine how to distribute discovery slots across roles (§9, §31).

        Uses weighted allocation based on:
        1. Base weights (all roles get some representation)
        2. Yield history (high-yield roles get more)
        3. Project signals (low test coverage → more test auditors)
        4. Cooldown filtering (skip recently-scanned scopes)
        5. Backpressure: large downstream backlog reduces discovery slots (§31)
        6. Burst detection: active bursts suppress discovery (§52)

        Args:
            available_slots: number of free slots for discovery
            config: SchedulerConfig instance
            current_commit_sha: HEAD commit for cooldown checking
            project_signals: optional hints like {"test_coverage": 0.3}
            now: current timestamp
            downstream_backlog: count of actionable downstream tickets (§31)

        Returns:
            DiscoveryAllocation with per-role counts
        """
        now = now or time.time()
        project_signals = project_signals or {}

        if available_slots <= 0:
            return DiscoveryAllocation(
                allocations={}, total_slots=0, reason="no slots available"
            )

        if self.detect_burst(now):
            return DiscoveryAllocation(
                allocations={}, total_slots=0,
                reason="ticket burst detected — discovery throttled (§52)",
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

        # Backpressure (§31): large downstream backlog reduces discovery slots.
        backlog_cfg = getattr(config, "backlog", None)
        backlog_target = getattr(backlog_cfg, "target", 50) if backlog_cfg else 50
        backlog_high = getattr(backlog_cfg, "high_watermark", 100) if backlog_cfg else 100
        if downstream_backlog >= backlog_high:
            effective_slots = max(1, effective_slots // 4)
        elif downstream_backlog >= backlog_target:
            effective_slots = max(1, effective_slots // 2)

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

    def render_yield_table_html(self, sort_by: str = "role") -> str:
        """Render yield statistics as an accessible HTML table.

        Uses <th> elements with scope='col' for column headers and
        scope='row' for role cells. Includes sortable column controls
        via focusable <button> elements with aria-sort attributes.

        Args:
            sort_by: Column to sort by ('role', 'total_scans', 'validated_tickets',
                     'yield_rate', 'duplicate_rate', 'cost_per_ticket')

        Returns:
            HTML string containing the accessible table
        """
        # Determine sort direction based on current sort key
        sort_keys = {
            "role": lambda s: s.role,
            "total_scans": lambda s: s.total_scans,
            "validated_tickets": lambda s: s.validated_tickets,
            "yield_rate": lambda s: s.yield_rate,
            "duplicate_rate": lambda s: s.duplicate_rate,
            "cost_per_ticket": lambda s: s.cost_per_ticket if s.cost_per_ticket != float("inf") else float("-inf"),
        }

        stats_list = list(self._yields.values())
        if not stats_list:
            return '<table><caption>Yield Statistics</caption><tbody><tr><td>No data available</td></tr></tbody></table>'

        sort_key_func = sort_keys.get(sort_by, sort_keys["role"])
        sorted_stats = sorted(stats_list, key=sort_key_func, reverse=(sort_by in ("yield_rate",)))

        columns = [
            ("role", "Role"),
            ("total_scans", "Total Scans"),
            ("validated_tickets", "Validated Tickets"),
            ("yield_rate", "Yield Rate"),
            ("duplicate_rate", "Duplicate Rate"),
            ("cost_per_ticket", "Cost/Ticket"),
        ]

        rows_html = []
        for stats in sorted_stats:
            cost_val = f"{stats.cost_per_ticket:.1f}" if stats.cost_per_ticket != float("inf") else "N/A"
            row = (
                f"<tr>"
                f"<th scope=\"row\">{html_module.escape(stats.role)}</th>"
                f"<td>{stats.total_scans}</td>"
                f"<td>{stats.validated_tickets}</td>"
                f"<td>{stats.yield_rate:.4f}</td>"
                f"<td>{stats.duplicate_rate:.4f}</td>"
                f"<td>{cost_val}</td>"
                f"</tr>"
            )
            rows_html.append(row)

        # Build header with sort controls
        headers_html = []
        for col_key, col_label in columns:
            is_current_sort = col_key == sort_by
            aria_sort = "ascending" if is_current_sort else "none"
            button_html = (
                f'<button type="button" tabindex="0" aria-sort="{aria_sort}" '
                f'data-sort-key="{col_key}">{html_module.escape(col_label)}</button>'
            )
            headers_html.append(f'<th scope="col">{button_html}</th>')

        table_html = (
            '<table>\n'
            f'<caption>Discovery Yield Statistics</caption>\n'
            '<thead>\n<tr>\n' + '\n'.join(headers_html) + '\n</tr>\n</thead>\n'
            '<tbody>\n' + '\n'.join(rows_html) + '\n</tbody>\n'
            '</table>'
        )
        return table_html

    def render_yield_chart_html(self) -> str:
        """Render yield chart container with accessible aria-label.

        Provides a textual description of yield trends for screen readers,
        including highest and lowest yielding roles.

        Returns:
            HTML string containing chart container with aria-label
        """
        if not self._yields:
            return '<div role="img" aria-label="No yield data available"></div>'

        stats_list = list(self._yields.values())
        
        # Find highest and lowest yield rates (only for roles with scans)
        roles_with_scans = [s for s in stats_list if s.total_scans > 0]
        
        if not roles_with_scans:
            return '<div role="img" aria-label="No scan data available for trend analysis"></div>'

        highest = max(roles_with_scans, key=lambda s: s.yield_rate)
        lowest = min(roles_with_scans, key=lambda s: s.yield_rate)

        total_scans = sum(s.total_scans for s in stats_list)
        total_validated = sum(s.validated_tickets for s in stats_list)
        overall_yield = total_validated / total_scans if total_scans > 0 else 0

        insight = (
            f"Yield trend: Overall yield rate is {overall_yield:.2%}. "
            f"Highest yielding role is {highest.role} at {highest.yield_rate:.2%} yield rate "
            f"with {highest.validated_tickets} validated tickets from {highest.total_scans} scans. "
            f"Lowest yielding role is {lowest.role} at {lowest.yield_rate:.2%} yield rate. "
            f"Total scans across all roles: {total_scans}."
        )

        chart_html = (
            f'<div role="img" aria-label="{html_module.escape(insight)}">'
            f'<p>Chart visualization placeholder - see aria-label for data summary</p>'
            f'</div>'
        )
        return chart_html
