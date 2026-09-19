"""Tests for role_registry.py — model satisfaction, tool policy enforcement, adversarial lookup."""

import pytest
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from codebot.role_registry import (
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
    PLANNING_ROLES,
    STANDARD_TOOLS,
    READ_ONLY_TOOLS,
    RESEARCH_TOOLS,
    UX_AUDIT_TOOLS,
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
            if role.name == "ux_auditor":
                assert role.tool_policy.allowed_tools == UX_AUDIT_TOOLS
            elif role.name in ("bug_hunter", "security_auditor", "architecture_auditor",
                               "performance_auditor", "test_gap_auditor", "dependency_auditor"):
                assert role.tool_policy.allowed_tools == RESEARCH_TOOLS
            else:
                assert role.tool_policy.allowed_tools == READ_ONLY_TOOLS

    def test_implementation_roles_have_git_write(self):
        for role in IMPLEMENTATION_ROLES:
            assert role.tool_policy.git_write is True

    def test_review_roles_read_only(self):
        for role in REVIEW_ROLES:
            if role.name == "ux_reviewer":
                # ux_reviewer needs screenshot/a11y_snapshot for visual review
                assert role.tool_policy.allowed_tools == UX_AUDIT_TOOLS
            else:
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

    def test_total_role_count(self):
        """Total roles should be 29 (8 discovery + 6 implementation + 8 review + 6 control + 1 planning)."""
        assert len(ALL_ROLES) == 29

    def test_planning_role_exists(self):
        assert len(PLANNING_ROLES) >= 1


class TestNewRoles:
    def test_ux_reviewer_exists(self):
        role = get_role("ux_reviewer")
        assert role is not None
        assert role.category == RoleCategory.REVIEW
        assert role.tool_policy.allowed_tools == UX_AUDIT_TOOLS

    def test_ticket_triager_exists(self):
        role = get_role("ticket_triager")
        assert role is not None
        assert role.category == RoleCategory.CONTROL
        assert "write" in role.tool_policy.allowed_tools

    def test_frontend_implementer_cost_class(self):
        """frontend_implementer should be STANDARD, not CHEAP, to match ADVANCED coding."""
        role = get_role("frontend_implementer")
        assert role.required_model.cost_class == CostClass.STANDARD
        assert role.required_model.reasoning == ReasoningLevel.MEDIUM

    def test_test_gap_auditor_reasoning(self):
        """test_gap_auditor should be MEDIUM reasoning for coverage analysis."""
        role = get_role("test_gap_auditor")
        assert role.required_model.reasoning == ReasoningLevel.MEDIUM

    def test_documentation_implementer_reasoning(self):
        """documentation_implementer should be MEDIUM reasoning for accurate docs."""
        role = get_role("documentation_implementer")
        assert role.required_model.reasoning == ReasoningLevel.MEDIUM
        assert role.required_model.cost_class == CostClass.STANDARD

    def test_budget_controller_can_write(self):
        """budget_controller needs write access to pause/throttle agents."""
        role = get_role("budget_controller")
        assert "write" in role.tool_policy.allowed_tools

    def test_ux_auditor_has_npm(self):
        """ux_auditor should have npm/npx for linting tools."""
        role = get_role("ux_auditor")
        assert "npm" in role.tool_policy.allowed_commands
        assert "npx" in role.tool_policy.allowed_commands


class TestAdversarialReviewExtended:
    def test_frontend_implementer_has_ux_reviewer(self):
        """frontend_implementer should be reviewed by ux_reviewer."""
        reviewers = find_adversarial_reviewers("frontend_implementer")
        names = [r.name for r in reviewers]
        assert "ux_reviewer" in names

    def test_test_implementer_has_test_reviewer(self):
        """test_implementer should be reviewed by test_reviewer."""
        reviewers = find_adversarial_reviewers("test_implementer")
        names = [r.name for r in reviewers]
        assert "test_reviewer" in names

    def test_migration_implementer_has_reviewers(self):
        """migration_implementer should have correctness and security reviewers."""
        reviewers = find_adversarial_reviewers("migration_implementer")
        names = [r.name for r in reviewers]
        assert "correctness_reviewer" in names
        assert "security_reviewer" in names

    def test_architecture_reviewer_adversarial_to_simplicity(self):
        """architecture_reviewer should challenge simplicity_reviewer."""
        role = get_role("architecture_reviewer")
        assert "simplicity_reviewer" in role.adversarial_to

    def test_security_reviewer_adversarial_to_architecture(self):
        """security_reviewer should challenge architecture_reviewer."""
        role = get_role("security_reviewer")
        assert "architecture_reviewer" in role.adversarial_to

    def test_general_implementer_declares_reviewers(self):
        """general_implementer should declare which reviewers it's adversarial to."""
        role = get_role("general_implementer")
        assert "correctness_reviewer" in role.adversarial_to
        assert "security_reviewer" in role.adversarial_to
        assert "architecture_reviewer" in role.adversarial_to

    def test_backend_implementer_declares_reviewers(self):
        """backend_implementer should declare which reviewers it's adversarial to."""
        role = get_role("backend_implementer")
        assert "correctness_reviewer" in role.adversarial_to
        assert "security_reviewer" in role.adversarial_to
        assert "architecture_reviewer" in role.adversarial_to

    def test_ux_reviewer_has_npm(self):
        """ux_reviewer should have npm/npx for consistency with ux_auditor."""
        role = get_role("ux_reviewer")
        assert "npm" in role.tool_policy.allowed_commands
        assert "npx" in role.tool_policy.allowed_commands

    def test_to_dict_includes_max_file_size(self):
        """to_dict() should include max_file_size_bytes for completeness."""
        role = get_role("bug_hunter")
        d = role.to_dict()
        assert "max_file_size_bytes" in d["tool_policy"]
        assert d["tool_policy"]["max_file_size_bytes"] == 1_000_000

    def test_all_adversarial_references_are_valid(self):
        """All adversarial_to references must point to existing roles."""
        for role in ALL_ROLES:
            for ref in role.adversarial_to:
                assert ref in ROLE_REGISTRY, (
                    f"{role.name}.adversarial_to references '{ref}' which does not exist in ROLE_REGISTRY"
                )
