#!/usr/bin/env python3
"""Project adapter interface for CodeBot portability.

Purpose
-------
Defines the abstract interface that every CodeBot-managed project must
implement. The CodeBot core (orchestrator, runner, RL engine, ticket engine,
quality gates) operates exclusively through this interface, never through
hardcoded paths or project-specific knowledge.

Why
---
CODEBOT-ROADMAP.md §18 requires that CodeBot core contains no project-specific
business logic. Currently, orchestrator.py hardcodes /home/kozuka/Work paths,
Monitor-specific BOT_REGISTRY entries, and dialagram model names. This adapter
extracts those assumptions behind a pluggable interface so the same CodeBot
core can operate Monitor, Customer App A, or any future project.

Invariants
----------
- stdlib-only (abc, pathlib, dataclasses, json)
- Core modules MUST use ProjectAdapter for all path/config resolution
- Adapter implementations live in the project repository, not CodeBot core
- Changing projects means swapping the adapter, not modifying core
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ProjectPaths:
    repository_root: Path
    state_dir: Path
    logs_dir: Path
    docs_dir: Path
    issues_dir: Path
    adr_dir: Path
    modules_docs_dir: Path
    queue_file: Path
    api_contract: Path
    entrypoint: Path
    context_map: Path
    bugs_file: Path
    features_file: Path
    roadmap_file: Path
    constitution_file: Path
    project_config: Path


@dataclass(frozen=True)
class ProjectTestConfig:
    framework: str
    test_command: str
    test_directories: list[str]
    coverage_tool: str
    lint_tool: str
    type_checker: str
    type_check_command: str


@dataclass(frozen=True)
class DependencyPolicy:
    policy: str
    allowed_third_party: list[dict[str, str]]
    dependency_files: list[str]


@dataclass(frozen=True)
class AutonomyConfig:
    level: int
    human_approval_required_for: list[str]
    autonomous_allowed_for: list[str]


@dataclass(frozen=True)
class ComponentDef:
    name: str
    path: str
    component_type: str
    language: str
    description: str


class ProjectAdapter(ABC):
    @abstractmethod
    def project_name(self) -> str:
        ...

    @abstractmethod
    def paths(self) -> ProjectPaths:
        ...

    @abstractmethod
    def test_config(self) -> ProjectTestConfig:
        ...

    @abstractmethod
    def dependency_policy(self) -> DependencyPolicy:
        ...

    @abstractmethod
    def autonomy_config(self) -> AutonomyConfig:
        ...

    @abstractmethod
    def components(self) -> list[ComponentDef]:
        ...

    @abstractmethod
    def bot_registry(self) -> list[dict[str, Any]]:
        ...

    @abstractmethod
    def model_profiles(self) -> dict[str, dict[str, Any]]:
        ...

    @abstractmethod
    def tier_priority(self) -> dict[str, int]:
        ...

    @abstractmethod
    def prompt_directory(self) -> Path:
        ...

    @abstractmethod
    def api_runner_command(self, bot_name: str, prompt_file: str) -> list[str]:
        ...

    @abstractmethod
    def is_protected_path(self, path: str) -> bool:
        ...

    @abstractmethod
    def validate_project(self) -> list[str]:
        ...
