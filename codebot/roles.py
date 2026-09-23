"""Canonical role definitions for the CodeBot pipeline.

This module centralizes role name constants to avoid circular dependencies
between dispatch_service, scheduler_v2, and other modules.
"""

from __future__ import annotations

# Implementation roles
IMPLEMENTER_ROLE_NAMES: frozenset[str] = frozenset({"implementer"})

REVIEWER_ROLE_NAMES: frozenset[str] = frozenset({
    "reviewer", "security_reviewer", "architecture_reviewer",
    "performance_reviewer", "concurrency_reviewer",
    "data_integrity_reviewer",
})

# Planning roles
PLANNING_ROLE_NAMES: frozenset[str] = frozenset({"planner"})

# Decomposition roles
DECOMPOSER_ROLE_NAMES: frozenset[str] = frozenset({"decomposer"})

# Discovery/Audit roles
DISCOVERY_ROLE_NAMES: frozenset[str] = frozenset({
    "bug_hunter", "security_auditor", "architecture_auditor", "performance_auditor",
    "test_gap_auditor", "documentation_auditor", "dependency_auditor", "ux_auditor",
    "feature_hunter",
})

# Control roles (platform infrastructure, not part of ticket pipeline)
CONTROL_ROLE_NAMES: frozenset[str] = frozenset({
    "scheduler", "ticket_triager", "conflict_resolver", "budget_controller",
    "git_sync", "github_mirror",
})
