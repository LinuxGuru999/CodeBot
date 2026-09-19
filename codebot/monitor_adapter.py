#!/usr/bin/env python3
"""Monitor project adapter — implements ProjectAdapter for the Monitor platform.

Purpose
-------
Translates all Monitor-specific paths, configurations, bot registries, and
model profiles into the standardized ProjectAdapter interface. This is the
ONLY file in the CodeBot core that contains Monitor-specific knowledge.

Why
---
Per CODEBOT-ROADMAP.md §18 step 3-4: Monitor-specific assumptions must move
behind a project profile. After this adapter exists, the orchestrator and
all core modules reference adapter.paths() instead of hardcoded paths,
adapter.bot_registry() instead of BOT_REGISTRY, etc.

Invariants
----------
- This file lives in the Monitor repository, NOT in the extracted CodeBot repo
- Contains all Monitor-specific domain knowledge
- If CodeBot core references anything Monitor-specific directly, it's a bug
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


MONITOR_ROOT = Path.home() / "Work"


class MonitorAdapter(ProjectAdapter):
    def project_name(self) -> str:
        return "monitor"

    def paths(self) -> ProjectPaths:
        root = MONITOR_ROOT
        return ProjectPaths(
            repository_root=root,
            state_dir=root / "bots" / "state",
            logs_dir=root / "bots" / "logs",
            docs_dir=root / "docs",
            issues_dir=root / "docs" / "issues",
            adr_dir=root / "docs" / "adr",
            modules_docs_dir=root / "docs" / "modules",
            queue_file=root / "bots" / "QUEUE.md",
            api_contract=root / "API_CONTRACT.md",
            entrypoint=root / "ENTRYPOINT.md",
            context_map=root / "CONTEXT-MAP.md",
            bugs_file=root / "BUGS.md",
            features_file=root / "FEATURES.md",
            roadmap_file=root / "ROADMAP.md",
            constitution_file=root / ".codebot" / "constitution.md",
            project_config=root / ".codebot" / "project.yaml",
        )

    def test_config(self) -> ProjectTestConfig:
        return ProjectTestConfig(
            framework="pytest",
            test_command="python3 -m pytest -q",
            test_directories=[
                "Monitor-Manager-Python/tests/",
                "Monitor-Client-Python/tests/",
                "lib-common/tests/",
                "bots/tests/",
            ],
            coverage_tool="pytest-cov",
            lint_tool="none",
            type_checker="mypy",
            type_check_command="mypy --config-file mypy.ini",
        )

    def dependency_policy(self) -> DependencyPolicy:
        return DependencyPolicy(
            policy="stdlib-only",
            allowed_third_party=[
                {"name": "argon2-cffi", "scope": "manager", "justification": "Password hashing"},
                {"name": "pytest", "scope": "dev", "justification": "Test runner"},
            ],
            dependency_files=[
                "Monitor-Manager-Python/pyproject.toml",
                "Monitor-Client-Python/pyproject.toml",
                "lib-common/pyproject.toml",
            ],
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
            ],
            autonomous_allowed_for=[
                "documentation_corrections",
                "test_additions",
                "lint_fixes",
                "safe_refactors",
                "known_bug_fixes",
                "low_risk_ui_corrections",
            ],
        )

    def components(self) -> list[ComponentDef]:
        return [
            ComponentDef("manager", "Monitor-Manager-Python/", "backend", "python", "Central manager"),
            ComponentDef("client", "Monitor-Client-Python/", "agent", "python", "Per-machine agent"),
            ComponentDef("lib-common", "lib-common/", "shared_kernel", "python", "Canonical shared library"),
            ComponentDef("botnet", "bots/", "autonomous_improvement", "python", "AI agent fleet"),
            ComponentDef("frontend", "Monitor-Manager-Python/static_manager/", "frontend", "javascript", "Admin console"),
        ]

    def bot_registry(self) -> list[dict[str, Any]]:
        return [
            {"name": "issues", "prompt": "ISSUES_BOT.md", "interval": 300, "model": "xiaomi-mimo-2.5", "tier": 1},
            {"name": "bug_triage", "prompt": "BUG_TRIAGE_BOT.md", "interval": 600, "model": "qwen-3.6-plus", "tier": 1},
            {"name": "build", "prompt": "BUILD_BOT.md", "interval": 300, "model": "xiaomi-mimo-2.5", "tier": 1},
            {"name": "code_quality", "prompt": "CODE_QUALITY_BOT.md", "interval": 600, "model": "qwen-3.7-plus", "tier": 2},
            {"name": "security_auditor", "prompt": "SECURITY_AUDITOR_BOT.md", "interval": 600, "model": "qwen-3.8-max", "tier": 2},
            {"name": "e2e_smoke", "prompt": "E2E_SMOKE_BOT.md", "interval": 600, "model": "qwen-3.7-plus", "tier": 2},
            {"name": "test_coverage", "prompt": "TEST_COVERAGE_BOT.md", "interval": 600, "model": "xiaomi-mimo-2.5", "tier": 2},
            {"name": "gitsync", "prompt": "GITSYNC_BOT.md", "interval": 600, "model": "xiaomi-mimo-2.5", "tier": 1},
            {"name": "github_bot", "prompt": "GITHUB_BOT.md", "interval": 600, "model": "xiaomi-mimo-2.5", "tier": 1},
            {"name": "doc_sync", "prompt": "DOC_SYNC_BOT.md", "interval": 900, "model": "xiaomi-mimo-2.5", "tier": 3},
            {"name": "dependency", "prompt": "DEPENDENCY_BOT.md", "interval": 3600, "model": "xiaomi-mimo-2.5", "tier": 3},
            {"name": "features", "prompt": "FEATURE_BOT.md", "interval": 600, "model": "qwen-3.7-plus", "tier": 2},
            {"name": "decomposer", "prompt": "FEATURE_DECOMPOSER_BOT.md", "interval": 300, "model": "xiaomi-mimo-2.5", "tier": 1},
            {"name": "goal_steering", "prompt": "GOAL_STEERING_BOT.md", "interval": 600, "model": "qwen-3.8-max", "tier": 2},
            {"name": "ui_improve", "prompt": "UI_IMPROVE_BOT.md", "interval": 600, "model": "qwen-3.7-plus", "tier": 2},
            {"name": "release", "prompt": "RELEASE_BOT.md", "interval": 3600, "model": "qwen-3.8-max", "tier": 3, "enabled": False},
            {"name": "prompt_opt", "prompt": "PROMPT_OPTIMIZER.md", "interval": 1800, "model": "qwen-3.8-max-thinking", "tier": 3},
            {"name": "alignment", "prompt": "ALIGNMENT_BOT.md", "interval": 1800, "model": "qwen-3.8-max", "tier": 3},
            {"name": "worker-1", "prompt": "WORKER_BOT.md", "interval": 300, "model": "xiaomi-mimo-2.5", "tier": 1},
            {"name": "worker-2", "prompt": "WORKER_BOT.md", "interval": 300, "model": "xiaomi-mimo-2.5", "tier": 1},
            {"name": "worker-3", "prompt": "WORKER_BOT.md", "interval": 300, "model": "xiaomi-mimo-2.5", "tier": 1},
            {"name": "worker-4", "prompt": "WORKER_BOT.md", "interval": 300, "model": "xiaomi-mimo-2.5", "tier": 1},
            {"name": "worker-5", "prompt": "WORKER_BOT.md", "interval": 300, "model": "qwen-3.8-max", "tier": 1},
            {"name": "worker-6", "prompt": "WORKER_BOT.md", "interval": 300, "model": "qwen-3.8-max", "tier": 1},
            {"name": "worker-7", "prompt": "WORKER_BOT.md", "interval": 300, "model": "qwen-3.7-plus", "tier": 1},
            {"name": "worker-8", "prompt": "WORKER_BOT.md", "interval": 300, "model": "qwen-3.7-plus", "tier": 1},
        ]

    def model_profiles(self) -> dict[str, dict[str, Any]]:
        return {
            "qwen-3.8-max-thinking": {"lockup_risk": "high", "heartbeat_multiplier": 2.8, "log_stall_seconds": 280, "restart_cooldown": 12},
            "qwen-3.7-max-thinking": {"lockup_risk": "high", "heartbeat_multiplier": 2.5, "log_stall_seconds": 240, "restart_cooldown": 10},
            "qwen-3.6-plus-thinking": {"lockup_risk": "medium-high", "heartbeat_multiplier": 2.3, "log_stall_seconds": 210, "restart_cooldown": 9},
            "qwen-3.5-plus-thinking": {"lockup_risk": "medium-high", "heartbeat_multiplier": 2.2, "log_stall_seconds": 200, "restart_cooldown": 8},
            "qwen-3.8-max": {"lockup_risk": "medium-high", "heartbeat_multiplier": 2.0, "log_stall_seconds": 180, "restart_cooldown": 8},
            "qwen-3.7-plus": {"lockup_risk": "medium", "heartbeat_multiplier": 1.8, "log_stall_seconds": 150, "restart_cooldown": 6},
            "qwen-3.6-plus": {"lockup_risk": "medium", "heartbeat_multiplier": 1.8, "log_stall_seconds": 150, "restart_cooldown": 6},
            "qwen-3.5-plus": {"lockup_risk": "medium", "heartbeat_multiplier": 1.7, "log_stall_seconds": 140, "restart_cooldown": 6},
            "qwen-3.4-plus": {"lockup_risk": "medium-low", "heartbeat_multiplier": 1.5, "log_stall_seconds": 120, "restart_cooldown": 5},
            "qwen-3.3-plus": {"lockup_risk": "medium-low", "heartbeat_multiplier": 1.5, "log_stall_seconds": 120, "restart_cooldown": 5},
            "xiaomi-mimo-2.5": {"lockup_risk": "low", "heartbeat_multiplier": 1.3, "log_stall_seconds": 90, "restart_cooldown": 4},
            "qwen-3.8-max-fast": {"lockup_risk": "low", "heartbeat_multiplier": 1.3, "log_stall_seconds": 90, "restart_cooldown": 4},
        }

    def tier_priority(self) -> dict[str, int]:
        return {
            "issues": 11, "bug_triage": 11, "build": 11, "github_bot": 11, "decomposer": 11,
            "gitsync": 12,
            "worker-1": 12, "worker-2": 12, "worker-3": 12, "worker-4": 12, "worker-7": 12, "worker-8": 12,
            "worker-5": 13, "worker-6": 13, "worker-9": 13, "worker-10": 13, "worker-11": 13, "worker-12": 13,
            "github_issues": 99,
            "features": 21, "ui_improve": 21, "e2e_smoke": 21,
            "test_coverage": 22, "code_quality": 22, "security_auditor": 22, "goal_steering": 22,
            "doc_sync": 31, "dependency": 31,
            "prompt_opt": 99, "release": 99, "alignment": 31,
        }

    def prompt_directory(self) -> Path:
        return MONITOR_ROOT / "bots"

    def api_runner_command(self, bot_name: str, prompt_file: str) -> list[str]:
        return [
            "python3", str(MONITOR_ROOT / "bots" / "api_runner.py"),
            "--bot", bot_name,
            "--prompt", str(self.prompt_directory() / prompt_file),
        ]

    def is_protected_path(self, path: str) -> bool:
        protected_prefixes = (
            ".codebot/constitution.md",
            "AGENTS.md",
            "lib-common/lib_common/store.py",
        )
        return any(path.startswith(p) for p in protected_prefixes)

    def validate_project(self) -> list[str]:
        errors: list[str] = []
        p = self.paths()
        if not p.repository_root.exists():
            errors.append(f"repository root missing: {p.repository_root}")
        if not p.project_config.exists():
            errors.append(f"project config missing: {p.project_config}")
        if not p.constitution_file.exists():
            errors.append(f"constitution missing: {p.constitution_file}")
        for comp in self.components():
            comp_path = p.repository_root / comp.path
            if not comp_path.exists():
                errors.append(f"component path missing: {comp.path}")
        return errors
