"""Tests for role_registry.py — model satisfaction, tool policy enforcement, adversarial lookup."""

import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from role_registry import (
    ModelProfile,
    ToolPolicy,
    AgentRole,
    RoleCategory,
    ReasoningLevel,
    CodingLevel,
    ContextSize,
    CostClass,
    LatencyClass,
    get_role,
    roles_by_category,
    find_adversarial_reviewers,
    ALL_ROLES,
    ROLE_REGISTRY,
    DISCOVERY_ROLES,
    IMPLEMENTATION_ROLES,
    REVIEW_ROLES,
    CONTROL_ROLES,
    STANDARD_TOOLS,
    READ_ONLY_TOOLS,
)


class TestModelProfileSatisfaction:
    def test_equal_satisfies(self):
        m = ModelProfile(ReasoningLevel.MEDIUM, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.STANDARD, LatencyClass.BACKGROUND)
        assert m.satisfies(m) is True

    def test_higher_reasoning_satisfies_lower(self):
        high = ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND)
        low = ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.INTERACTIVE)
        assert high.satisfies(low) is True

    def test_lower_reasoning_fails_higher(self):
        low = ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.INTERACTIVE)
        high = ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND)
        assert low.satisfies(high) is False

    def test_security_review_requirement(self):
        no_sec = ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND, security_review=False)
        needs_sec = ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.BACKGROUND, security_review=True)
        assert no_sec.satisfies(needs_sec) is False

    def test_security_review_satisfied(self):
        has_sec = ModelProfile(ReasoningLevel.HIGH, CodingLevel.ADVANCED, ContextSize.LARGE, CostClass.PREMIUM, LatencyClass.BACKGROUND, security_review=True)
        needs_sec = ModelProfile(ReasoningLevel.LOW, CodingLevel.BASIC, ContextSize.SMALL, CostClass.CHEAP, LatencyClass.BACKGROUND, security_review=True)
        assert has_sec.satisfies(needs_sec) is True


class TestToolPolicy:
    def test_allows_known_tool(self):
        tp = ToolPolicy(frozenset({"read", "write"}), frozenset({"python3"}), "project_root")
        assert tp.allows_tool("read") is True

    def test_denies_unknown_tool(self):
        tp = ToolPolicy(frozenset({"read"}), frozenset({"python3"}), "project_root")
        assert tp.allows_tool("bash") is False

    def test_allows_known_command(self):
        tp = ToolPolicy(STANDARD_TOOLS, frozenset({"python3", "pytest"}), "project_root")
        assert tp.allows_command("python3 script.py") is True

    def test_denies_unknown_command(self):
        tp = ToolPolicy(STANDARD_TOOLS, frozenset({"python3"}), "project_root")
        assert tp.allows_command("rm -rf /") is False

    def test_empty_command_denied(self):
        tp = ToolPolicy(STANDARD_TOOLS, frozenset({"python3"}), "project_root")
        assert tp.allows_command("") is False


class TestRoleRegistry:
    def test_all_roles_registered(self):
        assert len(ROLE_REGISTRY) == len(ALL_ROLES)

    def test_get_existing_role(self):
        role = get_role("bug_hunter")
        assert role is not None
        assert role.category == RoleCategory.DISCOVERY

    def test_get_nonexistent_role(self):
        assert get_role("nonexistent_bot") is None

    def test_discovery_roles_read_only(self):
        for role in DISCOVERY_ROLES:
            assert role.tool_policy.allowed_tools == READ_ONLY_TOOLS

    def test_implementation_roles_have_git_write(self):
        for role in IMPLEMENTATION_ROLES:
            assert role.tool_policy.git_write is True

    def test_review_roles_read_only(self):
        for role in REVIEW_ROLES:
            assert role.tool_policy.allowed_tools == READ_ONLY_TOOLS

    def test_roles_by_category(self):
        discovery = roles_by_category(RoleCategory.DISCOVERY)
        assert len(discovery) == len(DISCOVERY_ROLES)
        assert all(r.category == RoleCategory.DISCOVERY for r in discovery)

    def test_every_role_has_incentive(self):
        for role in ALL_ROLES:
            assert role.incentive, f"{role.name} missing incentive"


class TestAdversarialReview:
    def test_backend_implementer_has_reviewers(self):
        reviewers = find_adversarial_reviewers("backend_implementer")
        assert len(reviewers) >= 2
        names = [r.name for r in reviewers]
        assert "security_reviewer" in names
        assert "correctness_reviewer" in names

    def test_documentation_implementer_has_reviewer(self):
        reviewers = find_adversarial_reviewers("documentation_implementer")
        names = [r.name for r in reviewers]
        assert "documentation_reviewer" in names

    def test_no_reviewers_for_scheduler(self):
        reviewers = find_adversarial_reviewers("scheduler")
        assert len(reviewers) == 0

    def test_reviewers_have_conflicting_incentives(self):
        reviewers = find_adversarial_reviewers("general_implementer")
        for r in reviewers:
            assert "general_implementer" in r.adversarial_to


class TestRoleCounts:
    def test_minimum_discovery_roles(self):
        assert len(DISCOVERY_ROLES) >= 5

    def test_minimum_implementation_roles(self):
        assert len(IMPLEMENTATION_ROLES) >= 4

    def test_minimum_review_roles(self):
        assert len(REVIEW_ROLES) >= 5

    def test_minimum_control_roles(self):
        assert len(CONTROL_ROLES) >= 3
