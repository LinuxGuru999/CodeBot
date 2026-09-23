#!/usr/bin/env python3
"""Role-based agent abstraction for CodeBot.

Purpose
-------
Replaces hardcoded bot names (issues-bot, worker-5, security_auditor) with
a composable ROLE + TASK + MODEL_PROFILE + TOOL_POLICY abstraction. Each
agent instance is assembled from these four dimensions rather than being
a monolithic named entity.

Why
---
CODEBOT-ROADMAP.md §2.H requires moving away from individually named bots.
Hardcoded bot names couple orchestration logic to specific implementations,
making it impossible to reuse the orchestrator for a different project.
Roles define *what* an agent does; models define *how smart* it is;
tool policies define *what it can touch*; tasks define *what it works on*.

Invariants
----------
- stdlib-only (dataclasses, enum, json)
- Role definitions are immutable once created
- Tool policies reference allowlists, not blocklists (fail-closed)
- Model profiles specify capability requirements, not provider names

Role Count: 31 (9 discovery + 6 implementation + 8 review + 6 control + 2 planning)
"""

from __future__ import annotations

import json
import logging
import traceback
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger(__name__)


class RoleCategory(str, Enum):
    DISCOVERY = "discovery"
    PLANNING = "planning"
    IMPLEMENTATION = "implementation"
    REVIEW = "review"
    CONTROL = "control"


class ReasoningLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class CodingLevel(str, Enum):
    BASIC = "basic"
    ADVANCED = "advanced"


class ContextSize(str, Enum):
    SMALL = "small"
    LARGE = "large"


class CostClass(str, Enum):
    CHEAP = "cheap"
    STANDARD = "standard"
    PREMIUM = "premium"


class LatencyClass(str, Enum):
    INTERACTIVE = "interactive"
    BACKGROUND = "background"


@dataclass(frozen=True)
class ModelProfile:
    reasoning: ReasoningLevel
    coding: CodingLevel
    context: ContextSize
    cost_class: CostClass
    latency: LatencyClass
    security_review: bool = False
    description: str = ""

    def satisfies(self, required: ModelProfile) -> bool:
        reasoning_order = {ReasoningLevel.LOW: 0, ReasoningLevel.MEDIUM: 1, ReasoningLevel.HIGH: 2}
        coding_order = {CodingLevel.BASIC: 0, CodingLevel.ADVANCED: 1}
        context_order = {ContextSize.SMALL: 0, ContextSize.LARGE: 1}
        if reasoning_order[self.reasoning] < reasoning_order[required.reasoning]:
            return False
        if coding_order[self.coding] < coding_order[required.coding]:
            return False
        if context_order[self.context] < context_order[required.context]:
            return False
        if required.security_review and not self.security_review:
            return False
        return True


@dataclass(frozen=True)
class ToolPolicy:
    allowed_tools: frozenset[str]
    allowed_commands: frozenset[str]
    filesystem_scope: str
    network_access: bool = False
    git_write: bool = False
    max_file_size_bytes: int = 1_000_000

    def allows_tool(self, tool_name: str) -> bool:
        return tool_name in self.allowed_tools

    def allows_command(self, cmd: str) -> bool:
        base_cmd = cmd.split()[0] if cmd else ""
        return base_cmd in self.allowed_commands


@dataclass(frozen=True)
class AgentRole:
    name: str
    category: RoleCategory
    description: str
    required_model: ModelProfile
    tool_policy: ToolPolicy
    incentive: str = ""
    adversarial_to: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "category": self.category.value,
            "description": self.description,
            "incentive": self.incentive,
            "adversarial_to": list(self.adversarial_to),
            "required_model": {
                "reasoning": self.required_model.reasoning.value,
                "coding": self.required_model.coding.value,
                "context": self.required_model.context.value,
                "cost_class": self.required_model.cost_class.value,
                "latency": self.required_model.latency.value,
                "security_review": self.required_model.security_review,
            },
            "tool_policy": {
                "allowed_tools": sorted(self.tool_policy.allowed_tools),
                "allowed_commands": sorted(self.tool_policy.allowed_commands),
                "filesystem_scope": self.tool_policy.filesystem_scope,
                "network_access": self.tool_policy.network_access,
                "git_write": self.tool_policy.git_write,
                "max_file_size_bytes": self.tool_policy.max_file_size_bytes,
            },
        }


