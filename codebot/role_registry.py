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

Role Count: 29 (9 discovery + 6 implementation + 8 review + 4 control + 2 planning)
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
        adversarial_to=("backend_implementer", "frontend_implementer", "general_implementer"),
    ),
    AgentRole(
        name="architecture_auditor",
        category=RoleCategory.DISCOVERY,
        description="Detects coupling violations, boundary breaches, technical debt",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(RESEARCH_TOOLS, READ_ONLY_COMMANDS, "project_root", network_access=True),
        incentive="Find architectural violations. Adversarial to implementers who add complexity.",
        adversarial_to=("backend_implementer", "simplicity_reviewer"),
    ),
    AgentRole(
        name="performance_auditor",
        category=RoleCategory.DISCOVERY,
        description="Identifies O(n²) patterns, unbounded allocations, hot-path waste",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(RESEARCH_TOOLS, READ_ONLY_COMMANDS, "project_root", network_access=True),
        incentive="Find scalability regressions and resource waste. Adversarial to implementers who add overhead.",
        adversarial_to=("backend_implementer", "general_implementer"),
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
        adversarial_to=("documentation_implementer",),
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
        name="general_implementer",
        category=RoleCategory.IMPLEMENTATION,
        description="Implements bug fixes, features, and refactors across the codebase",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(STANDARD_TOOLS, STANDARD_COMMANDS, "project_root", git_write=True),
        incentive="Make the requested change work correctly and completely.",
        adversarial_to=("correctness_reviewer", "security_reviewer", "architecture_reviewer", "performance_reviewer", "simplicity_reviewer"),
    ),
    AgentRole(
        name="backend_implementer",
        category=RoleCategory.IMPLEMENTATION,
        description="Implements server-side logic, APIs, data models",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(STANDARD_TOOLS, STANDARD_COMMANDS, "project_root", git_write=True),
        incentive="Implement backend changes. Defended against by security and architecture reviewers.",
        adversarial_to=("correctness_reviewer", "security_reviewer", "architecture_reviewer", "performance_reviewer"),
    ),
    AgentRole(
        name="frontend_implementer",
        category=RoleCategory.IMPLEMENTATION,
        description="Implements UI components, styling, client-side logic",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(STANDARD_TOOLS, STANDARD_COMMANDS, "project_root", git_write=True),
        incentive="Implement frontend changes. Defended against by UX reviewer.",
        adversarial_to=("ux_auditor",),
    ),
    AgentRole(
        name="test_implementer",
        category=RoleCategory.IMPLEMENTATION,
        description="Writes unit, integration, and E2E tests",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.SMALL, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(STANDARD_TOOLS, STANDARD_COMMANDS, "project_root", git_write=True),
        incentive="Maximize test coverage for the target change.",
        adversarial_to=("test_reviewer",),
    ),
    AgentRole(
        name="migration_implementer",
        category=RoleCategory.IMPLEMENTATION,
        description="Implements data migrations, schema changes, version transitions",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(STANDARD_TOOLS, STANDARD_COMMANDS, "project_root", git_write=True),
        incentive="Safe, reversible data transformation.",
        adversarial_to=("correctness_reviewer", "security_reviewer"),
    ),
    AgentRole(
        name="documentation_implementer",
        category=RoleCategory.IMPLEMENTATION,
        description="Updates module docs, API contracts, READMEs, ADRs",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.BASIC, ContextSize.SMALL, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(STANDARD_TOOLS, STANDARD_COMMANDS, "project_root", git_write=True),
        incentive="Accurate documentation matching actual system state.",
    ),
]


