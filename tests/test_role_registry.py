"""Tests for codebot.role_registry OCP-derived role sets."""
from __future__ import annotations

import logging

import pytest

from codebot import role_registry
from codebot.role_registry import (
    ALL_ROLES,
    AgentRole,
    CONTROL_ROLE_NAMES,
    CodingLevel,
    ContextSize,
    CostClass,
    DISCOVERY_ROLE_NAMES,
    IMPLEMENTATION_ROLE_ORDER,
    IMPLEMENTER_ROLE_NAMES,
    LatencyClass,
    ModelProfile,
    PLANNING_ROLE_NAMES,
    ReasoningLevel,
    REVIEWER_ROLE_NAMES,
    RoleCategory,
    ToolPolicy,
    get_implementer_for_class,
    get_role,
    roles_by_category,
)


def _profile(
    reasoning: ReasoningLevel = ReasoningLevel.HIGH,
    coding: CodingLevel = CodingLevel.ADVANCED,
    context: ContextSize = ContextSize.LARGE,
    cost: CostClass = CostClass.PREMIUM,
    latency: LatencyClass = LatencyClass.BACKGROUND,
    security_review: bool = False,
) -> ModelProfile:
    return ModelProfile(
        reasoning=reasoning,
        coding=coding,
        context=context,
        cost_class=cost,
        latency=latency,
        security_review=security_review,
    )


class _ExplodingRegistry(dict):
    def get(self, key, default=None):
        raise RuntimeError("registry failure")


def test_all_roles_are_unique_and_registered():
    names = [r.name for r in ALL_ROLES]
    assert len(names) == len(set(names))
    assert set(names) == set(role_registry.ROLE_REGISTRY)


def test_derived_role_name_sets_match_categories():
    assert IMPLEMENTER_ROLE_NAMES == frozenset(
        r.name for r in ALL_ROLES if r.category == RoleCategory.IMPLEMENTATION
    )
    assert DISCOVERY_ROLE_NAMES == frozenset(
        r.name for r in ALL_ROLES if r.category == RoleCategory.DISCOVERY
    )
    assert REVIEWER_ROLE_NAMES == frozenset(
        r.name for r in ALL_ROLES if r.category == RoleCategory.REVIEW
    )
    assert PLANNING_ROLE_NAMES == frozenset(
        r.name for r in ALL_ROLES if r.category == RoleCategory.PLANNING
    )
    assert CONTROL_ROLE_NAMES == frozenset(
        r.name for r in ALL_ROLES if r.category == RoleCategory.CONTROL
    )


def test_implementation_order_is_derived_not_hardcoded():
    assert IMPLEMENTATION_ROLE_ORDER == tuple(r.name for r in role_registry.IMPLEMENTATION_ROLES)
    expected_default = IMPLEMENTATION_ROLE_ORDER[0] if IMPLEMENTATION_ROLE_ORDER else ""
    assert role_registry._DEFAULT_IMPLEMENTER_ROLE == expected_default


def test_get_implementer_for_class_uses_registry_default():
    expected = role_registry._DEFAULT_IMPLEMENTER_ROLE
    for ticket_class in ("bug", "feature", "architecture", "unknown"):
        assert get_implementer_for_class(ticket_class) == expected


def test_roles_by_category_filters():
    impl = roles_by_category(RoleCategory.IMPLEMENTATION)
    assert all(r.category == RoleCategory.IMPLEMENTATION for r in impl)
    assert {r.name for r in impl} == IMPLEMENTER_ROLE_NAMES


def test_get_role_found_and_missing(caplog):
    with caplog.at_level(logging.WARNING):
        role = get_role("implementer")
        assert role is not None
        assert role.name == "implementer"

        missing = get_role("not-a-role", ticket_id="CB-1")
        assert missing is None
    assert "Role not found" in caplog.text
    assert "CB-1" in caplog.text


def test_get_role_logs_exception(monkeypatch, caplog):
    monkeypatch.setattr(role_registry, "ROLE_REGISTRY", _ExplodingRegistry())
    with caplog.at_level(logging.ERROR):
        assert get_role("implementer", ticket_id="CB-2") is None
    assert "Role lookup failed" in caplog.text
    assert "CB-2" in caplog.text
    assert "registry failure" in caplog.text


def test_model_profile_satisfies_all_dimensions():
    strong = _profile()
    weak = _profile(
        reasoning=ReasoningLevel.LOW,
        coding=CodingLevel.BASIC,
        context=ContextSize.SMALL,
        cost=CostClass.CHEAP,
        latency=LatencyClass.INTERACTIVE,
    )
    assert strong.satisfies(weak)
    assert not weak.satisfies(strong)


def test_model_profile_satisfies_reasoning_failure():
    assert not _profile(reasoning=ReasoningLevel.LOW).satisfies(_profile(reasoning=ReasoningLevel.MEDIUM))


def test_model_profile_satisfies_coding_failure():
    assert not _profile(coding=CodingLevel.BASIC).satisfies(_profile(coding=CodingLevel.ADVANCED))


def test_model_profile_satisfies_context_failure():
    assert not _profile(context=ContextSize.SMALL).satisfies(_profile(context=ContextSize.LARGE))


def test_model_profile_satisfies_security_failure():
    assert not _profile(security_review=False).satisfies(_profile(security_review=True))
    assert _profile(security_review=True).satisfies(_profile(security_review=False))


def test_tool_policy_allows_tool_and_command():
    policy = ToolPolicy(
        allowed_tools=frozenset({"read", "grep"}),
        allowed_commands=frozenset({"python3", "git"}),
        filesystem_scope="project_root",
    )
    assert policy.allows_tool("read")
    assert not policy.allows_tool("write")
    assert policy.allows_command("python3 -m pytest")
    assert policy.allows_command("git status")
    assert not policy.allows_command("rm -rf /")
    assert not policy.allows_command("")


def test_agent_role_to_dict_roundtrips_shape():
    role = get_role("security_auditor")
    assert isinstance(role, AgentRole)
    data = role.to_dict()
    assert data["name"] == "security_auditor"
    assert data["category"] == RoleCategory.DISCOVERY.value
    assert data["adversarial_to"] == ["implementer"]
    assert data["required_model"]["security_review"] is True
    assert "read" in data["tool_policy"]["allowed_tools"]
    assert data["tool_policy"]["filesystem_scope"] == "project_root"


def test_role_dataclasses_are_frozen():
    role = get_role("implementer")
    assert isinstance(role, AgentRole)
    with pytest.raises(Exception):
        role.name = "mutated"