@dataclass(frozen=True)
class AgentTask:
    role_name: str
    ticket_id: str
    model_id: str
    prompt_file: str = ""
    max_iterations: int = 50
    session_timeout: int = 300
    extra_context: dict[str, Any] = field(default_factory=dict)


STANDARD_TOOLS = frozenset({"read", "write", "edit", "grep", "glob", "bash"})
READ_ONLY_TOOLS = frozenset({"read", "grep", "glob"})
RESEARCH_TOOLS = frozenset({"read", "grep", "glob", "web_search", "web_fetch"})
UX_AUDIT_TOOLS = frozenset({"read", "grep", "glob", "screenshot", "a11y_snapshot"})
STANDARD_COMMANDS = frozenset({
    "python3", "pytest", "ls", "wc", "cat", "head", "tail",
    "git", "cp", "mv", "mkdir", "date", "realpath",
})
READ_ONLY_COMMANDS = frozenset({"python3", "pytest", "ls", "wc", "cat", "head", "tail", "git"})


DISCOVERY_ROLES: list[AgentRole] = [
    AgentRole(
        name="bug_hunter",
        category=RoleCategory.DISCOVERY,
        description="Scans source code for logic errors, unhandled paths, race conditions",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(RESEARCH_TOOLS, READ_ONLY_COMMANDS, "project_root", network_access=True),
        incentive="Find real bugs. Maximize true positives. Penalized for false reports.",
    ),
    AgentRole(
        name="security_auditor",
        category=RoleCategory.DISCOVERY,
        description="Identifies injection vulnerabilities, auth bypasses, secret leaks",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND, security_review=True),
        tool_policy=ToolPolicy(RESEARCH_TOOLS, READ_ONLY_COMMANDS, "project_root", network_access=True),
        incentive="Find exploitable vulnerabilities. Adversarial to implementers.",
        adversarial_to=("implementer",),
    ),
    AgentRole(
        name="architecture_auditor",
        category=RoleCategory.DISCOVERY,
        description="Detects coupling violations, boundary breaches, technical debt",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(RESEARCH_TOOLS, READ_ONLY_COMMANDS, "project_root", network_access=True),
        incentive="Find architectural violations. Adversarial to implementers who add complexity.",
        adversarial_to=("implementer",),
    ),
    AgentRole(
        name="performance_auditor",
        category=RoleCategory.DISCOVERY,
        description="Identifies O(n²) patterns, unbounded allocations, hot-path waste",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(RESEARCH_TOOLS, READ_ONLY_COMMANDS, "project_root", network_access=True),
        incentive="Find scalability regressions and resource waste. Adversarial to implementers who add overhead.",
        adversarial_to=("implementer",),
    ),
    AgentRole(
        name="test_gap_auditor",
        category=RoleCategory.DISCOVERY,
        description="Identifies public functions and critical paths lacking test coverage",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.BASIC, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(RESEARCH_TOOLS, READ_ONLY_COMMANDS, "project_root", network_access=True),
        incentive="Maximize coverage gap detection accuracy.",
    ),
    AgentRole(
        name="documentation_auditor",
        category=RoleCategory.DISCOVERY,
        description="Detects stale docs, missing docstrings, API contract drift",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.BASIC, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find claims that are no longer true.",
        adversarial_to=("implementer",),
    ),
    AgentRole(
        name="dependency_auditor",
        category=RoleCategory.DISCOVERY,
        description="Checks for CVEs, outdated packages, license violations",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.BASIC, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(RESEARCH_TOOLS, READ_ONLY_COMMANDS | frozenset({"pip"}), "project_root", network_access=True),
        incentive="Find supply chain risks.",
    ),
    AgentRole(
        name="ux_auditor",
        category=RoleCategory.DISCOVERY,
        description="Evaluates UI for usability, accessibility (WCAG), and workflow friction using static analysis and Playwright browser snapshots",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.BASIC, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(UX_AUDIT_TOOLS, READ_ONLY_COMMANDS | frozenset({"node", "npm", "npx"}), "project_root", network_access=True),
        incentive="Find usability issues, accessibility violations, and workflow friction.",
    ),
    AgentRole(
        name="feature_hunter",
        category=RoleCategory.DISCOVERY,
        description="Reads roadmap index and creates feature tickets for pending deliverables",
        required_model=ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(frozenset({"read", "write", "grep", "glob", "bash", "create_ticket"}), READ_ONLY_COMMANDS | frozenset({"python3"}), "project_root"),
        incentive="Find roadmap gaps and create actionable feature tickets. Maximize coverage of pending deliverables.",
    ),
]


