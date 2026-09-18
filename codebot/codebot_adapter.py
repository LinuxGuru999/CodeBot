#!/usr/bin/env python3
"""CodeBot self-hosting adapter — implements ProjectAdapter for CodeBot's own repository.

Purpose
-------
Allows CodeBot to manage its own codebase by providing the ProjectAdapter
interface with CodeBot-specific paths, components, roles, and model profiles.
This replaces the 3-bot fallback registry with the full 26-role system.

Why
---
CODEBOT-ROADMAP.md §17 requires CodeBot to work on itself under stronger
safeguards. This adapter is the bridge: it tells the portable core where
CodeBot's source lives, how to test it, which roles to schedule, and
which paths are constitution-protected.

Invariants
----------
- stdlib-only
- Never modifies source code
- Protected paths include orchestrator, gatekeeper, tool_policy, constitution
- Self-improvement autonomy level is 2 (lower than normal projects)
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from codebot.project_adapter import (
    ProjectAdapter,
    ProjectPaths,
    ProjectTestConfig,
    DependencyPolicy,
    AutonomyConfig,
    ComponentDef,
)


class CodeBotAdapter(ProjectAdapter):
    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or Path(__file__).parent.parent).resolve()

    def project_name(self) -> str:
        return "codebot"

    def paths(self) -> ProjectPaths:
        r = self._root
        return ProjectPaths(
            repository_root=r,
            state_dir=r / ".codebot" / "state",
            logs_dir=r / ".codebot" / "logs",
            docs_dir=r / "docs",
            issues_dir=r / "docs" / "issues",
            adr_dir=r / "docs" / "adr",
            modules_docs_dir=r / "docs" / "modules",
            queue_file=r / "docs" / "QUEUE.md",
            api_contract=r / ".codebot" / "api" / "contract.md",
            entrypoint=r / "codebot" / "__main__.py",
            context_map=r / ".codebot" / "CONTEXT-MAP.md",
            bugs_file=r / "docs" / "bugs.md",
            features_file=r / "docs" / "features.md",
            roadmap_file=r / "docs" / "GOALS.md",
            constitution_file=r / ".codebot" / "constitution.md",
            project_config=r / ".codebot" / "project.yaml",
        )

    def test_config(self) -> ProjectTestConfig:
        return ProjectTestConfig(
            framework="pytest",
            test_command="python3 -m pytest -q",
            test_directories=["tests/"],
            coverage_tool="pytest-cov",
            lint_tool="none",
            type_checker="none",
            type_check_command="",
        )

    def dependency_policy(self) -> DependencyPolicy:
        return DependencyPolicy(
            policy="stdlib-only",
            allowed_third_party=[
                {"name": "pytest", "scope": "dev", "justification": "Test runner"},
                {"name": "pytest-cov", "scope": "dev", "justification": "Coverage measurement"},
            ],
            dependency_files=["pyproject.toml"],
        )

    def autonomy_config(self) -> AutonomyConfig:
        return AutonomyConfig(
            level=2,
            human_approval_required_for=[
                "authentication_architecture",
                "authorization_boundaries",
                "cryptography",
                "destructive_migrations",
                "secrets",
                "security_policy_relaxation",
                "constitution_changes",
                "major_architecture_changes",
                "self_improvement_controls",
                "orchestrator_modifications",
                "gatekeeper_weakening",
            ],
            autonomous_allowed_for=[
                "documentation_corrections",
                "test_additions",
                "safe_refactors",
                "known_bug_fixes",
                "role_prompt_improvements",
                "coverage_improvements",
            ],
        )

    def components(self) -> list[ComponentDef]:
        return [
            ComponentDef("core", "codebot/", "backend", "python", "All portable CodeBot modules"),
            ComponentDef("tests", "tests/", "test", "python", "Unit and integration tests"),
            ComponentDef("docs", "docs/", "documentation", "markdown", "Architecture, API, deployment docs"),
            ComponentDef("roles", "codebot/roles/", "configuration", "markdown", "37 agent role prompt templates"),
        ]

    def bot_registry(self) -> list[dict[str, Any]]:
        from codebot.role_registry import ALL_ROLES
        registry = []
        interval_map = {
            "discovery": 1800,
            "planning": 3600,
            "implementation": 300,
            "review": 600,
            "control": 900,
        }
        for role in ALL_ROLES:
            interval = interval_map.get(role.category.value, 600)
            registry.append({
                "name": role.name,
                "prompt": f"codebot/roles/{role.name}.md",
                "interval": interval,
                "model": "default",
                "tier": 1 if role.category.value in ("discovery", "implementation") else 2,
                "enabled": True,
                "max_restarts": 5,
                "clean_exit_wait": True,
                "runner_mode": "api",
                "category": role.category.value,
            })
        return registry

    def model_profiles(self) -> dict[str, dict[str, Any]]:
        return {
            "default": {
                "lockup_risk": "low",
                "heartbeat_multiplier": 1.5,
                "log_stall_seconds": 120,
                "restart_cooldown": 5,
                "description": "Default model profile for CodeBot self-hosting",
            },
        }

    def tier_priority(self) -> dict[str, int]:
        from codebot.role_registry import ALL_ROLES
        priorities = {}
        category_tier = {
            "discovery": 11,
            "planning": 12,
            "implementation": 13,
            "review": 21,
            "control": 31,
        }
        for role in ALL_ROLES:
            priorities[role.name] = category_tier.get(role.category.value, 50)
        return priorities

    def prompt_directory(self) -> Path:
        return self._root / "codebot" / "roles"

    def api_runner_command(self, bot_name: str, prompt_file: str) -> list[str]:
        return [
            "python3", "-m", "codebot.api_runner",
            "--bot", bot_name,
            "--prompt", str(self.prompt_directory() / prompt_file),
        ]

    def is_protected_path(self, path: str) -> bool:
        protected = (
            ".codebot/constitution.md",
            "codebot/orchestrator.py",
            "codebot/gatekeeper.py",
            "codebot/tool_policy.py",
            "codebot/quality_gate.py",
            "codebot/prompt_gateway.py",
        )
        return any(path.startswith(p) or path == p for p in protected)

    def validate_project(self) -> list[str]:
        errors: list[str] = []
        p = self.paths()
        if not p.repository_root.exists():
            errors.append(f"repository root missing: {p.repository_root}")
        if not p.project_config.exists():
            errors.append(f"project config missing: {p.project_config}")
        if not p.constitution_file.exists():
            errors.append(f"constitution missing: {p.constitution_file}")
        core = p.repository_root / "codebot"
        if not core.exists():
            errors.append("codebot/ package directory missing")
        init = core / "__init__.py"
        if not init.exists():
            errors.append("codebot/__init__.py missing")
        tests = p.repository_root / "tests"
        if not tests.exists():
            errors.append("tests/ directory missing")
        roles = p.repository_root / "codebot" / "roles"
        if not roles.exists():
            errors.append("codebot/roles/ directory missing")
        else:
            role_count = len(list(roles.glob("*.md")))
            if role_count < 20:
                errors.append(f"only {role_count} role prompts found, expected >= 20")
        return errors
