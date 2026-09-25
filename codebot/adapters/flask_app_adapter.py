#!/usr/bin/env python3
"""Flask App adapter — reference second-project adapter for portability validation.

Purpose
-------
Demonstrates CodeBot's ability to manage an unrelated Flask web application
without any Monitor or CodeBot-specific assumptions. This adapter serves as
the "second project" required by ROADMAP.md §2.A to prove portability.

Why
---
Without a second adapter, we cannot verify that CodeBot core is truly
project-agnostic. This adapter represents a typical external codebase:
standard Python web app with REST API, database models, tests, and docs.

Invariants
----------
- stdlib-only (no Flask dependency in adapter itself)
- No references to Monitor, CodeBot roles, or internal state paths
- All paths relative to the Flask app repository root
- Protected paths limited to Flask app's own config/secrets
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


class FlaskAppAdapter(ProjectAdapter):
    """Reference adapter for a generic Flask web application."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = (root or Path.cwd()).resolve()

    def project_name(self) -> str:
        return "flask-demo-app"

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
            api_contract=r / "docs" / "api" / "openapi.yaml",
            entrypoint=r / "app" / "main.py",
            context_map=r / "docs" / "CONTEXT-MAP.md",
            bugs_file=r / "docs" / "bugs.md",
            features_file=r / "docs" / "features.md",
            roadmap_file=r / "docs" / "roadmap.md",
            constitution_file=r / ".codebot" / "constitution.md",
            project_config=r / ".codebot" / "project.yaml",
        )

    def test_config(self) -> ProjectTestConfig:
        return ProjectTestConfig(
            framework="pytest",
            test_command="python3 -m pytest -q --tb=short",
            test_directories=["tests/", "app/tests/"],
            coverage_tool="coverage",
            lint_tool="ruff",
            type_checker="mypy",
            type_check_command="python3 -m mypy app/",
        )

    def dependency_policy(self) -> DependencyPolicy:
        return DependencyPolicy(
            policy="requirements-file",
            allowed_third_party=[
                {"name": "flask", "scope": "runtime", "justification": "Web framework"},
                {"name": "sqlalchemy", "scope": "runtime", "justification": "ORM"},
                {"name": "pytest", "scope": "dev", "justification": "Test runner"},
                {"name": "coverage", "scope": "dev", "justification": "Coverage measurement"},
                {"name": "ruff", "scope": "dev", "justification": "Linter"},
                {"name": "mypy", "scope": "dev", "justification": "Type checker"},
            ],
            dependency_files=["requirements.txt", "requirements-dev.txt"],
        )

    def autonomy_config(self) -> AutonomyConfig:
        return AutonomyConfig(
            level=3,
            human_approval_required_for=[
                "database_migrations",
                "authentication_changes",
                "payment_logic",
                "secrets",
                "production_deployment",
            ],
            autonomous_allowed_for=[
                "test_additions",
                "documentation_updates",
                "safe_refactors",
                "bug_fixes_with_tests",
                "dependency_updates",
                "linting_fixes",
            ],
        )

    def components(self) -> list[ComponentDef]:
        return [
            ComponentDef("api", "app/api/", "backend", "python", "REST API endpoints and views"),
            ComponentDef("models", "app/models/", "backend", "python", "SQLAlchemy database models"),
            ComponentDef("services", "app/services/", "backend", "python", "Business logic layer"),
            ComponentDef("tests", "tests/", "test", "python", "Unit and integration tests"),
            ComponentDef("docs", "docs/", "documentation", "markdown", "API docs and architecture"),
            ComponentDef("config", "app/config/", "configuration", "python", "App configuration"),
        ]

    def bot_registry(self) -> list[dict[str, Any]]:
        # Generic roles suitable for a standard web app
        return [
            {
"name": "implementer",
"prompt": "roles/implementer.md",
                "interval": 300,
                "model": "qwen-3.7-plus",
                "fallback_model": "qwen-3.6-plus",
                "tier": 1,
                "enabled": True,
                "max_restarts": 3,
                "clean_exit_wait": True,
                "runner_mode": "api",
                "category": "implementation",
            },
            {
"name": "implementer",
"prompt": "roles/implementer.md",
                "interval": 300,
                "model": "qwen-3.6-plus",
                "fallback_model": "qwen-3.5-plus",
                "tier": 1,
                "enabled": True,
                "max_restarts": 3,
                "clean_exit_wait": True,
                "runner_mode": "api",
                "category": "implementation",
            },
            {
                "name": "security_reviewer",
                "prompt": "roles/security_reviewer.md",
                "interval": 600,
                "model": "qwen-3.7-max-thinking",
                "fallback_model": "qwen-3.6-plus-thinking",
                "tier": 2,
                "enabled": True,
                "max_restarts": 2,
                "clean_exit_wait": True,
                "runner_mode": "api",
                "category": "review",
            },
            {
                "name": "bug_hunter",
                "prompt": "roles/bug_hunter.md",
                "interval": 1800,
                "model": "qwen-3.5-plus",
                "fallback_model": "xiaomi-mimo-2.5",
                "tier": 1,
                "enabled": True,
                "max_restarts": 3,
                "clean_exit_wait": True,
                "runner_mode": "api",
                "category": "discovery",
            },
        ]

    def model_profiles(self) -> dict[str, dict[str, Any]]:
        return {
            "default": {
                "lockup_risk": "low",
                "heartbeat_multiplier": 1.5,
                "log_stall_seconds": 180,
                "restart_cooldown": 10,
                "description": "Default profile for Flask app workers",
            },
            "thinking": {
                "lockup_risk": "medium",
                "heartbeat_multiplier": 2.0,
                "log_stall_seconds": 300,
                "restart_cooldown": 15,
                "description": "Extended timeout for reasoning-heavy reviews",
            },
        }

    def tier_priority(self) -> dict[str, int]:
        return {
            "implementer": 13,
            "security_reviewer": 21,
            "bug_hunter": 11,
        }

    def prompt_directory(self) -> Path:
        return self._root / "roles"

    def api_runner_command(self, bot_name: str, prompt_file: str) -> list[str]:
        return [
            "python3", "-m", "codebot.api_runner",
            "--bot", bot_name,
            "--prompt", str(self.prompt_directory() / prompt_file),
        ]

    def is_protected_path(self, path: str) -> bool:
        protected = (
            ".codebot/constitution.md",
            "app/config/settings.py",
            "app/config/secrets.py",
            "migrations/",
            ".env",
        )
        return any(path.startswith(p) or path == p for p in protected)

    def validate_project(self) -> list[str]:
        errors: list[str] = []
        p = self.paths()
        if not p.repository_root.exists():
            errors.append(f"repository root missing: {p.repository_root}")
        app_dir = p.repository_root / "app"
        if not app_dir.exists():
            errors.append("app/ directory missing")
        main_py = app_dir / "main.py"
        if not main_py.exists():
            errors.append("app/main.py entrypoint missing")
        tests_dir = p.repository_root / "tests"
        if not tests_dir.exists():
            errors.append("tests/ directory missing")
        req_file = p.repository_root / "requirements.txt"
        if not req_file.exists():
            errors.append("requirements.txt missing")
        return errors

    def queue_depth(self) -> int:
        """FlaskApp adapter stub — no TicketStore access."""
        return 0

    def ticket_class_counts(self) -> dict[str, int]:
        """FlaskApp adapter stub — no TicketStore access."""
        return {}