IMPLEMENTATION_ROLES: list[AgentRole] = [
    AgentRole(
        name="implementer",
        category=RoleCategory.IMPLEMENTATION,
        description="Unified implementation agent for all ticket classes: bugs, features, refactors, tests, migrations, docs",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(STANDARD_TOOLS, STANDARD_COMMANDS, "project_root", git_write=True),
        incentive="Make the requested change work correctly and completely.",
        adversarial_to=("reviewer", "security_reviewer", "architecture_reviewer", "performance_reviewer"),
    ),
]


REVIEW_ROLES: list[AgentRole] = [
    AgentRole(
        name="reviewer",
        category=RoleCategory.REVIEW,
        description="Default broad reviewer for all tickets. Verifies correctness, acceptance criteria, tests, scope compliance. Escalates to specialists when evidence warrants it.",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Determine whether the implementation satisfies the ticket. Escalate when specialist expertise is needed.",
        adversarial_to=("implementer",),
    ),
    AgentRole(
        name="security_reviewer",
        category=RoleCategory.REVIEW,
        description="Specialist: evaluates security concerns escalated by primary reviewer",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND, security_review=True),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Answer only the specific security question escalated to you.",
        adversarial_to=("implementer",),
    ),
    AgentRole(
        name="architecture_reviewer",
        category=RoleCategory.REVIEW,
        description="Specialist: evaluates architecture concerns escalated by primary reviewer",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Answer only the specific architecture question escalated to you.",
        adversarial_to=("implementer",),
    ),
    AgentRole(
        name="performance_reviewer",
        category=RoleCategory.REVIEW,
        description="Specialist: evaluates performance concerns escalated by primary reviewer",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Answer only the specific performance question escalated to you.",
        adversarial_to=("implementer",),
    ),
    AgentRole(
        name="concurrency_reviewer",
        category=RoleCategory.REVIEW,
        description="Evaluates lock ordering, TOCTOU races, claim contention, shared mutable state hazards",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find real concurrency hazards. Answer only the specific question escalated to you.",
        adversarial_to=("implementer",),
    ),
    AgentRole(
        name="data_integrity_reviewer",
        category=RoleCategory.REVIEW,
        description="Evaluates database migrations, schema changes, destructive writes, state reconciliation correctness",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find data loss risks and migration defects. Answer only the specific question escalated to you.",
        adversarial_to=("implementer",),
    ),
]


CONTROL_ROLES: list[AgentRole] = [
    AgentRole(
        name="scheduler",
        category=RoleCategory.CONTROL,
        description="Orchestrates agent scheduling, spawn gating, tier priority",
        required_model=ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.INTERACTIVE),
        tool_policy=ToolPolicy(frozenset({"read"}), frozenset({"python3"}), "state_dir"),
        incentive="Optimize throughput within budget constraints.",
    ),
    AgentRole(
        name="budget_controller",
        category=RoleCategory.CONTROL,
        description="Tracks token spend, enforces per-ticket and fleet-wide budgets, can pause agents exceeding limits",
        required_model=ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.INTERACTIVE),
        tool_policy=ToolPolicy(frozenset({"read", "write"}), frozenset({"python3"}), "state_dir"),
        incentive="Minimize cost per accepted ticket. Pause runaway agents.",
    ),
    AgentRole(
        name="git_sync",
        category=RoleCategory.CONTROL,
        description="Pushes committed ticket branches to origin and syncs GitHub Issues for completed tickets",
        required_model=ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(frozenset({"read", "write"}), frozenset({"python3"}), "project_root"),
        incentive="Zero lost work: every local commit reaches origin, every COMPLETE ticket is mirrored on GitHub.",
    ),
    AgentRole(
        name="github_mirror",
        category=RoleCategory.CONTROL,
        description="Mirrors ticket lifecycle to GitHub Issues: opens issues for active work, closes on COMPLETE",
        required_model=ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(frozenset({"read", "write"}), frozenset({"python3"}), "project_root", network_access=True),
        incentive="Full visibility: no completed ticket without a linked GitHub issue state.",
    ),
    AgentRole(
        name="ticket_triager",
        category=RoleCategory.CONTROL,
        description="Classifies incoming tickets by priority, complexity, and goal alignment for routing",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.INTERACTIVE),
        tool_policy=ToolPolicy(frozenset({"read", "write"}), frozenset({"python3"}), "state_dir"),
        incentive="Accurately classify tickets to maximize throughput and goal alignment.",
    ),
]