REVIEW_ROLES: list[AgentRole] = [
    AgentRole(
        name="correctness_reviewer",
        category=RoleCategory.REVIEW,
        description="Verifies implementation matches specification and acceptance criteria",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find behavior the tests failed to cover or spec violations.",
        adversarial_to=("general_implementer", "backend_implementer", "migration_implementer"),
    ),
    AgentRole(
        name="security_reviewer",
        category=RoleCategory.REVIEW,
        description="Attempts to exploit or abuse the implementation",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND, security_review=True),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find a way to exploit or abuse the change. Adversarial to implementer.",
        adversarial_to=("general_implementer", "backend_implementer", "frontend_implementer", "architecture_reviewer", "migration_implementer"),
    ),
    AgentRole(
        name="architecture_reviewer",
        category=RoleCategory.REVIEW,
        description="Evaluates coupling, boundary violations, technical debt introduction",
        required_model=ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find coupling, boundary violations, or technical debt.",
        adversarial_to=("general_implementer", "backend_implementer", "simplicity_reviewer"),
    ),
    AgentRole(
        name="test_reviewer",
        category=RoleCategory.REVIEW,
        description="Evaluates test adequacy, edge case coverage, assertion quality",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.SMALL, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find behavior the tests failed to cover.",
        adversarial_to=("test_implementer",),
    ),
    AgentRole(
        name="performance_reviewer",
        category=RoleCategory.REVIEW,
        description="Evaluates scalability implications and resource usage",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find scalability or resource regressions.",
        adversarial_to=("general_implementer", "backend_implementer"),
    ),
    AgentRole(
        name="simplicity_reviewer",
        category=RoleCategory.REVIEW,
        description="Finds unnecessary complexity, over-engineering, dead code introduced",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.SMALL, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find unnecessary complexity. Adversarial to over-engineering.",
        adversarial_to=("general_implementer", "backend_implementer", "architecture_auditor"),
    ),
    AgentRole(
        name="documentation_reviewer",
        category=RoleCategory.REVIEW,
        description="Verifies documentation accuracy against implementation",
        required_model=ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS, READ_ONLY_COMMANDS, "project_root"),
        incentive="Find claims that are no longer true.",
        adversarial_to=("documentation_implementer",),
    ),
    AgentRole(
        name="ux_reviewer",
        category=RoleCategory.REVIEW,
        description="Evaluates UI implementations for usability, accessibility (WCAG), visual consistency, and workflow friction",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.BASIC, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(UX_AUDIT_TOOLS, READ_ONLY_COMMANDS | frozenset({"node", "npm", "npx"}), "project_root", network_access=True),
        incentive="Find usability issues, accessibility violations, and visual regressions introduced by implementation.",
        adversarial_to=("frontend_implementer", "general_implementer"),
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
        name="ticket_triager",
        category=RoleCategory.CONTROL,
        description="Validates incoming tickets: checks completeness, deduplicates via SHA-256, assigns severity, and routes to ready queue",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.BASIC, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS | frozenset({"write"}), READ_ONLY_COMMANDS | frozenset({"python3"}), "project_root"),
        incentive="Maximize valid ticket throughput. Reject incomplete or duplicate tickets early.",
    ),
    AgentRole(
        name="conflict_resolver",
        category=RoleCategory.CONTROL,
        description="Detects and resolves merge conflicts between concurrent agents",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(STANDARD_TOOLS, STANDARD_COMMANDS, "project_root", git_write=True),
        incentive="Resolve conflicts with minimal information loss.",
    ),
    AgentRole(
        name="budget_controller",
        category=RoleCategory.CONTROL,
        description="Tracks token spend, enforces per-ticket and fleet-wide budgets, can pause agents exceeding limits",
        required_model=ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.INTERACTIVE),
        tool_policy=ToolPolicy(frozenset({"read", "write"}), frozenset({"python3"}), "state_dir"),
        incentive="Minimize cost per accepted ticket. Pause runaway agents.",
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
        name="implementation_planner",
        category=RoleCategory.PLANNING,
        description="Generates implementation plans for PLANNING tickets, enabling PLANNING -> IMPLEMENTING transitions",
        required_model=ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND),
        tool_policy=ToolPolicy(READ_ONLY_TOOLS | frozenset({"write"}), frozenset({"python3"}), "project_root"),
        incentive="Unblock the implementer fleet by generating plans for every PLANNING ticket.",
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


def find_adversarial_reviewers(implementer_role: str, ticket_id: str | None = None) -> list[AgentRole]:
    """Find reviewers adversarial to the given implementer role with structured error logging.
    
    Args:
        implementer_role: The implementer role name to find adversarial reviewers for
        ticket_id: Optional ticket ID for context in error logs
    
    Returns:
        List of adversarial reviewer roles, empty list on error
    """
    try:
        # Check if the implementer role exists first
        if implementer_role not in ROLE_REGISTRY:
            context = {"implementer_role": implementer_role}
            if ticket_id:
                context["ticket_id"] = ticket_id
            logger.warning(
                "Implementer role not found for adversarial lookup: implementer_role=%s, ticket_id=%s",
                implementer_role,
                ticket_id
            )
            return []
        
        reviewers = []
        for role in REVIEW_ROLES:
            if implementer_role in role.adversarial_to:
                reviewers.append(role)
        return reviewers
    except Exception as e:
        context = {"implementer_role": implementer_role}
        if ticket_id:
            context["ticket_id"] = ticket_id
        logger.error(
            "Finding adversarial reviewers failed: implementer_role=%s, ticket_id=%s, exception_type=%s, message=%s\nTraceback:\n%s",
            implementer_role,
            ticket_id,
            type(e).__name__,
            str(e),
            traceback.format_exc()
        )
        return []