PLANNING_ROLES: list[AgentRole] = [
    AgentRole(
        name="decomposer",
        category=RoleCategory.PLANNING,
        description="Breaks down tickets into atomic implementable sub-tickets for planning",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS | frozenset({"write"}), READ_ONLY_COMMANDS | frozenset({"python3"}), "project_root"),
        incentive="Break tickets into atomic, implementable pieces. Every decomposition feeds planning.",
    ),
    AgentRole(
        name="planner",
        category=RoleCategory.PLANNING,
        description="Generates implementation plans for PLANNING tickets, enabling PLANNING -> IMPLEMENT transitions",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS | frozenset({"write"}), frozenset({"python3"}), "project_root"),
        incentive="Unblock the implementer fleet by generating plans for every PLANNING ticket.",
    ),
    AgentRole(
        name="goal_aligner",
        category=RoleCategory.PLANNING,
        description="Evaluates tickets against project goals for NOW/LATER/NEVER classification",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS | frozenset({"write"}), READ_ONLY_COMMANDS, "project_root"),
        incentive="Correctly classify tickets by goal relevance. Maximize NOW→COMPLETE rate.",
    ),
]


ALL_ROLES: list[AgentRole] = DISCOVERY_ROLES + IMPLEMENTATION_ROLES + REVIEW_ROLES + CONTROL_ROLES + PLANNING_ROLES
ROLE_REGISTRY: dict[str, AgentRole] = {r.name: r for r in ALL_ROLES}


def get_role(name: str, ticket_id: str | None = None) -> AgentRole | None:
    """Retrieve a role by name with structured error logging.
    
    Args:
        name: The role name to look up
        ticket_id: Optional ticket ID for context in error logs
    
    Returns:
        AgentRole if found, None otherwise
    """
    try:
        role = ROLE_REGISTRY.get(name)
        if role is None:
            context = {"role_name": name}
            if ticket_id:
                context["ticket_id"] = ticket_id
            logger.warning(
                "Role not found: role_name=%s, ticket_id=%s",
                name,
                ticket_id
            )
        return role
    except Exception as e:
        context = {"role_name": name}
        if ticket_id:
            context["ticket_id"] = ticket_id
        logger.error(
            "Role lookup failed: role_name=%s, ticket_id=%s, exception_type=%s, message=%s\nTraceback:\n%s",
            name,
            ticket_id,
            type(e).__name__,
            str(e),
            traceback.format_exc()
        )
        return None


def roles_by_category(category: RoleCategory) -> list[AgentRole]:
    return [r for r in ALL_ROLES if r.category == category]


# ---------------------------------------------------------------------------
# Derived Role Name Sets (OCP-compliant: derived from registry, not hardcoded)
# ---------------------------------------------------------------------------
IMPLEMENTER_ROLE_NAMES: frozenset[str] = frozenset(
    r.name for r in ALL_ROLES if r.category == RoleCategory.IMPLEMENTATION
)
DISCOVERY_ROLE_NAMES: frozenset[str] = frozenset(
    r.name for r in ALL_ROLES if r.category == RoleCategory.DISCOVERY
)
REVIEWER_ROLE_NAMES: frozenset[str] = frozenset(
    r.name for r in ALL_ROLES if r.category == RoleCategory.REVIEW
)
PLANNING_ROLE_NAMES: frozenset[str] = frozenset(
    r.name for r in ALL_ROLES if r.category == RoleCategory.PLANNING
)
CONTROL_ROLE_NAMES: frozenset[str] = frozenset(
    r.name for r in ALL_ROLES if r.category == RoleCategory.CONTROL
)

IMPLEMENTATION_ROLE_ORDER: tuple[str, ...] = tuple(r.name for r in IMPLEMENTATION_ROLES)

_DEFAULT_IMPLEMENTER_ROLE: str = IMPLEMENTATION_ROLE_ORDER[0] if IMPLEMENTATION_ROLE_ORDER else ""


def get_implementer_for_class(ticket_class: str) -> str:
    """Return the implementer role name for a ticket class.

    The default is derived from the registered implementation roles, so adding
    or renaming an implementation role only requires changing the registry.
    """
    return _DEFAULT_IMPLEMENTER_ROLE
